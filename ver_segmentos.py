"""ver_segmentos.py - Muestra los segmentos detallados de un archivo .kicad_pcb"""
import sys, math, re

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

ruta = sys.argv[1] if len(sys.argv) > 1 else None
red_filtro = sys.argv[2] if len(sys.argv) > 2 else None

if not ruta:
    print("Uso: python ver_segmentos.py <archivo.kicad_pcb> [red]")
    sys.exit(1)

with open(ruta, encoding='utf-8') as f:
    contenido = f.read()

segs = []
blocks = re.split(r'\t\(segment', contenido)
for b in blocks[1:]:
    ms = re.search(r'\(start ([\d.-]+) ([\d.-]+)\)', b)
    me = re.search(r'\(end ([\d.-]+) ([\d.-]+)\)', b)
    ml = re.search(r'\(layer "F\.Cu"\)', b)
    mn = re.search(r'\(net "([^"]+)"\)', b)
    if ms and me and ml and mn:
        segs.append({
            'x1': float(ms.group(1)), 'y1': float(ms.group(2)),
            'x2': float(me.group(1)), 'y2': float(me.group(2)),
            'net': mn.group(1)
        })

# Agrupar por red
redes = {}
for s in segs:
    redes.setdefault(s['net'], []).append(s)

for net, net_segs in sorted(redes.items()):
    if red_filtro and red_filtro not in net:
        continue
    long_total = sum(math.hypot(s['x2']-s['x1'], s['y2']-s['y1']) for s in net_segs)
    print(f"\n=== {net} === ({len(net_segs)} segs, {long_total:.3f}mm)")
    for i, s in enumerate(net_segs):
        dx = s['x2']-s['x1']; dy = s['y2']-s['y1']
        lg = math.hypot(dx, dy)
        adx, ady = abs(dx), abs(dy)
        if ady < 1e-3: tipo = "H"
        elif adx < 1e-3: tipo = "V"
        elif abs(adx-ady) < 1e-3: tipo = "45"
        else: tipo = f"?{math.degrees(math.atan2(dy,dx)):.0f}"
        print(f"  [{i+1}] ({s['x1']:.3f},{s['y1']:.3f}) -> ({s['x2']:.3f},{s['y2']:.3f})  {tipo}  {lg:.3f}mm")
