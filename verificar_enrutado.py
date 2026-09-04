"""
verificar_enrutado.py
Usa el lector_pcb existente para verificar que todas las conexiones estan enrutadas.
Compara el enrutado del asistente contra el original.
"""
import sys, math, re
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lector_pcb import leer_pcb, obtener_conexiones_a_enrutar

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

def longitud_total(segs):
    return sum(math.hypot(s['x2']-s['x1'], s['y2']-s['y1']) for s in segs)

def verificar_conexion(pad_o, pad_d, segs_red, tol=0.35):
    """Verifica si dos pads están conectados mediante BFS sobre segmentos."""
    ox, oy = pad_o.x, pad_o.y
    dx, dy = pad_d.x, pad_d.y

    # BFS: nodo = punto, aristas = segmentos
    # Un punto "toca" un extremo de segmento si dist < tol
    def toca(px, py, ex, ey):
        return math.hypot(px-ex, py-ey) < tol

    visitados = set()  # puntos visitados como frozenset de índices de segmento
    frontera = [(ox, oy)]
    puntos_visitados = [(ox, oy)]

    for _ in range(1000):
        nuevos = []
        for fx, fy in frontera:
            # Ver si llegamos al destino
            if toca(fx, fy, dx, dy):
                return True
            for s in segs_red:
                if toca(fx, fy, s['x1'], s['y1']):
                    np = (s['x2'], s['y2'])
                elif toca(fx, fy, s['x2'], s['y2']):
                    np = (s['x1'], s['y1'])
                else:
                    continue
                # Ver si ya visitamos este punto
                ya_visitado = any(toca(np[0], np[1], vp[0], vp[1]) for vp in puntos_visitados)
                if not ya_visitado:
                    nuevos.append(np)
                    puntos_visitados.append(np)
        if not nuevos:
            break
        frontera = nuevos

    return False

if len(sys.argv) < 3:
    print("Uso: python verificar_enrutado.py <original.kicad_pcb> <enrutado.kicad_pcb>")
    sys.exit(1)

ruta_orig = sys.argv[1]
ruta_enr  = sys.argv[2]

print("=== Leyendo PCBs ===")
datos = leer_pcb(ruta_orig)
conexiones = obtener_conexiones_a_enrutar(datos)

segs_orig = extraer_segmentos_fcu(ruta_orig)
segs_enr  = extraer_segmentos_fcu(ruta_enr)

print(f"\nOriginal:  {len(segs_orig)} segmentos F.Cu, {longitud_total(segs_orig):.2f}mm")
print(f"Enrutado:  {len(segs_enr)} segmentos F.Cu, {longitud_total(segs_enr):.2f}mm")
print(f"Conexiones a verificar: {len(conexiones)}")

# Agrupar segmentos por red
def segs_por_red(segs):
    d = {}
    for s in segs:
        d.setdefault(s['net'], []).append(s)
    return d

orig_by_net = segs_por_red(segs_orig)
enr_by_net  = segs_por_red(segs_enr)

print("\n=== Verificacion de Conexiones ===")
print(f"{'Conexion':<40} {'Original':<12} {'Asistente':<12}")
print("-" * 65)

exitosas = 0
fallidas = 0

for c in conexiones:
    o, d = c[0], c[1]
    red = o.nombre_red
    label = f"{o.referencia}.{o.numero_pad}->{d.referencia}.{d.numero_pad} [{red}]"

    orig_segs = orig_by_net.get(red, [])
    enr_segs  = enr_by_net.get(red, [])

    ok_orig = verificar_conexion(o, d, orig_segs)
    ok_enr  = verificar_conexion(o, d, enr_segs)

    orig_str = "OK" if ok_orig else "FALTA"
    enr_str  = "OK" if ok_enr  else "FALTA"

    if not ok_enr:
        fallidas += 1
        print(f"  {label:<38} {orig_str:<12} {enr_str:<12} <<< PROBLEMA")
    else:
        exitosas += 1
        print(f"  {label:<38} {orig_str:<12} {enr_str:<12}")

print("-" * 65)
print(f"\nConexiones completas: {exitosas}/{len(conexiones)}")
if fallidas:
    print(f"Conexiones faltantes: {fallidas}")
else:
    print("Todas las conexiones estan completas.")

# Analisis de angulos
print("\n=== Analisis de Angulos ===")
for nombre, segs in [("Original", segs_orig), ("Asistente", segs_enr)]:
    malos = 0
    for s in segs:
        dx = s['x2']-s['x1']; dy = s['y2']-s['y1']
        lg = math.hypot(dx, dy)
        if lg < 1e-4: continue
        adx, ady = abs(dx), abs(dy)
        if not (ady < 1e-3 or adx < 1e-3 or abs(adx-ady) < 1e-3):
            malos += 1
    pct = 100*(len(segs)-malos)/len(segs) if segs else 0
    print(f"  {nombre}: {len(segs)} segs, {longitud_total(segs):.2f}mm, angulos limpios: {len(segs)-malos}/{len(segs)} ({pct:.0f}%)")
