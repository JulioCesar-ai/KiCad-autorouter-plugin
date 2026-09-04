"""
test_regresion_aptitud.py — Test de regresión contra la aptitud constante

BUG HISTÓRICO QUE ESTE TEST PREVIENE:
La aptitud original del AG era longitud_estimada + peso×cruces, dos términos
invariantes ante permutaciones (una suma no depende del orden de los sumandos;
el conjunto de pares i<j es el mismo en cualquier orden). El AG optimizaba una
función constante y nadie lo notó hasta inspeccionar los logs de convergencia.

Este test afirma que AMBOS evaluadores devuelven valores DISTINTOS para dos
permutaciones distintas del mismo conjunto de conexiones. Si alguien
reintroduce una aptitud invariante al orden, este test falla.

Uso:
    python test_regresion_aptitud.py
"""

import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import io
import contextlib

from lector_pcb import PadPCB
from optimizador_genetico import EvaluadorSurrogate, EvaluadorAproximado


def _pad(x, y, red, ref, num='1'):
    return PadPCB(x=x, y=y, referencia=ref, numero_pad=num,
                  nombre_red=red, ancho=1.0, alto=1.0,
                  es_smd=True, forma='rect')


def construir_escenario():
    """
    Escenario sintético donde el orden importa por construcción:

      - Conexión 0: horizontal larga (1,5)→(21,5), red A
      - Conexión 1: vertical (11,1)→(11,11), red B — CRUZA a la conexión 0
      - Conexión 2: corta e independiente (1,1)→(3,1), red C

    Quien se enruta segundo entre 0 y 1 debe rodear la pista del primero.
    Los rodeos son de longitudes muy distintas (la horizontal mide 20mm, la
    vertical 10mm), así que [0,1,2] y [1,0,2] DEBEN tener costos distintos.
    """
    conexiones = [
        (_pad(1.0, 5.0, 'NET_A', 'J1'), _pad(21.0, 5.0, 'NET_A', 'J2')),
        (_pad(11.0, 1.0, 'NET_B', 'U1'), _pad(11.0, 11.0, 'NET_B', 'U2')),
        (_pad(1.0, 1.0, 'NET_C', 'R1'), _pad(3.0, 1.0, 'NET_C', 'R2')),
    ]
    todos_los_pads = [p for c in conexiones for p in c]
    limite_tablero = (0.0, 0.0, 23.0, 13.0)
    return conexiones, todos_los_pads, limite_tablero


def main():
    conexiones, pads, limite = construir_escenario()
    orden_a = [0, 1, 2]
    orden_b = [1, 0, 2]
    fallos = []

    # ── EvaluadorSurrogate ───────────────────────────────────────────────────
    with contextlib.redirect_stdout(io.StringIO()):
        surrogate = EvaluadorSurrogate(conexiones, pads, limite,
                                       paso=0.5, ancho_pista=0.3, clearance=0.2)
        apt_sa = surrogate.evaluar(orden_a)
        apt_sb = surrogate.evaluar(orden_b)

    print(f"Surrogate:  aptitud({orden_a}) = {apt_sa:.4f}")
    print(f"            aptitud({orden_b}) = {apt_sb:.4f}")
    if apt_sa == apt_sb:
        fallos.append("EvaluadorSurrogate devolvió la MISMA aptitud para dos "
                      "permutaciones distintas — aptitud invariante al orden")
    else:
        print("  ✓ Surrogate distingue permutaciones")

    # Caché de permutaciones: re-evaluar debe dar exactamente lo mismo
    with contextlib.redirect_stdout(io.StringIO()):
        apt_sa2 = surrogate.evaluar(orden_a)
    if apt_sa2 != apt_sa:
        fallos.append("El caché del surrogate devolvió un valor distinto al "
                      "re-evaluar la misma permutación")
    else:
        print("  ✓ Caché de permutaciones consistente")

    # ── EvaluadorAproximado ──────────────────────────────────────────────────
    aproximado = EvaluadorAproximado(conexiones)
    apt_aa = aproximado.evaluar(orden_a)
    apt_ab = aproximado.evaluar(orden_b)

    print(f"Aproximada: aptitud({orden_a}) = {apt_aa:.4f}")
    print(f"            aptitud({orden_b}) = {apt_ab:.4f}")
    if apt_aa == apt_ab:
        fallos.append("EvaluadorAproximado devolvió la MISMA aptitud para dos "
                      "permutaciones distintas — aptitud invariante al orden")
    else:
        print("  ✓ Aproximada distingue permutaciones")

    # ── Resultado ────────────────────────────────────────────────────────────
    print()
    if fallos:
        for f in fallos:
            print(f"✗ FALLO: {f}")
        sys.exit(1)
    print("✓ TODOS LOS TESTS PASARON — la aptitud depende del orden")
    sys.exit(0)


if __name__ == "__main__":
    main()
