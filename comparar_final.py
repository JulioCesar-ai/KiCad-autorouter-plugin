"""
comparar_final.py
Comparacion detallada entre enrutamiento original y del asistente.

Uso:
    python comparar_final.py <placa_original.kicad_pcb> <placa_enrutada.kicad_pcb>
"""
import sys, math, re, argparse
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lector_pcb import leer_pcb, obtener_conexiones_a_enrutar, _agrupar_pads_por_conectividad

def extraer_segmentos_fcu(ruta):
    with open(ruta, encoding='utf-8') as f:
        contenido = f.read()
    segments = []
    blocks = re.split(r'\t\(segment', contenido)
    for b in blocks[1:]:
        ms = re.search(r'\(start ([\d.-]+) ([\d.-]+)\)', b)
        me = re.search(r'\(end ([\d.-]+) ([\d.-]+)\)', b)
        ml = re.search(r'\(layer "F\.Cu"\)', b)
        mn = re.search(r'\(net "([^"]+)"\)', b)
        if ms and me and ml and mn:
            segments.append({
                'x1': float(ms.group(1)), 'y1': float(ms.group(2)),
                'x2': float(me.group(1)), 'y2': float(me.group(2)),
                'net': mn.group(1)
            })
    return segments

def angulos_limpios(segs):
    malos = 0
    for s in segs:
        dx = s['x2']-s['x1']; dy = s['y2']-s['y1']
        lg = math.hypot(dx, dy)
        if lg < 1e-4: continue
        adx, ady = abs(dx), abs(dy)
        if not (ady < 1e-3 or adx < 1e-3 or abs(adx-ady) < 1e-3):
            malos += 1
    return len(segs) - malos, malos

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('placa_original', help='Archivo .kicad_pcb original (sin enrutar)')
parser.add_argument('placa_enrutada', help='Archivo .kicad_pcb enrutado por el asistente')
args = parser.parse_args()

ruta_orig = args.placa_original
ruta_enr = args.placa_enrutada

segs_o = extraer_segmentos_fcu(ruta_orig)
segs_e = extraer_segmentos_fcu(ruta_enr)

long_o = sum(math.hypot(s['x2']-s['x1'], s['y2']-s['y1']) for s in segs_o)
long_e = sum(math.hypot(s['x2']-s['x1'], s['y2']-s['y1']) for s in segs_e)

ok_o, mal_o = angulos_limpios(segs_o)
ok_e, mal_e = angulos_limpios(segs_e)

print("=" * 60)
print("  COMPARACION: Original vs Asistente")
print("=" * 60)
print(f"{'Metrica':<30} {'Original':>12} {'Asistente':>12}")
print("-" * 56)
print(f"{'Segmentos F.Cu':<30} {len(segs_o):>12} {len(segs_e):>12}")
print(f"{'Longitud total (mm)':<30} {long_o:>12.2f} {long_e:>12.2f}")
print(f"{'Angulos limpios':<30} {ok_o:>11}/{len(segs_o)} {ok_e:>11}/{len(segs_e)}")
print(f"{'% angulos correctos':<30} {100*ok_o/len(segs_o):>11.0f}% {100*ok_e/len(segs_e):>11.0f}%")
print("-" * 56)

# Por red
print()
print("--- Por Red ---")
redes_o = {}
redes_e = {}
for s in segs_o:
    redes_o.setdefault(s['net'], []).append(s)
for s in segs_e:
    redes_e.setdefault(s['net'], []).append(s)

for net in sorted(set(list(redes_o.keys()) + list(redes_e.keys()))):
    so = redes_o.get(net, [])
    se = redes_e.get(net, [])
    lo = sum(math.hypot(s['x2']-s['x1'], s['y2']-s['y1']) for s in so)
    le = sum(math.hypot(s['x2']-s['x1'], s['y2']-s['y1']) for s in se)
    print(f"  {net:<25} orig={len(so):3d} segs ({lo:6.2f}mm)   asist={len(se):3d} segs ({le:6.2f}mm)")

print()
print("--- Analisis de Redundancia ---")
# Ver si el MST del original asigna dos conexiones desde el mismo pad de
# origen — no implica por si solo que el asistente vaya a duplicar trayecto
# (puede resolver la segunda como tap sobre el cobre de la primera, ver
# decidir_objetivo() en enrutador_astar.py), solo lo señala como candidato.
datos = leer_pcb(ruta_orig)
conexiones = obtener_conexiones_a_enrutar(datos)

pads_origen = {}
for c in conexiones:
    o, d = c[0], c[1]
    key = f"{o.referencia}.{o.numero_pad}"
    pads_origen.setdefault(key, []).append(f"{d.referencia}.{d.numero_pad}")

hay_redundancia = False
for pad, destinos in pads_origen.items():
    if len(destinos) > 1:
        hay_redundancia = True
        print(f"  Pad {pad} es origen de {len(destinos)} conexiones: {', '.join(destinos)}")
        print(f"    -> El MST le asigna {len(destinos)} aristas desde el mismo pad "
              f"(candidato a ruta redundante, no confirmado)")

if not hay_redundancia:
    print("  Ningun pad del MST original es origen de mas de una conexion.")

print()
print("=" * 60)
print("  DIAGNOSTICO")
print("=" * 60)
print()

if hay_redundancia:
    print("Posible causa de mas segmentos en el asistente:")
    print("  - El MST del original asigna varias conexiones desde el mismo pad")
    print("  - Si el asistente NO reconoce la segunda como tap sobre el cobre")
    print("    de la primera, la enruta pad-a-pad por separado en vez de")
    print("    encadenar A->B->C como suele hacerlo el enrutado manual")
    print()

# Conectividad real, calculada sobre el archivo ENRUTADO (no una cifra fija):
# cuenta como completa cada red con >=2 pads cuyo cobre F.Cu los una a todos
# (mismo criterio que verificar_conectividad en escritor_pcb.py).
print("Conectividad (calculada sobre la placa enrutada):")
datos_enr = leer_pcb(ruta_enr)
total_redes = 0
completas = 0
incompletas = []
for nombre, red in datos_enr.redes.items():
    if len(red.pads) < 2:
        continue
    total_redes += 1
    segs_red = [s for s in datos_enr.segmentos_existentes if s.nombre_red == nombre]
    grupos = _agrupar_pads_por_conectividad(red.pads, segs_red)
    if len(grupos) == 1:
        completas += 1
    else:
        incompletas.append(nombre)

if total_redes:
    print(f"  - {completas}/{total_redes} redes completas "
          f"({100*completas/total_redes:.0f}%)")
else:
    print("  - No hay redes con 2+ pads que evaluar")
if incompletas:
    print(f"  - Redes incompletas: {', '.join(sorted(incompletas))}")

zonas = datos_enr.redes_con_zona_fcu
if zonas:
    print()
    print(f"Redes cubiertas por zona de cobre (no se enrutan con pistas): "
          f"{', '.join(sorted(zonas))}")
    print("  - Sus pads no muestran ratsnest resuelto hasta ejecutar")
    print("    'Edit -> Fill All Zones' (tecla B) en KiCad")

print()
print("NOTA: este script NO corre el DRC nativo de KiCad ni verifica clearance —")
print("      solo compara segmentos F.Cu y conectividad geometrica. Para un")
print("      veredicto de fabricabilidad, use el DRC de KiCad sobre el archivo")
print("      enrutado.")
