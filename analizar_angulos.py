"""Analiza los ángulos de los segmentos en un archivo .kicad_pcb"""
import sys, math, re

if len(sys.argv) < 2:
    print("Uso: python analizar_angulos.py <archivo.kicad_pcb>")
    sys.exit(1)

ruta = sys.argv[1]
with open(ruta, encoding='utf-8') as f:
    contenido = f.read()

# Dividir por bloques de segmento
blocks = re.split(r'\t\(segment', contenido)

total = 0
angulos_malos = 0
total_long = 0
resultados = []

for b in blocks[1:]:
    ms = re.search(r'\(start ([\d.]+) ([\d.]+)\)', b)
    me = re.search(r'\(end ([\d.]+) ([\d.]+)\)', b)
    ml = re.search(r'\(layer "F\.Cu"\)', b)
    mn = re.search(r'\(net "([^"]+)"\)', b)

    if not (ms and me and ml and mn):
        continue

    x1, y1 = float(ms.group(1)), float(ms.group(2))
    x2, y2 = float(me.group(1)), float(me.group(2))
    net = mn.group(1)

    dx = x2 - x1
    dy = y2 - y1
    lg = math.hypot(dx, dy)
    total_long += lg
    total += 1

    if lg < 1e-4:
        continue

    adx, ady = abs(dx), abs(dy)
    es_h = ady < 1e-3
    es_v = adx < 1e-3
    es_45 = abs(adx - ady) < 1e-3

    if not (es_h or es_v or es_45):
        angulos_malos += 1
        ang = math.degrees(math.atan2(dy, dx))
        resultados.append(f'  MAL: ({x1:.3f},{y1:.3f})->({x2:.3f},{y2:.3f}) [{net}] {ang:.1f} grados')

print(f'=== Análisis de Ángulos ===')
print(f'Archivo: {ruta}')
print(f'Total segmentos F.Cu: {total}')
print(f'Longitud total: {total_long:.2f} mm')
print(f'Ángulos limpios (0/45/90): {total - angulos_malos}/{total} ({100*(total-angulos_malos)/total:.0f}%)')
print(f'Ángulos irregulares: {angulos_malos}')
if resultados:
    print()
    for r in resultados:
        print(r)
else:
    print()
    print('  ✓ Todos los segmentos tienen ángulos válidos (0°/45°/90°)')
