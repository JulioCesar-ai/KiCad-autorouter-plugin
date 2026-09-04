"""
medir_sesgo_surrogate.py — ¿Cuánto se desvía el surrogate del enrutado real?

El AG optimiza contra el surrogate (A* secuencial en cuadrícula gruesa), pero
el resultado que importa es el enrutado a resolución completa (0.25mm). Si el
surrogate subestima de forma sistemática, el AG puede sobreajustarse a esa
aproximación: elegir órdenes que puntúan bien en la métrica barata y mal en la
real.

Este script mide, para cada paso de surrogate:
  - Correlación de Pearson entre costo surrogate y longitud real
  - Sesgo promedio (real / surrogate): >1 = el surrogate subestima
  - Tiempo por evaluación

Ambos enrutados usan exactamente las mismas reglas (variante B + tap vía
`decidir_objetivo`), así que la única diferencia es la resolución.

Uso:
    python medir_sesgo_surrogate.py <placa.kicad_pcb> [--perms 15]
                                    [--pasos 0.5 0.375]
                                    [--ancho 0.4] [--clearance 0.2]
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
from enrutador_astar import enrutar_todos
from optimizador_genetico import EvaluadorSurrogate


def correlacion_pearson(xs: List[float], ys: List[float]) -> float:
    """Coeficiente de correlación de Pearson entre dos series."""
    n = len(xs)
    if n < 2:
        return float('nan')
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx < 1e-12 or dy < 1e-12:
        return float('nan')
    return num / (dx * dy)


def enrutar_real(orden: List[int], conexiones, datos,
                 ancho: float, clearance: float) -> Tuple[float, int, int]:
    """
    Enruta un orden a resolución completa (0.25mm).

    Returns:
        (longitud_total, fallidas, redes_desconectadas)

    Nota: la longitud devuelta es cruda. Para comparar contra el surrogate hay
    que aplicarle la MISMA penalización por fallo que usa el evaluador, porque
    el costo surrogate ya la incluye — comparar costo penalizado contra
    longitud pelada mezcla dos magnitudes distintas.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        segs, met = enrutar_todos(
            [conexiones[i] for i in orden],
            datos.todos_los_pads,
            datos.limite_tablero,
            paso_cuadricula=0.25,
            ancho_pista=ancho,
            clearance=clearance,
            segmentos_existentes=datos.segmentos_existentes,
            estrategia_orden='ninguna',   # respetar el orden dado
        )
        from escritor_pcb import verificar_conectividad
        conect = verificar_conectividad(datos, segs, ancho)
    return (met['longitud_total_mm'], met['fallidas'],
            conect['total'] - conect['completas'])


def main():
    parser = argparse.ArgumentParser(
        description='Mide el sesgo del surrogate frente al enrutado real')
    parser.add_argument('placa', help='Archivo .kicad_pcb')
    parser.add_argument('--perms', type=int, default=15,
                        help='Permutaciones aleatorias a evaluar (defecto: 15)')
    parser.add_argument('--pasos', type=float, nargs='+', default=[0.5, 0.375],
                        help='Pasos de surrogate a comparar (defecto: 0.5 0.375)')
    parser.add_argument('--ancho', type=float, default=0.4)
    parser.add_argument('--clearance', type=float, default=0.2)
    parser.add_argument('--semilla', type=int, default=42,
                        help='Semilla de las permutaciones (fija, para que los '
                             'pasos se comparen sobre los MISMOS órdenes)')
    args = parser.parse_args()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        datos = leer_pcb(args.placa)
        conexiones = obtener_conexiones_a_enrutar(datos)

    n = len(conexiones)
    print(f"Placa: {args.placa}")
    print(f"Conexiones: {n}   Reglas: ancho={args.ancho}mm "
          f"clearance={args.clearance}mm")
    print(f"Permutaciones: {args.perms} (semilla {args.semilla}, "
          f"las mismas para todos los pasos)\n")

    # Mismas permutaciones para todos los pasos: comparación pareada
    rng = random.Random(args.semilla)
    permutaciones = []
    for _ in range(args.perms):
        p = list(range(n))
        rng.shuffle(p)
        permutaciones.append(p)

    # Penalización por fallo: la MISMA que aplica EvaluadorSurrogate, para que
    # el costo real y el surrogate sean la misma magnitud.
    min_x, min_y, max_x, max_y = datos.limite_tablero
    penalizacion = 2.0 * math.hypot(max_x - min_x, max_y - min_y)
    print(f"Penalización por conexión fallida: {penalizacion:.1f}mm "
          f"(2x la diagonal del tablero)\n")

    # ── Enrutado real (referencia, 0.25mm) — una sola vez ────────────────────
    print("Enrutando a resolución completa (0.25mm) — referencia...")
    reales, longitudes_crudas, fallidas_reales, desconectadas = [], [], [], []
    t0 = time.time()
    for i, perm in enumerate(permutaciones):
        L, fal, desc = enrutar_real(perm, conexiones, datos,
                                    args.ancho, args.clearance)
        costo_real = L + penalizacion * fal
        reales.append(costo_real)
        longitudes_crudas.append(L)
        fallidas_reales.append(fal)
        desconectadas.append(desc)
        print(f"  perm {i+1:2d}: costo={costo_real:9.2f}  ({L:8.2f}mm + "
              f"{fal} fallida(s))  redes abiertas={desc}")
    t_real = (time.time() - t0) / len(permutaciones)
    print(f"  tiempo medio por enrutado real: {t_real:.2f}s\n")

    # ── Surrogate en cada paso ───────────────────────────────────────────────
    filas = []
    for paso in args.pasos:
        print(f"Surrogate a paso {paso}mm...")
        with contextlib.redirect_stdout(buf):
            ev = EvaluadorSurrogate(
                conexiones, datos.todos_los_pads, datos.limite_tablero,
                paso=paso, ancho_pista=args.ancho, clearance=args.clearance)
        costos = []
        t0 = time.time()
        for perm in permutaciones:
            with contextlib.redirect_stdout(buf):
                costos.append(-ev.evaluar(perm))
        t_sur = (time.time() - t0) / len(permutaciones)

        r = correlacion_pearson(costos, reales)
        sesgos = [real / cost for real, cost in zip(reales, costos) if cost > 1e-9]
        sesgo_medio = sum(sesgos) / len(sesgos)
        sesgo_min, sesgo_max = min(sesgos), max(sesgos)
        filas.append((paso, r, sesgo_medio, sesgo_min, sesgo_max, t_sur))
        print(f"  correlación={r:.3f}  sesgo medio={sesgo_medio:.3f}  "
              f"tiempo={t_sur:.2f}s\n")

    # ── Tabla final ──────────────────────────────────────────────────────────
    print("=" * 78)
    print(f"  SESGO DEL SURROGATE — {args.perms} permutaciones aleatorias")
    print("=" * 78)
    print(f"  {'paso':>7}  {'Pearson':>8}  {'sesgo medio':>12}  "
          f"{'rango sesgo':>16}  {'t/eval':>8}  {'vs real':>8}")
    for paso, r, sm, smin, smax, t in filas:
        print(f"  {paso:>6.3f}mm  {r:>8.3f}  {sm:>12.3f}  "
              f"{smin:>6.2f}–{smax:<8.2f}  {t:>7.2f}s  {t/t_real:>7.1%}")
    print()
    print(f"  Referencia real (0.25mm): {t_real:.2f}s por enrutado")
    print("  Ambos costos = longitud + 2x diagonal por conexión fallida")
    print("  sesgo = costo_real / costo_surrogate  (>1 = el surrogate subestima)")
    print(f"  Fallos en el enrutado real: "
          f"{sum(1 for f in fallidas_reales if f)}/{len(fallidas_reales)} "
          f"permutaciones con al menos una")


if __name__ == "__main__":
    main()
