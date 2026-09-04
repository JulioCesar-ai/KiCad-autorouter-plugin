"""
verificar_pads.py
Verifica si los extremos de los segmentos coinciden con las posiciones de los pads.
Usa el lector_pcb para obtener las posiciones exactas de los pads.
"""
import sys, math, re

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lector_pcb import leer_pcb

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

if len(sys.argv) < 2:
    print("Uso: python verificar_pads.py <enrutado.kicad_pcb>")
    sys.exit(1)

ruta = sys.argv[1]
datos = leer_pcb(ruta)
segs  = extraer_segmentos_fcu(ruta)

# todos_los_pads es una lista de PadPCB con campos: x, y, referencia, numero_pad, nombre_red, capa
todos_pads = [p for p in datos.todos_los_pads if p.nombre_red != '']

print(f"Pads en F.Cu: {len(todos_pads)}")
print(f"Segmentos F.Cu: {len(segs)}")
print()

TOL = 0.26  # mm - radio del pad mas pequeño aproximado

# Para cada pad, ver si hay algun extremo de segmento cerca
print("=== Verificacion Pad → Segmento ===")
problemas = 0
for p in todos_pads:
    # Buscar segmentos de la misma red
    segs_red = [s for s in segs if s['net'] == p.nombre_red]

    # Ver si algun extremo del segmento toca el pad
    mejor_dist = 999
    for s in segs_red:
        d1 = math.hypot(p.x - s['x1'], p.y - s['y1'])
        d2 = math.hypot(p.x - s['x2'], p.y - s['y2'])
        mejor_dist = min(mejor_dist, d1, d2)

    if mejor_dist > TOL:
        problemas += 1
        print(f"  PROBLEMA: {p.referencia}.{p.numero_pad} @ ({p.x:.3f}, {p.y:.3f}) [{p.nombre_red}]")
        print(f"    Extremo de segmento mas cercano: {mejor_dist:.4f}mm (tolerancia: {TOL}mm)")
        # Mostrar segmentos mas cercanos
        segs_con_dist = []
        for s in segs_red:
            d1 = math.hypot(p.x - s['x1'], p.y - s['y1'])
            d2 = math.hypot(p.x - s['x2'], p.y - s['y2'])
            segs_con_dist.append((min(d1,d2), s))
        segs_con_dist.sort()
        for dist, s in segs_con_dist[:3]:
            print(f"    Seg: ({s['x1']:.3f},{s['y1']:.3f})->({s['x2']:.3f},{s['y2']:.3f}) dist={dist:.4f}mm")
    else:
        print(f"  OK: {p.referencia}.{p.numero_pad} @ ({p.x:.3f},{p.y:.3f}) [{p.nombre_red}] dist_min={mejor_dist:.4f}mm")

print()
if problemas == 0:
    print("Todos los pads estan conectados a segmentos.")
else:
    print(f"{problemas} pads tienen problemas de conexion.")
