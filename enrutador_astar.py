"""
enrutador_astar.py — Enrutador A* para PCBs de una sola capa (F.Cu)

Implementa el algoritmo A* (A-estrella) puro en Python sobre una cuadrícula
de 0.25mm con movimientos en 8 direcciones (horizontal, vertical y diagonal).

Características:
  - Cuadrícula configurable (por defecto 0.25mm)
  - 8 direcciones de movimiento (incluye diagonales a 45°)
  - Heurística de Chebyshev (óptima para movimiento en 8 direcciones)
  - Mapa de obstáculos actualizable entre rutas
  - Respeto de clearance mínimo configurable
  - Penalización de cambios de dirección (prefiere rutas rectas)
"""

import heapq
import math
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Set

from lector_pcb import PadPCB, SegmentoPCB


# ─────────────────────────────────────────────────────────────────────────────
# Constantes de movimiento
# ─────────────────────────────────────────────────────────────────────────────

# 8 direcciones: (delta_col, delta_fila, costo_base)
# Cardinal = 1.0, Diagonal = sqrt(2) ≈ 1.414
MOVIMIENTOS = [
    ( 1,  0, 1.000),   # Derecha
    (-1,  0, 1.000),   # Izquierda
    ( 0,  1, 1.000),   # Abajo
    ( 0, -1, 1.000),   # Arriba
    ( 1,  1, 1.414),   # Diagonal abajo-derecha
    (-1,  1, 1.414),   # Diagonal abajo-izquierda
    ( 1, -1, 1.414),   # Diagonal arriba-derecha
    (-1, -1, 1.414),   # Diagonal arriba-izquierda
]

# Penalización por cambio de dirección (en unidades de celdas)
COSTO_GIRO = 2.0

# Desvío (longitud_enrutada / distancia_directa) a partir del cual una ruta se
# considera PATOLÓGICA: rodea gran parte de la placa en vez de ir a destino.
# Es un único umbral para todo el sistema — lo usan el conteo de métricas, el
# marcado en el reporte y el disparo del rip-up. Tenerlo repetido con valores
# distintos haría que el reporte y el enrutador discrepen sobre qué es "malo".
UMBRAL_DESVIO_PATOLOGICO = 3.0

# Ganancia mínima (mm) para que valga la pena rehacer una red entera por una
# ruta patológica. Se mide contra el camino ideal (placa despejada), no se
# estima: un desvío de 5x en una conexión de 2mm desperdicia 8mm y no justifica
# el trabajo, mientras que 4x en una de 15mm desperdicia 45mm y sí.
GANANCIA_MINIMA_RIPUP_MM = 10.0


# ─────────────────────────────────────────────────────────────────────────────
# Nodo A*
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(order=True)
class NodoAstar:
    """Nodo de la cola de prioridad del algoritmo A*."""
    f: float              # f = g + h (costo total estimado)
    g: float = field(compare=False)  # Costo real desde inicio
    col: int = field(compare=False)  # Columna en la cuadrícula
    fila: int = field(compare=False) # Fila en la cuadrícula
    dir_col: int = field(compare=False)  # Dirección actual (delta columna)
    dir_fila: int = field(compare=False) # Dirección actual (delta fila)
    padre: Optional[object] = field(compare=False, default=None)  # Nodo padre


# ─────────────────────────────────────────────────────────────────────────────
# Enrutador A*
# ─────────────────────────────────────────────────────────────────────────────

class EnrutadorAstar:
    """
    Enrutador de pistas para PCB de una sola capa usando el algoritmo A*.

    Trabaja sobre una cuadrícula binaria donde cada celda puede ser:
      - Libre (0): disponible para enrutar
      - Obstáculo (1): ocupado por un pad o pista existente

    Uso típico:
        enrutador = EnrutadorAstar(limite_tablero, paso=0.25)
        enrutador.agregar_pads_como_obstaculos(todos_los_pads, clearance=0.15)
        segmentos = enrutador.enrutar_conexion(pad_origen, pad_destino)
        enrutador.marcar_ruta_como_obstaculo(segmentos, ancho=0.3, clearance=0.15)
    """

    def __init__(self,
                 limite_tablero: Tuple[float, float, float, float],
                 paso_cuadricula: float = 0.25,
                 max_iteraciones: int = 500_000):
        """
        Inicializa el enrutador con el tamaño del tablero.

        Args:
            limite_tablero: (min_x, min_y, max_x, max_y) en mm
            paso_cuadricula: Resolución de la cuadrícula en mm
            max_iteraciones: Límite de iteraciones A* para evitar bucles infinitos
        """
        self.min_x, self.min_y, self.max_x, self.max_y = limite_tablero
        self.paso = paso_cuadricula
        self.max_iter = max_iteraciones

        # Dimensiones de la cuadrícula
        self.num_cols = int((self.max_x - self.min_x) / self.paso) + 2
        self.num_filas = int((self.max_y - self.min_y) / self.paso) + 2

        # Mapa de obstáculos: set de celdas bloqueadas (col, fila)
        self.obstaculos: Set[Tuple[int, int]] = set()

        # Costos de proximidad a stubs: penaliza celdas cerca de pads aún no
        # enrutados para que A* evite bloquear conexiones futuras.
        # Clave: (col, fila), Valor: costo adicional a sumar a g.
        self.costos_proximidad: Dict[Tuple[int, int], float] = {}

        print(f"[A*] Cuadrícula: {self.num_cols}×{self.num_filas} celdas "
              f"({self.num_cols * self.paso:.1f}×{self.num_filas * self.paso:.1f} mm)")

    # ── Conversión de coordenadas ────────────────────────────────────────────

    def mm_a_celda(self, x: float, y: float) -> Tuple[int, int]:
        """Convierte coordenadas en mm a índices de celda (col, fila)."""
        col = int(round((x - self.min_x) / self.paso))
        fila = int(round((y - self.min_y) / self.paso))
        return col, fila

    def celda_a_mm(self, col: int, fila: int) -> Tuple[float, float]:
        """Convierte índices de celda a coordenadas en mm."""
        x = self.min_x + col * self.paso
        y = self.min_y + fila * self.paso
        return round(x, 6), round(y, 6)

    def _celda_valida(self, col: int, fila: int) -> bool:
        """Verifica que una celda esté dentro de los límites de la cuadrícula."""
        return 0 <= col < self.num_cols and 0 <= fila < self.num_filas

    # ── Construcción del mapa de obstáculos ─────────────────────────────────

    def _celdas_de_rectangulo(self, cx: float, cy: float,
                               ancho: float, alto: float,
                               margen: float = 0.0) -> List[Tuple[int, int]]:
        """
        Retorna todas las celdas que ocupa un rectángulo con margen adicional.

        Args:
            cx, cy: Centro del rectángulo en mm
            ancho, alto: Dimensiones en mm
            margen: Clearance adicional en mm
        """
        mitad_w = (ancho / 2.0) + margen
        mitad_h = (alto / 2.0) + margen

        col_min, fila_min = self.mm_a_celda(cx - mitad_w, cy - mitad_h)
        col_max, fila_max = self.mm_a_celda(cx + mitad_w, cy + mitad_h)

        celdas = []
        for col in range(col_min - 1, col_max + 2):
            for fila in range(fila_min - 1, fila_max + 2):
                if self._celda_valida(col, fila):
                    celdas.append((col, fila))
        return celdas

    def _celdas_de_segmento(self, x1: float, y1: float,
                             x2: float, y2: float,
                             ancho: float, margen: float = 0.0) -> List[Tuple[int, int]]:
        """
        Retorna las celdas que ocupa un segmento de pista con margen.

        Usa el algoritmo de Bresenham para recorrer el segmento celda a celda,
        luego expande cada celda por el radio (ancho/2 + margen).
        """
        col1, fila1 = self.mm_a_celda(x1, y1)
        col2, fila2 = self.mm_a_celda(x2, y2)

        radio_celdas = int(math.ceil((ancho / 2.0 + margen) / self.paso)) + 1
        celdas = set()

        # Bresenham para recorrer el segmento
        dc = abs(col2 - col1)
        df = abs(fila2 - fila1)
        sc = 1 if col2 > col1 else -1
        sf = 1 if fila2 > fila1 else -1
        col, fila = col1, fila1
        err = dc - df

        while True:
            # Expandir por radio
            for dc2 in range(-radio_celdas, radio_celdas + 1):
                for df2 in range(-radio_celdas, radio_celdas + 1):
                    c = col + dc2
                    f = fila + df2
                    if self._celda_valida(c, f):
                        celdas.add((c, f))

            if col == col2 and fila == fila2:
                break
            e2 = 2 * err
            if e2 > -df:
                err -= df
                col += sc
            if e2 < dc:
                err += dc
                fila += sf

        return list(celdas)

    def agregar_pads_como_obstaculos(self,
                                     pads: List[PadPCB],
                                     clearance: float = 0.2,
                                     red_a_ignorar: str = "") -> None:
        """
        Marca los pads como obstáculos en el mapa.

        El margen de clearance se agrega al tamaño del pad. Si red_a_ignorar
        está definida, los pads de esa red se marcan con menor margen para
        permitir que la ruta los toque.

        Args:
            pads: Lista de pads a marcar
            clearance: Distancia mínima a respetar en mm
            red_a_ignorar: Nombre de red cuyos pads son los terminales de la ruta actual
        """
        for pad in pads:
            if pad.nombre_red == red_a_ignorar:
                # Terminal de la ruta actual: solo el área del pad, sin clearance
                celdas = self._celdas_de_rectangulo(pad.x, pad.y, pad.ancho, pad.alto, 0.0)
            else:
                # Obstáculo con clearance completo
                celdas = self._celdas_de_rectangulo(pad.x, pad.y, pad.ancho, pad.alto, clearance)
            self.obstaculos.update(celdas)

    def marcar_ruta_como_obstaculo(self,
                                   segmentos_ruta: List[dict],
                                   ancho_pista: float = 0.3,
                                   clearance: float = 0.2) -> None:
        """
        Marca una ruta recién enrutada como obstáculo para futuras rutas.

        Args:
            segmentos_ruta: Lista de dicts {'x1', 'y1', 'x2', 'y2'}
            ancho_pista: Ancho de la pista en mm
            clearance: Clearance a añadir en mm
        """
        for seg in segmentos_ruta:
            celdas = self._celdas_de_segmento(
                seg['x1'], seg['y1'], seg['x2'], seg['y2'],
                ancho_pista, clearance
            )
            self.obstaculos.update(celdas)

    def marcar_segmentos_existentes(self,
                                    segmentos: List[SegmentoPCB],
                                    clearance: float = 0.2) -> None:
        """
        Marca los segmentos existentes del PCB como obstáculos.

        Args:
            segmentos: Segmentos leídos del archivo PCB
            clearance: Clearance a añadir en mm
        """
        for seg in segmentos:
            celdas = self._celdas_de_segmento(
                seg.x_inicio, seg.y_inicio,
                seg.x_fin, seg.y_fin,
                seg.ancho, clearance
            )
            self.obstaculos.update(celdas)

    def limpiar_obstaculos(self) -> None:
        """Elimina todos los obstáculos del mapa (inicio limpio)."""
        self.obstaculos.clear()

    def agregar_costos_proximidad_stubs(self,
                                        pads_futuros: List[PadPCB],
                                        radio_mm: float = 2.0,
                                        costo_max: float = 5.0) -> None:
        """
        Añade costos suaves alrededor de pads que aún no han sido enrutados.

        Objetivo: penalizar rutas que pasen cerca de pads futuros, para que A*
        prefiera caminos que no bloqueen las conexiones que vienen después.

        El costo decae linealmente desde costo_max (en el centro del pad) hasta
        0 en el borde del radio:
            costo(d) = (1 - d / radio_mm) * costo_max   si d < radio_mm

        Este costo se SUMA al costo g en A*, NO reemplaza. Las celdas de los
        propios terminales actuales no se penalizan (A* los desbloquea antes).

        Args:
            pads_futuros: Pads de conexiones aún no enrutadas
            radio_mm: Radio de influencia en mm (default 2.0 mm)
            costo_max: Penalización máxima en el centro (default 5.0 celdas)
        """
        radio_celdas = int(math.ceil(radio_mm / self.paso))

        # Deduplicar por posición (un pad puede aparecer en varias conexiones)
        posiciones_vistas: Set[Tuple[int, int]] = set()

        for pad in pads_futuros:
            gcx, gcy = self.mm_a_celda(pad.x, pad.y)

            if (gcx, gcy) in posiciones_vistas:
                continue
            posiciones_vistas.add((gcx, gcy))

            for dc in range(-radio_celdas, radio_celdas + 1):
                for df in range(-radio_celdas, radio_celdas + 1):
                    c = gcx + dc
                    f = gcy + df

                    if not self._celda_valida(c, f):
                        continue

                    # Distancia en mm desde esta celda al centro del pad
                    dist_mm = math.hypot(dc * self.paso, df * self.paso)

                    if dist_mm >= radio_mm:
                        continue

                    costo = (1.0 - dist_mm / radio_mm) * costo_max

                    # Tomar el máximo si varios pads afectan la misma celda
                    clave = (c, f)
                    if costo > self.costos_proximidad.get(clave, 0.0):
                        self.costos_proximidad[clave] = costo

    def limpiar_costos_proximidad(self) -> None:
        """Elimina todos los costos de proximidad (llamar antes de cada conexión)."""
        self.costos_proximidad.clear()

    # ── Heurística ──────────────────────────────────────────────────────────

    @staticmethod
    def _heuristica_chebyshev(col1: int, fila1: int,
                               col2: int, fila2: int) -> float:
        """
        Heurística de Chebyshev: óptima para movimiento en 8 direcciones.

        h(n) = max(|Δcol|, |Δfila|)

        Es admisible porque el movimiento diagonal tiene costo √2 ≈ 1.414,
        pero la heurística usa 1.0 por celda, nunca sobreestima.
        """
        dc = abs(col2 - col1)
        df = abs(fila2 - fila1)
        return max(dc, df) + (math.sqrt(2) - 1) * min(dc, df)

    # ── Algoritmo A* ────────────────────────────────────────────────────────

    def enrutar_conexion(self,
                         pad_origen: PadPCB,
                         pad_destino: PadPCB,
                         ancho_pista: float = 0.3,
                         clearance: float = 0.2) -> Optional[List[dict]]:
        """
        Encuentra la ruta óptima entre dos pads usando A*.

        Temporalmente desbloquea las celdas de los pads origen y destino
        para permitir que la ruta llegue hasta ellos.

        Args:
            pad_origen: Pad de inicio
            pad_destino: Pad de destino
            ancho_pista: Ancho de pista a enrutar en mm
            clearance: Clearance mínimo en mm

        Returns:
            Lista de segmentos {'x1','y1','x2','y2','red'} o None si falla
        """
        col_ini, fila_ini = self.mm_a_celda(pad_origen.x, pad_origen.y)
        col_fin, fila_fin = self.mm_a_celda(pad_destino.x, pad_destino.y)

        # Si ya están en la misma celda, conexión trivial
        if col_ini == col_fin and fila_ini == fila_fin:
            return [{
                'x1': pad_origen.x, 'y1': pad_origen.y,
                'x2': pad_destino.x, 'y2': pad_destino.y,
                'red': pad_origen.nombre_red
            }]

        # Desbloquear temporalmente celdas del destino para poder alcanzarlo
        celdas_destino = self._celdas_de_rectangulo(
            pad_destino.x, pad_destino.y,
            pad_destino.ancho, pad_destino.alto, 0.0
        )
        celdas_origen = self._celdas_de_rectangulo(
            pad_origen.x, pad_origen.y,
            pad_origen.ancho, pad_origen.alto, 0.0
        )
        obstaculos_backup = self.obstaculos - set(celdas_destino) - set(celdas_origen)

        resultado = self._astar(
            col_ini, fila_ini,
            col_fin, fila_fin,
            obstaculos_backup,
            pad_origen.nombre_red,
            pad_origen.x, pad_origen.y,
            pad_destino.x, pad_destino.y
        )

        return resultado

    def celdas_centro_segmento(self, x1: float, y1: float,
                                x2: float, y2: float) -> Set[Tuple[int, int]]:
        """
        Celdas de la línea CENTRAL de un segmento, sin expandir por ancho.

        Se usa para construir el conjunto de destinos de una derivación que se
        engancha a una pista ya trazada de su propia red: al terminar sobre la
        línea central, el punto final queda a menos de medio paso de cuadrícula
        (≤0.125mm con paso 0.25mm) del eje de la pista, dentro de la tolerancia
        de conectividad (0.15mm).
        """
        col1, fila1 = self.mm_a_celda(x1, y1)
        col2, fila2 = self.mm_a_celda(x2, y2)
        celdas: Set[Tuple[int, int]] = set()

        dc = abs(col2 - col1)
        df = abs(fila2 - fila1)
        sc = 1 if col2 > col1 else -1
        sf = 1 if fila2 > fila1 else -1
        col, fila = col1, fila1
        err = dc - df

        while True:
            if self._celda_valida(col, fila):
                celdas.add((col, fila))
            if col == col2 and fila == fila2:
                break
            e2 = 2 * err
            if e2 > -df:
                err -= df
                col += sc
            if e2 < dc:
                err += dc
                fila += sf
        return celdas

    def enrutar_a_objetivos(self,
                            pad_origen: PadPCB,
                            celdas_objetivo: Set[Tuple[int, int]],
                            ancho_pista: float = 0.3,
                            clearance: float = 0.2) -> Optional[List[dict]]:
        """
        Enruta desde un pad hasta CUALQUIER celda de un conjunto de destinos.

        Es el mecanismo de "tap": una derivación de una red multipunto no tiene
        por qué alcanzar un pad lejano concreto, le basta con tocar el cobre ya
        trazado de su propia red en el punto más conveniente. A* termina al
        alcanzar la primera celda del conjunto.

        Returns:
            Lista de segmentos, [] si el pad YA está sobre el cobre destino
            (nada que enrutar), o None si no hay camino. Distinga [] de None:
            ambos son falsy pero significan cosas opuestas.
        """
        col_ini, fila_ini = self.mm_a_celda(pad_origen.x, pad_origen.y)
        if (col_ini, fila_ini) in celdas_objetivo:
            return []

        celdas_origen = self._celdas_de_rectangulo(
            pad_origen.x, pad_origen.y, pad_origen.ancho, pad_origen.alto, 0.0)
        obstaculos_efectivos = (self.obstaculos
                                - set(celdas_origen)
                                - celdas_objetivo)

        return self._astar(
            col_ini, fila_ini, col_ini, fila_ini,
            obstaculos_efectivos, pad_origen.nombre_red,
            pad_origen.x, pad_origen.y, pad_origen.x, pad_origen.y,
            celdas_objetivo=celdas_objetivo
        )

    def _astar(self,
               col_ini: int, fila_ini: int,
               col_fin: int, fila_fin: int,
               obstaculos: Set[Tuple[int, int]],
               nombre_red: str,
               x_inicio: float, y_inicio: float,
               x_fin: float, y_fin: float,
               celdas_objetivo: Optional[Set[Tuple[int, int]]] = None) -> Optional[List[dict]]:
        """
        Implementación interna del algoritmo A*.

        Usa cola de prioridad (heap mínimo) con el costo f = g + h.
        Registra la dirección actual para penalizar giros.

        Si `celdas_objetivo` está definido, la búsqueda termina al alcanzar
        CUALQUIER celda de ese conjunto (modo multi-objetivo) y la heurística
        pasa a ser la distancia a la caja envolvente de los objetivos — una
        cota inferior admisible, ya que ningún objetivo puede estar más cerca
        que su propia caja.

        Returns:
            Lista de segmentos o None si no hay camino.
        """
        multiobjetivo = celdas_objetivo is not None and len(celdas_objetivo) > 0

        if multiobjetivo:
            cols = [c for c, _ in celdas_objetivo]
            filas = [f for _, f in celdas_objetivo]
            bb_cmin, bb_cmax = min(cols), max(cols)
            bb_fmin, bb_fmax = min(filas), max(filas)

            def _h(col: int, fila: int) -> float:
                dc = max(bb_cmin - col, 0, col - bb_cmax)
                df = max(bb_fmin - fila, 0, fila - bb_fmax)
                return max(dc, df) + (math.sqrt(2) - 1) * min(dc, df)
        else:
            def _h(col: int, fila: int) -> float:
                return self._heuristica_chebyshev(col, fila, col_fin, fila_fin)

        # Cola de prioridad: (f, nodo)
        cola = []
        nodo_inicio = NodoAstar(
            f=_h(col_ini, fila_ini),
            g=0.0,
            col=col_ini, fila=fila_ini,
            dir_col=0, dir_fila=0,
            padre=None
        )
        heapq.heappush(cola, nodo_inicio)

        # Costo mínimo conocido para cada celda
        g_conocido: Dict[Tuple[int, int], float] = {(col_ini, fila_ini): 0.0}

        iteraciones = 0

        while cola:
            iteraciones += 1
            if iteraciones > self.max_iter:
                print(f"[A*] ADVERTENCIA: Límite de {self.max_iter} iteraciones alcanzado")
                return None

            nodo_actual = heapq.heappop(cola)

            # ¿Llegamos al destino?
            if multiobjetivo:
                llego = (nodo_actual.col, nodo_actual.fila) in celdas_objetivo
            else:
                llego = (nodo_actual.col == col_fin and nodo_actual.fila == fila_fin)

            if llego:
                print(f"[A*] Ruta encontrada en {iteraciones} iteraciones")
                if multiobjetivo:
                    # El extremo final es la celda alcanzada del cobre propio,
                    # no un centro de pad.
                    x_meta, y_meta = self.celda_a_mm(nodo_actual.col, nodo_actual.fila)
                else:
                    x_meta, y_meta = x_fin, y_fin
                return self._reconstruir_ruta(nodo_actual, nombre_red,
                                              x_inicio, y_inicio, x_meta, y_meta)

            # Explorar vecinos en 8 direcciones
            for dc, df, costo_mov in MOVIMIENTOS:
                nueva_col = nodo_actual.col + dc
                nueva_fila = nodo_actual.fila + df

                if not self._celda_valida(nueva_col, nueva_fila):
                    continue

                if (nueva_col, nueva_fila) in obstaculos:
                    continue

                # Penalizar giro de dirección
                costo_giro = 0.0
                if (nodo_actual.dir_col != 0 or nodo_actual.dir_fila != 0):
                    if dc != nodo_actual.dir_col or df != nodo_actual.dir_fila:
                        costo_giro = COSTO_GIRO

                # Costo de proximidad a stubs futuros (suave, no bloquea)
                costo_stub = self.costos_proximidad.get((nueva_col, nueva_fila), 0.0)

                nuevo_g = nodo_actual.g + costo_mov + costo_giro + costo_stub

                clave = (nueva_col, nueva_fila)
                if nuevo_g >= g_conocido.get(clave, float('inf')):
                    continue

                g_conocido[clave] = nuevo_g
                h = _h(nueva_col, nueva_fila)
                nuevo_nodo = NodoAstar(
                    f=nuevo_g + h,
                    g=nuevo_g,
                    col=nueva_col, fila=nueva_fila,
                    dir_col=dc, dir_fila=df,
                    padre=nodo_actual
                )
                heapq.heappush(cola, nuevo_nodo)

        print(f"[A*] No se encontró ruta después de {iteraciones} iteraciones")
        return None

    def _reconstruir_ruta(self,
                          nodo_final: NodoAstar,
                          nombre_red: str,
                          x_inicio: float, y_inicio: float,
                          x_fin: float, y_fin: float) -> List[dict]:
        """
        Reconstruye la lista de segmentos a partir del nodo final.

        Agrupa segmentos consecutivos con la misma dirección en uno solo,
        reduciendo el número de segmentos en el archivo de salida.

        Returns:
            Lista de dicts {'x1','y1','x2','y2','red'}
        """
        # Reconstruir camino de celdas desde destino hasta inicio
        camino = []
        nodo = nodo_final
        while nodo is not None:
            camino.append((nodo.col, nodo.fila))
            nodo = nodo.padre
        camino.reverse()

        if len(camino) < 2:
            return []

        # Convertir celdas a coordenadas en mm
        puntos_mm = []
        for col, fila in camino:
            x, y = self.celda_a_mm(col, fila)
            puntos_mm.append((x, y))

        # --- Paso 1: Simplificar el cuerpo del A* (solo pasos 0/45/90°) ---
        # El A* en 8 direcciones produce SOLO pasos H/V/45°, por lo que
        # _simplificar_ruta puede fusionar tramos colineales directamente.
        # Hacemos esto ANTES de poner los extremos reales del pad, para que
        # los pasos uniformes del grid se fusionen con máxima eficacia.
        segmentos_grid = self._simplificar_ruta(puntos_mm, nombre_red)

        if not segmentos_grid:
            return []

        # --- Paso 2: Reemplazar extremos con coordenadas exactas del pad ---
        # Los extremos del A* son celdas del grid; los pads pueden estar
        # ligeramente desplazados (sub-grid). Actualizamos inicio y fin.
        # Esto puede crear segmentos muy cortos no-45° en los extremos.
        segmentos_grid[0]['x1']  = x_inicio
        segmentos_grid[0]['y1']  = y_inicio
        segmentos_grid[-1]['x2'] = x_fin
        segmentos_grid[-1]['y2'] = y_fin

        # Reconstruir lista de puntos desde los segmentos actualizados
        puntos_snap = [( segmentos_grid[0]['x1'],  segmentos_grid[0]['y1'])]
        for s in segmentos_grid:
            puntos_snap.append((s['x2'], s['y2']))

        # --- Paso 3: Corregir ángulos en los extremos ---
        # Solo el primer y último segmento pueden tener ángulo no estándar
        # (por el desplazamiento sub-grid del pad). _forzar_angulos_limpios
        # los convierte en el patrón codo estándar (diagonal + recto).
        puntos_snap = self._forzar_angulos_limpios(puntos_snap)

        # --- Paso 4: Fusión final de collineales (después del codo) ---
        segmentos = self._simplificar_ruta(puntos_snap, nombre_red)

        # --- Paso 5: Absorber stubs diminutos en la ruta final ---
        # Pequeños "jogs" (<0.05mm) entre segmentos del grid y el centro real
        # del pad se eliminan extendiendo el segmento previo. El resultado es
        # un segmento ligeramente desviado del eje pero visualmente continuo.
        segmentos = self._absorber_stubs(segmentos, umbral_mm=0.05)

        return segmentos

    def _absorber_stubs(self, segmentos: List[dict], umbral_mm: float = 0.05) -> List[dict]:
        """
        Elimina segmentos diminutos absorbiéndolos en el segmento adyacente.

        Estrategia: para cada segmento más corto que ``umbral_mm``, extiende
        el extremo del segmento previo hasta el punto final del stub, y luego
        elimina el stub. Si el stub está en el primer segmento, extiende el
        inicio del segundo segmento hacia atrás.

        Esto elimina los micro-jogs (<0.1mm) que aparecen en los extremos de
        las rutas cuando el A* termina cerca pero no exactamente sobre el
        centro del pad. El resultado puede tener un segmento ligeramente
        fuera de los ángulos 0°/45°/90° estándar — KiCad lo acepta sin
        problema (lo mismo hace el enrutamiento manual).
        """
        if len(segmentos) <= 1:
            return segmentos

        # Iterar de adelante hacia atrás para que los índices se mantengan válidos
        i = len(segmentos) - 1
        while i >= 0 and len(segmentos) > 1:
            s = segmentos[i]
            L = math.hypot(s['x2'] - s['x1'], s['y2'] - s['y1'])
            if L < umbral_mm:
                if i > 0:
                    # Extender el segmento previo hasta el final del stub
                    segmentos[i - 1]['x2'] = s['x2']
                    segmentos[i - 1]['y2'] = s['y2']
                    segmentos.pop(i)
                else:
                    # Stub al inicio: extender el inicio del siguiente segmento
                    segmentos[i + 1]['x1'] = s['x1']
                    segmentos[i + 1]['y1'] = s['y1']
                    segmentos.pop(i)
            i -= 1

        return segmentos

    def _segmento_libre(self, x1: float, y1: float, x2: float, y2: float) -> bool:
        """
        Verifica si la línea recta entre (x1,y1) y (x2,y2) no cruza ningún obstáculo.

        Usa el algoritmo de Bresenham para recorrer todas las celdas del grid
        a lo largo de la línea y comprueba que ninguna esté marcada como obstáculo.
        Las celdas de inicio y fin se ignoran (son los pads, ya libres).
        """
        col1, fila1 = self.mm_a_celda(x1, y1)
        col2, fila2 = self.mm_a_celda(x2, y2)

        dc = abs(col2 - col1)
        df = abs(fila2 - fila1)
        sc = 1 if col2 > col1 else -1
        sf = 1 if fila2 > fila1 else -1
        col, fila = col1, fila1
        err = dc - df
        primero = True
        pasos = 0
        max_pasos = dc + df + 2

        while pasos <= max_pasos:
            pasos += 1
            es_inicio = primero
            es_fin = (col == col2 and fila == fila2)
            primero = False

            # Ignorar celdas de inicio/fin (son los propios pads)
            if not es_inicio and not es_fin:
                if (col, fila) in self.obstaculos:
                    return False

            if es_fin:
                break

            e2 = 2 * err
            if e2 > -df:
                err -= df
                col += sc
            if e2 < dc:
                err += dc
                fila += sf

        return True

    def _optimizar_vision_linea(self,
                                puntos: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        """
        Optimización greedy de line-of-sight.

        Para cada punto del camino, intenta saltar la mayor cantidad posible
        de puntos intermedios dibujando una línea recta directa. Si esa línea
        no cruza obstáculos, los puntos intermedios se eliminan.

        Ejemplo:
          Antes: 15 puntos en escalera (staircase del grid A*)
          Después: 3 puntos formando 2 segmentos limpios

        Returns:
            Lista reducida de puntos (x, y) en mm
        """
        if len(puntos) <= 2:
            return puntos

        resultado = [puntos[0]]
        i = 0

        while i < len(puntos) - 1:
            # Buscar el punto más lejano alcanzable en línea recta
            # (búsqueda desde el final hacia atrás para maximizar el salto)
            mejor_j = i + 1
            for j in range(len(puntos) - 1, i + 1, -1):
                if self._segmento_libre(
                    puntos[i][0], puntos[i][1],
                    puntos[j][0], puntos[j][1]
                ):
                    mejor_j = j
                    break
            resultado.append(puntos[mejor_j])
            i = mejor_j

        return resultado

    def _forzar_angulos_limpios(self,
                               puntos: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        """
        Convierte cualquier segmento con ángulo arbitrario en dos segmentos
        de 0°/45°/90° usando el patrón de "codo" estándar de PCB routing.

        Para un segmento (x1,y1)→(x2,y2) con ángulo no estándar:
          - Primero va en diagonal 45° tantos pasos como el eje menor
          - Luego va recto (horizontal o vertical) para cubrir el eje mayor
          Resultado: punto intermedio "codo" = (x1 ± d, y1 ± d)
                     donde d = min(|dx|, |dy|)

        Ejemplo: (0,0)→(6,4) se convierte en (0,0)→(4,4)→(6,4)
                 — diagonal 45° de longitud 4, luego horizontal 2 —

        El paso posterior _simplificar_ruta() fusionará cualquier par de
        segmentos colineales que resulten del proceso.
        """
        if len(puntos) <= 1:
            return puntos

        resultado = [puntos[0]]

        for i in range(len(puntos) - 1):
            x1, y1 = puntos[i]
            x2, y2 = puntos[i + 1]
            dx = x2 - x1
            dy = y2 - y1
            adx = abs(dx)
            ady = abs(dy)

            # Segmento ya es 0°/90° (horizontal o vertical)
            if ady < 1e-4:
                resultado.append((x2, y2))
                continue
            if adx < 1e-4:
                resultado.append((x2, y2))
                continue

            # Segmento ya es 45° exacto
            if abs(adx - ady) < 1e-4:
                resultado.append((x2, y2))
                continue

            # Umbral mínimo para hacer codo: si el desfase del eje menor
            # es < 0.15mm, NO vale la pena descomponer el segmento. En su
            # lugar dejamos UN segmento ligeramente inclinado (KiCad lo
            # acepta — es lo mismo que hace el enrutamiento manual cuando
            # tiene que ajustar un pad ligeramente fuera de grid).
            # Esto evita los stubs diminutos (0.02–0.10mm) que visualmente
            # parecen desconexiones.
            UMBRAL_CODO_MIN = 0.15  # mm

            menor = min(adx, ady)
            if menor < UMBRAL_CODO_MIN:
                # Aceptar el segmento "casi recto" tal cual
                resultado.append((x2, y2))
                continue

            # Ángulo arbitrario con desfase significativo: insertar codo limpio
            # El codo va en diagonal el mínimo de los dos ejes
            sx = 1.0 if dx > 0 else -1.0
            sy = 1.0 if dy > 0 else -1.0
            d = menor
            xm = x1 + d * sx
            ym = y1 + d * sy

            resultado.append((xm, ym))
            resultado.append((x2, y2))

        return resultado

    def _simplificar_ruta(self,
                          puntos: List[Tuple[float, float]],
                          nombre_red: str) -> List[dict]:
        """
        Simplifica la ruta agrupando puntos colineales en un solo segmento.

        Dos puntos consecutivos están en la misma dirección si el ángulo
        entre ellos y el punto anterior es el mismo (tolerancia pequeña).

        Returns:
            Lista de dicts {'x1','y1','x2','y2','red'}
        """
        if len(puntos) < 2:
            return []

        segmentos = []
        x_seg_ini, y_seg_ini = puntos[0]

        def misma_direccion(xa, ya, xb, yb, xc, yc) -> bool:
            """¿Son (xa,ya)→(xb,yb) y (xb,yb)→(xc,yc) la misma dirección?"""
            d1x, d1y = xb - xa, yb - ya
            d2x, d2y = xc - xb, yc - yb
            # Normalizar
            n1 = math.hypot(d1x, d1y)
            n2 = math.hypot(d2x, d2y)
            if n1 < 1e-9 or n2 < 1e-9:
                return True
            # Producto cruzado normalizado ≈ 0 si son paralelos
            cruz = (d1x / n1) * (d2y / n2) - (d1y / n1) * (d2x / n2)
            return abs(cruz) < 0.01

        for i in range(1, len(puntos)):
            x_actual, y_actual = puntos[i]

            # ¿Podemos extender el segmento actual?
            puede_continuar = False
            if i < len(puntos) - 1:
                x_sig, y_sig = puntos[i + 1]
                puede_continuar = misma_direccion(
                    x_seg_ini, y_seg_ini,
                    x_actual, y_actual,
                    x_sig, y_sig
                )

            if not puede_continuar or i == len(puntos) - 1:
                # Emitir segmento si tiene longitud real
                if abs(x_actual - x_seg_ini) > 1e-6 or abs(y_actual - y_seg_ini) > 1e-6:
                    segmentos.append({
                        'x1': x_seg_ini, 'y1': y_seg_ini,
                        'x2': x_actual, 'y2': y_actual,
                        'red': nombre_red
                    })
                x_seg_ini, y_seg_ini = x_actual, y_actual

        return segmentos


# ─────────────────────────────────────────────────────────────────────────────
# MPS net ordering (Maximum Planar Subset)
# ─────────────────────────────────────────────────────────────────────────────

def _conexiones_se_cruzan(a: Tuple[PadPCB, PadPCB],
                          b: Tuple[PadPCB, PadPCB]) -> bool:
    """
    Devuelve True si las líneas directas de las conexiones a y b se cruzan.

    Usa el test de orientación CCW (counter-clockwise), el mismo que emplea
    KiCadRoutingTools en connectivity.segments_intersect.

    Nota: si las conexiones comparten un pad terminal (mismo net) se consideran
    no cruzadas, porque rutas de la misma red pueden pasar por el mismo punto.
    """
    ax1, ay1 = a[0].x, a[0].y
    ax2, ay2 = a[1].x, a[1].y
    bx1, by1 = b[0].x, b[0].y
    bx2, by2 = b[1].x, b[1].y

    # Si comparten un extremo no hay cruce real
    eps = 0.001
    for px, py in [(ax1, ay1), (ax2, ay2)]:
        for qx, qy in [(bx1, by1), (bx2, by2)]:
            if abs(px - qx) < eps and abs(py - qy) < eps:
                return False

    def ccw(p1x, p1y, p2x, p2y, p3x, p3y) -> bool:
        return (p3y - p1y) * (p2x - p1x) > (p2y - p1y) * (p3x - p1x)

    return (ccw(ax1, ay1, bx1, by1, bx2, by2) != ccw(ax2, ay2, bx1, by1, bx2, by2) and
            ccw(ax1, ay1, ax2, ay2, bx1, by1) != ccw(ax1, ay1, ax2, ay2, bx2, by2))


def ordenar_conexiones_mps(conexiones: List[Tuple[PadPCB, PadPCB]]) -> List[Tuple[PadPCB, PadPCB]]:
    """
    Ordena las conexiones con el algoritmo MPS (Maximum Planar Subset).

    Objetivo: enrutar primero las conexiones que menos conflictos tienen con
    otras, para que las rutas más conflictivas encuentren más espacio libre
    cuando les toque el turno.

    Algoritmo (basado en KiCadRoutingTools compute_mps_net_ordering):

      1. Construir grafo de conflictos:
           dos conexiones conflictan si sus segmentos directos (ratsnest) se
           cruzan en el espacio 2D.

      2. Ordenamiento greedy por rondas:
           - Ronda 1: elegir iterativamente la conexión con menos conflictos
             activos; marcar sus conflictivas como "perdedoras" de esta ronda.
           - Rondas 2, 3, …: repetir con las perdedoras, hasta agotar todas.

      3. Dentro de cada ronda, ordenar por distancia euclídea (más corta primero)
         como criterio de desempate y para favorecer rutas directas.

    Returns:
        Lista reordenada de conexiones.
    """
    n = len(conexiones)
    if n <= 1:
        return list(conexiones)

    # Distancias euclídeas (para desempate y orden interno de cada ronda)
    distancias = [
        math.hypot(c[1].x - c[0].x, c[1].y - c[0].y)
        for c in conexiones
    ]

    # Construir grafo de conflictos
    conflictos: List[Set[int]] = [set() for _ in range(n)]
    num_cruces = 0
    for i in range(n):
        for j in range(i + 1, n):
            if _conexiones_se_cruzan(conexiones[i], conexiones[j]):
                conflictos[i].add(j)
                conflictos[j].add(i)
                num_cruces += 1

    print(f"[MPS] {n} conexiones, {num_cruces} cruces de ratsnest detectados")

    if num_cruces == 0:
        # Sin cruces: solo ordenar por distancia
        idx_ordenado = sorted(range(n), key=lambda i: distancias[i])
        print("[MPS] Sin conflictos — orden por distancia aplicado")
        return [conexiones[i] for i in idx_ordenado]

    # Greedy por rondas
    resultado_idx: List[int] = []
    pendientes = set(range(n))
    ronda = 0

    while pendientes:
        ronda += 1
        ganadores: List[int] = []
        perdedores: Set[int] = set()
        candidatos = set(pendientes)

        while candidatos:
            # Elegir la conexión con menos conflictos activos;
            # desempatar por distancia (más corta primero)
            mejor = min(
                candidatos,
                key=lambda i: (len(conflictos[i] & candidatos), distancias[i])
            )
            ganadores.append(mejor)
            candidatos.discard(mejor)

            # Los conflictivos del ganador son perdedores de esta ronda
            for perdedor in conflictos[mejor] & candidatos:
                perdedores.add(perdedor)
                candidatos.discard(perdedor)

        # Dentro de la ronda, ordenar ganadores por distancia
        ganadores.sort(key=lambda i: distancias[i])
        resultado_idx.extend(ganadores)
        pendientes = perdedores

        nombres = [f"{conexiones[i][0].referencia}.{conexiones[i][0].numero_pad}"
                   f"→{conexiones[i][1].referencia}.{conexiones[i][1].numero_pad}"
                   f"[{conexiones[i][0].nombre_red}]"
                   for i in ganadores]
        print(f"[MPS] Ronda {ronda} ({len(ganadores)} conexiones): {', '.join(nombres)}")

    return [conexiones[i] for i in resultado_idx]


# ─────────────────────────────────────────────────────────────────────────────
# Conectividad del cobre propio de una red (para derivaciones tipo "tap")
# ─────────────────────────────────────────────────────────────────────────────

def _dist_punto_segmento(px: float, py: float,
                         x1: float, y1: float,
                         x2: float, y2: float) -> float:
    """Distancia mínima del punto (px,py) al segmento (x1,y1)-(x2,y2)."""
    dx, dy = x2 - x1, y2 - y1
    den = dx * dx + dy * dy
    if den < 1e-12:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / den
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _componentes_de_cobre(segmentos: List[dict],
                          tolerancia: float = 0.15) -> List[List[dict]]:
    """
    Agrupa segmentos de una MISMA red en componentes eléctricamente conexas.

    Dos segmentos pertenecen a la misma componente si se tocan por extremos o
    en T (un extremo de uno cae sobre el cuerpo del otro), igual criterio que
    usa la verificación de conectividad.
    """
    n = len(segmentos)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        a = segmentos[i]
        for j in range(i + 1, n):
            b = segmentos[j]
            tocan = (
                _dist_punto_segmento(a['x1'], a['y1'], b['x1'], b['y1'], b['x2'], b['y2']) < tolerancia or
                _dist_punto_segmento(a['x2'], a['y2'], b['x1'], b['y1'], b['x2'], b['y2']) < tolerancia or
                _dist_punto_segmento(b['x1'], b['y1'], a['x1'], a['y1'], a['x2'], a['y2']) < tolerancia or
                _dist_punto_segmento(b['x2'], b['y2'], a['x1'], a['y1'], a['x2'], a['y2']) < tolerancia
            )
            if tocan:
                union(i, j)

    grupos: Dict[int, List[dict]] = {}
    for i in range(n):
        grupos.setdefault(find(i), []).append(segmentos[i])
    return list(grupos.values())


def _componente_que_toca(pad: PadPCB,
                         componentes: List[List[dict]],
                         tolerancia: float = 0.15) -> Optional[int]:
    """Índice de la componente de cobre que toca este pad, o None."""
    for idx, comp in enumerate(componentes):
        for s in comp:
            if _dist_punto_segmento(pad.x, pad.y,
                                    s['x1'], s['y1'], s['x2'], s['y2']) < tolerancia:
                return idx
    return None


def decidir_objetivo(origen: PadPCB,
                     destino: PadPCB,
                     cobre_red: List[dict],
                     enrutador: 'EnrutadorAstar') -> Tuple[str, PadPCB, Optional[Set[Tuple[int, int]]]]:
    """
    Decide CÓMO enrutar una arista MST según el cobre ya trazado de su red.

    Una arista MST no obliga a unir dos pads concretos: obliga a dejar la red
    conectada. Si un extremo ya cuelga del cobre de su propia red, basta con
    llevar el otro hasta ese cobre por el punto más cercano (tap), en vez de
    forzarlo a alcanzar un pad lejano dando un rodeo.

    Esta decisión la comparten el enrutador real y el surrogate del AG: si sólo
    uno de los dos la aplicara, el AG optimizaría para un problema distinto del
    que realmente se resuelve.

    Returns:
        (accion, pad_a_enrutar, celdas_objetivo) donde accion es:
          'omitir' — ambos extremos ya cuelgan del mismo cobre; nada que trazar
          'tap'    — enrutar `pad_a_enrutar` hasta `celdas_objetivo`
          'normal' — enrutar origen → destino como conexión punto a punto
    """
    if not cobre_red:
        return 'normal', origen, None

    componentes = _componentes_de_cobre(cobre_red)
    comp_origen = _componente_que_toca(origen, componentes)
    comp_destino = _componente_que_toca(destino, componentes)

    if comp_origen is not None and comp_origen == comp_destino:
        return 'omitir', origen, None

    def _celdas(idx: int) -> Set[Tuple[int, int]]:
        celdas: Set[Tuple[int, int]] = set()
        for s in componentes[idx]:
            celdas |= enrutador.celdas_centro_segmento(
                s['x1'], s['y1'], s['x2'], s['y2'])
        return celdas

    if comp_destino is not None and comp_origen is None:
        return 'tap', origen, _celdas(comp_destino)
    if comp_origen is not None and comp_destino is None:
        return 'tap', destino, _celdas(comp_origen)

    return 'normal', origen, None


# ─────────────────────────────────────────────────────────────────────────────
# Función auxiliar de alto nivel
# ─────────────────────────────────────────────────────────────────────────────

def enrutar_todos(conexiones: List[Tuple[PadPCB, PadPCB]],
                  todos_los_pads: List[PadPCB],
                  limite_tablero: Tuple[float, float, float, float],
                  paso_cuadricula: float = 0.25,
                  ancho_pista: float = 0.3,
                  clearance: float = 0.2,
                  segmentos_existentes: Optional[List[SegmentoPCB]] = None,
                  estrategia_orden: str = 'mps',
                  anchos_por_red: Optional[Dict[str, float]] = None,
                  clearance_minimo: float = 0.127,
                  callback_progreso=None) -> Tuple[List[dict], dict]:
    """
    Enruta todas las conexiones en el orden dado.

    Para cada conexión:
      1. Reactiva pads de la red activa (quita obstáculos de terminales)
      2. Ejecuta A*
      3. Marca la ruta como obstáculo para siguientes conexiones

    Args:
        conexiones: Lista de (pad_origen, pad_destino) a enrutar
        todos_los_pads: Todos los pads del PCB (para obstáculos)
        limite_tablero: (min_x, min_y, max_x, max_y) en mm
        paso_cuadricula: Resolución en mm
        ancho_pista: Ancho de pista por defecto en mm
        clearance: Clearance mínimo en mm
        estrategia_orden: 'mps' (greedy por rondas de cruces, por defecto),
            'distancia' (más corta primero), o 'ninguna' (respeta el orden
            recibido — usado cuando el AG ya optimizó la secuencia).
        anchos_por_red: Dict opcional {nombre_red: ancho_mm}. Las redes no
            listadas usan ancho_pista. Cada segmento generado lleva su ancho
            en la clave 'ancho'.
        clearance_minimo: Piso de clearance fabricable en mm (defecto 0.127,
            mínimo de JLCPCB). Los reintentos progresivos NUNCA bajan de este
            valor: una conexión que solo se resolvería por debajo se marca
            FALLIDA, porque una pista con menos separación no es fabricable.
        callback_progreso: Función opcional f(i, total, red, exito) para UI

    Returns:
        (lista_segmentos, metricas) donde metricas es un dict con estadísticas
    """
    enrutador = EnrutadorAstar(limite_tablero, paso_cuadricula)
    anchos_por_red = anchos_por_red or {}

    # ── Estrategia de orden ──────────────────────────────────────────────────
    # 'mps'      : greedy por rondas de cruces (MPS) + distancia dentro de ronda
    # 'distancia': más corta primero (baseline)
    # 'ninguna'  : respeta el orden recibido tal cual — el llamador ya lo
    #              optimizó (p. ej. el AG). NO reordenar: hacerlo destruiría
    #              la salida del optimizador.
    if estrategia_orden == 'mps':
        conexiones = ordenar_conexiones_mps(list(conexiones))
        etiqueta_orden = "MPS + distancia"
    elif estrategia_orden == 'distancia':
        conexiones = sorted(
            conexiones,
            key=lambda c: math.hypot(c[1].x - c[0].x, c[1].y - c[0].y)
        )
        etiqueta_orden = "distancia creciente"
    else:
        conexiones = list(conexiones)
        etiqueta_orden = "recibido (optimizado por el llamador)"

    print(f"\n[A*] Orden de enrutamiento ({etiqueta_orden}):")
    for idx, (o, d) in enumerate(conexiones):
        dist = math.hypot(d.x - o.x, d.y - o.y)
        print(f"       {idx+1}. {o.referencia}.{o.numero_pad}->{d.referencia}.{d.numero_pad} [{o.nombre_red}] {dist:.2f}mm")

    # ── Niveles de clearance para los reintentos ─────────────────────────────
    # Nunca por debajo del piso de fabricación. Si el clearance nominal ya es
    # menor que el piso, se respeta la decisión explícita del usuario pero se
    # avisa: el resultado no será fabricable con ese proceso.
    if clearance < clearance_minimo - 1e-9:
        print(f"[A*] ADVERTENCIA: clearance nominal {clearance:.3f}mm < piso de "
              f"fabricación {clearance_minimo:.3f}mm — sin reintentos por debajo")
        niveles_clearance = [clearance]
    else:
        candidatos = [clearance, clearance * 0.5, clearance_minimo]
        niveles_clearance = []
        for c in candidatos:
            if c < clearance_minimo - 1e-9:
                continue
            if any(abs(c - existente) < 1e-9 for existente in niveles_clearance):
                continue
            niveles_clearance.append(c)
    print(f"[A*] Niveles de clearance: "
          f"{', '.join(f'{c:.3f}mm' for c in niveles_clearance)} "
          f"(piso fabricable: {clearance_minimo:.3f}mm)")

    # Construir mapa base de obstáculos (todos los pads)
    enrutador.agregar_pads_como_obstaculos(todos_los_pads, clearance)

    todos_segmentos = []
    metricas = {
        'total': len(conexiones),
        'exitosas': 0,
        'fallidas': 0,
        'longitud_total_mm': 0.0,
        'clearance_minimo': clearance_minimo,
        'detalle': []
    }

    def _marcar_rutas_previas(clearance_marca: float, red_activa: str = "") -> None:
        """
        Marca las rutas ya trazadas como obstáculo, cada una con SU ancho.

        Las pistas de `red_activa` se OMITEN: son del mismo net que la conexión
        que se está enrutando, así que tocarlas o solaparlas no es una falta de
        DRC (mismo potencial eléctrico). Tratarlas como obstáculo hacía que el
        enrutador peleara contra su propio cobre — la causa de que las últimas
        aristas de redes multipunto dieran rodeos enormes o fallaran.
        """
        for s in todos_segmentos:
            if red_activa and s.get('red') == red_activa:
                continue
            enrutador.marcar_ruta_como_obstaculo(
                [s], s.get('ancho', ancho_pista), clearance_marca)

    # Cobre preexistente (enrutado manual del usuario) indexado por red, en el
    # mismo formato dict que los segmentos que genera el enrutador.
    cobre_previo_por_red: Dict[str, List[dict]] = {}
    for s in (segmentos_existentes or []):
        cobre_previo_por_red.setdefault(s.nombre_red, []).append({
            'x1': s.x_inicio, 'y1': s.y_inicio,
            'x2': s.x_fin, 'y2': s.y_fin,
            'red': s.nombre_red, 'ancho': s.ancho,
        })

    # Pads por red y conjunto de redes que se están enrutando: base para medir
    # cuántas redes quedan eléctricamente completas en cada momento.
    redes_a_enrutar = {o.nombre_red for o, _ in conexiones}
    pads_por_red: Dict[str, List[PadPCB]] = {}
    for p in todos_los_pads:
        if p.nombre_red in redes_a_enrutar:
            pads_por_red.setdefault(p.nombre_red, []).append(p)

    def _grupos_de_red(nombre_red: str) -> int:
        """
        En cuántos grupos aislados están los pads de la red ahora mismo.

        1 = red completa. Los pads que no tocan cobre cuentan cada uno como su
        propio grupo. Se mide en grupos y no como booleano porque durante el
        enrutado una red está legítimamente incompleta: sus aristas restantes
        aún no llegaron. Lo exigible a media construcción es no EMPEORAR, no
        estar terminada.
        """
        pads_red = pads_por_red.get(nombre_red, [])
        if len(pads_red) < 2:
            return 1
        cobre = [s for s in todos_segmentos if s.get('red') == nombre_red]
        cobre += cobre_previo_por_red.get(nombre_red, [])
        if not cobre:
            return len(pads_red)
        componentes = _componentes_de_cobre(cobre)
        grupos, sueltos = set(), 0
        for p in pads_red:
            c = _componente_que_toca(p, componentes)
            if c is None:
                sueltos += 1
            else:
                grupos.add(c)
        return len(grupos) + sueltos

    def _red_completa(nombre_red: str) -> bool:
        """True si todos los pads de la red cuelgan de un mismo cobre conexo."""
        return _grupos_de_red(nombre_red) == 1

    def _contar_redes_completas() -> int:
        return sum(1 for r in redes_a_enrutar if _red_completa(r))

    # ── Helpers del bucle principal (compartidos con el rip-up) ──────────────

    def _resolver_conexion(idx: int):
        """
        Decisión (omitir/tap/normal) + escalera de reintentos para UNA conexión.

        NO toca métricas ni todos_segmentos: es la primitiva pura de enrutado
        que usan tanto el bucle principal como el rip-up.

        Returns:
            ('ok', segmentos_etiquetados, clearance_usado)
            ('omitida', [], clearance)   — ya conectada por cobre de su red
            ('fallo', None, None)        — sin camino ni al piso de clearance
        """
        origen, destino = conexiones[idx]
        red = origen.nombre_red
        ancho_conexion = anchos_por_red.get(red, ancho_pista)

        cobre_red = [s for s in todos_segmentos if s.get('red') == red]
        cobre_red += cobre_previo_por_red.get(red, [])
        accion, pad_a_enrutar, celdas_objetivo = decidir_objetivo(
            origen, destino, cobre_red, enrutador)

        if accion == 'omitir':
            return 'omitida', [], clearance
        if accion == 'tap':
            print(f"[A*]   Tap sobre cobre de {red}: enrutando "
                  f"{pad_a_enrutar.referencia}.{pad_a_enrutar.numero_pad} "
                  f"hacia {len(celdas_objetivo)} celdas objetivo")

        segmentos = None
        clearance_usado = clearance
        for intento_num, clearance_intento in enumerate(niveles_clearance):
            enrutador.limpiar_obstaculos()
            enrutador.agregar_pads_como_obstaculos(
                todos_los_pads, clearance_intento, red_a_ignorar=red)
            if segmentos_existentes:
                segs_otros = [s for s in segmentos_existentes if s.nombre_red != red]
                if segs_otros:
                    enrutador.marcar_segmentos_existentes(segs_otros, clearance_intento)
            _marcar_rutas_previas(clearance_intento, red)

            if celdas_objetivo:
                segmentos = enrutador.enrutar_a_objetivos(
                    pad_a_enrutar, celdas_objetivo, ancho_conexion, clearance_intento)
            else:
                segmentos = enrutador.enrutar_conexion(
                    origen, destino, ancho_conexion, clearance_intento)

            if segmentos:
                clearance_usado = clearance_intento
                if intento_num > 0:
                    print(f"[A*] Reintento {intento_num} exitoso con "
                          f"clearance={clearance_intento:.3f}mm")
                break
            if segmentos is not None:
                # Lista vacía: el pad ya estaba sobre el cobre destino.
                return 'omitida', [], clearance
            if intento_num < len(niveles_clearance) - 1:
                print(f"[A*] Intento {intento_num+1} fallido "
                      f"(clearance={clearance_intento:.3f}mm), reintentando...")

        if not segmentos:
            return 'fallo', None, None

        for s in segmentos:
            s['ancho'] = ancho_conexion
            s['conexion_idx'] = idx   # trazabilidad: qué conexión puso cada segmento
        return 'ok', segmentos, clearance_usado

    def _entrada_detalle(idx: int, **extra) -> dict:
        """Entrada base de metricas['detalle'] para la conexión idx."""
        origen, destino = conexiones[idx]
        base = {
            'red': origen.nombre_red,
            'origen': f"{origen.referencia}.{origen.numero_pad}",
            'destino': f"{destino.referencia}.{destino.numero_pad}",
            'longitud_directa_mm': round(
                math.hypot(destino.x - origen.x, destino.y - origen.y), 3),
        }
        base.update(extra)
        return base

    def _campos_exito(idx: int, segmentos: List[dict],
                      clearance_usado: float) -> Tuple[float, dict]:
        """Longitud y campos de detalle de una conexión enrutada con éxito."""
        origen, destino = conexiones[idx]
        directa = math.hypot(destino.x - origen.x, destino.y - origen.y)
        longitud = sum(math.hypot(s['x2'] - s['x1'], s['y2'] - s['y1'])
                       for s in segmentos)
        desvio = (longitud / directa) if directa > 1e-9 else 1.0
        return longitud, {
            'exito': True,
            'longitud_mm': round(longitud, 3),
            'desvio': round(desvio, 2),
            'num_segmentos': len(segmentos),
            'clearance_usado': clearance_usado,
            'ancho_usado': segmentos[0].get('ancho', ancho_pista) if segmentos else ancho_pista,
        }

    def _quitar_resultado(j: int) -> None:
        """Descuenta de las métricas el resultado vigente de la conexión j."""
        e = metricas['detalle'][j]
        if e.get('exito'):
            metricas['exitosas'] -= 1
            metricas['longitud_total_mm'] -= e.get('longitud_mm', 0.0)
        elif e.get('omitida'):
            metricas['omitidas'] = metricas.get('omitidas', 0) - 1

    def _aplicar_resultado(j: int, estado: str,
                           segmentos: Optional[List[dict]],
                           clearance_usado: Optional[float]) -> None:
        """Escribe el resultado de la conexión j en detalle y suma a métricas."""
        if estado == 'ok':
            longitud, campos = _campos_exito(j, segmentos, clearance_usado)
            entrada = _entrada_detalle(j, **campos)
            metricas['exitosas'] += 1
            metricas['longitud_total_mm'] += longitud
        else:   # 'omitida'
            entrada = _entrada_detalle(j, exito=False, omitida=True,
                                       longitud_mm=0.0, desvio=None,
                                       num_segmentos=0)
            metricas['omitidas'] = metricas.get('omitidas', 0) + 1

        if j < len(metricas['detalle']):
            metricas['detalle'][j] = entrada
        else:
            metricas['detalle'].append(entrada)

    def _identificar_bloqueadores(idx: int) -> Tuple[List[int], float]:
        """
        Conexiones ya enrutadas que bloquean el camino ideal de `idx`.

        Método: se corre A* con SOLO pads y cobre manual como obstáculos (sin
        las rutas de esta corrida) para obtener el camino ideal; se rasterizan
        sus celdas y se mide el solape con cada conexión ya trazada.

        Returns:
            (indices ordenados de mayor a menor solape, longitud del camino
            ideal en mm). La longitud ideal sale gratis del mismo A* y es la
            base del filtro de ganancia: si la ruta actual no es mucho más
            larga que el ideal, no hay nada que rescatar.

        El camino ideal se calcula contra el MISMO objetivo que se intentó
        enrutar: si la conexión va como tap (hacia el cobre de su propia red),
        analizar en cambio el camino pad-a-pad describe una geometría que nunca
        se usó, y el solape resultante señala culpables equivocados o ninguno.
        Con el tap como caso mayoritario, eso dejaba al rip-up sin candidatas
        justo cuando más falta hacían.
        """
        origen, destino = conexiones[idx]
        red = origen.nombre_red
        ancho_conexion = anchos_por_red.get(red, ancho_pista)

        cobre_red = [s for s in todos_segmentos if s.get('red') == red]
        cobre_red += cobre_previo_por_red.get(red, [])
        accion, pad_a_enrutar, celdas_objetivo = decidir_objetivo(
            origen, destino, cobre_red, enrutador)
        if accion == 'omitir':
            return [], 0.0   # ya conectada: no hay nada que rescatar

        enrutador.limpiar_obstaculos()
        enrutador.agregar_pads_como_obstaculos(todos_los_pads, clearance,
                                               red_a_ignorar=red)
        if segmentos_existentes:
            segs_otros = [s for s in segmentos_existentes if s.nombre_red != red]
            if segs_otros:
                enrutador.marcar_segmentos_existentes(segs_otros, clearance)

        if accion == 'tap':
            ideal = enrutador.enrutar_a_objetivos(
                pad_a_enrutar, celdas_objetivo, ancho_conexion, clearance)
        else:
            ideal = enrutador.enrutar_conexion(
                origen, destino, ancho_conexion, clearance)

        if not ideal:
            return [], float('inf')   # ni despejada hay camino: irrescatable

        longitud_ideal = sum(math.hypot(s['x2'] - s['x1'], s['y2'] - s['y1'])
                             for s in ideal)

        celdas_ideal: Set[Tuple[int, int]] = set()
        for s in ideal:
            celdas_ideal.update(enrutador._celdas_de_segmento(
                s['x1'], s['y1'], s['x2'], s['y2'], ancho_conexion, clearance))

        solapes: Dict[int, int] = {}
        for s in todos_segmentos:
            j = s.get('conexion_idx')
            if j is None or conexiones[j][0].nombre_red == red:
                continue   # el cobre propio no bloquea (variante B)
            celdas_s = set(enrutador._celdas_de_segmento(
                s['x1'], s['y1'], s['x2'], s['y2'],
                s.get('ancho', ancho_pista), 0.0))
            n = len(celdas_s & celdas_ideal)
            if n:
                solapes[j] = solapes.get(j, 0) + n

        ordenados = [j for j, _ in sorted(solapes.items(), key=lambda kv: -kv[1])]
        return ordenados, longitud_ideal

    def _intentar_ripup(idx: int, motivo: str = 'fallo') -> bool:
        """
        Rip-up and reroute acotado, con dos disparadores.

        motivo='fallo'      — la conexión agotó la escalera de clearance sin
                              encontrar ruta. El estado base NO la incluye.
        motivo='patologica' — la conexión SÍ se enrutó, pero con un desvío
                              >= UMBRAL_DESVIO_PATOLOGICO. El estado base ya la
                              incluye, con su ruta larga puesta. Sin este
                              disparador el mecanismo nunca se entera de las
                              rutas absurdas: una de 272mm para 27mm directos es
                              peor que fallar y rescatar, pero como técnicamente
                              encontró camino, no dispara nada.

        Procedimiento:
          1. Camino ideal (placa despejada) → conexiones que lo bloquean y
             longitud ideal. Para 'patologica', si la ganancia posible contra
             ese ideal es menor que GANANCIA_MINIMA_RIPUP_MM, se descarta sin
             tocar ninguna red: el trabajo no se paga.
          2. Elegir la RED de una candidata — 2 redes como máximo para 'fallo',
             UNA sola para 'patologica' (si sigue larga, se queda así y se
             reporta, para que el costo no escale en placas densas).
          3. Retirar TODAS las conexiones ya enrutadas de esa red: quitar una
             sola arista de una red multipunto la dejaría partida.
          4. Enrutar la conexión objetivo; reenrutar la red retirada completa.
          5. La red retirada no puede quedar MÁS partida que antes (a media
             secuencia una red está legítimamente incompleta: le faltan aristas
             que vienen después en el orden).
          6. Aceptar sólo si el estado resultante gana en la comparación
             lexicográfica (redes_desconectadas, fallidas, longitud) — la misma
             clave que usa --repeticiones. Encaja sola en ambos disparadores:
             con 'fallo' el base tiene una fallida más y cualquier rescate gana
             en la segunda clave; con 'patologica' las fallidas empatan y decide
             la longitud, que es justo lo que se quiere mejorar.
             Para 'fallo' se mantiene además el tope de +2.0x la distancia
             directa: ahí se paga longitud a cambio de ganar una conexión, pero
             no a cualquier precio.

        Los reenrutados internos usan _resolver_conexion, que no hace rip-up:
        sin recursión por construcción.
        """
        stats = metricas.setdefault('ripup_stats', {
            'fallo': {'triggers': 0, 'aceptados': 0, 'revertidos': 0,
                      'sin_candidatas': 0},
            'patologica': {'triggers': 0, 'aceptados': 0, 'revertidos': 0,
                           'sin_candidatas': 0, 'sin_ganancia': 0},
        })
        stats[motivo]['triggers'] += 1

        # Tope global de intentos por corrida: acota el peor caso si rehacer
        # una red vuelve patológica a otra y se encadenan disparos.
        if metricas.get('ripup_intentos', 0) >= 2 * len(conexiones):
            print(f"[RipUp] Presupuesto de intentos agotado "
                  f"({metricas['ripup_intentos']}); no se intenta más")
            return False

        origen, destino = conexiones[idx]
        dist_directa = math.hypot(destino.x - origen.x, destino.y - origen.y)
        nombre_idx = (f"{origen.referencia}.{origen.numero_pad}->"
                      f"{destino.referencia}.{destino.numero_pad}")

        total_redes = len(redes_a_enrutar)

        # ── Estado BASE: el que hay que batir ────────────────────────────────
        redes_base = _contar_redes_completas()
        # Con 'fallo' la conexión aún no se contabilizó como fallida (el
        # llamador lo hace después), así que se suma aquí para comparar peras
        # con peras.
        fallidas_base = metricas['fallidas'] + (1 if motivo == 'fallo' else 0)
        longitud_base = metricas['longitud_total_mm']
        resueltas_base = metricas['exitosas']
        clave_base = (total_redes - redes_base, fallidas_base, longitud_base)

        # Snapshot completo para poder volver exactamente a este punto
        snap_segs = list(todos_segmentos)
        snap_detalle = [dict(e) for e in metricas['detalle']]
        snap_exitosas = metricas['exitosas']
        snap_omitidas = metricas.get('omitidas', 0)
        snap_longitud = metricas['longitud_total_mm']

        def _restaurar() -> None:
            todos_segmentos[:] = snap_segs
            metricas['detalle'][:] = snap_detalle
            metricas['exitosas'] = snap_exitosas
            metricas['omitidas'] = snap_omitidas
            metricas['longitud_total_mm'] = snap_longitud

        def _revertir(razon: str) -> None:
            _restaurar()
            stats[motivo]['revertidos'] += 1
            print(f"[RipUp] ✗ Revertido ({motivo}): {razon}")

        # Longitud actual de la conexión (sólo existe en el caso patológico)
        longitud_actual = (snap_detalle[idx].get('longitud_mm', 0.0)
                           if motivo == 'patologica' and idx < len(snap_detalle)
                           else 0.0)

        # En el caso patológico hay que retirar la ruta actual ANTES de buscar
        # bloqueadores: con sus propios segmentos puestos, `decidir_objetivo` ve
        # los dos extremos ya conectados, responde 'omitir' y la identificación
        # devuelve cero candidatas — el disparador quedaría inerte.
        if motivo == 'patologica':
            _quitar_resultado(idx)
            todos_segmentos[:] = [s for s in todos_segmentos
                                  if s.get('conexion_idx') != idx]

        # ── Candidatas y camino ideal ────────────────────────────────────────
        bloqueadores, longitud_ideal = _identificar_bloqueadores(idx)

        if motivo == 'patologica':
            ganancia = longitud_actual - longitud_ideal
            if ganancia < GANANCIA_MINIMA_RIPUP_MM:
                stats['patologica']['sin_ganancia'] += 1
                _restaurar()
                print(f"[RipUp] {nombre_idx}: ganancia posible {ganancia:.1f}mm "
                      f"< {GANANCIA_MINIMA_RIPUP_MM:.1f}mm — no se intenta")
                return False
            print(f"[RipUp] {nombre_idx} patológica ({longitud_actual:.1f}mm vs "
                  f"ideal {longitud_ideal:.1f}mm): hasta {ganancia:.1f}mm por ganar")

        if not bloqueadores:
            stats[motivo]['sin_candidatas'] += 1
            if motivo == 'patologica':
                _restaurar()
            print(f"[RipUp] Sin candidatas que bloqueen a {nombre_idx}")
            return False

        max_redes = 1 if motivo == 'patologica' else 2
        redes_intentadas: Set[str] = set()

        for victima_idx in bloqueadores:
            vo, vd = conexiones[victima_idx]
            victima_net = vo.nombre_red
            if victima_net in redes_intentadas:
                continue      # esa red ya se probó entera
            if len(redes_intentadas) >= max_redes:
                break
            redes_intentadas.add(victima_net)
            metricas['ripup_intentos'] = metricas.get('ripup_intentos', 0) + 1

            nombre_v = (f"{vo.referencia}.{vo.numero_pad}->"
                        f"{vd.referencia}.{vd.numero_pad}")
            indices_a_rehacer = [j for j in range(idx)
                                 if conexiones[j][0].nombre_red == victima_net]
            grupos_victima_antes = _grupos_de_red(victima_net)

            print(f"[RipUp] [{motivo}] Retirando la red {victima_net} completa "
                  f"({len(indices_a_rehacer)} conexión/es, bloqueante {nombre_v}) "
                  f"para {'rescatar' if motivo == 'fallo' else 'acortar'} {nombre_idx}")

            # ── Retirar las pistas de toda la red de la víctima ──────────────
            for j in indices_a_rehacer:
                _quitar_resultado(j)
            a_rehacer = set(indices_a_rehacer)
            todos_segmentos[:] = [s for s in todos_segmentos
                                  if s.get('conexion_idx') not in a_rehacer]

            # ── Enrutar la conexión objetivo ─────────────────────────────────
            est, segs, clr = _resolver_conexion(idx)
            if est == 'fallo':
                _revertir(f"{nombre_idx} quedó sin ruta")
                continue
            if segs:
                todos_segmentos.extend(segs)
            _aplicar_resultado(idx, est, segs, clr)

            # ── Reenrutar la red de la víctima, en orden ─────────────────────
            fallo_en = None
            for j in indices_a_rehacer:
                est_j, segs_j, clr_j = _resolver_conexion(j)
                if est_j == 'fallo':
                    fallo_en = j
                    break
                if segs_j:
                    todos_segmentos.extend(segs_j)
                _aplicar_resultado(j, est_j, segs_j, clr_j)

            if fallo_en is not None:
                oj, dj = conexiones[fallo_en]
                _revertir(f"{oj.referencia}.{oj.numero_pad}->"
                          f"{dj.referencia}.{dj.numero_pad} no pudo reenrutarse")
                continue

            # ── La víctima no puede quedar más partida que antes ─────────────
            grupos_victima_despues = _grupos_de_red(victima_net)
            if grupos_victima_despues > grupos_victima_antes:
                _revertir(f"la red {victima_net} quedó más partida "
                          f"({grupos_victima_antes} -> {grupos_victima_despues} grupos)")
                continue

            # ── Comparación lexicográfica contra el estado base ──────────────
            redes_despues = _contar_redes_completas()
            longitud_despues = metricas['longitud_total_mm']
            clave_despues = (total_redes - redes_despues,
                             metricas['fallidas'],
                             longitud_despues)

            if clave_despues >= clave_base:
                _revertir(
                    f"no mejora: (redes abiertas, fallidas, longitud) "
                    f"{clave_base[0]},{clave_base[1]},{clave_base[2]:.1f} -> "
                    f"{clave_despues[0]},{clave_despues[1]},{clave_despues[2]:.1f}")
                continue

            # Con 'fallo' se paga longitud por ganar una conexión, pero acotado.
            if motivo == 'fallo':
                limite = longitud_base + 2.0 * dist_directa
                if longitud_despues > limite:
                    _revertir(f"longitud {longitud_base:.1f} -> "
                              f"{longitud_despues:.1f}mm supera el límite "
                              f"{limite:.1f}mm (+2.0x la directa de "
                              f"{dist_directa:.1f}mm)")
                    continue

            # ── Aceptado ─────────────────────────────────────────────────────
            if motivo == 'fallo':
                metricas['detalle'][idx]['rescatada_por_ripup'] = nombre_v
            else:
                metricas['detalle'][idx]['acortada_por_ripup'] = nombre_v
            for j in indices_a_rehacer:
                metricas['detalle'][j]['reenrutada_por_ripup'] = True

            stats[motivo]['aceptados'] += 1
            metricas.setdefault('ripups', []).append({
                'motivo': motivo,
                'conexion': nombre_idx,
                'victima': nombre_v,
                'red_victima': victima_net,
                'conexiones_rehechas': len(indices_a_rehacer),
                'redes_antes': redes_base,
                'redes_despues': redes_despues,
                'delta_longitud': round(longitud_despues - longitud_base, 2),
            })
            print(f"[RipUp] ✓ [{motivo}] {nombre_idx}: rehechas "
                  f"{len(indices_a_rehacer)} conexión/es de {victima_net}, "
                  f"redes completas {redes_base}->{redes_despues}, "
                  f"longitud total {longitud_despues - longitud_base:+.1f}mm")
            return True

        # Ninguna candidata prosperó: dejar el estado exactamente como estaba
        _restaurar()
        return False

    for i, (origen, destino) in enumerate(conexiones):
        red = origen.nombre_red
        ancho_conexion = anchos_por_red.get(red, ancho_pista)
        print(f"\n[A*] [{i+1}/{len(conexiones)}] Enrutando {origen.referencia}.{origen.numero_pad} "
              f"-> {destino.referencia}.{destino.numero_pad} [{red}]"
              + (f" (ancho {ancho_conexion}mm)" if ancho_conexion != ancho_pista else ""))

        # ── Stub proximity costs ─────────────────────────────────────────────
        # Penalizar celdas cercanas a pads de conexiones que AÚN no se han
        # enrutado (índices i+1 en adelante). Así A* evita bloquear futuros
        # terminales al trazar la ruta actual.
        enrutador.limpiar_costos_proximidad()
        if i < len(conexiones) - 1:
            pads_futuros: List[PadPCB] = []
            for j in range(i + 1, len(conexiones)):
                pads_futuros.append(conexiones[j][0])
                pads_futuros.append(conexiones[j][1])
            enrutador.agregar_costos_proximidad_stubs(pads_futuros)
            print(f"[A*]   Stub proximity: {len(pads_futuros)} pads futuros penalizados")

        estado, segmentos, clearance_usado = _resolver_conexion(i)
        exito_final = estado != 'fallo'

        if estado == 'omitida':
            metricas['omitidas'] = metricas.get('omitidas', 0) + 1
            metricas['detalle'].append(_entrada_detalle(
                i, exito=False, omitida=True,
                longitud_mm=0.0, desvio=None, num_segmentos=0))
            print(f"[A*] — Omitida: ya conectada por cobre de {red}")

        elif estado == 'ok':
            longitud, campos = _campos_exito(i, segmentos, clearance_usado)
            metricas['exitosas'] += 1
            metricas['longitud_total_mm'] += longitud
            metricas['detalle'].append(_entrada_detalle(i, **campos))
            todos_segmentos.extend(segmentos)
            print(f"[A*] ✓ Enrutado: {longitud:.2f} mm, {len(segmentos)} segmento(s), "
                  f"desvío {campos['desvio']:.2f}x")

            # Encontrar ruta no basta: una que rodea media placa es peor que
            # fallar y rescatar. Con desvío patológico se intenta rip-up igual,
            # y sólo se acepta si el estado completo mejora.
            if campos['desvio'] >= UMBRAL_DESVIO_PATOLOGICO:
                print(f"[A*]   Desvío patológico "
                      f"({campos['desvio']:.2f}x >= {UMBRAL_DESVIO_PATOLOGICO:.1f}x) "
                      f"— intentando rip-up para acortarla...")
                _intentar_ripup(i, motivo='patologica')

        else:
            # Escalera agotada: rip-up acotado antes de declarar el fallo
            print(f"[A*] ✗ Sin ruta al piso de clearance — "
                  f"intentando rip-up (N=1, máx 2 candidatas)...")
            exito_final = _intentar_ripup(i, motivo='fallo')
            if not exito_final:
                metricas['fallidas'] += 1
                metricas['detalle'].append(_entrada_detalle(
                    i, exito=False,
                    longitud_mm=0.0, desvio=None, num_segmentos=0))
                print(f"[A*] ✗ CONEXION NO ENRUTADA (escalera y rip-up agotados, "
                      f"piso {clearance_minimo:.3f}mm): "
                      f"{origen.referencia}.{origen.numero_pad} -> "
                      f"{destino.referencia}.{destino.numero_pad} [{red}]")

        if callback_progreso:
            callback_progreso(i + 1, len(conexiones), red, exito_final)

    metricas['longitud_total_mm'] = round(metricas['longitud_total_mm'], 3)

    # ── Estadísticas de desvío sobre las conexiones exitosas ─────────────────
    desvios = [d['desvio'] for d in metricas['detalle']
               if d.get('exito') and d.get('desvio') is not None]
    if desvios:
        peor = max(metricas['detalle'],
                   key=lambda d: d['desvio'] if (d.get('exito') and d.get('desvio')) else -1)
        metricas['desvio_promedio'] = round(sum(desvios) / len(desvios), 2)
        metricas['desvio_maximo'] = round(max(desvios), 2)
        metricas['desvio_peor_conexion'] = f"{peor['origen']} → {peor['destino']}"
        # Rutas patológicas: mismo umbral que dispara el rip-up
        metricas['rutas_patologicas'] = sum(
            1 for d in desvios if d >= UMBRAL_DESVIO_PATOLOGICO)
    else:
        metricas['desvio_promedio'] = 0.0
        metricas['desvio_maximo'] = 0.0
        metricas['desvio_peor_conexion'] = "—"
        metricas['rutas_patologicas'] = 0

    return todos_segmentos, metricas


# ─────────────────────────────────────────────────────────────────────────────
# Ejecución directa para prueba
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from lector_pcb import leer_pcb, obtener_conexiones_a_enrutar

    pcb = leer_pcb("casos_prueba/mi_pcb.kicad_pcb")
    conexiones = obtener_conexiones_a_enrutar(pcb)

    print(f"\nConexiones a enrutar: {len(conexiones)}")
    segmentos, metricas = enrutar_todos(
        conexiones,
        pcb.todos_los_pads,
        pcb.limite_tablero
    )

    print(f"\n=== RESULTADO A* ===")
    print(f"Exitosas: {metricas['exitosas']}/{metricas['total']}")
    print(f"Longitud total: {metricas['longitud_total_mm']} mm")
