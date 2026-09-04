"""
escritor_pcb.py — Orquestador principal del asistente de enrutamiento PCB

Este módulo coordina el flujo completo:
  1. Lectura del archivo .kicad_pcb
  2. Extracción de conexiones a enrutar (ratsnest)
  3. Optimización del orden con Algoritmo Genético
  4. Enrutamiento con A*
  5. DRC básico (Design Rule Check)
  6. Escritura del nuevo archivo .kicad_pcb
  7. Reporte de métricas para la tesis

Formato de salida por defecto: nombreoriginal_enrutado.kicad_pcb
(sobrescribe la corrida anterior; usar --con-timestamp para conservar cada
corrida como un archivo distinto: nombreoriginal_enrutado_YYYYMMDD_HHMMSS.kicad_pcb)

Uso:
    python escritor_pcb.py mi_pcb.kicad_pcb [--ancho 0.3] [--clearance 0.2]
                           [--poblacion 50] [--generaciones 100]
                           [--sin-ag] [--sin-drc] [--con-timestamp]
"""

import sys

# Forzar UTF-8 en la salida estándar (necesario en Windows con consola cp1252)
# Debe hacerse antes de cualquier print() o import de módulos con prints
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

import os
import re
import uuid
import math
import time
import random
import argparse
from datetime import datetime
from typing import List, Tuple, Dict, Optional

# Módulos del proyecto
from lector_pcb import (
    leer_pcb, obtener_conexiones_a_enrutar,
    DatosPCB, PadPCB, SegmentoPCB
)
from enrutador_astar import enrutar_todos, UMBRAL_DESVIO_PATOLOGICO
from optimizador_genetico import optimizar_orden_enrutamiento
from analisis_zona import analizar_fragmentacion_zona


# ─────────────────────────────────────────────────────────────────────────────
# Generador de segmentos KiCad 10
# ─────────────────────────────────────────────────────────────────────────────

def generar_segmento_kicad10(x1: float, y1: float,
                              x2: float, y2: float,
                              ancho: float, nombre_red: str) -> str:
    """
    Genera la S-expresión de un segmento en formato KiCad 10.

    Reglas críticas de compatibilidad con KiCad 10:
      - La red se escribe como (net "NOMBRE") — sin ID numérico
      - La capa va entre comillas: (layer "F.Cu")
      - Cada segmento necesita un UUID único
      - No se usan punto y coma como comentarios dentro del archivo

    Args:
        x1, y1: Coordenada de inicio en mm
        x2, y2: Coordenada de fin en mm
        ancho: Ancho de la pista en mm
        nombre_red: Nombre de la red (ej: "GND", "VCC")

    Returns:
        Cadena de texto con la S-expresión del segmento
    """
    uid = str(uuid.uuid4())
    return (
        f'\t(segment\n'
        f'\t\t(start {x1:.6f} {y1:.6f})\n'
        f'\t\t(end {x2:.6f} {y2:.6f})\n'
        f'\t\t(width {ancho})\n'
        f'\t\t(layer "F.Cu")\n'
        f'\t\t(net "{nombre_red}")\n'
        f'\t\t(uuid "{uid}")\n'
        f'\t)'
    )


# ─────────────────────────────────────────────────────────────────────────────
# DRC básico
# ─────────────────────────────────────────────────────────────────────────────

def drc_basico(segmentos_nuevos: List[dict],
               pads: List[PadPCB],
               ancho_pista: float = 0.3,
               clearance: float = 0.2) -> List[str]:
    """
    Realiza una verificación básica de reglas de diseño (DRC).

    Comprueba:
      1. Ancho mínimo de pista (>= 0.1mm)
      2. Longitud mínima de segmento (> 0.001mm)
      3. Cruces entre segmentos de redes diferentes

    Args:
        segmentos_nuevos: Lista de segmentos enrutados
        pads: Lista de pads del PCB
        ancho_pista: Ancho de pista configurado
        clearance: Clearance mínimo

    Returns:
        Lista de mensajes de error/advertencia DRC
    """
    errores = []

    # Verificar ancho mínimo
    ANCHO_MINIMO = 0.1
    if ancho_pista < ANCHO_MINIMO:
        errores.append(f"ADVERTENCIA DRC: Ancho de pista {ancho_pista}mm < mínimo {ANCHO_MINIMO}mm")

    # Verificar cada segmento
    for i, seg in enumerate(segmentos_nuevos):
        longitud = math.hypot(seg['x2'] - seg['x1'], seg['y2'] - seg['y1'])
        if longitud < 0.001:
            errores.append(f"ADVERTENCIA DRC: Segmento #{i} de red '{seg['red']}' "
                           f"tiene longitud casi cero ({longitud:.6f}mm)")

    # Verificar cruces entre redes diferentes
    n = len(segmentos_nuevos)
    cruces_encontrados = 0
    for i in range(n):
        s1 = segmentos_nuevos[i]
        for j in range(i + 1, n):
            s2 = segmentos_nuevos[j]
            if s1['red'] == s2['red']:
                continue
            if _segmentos_se_cruzan_drc(
                s1['x1'], s1['y1'], s1['x2'], s1['y2'],
                s2['x1'], s2['y1'], s2['x2'], s2['y2']
            ):
                cruces_encontrados += 1
                if cruces_encontrados <= 5:  # Limitar mensajes
                    errores.append(
                        f"ERROR DRC: Cruce entre red '{s1['red']}' y red '{s2['red']}' "
                        f"en zona ({s1['x1']:.2f},{s1['y1']:.2f})"
                    )

    if cruces_encontrados > 5:
        errores.append(f"... y {cruces_encontrados - 5} cruces adicionales")

    return errores


def _segmentos_se_cruzan_drc(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2) -> bool:
    """Verifica cruce de segmentos para DRC (mismo código que en optimizador)."""
    def orientacion(px, py, qx, qy, rx, ry) -> int:
        val = (qy - py) * (rx - qx) - (qx - px) * (ry - qy)
        if abs(val) < 1e-9:
            return 0
        return 1 if val > 0 else 2

    o1 = orientacion(ax1, ay1, ax2, ay2, bx1, by1)
    o2 = orientacion(ax1, ay1, ax2, ay2, bx2, by2)
    o3 = orientacion(bx1, by1, bx2, by2, ax1, ay1)
    o4 = orientacion(bx1, by1, bx2, by2, ax2, ay2)

    return o1 != o2 and o3 != o4


# ─────────────────────────────────────────────────────────────────────────────
# Modificación del contenido del PCB
# ─────────────────────────────────────────────────────────────────────────────

def eliminar_segmentos_existentes_fcu(contenido: str) -> str:
    """
    Elimina todos los segmentos de F.Cu del contenido del archivo PCB.

    Esto permite enrutar desde cero sin interferencia de rutas anteriores.
    Los segmentos de otras capas (B.Cu, etc.) se mantienen intactos.

    Args:
        contenido: Texto completo del archivo .kicad_pcb

    Returns:
        Contenido modificado sin segmentos en F.Cu
    """
    resultado = []
    i = 0
    eliminados = 0

    while i < len(contenido):
        # Buscar el inicio de un bloque (segment
        if contenido[i:i+9] == '(segment\n' or contenido[i:i+9] == '(segment\t' or \
           contenido[i:i+10] == '\t(segment\n':
            # Encontrar el inicio real del bloque
            inicio_bloque = i

            # Encontrar el cierre del bloque
            profundidad = 0
            j = i
            while j < len(contenido):
                if contenido[j] == '"':
                    j += 1
                    while j < len(contenido) and contenido[j] != '"':
                        j += 1
                elif contenido[j] == '(':
                    profundidad += 1
                elif contenido[j] == ')':
                    profundidad -= 1
                    if profundidad == 0:
                        break
                j += 1

            bloque = contenido[i:j + 1]

            # Solo eliminar si está en F.Cu
            if '"F.Cu"' in bloque:
                # Saltar también el newline después del bloque
                fin = j + 1
                if fin < len(contenido) and contenido[fin] == '\n':
                    fin += 1
                i = fin
                eliminados += 1
                continue

        resultado.append(contenido[i])
        i += 1

    print(f"[Escritor] Segmentos F.Cu eliminados: {eliminados}")
    return ''.join(resultado)


def insertar_segmentos_nuevos(contenido: str, segmentos_texto: str) -> str:
    """
    Inserta los nuevos segmentos antes del cierre del archivo PCB.

    En KiCad 10, el archivo termina con:
      (embedded_fonts no)
    )

    Los segmentos se insertan antes del paréntesis final.

    Args:
        contenido: Contenido del archivo PCB (sin segmentos F.Cu viejos)
        segmentos_texto: Texto de los nuevos segmentos concatenados

    Returns:
        Contenido con los nuevos segmentos insertados
    """
    # Buscar el cierre final: "(embedded_fonts no)\n)"
    # o simplemente el último ')'
    patron_embedded = re.search(r'\(embedded_fonts\s+no\)\s*\n\)', contenido)
    if patron_embedded:
        pos_insercion = patron_embedded.end() - 1
    else:
        # Fallback: insertar antes del último ')'
        pos_insercion = contenido.rfind(')')

    if pos_insercion < 0:
        return contenido + '\n' + segmentos_texto

    return (contenido[:pos_insercion] +
            '\n' + segmentos_texto +
            '\n' + contenido[pos_insercion:])


# ─────────────────────────────────────────────────────────────────────────────
# Escritura del archivo de salida
# ─────────────────────────────────────────────────────────────────────────────

# Sufijo "_enrutado" con timestamp opcional al final: "_enrutado" o
# "_enrutado_20260510_143022". Se usa tanto para detectar si un archivo de
# ENTRADA ya es un archivo generado, como para construir el nombre de salida.
_PATRON_SUFIJO_ENRUTADO = re.compile(r'_enrutado(_\d{8}_\d{6})?$')


def _construir_ruta_salida(ruta_entrada: str, con_timestamp: bool) -> str:
    """
    Construye la ruta de salida a partir de la ruta de entrada.

    Reglas:
      - Si la entrada ya termina en "_enrutado" o "_enrutado_YYYYMMDD_HHMMSS"
        (es decir, ya es un archivo generado por el asistente), el sufijo NO
        se encadena de nuevo: se usa la misma base normalizada. Esto evita
        "mi_pcb_enrutado_enrutado.kicad_pcb" y hace que, por defecto, el
        asistente sobrescriba su propio archivo de salida en vez de crear uno
        nuevo cada vez que se re-enruta.
      - Un archivo de entrada con timestamp viejo (de una corrida anterior con
        --con-timestamp) se normaliza al nombre SIN timestamp al usarse como
        entrada de una nueva corrida sin --con-timestamp. El archivo viejo con
        fecha no se toca ni se borra — solo no se usa su timestamp.
      - Si `con_timestamp` es True, se agrega un timestamp nuevo al final
        (comportamiento anterior, útil para conservar corridas comparables).

    Args:
        ruta_entrada: Ruta completa del archivo .kicad_pcb de entrada
        con_timestamp: Si True, agrega sufijo de fecha/hora único por corrida

    Returns:
        Ruta de salida calculada
    """
    raiz, extension = os.path.splitext(ruta_entrada)

    coincidencia = _PATRON_SUFIJO_ENRUTADO.search(raiz)
    raiz_base = raiz[:coincidencia.start()] if coincidencia else raiz

    if con_timestamp:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{raiz_base}_enrutado_{timestamp}{extension}"

    return f"{raiz_base}_enrutado{extension}"


def escribir_pcb_enrutado(datos_pcb: DatosPCB,
                           segmentos_nuevos: List[dict],
                           ancho_pista: float = 0.3,
                           ruta_salida: Optional[str] = None,
                           con_timestamp: bool = False) -> str:
    """
    Escribe el archivo PCB enrutado compatible con KiCad 10.

    Proceso:
      1. Parte del contenido original (preserva footprints, Edge.Cuts, etc.)
      2. Preserva los segmentos F.Cu existentes (enrutado manual del usuario)
      3. Inserta los nuevos segmentos generados por A*
      4. Escribe el archivo de salida

    Por defecto, el nombre de salida NO lleva timestamp y siempre es el mismo
    para un archivo de entrada dado (`<base>_enrutado.kicad_pcb`), de modo que
    corridas sucesivas con distintos parámetros sobrescriben el resultado
    anterior en lugar de acumular archivos. Use `con_timestamp=True` para
    conservar cada corrida como un archivo distinto.

    Args:
        datos_pcb: Datos del PCB leídos originalmente
        segmentos_nuevos: Lista de segmentos {'x1','y1','x2','y2','red'}
        ancho_pista: Ancho de pista en mm
        ruta_salida: Ruta de destino opcional (si None, se calcula automáticamente).
                     Si se especifica explícitamente, se usa tal cual, ignorando
                     `con_timestamp`.
        con_timestamp: Si True y `ruta_salida` es None, agrega un sufijo de
                       fecha/hora único al nombre generado.

    Returns:
        Ruta del archivo generado

    Raises:
        PermissionError: si el archivo de salida existe y está bloqueado
            (por ejemplo, abierto en KiCad). Se relanza con un mensaje claro.
    """
    # Generar nombre de archivo de salida
    if ruta_salida is None:
        ruta_salida = _construir_ruta_salida(datos_pcb.nombre_archivo, con_timestamp)

    print(f"\n[Escritor] Generando archivo: {ruta_salida}")
    print(f"[Escritor] Segmentos a escribir: {len(segmentos_nuevos)}")
    if datos_pcb.segmentos_existentes:
        print(f"[Escritor] Preservando {len(datos_pcb.segmentos_existentes)} "
              f"segmentos F.Cu existentes (enrutado manual del usuario)")

    # Partir del contenido original — preserva los segmentos F.Cu existentes
    # del usuario. El asistente AÑADE rutas nuevas sin borrar el trabajo manual.
    contenido = datos_pcb.contenido_original

    # Generar texto de nuevos segmentos (cada uno con su ancho: las redes
    # con ancho propio via anchos_por_red llevan la clave 'ancho' en el dict)
    lineas_segmentos = []
    for seg in segmentos_nuevos:
        linea = generar_segmento_kicad10(
            seg['x1'], seg['y1'],
            seg['x2'], seg['y2'],
            seg.get('ancho', ancho_pista), seg['red']
        )
        lineas_segmentos.append(linea)

    segmentos_texto = '\n'.join(lineas_segmentos)

    # Insertar nuevos segmentos
    contenido = insertar_segmentos_nuevos(contenido, segmentos_texto)

    # Escribir archivo
    try:
        with open(ruta_salida, 'w', encoding='utf-8') as f:
            f.write(contenido)
    except PermissionError as e:
        raise PermissionError(
            f"No se pudo escribir '{ruta_salida}'. El archivo puede estar "
            f"abierto en KiCad u otro programa. Ciérrelo e intente de nuevo."
        ) from e

    print(f"[Escritor] Archivo escrito exitosamente: {ruta_salida}")
    return ruta_salida


# ─────────────────────────────────────────────────────────────────────────────
# Reporte de métricas
# ─────────────────────────────────────────────────────────────────────────────

def generar_reporte(metricas_astar: dict,
                    estadisticas_ag: dict,
                    errores_drc: List[str],
                    ruta_salida: str,
                    tiempo_total: float,
                    ancho_pista: float,
                    clearance: float,
                    modo_orden: str = 'mps',
                    advertencias_clearance: Optional[List[dict]] = None,
                    paso_cuadricula: Optional[float] = None) -> str:
    """
    Genera un reporte completo de métricas para la tesis.

    Incluye:
      - Estadísticas del Algoritmo Genético
      - Resultados del enrutamiento A*
      - Errores DRC
      - Métricas globales

    Args:
        metricas_astar: Dict con resultados del enrutador
        estadisticas_ag: Dict con estadísticas del AG
        errores_drc: Lista de mensajes DRC
        ruta_salida: Ruta del archivo generado
        tiempo_total: Tiempo total del proceso en segundos
        ancho_pista: Ancho de pista usado
        clearance: Clearance usado

    Returns:
        Texto del reporte formateado
    """
    sep = "=" * 60
    lineas = [
        sep,
        "  REPORTE DE ENRUTAMIENTO — ASISTENTE PCB TESIS",
        sep,
        f"  Archivo de salida: {os.path.basename(ruta_salida)}",
        f"  Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "─── PARÁMETROS ───────────────────────────────────────────",
        f"  Ancho de pista:    {ancho_pista} mm",
        f"  Clearance nominal: {clearance} mm",
        f"  Piso fabricable:   {metricas_astar.get('clearance_minimo', 0.127)} mm",
        # El paso de cuadrícula cambia la geometría que produce A* y no estaba
        # registrado: dos reportes con la misma semilla y todo lo demás igual
        # podían mostrar longitudes distintas sin nada que explicara por qué,
        # y la corrida no era reproducible desde su propio reporte.
        f"  Paso cuadrícula:   {paso_cuadricula} mm" if paso_cuadricula is not None
        else "  Paso cuadrícula:   (no informado)",
        f"  Modo de orden:     {modo_orden}",
        "",
        "─── ALGORITMO GENÉTICO ───────────────────────────────────",
    ]

    if estadisticas_ag.get('generaciones', 0) > 0:
        lineas += [
            f"  Generaciones:      {estadisticas_ag['generaciones']}",
            f"  Tamaño población:  {estadisticas_ag['tamano_poblacion']}",
            f"  Fitness:           {estadisticas_ag.get('fitness', 'surrogate')}",
            f"  Semilla:           {estadisticas_ag.get('semilla', '—')}"
            + _ETIQUETA_ORIGEN_SEMILLA.get(
                estadisticas_ag.get('origen_semilla'),
                "  (origen no informado)"),
            f"  Costo del mejor:   {estadisticas_ag['mejor_costo']:.3f}",
            f"  Mejora vs aleatorio: {estadisticas_ag.get('mejora_vs_aleatorio', 0.0)}%"
            f"  (costo ref: {estadisticas_ag.get('costo_aleatorio_promedio', '—')})",
            f"  Mejora vs distancia: {estadisticas_ag.get('mejora_vs_distancia', 0.0)}%"
            f"  (costo ref: {estadisticas_ag.get('costo_distancia', '—')})",
            f"  Mejora vs MPS:       {estadisticas_ag.get('mejora_vs_mps', 0.0)}%"
            f"  (costo ref: {estadisticas_ag.get('costo_mps', '—')})",
            f"  Tiempo AG:         {estadisticas_ag['tiempo_segundos']}s",
        ]
        if estadisticas_ag.get('mejora_vs_mps', 0.0) == 0.0 \
                and estadisticas_ag.get('mejora_vs_aleatorio', 0.0) > 0.0:
            lineas.append("  Nota: mejora vs MPS = 0% con mejora vs aleatorio > 0% "
                          "indica que la semilla MPS ya era óptima (AG operativo).")
    else:
        lineas.append(f"  AG no aplicado (modo '{modo_orden}' o pocas conexiones)")

    lineas += [
        "",
        "─── ENRUTAMIENTO A* ──────────────────────────────────────",
        f"  Total conexiones:  {metricas_astar['total']}",
        f"  Exitosas:          {metricas_astar['exitosas']}",
        f"  Fallidas:          {metricas_astar['fallidas']}",
    ]

    if metricas_astar.get('omitidas'):
        lineas.append(f"  Omitidas:          {metricas_astar['omitidas']} "
                      f"(extremos ya conectados por cobre de la misma red)")

    conect = metricas_astar.get('conectividad')
    # Las omitidas no cuentan como éxito ni como fallo: el denominador de la
    # tasa son las conexiones que realmente había que enrutar, para que la
    # comparación entre corridas siga siendo válida.
    requeridas = max(1, metricas_astar['total'] - metricas_astar.get('omitidas', 0))
    lineas += [
        f"  Longitud total:    {metricas_astar['longitud_total_mm']} mm",
        f"  Tasa de éxito:     {metricas_astar['exitosas']/requeridas*100:.1f}%"
        f"  ({metricas_astar['exitosas']}/{requeridas} conexiones que requerían ruta)",
    ]
    if conect:
        marca = "" if conect['completas'] == conect['total'] else "  ✗"
        lineas.append(f"  Conectividad:      {conect['completas']}/{conect['total']} "
                      f"redes completas{marca}")
        for nombre, n in conect['incompletas']:
            lineas.append(f"                     ✗ {nombre}: quedó en {n} grupos aislados")

    lineas += [
        "",
        "─── DETALLE POR CONEXIÓN ─────────────────────────────────",
    ]

    for det in metricas_astar.get('detalle', []):
        if det.get('omitida'):
            lineas.append(
                f"  — [{det['red']:20s}] "
                f"{det['origen']:12s} → {det['destino']:12s}"
                f"  OMITIDA (ya conectada por cobre de la red)"
            )
            continue
        estado = "✓" if det['exito'] else "✗"
        if det['exito']:
            desvio = det.get('desvio')
            marca = (" ⚠" if (desvio is not None
                              and desvio >= UMBRAL_DESVIO_PATOLOGICO) else "")
            cola = (f"  {det['longitud_mm']:7.2f}mm  ({det['num_segmentos']} segs)"
                    f"  directa {det.get('longitud_directa_mm', 0):6.2f}mm"
                    f"  desvío {desvio:.2f}x{marca}" if desvio is not None
                    else f"  {det['longitud_mm']:.2f}mm  ({det['num_segmentos']} segs)")
        else:
            cola = f"  FALLIDO  (directa {det.get('longitud_directa_mm', 0):.2f}mm)"
        lineas.append(
            f"  {estado} [{det['red']:20s}] "
            f"{det['origen']:12s} → {det['destino']:12s}" + cola
        )

    # ── Integridad del plano de cobre ────────────────────────────────────────
    zona = metricas_astar.get('zona')
    if zona and zona.get('detalle'):
        lineas += ["", "─── INTEGRIDAD DEL PLANO DE COBRE ────────────────────────"]
        for z in zona['detalle']:
            if z['piezas'] <= 1:
                lineas.append(f"  ✓ Zona '{z['red']}': plano entero "
                              f"(1 pieza, todos los pads unidos)")
                continue
            lineas.append(f"  ⚠ Zona '{z['red']}': plano PARTIDO en "
                          f"{z['piezas']} piezas por las pistas de otras redes")
            for g in sorted(z['grupos'], key=len, reverse=True):
                lineas.append(f"      pieza: {', '.join(g)}")
        if zona['fragmentos']:
            lineas.append(f"  Pads sin conexión al plano que esto implica: "
                          f"{zona['fragmentos']}")
            lineas.append("  (estimación geométrica; el juez final es el DRC "
                          "nativo tras rellenar zonas con B en KiCad)")

    # ── Repeticiones del AG ──────────────────────────────────────────────────
    rep = metricas_astar.get('repeticiones')
    if rep:
        lineas += ["", "─── SELECCIÓN BEST-OF-N ──────────────────────────────────",
                   f"  Intentos ejecutados: {rep['n']}",
                   f"  {'intento':>7}  {'semilla':<12} {'fallidas':>9}  {'redes':>7}"
                   f"  {'frag.plano':>10}  {'clear.red':>9}  {'patol.':>6}"
                   f"  {'longitud':>10}"]
        for r in rep['resumen']:
            marca = "  ← ELEGIDO" if r['elegida'] else ""
            lineas.append(
                f"  {r['intento']:>7}  {r['semilla']:<12} {r['fallidas']:>9}  "
                f"{r['redes_completas']:>3}/{r['redes_total']:<3}  "
                f"{r.get('fragmentos_zona', 0):>10}  "
                f"{r.get('clearance_reducido', 0):>9}  "
                f"{r.get('patologicas', 0):>6}  "
                f"{r['longitud']:>8.2f}mm{marca}")
        lineas.append("  Criterio: fallidas → redes incompletas → fragmentos del "
                      "plano → clearance reducido → patológicas → longitud")
        lineas.append(f"  Tiempo total de los intentos: {rep.get('tiempo_total', 0)}s")
        lineas.append(f"  Reproducir el elegido: --semilla {rep['elegida']}")
        if rep['ninguna_completa']:
            lineas.append("  ⚠ Ningún intento logró conectividad completa; "
                          "se devuelve el de más redes conectadas.")

    # ── Rip-up and reroute ───────────────────────────────────────────────────
    stats_rip = metricas_astar.get('ripup_stats')
    if stats_rip or metricas_astar.get('ripups'):
        lineas += ["", "─── RIP-UP AND REROUTE ───────────────────────────────────"]

        if stats_rip:
            # Los dos disparadores se contabilizan por separado: con dos
            # mecanismos activos hay que poder saber cuál actuó.
            lineas.append(f"  {'disparador':>12}  {'triggers':>8}  {'aceptados':>9}"
                          f"  {'revertidos':>10}  {'descartados':>11}")
            for motivo, etiqueta in (('fallo', 'por FALLO'),
                                     ('patologica', 'por PATOLÓG.')):
                s = stats_rip.get(motivo)
                if not s:
                    continue
                descartados = (s.get('sin_candidatas', 0)
                               + s.get('sin_ganancia', 0))
                lineas.append(
                    f"  {etiqueta:>12}  {s['triggers']:>8}  {s['aceptados']:>9}"
                    f"  {s['revertidos']:>10}  {descartados:>11}")
            lineas.append(f"  Intentos totales (tope {2 * metricas_astar['total']}): "
                          f"{metricas_astar.get('ripup_intentos', 0)}")

        for r in metricas_astar.get('ripups', []):
            motivo = r.get('motivo', 'fallo')
            verbo = 'rescatada' if motivo == 'fallo' else 'acortada'
            lineas.append(
                f"  ↻ [{motivo}] {r.get('conexion', r.get('rescatada', '?'))} "
                f"{verbo} rehaciendo la red {r.get('red_victima', '?')} "
                f"({r.get('conexiones_rehechas', '?')} conexión/es, "
                f"bloqueante {r['victima']})")
            lineas.append(
                f"     redes completas {r.get('redes_antes', '?')}→"
                f"{r.get('redes_despues', '?')}, "
                f"longitud total {r.get('delta_longitud', 0):+.2f}mm")

    # ── Desvío: ruta real vs línea directa ───────────────────────────────────
    if metricas_astar.get('desvio_promedio'):
        lineas += [
            "",
            "─── DESVÍO (longitud enrutada / distancia directa) ───────",
            f"  Promedio:          {metricas_astar['desvio_promedio']:.2f}x",
            f"  Máximo:            {metricas_astar['desvio_maximo']:.2f}x "
            f"({metricas_astar.get('desvio_peor_conexion', '—')})",
            f"  Rutas patológicas: {metricas_astar.get('rutas_patologicas', 0)} "
            f"(desvío ≥ {UMBRAL_DESVIO_PATOLOGICO:.1f}x)",
        ]

    lineas += [
        "",
        "─── DRC (Design Rule Check) ──────────────────────────────",
    ]

    if errores_drc:
        for error in errores_drc:
            lineas.append(f"  {error}")
    else:
        lineas.append("  Sin errores DRC detectados (contra el clearance nominal)")

    # Advertencias: conexiones resueltas con clearance por debajo de la regla
    # nominal configurada. La placa queda conectada, pero NO cumple la regla.
    advertencias_clearance = advertencias_clearance or []
    if advertencias_clearance:
        lineas += ["", "─── ADVERTENCIAS DE CLEARANCE ────────────────────────────"]
        for det in advertencias_clearance:
            lineas.append(
                f"  ⚠ [{det['red']:20s}] {det['origen']} → {det['destino']}: "
                f"enrutada con clearance reducido {det['clearance_usado']:.3f}mm "
                f"(regla: {clearance:.3f}mm)"
            )
        lineas.append(f"  Total: {len(advertencias_clearance)} conexión(es) "
                      f"por debajo de la regla nominal")

    conect = metricas_astar.get('conectividad')
    frag = (metricas_astar.get('zona') or {}).get('fragmentos', 0)
    if conect and conect['incompletas']:
        # La conectividad manda sobre todo lo demás: si una red quedó abierta,
        # el resultado no sirve aunque A* haya "resuelto" todas las conexiones.
        estado = 'INCOMPLETO (hay redes eléctricamente abiertas)'
    elif frag:
        # Un plano partido deja pads de masa sueltos: es una red abierta igual
        # que las anteriores, sólo que invisible para `conectividad` porque las
        # redes con zona se excluyen del enrutado.
        estado = f'INCOMPLETO (plano de cobre partido; {frag} pad(s) sin masa)'
    elif metricas_astar['fallidas'] > 0:
        estado = 'PARCIAL (hay fallidas)'
    elif advertencias_clearance:
        estado = 'COMPLETADO CON ADVERTENCIAS'
    else:
        estado = 'COMPLETADO'

    lineas += [
        "",
        "─── RESUMEN GLOBAL ───────────────────────────────────────",
        f"  Tiempo total:      {tiempo_total:.2f}s",
        f"  Estado:            {estado}",
        sep,
    ]

    return '\n'.join(lineas)


# ─────────────────────────────────────────────────────────────────────────────
# Verificación de conectividad eléctrica
# ─────────────────────────────────────────────────────────────────────────────

def verificar_conectividad(datos_pcb: DatosPCB,
                           segmentos_nuevos: List[dict],
                           ancho_pista: float = 0.3) -> dict:
    """
    Verifica que cada red quede ELÉCTRICAMENTE completa tras el enrutamiento.

    Una conexión puede reportarse como "exitosa" (A* encontró un camino) y aun
    así dejar la red incompleta: por ejemplo si la ruta termina en un punto que
    no es el pad requerido. Contar sólo conexiones exitosas oculta ese caso.
    Esta verificación es la fuente de verdad: agrupa los pads de cada red con
    union-find sobre el cobre resultante (segmentos existentes + nuevos) y
    exige un único grupo por red.

    Se omiten las redes cubiertas por zona de cobre y las de menos de 2 pads.

    Returns:
        dict con 'completas', 'total', 'incompletas' (lista de (red, n_grupos))
    """
    from collections import defaultdict
    from lector_pcb import _agrupar_pads_por_conectividad

    # Unificar el cobre: segmentos preexistentes + los recién generados
    por_red: Dict[str, List[SegmentoPCB]] = defaultdict(list)
    for s in datos_pcb.segmentos_existentes:
        por_red[s.nombre_red].append(s)
    for s in segmentos_nuevos:
        por_red[s['red']].append(SegmentoPCB(
            x_inicio=s['x1'], y_inicio=s['y1'],
            x_fin=s['x2'], y_fin=s['y2'],
            ancho=s.get('ancho', ancho_pista),
            capa='F.Cu', nombre_red=s['red']
        ))

    completas, total, incompletas = 0, 0, []
    for nombre, red in datos_pcb.redes.items():
        if nombre in datos_pcb.redes_con_zona_fcu or len(red.pads) < 2:
            continue
        total += 1
        grupos = _agrupar_pads_por_conectividad(red.pads, por_red.get(nombre, []))
        if len(grupos) == 1:
            completas += 1
        else:
            incompletas.append((nombre, len(grupos)))

    return {'completas': completas, 'total': total, 'incompletas': incompletas}


# Etiquetas del origen de la semilla en el reporte. Importan porque estos
# reportes son evidencia experimental: decir "fijada por el usuario" sobre una
# semilla que generó el sistema contradice el diseño del experimento, que es
# justamente NO fijarla.
_ETIQUETA_ORIGEN_SEMILLA = {
    'usuario': "  (fijada por el usuario)",
    'sistema': "  (del sistema)",
    'sistema_bestofn': "  (del sistema, elegida por best-of-N)",
}


def _derivar_semillas(semilla_base: Optional[int], n: int) -> List[int]:
    """
    Genera n semillas para las repeticiones del AG.

    Con `semilla_base` definida, las n semillas son `semilla_base + i`. Si se
    reutilizara la misma semilla en los n intentos, el AG produciría n órdenes
    IDÉNTICOS y el best-of-N no compararía nada — se gastaría n veces el tiempo
    para elegir entre n copias. Derivar mantiene la corrida reproducible
    (mismo comando, mismas n semillas) sin desperdiciar los intentos.

    Sin semilla base, se toman de la entropía del sistema: intentos
    independientes, que es el escenario en el que el best-of-N rinde.

    Caso n=1: la semilla se usa TAL CUAL, sin derivar. Derivarla rompería el
    contrato de --semilla — la semilla que el reporte informa debe reproducir
    esa misma corrida al pasarla de vuelta, y una derivación la convertiría en
    otra distinta en cada intento.
    """
    if n == 1:
        return [semilla_base]

    if semilla_base is not None:
        return [semilla_base + i for i in range(n)]
    sistema = random.SystemRandom()
    return [sistema.randrange(2 ** 31) for _ in range(n)]


def _clave_calidad(metricas: dict) -> Tuple[int, int, int, int, int, float]:
    """
    Clave de comparación entre intentos — orden lexicográfico ESTRICTO:

        1. conexiones_fallidas
        2. redes_incompletas
        3. fragmentos_zona (piezas extra del plano de cobre)
        4. conexiones_con_clearance_reducido
        5. rutas_patologicas (desvío >= 3.0x)
        6. longitud_total_mm            ← sólo desempate

    POR QUÉ LEXICOGRÁFICO Y NO SUMA PONDERADA: cualquier ponderación admite
    que una placa rota gane por ser lo bastante más corta, y no existe un peso
    "correcto" que impida el intercambio en todos los casos — habría que
    recalibrarlo por placa. Con orden estricto el intercambio es imposible por
    construcción: la longitud sólo decide entre soluciones idénticas en todo
    lo que importa antes.

    Caso real que lo motiva: de 5 corridas, la de 641.79mm era la MÁS CORTA y
    lo era precisamente porque dejó una conexión sin enrutar (31/32) y la red
    VIN- eléctricamente abierta. Con cualquier suma ponderada razonable habría
    ganado; con este orden queda última en el primer criterio.

    Los criterios 4 y 5 son de fabricabilidad, no de estética: una pista con
    clearance por debajo de la regla nominal puede no ser fabricable, y una
    ruta patológica (>= 3.0x de desvío) rodea media placa acoplándose a todo
    lo que cruza. Ambos importan más que unos milímetros de longitud.

    El criterio 3 (fragmentos del plano) va junto a los de conectividad y no
    entre los de fabricabilidad porque describe lo mismo que ellos: una red
    eléctricamente abierta. `redes_incompletas` no puede verlo — las redes con
    zona se excluyen del enrutado y por lo tanto del conteo de conectividad —,
    así que sin este criterio best-of-N era CIEGO al modo de falla dominante y
    elegía por longitud entre candidatos con 0, 1 o 2 pads de masa sueltos.
    Medido: 5 corridas, 3 limpias y 2 con el plano partido, sin que ninguna
    métrica de selección distinguiera unas de otras.
    """
    c = metricas.get('conectividad') or {'total': 0, 'completas': 0}
    return (
        metricas.get('fallidas', 0),
        c['total'] - c['completas'],
        metricas.get('fragmentos_zona', 0),
        metricas.get('clearance_reducido', 0),
        metricas.get('rutas_patologicas', 0),
        metricas.get('longitud_total_mm', float('inf')),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Flujo principal
# ─────────────────────────────────────────────────────────────────────────────

def enrutar_pcb(ruta_entrada: str,
                ancho_pista: float = 0.3,
                clearance: float = 0.2,
                paso_cuadricula: float = 0.25,
                tamano_poblacion: int = 50,
                num_generaciones: int = 100,
                usar_ag: bool = True,
                ejecutar_drc: bool = True,
                ruta_salida: Optional[str] = None,
                con_timestamp: bool = False,
                escribir_archivo: bool = True,
                modo_orden: Optional[str] = None,
                fitness: str = 'surrogate',
                paso_surrogate: float = 0.5,
                anchos_por_red: Optional[Dict[str, float]] = None,
                clearance_minimo: float = 0.127,
                semilla_ag: Optional[int] = None,
                repeticiones: int = 1,
                callback_progreso=None) -> Tuple[Optional[str], str, list, bool]:
    """
    Ejecuta el flujo completo de enrutamiento.

    Orquesta: Lector → AG → A* → DRC → Escritor

    Args:
        ruta_entrada: Ruta al archivo .kicad_pcb sin enrutar
        ancho_pista: Ancho de pista en mm (por defecto 0.3mm)
        clearance: Clearance mínimo en mm (por defecto 0.2mm)
        paso_cuadricula: Resolución A* en mm (por defecto 0.25mm)
        tamano_poblacion: Tamaño de población del AG
        num_generaciones: Número de generaciones del AG
        usar_ag: Compatibilidad con la GUI: si modo_orden es None, usar_ag=True
            equivale a modo_orden='ag' y usar_ag=False a modo_orden='mps'.
        ejecutar_drc: Si True, realiza verificación DRC
        ruta_salida: Ruta de salida (None = genera automáticamente)
        con_timestamp: Si True y `ruta_salida` es None, cada corrida genera
            un archivo distinto con fecha/hora. Si False (por defecto),
            siempre sobrescribe `<base>_enrutado.kicad_pcb`.
        escribir_archivo: Si True (por defecto), escribe el resultado a disco.
            Si False, se salta el paso de escritura — usado por el plugin GUI
            cuando va a aplicar los segmentos directamente sobre el tablero
            abierto en memoria de KiCad (evita que dos procesos escriban el
            mismo archivo). La CLI siempre usa True.
        modo_orden: 'distancia' | 'mps' | 'ag'. Determina quién decide el
            orden de enrutamiento. Con 'ag', la salida del AG se respeta tal
            cual (enrutar_todos recibe estrategia_orden='ninguna').
        fitness: 'surrogate' (A* grueso secuencial) o 'aproximada' (oclusión
            progresiva) — solo aplica con modo_orden='ag'.
        paso_surrogate: Paso de cuadrícula del surrogate en mm (defecto 0.5).
        anchos_por_red: Dict opcional {nombre_red: ancho_mm}; las redes no
            listadas usan ancho_pista.
        callback_progreso: Función f(paso, total, mensaje) para UI

    Returns:
        (ruta_archivo_salida, texto_reporte, segmentos_nuevos, hubo_conexiones)
        ruta_archivo_salida: None si escribir_archivo=False
        segmentos_nuevos: lista de dicts {'x1','y1','x2','y2','red'}
        hubo_conexiones: False si no había nada pendiente de enrutar
    """
    tiempo_inicio = time.time()

    # Contexto del intento actual, para que la barra de progreso avance de 0% a
    # 100% a lo largo de los N intentos en vez de reiniciarse en cada uno.
    #
    # `total` arranca en el N pedido y no en 1: el primer aviso de progreso (la
    # lectura del PCB) ocurre ANTES de saber si el best-of-N aplica realmente,
    # y si empezara en 1 ese primer aviso se calcularía sobre 5 pasos en vez de
    # sobre 5*N — la barra saltaría a 20% y luego RETROCEDERÍA a 13%. Partir del
    # máximo garantiza que cualquier corrección posterior sólo pueda subir el
    # porcentaje, nunca bajarlo.
    _ctx_intento = {'indice': 0, 'total': max(1, repeticiones)}

    def _progreso(paso, total, mensaje=""):
        if callback_progreso:
            paso_global = _ctx_intento['indice'] * total + paso
            total_global = _ctx_intento['total'] * total
            callback_progreso(paso_global, total_global, mensaje)
        else:
            print(f"[{paso}/{total}] {mensaje}")

    _progreso(1, 5, "Leyendo archivo PCB...")

    # ── Paso 1: Leer PCB ────────────────────────────────────────────────────
    datos = leer_pcb(ruta_entrada)
    conexiones = obtener_conexiones_a_enrutar(datos)

    if not conexiones:
        reporte = "No hay conexiones para enrutar. El PCB ya está completamente enrutado."
        return (ruta_entrada if escribir_archivo else None), reporte, [], False

    print(f"\n[Main] {len(conexiones)} conexiones a enrutar")

    # ── Resolver modo de orden ───────────────────────────────────────────────
    # modo_orden explícito manda; si es None, usar_ag (checkbox de la GUI)
    # decide entre 'ag' y 'mps'.
    if modo_orden is None:
        modo_orden = 'ag' if usar_ag else 'mps'
    if modo_orden not in ('distancia', 'mps', 'ag'):
        raise ValueError(f"modo_orden inválido: {modo_orden!r}")

    # ── Pasos 2-3: orden + enrutado, posiblemente repetido ──────────────────
    # Sólo el AG tiene componente aleatoria: con 'distancia' o 'mps' el
    # resultado es determinista y repetir N veces daría N copias iguales.
    usa_ag = modo_orden == 'ag' and len(conexiones) > 3
    n_repeticiones = max(1, repeticiones) if usa_ag else 1
    if repeticiones > 1 and not usa_ag:
        print(f"[Main] Los intentos best-of-N sólo aplican con --orden ag "
              f"(el orden '{modo_orden}' es determinista y daría N copias "
              f"iguales); usando 1")

    def _cb_astar(i, total, red, exito):
        _progreso(3, 5, f"A* conexión {i}/{total}: {red}")

    def _una_corrida(semilla_corrida: Optional[int],
                     origen: str) -> dict:
        """Una corrida completa: orden (AG si aplica) + A* a resolución plena."""
        if usa_ag:
            _progreso(2, 5, "Optimizando orden con Algoritmo Genético...")
            cx, stats = optimizar_orden_enrutamiento(
                conexiones,
                datos.todos_los_pads,
                datos.limite_tablero,
                tamano_poblacion=tamano_poblacion,
                num_generaciones=num_generaciones,
                fitness=fitness,
                paso_surrogate=paso_surrogate,
                ancho_pista=ancho_pista,
                clearance=clearance,
                anchos_por_red=anchos_por_red,
                semilla=semilla_corrida,
                origen_semilla=origen
            )
            estrategia = 'ninguna'   # respetar la salida del AG
        else:
            _progreso(2, 5, f"Orden por estrategia '{modo_orden}' (sin AG)...")
            cx = conexiones
            stats = {
                'generaciones': 0, 'tamano_poblacion': 0,
                'mejor_costo': 0.0, 'tiempo_segundos': 0.0,
                'mejora_porcentual': 0.0,
                'mejora_vs_aleatorio': 0.0, 'mejora_vs_distancia': 0.0,
                'mejora_vs_mps': 0.0,
                'historial_mejor': [], 'historial_promedio': []
            }
            estrategia = modo_orden if modo_orden != 'ag' else 'mps'

        _progreso(3, 5, "Enrutando con algoritmo A*...")
        segs, met = enrutar_todos(
            cx,
            datos.todos_los_pads,
            datos.limite_tablero,
            paso_cuadricula=paso_cuadricula,
            ancho_pista=ancho_pista,
            clearance=clearance,
            segmentos_existentes=datos.segmentos_existentes,
            estrategia_orden=estrategia,
            anchos_por_red=anchos_por_red,
            clearance_minimo=clearance_minimo,
            callback_progreso=_cb_astar
        )
        # Conectividad y clearance reducido se evalúan POR INTENTO: son
        # criterios de selección entre intentos, no chequeos posteriores.
        met['conectividad'] = verificar_conectividad(datos, segs, ancho_pista)
        # Fragmentación del plano: las redes con zona no se enrutan, así que
        # `conectividad` las excluye por completo y no puede ver que las pistas
        # de las demás redes hayan partido el plano en islas. Es un criterio de
        # selección, no un chequeo posterior: por eso se evalúa POR INTENTO.
        met['zona'] = analizar_fragmentacion_zona(datos, segs, ancho_pista)
        met['fragmentos_zona'] = met['zona']['fragmentos']
        met['clearance_reducido'] = sum(
            1 for d in met.get('detalle', [])
            if d.get('exito')
            and d.get('clearance_usado', clearance) < clearance - 1e-9)
        return {'segmentos': segs, 'metricas': met, 'estadisticas_ag': stats}

    semillas_usadas = _derivar_semillas(semilla_ag, n_repeticiones)
    if n_repeticiones > 1:
        print(f"\n[Main] Best-of-N: {n_repeticiones} intentos; "
              f"semillas: {', '.join(str(s) for s in semillas_usadas)}")
        if semilla_ag is not None:
            # Reutilizar la misma semilla daría N órdenes idénticos: el
            # best-of-N no compararía nada y el tiempo se gastaría en balde.
            print(f"[Main] Semilla fija {semilla_ag}: los intentos usan "
                  f"{semilla_ag}..{semilla_ag + n_repeticiones - 1} para no "
                  f"repetir el mismo orden N veces.")

    resultados = []
    tiempo_intentos_inicio = time.time()
    _ctx_intento['total'] = n_repeticiones
    for k, s in enumerate(semillas_usadas):
        _ctx_intento['indice'] = k
        if n_repeticiones > 1:
            print(f"\n{'='*60}\n[Best-of-N] Intento {k+1}/{n_repeticiones} "
                  f"(semilla {s})\n{'='*60}")
        t_intento = time.time()
        # El origen lo declara quien conoce la procedencia real: con
        # best-of-N las semillas las sortea _derivar_semillas, así que
        # llegan al AG como enteros concretos y allí ya no se distinguen
        # de una que el usuario haya escrito.
        if semilla_ag is not None:
            origen = 'usuario'
        elif n_repeticiones > 1:
            origen = 'sistema_bestofn'
        else:
            origen = 'sistema'
        r = _una_corrida(s if usa_ag else semilla_ag, origen)
        r['semilla'] = s
        r['clave'] = _clave_calidad(r['metricas'])
        r['tiempo'] = time.time() - t_intento
        resultados.append(r)
        if n_repeticiones > 1:
            m = r['metricas']
            c = m['conectividad']
            print(f"[Best-of-N] Intento {k+1}/{n_repeticiones} — semilla {s}: "
                  f"{m['exitosas']}/{m['total']} conexiones, "
                  f"{c['completas']}/{c['total']} redes, "
                  f"{m['longitud_total_mm']}mm, "
                  f"{m.get('rutas_patologicas', 0)} patológicas")
    tiempo_intentos = time.time() - tiempo_intentos_inicio

    mejor = min(resultados, key=lambda r: r['clave'])
    segmentos_nuevos = mejor['segmentos']
    metricas_astar = mejor['metricas']
    estadisticas_ag = mejor['estadisticas_ag']
    conectividad = metricas_astar['conectividad']

    if n_repeticiones > 1:
        metricas_astar['repeticiones'] = {
            'n': n_repeticiones,
            'semillas': semillas_usadas,
            'elegida': mejor['semilla'],
            'tiempo_total': round(tiempo_intentos, 1),
            'resumen': [
                {'intento': i + 1,
                 'semilla': r['semilla'],
                 'redes_completas': r['metricas']['conectividad']['completas'],
                 'redes_total': r['metricas']['conectividad']['total'],
                 'fallidas': r['metricas']['fallidas'],
                 'fragmentos_zona': r['metricas'].get('fragmentos_zona', 0),
                 'clearance_reducido': r['metricas'].get('clearance_reducido', 0),
                 'patologicas': r['metricas'].get('rutas_patologicas', 0),
                 'longitud': r['metricas']['longitud_total_mm'],
                 'elegida': r is mejor}
                for i, r in enumerate(resultados)
            ],
            'ninguna_completa': conectividad['completas'] < conectividad['total'],
        }
        print(f"\n[Main] Mejor repetición: semilla {mejor['semilla']} "
              f"({conectividad['completas']}/{conectividad['total']} redes, "
              f"{metricas_astar['fallidas']} fallidas, "
              f"{metricas_astar['longitud_total_mm']}mm)")
        if conectividad['completas'] < conectividad['total']:
            print("[Main] ADVERTENCIA: ninguna repetición logró conectividad "
                  "completa; se devuelve la de más redes conectadas")

    # ── Paso 4: DRC ─────────────────────────────────────────────────────────
    _progreso(4, 5, "Verificando reglas DRC...")

    errores_drc = []
    if ejecutar_drc and segmentos_nuevos:
        errores_drc = drc_basico(segmentos_nuevos, datos.todos_los_pads,
                                  ancho_pista, clearance)

    # ── Advertencias por clearance reducido (contra la regla NOMINAL) ────────
    # Los reintentos pueden resolver una conexión con menos clearance que el
    # configurado; eso NO es un enrutado limpio y debe quedar en el reporte.
    advertencias_clearance = [
        det for det in metricas_astar.get('detalle', [])
        if det.get('exito') and det.get('clearance_usado', clearance) < clearance - 1e-9
    ]

    # ── Paso 5: Escribir archivo ─────────────────────────────────────────────
    if escribir_archivo:
        _progreso(5, 5, "Escribiendo archivo de salida...")
        ruta_final = escribir_pcb_enrutado(
            datos, segmentos_nuevos, ancho_pista, ruta_salida, con_timestamp
        )
    else:
        _progreso(5, 5, "Omitiendo escritura a disco (se aplicará en memoria)...")
        ruta_final = None

    tiempo_total = time.time() - tiempo_inicio

    # ── Generar reporte ──────────────────────────────────────────────────────
    reporte = generar_reporte(
        metricas_astar, estadisticas_ag, errores_drc,
        ruta_final if ruta_final else "(aplicado en memoria, sin archivo)",
        tiempo_total, ancho_pista, clearance,
        modo_orden=modo_orden,
        advertencias_clearance=advertencias_clearance,
        paso_cuadricula=paso_cuadricula
    )

    print(f"\n{reporte}")

    return ruta_final, reporte, segmentos_nuevos, True


# ─────────────────────────────────────────────────────────────────────────────
# Interfaz de línea de comandos
# ─────────────────────────────────────────────────────────────────────────────

def main():
    """Punto de entrada para ejecución desde línea de comandos."""
    parser = argparse.ArgumentParser(
        description='Asistente de Enrutamiento PCB en KiCad 10',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python escritor_pcb.py casos_prueba/mi_pcb.kicad_pcb
  python escritor_pcb.py mi_pcb.kicad_pcb --ancho 0.25 --clearance 0.15
  python escritor_pcb.py mi_pcb.kicad_pcb --orden ag                 (AG, fitness surrogate)
  python escritor_pcb.py mi_pcb.kicad_pcb --orden ag --fitness aproximada
  python escritor_pcb.py mi_pcb.kicad_pcb --orden distancia          (baseline)
  python escritor_pcb.py mi_pcb.kicad_pcb --ancho-red "GND=1.0" --ancho-red "+12V=1.0"
"""
    )

    parser.add_argument('archivo_pcb',
                        help='Archivo .kicad_pcb a enrutar')
    parser.add_argument('--ancho', type=float, default=0.3,
                        help='Ancho de pista en mm (defecto: 0.3)')
    parser.add_argument('--clearance', type=float, default=0.2,
                        help='Clearance nominal en mm (defecto: 0.2)')
    parser.add_argument('--clearance-minimo', type=float, default=0.127,
                        help='Piso de clearance fabricable en mm (defecto: 0.127, '
                             'mínimo de JLCPCB). Los reintentos nunca bajan de '
                             'aquí; lo que solo se resuelve por debajo se marca '
                             'FALLIDO en vez de éxito con advertencia.')
    parser.add_argument('--paso', type=float, default=0.25,
                        help='Resolución de cuadrícula A* en mm (defecto: 0.25)')
    parser.add_argument('--orden', choices=['distancia', 'mps', 'ag'],
                        default='mps',
                        help="Estrategia de orden de enrutamiento: 'distancia' "
                             "(más corta primero), 'mps' (rondas de cruces, "
                             "defecto), 'ag' (Algoritmo Genético con aptitud "
                             "secuencial)")
    parser.add_argument('--fitness', choices=['surrogate', 'aproximada'],
                        default='surrogate',
                        help="Aptitud del AG: 'surrogate' (A* grueso, fiel, "
                             "defecto) o 'aproximada' (oclusión, rápida)")
    parser.add_argument('--paso-surrogate', type=float, default=0.5,
                        help='Paso de cuadrícula del surrogate en mm (defecto: 0.5)')
    parser.add_argument('--poblacion', type=int, default=24,
                        help='Tamaño de población del AG (defecto: 24)')
    parser.add_argument('--intentos', '--repeticiones', type=int, default=1,
                        dest='intentos',
                        help='Best-of-N: corre el enrutado completo N veces con '
                             'semillas distintas y aplica sólo el mejor, según '
                             '(fallidas → redes incompletas → clearance reducido '
                             '→ patológicas → longitud). El tiempo se multiplica '
                             'por N. Sólo aplica con --orden ag. Defecto: 1.')
    parser.add_argument('--semilla', type=int, default=None,
                        help='Semilla aleatoria del AG. Por defecto NO se fija: '
                             'cada corrida usa entropía del sistema y es '
                             'independiente (necesario para reportar media y '
                             'desviación). Pase un valor para hacer la corrida '
                             'reproducible.')
    parser.add_argument('--generaciones', type=int, default=40,
                        help='Número de generaciones del AG (defecto: 40)')
    parser.add_argument('--ancho-red', action='append', default=[],
                        metavar='RED=ANCHO',
                        help='Ancho de pista por red, ej: --ancho-red "+12V=1.0" '
                             '--ancho-red "GND=1.0" (repetible; las redes no '
                             'listadas usan --ancho)')
    parser.add_argument('--sin-ag', action='store_true',
                        help='(Compatibilidad) Equivale a --orden mps')
    parser.add_argument('--sin-drc', action='store_true',
                        help='Deshabilitar verificación DRC')
    parser.add_argument('--salida', type=str, default=None,
                        help='Ruta de archivo de salida (opcional)')
    parser.add_argument('--con-timestamp', action='store_true',
                        help='Agregar fecha/hora al nombre de salida en vez de '
                             'sobrescribir siempre <base>_enrutado.kicad_pcb '
                             '(permite conservar corridas para comparar)')

    args = parser.parse_args()

    if not os.path.exists(args.archivo_pcb):
        print(f"ERROR: No se encuentra el archivo: {args.archivo_pcb}")
        sys.exit(1)

    # --sin-ag (compatibilidad) fuerza modo mps
    modo_orden = 'mps' if args.sin_ag else args.orden

    # Parsear --ancho-red RED=ANCHO
    anchos_por_red = {}
    for entrada in args.ancho_red:
        if '=' not in entrada:
            print(f"ERROR: --ancho-red inválido: {entrada!r} (formato: RED=ANCHO)")
            sys.exit(1)
        red, _, valor = entrada.rpartition('=')
        try:
            anchos_por_red[red] = float(valor)
        except ValueError:
            print(f"ERROR: ancho no numérico en --ancho-red: {entrada!r}")
            sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  ASISTENTE DE ENRUTAMIENTO PCB en KiCad 10")
    print(f"{'='*60}")
    print(f"  Entrada:     {args.archivo_pcb}")
    print(f"  Ancho pista: {args.ancho} mm"
          + (f" (+{len(anchos_por_red)} redes con ancho propio)" if anchos_por_red else ""))
    print(f"  Clearance:   {args.clearance} mm")
    print(f"  Orden:       {modo_orden}"
          + (f" (fitness={args.fitness}, paso={args.paso_surrogate}mm, "
             f"{args.generaciones} gen, {args.poblacion} ind)"
             if modo_orden == 'ag' else ""))
    print(f"{'='*60}\n")

    try:
        ruta_salida, reporte, _, _ = enrutar_pcb(
            ruta_entrada=args.archivo_pcb,
            ancho_pista=args.ancho,
            clearance=args.clearance,
            paso_cuadricula=args.paso,
            tamano_poblacion=args.poblacion,
            num_generaciones=args.generaciones,
            ejecutar_drc=not args.sin_drc,
            ruta_salida=args.salida,
            con_timestamp=args.con_timestamp,
            modo_orden=modo_orden,
            fitness=args.fitness,
            paso_surrogate=args.paso_surrogate,
            anchos_por_red=anchos_por_red or None,
            clearance_minimo=args.clearance_minimo,
            semilla_ag=args.semilla,
            repeticiones=args.intentos
        )
    except PermissionError as e:
        print(f"\nERROR: {e}")
        sys.exit(1)

    print(f"\nArchivo generado: {ruta_salida}")
    print("Abra el archivo en KiCad 10 para verificar el resultado.")


if __name__ == "__main__":
    main()
