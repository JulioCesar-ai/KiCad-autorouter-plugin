"""
validar_surrogate.py — Validación del surrogate de aptitud (A* con cuadrícula gruesa)

Pregunta que responde: ¿el enrutador secuencial a paso grueso (0.5mm / 1.0mm)
produce aptitudes DISTINTAS para permutaciones distintas del mismo conjunto de
conexiones, o los pasajes entre pads THT se cierran y todo falla por igual?

Método:
  1. Carga una placa y obtiene sus conexiones (MST por red).
  2. Genera N permutaciones aleatorias (semilla fija, mismas para cada paso).
  3. Evalúa cada permutación con el surrogate: enrutamiento SECUENCIAL con A*
     al paso indicado — cada ruta trazada se convierte en obstáculo para las
     siguientes. Sin reintentos de clearance (un fallo es un fallo).
  4. Reporta por paso: mín/máx/media/desv.est. del costo, % de conexiones
     resueltas, y tiempo por evaluación.

Uso:
    python validar_surrogate.py <placa.kicad_pcb> [--perms 20]
                                [--ancho 1.0] [--clearance 0.3]
                                [--pasos 1.0 0.5]
"""

import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import io
import math
import time
import random
import argparse
import contextlib
from typing import List, Tuple

from lector_pcb import leer_pcb, obtener_conexiones_a_enrutar, PadPCB
from enrutador_astar import EnrutadorAstar


def evaluar_orden_surrogate(orden: List[int],
                            conexiones: List[Tuple[PadPCB, PadPCB]],
                            todos_los_pads: List[PadPCB],
                            limite_tablero: Tuple[float, float, float, float],
                            paso: float,
                            ancho_pista: float,
                            clearance: float,
                            penalizacion_fallo: float) -> Tuple[float, int, float]:
    """
    Evalúa una permutación con el enrutador secuencial a paso grueso.

    Returns:
        (costo, conexiones_resueltas, longitud_total)
        costo = longitud_total + penalizacion_fallo * fallos
    """
    # max_iteraciones bajo: en cuadrícula gruesa un A* que no encuentra camino
    # debe fallar rápido, no quemar tiempo explorando todo el tablero.
    from enrutador_astar import decidir_objetivo

    enrutador = EnrutadorAstar(limite_tablero, paso, max_iteraciones=30_000)

    segmentos_acumulados: List[dict] = []
    longitud_total = 0.0
    resueltas = 0

    for idx in orden:
        origen, destino = conexiones[idx]
        red = origen.nombre_red

        # Reconstruir obstáculos: pads (menos la red activa) + rutas ya
        # trazadas, EXCLUYENDO el cobre de la propia red (variante B).
        enrutador.limpiar_obstaculos()
        enrutador.agregar_pads_como_obstaculos(todos_los_pads, clearance,
                                               red_a_ignorar=red)
        for s in segmentos_acumulados:
            if s.get('red') == red:
                continue
            enrutador.marcar_ruta_como_obstaculo([s], ancho_pista, clearance)

        # Misma decisión de tap/omitir que usa el enrutador real
        cobre_red = [s for s in segmentos_acumulados if s.get('red') == red]
        accion, pad_a_enrutar, celdas_objetivo = decidir_objetivo(
            origen, destino, cobre_red, enrutador)

        if accion == 'omitir':
            resueltas += 1
            continue
        if accion == 'tap':
            segmentos = enrutador.enrutar_a_objetivos(
                pad_a_enrutar, celdas_objetivo, ancho_pista, clearance)
        else:
            segmentos = enrutador.enrutar_conexion(origen, destino,
                                                   ancho_pista, clearance)

        if segmentos is not None:
            resueltas += 1
            longitud_total += sum(
                math.hypot(s['x2'] - s['x1'], s['y2'] - s['y1'])
                for s in segmentos
            )
            segmentos_acumulados.extend(segmentos)
        # Fallo (None): no acumula obstáculo (la conexión no se trazó)

    fallos = len(orden) - resueltas
    costo = longitud_total + penalizacion_fallo * fallos
    return costo, resueltas, longitud_total


def estadisticas(valores: List[float]) -> dict:
    n = len(valores)
    media = sum(valores) / n
    var = sum((v - media) ** 2 for v in valores) / n
    return {
        'min': min(valores),
        'max': max(valores),
        'media': media,
        'desv': math.sqrt(var),
    }


def main():
    parser = argparse.ArgumentParser(description='Validación del surrogate de aptitud')
    parser.add_argument('placa', help='Archivo .kicad_pcb a analizar')
    parser.add_argument('--perms', type=int, default=20,
                        help='Número de permutaciones aleatorias (defecto: 20)')
    parser.add_argument('--ancho', type=float, default=1.0,
                        help='Ancho de pista en mm (defecto: 1.0)')
    parser.add_argument('--clearance', type=float, default=0.3,
                        help='Clearance en mm (defecto: 0.3)')
    parser.add_argument('--pasos', type=float, nargs='+', default=[1.0, 0.5],
                        help='Pasos de cuadrícula a comparar (defecto: 1.0 0.5)')
    parser.add_argument('--semilla', type=int, default=42)
    args = parser.parse_args()

    print(f"Placa: {args.placa}")
    print(f"Parámetros: ancho={args.ancho}mm  clearance={args.clearance}mm  "
          f"permutaciones={args.perms}  semilla={args.semilla}")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        datos = leer_pcb(args.placa)
        conexiones = obtener_conexiones_a_enrutar(datos)

    n = len(conexiones)
    print(f"Conexiones a enrutar: {n}   Pads: {len(datos.todos_los_pads)}")
    min_x, min_y, max_x, max_y = datos.limite_tablero
    diagonal = math.hypot(max_x - min_x, max_y - min_y)
    penalizacion = 2.0 * diagonal
    print(f"Tablero: {max_x-min_x:.1f}×{max_y-min_y:.1f}mm  "
          f"(penalización por fallo: {penalizacion:.1f}mm)")

    if n < 2:
        print("Muy pocas conexiones para validar dispersión. Abortando.")
        return

    # Mismas permutaciones para todos los pasos (comparación justa)
    rng = random.Random(args.semilla)
    permutaciones = []
    for _ in range(args.perms):
        p = list(range(n))
        rng.shuffle(p)
        permutaciones.append(p)

    for paso in args.pasos:
        print(f"\n{'='*66}")
        print(f"  PASO DE CUADRÍCULA: {paso}mm")
        print(f"{'='*66}")

        costos, pct_resueltas, tiempos = [], [], []

        for i, perm in enumerate(permutaciones):
            t0 = time.time()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                costo, resueltas, longitud = evaluar_orden_surrogate(
                    perm, conexiones, datos.todos_los_pads,
                    datos.limite_tablero, paso,
                    args.ancho, args.clearance, penalizacion
                )
            dt = time.time() - t0
            costos.append(costo)
            pct_resueltas.append(100.0 * resueltas / n)
            tiempos.append(dt)
            print(f"  perm {i+1:2d}: costo={costo:9.2f}  "
                  f"resueltas={resueltas:2d}/{n} ({100.0*resueltas/n:5.1f}%)  "
                  f"longitud={longitud:8.2f}mm  t={dt:.2f}s")

        est = estadisticas(costos)
        est_res = estadisticas(pct_resueltas)
        rango = est['max'] - est['min']
        print(f"\n  ── Estadísticas del costo ──")
        print(f"  mín={est['min']:.2f}  máx={est['max']:.2f}  "
              f"media={est['media']:.2f}  desv={est['desv']:.2f}")
        print(f"  rango={rango:.2f}  "
              f"coef.variación={100.0*est['desv']/est['media'] if est['media'] else 0:.2f}%")
        print(f"  ── Conexiones resueltas ──")
        print(f"  mín={est_res['min']:.1f}%  máx={est_res['max']:.1f}%  "
              f"media={est_res['media']:.1f}%")
        print(f"  ── Tiempo por evaluación ──")
        print(f"  media={sum(tiempos)/len(tiempos):.2f}s  "
              f"total {args.perms} perms={sum(tiempos):.1f}s")

        # Veredicto automático
        distintos = len(set(round(c, 6) for c in costos))
        print(f"\n  Valores de costo distintos: {distintos}/{args.perms}")
        if distintos == 1:
            print("  ✗ VEREDICTO: aptitud CONSTANTE — este paso NO sirve para el AG")
        elif est['desv'] / est['media'] < 0.005 if est['media'] else True:
            print("  ⚠ VEREDICTO: dispersión casi nula — señal débil para el AG")
        else:
            print("  ✓ VEREDICTO: hay dispersión — el orden afecta la aptitud")


if __name__ == "__main__":
    main()
