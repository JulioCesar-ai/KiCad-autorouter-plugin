"""
verificar_conectividad.py
Verifica si todos los pads de cada red están conectados por los segmentos F.Cu.
"""
import sys, math, re

if len(sys.argv) < 2:
    print("Uso: python verificar_conectividad.py <archivo.kicad_pcb>")
    sys.exit(1)

ruta = sys.argv[1]
with open(ruta, encoding='utf-8') as f:
    contenido = f.read()

# --- Extraer pads ---
pads = []
for fp_match in re.finditer(r'\(footprint\s+"[^"]*"[^(]*(\(at\s+([\d.-]+)\s+([\d.-]+)(?:\s+[\d.-]+)?\))', contenido):
    fp_start = fp_match.start()
    # Encontrar el bloque completo del footprint
    depth = 0
    i = fp_start
    while i < len(contenido):
        if contenido[i] == '(':
            depth += 1
        elif contenido[i] == ')':
            depth -= 1
            if depth == 0:
                break
        i += 1
    fp_block = contenido[fp_start:i+1]

    fp_x = float(fp_match.group(2))
    fp_y = float(fp_match.group(3))

    # Referencia del footprint
    ref_match = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', fp_block)
    ref = ref_match.group(1) if ref_match else "?"

    # Rotación del footprint
    rot_match = re.search(r'\(at\s+[\d.-]+\s+[\d.-]+\s+([\d.-]+)\)', fp_match.group(1))
    fp_rot = float(rot_match.group(1)) if rot_match else 0.0

    # Extraer pads del footprint
    for pad_match in re.finditer(r'\(pad\s+"([^"]+)"\s+\w+\s+\w+\s+\(at\s+([\d.-]+)\s+([\d.-]+)(?:\s+[\d.-]+)?\)', fp_block):
        pad_num = pad_match.group(1)
        pad_rel_x = float(pad_match.group(2))
        pad_rel_y = float(pad_match.group(3))

        # Rotar la posicion del pad segun la rotacion del footprint
        if fp_rot != 0:
            rad = math.radians(fp_rot)
            cos_r = math.cos(rad)
            sin_r = math.sin(rad)
            rx = pad_rel_x * cos_r - pad_rel_y * sin_r
            ry = pad_rel_x * sin_r + pad_rel_y * cos_r
        else:
            rx, ry = pad_rel_x, pad_rel_y

        pad_x = fp_x + rx
        pad_y = fp_y + ry

        # Red del pad
        net_match = re.search(r'\(net\s+\d+\s+"([^"]+)"\)', pad_match.group(0))
        if not net_match:
            # Buscar en un contexto mayor del bloque del pad
            pad_start = fp_block.find(pad_match.group(0))
            pad_sub = fp_block[pad_start:pad_start+300]
            net_match = re.search(r'\(net\s+\d+\s+"([^"]+)"\)', pad_sub)

        if net_match:
            net = net_match.group(1)
            pads.append({'ref': ref, 'pad': pad_num, 'x': pad_x, 'y': pad_y, 'net': net})

# --- Extraer segmentos F.Cu ---
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

print(f"Pads encontrados: {len(pads)}")
print(f"Segmentos F.Cu: {len(segments)}")

# --- Verificar conectividad por red ---
# Agrupar pads y segmentos por red
redes_pads = {}
for p in pads:
    net = p['net']
    if net not in redes_pads:
        redes_pads[net] = []
    redes_pads[net].append(p)

redes_segs = {}
for s in segments:
    net = s['net']
    if net not in redes_segs:
        redes_segs[net] = []
    redes_segs[net].append(s)

TOL = 0.3  # mm - tolerancia para considerar que un punto toca un pad/extremo

def punto_cerca(px, py, x, y, tol=TOL):
    return math.hypot(px - x, py - y) <= tol

def conectados_bfs(nodos, aristas):
    """BFS para verificar conectividad. Nodos = lista de (x,y), aristas = lista de ((x1,y1),(x2,y2))"""
    if not nodos:
        return True
    visitados = set()
    cola = [0]
    visitados.add(0)
    while cola:
        actual = cola.pop(0)
        nx, ny = nodos[actual]
        # Ver qué aristas tocan este nodo
        for a in aristas:
            (ax1, ay1), (ax2, ay2) = a
            for ei, ep in enumerate(nodos):
                if ei in visitados:
                    continue
                ex, ey = ep
                if (punto_cerca(nx, ny, ax1, ay1) and punto_cerca(ex, ey, ax2, ay2)) or \
                   (punto_cerca(nx, ny, ax2, ay2) and punto_cerca(ex, ey, ax1, ay1)):
                    visitados.add(ei)
                    cola.append(ei)
    return len(visitados) == len(nodos)

print()
print("=== Verificacion de Conectividad por Red ===")
hay_problemas = False

for net, net_pads in sorted(redes_pads.items()):
    if net == '':
        continue
    if len(net_pads) < 2:
        continue  # Red con un solo pad, no necesita conexion

    net_segs = redes_segs.get(net, [])

    # Nodos = posicion de pads
    nodos = [(p['x'], p['y']) for p in net_pads]
    # Aristas = extremos de segmentos de esa red
    aristas = [((s['x1'], s['y1']), (s['x2'], s['y2'])) for s in net_segs]

    # Verificar que cada pad toca al menos un segmento
    pads_conectados = []
    for p in net_pads:
        toca = False
        for s in net_segs:
            if punto_cerca(p['x'], p['y'], s['x1'], s['y1']) or \
               punto_cerca(p['x'], p['y'], s['x2'], s['y2']):
                toca = True
                break
        pads_conectados.append(toca)

    todos_conectados = all(pads_conectados)

    if not todos_conectados or not net_segs:
        print(f"  PROBLEMA [{net}]: {len(net_pads)} pads, {len(net_segs)} segmentos")
        for i, (p, c) in enumerate(zip(net_pads, pads_conectados)):
            estado = "OK" if c else "SIN CONEXION"
            print(f"    {p['ref']}.{p['pad']} @ ({p['x']:.3f}, {p['y']:.3f}) -> {estado}")
        hay_problemas = True
    else:
        pad_info = ', '.join(f"{p['ref']}.{p['pad']}" for p in net_pads)
        print(f"  OK [{net}]: {len(net_pads)} pads ({pad_info}), {len(net_segs)} segs")

if not hay_problemas:
    print()
    print("  Todas las redes estan bien conectadas.")
