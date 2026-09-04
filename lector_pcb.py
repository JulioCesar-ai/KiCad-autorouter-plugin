"""
lector_pcb.py — Lector de archivos PCB para KiCad 10

Extrae componentes, pads y netlist del formato .kicad_pcb.
Solo procesa la capa F.Cu (enrutamiento de una sola capa).

Compatibilidad verificada con KiCad 10 (version 20260206):
  - Las redes se referencian por NOMBRE: (net "GND")
  - No se usan IDs numéricos de red en KiCad 10
  - Las capas se escriben entre comillas: (layer "F.Cu")
"""

import re
import math
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Estructuras de datos
# ─────────────────────────────────────────────────────────────────────────────

# Nombre de red reservado para pads sin red asignada (NC — not connected) o
# marcados "unconnected-*" por KiCad. Estos pads son cobre físico real: deben
# seguir siendo obstáculo para A* (de ahí que NUNCA se descarten al leer el
# archivo), pero no representan una red a enrutar ni participan del análisis
# de conectividad. Se filtran por este nombre en vez de descartarse en el
# parser porque el descarte temprano fue justamente el bug: un pad ausente
# del modelo tampoco entra en el mapa de obstáculos, y A* trazaba pistas
# pegadas a él sin respetar clearance (cortocircuito + puente de máscara
# reportados por el DRC nativo de KiCad).
RED_SIN_ASIGNAR = "__SIN_RED__"


@dataclass
class PadPCB:
    """Representa un pad de componente con coordenadas globales en mm."""
    referencia: str      # Referencia del componente (ej: U1, R1, C1)
    numero_pad: str      # Número o nombre del pad (ej: 1, 2, A, K)
    x: float             # Posición global X en mm
    y: float             # Posición global Y en mm
    ancho: float         # Ancho del pad en mm
    alto: float          # Alto del pad en mm
    nombre_red: str      # Nombre de la red conectada (ej: "GND", "VCC")
    es_smd: bool         # True = SMD (solo F.Cu), False = through-hole
    forma: str           # Forma: rect, roundrect, circle, oval

    def __repr__(self):
        return f"PadPCB({self.referencia}.{self.numero_pad} @ ({self.x:.3f},{self.y:.3f}) red={self.nombre_red!r})"


@dataclass
class SegmentoPCB:
    """Representa un segmento de pista ya enrutado en el archivo."""
    x_inicio: float
    y_inicio: float
    x_fin: float
    y_fin: float
    ancho: float
    capa: str
    nombre_red: str


@dataclass
class RedPCB:
    """Representa una red eléctrica con todos sus pads conectados."""
    nombre: str
    pads: List[PadPCB] = field(default_factory=list)

    def __repr__(self):
        return f"RedPCB({self.nombre!r}, {len(self.pads)} pads)"


@dataclass
class ZonaFCu:
    """
    Zona de cobre (copper pour) en F.Cu, con lo necesario para razonar sobre
    su relleno sin depender de `pcbnew`.

    `contorno` es el polígono declarado por el usuario, NO el relleno: el
    relleno lo calcula KiCad y depende de las pistas que haya alrededor, que
    es justamente lo que queremos predecir.
    """
    nombre_red: str
    contorno: List[Tuple[float, float]] = field(default_factory=list)
    clearance: float = 0.3          # (connect_pads (clearance X))
    min_thickness: float = 0.25     # (min_thickness X): cobre más fino no se rellena


@dataclass
class DatosPCB:
    """Datos completos extraídos del archivo .kicad_pcb."""
    nombre_archivo: str
    version_kicad: int                              # ej: 20260206
    redes: Dict[str, RedPCB]                        # nombre_red → RedPCB
    todos_los_pads: List[PadPCB]                    # Lista global de pads
    limite_tablero: Tuple[float, float, float, float]  # min_x, min_y, max_x, max_y
    segmentos_existentes: List[SegmentoPCB]         # Segmentos ya en el archivo
    contenido_original: str                         # Texto completo del archivo
    redes_con_zona_fcu: set = field(default_factory=set)  # Redes cubiertas por zona/cobre de relleno en F.Cu
    zonas_fcu: List['ZonaFCu'] = field(default_factory=list)  # Geometría de esas zonas


# ─────────────────────────────────────────────────────────────────────────────
# Tokenizador de S-expresiones KiCad
# ─────────────────────────────────────────────────────────────────────────────

def tokenizar(texto: str) -> List[str]:
    """
    Convierte texto de S-expresión KiCad en lista de tokens.

    Produce tokens de tres tipos:
      - '(' y ')' : delimitadores
      - '"..."'   : cadenas entre comillas (con las comillas incluidas)
      - 'word'    : palabras o números sin comillas
    """
    tokens = []
    i = 0
    n = len(texto)
    while i < n:
        c = texto[i]
        if c in (' ', '\t', '\n', '\r'):
            i += 1
        elif c == '(':
            tokens.append('(')
            i += 1
        elif c == ')':
            tokens.append(')')
            i += 1
        elif c == '"':
            # Cadena entre comillas — incluye comillas en el token
            j = i + 1
            while j < n and texto[j] != '"':
                if texto[j] == '\\':
                    j += 1  # Saltar carácter escapado
                j += 1
            tokens.append(texto[i:j + 1])
            i = j + 1
        else:
            # Palabra o número hasta espacio/paréntesis
            j = i
            while j < n and texto[j] not in (' ', '\t', '\n', '\r', '(', ')'):
                j += 1
            tokens.append(texto[i:j])
            i = j
    return tokens


def des_comillar(token: str) -> str:
    """Quita las comillas de un token tipo '"valor"' → 'valor'."""
    if token.startswith('"') and token.endswith('"'):
        return token[1:-1]
    return token


# ─────────────────────────────────────────────────────────────────────────────
# Parser principal
# ─────────────────────────────────────────────────────────────────────────────

def _calcular_posicion_global(
    lx: float, ly: float,
    fx: float, fy: float, fr_grados: float
) -> Tuple[float, float]:
    """
    Calcula la posición global de un pad dado su posición local y la rotación del footprint.

    KiCad usa Y hacia abajo en pantalla. Cuando se rota un footprint con un
    ángulo positivo, KiCad lo gira HORARIAMENTE en pantalla. En coordenadas
    matemáticas con Y-up esto sería antihorario, pero como almacenamos Y-down,
    la matriz de rotación efectiva es:

        x' =  x·cos(θ) + y·sin(θ)
        y' = -x·sin(θ) + y·cos(θ)

    Verificación:
      - rot=0  : (x,y) → (x,y)                ✓
      - rot=90 : (1,0) → (0,-1)  (al norte)   ✓ (en pantalla con Y abajo)
      - rot=180: (1,0) → (-1,0)               ✓
      - rot=270: (1,0) → (0, 1)  (al sur)     ✓
    """
    angle_rad = math.radians(fr_grados)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)
    gx = fx + lx * cos_a + ly * sin_a
    gy = fy - lx * sin_a + ly * cos_a
    return round(gx, 6), round(gy, 6)


def _extraer_numero(valor: str, defecto: float = 0.0) -> float:
    """Convierte string a float, devuelve defecto si falla."""
    try:
        return float(valor)
    except (ValueError, TypeError):
        return defecto


def _encontrar_bloque(contenido: str, inicio: int) -> int:
    """
    Dado el índice del '(' de apertura, retorna el índice del ')' de cierre
    que le corresponde (balanceado).
    """
    profundidad = 0
    i = inicio
    while i < len(contenido):
        if contenido[i] == '(':
            profundidad += 1
        elif contenido[i] == ')':
            profundidad -= 1
            if profundidad == 0:
                return i
        elif contenido[i] == '"':
            i += 1
            while i < len(contenido) and contenido[i] != '"':
                if contenido[i] == '\\':
                    i += 1
                i += 1
        i += 1
    return len(contenido) - 1


def _parsear_footprint(bloque: str) -> Tuple[Optional[str], List[PadPCB]]:
    """
    Parsea un bloque de footprint y retorna (referencia, lista_de_pads).

    El bloque comienza con '(footprint "nombre"' y contiene
    propiedades, líneas de dibujo y pads.
    """
    # Extraer nombre de referencia (propiedad "Reference")
    ref_match = re.search(
        r'\(property\s+"Reference"\s+"([^"]+)"',
        bloque
    )
    referencia = ref_match.group(1) if ref_match else "?"

    # Extraer posición y rotación del footprint: (at X Y [ROT])
    at_match = re.search(r'\(at\s+([\d.+-]+)\s+([\d.+-]+)(?:\s+([\d.+-]+))?\)', bloque)
    if not at_match:
        return referencia, []

    fx = _extraer_numero(at_match.group(1))
    fy = _extraer_numero(at_match.group(2))
    fr = _extraer_numero(at_match.group(3), 0.0)

    pads = []

    # Encontrar todos los bloques (pad ...)
    # Usamos regex para encontrar el inicio de cada pad
    for pad_start in re.finditer(r'\(pad\s+', bloque):
        inicio = pad_start.start()
        fin = _encontrar_bloque(bloque, inicio)
        bloque_pad = bloque[inicio:fin + 1]

        pad = _parsear_pad(bloque_pad, referencia, fx, fy, fr)
        if pad is not None:
            pads.append(pad)

    return referencia, pads


def _parsear_pad(bloque_pad: str, referencia: str,
                 fx: float, fy: float, fr: float) -> Optional[PadPCB]:
    """
    Parsea un bloque de pad individual.

    Formato KiCad 10:
      (pad "1" smd roundrect
        (at X Y [ROT])
        (size W H)
        (layers "F.Cu" "F.Mask" "F.Paste")
        (net "GND")
        ...
      )
    """
    # Número de pad: primer token después de '(pad '
    num_match = re.match(r'\(pad\s+"?([^"\s)]+)"?\s+(\w+)\s+(\w+)', bloque_pad)
    if not num_match:
        return None

    numero_pad = num_match.group(1)
    tipo_pad = num_match.group(2)    # smd, thru_hole, np_thru_hole
    forma_pad = num_match.group(3)   # rect, roundrect, circle, oval

    # Posición local: (at X Y [ROT])
    at_match = re.search(
        r'\(at\s+([\d.+-]+)\s+([\d.+-]+)(?:\s+([\d.+-]+))?\)',
        bloque_pad
    )
    if not at_match:
        return None

    lx = _extraer_numero(at_match.group(1))
    ly = _extraer_numero(at_match.group(2))

    # Tamaño: (size W H)
    size_match = re.search(
        r'\(size\s+([\d.+-]+)\s+([\d.+-]+)\)',
        bloque_pad
    )
    ancho = _extraer_numero(size_match.group(1)) if size_match else 1.0
    alto = _extraer_numero(size_match.group(2)) if size_match else 1.0

    # Red conectada (KiCad 10: solo nombre): (net "GND")
    net_match = re.search(r'\(net\s+"([^"]+)"\)', bloque_pad)
    nombre_red = net_match.group(1) if net_match else ""

    # Pad sin red asignada (NC) o "unconnected-*": sigue siendo cobre físico.
    # NO se descarta — se marca con RED_SIN_ASIGNAR para que quede en
    # `todos_los_pads` (mapa de obstáculos) pero fuera de `redes` (nada que
    # enrutar ni que contar como red en el reporte). Ver RED_SIN_ASIGNAR.
    if not nombre_red or nombre_red.startswith("unconnected-"):
        nombre_red = RED_SIN_ASIGNAR

    # Verificar que el pad está en F.Cu
    # SMD → solo su capa, through-hole → todas las capas (*.Cu)
    capas_match = re.search(r'\(layers\s+(.+?)\)', bloque_pad)
    if capas_match:
        capas_texto = capas_match.group(1)
        # Aceptar si tiene "F.Cu" o "*.Cu" (through-hole)
        if '"F.Cu"' not in capas_texto and '"*.Cu"' not in capas_texto:
            return None

    # Calcular posición global
    gx, gy = _calcular_posicion_global(lx, ly, fx, fy, fr)

    es_smd = (tipo_pad == "smd")

    return PadPCB(
        referencia=referencia,
        numero_pad=numero_pad,
        x=gx,
        y=gy,
        ancho=ancho,
        alto=alto,
        nombre_red=nombre_red,
        es_smd=es_smd,
        forma=forma_pad
    )


def _extraer_redes_con_zona_fcu(contenido: str) -> set:
    """
    Detecta qué redes tienen una zona de cobre (copper fill/pour) en F.Cu.

    Si una red tiene zona en F.Cu, sus pads ya están conectados eléctricamente
    a través del plano de cobre y NO deben enrutarse con pistas individuales.
    Ejemplo típico: la red GND en un PCB con plano de masa.

    Returns:
        Conjunto de nombres de redes con zona en F.Cu (ej: {'GND'})
    """
    return {z.nombre_red for z in _extraer_zonas_fcu(contenido)}


def _extraer_zonas_fcu(contenido: str) -> List['ZonaFCu']:
    """
    Extrae las zonas de cobre en F.Cu con su contorno y parámetros de relleno.

    Se toma el `(polygon (pts ...))` — el contorno DECLARADO — y no los
    `(filled_polygon ...)`, que son el resultado del último relleno hecho por
    KiCad y por lo tanto describen el estado anterior al enrutamiento que
    estamos evaluando.
    """
    zonas = []

    for match in re.finditer(r'\(zone\b', contenido):
        inicio = match.start()
        fin = _encontrar_bloque(contenido, inicio)
        bloque = contenido[inicio:fin + 1]

        # Solo zonas en F.Cu
        if '"F.Cu"' not in bloque:
            continue

        net_match = re.search(r'\(net\s+"([^"]+)"\)', bloque)
        if not net_match:
            continue

        zona = ZonaFCu(nombre_red=net_match.group(1))

        # Contorno: primer (polygon ...) del bloque. Se delimita con el mismo
        # balanceo de paréntesis que el resto del parser y NO con una regex
        # no codiciosa: `(.*?)\)\s*\)` se come el paréntesis de cierre del
        # último vértice y lo pierde silenciosamente (dejaba polígonos de 3
        # puntos donde el archivo declara 4).
        # `\(polygon\b` no matchea dentro de `(filled_polygon`, así que esto
        # toma el contorno declarado y no el relleno ya calculado.
        pol = re.search(r'\(polygon\b', bloque)
        if pol:
            fin_pol = _encontrar_bloque(bloque, pol.start())
            zona.contorno = [(float(a), float(b)) for a, b in
                             re.findall(r'\(xy\s+([-\d.]+)\s+([-\d.]+)\)',
                                        bloque[pol.start():fin_pol + 1])]

        m = re.search(r'\(connect_pads[^)]*\(clearance\s+([\d.]+)\)', bloque, re.DOTALL)
        if m:
            zona.clearance = float(m.group(1))
        m = re.search(r'\(min_thickness\s+([\d.]+)\)', bloque)
        if m:
            zona.min_thickness = float(m.group(1))

        zonas.append(zona)

    return zonas


def _parsear_segmentos(contenido: str) -> List[SegmentoPCB]:
    """
    Extrae todos los segmentos de pista del archivo PCB.

    Solo procesa segmentos en la capa F.Cu.
    """
    segmentos = []

    # Patrón para bloque completo de segmento
    patron = re.compile(r'\(segment\s[^)]*?\(uuid', re.DOTALL)

    for match in re.finditer(r'\(segment\b', contenido):
        inicio = match.start()
        fin = _encontrar_bloque(contenido, inicio)
        bloque = contenido[inicio:fin + 1]

        # Solo F.Cu
        if '"F.Cu"' not in bloque:
            continue

        start_m = re.search(r'\(start\s+([\d.+-]+)\s+([\d.+-]+)\)', bloque)
        end_m = re.search(r'\(end\s+([\d.+-]+)\s+([\d.+-]+)\)', bloque)
        width_m = re.search(r'\(width\s+([\d.+-]+)\)', bloque)
        net_m = re.search(r'\(net\s+"([^"]+)"\)', bloque)

        if not (start_m and end_m):
            continue

        segmentos.append(SegmentoPCB(
            x_inicio=_extraer_numero(start_m.group(1)),
            y_inicio=_extraer_numero(start_m.group(2)),
            x_fin=_extraer_numero(end_m.group(1)),
            y_fin=_extraer_numero(end_m.group(2)),
            ancho=_extraer_numero(width_m.group(1)) if width_m else 0.3,
            capa="F.Cu",
            nombre_red=net_m.group(1) if net_m else ""
        ))

    return segmentos


def _extraer_limite_tablero(contenido: str) -> Tuple[float, float, float, float]:
    """
    Extrae los límites del tablero desde Edge.Cuts (gr_poly o gr_rect o gr_line).

    Retorna (min_x, min_y, max_x, max_y) en mm.
    Si no encuentra Edge.Cuts, usa los límites de los pads.
    """
    xs, ys = [], []

    # Buscar polígono de borde: (gr_poly (pts (xy X Y) (xy X Y) ...) ... "Edge.Cuts")
    for match in re.finditer(r'\(gr_poly\b', contenido):
        inicio = match.start()
        fin = _encontrar_bloque(contenido, inicio)
        bloque = contenido[inicio:fin + 1]
        if '"Edge.Cuts"' not in bloque:
            continue
        for xy_m in re.finditer(r'\(xy\s+([\d.+-]+)\s+([\d.+-]+)\)', bloque):
            xs.append(float(xy_m.group(1)))
            ys.append(float(xy_m.group(2)))

    # Buscar líneas de borde: (gr_line ... "Edge.Cuts")
    for match in re.finditer(r'\(gr_line\b', contenido):
        inicio = match.start()
        fin = _encontrar_bloque(contenido, inicio)
        bloque = contenido[inicio:fin + 1]
        if '"Edge.Cuts"' not in bloque:
            continue
        for coord_match in re.finditer(r'\((start|end)\s+([\d.+-]+)\s+([\d.+-]+)\)', bloque):
            xs.append(float(coord_match.group(2)))
            ys.append(float(coord_match.group(3)))

    if xs and ys:
        return min(xs), min(ys), max(xs), max(ys)

    # Fallback: límites amplios
    return 0.0, 0.0, 200.0, 200.0


# ─────────────────────────────────────────────────────────────────────────────
# Función principal de lectura
# ─────────────────────────────────────────────────────────────────────────────

def leer_pcb(ruta_archivo: str) -> DatosPCB:
    """
    Lee un archivo .kicad_pcb y retorna los datos estructurados.

    Solo procesa la capa F.Cu para enrutamiento de una sola capa.
    Compatible con KiCad 10 (version 20260206+).

    Args:
        ruta_archivo: Ruta al archivo .kicad_pcb

    Returns:
        DatosPCB con todos los datos extraídos

    Raises:
        FileNotFoundError: Si el archivo no existe
        ValueError: Si el archivo no es un PCB KiCad válido
    """
    with open(ruta_archivo, 'r', encoding='utf-8') as f:
        contenido = f.read()

    if '(kicad_pcb' not in contenido:
        raise ValueError(f"El archivo no parece ser un PCB KiCad válido: {ruta_archivo}")

    # Extraer versión
    ver_match = re.search(r'\(version\s+(\d+)\)', contenido)
    version = int(ver_match.group(1)) if ver_match else 0

    print(f"[Lector] Leyendo PCB: {ruta_archivo}")
    print(f"[Lector] Versión KiCad: {version}")

    # Extraer todos los footprints
    todos_los_pads: List[PadPCB] = []
    redes: Dict[str, RedPCB] = {}

    for fp_match in re.finditer(r'\(footprint\b', contenido):
        inicio = fp_match.start()
        fin = _encontrar_bloque(contenido, inicio)
        bloque_fp = contenido[inicio:fin + 1]

        _, pads = _parsear_footprint(bloque_fp)
        todos_los_pads.extend(pads)  # incluye los RED_SIN_ASIGNAR: son obstáculo

        for pad in pads:
            if pad.nombre_red == RED_SIN_ASIGNAR:
                continue  # no es una red a enrutar ni a contar en el reporte
            if pad.nombre_red not in redes:
                redes[pad.nombre_red] = RedPCB(nombre=pad.nombre_red)
            redes[pad.nombre_red].pads.append(pad)

    # Extraer segmentos existentes
    segmentos = _parsear_segmentos(contenido)

    # Extraer límites del tablero
    limite = _extraer_limite_tablero(contenido)

    # Detectar redes cubiertas por zonas de cobre en F.Cu
    zonas_fcu = _extraer_zonas_fcu(contenido)
    redes_zona = {z.nombre_red for z in zonas_fcu}
    if redes_zona:
        print(f"[Lector] Redes con zona de cobre en F.Cu (no se enrutaran): {redes_zona}")

    print(f"[Lector] Componentes encontrados: {_contar_componentes(todos_los_pads)}")
    print(f"[Lector] Pads en F.Cu: {len(todos_los_pads)}")
    print(f"[Lector] Redes: {list(redes.keys())}")
    print(f"[Lector] Segmentos existentes en F.Cu: {len(segmentos)}")
    print(f"[Lector] Limites: X[{limite[0]:.2f}-{limite[2]:.2f}] Y[{limite[1]:.2f}-{limite[3]:.2f}] mm")

    return DatosPCB(
        nombre_archivo=ruta_archivo,
        version_kicad=version,
        redes=redes,
        todos_los_pads=todos_los_pads,
        limite_tablero=limite,
        segmentos_existentes=segmentos,
        contenido_original=contenido,
        redes_con_zona_fcu=redes_zona,
        zonas_fcu=zonas_fcu
    )


def _contar_componentes(pads: List[PadPCB]) -> int:
    """Cuenta cuántos componentes únicos hay en la lista de pads."""
    return len(set(p.referencia for p in pads))


def _agrupar_pads_por_conectividad(pads: List[PadPCB],
                                   segmentos_red: List[SegmentoPCB],
                                   tolerancia: float = 0.15) -> List[List[PadPCB]]:
    """
    Agrupa los pads de una red según las conexiones existentes en F.Cu.

    Si el usuario ya enrutó manualmente parte de la red, los pads que están
    conectados a través de esos segmentos forman un grupo. La función devuelve
    una lista de grupos: cada grupo contiene pads que ya están conectados
    entre sí (no necesitan re-enrutamiento).

    Algoritmo: union-find sobre todos los "puntos de unión" (pads + extremos
    de segmentos). Dos puntos se unen si están a menos de ``tolerancia`` mm.

    También detecta **uniones en T**: un punto que cae sobre el CUERPO de un
    segmento (no sobre sus extremos) queda unido a él. KiCad considera esto una
    conexión eléctrica válida, así que ignorarlo daría falsos "red abierta"
    cuando una derivación se engancha a mitad de una pista.

    Args:
        pads: Pads de la red
        segmentos_red: Segmentos F.Cu que pertenecen a esta red
        tolerancia: Distancia máxima para considerar dos puntos coincidentes

    Returns:
        Lista de grupos de pads. Cada grupo es una lista de pads ya conectados.
        Si no hay segmentos, devuelve [[pad1], [pad2], ...] (cada pad aislado).
    """
    if not segmentos_red:
        return [[p] for p in pads]

    # Recolectar todos los puntos: primero los pads (índices 0..n-1),
    # luego los extremos de segmentos (índices n..)
    puntos = [(p.x, p.y) for p in pads]
    for s in segmentos_red:
        puntos.append((s.x_inicio, s.y_inicio))
        puntos.append((s.x_fin, s.y_fin))

    n_pads = len(pads)

    # Union-find
    parent = list(range(len(puntos)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    def cerca(p1, p2):
        return abs(p1[0] - p2[0]) < tolerancia and abs(p1[1] - p2[1]) < tolerancia

    def distancia_punto_segmento(px, py, x1, y1, x2, y2) -> float:
        """Distancia mínima del punto (px,py) al segmento (x1,y1)-(x2,y2)."""
        dx, dy = x2 - x1, y2 - y1
        den = dx * dx + dy * dy
        if den < 1e-12:
            return math.hypot(px - x1, py - y1)
        t = ((px - x1) * dx + (py - y1) * dy) / den
        t = max(0.0, min(1.0, t))
        return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))

    # Unir cada segmento: extremos del segmento → mismo grupo
    for k, s in enumerate(segmentos_red):
        idx_ini = n_pads + 2 * k
        idx_fin = n_pads + 2 * k + 1
        union(idx_ini, idx_fin)

    # Uniones en T: cualquier punto (pad o extremo de otro segmento) que caiga
    # sobre el cuerpo de un segmento queda unido a él.
    for k, s in enumerate(segmentos_red):
        idx_seg = n_pads + 2 * k
        for i, (px, py) in enumerate(puntos):
            if i in (idx_seg, idx_seg + 1):
                continue  # sus propios extremos ya están unidos
            if distancia_punto_segmento(px, py,
                                        s.x_inicio, s.y_inicio,
                                        s.x_fin, s.y_fin) < tolerancia:
                union(i, idx_seg)

    # Unir cada pad con cualquier extremo de segmento cercano
    for i in range(n_pads):
        for j in range(n_pads, len(puntos)):
            if cerca(puntos[i], puntos[j]):
                union(i, j)

    # Unir extremos de segmentos cercanos entre sí (segmentos que se tocan)
    for i in range(n_pads, len(puntos)):
        for j in range(i + 1, len(puntos)):
            if cerca(puntos[i], puntos[j]):
                union(i, j)

    # Agrupar pads por su raíz en union-find
    grupos_dict = {}
    for i in range(n_pads):
        raiz = find(i)
        grupos_dict.setdefault(raiz, []).append(pads[i])

    return list(grupos_dict.values())


def obtener_conexiones_a_enrutar(datos: DatosPCB) -> List[Tuple[PadPCB, PadPCB]]:
    """
    Genera la lista de pares de pads que necesitan ser enrutados.

    Para redes con N pads, genera N-1 conexiones usando árbol de expansión
    mínima (MST) simplificado: conecta cada pad al más cercano ya conectado.

    **Respeta el enrutamiento manual existente**: si el usuario ya conectó
    parte de la red con segmentos en F.Cu, esos pads se agrupan y solo se
    enruta lo que falta entre grupos. Si toda la red ya está conectada,
    no se genera ninguna conexión.

    Args:
        datos: Datos PCB leídos

    Returns:
        Lista de tuplas (pad_origen, pad_destino) a enrutar
    """
    from collections import defaultdict

    # Indexar segmentos existentes por nombre de red
    segmentos_por_red: Dict[str, List[SegmentoPCB]] = defaultdict(list)
    for s in datos.segmentos_existentes:
        segmentos_por_red[s.nombre_red].append(s)

    conexiones = []

    for nombre_red, red in datos.redes.items():
        if nombre_red in datos.redes_con_zona_fcu:
            print(f"[Conexiones] Omitiendo red '{nombre_red}' — cubierta por zona de cobre en F.Cu")
            continue
        if len(red.pads) < 2:
            continue

        # Agrupar pads según conexiones manuales existentes
        segs_red = segmentos_por_red.get(nombre_red, [])
        grupos = _agrupar_pads_por_conectividad(red.pads, segs_red)

        if len(grupos) == 1:
            # Toda la red ya está conectada manualmente
            if segs_red:
                print(f"[Conexiones] Red '{nombre_red}' ya enrutada manualmente "
                      f"({len(segs_red)} segs) — se preserva")
            continue

        if segs_red:
            print(f"[Conexiones] Red '{nombre_red}' parcialmente enrutada: "
                  f"{len(grupos)} grupos de pads, {len(segs_red)} segs existentes")

        # MST entre GRUPOS: cada nodo del MST es un grupo. La distancia entre
        # dos grupos es la mínima distancia entre cualquier pad de uno y
        # cualquier pad del otro.
        grupos_conectados = [grupos[0]]
        grupos_restantes = list(grupos[1:])

        while grupos_restantes:
            mejor_dist = float('inf')
            mejor_par = None
            mejor_idx = 0

            for i, gr_libre in enumerate(grupos_restantes):
                for gr_conectado in grupos_conectados:
                    for pad_a in gr_libre:
                        for pad_b in gr_conectado:
                            dx = pad_a.x - pad_b.x
                            dy = pad_a.y - pad_b.y
                            dist = math.sqrt(dx * dx + dy * dy)
                            if dist < mejor_dist:
                                mejor_dist = dist
                                mejor_par = (pad_b, pad_a)
                                mejor_idx = i

            if mejor_par:
                conexiones.append(mejor_par)
                grupos_conectados.append(grupos_restantes[mejor_idx])
                grupos_restantes.pop(mejor_idx)

    return conexiones


# ─────────────────────────────────────────────────────────────────────────────
# Ejecución directa para prueba
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    ruta = sys.argv[1] if len(sys.argv) > 1 else "casos_prueba/mi_pcb.kicad_pcb"
    datos = leer_pcb(ruta)

    print("\n=== RESUMEN ===")
    for nombre, red in datos.redes.items():
        print(f"  Red '{nombre}': {len(red.pads)} pads")
        for pad in red.pads:
            print(f"    {pad}")

    conexiones = obtener_conexiones_a_enrutar(datos)
    print(f"\nConexiones a enrutar (MST): {len(conexiones)}")
    for origen, destino in conexiones:
        dx = origen.x - destino.x
        dy = origen.y - destino.y
        dist = math.sqrt(dx*dx + dy*dy)
        print(f"  {origen.referencia}.{origen.numero_pad} → {destino.referencia}.{destino.numero_pad}"
              f" ({dist:.2f} mm) [{origen.nombre_red}]")
