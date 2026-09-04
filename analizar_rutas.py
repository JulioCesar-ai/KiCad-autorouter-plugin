import re
import sys
from collections import defaultdict

orig_path = r'C:\Users\fuent\Downloads\Pcbpruebas\Proyectos_KiCAD\Regulador_tension_5V\Regulador_tension_5V.kicad_pcb'
rout_path = r'C:\Users\fuent\Downloads\Pcbpruebas\Proyectos_KiCAD\Regulador_tension_5V\Regulador_tension_5V_enrutado_20260510_095022.kicad_pcb'

orig = open(orig_path, encoding='utf-8').read()
rout = open(rout_path, encoding='utf-8').read()

def get_segments(content):
    segs = []
    i = 0
    while i < len(content):
        pos = content.find('\t(segment\n', i)
        if pos == -1:
            break
        depth, j = 0, pos
        while j < len(content):
            if content[j] == '(':
                depth += 1
            elif content[j] == ')':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        segs.append(content[pos:j+1])
        i = j + 1
    return segs

def parse_net(seg):
    m = re.search(r'\(net "([^"]+)"\)', seg)
    return m.group(1) if m else '?'

def parse_coords(seg):
    s = re.search(r'\(start ([\d.\-]+) ([\d.\-]+)\)', seg)
    e = re.search(r'\(end ([\d.\-]+) ([\d.\-]+)\)', seg)
    if s and e:
        return float(s.group(1)), float(s.group(2)), float(e.group(1)), float(e.group(2))
    return None

orig_segs = get_segments(orig)
rout_segs = get_segments(rout)

print(f'Original: {len(orig_segs)} segmentos')
print(f'Enrutado: {len(rout_segs)} segmentos')
print()

orig_by_net = defaultdict(list)
rout_by_net = defaultdict(list)
for s in orig_segs:
    orig_by_net[parse_net(s)].append(s)
for s in rout_segs:
    rout_by_net[parse_net(s)].append(s)

print('=== REDES EN ORIGINAL ===')
for net, segs in sorted(orig_by_net.items()):
    total_len = 0
    for s in segs:
        c = parse_coords(s)
        if c:
            import math
            total_len += math.hypot(c[2]-c[0], c[3]-c[1])
    print(f'  {net}: {len(segs)} segs, {total_len:.2f}mm total')

print()
print('=== REDES EN ENRUTADO (solo F.Cu) ===')
for net, segs in sorted(rout_by_net.items()):
    total_len = 0
    for s in segs:
        c = parse_coords(s)
        if c:
            import math
            total_len += math.hypot(c[2]-c[0], c[3]-c[1])
    print(f'  {net}: {len(segs)} segs, {total_len:.2f}mm total')

print()
# Show redes that are in original but missing/different in enrutado
print('=== COMPARACION DE REDES ===')
all_nets = set(list(orig_by_net.keys()) + list(rout_by_net.keys()))
for net in sorted(all_nets):
    orig_n = len(orig_by_net.get(net, []))
    rout_n = len(rout_by_net.get(net, []))
    status = 'OK' if orig_n > 0 and rout_n > 0 else ('FALTA' if orig_n > 0 and rout_n == 0 else 'NUEVO')
    print(f'  {net}: original={orig_n}, enrutado={rout_n} [{status}]')

# Check if any segments go outside the board boundary
print()
print('=== VERIFICACION LIMITES ===')
# Board edge from original: look for Edge.Cuts
edge_x = re.findall(r'\(gr_rect.*?\(start ([\d.\-]+) ([\d.\-]+)\).*?\(end ([\d.\-]+) ([\d.\-]+)\)', orig, re.DOTALL)
if not edge_x:
    # try xy format
    edge_xy = re.findall(r'Edge\.Cuts.*?(\d+\.\d+)', orig)
    print(f'  Edge.Cuts encontrado: {edge_xy[:4]}')
else:
    print(f'  Board rect: {edge_x}')

# Show coordinate range of generated segments
all_coords = []
for s in rout_segs:
    c = parse_coords(s)
    if c:
        all_coords.extend([(c[0], c[1]), (c[2], c[3])])

if all_coords:
    xs = [p[0] for p in all_coords]
    ys = [p[1] for p in all_coords]
    print(f'  Rango X segmentos: {min(xs):.2f} - {max(xs):.2f} mm')
    print(f'  Rango Y segmentos: {min(ys):.2f} - {max(ys):.2f} mm')
