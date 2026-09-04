"""
optimizador_genetico.py — Algoritmo Genético para optimizar el orden de enrutamiento

El orden en que se enrutan las conexiones de un PCB afecta el resultado porque
cada pista trazada se convierte en obstáculo para las siguientes. El AG busca
el orden que minimiza el costo SECUENCIAL real.

HISTORIA / POR QUÉ LA APTITUD ES SECUENCIAL:
La versión anterior usaba aptitud = longitud_estimada + peso×cruces, pero ambos
términos son invariantes ante permutaciones (una suma no depende del orden de
los sumandos; el conjunto de pares i<j es el mismo en cualquier orden). El AG
optimizaba una función constante. La aptitud actual evalúa el orden ENRUTANDO
secuencialmente, de modo que el efecto obstáculo quede capturado.

Dos evaluadores disponibles:
  - EvaluadorSurrogate  (fitness='surrogate', por defecto): A* real con
    cuadrícula gruesa (0.5mm por defecto) + caché de permutaciones y prefijos.
    Fiel pero cuesta ~0.5–2s por individuo según el tamaño de la placa.
  - EvaluadorAproximado (fitness='aproximada'): modelo de oclusión progresiva —
    el costo de cada cruce lo paga la conexión que se enruta DESPUÉS, más
    penalización superlineal por encierro. Microsegundos por individuo.

Siembra de la población: el orden MPS y el orden por distancia entran como
individuos iniciales. Con elitismo, el AG nunca termina peor que el mejor de
los dos heurísticos.

Operadores genéticos:
  - Representación: permutación de índices de conexiones
  - Selección:      Torneo (k=3)
  - Cruce:          OX — Order Crossover (preserva permutación válida)
  - Mutación:       Intercambio de dos posiciones aleatorias
  - Elitismo:       El mejor individuo pasa sin cambios a la siguiente generación
"""

import random
import math
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

from lector_pcb import PadPCB


# ─────────────────────────────────────────────────────────────────────────────
# Tipos
# ─────────────────────────────────────────────────────────────────────────────

# Un individuo es una permutación de índices de conexiones
Individuo = List[int]

# Una conexión es un par (pad_origen, pad_destino)
Conexion = Tuple[PadPCB, PadPCB]


# ─────────────────────────────────────────────────────────────────────────────
# Utilidades geométricas compartidas
# ─────────────────────────────────────────────────────────────────────────────

def distancia_chebyshev_octilineal(origen: PadPCB, destino: PadPCB) -> float:
    """Longitud mínima de un camino octilineal (H/V/45°) entre dos pads."""
    dx = abs(destino.x - origen.x)
    dy = abs(destino.y - origen.y)
    return max(dx, dy) + (math.sqrt(2) - 1) * min(dx, dy)


def _segmentos_se_cruzan(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2) -> bool:
    """Intersección de segmentos por orientación cruzada (sin contar extremos)."""
    def orientacion(px, py, qx, qy, rx, ry) -> int:
        val = (qy - py) * (rx - qx) - (qx - px) * (ry - qy)
        if abs(val) < 1e-10:
            return 0
        return 1 if val > 0 else 2

    def en_segmento(px, py, qx, qy, rx, ry) -> bool:
        return (min(px, qx) <= rx <= max(px, qx) and
                min(py, qy) <= ry <= max(py, qy))

    o1 = orientacion(ax1, ay1, ax2, ay2, bx1, by1)
    o2 = orientacion(ax1, ay1, ax2, ay2, bx2, by2)
    o3 = orientacion(bx1, by1, bx2, by2, ax1, ay1)
    o4 = orientacion(bx1, by1, bx2, by2, ax2, ay2)

    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and en_segmento(ax1, ay1, ax2, ay2, bx1, by1): return True
    if o2 == 0 and en_segmento(ax1, ay1, ax2, ay2, bx2, by2): return True
    if o3 == 0 and en_segmento(bx1, by1, bx2, by2, ax1, ay1): return True
    if o4 == 0 and en_segmento(bx1, by1, bx2, by2, ax2, ay2): return True
    return False


def _punto_interseccion(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2):
    """Punto de intersección de las rectas que contienen a los dos segmentos."""
    d = (ax2 - ax1) * (by2 - by1) - (ay2 - ay1) * (bx2 - bx1)
    if abs(d) < 1e-12:
        return None
    t = ((bx1 - ax1) * (by2 - by1) - (by1 - ay1) * (bx2 - bx1)) / d
    return (ax1 + t * (ax2 - ax1), ay1 + t * (ay2 - ay1))


# ─────────────────────────────────────────────────────────────────────────────
# Evaluador SURROGATE: A* secuencial con cuadrícula gruesa + caché
# ─────────────────────────────────────────────────────────────────────────────

class EvaluadorSurrogate:
    """
    Aptitud = enrutamiento secuencial real con A* a paso grueso.

    Para cada conexión, en el orden del individuo: se reconstruyen los
    obstáculos (pads + rutas ya trazadas), se corre A*, y la ruta resultante
    se acumula como obstáculo para las siguientes. Un fallo suma una
    penalización de 2× la diagonal del tablero.

    Cachés:
      - Permutaciones completas: dict tuple(orden) → costo. Con elitismo y
        convergencia, las permutaciones repetidas abundan.
      - Prefijos: se conservan las últimas `max_trayectorias` trayectorias
        evaluadas con un snapshot del estado (segmentos acumulados, longitud,
        resueltas) después de CADA paso. Al evaluar un individuo nuevo se
        busca la trayectoria guardada con el prefijo común más largo y se
        reanuda desde ahí — dos órdenes que comparten los primeros k elementos
        producen exactamente el mismo estado tras k conexiones.
    """

    def __init__(self,
                 conexiones: List[Conexion],
                 todos_los_pads: List[PadPCB],
                 limite_tablero: Tuple[float, float, float, float],
                 paso: float = 0.5,
                 ancho_pista: float = 0.3,
                 clearance: float = 0.2,
                 anchos_por_red: Optional[Dict[str, float]] = None,
                 max_trayectorias: int = 8):
        from enrutador_astar import EnrutadorAstar
        self._EnrutadorAstar = EnrutadorAstar

        self.conexiones = conexiones
        self.todos_los_pads = todos_los_pads
        self.limite_tablero = limite_tablero
        self.paso = paso
        self.ancho_pista = ancho_pista
        self.clearance = clearance
        self.anchos_por_red = anchos_por_red or {}
        self.max_trayectorias = max_trayectorias

        min_x, min_y, max_x, max_y = limite_tablero
        self.penalizacion_fallo = 2.0 * math.hypot(max_x - min_x, max_y - min_y)

        # Caché de permutaciones completas: tuple(orden) → costo
        self._memo: Dict[Tuple[int, ...], float] = {}
        # Trayectorias para reanudación por prefijo:
        #   (orden_tuple, snapshots) con snapshots[k] = estado tras k pasos:
        #   (tuple_segmentos, longitud, resueltas). snapshots[0] = ((), 0, 0).
        self._trayectorias: List[Tuple[Tuple[int, ...], List[tuple]]] = []

        self.evaluaciones_reales = 0     # A* efectivamente ejecutados (conexiones)
        self.hits_memo = 0
        self.pasos_ahorrados_prefijo = 0

    def _ancho_de(self, red: str) -> float:
        return self.anchos_por_red.get(red, self.ancho_pista)

    def _rutear_paso(self, enrutador, segmentos_previos: list,
                     idx_conexion: int):
        """
        Enruta una conexión con el estado dado. Retorna (segmentos|None, longitud).

        Replica exactamente la lógica del enrutador real (`enrutar_todos`):
        variante B (el cobre de la propia red no es obstáculo) y decisión de
        tap/omitir vía `decidir_objetivo`. Si el surrogate no aplicara las
        mismas reglas, el AG optimizaría el orden para un problema distinto
        del que después se resuelve — que es justo lo que lo hacía empeorar.
        """
        from enrutador_astar import decidir_objetivo

        origen, destino = self.conexiones[idx_conexion]
        red = origen.nombre_red
        ancho = self._ancho_de(red)

        enrutador.limpiar_obstaculos()
        enrutador.agregar_pads_como_obstaculos(self.todos_los_pads,
                                               self.clearance, red_a_ignorar=red)
        if segmentos_previos:
            # Los segmentos previos pueden tener anchos distintos por red; cada
            # dict lleva su 'ancho' y se rasteriza individualmente. Los de la
            # MISMA red se omiten (variante B).
            for s in segmentos_previos:
                if s.get('red') == red:
                    continue
                enrutador.marcar_ruta_como_obstaculo(
                    [s], s.get('ancho', self.ancho_pista), self.clearance)

        cobre_red = [s for s in segmentos_previos if s.get('red') == red]
        accion, pad_a_enrutar, celdas_objetivo = decidir_objetivo(
            origen, destino, cobre_red, enrutador)

        if accion == 'omitir':
            # Ya conectada: no aporta longitud ni cuenta como fallo.
            return [], 0.0
        if accion == 'tap':
            segmentos = enrutador.enrutar_a_objetivos(
                pad_a_enrutar, celdas_objetivo, ancho, self.clearance)
        else:
            segmentos = enrutador.enrutar_conexion(origen, destino, ancho, self.clearance)

        if segmentos is None:
            return None, 0.0
        for s in segmentos:
            s['ancho'] = ancho
        longitud = sum(math.hypot(s['x2'] - s['x1'], s['y2'] - s['y1'])
                       for s in segmentos)
        return segmentos, longitud

    def _mejor_prefijo(self, orden: Tuple[int, ...]) -> Tuple[int, list]:
        """Busca la trayectoria guardada con el prefijo común más largo."""
        mejor_k, mejor_snapshots = 0, None
        for orden_guardado, snapshots in self._trayectorias:
            k = 0
            tope = min(len(orden), len(orden_guardado))
            while k < tope and orden[k] == orden_guardado[k]:
                k += 1
            if k > mejor_k:
                mejor_k, mejor_snapshots = k, snapshots
        return mejor_k, mejor_snapshots

    def evaluar(self, orden: Individuo) -> float:
        """
        Retorna la aptitud (negativa: mayor = mejor, el AG maximiza).

        costo = longitud_total_enrutada + penalizacion_fallo × fallos
        """
        clave = tuple(orden)
        if clave in self._memo:
            self.hits_memo += 1
            return self._memo[clave]

        # Reanudar desde el prefijo común más largo si existe
        k_inicio, snapshots_base = self._mejor_prefijo(clave)
        if k_inicio > 0:
            segs_tuple, longitud, resueltas = snapshots_base[k_inicio]
            segmentos_acumulados = list(segs_tuple)
            self.pasos_ahorrados_prefijo += k_inicio
        else:
            segmentos_acumulados, longitud, resueltas = [], 0.0, 0

        # max_iteraciones bajo: en grueso, un fallo debe fallar rápido
        enrutador = self._EnrutadorAstar(self.limite_tablero, self.paso,
                                         max_iteraciones=30_000)

        # snapshots de ESTA evaluación (para registrarla como trayectoria)
        snapshots = [None] * (len(clave) + 1)
        if k_inicio > 0:
            for k in range(k_inicio + 1):
                snapshots[k] = snapshots_base[k]
        else:
            snapshots[0] = ((), 0.0, 0)

        for pos in range(k_inicio, len(clave)):
            segmentos, long_paso = self._rutear_paso(
                enrutador, segmentos_acumulados, clave[pos])
            # None = no hay camino (fallo). [] = ya conectada (éxito sin trazo).
            if segmentos is not None:
                segmentos_acumulados.extend(segmentos)
                longitud += long_paso
                resueltas += 1
            self.evaluaciones_reales += 1
            snapshots[pos + 1] = (tuple(segmentos_acumulados), longitud, resueltas)

        fallos = len(clave) - resueltas
        costo = longitud + self.penalizacion_fallo * fallos
        aptitud = -costo

        self._memo[clave] = aptitud
        self._trayectorias.append((clave, snapshots))
        if len(self._trayectorias) > self.max_trayectorias:
            self._trayectorias.pop(0)

        return aptitud

    def resueltas_de(self, orden: Individuo) -> int:
        """Número de conexiones resueltas para un orden (evalúa si hace falta)."""
        aptitud = self.evaluar(orden)
        costo = -aptitud
        # Recuperar fallos del costo: longitud <= n × diagonal < penalización
        # (aprox.: el residuo modular no es fiable; recomputamos de snapshots)
        for orden_guardado, snapshots in self._trayectorias:
            if orden_guardado == tuple(orden):
                return snapshots[-1][2]
        # Trayectoria expulsada del LRU: estimación por penalización
        return len(orden) - int(costo // self.penalizacion_fallo)


# ─────────────────────────────────────────────────────────────────────────────
# Evaluador APROXIMADO: oclusión progresiva (modo rápido)
# ─────────────────────────────────────────────────────────────────────────────

class EvaluadorAproximado:
    """
    Aptitud rápida y dependiente del orden, sin ejecutar A*.

    Modelo: cuando las líneas directas de dos conexiones se cruzan, el costo
    del cruce lo paga la que se enruta DESPUÉS (debe desviarse alrededor de la
    pista ya trazada). El desvío se estima como 2× la distancia del punto de
    cruce al extremo más cercano del segmento anterior (rodear por el lado
    corto, ida y vuelta). Además, una conexión cruzada por k anteriores recibe
    una penalización de encierro k² × peso (riesgo creciente de quedar
    bloqueada por completo).

    O(n²) por individuo con aritmética simple — microsegundos.
    """

    PESO_ENCIERRO = 5.0  # mm equivalentes por unidad de k²

    def __init__(self, conexiones: List[Conexion]):
        self.conexiones = conexiones
        n = len(conexiones)

        # Precomputar longitudes y la matriz de cruces con su desvío estimado.
        self.longitudes = [distancia_chebyshev_octilineal(o, d)
                           for o, d in conexiones]

        # cruces[i][j] = desvío que paga j si i se enruta antes (y viceversa
        # en cruces[j][i]); 0.0 si no se cruzan o son de la misma red.
        self.desvio: List[List[float]] = [[0.0] * n for _ in range(n)]
        self.pares_cruce: List[Tuple[int, int]] = []

        for i in range(n):
            o1, d1 = conexiones[i]
            for j in range(i + 1, n):
                o2, d2 = conexiones[j]
                if o1.nombre_red == o2.nombre_red:
                    continue
                if not _segmentos_se_cruzan(o1.x, o1.y, d1.x, d1.y,
                                            o2.x, o2.y, d2.x, d2.y):
                    continue
                p = _punto_interseccion(o1.x, o1.y, d1.x, d1.y,
                                        o2.x, o2.y, d2.x, d2.y)
                if p is None:
                    continue
                px, py = p
                # Si i va primero, j debe rodear el segmento de i:
                # desvío ≈ 2 × distancia del cruce al extremo más cercano de i
                rodeo_de_i = 2.0 * min(math.hypot(px - o1.x, py - o1.y),
                                       math.hypot(px - d1.x, py - d1.y))
                # Si j va primero, i debe rodear el segmento de j
                rodeo_de_j = 2.0 * min(math.hypot(px - o2.x, py - o2.y),
                                       math.hypot(px - d2.x, py - d2.y))
                self.desvio[i][j] = rodeo_de_i   # lo paga j (posterior)
                self.desvio[j][i] = rodeo_de_j   # lo paga i (posterior)
                self.pares_cruce.append((i, j))

    def evaluar(self, orden: Individuo) -> float:
        """Retorna la aptitud (negativa: mayor = mejor)."""
        n = len(orden)
        posicion = [0] * n
        for pos, idx in enumerate(orden):
            posicion[idx] = pos

        costo = sum(self.longitudes)
        encierros = [0] * n

        for i, j in self.pares_cruce:
            if posicion[i] < posicion[j]:
                # i va primero → j paga el rodeo alrededor de i
                costo += self.desvio[i][j]
                encierros[j] += 1
            else:
                costo += self.desvio[j][i]
                encierros[i] += 1

        for k in encierros:
            if k >= 2:
                costo += self.PESO_ENCIERRO * (k ** 2)

        return -costo


# ─────────────────────────────────────────────────────────────────────────────
# Órdenes semilla (heurísticos que entran a la población inicial)
# ─────────────────────────────────────────────────────────────────────────────

def orden_por_distancia(conexiones: List[Conexion]) -> Individuo:
    """Permutación de índices ordenada por distancia directa creciente."""
    return sorted(range(len(conexiones)),
                  key=lambda i: math.hypot(
                      conexiones[i][1].x - conexiones[i][0].x,
                      conexiones[i][1].y - conexiones[i][0].y))


def orden_por_mps(conexiones: List[Conexion]) -> Individuo:
    """Permutación de índices según el ordenamiento MPS (greedy por rondas)."""
    from enrutador_astar import ordenar_conexiones_mps
    ordenadas = ordenar_conexiones_mps(list(conexiones))
    # ordenar_conexiones_mps devuelve las MISMAS tuplas reordenadas:
    # recuperar el índice original por identidad de objeto.
    indice_por_id = {id(c): i for i, c in enumerate(conexiones)}
    return [indice_por_id[id(c)] for c in ordenadas]


# ─────────────────────────────────────────────────────────────────────────────
# Operadores genéticos
# ─────────────────────────────────────────────────────────────────────────────

def cruce_ox(padre1: Individuo, padre2: Individuo) -> Tuple[Individuo, Individuo]:
    """Order Crossover (OX) — cruce que preserva la permutación válida."""
    n = len(padre1)
    if n <= 1:
        return padre1[:], padre2[:]

    a = random.randint(0, n - 1)
    b = random.randint(0, n - 1)
    if a > b:
        a, b = b, a
    b += 1  # Intervalo [a, b)

    def _ox_hijo(p1: Individuo, p2: Individuo) -> Individuo:
        hijo = [-1] * n
        en_hijo = set()
        for i in range(a, b):
            hijo[i] = p1[i]
            en_hijo.add(p1[i])
        pos = b % n
        for gen in (p2[(b + k) % n] for k in range(n)):
            if gen not in en_hijo:
                hijo[pos] = gen
                en_hijo.add(gen)
                pos = (pos + 1) % n
        return hijo

    return _ox_hijo(padre1, padre2), _ox_hijo(padre2, padre1)


def mutacion_swap(individuo: Individuo, prob_mutacion: float = 0.1) -> Individuo:
    """Mutación por intercambio de dos posiciones aleatorias."""
    resultado = individuo[:]
    if random.random() < prob_mutacion and len(resultado) >= 2:
        i = random.randint(0, len(resultado) - 1)
        j = random.randint(0, len(resultado) - 1)
        while j == i:
            j = random.randint(0, len(resultado) - 1)
        resultado[i], resultado[j] = resultado[j], resultado[i]
    return resultado


def seleccion_torneo(poblacion: List[Individuo],
                     aptitudes: List[float],
                     k: int = 3) -> Individuo:
    """Selección por torneo: k candidatos al azar, gana el de mayor aptitud."""
    candidatos_idx = random.sample(range(len(poblacion)), min(k, len(poblacion)))
    ganador_idx = max(candidatos_idx, key=lambda i: aptitudes[i])
    return poblacion[ganador_idx][:]


# ─────────────────────────────────────────────────────────────────────────────
# Algoritmo Genético principal
# ─────────────────────────────────────────────────────────────────────────────

class AlgoritmoGenetico:
    """
    AG para optimizar el orden de enrutamiento.

    La aptitud la provee un evaluador externo (EvaluadorSurrogate o
    EvaluadorAproximado) — SIEMPRE dependiente del orden. La población inicial
    se siembra con los órdenes heurísticos que se pasen en `semillas` (MPS,
    distancia) más individuos aleatorios; con elitismo, el resultado nunca es
    peor que la mejor semilla.
    """

    def __init__(self,
                 conexiones: List[Conexion],
                 funcion_aptitud: Callable[[Individuo], float],
                 tamano_poblacion: int = 24,
                 num_generaciones: int = 40,
                 prob_cruce: float = 0.8,
                 prob_mutacion: float = 0.15,
                 semillas: Optional[List[Individuo]] = None,
                 semilla_aleatoria: Optional[int] = None,
                 callback_generacion: Optional[Callable] = None):
        self.conexiones = conexiones
        self.n = len(conexiones)
        self.aptitud = funcion_aptitud
        self.tamano_pob = tamano_poblacion
        self.num_gen = num_generaciones
        self.prob_cruce = prob_cruce
        self.prob_mutacion = prob_mutacion
        self.semillas = [s[:] for s in (semillas or [])]
        self.callback = callback_generacion

        if semilla_aleatoria is not None:
            random.seed(semilla_aleatoria)

        self.historial_mejor: List[float] = []
        self.historial_promedio: List[float] = []
        # Aptitudes de los individuos aleatorios de la generación 0
        # (para la métrica "mejora vs aleatorio")
        self.aptitudes_aleatorios_iniciales: List[float] = []

    def _crear_individuo_aleatorio(self) -> Individuo:
        individuo = list(range(self.n))
        random.shuffle(individuo)
        return individuo

    def evolucionar(self) -> Tuple[Individuo, float, dict]:
        """Ejecuta el ciclo evolutivo. Retorna (mejor, aptitud, estadísticas)."""
        if self.n == 0:
            return [], 0.0, {}
        if self.n == 1:
            return [0], 0.0, {}

        tiempo_inicio = time.time()
        print(f"\n[AG] Iniciando evolución: {self.n} conexiones, "
              f"población={self.tamano_pob}, generaciones={self.num_gen}, "
              f"semillas heurísticas={len(self.semillas)}")

        # Población inicial: semillas heurísticas + aleatorios
        poblacion: List[Individuo] = [s[:] for s in self.semillas[:self.tamano_pob]]
        num_aleatorios = self.tamano_pob - len(poblacion)
        aleatorios = [self._crear_individuo_aleatorio() for _ in range(num_aleatorios)]
        poblacion.extend(aleatorios)

        mejor_global: Individuo = poblacion[0][:]
        mejor_aptitud_global = float('-inf')

        for gen in range(self.num_gen):
            aptitudes = [self.aptitud(ind) for ind in poblacion]

            if gen == 0 and num_aleatorios > 0:
                self.aptitudes_aleatorios_iniciales = aptitudes[len(self.semillas):]

            idx_mejor = max(range(len(aptitudes)), key=lambda i: aptitudes[i])
            mejor_gen = aptitudes[idx_mejor]
            promedio_gen = sum(aptitudes) / len(aptitudes)

            self.historial_mejor.append(-mejor_gen)
            self.historial_promedio.append(-promedio_gen)

            if mejor_gen > mejor_aptitud_global:
                mejor_aptitud_global = mejor_gen
                mejor_global = poblacion[idx_mejor][:]

            if gen % max(1, self.num_gen // 10) == 0 or gen == self.num_gen - 1:
                print(f"[AG] Gen {gen+1:4d}/{self.num_gen}: "
                      f"mejor={-mejor_gen:.2f}, prom={-promedio_gen:.2f}")

            if self.callback:
                self.callback(gen + 1, -mejor_gen, mejor_global)

            # Nueva generación: élite + hijos
            nueva_poblacion = [mejor_global[:]]
            while len(nueva_poblacion) < self.tamano_pob:
                padre1 = seleccion_torneo(poblacion, aptitudes)
                padre2 = seleccion_torneo(poblacion, aptitudes)
                if random.random() < self.prob_cruce:
                    hijo1, hijo2 = cruce_ox(padre1, padre2)
                else:
                    hijo1, hijo2 = padre1[:], padre2[:]
                hijo1 = mutacion_swap(hijo1, self.prob_mutacion)
                hijo2 = mutacion_swap(hijo2, self.prob_mutacion)
                nueva_poblacion.append(hijo1)
                if len(nueva_poblacion) < self.tamano_pob:
                    nueva_poblacion.append(hijo2)

            poblacion = nueva_poblacion

        tiempo_total = time.time() - tiempo_inicio

        estadisticas = {
            'generaciones': self.num_gen,
            'tamano_poblacion': self.tamano_pob,
            'mejor_costo': -mejor_aptitud_global,
            'tiempo_segundos': round(tiempo_total, 2),
            'historial_mejor': self.historial_mejor,
            'historial_promedio': self.historial_promedio,
        }

        print(f"\n[AG] Evolución completada en {tiempo_total:.2f}s")
        print(f"[AG] Costo del mejor orden: {-mejor_aptitud_global:.2f}")

        return mejor_global, mejor_aptitud_global, estadisticas


# ─────────────────────────────────────────────────────────────────────────────
# Función de alto nivel
# ─────────────────────────────────────────────────────────────────────────────

def optimizar_orden_enrutamiento(conexiones: List[Conexion],
                                  todos_los_pads: List[PadPCB],
                                  limite_tablero: Tuple[float, float, float, float],
                                  tamano_poblacion: int = 24,
                                  num_generaciones: int = 40,
                                  fitness: str = 'surrogate',
                                  paso_surrogate: float = 0.5,
                                  ancho_pista: float = 0.3,
                                  clearance: float = 0.2,
                                  anchos_por_red: Optional[Dict[str, float]] = None,
                                  prob_cruce: float = 0.8,
                                  prob_mutacion: float = 0.15,
                                  semilla: Optional[int] = None,
                                  origen_semilla: Optional[str] = None,
                                  callback=None) -> Tuple[List[Conexion], dict]:
    """
    Optimiza el orden de las conexiones con el AG (aptitud secuencial).

    La población inicial se siembra con el orden MPS y el orden por distancia;
    con elitismo, el AG nunca devuelve un orden peor (según la aptitud) que el
    mejor de esos heurísticos.

    Args:
        conexiones: Lista de conexiones a ordenar
        todos_los_pads, limite_tablero: Contexto del tablero (para el surrogate)
        fitness: 'surrogate' (A* grueso, fiel) o 'aproximada' (oclusión, rápida)
        paso_surrogate: Paso de cuadrícula del surrogate en mm (defecto 0.5)
        ancho_pista, clearance, anchos_por_red: Reglas usadas por el surrogate

    Returns:
        (conexiones_ordenadas, estadisticas_ag)

        estadisticas_ag incluye las mejoras por separado:
          mejora_vs_aleatorio / mejora_vs_distancia / mejora_vs_mps (en %),
          y los costos absolutos de cada referencia. Una mejora_vs_mps de 0.0%
          es legítima cuando MPS ya era óptimo (la semilla MPS ganó y el AG no
          la superó) — distinguible de un AG roto porque mejora_vs_aleatorio
          seguirá siendo > 0.
    """
    if len(conexiones) <= 3:
        print(f"[AG] Solo {len(conexiones)} conexiones — usando orden original")
        return conexiones, {
            'generaciones': 0, 'tamano_poblacion': 0,
            'mejor_costo': 0.0, 'tiempo_segundos': 0.0,
            'mejora_porcentual': 0.0,
            'mejora_vs_aleatorio': 0.0, 'mejora_vs_distancia': 0.0,
            'mejora_vs_mps': 0.0,
            'historial_mejor': [], 'historial_promedio': []
        }

    # Construir evaluador
    if fitness == 'aproximada':
        evaluador = EvaluadorAproximado(conexiones)
        print("[AG] Fitness: APROXIMADA (oclusión progresiva)")
    else:
        evaluador = EvaluadorSurrogate(
            conexiones, todos_los_pads, limite_tablero,
            paso=paso_surrogate, ancho_pista=ancho_pista,
            clearance=clearance, anchos_por_red=anchos_por_red
        )
        print(f"[AG] Fitness: SURROGATE (A* secuencial a paso {paso_surrogate}mm)")

    # Semilla del RNG: si no se pide una concreta, se toma de la entropía del
    # sistema y se REPORTA. Así cada corrida es independiente (necesario para
    # medir media y desviación entre repeticiones) pero sigue siendo
    # reproducible a posteriori pasando la semilla que quedó registrada.
    semilla_efectiva = (semilla if semilla is not None
                        else random.SystemRandom().randrange(2 ** 31))

    # El ORIGEN no se puede inferir de `semilla is not None`: quien llama puede
    # haber generado la semilla al azar y pasarla ya concreta (es lo que hace
    # best-of-N, que sortea N semillas del sistema antes de invocar). Inferirlo
    # marcaría como "fijada por el usuario" una semilla que el usuario nunca
    # escribió. Por eso el llamador lo declara; la inferencia queda sólo como
    # respaldo para invocaciones directas que no lo informen.
    if origen_semilla is None:
        origen_semilla = 'usuario' if semilla is not None else 'sistema'

    if origen_semilla == 'usuario':
        print(f"[AG] Semilla fija: {semilla_efectiva} (corrida reproducible)")
    else:
        print(f"[AG] Semilla aleatoria del sistema: {semilla_efectiva} "
              f"(use --semilla {semilla_efectiva} para repetir esta corrida)")

    # Órdenes heurísticos que se siembran en la población inicial
    semilla_mps = orden_por_mps(conexiones)
    semilla_dist = orden_por_distancia(conexiones)

    ag = AlgoritmoGenetico(
        conexiones=conexiones,
        funcion_aptitud=evaluador.evaluar,
        tamano_poblacion=tamano_poblacion,
        num_generaciones=num_generaciones,
        prob_cruce=prob_cruce,
        prob_mutacion=prob_mutacion,
        semillas=[semilla_mps, semilla_dist],
        semilla_aleatoria=semilla_efectiva,
        callback_generacion=callback
    )

    mejor_orden, mejor_aptitud, estadisticas = ag.evolucionar()

    # ── Métricas de mejora POR SEPARADO (punto 6) ────────────────────────────
    costo_mejor = -mejor_aptitud
    costo_mps = -evaluador.evaluar(semilla_mps)
    costo_dist = -evaluador.evaluar(semilla_dist)
    if ag.aptitudes_aleatorios_iniciales:
        costo_aleatorio = sum(-a for a in ag.aptitudes_aleatorios_iniciales) \
                          / len(ag.aptitudes_aleatorios_iniciales)
    else:
        costo_aleatorio = costo_mejor

    def _mejora(referencia: float) -> float:
        if referencia <= 0:
            return 0.0
        return round((referencia - costo_mejor) / referencia * 100.0, 2)

    estadisticas.update({
        'costo_mejor': round(costo_mejor, 2),
        'costo_aleatorio_promedio': round(costo_aleatorio, 2),
        'costo_distancia': round(costo_dist, 2),
        'costo_mps': round(costo_mps, 2),
        'mejora_vs_aleatorio': _mejora(costo_aleatorio),
        'mejora_vs_distancia': _mejora(costo_dist),
        'mejora_vs_mps': _mejora(costo_mps),
        # compat con reportes previos: mejora principal = vs aleatorio
        'mejora_porcentual': _mejora(costo_aleatorio),
        'fitness': fitness,
        'semilla': semilla_efectiva,
        'origen_semilla': origen_semilla,
    })

    if isinstance(evaluador, EvaluadorSurrogate):
        estadisticas['surrogate_hits_memo'] = evaluador.hits_memo
        estadisticas['surrogate_pasos_ahorrados'] = evaluador.pasos_ahorrados_prefijo

    print(f"\n[AG] Mejora vs aleatorio:  {estadisticas['mejora_vs_aleatorio']}%")
    print(f"[AG] Mejora vs distancia:  {estadisticas['mejora_vs_distancia']}%")
    print(f"[AG] Mejora vs MPS:        {estadisticas['mejora_vs_mps']}%")
    print(f"[AG] Orden optimizado: {mejor_orden}")

    conexiones_optimizadas = [conexiones[i] for i in mejor_orden]
    return conexiones_optimizadas, estadisticas


# ─────────────────────────────────────────────────────────────────────────────
# Ejecución directa para prueba
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from lector_pcb import leer_pcb, obtener_conexiones_a_enrutar

    pcb = leer_pcb("casos_prueba/mi_pcb.kicad_pcb")
    conexiones = obtener_conexiones_a_enrutar(pcb)

    print(f"Conexiones a optimizar: {len(conexiones)}")
    conexiones_opt, stats = optimizar_orden_enrutamiento(
        conexiones, pcb.todos_los_pads, pcb.limite_tablero,
        tamano_poblacion=12, num_generaciones=10
    )
    print(f"\nMejoras: aleatorio={stats['mejora_vs_aleatorio']}% "
          f"distancia={stats['mejora_vs_distancia']}% "
          f"mps={stats['mejora_vs_mps']}%")
