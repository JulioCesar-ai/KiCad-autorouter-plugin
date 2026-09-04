"""
Análisis de fragmentación del plano de cobre (zona) — sin dependencia de pcbnew.

PROBLEMA QUE RESUELVE
---------------------
En una placa de una cara, las redes cubiertas por una zona de cobre (típicamente
GND) no se enrutan: se dan por conectadas a través del plano. Pero el plano sólo
conecta si sigue siendo *una sola pieza*. Las pistas de las demás redes, con su
clearance, actúan como cortes; si un conjunto de cortes llega a atravesar el
plano de lado a lado, éste queda partido en regiones eléctricamente separadas y
los pads de la zona que caen en una región distinta a la principal quedan
desconectados.

Medido sobre una corrida real (32 conexiones, plano GND): la zona quedó en 3
polígonos disjuntos y el DRC nativo de KiCad reportó 2 pads sin conexión —
consistente con `pads_sin_conectar = fragmentos - 1`, que es el número de
uniones que faltan para reunir N regiones. Los 12 pads GND tenían spokes
térmicos correctos: el fallo NO era falta de cobre junto al pad (starvation),
era el plano partido.

POR QUÉ NO SE USA pcbnew
------------------------
El motor de relleno de zona vive dentro de pcbnew, que sólo existe del lado del
plugin de KiCad. El bucle de best-of-N y el criterio de selección viven en
Python puro (`escritor_pcb`), y la CLI corre headless. Un detector aproximado en
Python puro sirve en ambos lados.

PRECISIÓN NECESARIA: RANKING, NO VALOR ABSOLUTO
-----------------------------------------------
Este detector NO pretende reproducir el relleno de KiCad. Su uso es comparar
candidatos dentro de best-of-N, donde basta con que ordene bien: preferir el
candidato menos fragmentado. El DRC nativo sigue siendo el juez final.

MODELO
------
Se rasteriza la placa y se marca como cobre disponible toda celda que:
  1. cae dentro del contorno declarado de la zona, y
  2. está a >= `clearance` de cualquier pad o pista que NO sea de la red de la
     zona (los de la propia red son cobre del mismo potencial, no obstáculo).

Sobre ese conjunto se cuentan componentes conexas y se ve en cuántas caen los
pads de la red de la zona.

Dos decisiones de modelado que importan:

* **Erosión por `min_thickness/2`.** KiCad no rellena cobre más fino que
  `min_thickness`, así que un pasillo más angosto que eso no conduce aunque
  geométricamente exista. Se modela engordando los obstáculos esa mitad, que es
  la erosión morfológica equivalente; sin esto, pasillos de tamaño de celda
  aparecían como conexión válida y el plano se veía entero cuando no lo estaba.

* **Los pads de la red de la zona no son obstáculo y no se les modela el alivio
  térmico.** El alivio deja un hueco alrededor del pad que sólo cruzan los
  spokes, y modelarlo exigiría reproducir la generación de spokes de KiCad. Se
  omite deliberadamente porque la evidencia dice que los spokes no son el modo
  de falla: en la corrida analizada los 12 pads GND tenían cobre encima. Tratar
  el pad como unido al cobre que lo rodea captura el caso real y evita falsos
  positivos por spokes mal modelados.

Conectividad de 4 vecinos, no de 8: en 8 el cobre "pasa" por pellizcos
diagonales de un solo punto de contacto, que no conducen.
"""

import math
from typing import Dict, List, Optional, Sequence, Tuple

from lector_pcb import DatosPCB, PadPCB, SegmentoPCB, ZonaFCu

# Resolución del raster en mm. 0.15mm es el compromiso medido entre fidelidad y
# costo: reproduce el agrupamiento real de la placa de referencia y tarda ~1s.
# Bajarlo a 0.25 hace que pasillos finos se vean cerrados (sobreestima
# fragmentos); subir la resolución más allá no cambió el resultado y cuadruplica
# el tiempo.
PASO_RASTER_MM = 0.15


def _scanline_poligono(contorno: Sequence[Tuple[float, float]],
                       x0: float, y0: float, paso: float,
                       nc: int, nf: int, libre: bytearray) -> None:
    """Marca como libres las celdas interiores al polígono (relleno por filas)."""
    n = len(contorno)
    if n < 3:
        libre[:] = bytearray([1]) * (nc * nf)   # sin contorno: toda la placa
        return
    for fila in range(nf):
        y = y0 + (fila + 0.5) * paso
        cortes = []
        for k in range(n):
            ax, ay = contorno[k]
            bx, by = contorno[(k + 1) % n]
            if (ay > y) != (by > y):
                cortes.append((bx - ax) * (y - ay) / (by - ay) + ax)
        cortes.sort()
        base = fila * nc
        for i in range(0, len(cortes) - 1, 2):
            ci = max(0, int(math.ceil((cortes[i] - x0) / paso - 0.5)))
            cf = min(nc - 1, int(math.floor((cortes[i + 1] - x0) / paso - 0.5)))
            for col in range(ci, cf + 1):
                libre[base + col] = 1


def _bloquear_capsula(libre: bytearray, x0: float, y0: float, paso: float,
                      nc: int, nf: int,
                      ax: float, ay: float, bx: float, by: float,
                      radio: float) -> None:
    """Marca como ocupada toda celda a <= `radio` del segmento AB (cápsula)."""
    ci = max(0, int((min(ax, bx) - radio - x0) / paso))
    cf = min(nc - 1, int((max(ax, bx) + radio - x0) / paso) + 1)
    fi = max(0, int((min(ay, by) - radio - y0) / paso))
    ff = min(nf - 1, int((max(ay, by) + radio - y0) / paso) + 1)
    dx, dy = bx - ax, by - ay
    largo2 = dx * dx + dy * dy
    r2 = radio * radio
    for fila in range(fi, ff + 1):
        py = y0 + (fila + 0.5) * paso
        base = fila * nc
        for col in range(ci, cf + 1):
            if not libre[base + col]:
                continue
            px = x0 + (col + 0.5) * paso
            if largo2 <= 1e-12:
                t = 0.0
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / largo2
                t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
            qx, qy = ax + t * dx, ay + t * dy
            if (px - qx) ** 2 + (py - qy) ** 2 <= r2:
                libre[base + col] = 0


def _bloquear_rect(libre: bytearray, x0: float, y0: float, paso: float,
                   nc: int, nf: int,
                   cx: float, cy: float, w: float, h: float,
                   radio: float) -> None:
    """Marca como ocupada toda celda a <= `radio` del rectángulo (cx,cy,w,h)."""
    hx, hy = w / 2.0, h / 2.0
    ci = max(0, int((cx - hx - radio - x0) / paso))
    cf = min(nc - 1, int((cx + hx + radio - x0) / paso) + 1)
    fi = max(0, int((cy - hy - radio - y0) / paso))
    ff = min(nf - 1, int((cy + hy + radio - y0) / paso) + 1)
    r2 = radio * radio
    for fila in range(fi, ff + 1):
        py = y0 + (fila + 0.5) * paso
        base = fila * nc
        dy = abs(py - cy) - hy
        if dy < 0.0:
            dy = 0.0
        for col in range(ci, cf + 1):
            if not libre[base + col]:
                continue
            px = x0 + (col + 0.5) * paso
            dx = abs(px - cx) - hx
            if dx < 0.0:
                dx = 0.0
            if dx * dx + dy * dy <= r2:
                libre[base + col] = 0


def analizar_fragmentacion_zona(
        datos_pcb: DatosPCB,
        segmentos_nuevos: Sequence[dict],
        ancho_pista: float = 0.3,
        paso: float = PASO_RASTER_MM) -> dict:
    """
    Estima en cuántas piezas eléctricamente separadas queda cada zona de cobre.

    Args:
        datos_pcb: PCB leído (aporta pads, cobre preexistente y las zonas).
        segmentos_nuevos: segmentos generados por el enrutador, en el formato
            dict {'x1','y1','x2','y2','red', 'ancho'?}.
        ancho_pista: ancho por defecto para segmentos sin clave 'ancho'.
        paso: resolución del raster en mm.

    Returns:
        dict con:
          'fragmentos'          int  — suma sobre las zonas de (piezas con pads)
          'pads_aislados'       int  — pads que quedan fuera de la pieza mayor
          'detalle'             list — por zona: red, piezas y grupos de pads
        Con `fragmentos <= 1` por zona el plano está entero. `pads_aislados` es
        la estimación directa de lo que el DRC nativo reportaría como pads sin
        conexión a la zona.
    """
    resultado = {'fragmentos': 0, 'pads_aislados': 0, 'detalle': []}
    if not datos_pcb.zonas_fcu:
        return resultado

    x0, y0, x1, y1 = datos_pcb.limite_tablero
    # Margen para que el contorno de la zona entre completo en la grilla.
    x0 -= 1.0; y0 -= 1.0; x1 += 1.0; y1 += 1.0
    nc = int((x1 - x0) / paso) + 1
    nf = int((y1 - y0) / paso) + 1

    for zona in datos_pcb.zonas_fcu:
        red_zona = zona.nombre_red
        # Erosión: cobre más fino que min_thickness no se rellena.
        margen = zona.clearance + zona.min_thickness / 2.0

        libre = bytearray(nc * nf)
        _scanline_poligono(zona.contorno, x0, y0, paso, nc, nf, libre)

        for p in datos_pcb.todos_los_pads:
            if p.nombre_red == red_zona:
                continue
            _bloquear_rect(libre, x0, y0, paso, nc, nf,
                           p.x, p.y, p.ancho, p.alto, margen)

        for s in datos_pcb.segmentos_existentes:
            if s.nombre_red == red_zona:
                continue
            _bloquear_capsula(libre, x0, y0, paso, nc, nf,
                              s.x_inicio, s.y_inicio, s.x_fin, s.y_fin,
                              s.ancho / 2.0 + margen)

        for s in segmentos_nuevos:
            if s.get('red') == red_zona:
                continue
            _bloquear_capsula(libre, x0, y0, paso, nc, nf,
                              s['x1'], s['y1'], s['x2'], s['y2'],
                              s.get('ancho', ancho_pista) / 2.0 + margen)

        comp = _componentes_conexas(libre, nc, nf)
        grupos = _agrupar_pads_por_componente(
            datos_pcb.todos_los_pads, red_zona, comp, x0, y0, paso, nc, nf)

        piezas = len(grupos)
        aislados = 0
        if piezas > 1:
            mayor = max(grupos.values(), key=len)
            aislados = sum(len(v) for v in grupos.values()) - len(mayor)

        resultado['fragmentos'] += max(0, piezas - 1)
        resultado['pads_aislados'] += aislados
        resultado['detalle'].append({
            'red': red_zona,
            'piezas': piezas,
            'grupos': [sorted(v) for v in grupos.values()],
        })

    return resultado


def _componentes_conexas(libre: bytearray, nc: int, nf: int) -> List[int]:
    """Etiqueta componentes conexas (4 vecinos) de las celdas libres."""
    comp = [-1] * (nc * nf)
    etiqueta = 0
    for inicio in range(nc * nf):
        if not libre[inicio] or comp[inicio] >= 0:
            continue
        comp[inicio] = etiqueta
        pila = [inicio]
        while pila:
            u = pila.pop()
            uc = u % nc
            if uc > 0 and libre[u - 1] and comp[u - 1] < 0:
                comp[u - 1] = etiqueta; pila.append(u - 1)
            if uc < nc - 1 and libre[u + 1] and comp[u + 1] < 0:
                comp[u + 1] = etiqueta; pila.append(u + 1)
            if u >= nc and libre[u - nc] and comp[u - nc] < 0:
                comp[u - nc] = etiqueta; pila.append(u - nc)
            if u + nc < nc * nf and libre[u + nc] and comp[u + nc] < 0:
                comp[u + nc] = etiqueta; pila.append(u + nc)
        etiqueta += 1
    return comp


def _agrupar_pads_por_componente(pads: List[PadPCB], red_zona: str,
                                 comp: List[int],
                                 x0: float, y0: float, paso: float,
                                 nc: int, nf: int) -> Dict[int, List[str]]:
    """
    Asigna cada pad de `red_zona` a la componente de cobre que lo toca.

    Un pad puede rozar varias componentes por efecto del raster; se le asigna
    aquella con la que más solapa, que es la que realmente lo alimenta.
    """
    grupos: Dict[int, List[str]] = {}
    for p in pads:
        if p.nombre_red != red_zona:
            continue
        ci = max(0, int((p.x - p.ancho / 2 - x0) / paso))
        cf = min(nc - 1, int((p.x + p.ancho / 2 - x0) / paso))
        fi = max(0, int((p.y - p.alto / 2 - y0) / paso))
        ff = min(nf - 1, int((p.y + p.alto / 2 - y0) / paso))
        conteo: Dict[int, int] = {}
        for fila in range(fi, ff + 1):
            base = fila * nc
            for col in range(ci, cf + 1):
                k = comp[base + col]
                if k >= 0:
                    conteo[k] = conteo.get(k, 0) + 1
        nombre = f"{p.referencia}.{p.numero_pad}"
        if conteo:
            grupos.setdefault(max(conteo, key=conteo.get), []).append(nombre)
        else:
            # Pad sin cobre alcanzable: su propia "isla" de tamaño cero. Cuenta
            # como pieza aparte porque el DRC lo verá igual de desconectado.
            grupos.setdefault(-1 - len(grupos), []).append(nombre)
    return grupos


if __name__ == '__main__':
    import sys
    from lector_pcb import leer_pcb

    if len(sys.argv) < 2:
        print("Uso: python analisis_zona.py <archivo.kicad_pcb>")
        sys.exit(1)

    datos = leer_pcb(sys.argv[1])
    r = analizar_fragmentacion_zona(datos, [])
    print(f"\nFragmentos (piezas extra): {r['fragmentos']}")
    print(f"Pads aislados estimados:   {r['pads_aislados']}")
    for d in r['detalle']:
        print(f"\n  Zona '{d['red']}': {d['piezas']} pieza(s)")
        for g in d['grupos']:
            print(f"    - {g}")
