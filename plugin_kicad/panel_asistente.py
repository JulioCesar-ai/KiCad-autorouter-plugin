"""
panel_asistente.py — Interfaz gráfica wxPython del Asistente de Enrutamiento PCB

Implementa el diálogo modal con todos los controles para configurar y
ejecutar el enrutamiento desde dentro de KiCad 10.

"""

import os
import sys
import threading
from typing import Dict, Optional, Set

import wx
import wx.lib.scrolledpanel as scrolled

# Asegurar que los módulos del proyecto sean accesibles
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)


# ─────────────────────────────────────────────────────────────────────────────
# Modo de aplicación del resultado: archivo vs memoria
# ─────────────────────────────────────────────────────────────────────────────

# Registro a nivel de MÓDULO de los KIIDs de tracks creados por el asistente,
# por placa (ruta normalizada → set de KIIDs). Al ser de módulo y no de
# instancia, sobrevive a cerrar y reabrir el diálogo dentro de la misma sesión
# de KiCad (el intérprete Python embebido mantiene el módulo cargado). Se
# pierde al reiniciar KiCad — comportamiento aceptado; ver el mensaje al
# usuario en _enrutamiento_completado cuando el registro está vacío.
_UUIDS_ASISTENTE: Dict[str, Set[str]] = {}

# Aviso de "vas a modificar el archivo ORIGINAL en memoria": se muestra UNA vez
# mientras KiCad siga abierto. Vive a nivel de módulo y no de instancia a
# propósito: cerrar y reabrir el diálogo entre corridas es lo normal, y atado a
# la instancia el usuario vería el aviso cada vez — justo lo que se busca evitar.
_CONFIRMACION_ORIGINAL_MOSTRADA = False


def _clave_placa(ruta: str) -> str:
    """Normaliza la ruta para usarla como clave del registro (Windows-safe)."""
    return os.path.normcase(os.path.abspath(ruta))


def _es_archivo_derivado(ruta: str) -> bool:
    """True si el nombre coincide con el patrón de archivo generado (_enrutado)."""
    from escritor_pcb import _PATRON_SUFIJO_ENRUTADO
    raiz, _ext = os.path.splitext(ruta)
    return bool(_PATRON_SUFIJO_ENRUTADO.search(raiz))


def _tablero_abierto_coincide(ruta: str):
    """
    Retorna el BOARD de pcbnew si KiCad tiene abierto EXACTAMENTE ese archivo.

    Si no hay pcbnew (ejecución fuera de KiCad), no hay tablero abierto, o el
    tablero abierto es OTRO archivo distinto al seleccionado en el diálogo,
    retorna None — en ese caso se usa el modo archivo (Regla 1), nunca se
    inyecta sobre una placa que no corresponde.
    """
    try:
        import pcbnew
    except ImportError:
        return None

    board = pcbnew.GetBoard()
    if board is None:
        return None

    ruta_abierta = board.GetFileName() or ""
    if not ruta_abierta:
        return None

    if _clave_placa(ruta_abierta) != _clave_placa(ruta):
        return None

    return board


def _obtener_uuid_track(track) -> str:
    """KIID del track como string. AsString() es la vía estable; str() es fallback."""
    try:
        return track.m_Uuid.AsString()
    except AttributeError:
        return str(track.m_Uuid)


def _aplicar_en_memoria(board, ruta_clave: str,
                        segmentos: list, ancho_pista: float) -> int:
    """
    Aplica los segmentos sobre el tablero abierto en memoria de KiCad (Regla 2).

    Regla 3 — reenrutado sin destruir trabajo manual:
      1. Remueve SOLO los tracks cuyo KIID esté en el registro de la corrida
         anterior del asistente (_UUIDS_ASISTENTE). Cualquier otra pista —
         trazada a mano antes o después de abrir el archivo, o proveniente
         del propio archivo — se respeta, venga de donde venga.
      2. Inyecta los segmentos nuevos y registra el KIID de cada track creado.
      3. Actualiza el registro con los KIIDs nuevos.

    No escribe a disco: el usuario guarda con Ctrl+S cuando el resultado
    le convenza.

    Returns:
        Número de tracks agregados al tablero.
    """
    import pcbnew

    # 1. Remover únicamente los tracks de la corrida anterior del asistente
    anteriores = _UUIDS_ASISTENTE.get(ruta_clave, set())
    removidos = 0
    if anteriores:
        a_quitar = [
            t for t in board.GetTracks()
            if t.GetClass() == "PCB_TRACK" and _obtener_uuid_track(t) in anteriores
        ]
        for t in a_quitar:
            board.Remove(t)
            removidos += 1
    if removidos:
        print(f"[Memoria] {removidos} pistas de la corrida anterior del asistente removidas")

    # 2. Inyectar los segmentos nuevos y registrar sus KIIDs
    nuevos_kiids: Set[str] = set()
    for seg in segmentos:
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I(
            pcbnew.FromMM(round(seg['x1'], 4)),
            pcbnew.FromMM(round(seg['y1'], 4))
        ))
        track.SetEnd(pcbnew.VECTOR2I(
            pcbnew.FromMM(round(seg['x2'], 4)),
            pcbnew.FromMM(round(seg['y2'], 4))
        ))
        track.SetWidth(pcbnew.FromMM(round(seg.get('ancho', ancho_pista), 4)))
        track.SetLayer(pcbnew.F_Cu)

        net = board.FindNet(seg['red'])
        if net is not None:
            track.SetNetCode(net.GetNetCode())

        board.Add(track)
        nuevos_kiids.add(_obtener_uuid_track(track))

    # 3. Actualizar registro y refrescar vista
    _UUIDS_ASISTENTE[ruta_clave] = nuevos_kiids
    board.BuildConnectivity()
    pcbnew.Refresh()

    return len(nuevos_kiids)


def _deshacer_aplicacion(board, ruta_clave: str) -> int:
    """
    Retira del tablero las pistas de la última aplicación del asistente.

    Es la vía de escape fiable del modo memoria. NO se delega en Ctrl+Z de
    KiCad: no está verificado que `board.Add()` desde un plugin SWIG genere un
    punto de deshacer, y un escape que se cree tener y no existe es peor que no
    tener ninguno. Aquí la garantía es propia: se conocen exactamente los KIIDs
    que se agregaron, así que se retiran esos y sólo esos — las pistas del
    usuario nunca estuvieron en el registro y no pueden verse afectadas.

    Returns:
        Número de pistas retiradas.
    """
    import pcbnew

    anteriores = _UUIDS_ASISTENTE.get(ruta_clave, set())
    if not anteriores:
        return 0

    a_quitar = [
        t for t in board.GetTracks()
        if t.GetClass() == "PCB_TRACK" and _obtener_uuid_track(t) in anteriores
    ]
    for t in a_quitar:
        board.Remove(t)

    _UUIDS_ASISTENTE.pop(ruta_clave, None)
    board.BuildConnectivity()
    pcbnew.Refresh()
    return len(a_quitar)


# ─────────────────────────────────────────────────────────────────────────────
# Redireccionador de stdout hacia área de texto wx
# ─────────────────────────────────────────────────────────────────────────────

class RedireccionadorTexto:
    """
    Redirige la salida estándar (print) hacia un control wx.TextCtrl.

    Permite que los mensajes del enrutador (A*, AG) aparezcan en tiempo
    real en el área de texto del diálogo.
    """

    def __init__(self, texto_ctrl: wx.TextCtrl):
        self.texto_ctrl = texto_ctrl
        self._stdout_original = sys.stdout

    def write(self, texto: str):
        """Escribe en el control wx de forma segura desde otro hilo."""
        if texto.strip():
            wx.CallAfter(self._agregar_texto, texto)

    def _agregar_texto(self, texto: str):
        """Agrega texto al control (debe llamarse desde el hilo principal)."""
        try:
            self.texto_ctrl.AppendText(texto + '\n' if not texto.endswith('\n') else texto)
            # Hacer scroll al final
            self.texto_ctrl.ShowPosition(self.texto_ctrl.GetLastPosition())
        except Exception:
            pass

    def flush(self):
        pass

    def activar(self):
        sys.stdout = self

    def desactivar(self):
        sys.stdout = self._stdout_original


# ─────────────────────────────────────────────────────────────────────────────
# Diálogo principal
# ─────────────────────────────────────────────────────────────────────────────

class PanelAsistente(wx.Dialog):
    """
    Diálogo principal del Asistente de Enrutamiento PCB.

    Proporciona una interfaz gráfica completa para:
      - Seleccionar el archivo PCB a enrutar
      - Configurar parámetros de pista, clearance y cuadrícula
      - Configurar el Algoritmo Genético
      - Ejecutar el enrutamiento en segundo plano
      - Ver el progreso y los resultados en tiempo real
    """

    def __init__(self, parent, ruta_tablero_inicial: str = ""):
        super().__init__(
            parent,
            title="Asistente de Enrutamiento PCB en KiCad 10",
            size=(620, 750),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
        )

        self._enrutando = False
        # Ruta de la última placa sobre la que se aplicó en memoria; habilita
        # el botón de deshacer y le dice sobre qué tablero actuar.
        self._ultima_ruta_memoria = None
        self._hilo_enrutamiento = None
        self._redireccionador = None

        self._construir_interfaz()
        self._conectar_eventos()

        # Si KiCad tiene un tablero abierto, pre-rellenar la ruta
        if ruta_tablero_inicial and os.path.exists(ruta_tablero_inicial):
            self.campo_archivo.SetValue(ruta_tablero_inicial)

        self.CenterOnParent()

    # ── Construcción de la interfaz ──────────────────────────────────────────

    def _construir_interfaz(self):
        """Construye todos los controles del diálogo."""
        panel = wx.Panel(self)
        sizer_principal = wx.BoxSizer(wx.VERTICAL)

        # ── Sección: Archivo PCB ─────────────────────────────────────────────
        sizer_principal.Add(
            self._crear_seccion_archivo(panel), 0, wx.EXPAND | wx.ALL, 8
        )

        # ── Sección: Parámetros de pista ─────────────────────────────────────
        sizer_principal.Add(
            self._crear_seccion_pista(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8
        )

        # ── Sección: Algoritmo Genético ──────────────────────────────────────
        sizer_principal.Add(
            self._crear_seccion_ag(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8
        )

        # ── Sección: Opciones ────────────────────────────────────────────────
        sizer_principal.Add(
            self._crear_seccion_opciones(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8
        )

        # ── Botones de acción ────────────────────────────────────────────────
        sizer_principal.Add(
            self._crear_botones(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8
        )

        # ── Barra de progreso ────────────────────────────────────────────────
        sizer_progreso = wx.BoxSizer(wx.HORIZONTAL)
        lbl_progreso = wx.StaticText(panel, label="Progreso:")
        self.barra_progreso = wx.Gauge(panel, range=100, size=(-1, 20))
        self.lbl_porcentaje = wx.StaticText(panel, label="0%", size=(40, -1))
        sizer_progreso.Add(lbl_progreso, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        sizer_progreso.Add(self.barra_progreso, 1, wx.EXPAND | wx.RIGHT, 8)
        sizer_progreso.Add(self.lbl_porcentaje, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer_principal.Add(sizer_progreso, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── Área de resultados ───────────────────────────────────────────────
        sizer_principal.Add(
            self._crear_area_resultados(panel), 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8
        )

        panel.SetSizer(sizer_principal)

        # Sizer del diálogo
        sizer_dialogo = wx.BoxSizer(wx.VERTICAL)
        sizer_dialogo.Add(panel, 1, wx.EXPAND)
        self.SetSizer(sizer_dialogo)
        self.Layout()

    def _crear_seccion_archivo(self, parent) -> wx.StaticBoxSizer:
        """Sección para seleccionar el archivo PCB."""
        caja = wx.StaticBox(parent, label="Archivo PCB")
        sizer = wx.StaticBoxSizer(caja, wx.HORIZONTAL)

        self.campo_archivo = wx.TextCtrl(parent, style=wx.TE_READONLY)
        self.campo_archivo.SetHint("Seleccione un archivo .kicad_pcb...")

        btn_abrir = wx.Button(parent, label="Abrir...")
        btn_abrir.SetToolTip("Seleccionar archivo PCB de KiCad 10")
        self._btn_abrir = btn_abrir

        sizer.Add(self.campo_archivo, 1, wx.EXPAND | wx.ALL, 4)
        sizer.Add(btn_abrir, 0, wx.ALL, 4)
        return sizer

    def _crear_seccion_pista(self, parent) -> wx.StaticBoxSizer:
        """Sección para parámetros de pista."""
        caja = wx.StaticBox(parent, label="Parámetros de Pista")
        sizer = wx.StaticBoxSizer(caja, wx.VERTICAL)
        grid = wx.FlexGridSizer(3, 3, 4, 8)
        grid.AddGrowableCol(1)

        # Ancho de pista
        grid.Add(wx.StaticText(parent, label="Ancho de pista (mm):"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.campo_ancho = wx.SpinCtrlDouble(parent, value="0.50",
                                              min=0.1, max=5.0, inc=0.05)
        self.campo_ancho.SetDigits(2)
        self.campo_ancho.SetToolTip("Ancho de las pistas enrutadas (mínimo recomendado: 0.15mm)")
        grid.Add(self.campo_ancho, 0, wx.EXPAND)
        grid.Add(wx.StaticText(parent, label="mm"), 0, wx.ALIGN_CENTER_VERTICAL)

        # Clearance
        grid.Add(wx.StaticText(parent, label="Clearance mínimo (mm):"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.campo_clearance = wx.SpinCtrlDouble(parent, value="0.35",
                                                  min=0.05, max=2.0, inc=0.05)
        self.campo_clearance.SetDigits(2)
        self.campo_clearance.SetToolTip("Separación mínima entre pistas y pads")
        grid.Add(self.campo_clearance, 0, wx.EXPAND)
        grid.Add(wx.StaticText(parent, label="mm"), 0, wx.ALIGN_CENTER_VERTICAL)

        # Paso de cuadrícula
        grid.Add(wx.StaticText(parent, label="Paso cuadrícula A* (mm):"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.campo_paso = wx.SpinCtrlDouble(parent, value="0.25",
                                             min=0.05, max=1.0, inc=0.05)
        self.campo_paso.SetDigits(2)
        self.campo_paso.SetToolTip(
            "Resolución de la cuadrícula del enrutador A*.\n"
            "Menor valor = mayor precisión pero más lento."
        )
        grid.Add(self.campo_paso, 0, wx.EXPAND)
        grid.Add(wx.StaticText(parent, label="mm"), 0, wx.ALIGN_CENTER_VERTICAL)

        sizer.Add(grid, 1, wx.EXPAND | wx.ALL, 4)

        return sizer

    def _crear_seccion_ag(self, parent) -> wx.StaticBoxSizer:
        """Sección para el Algoritmo Genético."""
        caja = wx.StaticBox(parent, label="Algoritmo Genético (Optimización de Orden)")
        sizer = wx.StaticBoxSizer(caja, wx.VERTICAL)

        # Checkbox para habilitar AG
        self.check_ag = wx.CheckBox(parent, label="Usar Algoritmo Genético para optimizar el orden de enrutamiento")
        self.check_ag.SetValue(True)
        self.check_ag.SetToolTip(
            "El AG optimiza el orden en que se enrutan las conexiones\n"
            "para minimizar la longitud total y reducir cruces."
        )
        sizer.Add(self.check_ag, 0, wx.ALL, 4)

        grid = wx.FlexGridSizer(4, 3, 4, 8)
        grid.AddGrowableCol(1)

        # Generaciones
        grid.Add(wx.StaticText(parent, label="Generaciones:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.campo_generaciones = wx.SpinCtrl(parent, value="20", min=5, max=2000)
        self.campo_generaciones.SetToolTip(
            "Número de generaciones del AG.\n"
            "Mayor valor = mejor optimización pero más tiempo."
        )
        grid.Add(self.campo_generaciones, 0, wx.EXPAND)
        grid.Add(wx.StaticText(parent, label="(10–2000)"), 0, wx.ALIGN_CENTER_VERTICAL)

        # Tamaño de población
        grid.Add(wx.StaticText(parent, label="Tamaño de población:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.campo_poblacion = wx.SpinCtrl(parent, value="16", min=6, max=500)
        self.campo_poblacion.SetToolTip(
            "Número de individuos (órdenes de enrutamiento) por generación.\n"
            "Mayor valor = más diversidad pero más lento."
        )
        grid.Add(self.campo_poblacion, 0, wx.EXPAND)
        grid.Add(wx.StaticText(parent, label="(10–500)"), 0, wx.ALIGN_CENTER_VERTICAL)

        # Semilla (opcional)
        grid.Add(wx.StaticText(parent, label="Semilla (opcional):"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.campo_semilla = wx.TextCtrl(parent, value="")
        self.campo_semilla.SetHint("vacío = aleatoria")
        self.campo_semilla.SetToolTip(
            "Semilla del generador aleatorio del AG.\n"
            "Vacío: cada corrida es independiente (entropía del sistema).\n"
            "La semilla usada se informa en el reporte, así que una corrida\n"
            "puede repetirse después escribiéndola aquí."
        )
        grid.Add(self.campo_semilla, 0, wx.EXPAND)
        grid.Add(wx.StaticText(parent, label="(entero)"), 0, wx.ALIGN_CENTER_VERTICAL)

        # Best-of-N: el AG es muy estable en LONGITUD pero no en robustez —
        # con semilla aleatoria, algunas corridas dejan una red abierta o una
        # ruta patológica. Repetir y quedarse con el mejor ataca esa varianza.
        grid.Add(wx.StaticText(parent, label="Intentos internos (best-of-N):"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.campo_intentos = wx.SpinCtrl(parent, value="1", min=1, max=10)
        self.campo_intentos.SetToolTip(
            "Ejecuta N enrutamientos completos y aplica SÓLO el mejor.\n\n"
            "El tiempo se multiplica por N: con 3 intentos, tarda el triple.\n\n"
            "Sólo tiene efecto con semilla aleatoria (campo Semilla vacío):\n"
            "con semilla fija los intentos usan semilla, semilla+1, ... para\n"
            "no repetir el mismo orden N veces.\n\n"
            "Criterio de selección, en orden estricto:\n"
            "  fallidas → redes incompletas → clearance reducido →\n"
            "  rutas patológicas → longitud.\n"
            "Una placa más corta con una red abierta nunca gana."
        )
        grid.Add(self.campo_intentos, 0, wx.EXPAND)
        grid.Add(wx.StaticText(parent, label="(1–10)"), 0, wx.ALIGN_CENTER_VERTICAL)

        sizer.Add(grid, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)
        return sizer

    def _crear_seccion_opciones(self, parent) -> wx.StaticBoxSizer:
        """Sección de opciones adicionales."""
        caja = wx.StaticBox(parent, label="Opciones")
        sizer = wx.StaticBoxSizer(caja, wx.VERTICAL)

        self.check_drc = wx.CheckBox(parent, label="Ejecutar verificación DRC al terminar")
        self.check_drc.SetValue(True)
        self.check_drc.SetToolTip(
            "Verifica reglas de diseño básicas:\n"
            "- Ancho mínimo de pista\n"
            "- Cruces entre redes diferentes"
        )
        sizer.Add(self.check_drc, 0, wx.ALL, 4)

        # Permite usar el modo memoria sobre el archivo ORIGINAL del proyecto,
        # no sólo sobre derivados _enrutado. Con el derivado, KiCad no asocia
        # PCB y esquemático (los vincula por nombre) y no se puede correr la
        # prueba de "Paridad del esquema" del DRC sin renombrar a mano.
        self.check_memoria = wx.CheckBox(
            parent,
            label="Aplicar sobre el tablero abierto (modifica la placa en memoria)")
        self.check_memoria.SetValue(False)
        self.check_memoria.SetToolTip(
            "Aplica las pistas sobre la placa que KiCad tiene abierta, en vez\n"
            "de generar un archivo _enrutado aparte.\n\n"
            "El archivo en disco NO se modifica: se guarda con Ctrl+S sólo si\n"
            "el resultado convence, o se descarta cerrando sin guardar o con\n"
            "el botón «Deshacer última aplicación».\n\n"
            "Permite trabajar dentro del proyecto y correr el DRC completo,\n"
            "incluida la paridad con el esquemático."
        )
        sizer.Add(self.check_memoria, 0, wx.ALL, 4)

        return sizer

    def _crear_botones(self, parent) -> wx.BoxSizer:
        """Crea los botones de acción."""
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.btn_enrutar = wx.Button(parent, label="Iniciar Enrutamiento")
        self.btn_enrutar.SetMinSize((240, 36))
        self.btn_enrutar.SetToolTip("Ejecutar el flujo completo de enrutamiento")
        font_btn = self.btn_enrutar.GetFont()
        font_btn.SetWeight(wx.FONTWEIGHT_BOLD)
        self.btn_enrutar.SetFont(font_btn)

        # Escape del modo memoria. Arranca oculto y sólo aparece cuando hay algo
        # que deshacer, para que su presencia signifique exactamente eso.
        self.btn_deshacer = wx.Button(parent, label="Deshacer",
                                      size=(130, 36))
        self.btn_deshacer.SetToolTip(
            "Retira del tablero las pistas que aplicó el asistente en la última\n"
            "corrida. Las pistas trazadas a mano no se tocan: el asistente sólo\n"
            "retira las que él mismo agregó, identificadas por su UUID."
        )
        self.btn_deshacer.Hide()

        self.btn_cancelar = wx.Button(parent, label="Cancelar", size=(100, 36))
        self.btn_cancelar.SetToolTip("Cerrar el asistente")

        sizer.Add(self.btn_enrutar, 0, wx.RIGHT, 8)
        sizer.Add(self.btn_deshacer, 0, wx.RIGHT, 8)
        sizer.AddStretchSpacer()
        sizer.Add(self.btn_cancelar, 0)
        return sizer

    def _crear_area_resultados(self, parent) -> wx.StaticBoxSizer:
        """Crea el área de texto para resultados y métricas."""
        caja = wx.StaticBox(parent, label="Resultados y Métricas")
        sizer = wx.StaticBoxSizer(caja, wx.VERTICAL)

        self.texto_resultados = wx.TextCtrl(
            parent,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2 | wx.HSCROLL,
            size=(-1, 200)
        )
        # Fuente monoespaciada para mejor legibilidad
        font_mono = wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL,
                            wx.FONTWEIGHT_NORMAL)
        self.texto_resultados.SetFont(font_mono)

        self.texto_resultados.SetValue(
            "Bienvenido al Asistente de Enrutamiento PCB\n"
            "─────────────────────────────────────────────\n"
            "1. Seleccione un archivo .kicad_pcb\n"
            "2. Configure los parámetros deseados\n"
            "3. Haga clic en 'Iniciar Enrutamiento'\n\n"
            "Dos modos de aplicar el resultado:\n"
            "• ARCHIVO (por defecto): se genera un derivado\n"
            "  _enrutado.kicad_pcb junto al original. Ni el\n"
            "  original ni el tablero abierto se tocan.\n"
            "• MEMORIA: las pistas se aplican sobre el tablero\n"
            "  abierto en KiCad, sin escribir a disco. Se usa\n"
            "  con archivos _enrutado, o con el original si se\n"
            "  marca 'Aplicar sobre el tablero abierto' —útil\n"
            "  para correr el DRC completo dentro del proyecto.\n"
            "  Guarde con Ctrl+S, o descarte con el botón\n"
            "  'Deshacer última aplicación'.\n"
        )

        sizer.Add(self.texto_resultados, 1, wx.EXPAND | wx.ALL, 4)
        return sizer

    # ── Conexión de eventos ──────────────────────────────────────────────────

    def _conectar_eventos(self):
        """Conecta los eventos de los controles a sus manejadores."""
        self._btn_abrir.Bind(wx.EVT_BUTTON, self._on_abrir_archivo)
        self.btn_enrutar.Bind(wx.EVT_BUTTON, self._on_iniciar_enrutamiento)
        self.btn_cancelar.Bind(wx.EVT_BUTTON, self._on_cancelar)
        self.btn_deshacer.Bind(wx.EVT_BUTTON, self._on_deshacer)
        self.check_ag.Bind(wx.EVT_CHECKBOX, self._on_toggle_ag)
        self.Bind(wx.EVT_CLOSE, self._on_cerrar)

    # ── Manejadores de eventos ───────────────────────────────────────────────

    def _on_toggle_ag(self, evento):
        """Habilita/deshabilita campos del AG según el checkbox."""
        habilitado = self.check_ag.GetValue()
        self.campo_generaciones.Enable(habilitado)
        self.campo_poblacion.Enable(habilitado)

    def _on_abrir_archivo(self, evento):
        """Abre un diálogo para seleccionar el archivo .kicad_pcb."""
        dialogo = wx.FileDialog(
            self,
            message="Seleccionar archivo PCB de KiCad 10",
            wildcard="KiCad PCB (*.kicad_pcb)|*.kicad_pcb|Todos los archivos (*.*)|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST
        )

        if dialogo.ShowModal() == wx.ID_OK:
            ruta = dialogo.GetPath()
            self.campo_archivo.SetValue(ruta)
            self._log(f"Archivo seleccionado: {os.path.basename(ruta)}")

        dialogo.Destroy()

    def _on_iniciar_enrutamiento(self, evento):
        """Inicia el proceso de enrutamiento en un hilo separado."""
        ruta = self.campo_archivo.GetValue().strip()

        if not ruta:
            wx.MessageBox(
                "Por favor seleccione un archivo .kicad_pcb primero.",
                "Archivo no seleccionado",
                wx.OK | wx.ICON_WARNING
            )
            return

        if not os.path.exists(ruta):
            wx.MessageBox(
                f"No se puede encontrar el archivo:\n{ruta}",
                "Archivo no encontrado",
                wx.OK | wx.ICON_ERROR
            )
            return

        if self._enrutando:
            wx.MessageBox("Ya hay un enrutamiento en progreso.", "En progreso",
                          wx.OK | wx.ICON_INFORMATION)
            return

        # Limpiar área de resultados
        self.texto_resultados.Clear()
        self._log("Iniciando proceso de enrutamiento...")
        self._log("=" * 50)

        # Detectar el modo de aplicación (en el hilo principal — la API de
        # pcbnew no es thread-safe, no debe tocarse desde el hilo de trabajo):
        #   Regla 1 (archivo): se genera el derivado _enrutado y no se toca el
        #     tablero en memoria.
        #   Regla 2 (memoria): se aplican las pistas sobre el tablero abierto,
        #     sin escribir a disco. Aplica cuando el tablero abierto es
        #     exactamente el archivo seleccionado Y, o bien es un derivado, o
        #     bien el usuario marcó la casilla para trabajar sobre el original.
        tablero = _tablero_abierto_coincide(ruta)
        es_derivado = _es_archivo_derivado(ruta)
        memoria_pedida = self.check_memoria.GetValue()
        modo_memoria = tablero is not None and (es_derivado or memoria_pedida)

        # Aplicar sobre el ORIGINAL merece una confirmación explícita: es el
        # archivo del proyecto, no una copia descartable.
        if modo_memoria and not es_derivado:
            global _CONFIRMACION_ORIGINAL_MOSTRADA
            if not _CONFIRMACION_ORIGINAL_MOSTRADA:
                respuesta = wx.MessageBox(
                    "Va a aplicar el enrutamiento sobre el ARCHIVO ORIGINAL "
                    "del proyecto, en memoria.\n\n"
                    "· El archivo en disco NO se modifica. Guarde con Ctrl+S "
                    "si el resultado le convence, o cierre sin guardar para "
                    "descartarlo.\n\n"
                    "· Si tiene cambios sin guardar en el tablero, el asistente "
                    "no los ve: lee el archivo desde disco. Puede duplicar "
                    "cobre en conexiones que ya trazó. Guarde antes de "
                    "continuar si es el caso.\n\n"
                    "· El botón «Deshacer última aplicación» retira las pistas "
                    "que agregue el asistente, sin tocar las suyas.\n\n"
                    "Este aviso se muestra una sola vez mientras KiCad siga "
                    "abierto.\n\n"
                    "¿Continuar?",
                    "Aplicar sobre el archivo original",
                    wx.YES_NO | wx.ICON_WARNING
                )
                if respuesta != wx.YES:
                    self._log("[Modo] Cancelado por el usuario.")
                    return
                _CONFIRMACION_ORIGINAL_MOSTRADA = True

        if modo_memoria:
            destino = "archivo ORIGINAL" if not es_derivado else "archivo derivado"
            self._log(f"[Modo] MEMORIA — {destino} abierto en KiCad.")
            self._log("       Las pistas se aplicarán sobre el tablero abierto;")
            self._log("       guarde con Ctrl+S cuando el resultado le convenza.")
            if not es_derivado:
                # Recordatorio en cada corrida: el modal sale una sola vez, pero
                # el riesgo de divergencia disco/memoria existe siempre.
                self._log("[Aviso] Si el tablero tiene cambios sin guardar, el")
                self._log("        asistente no los ve (lee desde disco) y puede")
                self._log("        duplicar cobre ya trazado a mano.")
        else:
            if memoria_pedida:
                self._log("[Modo] ARCHIVO — se pidió aplicar sobre el tablero, pero")
                self._log("       el archivo seleccionado no es el que KiCad tiene abierto.")
            elif es_derivado:
                self._log("[Modo] ARCHIVO — el archivo es derivado pero no coincide")
                self._log("       con el tablero abierto en KiCad.")
            else:
                self._log("[Modo] ARCHIVO — se generará el derivado _enrutado.kicad_pcb.")
            self._log("       El tablero abierto en KiCad no se modificará.")
        self._log("=" * 50)

        # Semilla del AG: vacío = aleatoria (corridas independientes)
        texto_semilla = self.campo_semilla.GetValue().strip()
        semilla_ag = None
        if texto_semilla:
            try:
                semilla_ag = int(texto_semilla)
            except ValueError:
                wx.MessageBox(
                    f"La semilla debe ser un número entero (recibido: "
                    f"{texto_semilla!r}).\n\nDéjela vacía para que cada corrida "
                    f"sea independiente.",
                    "Semilla inválida", wx.OK | wx.ICON_WARNING
                )
                return

        # Recopilar parámetros
        parametros = {
            'ruta_entrada': ruta,
            'modo_memoria': modo_memoria,
            'semilla_ag': semilla_ag,
            'intentos': self.campo_intentos.GetValue(),
            'ancho_pista': self.campo_ancho.GetValue(),
            'clearance': self.campo_clearance.GetValue(),
            'paso_cuadricula': self.campo_paso.GetValue(),
            'num_generaciones': self.campo_generaciones.GetValue(),
            'tamano_poblacion': self.campo_poblacion.GetValue(),
            'usar_ag': self.check_ag.GetValue(),
            'ejecutar_drc': self.check_drc.GetValue(),
        }

        # Deshabilitar controles durante el proceso
        self._establecer_controles_habilitados(False)
        self._enrutando = True
        self.barra_progreso.SetValue(0)
        self.lbl_porcentaje.SetLabel("0%")

        # Iniciar hilo de enrutamiento
        self._hilo_enrutamiento = threading.Thread(
            target=self._ejecutar_enrutamiento_hilo,
            args=(parametros,),
            daemon=True
        )
        self._hilo_enrutamiento.start()

    def _ejecutar_enrutamiento_hilo(self, parametros: dict):
        """
        Ejecuta el enrutamiento en un hilo separado para no bloquear la GUI.

        Redirige print() hacia el área de texto del diálogo.
        Actualiza la barra de progreso mediante wx.CallAfter.
        """
        # Redirigir print al área de texto
        self._redireccionador = RedireccionadorTexto(self.texto_resultados)
        self._redireccionador.activar()

        try:
            from escritor_pcb import enrutar_pcb

            def _actualizar_progreso(paso, total, mensaje=""):
                porcentaje = int(paso / total * 100)
                wx.CallAfter(self._actualizar_barra, porcentaje, mensaje)

            ruta_salida, reporte, segmentos_nuevos, hubo_conexiones = enrutar_pcb(
                ruta_entrada=parametros['ruta_entrada'],
                ancho_pista=parametros['ancho_pista'],
                clearance=parametros['clearance'],
                paso_cuadricula=parametros['paso_cuadricula'],
                tamano_poblacion=parametros['tamano_poblacion'],
                num_generaciones=parametros['num_generaciones'],
                usar_ag=parametros['usar_ag'],
                ejecutar_drc=parametros['ejecutar_drc'],
                semilla_ag=parametros['semilla_ag'],
                repeticiones=parametros['intentos'],
                # Regla 2: en modo memoria NO se escribe a disco — evita que
                # el asistente y KiCad compitan por el mismo archivo.
                escribir_archivo=not parametros['modo_memoria'],
                callback_progreso=_actualizar_progreso
            )

            # La aplicación en memoria (pcbnew) debe ocurrir en el hilo
            # principal — se hace dentro de _enrutamiento_completado.
            wx.CallAfter(
                self._enrutamiento_completado,
                ruta_salida, reporte, segmentos_nuevos,
                parametros, hubo_conexiones
            )

        except PermissionError as e:
            # Mensaje legible (ej. archivo de salida abierto en KiCad),
            # sin traceback crudo.
            wx.CallAfter(self._enrutamiento_fallido, str(e))

        except Exception as e:
            import traceback
            error_msg = f"Error durante el enrutamiento:\n{traceback.format_exc()}"
            wx.CallAfter(self._enrutamiento_fallido, error_msg)

        finally:
            if self._redireccionador:
                self._redireccionador.desactivar()

    def _actualizar_barra(self, porcentaje: int, mensaje: str):
        """Actualiza la barra de progreso (llamado desde hilo principal vía wx.CallAfter)."""
        self.barra_progreso.SetValue(min(porcentaje, 100))
        self.lbl_porcentaje.SetLabel(f"{porcentaje}%")
        if mensaje:
            self.SetTitle(f"Enrutando... {porcentaje}% — {mensaje}")

    def _enrutamiento_completado(self, ruta_salida: Optional[str], reporte: str,
                                 segmentos_nuevos: list,
                                 parametros: dict, hubo_conexiones: bool):
        """
        Maneja la finalización exitosa del enrutamiento.

        Corre en el hilo principal (vía wx.CallAfter), por lo que aquí es
        seguro tocar la API de pcbnew para la aplicación en memoria (Regla 2).
        """
        self._enrutando = False
        self._establecer_controles_habilitados(True)
        self.barra_progreso.SetValue(100)
        self.lbl_porcentaje.SetLabel("100%")
        self.SetTitle("Asistente de Enrutamiento PCB en KiCad 10")

        self._log("\n" + "=" * 50)
        self._log("ENRUTAMIENTO COMPLETADO")

        if not parametros['modo_memoria']:
            # ── Regla 1: modo ARCHIVO ────────────────────────────────────────
            self._log(f"[Archivo] Generado: {os.path.basename(ruta_salida)}")
            mensaje = (
                f"Enrutamiento completado.\n\n"
                f"{len(segmentos_nuevos)} segmentos nuevos generados.\n\n"
                f"Archivo generado:\n{ruta_salida}\n\n"
                f"Abra este archivo en KiCad para ver el resultado."
            )
            wx.MessageBox(mensaje, "Enrutamiento Completado",
                          wx.OK | wx.ICON_INFORMATION)
            return

        # ── Regla 2: modo MEMORIA ────────────────────────────────────────────
        ruta_clave = _clave_placa(parametros['ruta_entrada'])

        if not hubo_conexiones:
            registro = _UUIDS_ASISTENTE.get(ruta_clave, set())
            if not registro:
                # FALLA 2: placa completamente enrutada y sin registro en esta
                # sesión (caso típico: se guardó con Ctrl+S y KiCad se reinició).
                self._log("[Memoria] Placa ya enrutada; sin registro de esta sesión.")
                wx.MessageBox(
                    "La placa ya está completamente enrutada.\n\n"
                    "El registro de pistas generadas solo dura mientras KiCad "
                    "esté abierto. Como KiCad se reinició desde la última "
                    "corrida, el asistente no puede distinguir sus propias "
                    "pistas de las que trazó a mano, y por seguridad no "
                    "borra ninguna.\n\n"
                    "Para reenrutar: borre las pistas manualmente en KiCad, "
                    "o vuelva a partir del archivo original.",
                    "Placa ya enrutada",
                    wx.OK | wx.ICON_INFORMATION
                )
            else:
                self._log("[Memoria] Sin conexiones pendientes; tablero sin cambios.")
                wx.MessageBox(
                    "No hay conexiones pendientes de enrutar.\n"
                    "El tablero no fue modificado.",
                    "Ya enrutado",
                    wx.OK | wx.ICON_INFORMATION
                )
            return

        board = _tablero_abierto_coincide(parametros['ruta_entrada'])
        if board is None:
            # El tablero se cerró o cambió mientras corría el enrutamiento.
            self._log("[Memoria] El tablero abierto cambió durante el proceso —")
            self._log("          no se aplicó nada para no tocar la placa equivocada.")
            wx.MessageBox(
                "El tablero abierto en KiCad cambió mientras corría el "
                "enrutamiento, por lo que no se aplicó ningún cambio.\n\n"
                "Vuelva a abrir el archivo y ejecute el asistente de nuevo.",
                "Tablero no disponible",
                wx.OK | wx.ICON_WARNING
            )
            return

        try:
            n_aplicados = _aplicar_en_memoria(
                board, ruta_clave, segmentos_nuevos, parametros['ancho_pista']
            )
        except Exception as e:
            self._log(f"[Memoria] Error al aplicar sobre el tablero: {e}")
            wx.MessageBox(
                f"Error al aplicar las pistas sobre el tablero:\n\n{e}",
                "Error al aplicar",
                wx.OK | wx.ICON_ERROR
            )
            return

        self._log(f"[Memoria] {n_aplicados} segmentos aplicados al tablero abierto.")
        self._log("[Memoria] NO se escribió a disco — guarde con Ctrl+S en KiCad")
        self._log("          cuando el resultado le convenza.")

        # Mostrar la vía de escape: ahora sí hay algo que deshacer.
        self._ultima_ruta_memoria = parametros['ruta_entrada']
        self.btn_deshacer.Show()
        self.btn_deshacer.Enable(True)
        self.Layout()

        wx.MessageBox(
            f"Enrutamiento completado.\n\n"
            f"{n_aplicados} segmentos aplicados al tablero abierto en KiCad.\n\n"
            f"NO se escribió ningún archivo.\n\n"
            f"· Para conservarlo: Ctrl+S en KiCad.\n"
            f"· Para descartarlo: botón «Deshacer última aplicación» de esta "
            f"ventana, o cerrar el tablero sin guardar.",
            "Enrutamiento Completado",
            wx.OK | wx.ICON_INFORMATION
        )

    def _enrutamiento_fallido(self, mensaje_error: str):
        """Maneja un error durante el enrutamiento."""
        self._enrutando = False
        self._establecer_controles_habilitados(True)
        self.barra_progreso.SetValue(0)
        self.lbl_porcentaje.SetLabel("Error")
        self.SetTitle("Asistente de Enrutamiento PCB en KiCad 10")

        self._log("\n✗ ERROR en el enrutamiento")
        self._log(mensaje_error)

        wx.MessageBox(
            f"Error durante el enrutamiento:\n\n{mensaje_error[:500]}",
            "Error de Enrutamiento",
            wx.OK | wx.ICON_ERROR
        )

    def _on_deshacer(self, evento):
        """Retira del tablero las pistas de la última aplicación en memoria."""
        if not self._ultima_ruta_memoria:
            return

        board = _tablero_abierto_coincide(self._ultima_ruta_memoria)
        if board is None:
            wx.MessageBox(
                "La placa sobre la que se aplicó ya no está abierta en KiCad, "
                "o el tablero abierto es otro archivo.\n\n"
                "No se puede deshacer desde aquí. Si el tablero sigue abierto "
                "con esos cambios, ciérrelo sin guardar para descartarlos.",
                "Tablero no disponible", wx.OK | wx.ICON_WARNING)
            return

        try:
            n = _deshacer_aplicacion(board, _clave_placa(self._ultima_ruta_memoria))
        except Exception as e:
            self._log(f"[Deshacer] Error: {e}")
            wx.MessageBox(f"Error al deshacer:\n\n{e}",
                          "Error", wx.OK | wx.ICON_ERROR)
            return

        if n == 0:
            self._log("[Deshacer] No había pistas del asistente por retirar.")
            wx.MessageBox(
                "No hay pistas del asistente para retirar en esta placa.\n\n"
                "Puede que ya se hayan deshecho, o que KiCad se haya "
                "reiniciado desde la última aplicación (el registro sólo dura "
                "mientras KiCad siga abierto).",
                "Nada que deshacer", wx.OK | wx.ICON_INFORMATION)
        else:
            self._log(f"[Deshacer] {n} pistas del asistente retiradas del tablero.")
            self._log("[Deshacer] Las pistas trazadas a mano no se tocaron.")
            wx.MessageBox(
                f"{n} pistas del asistente retiradas del tablero.\n\n"
                f"Las pistas que trazó a mano no se tocaron.",
                "Aplicación deshecha", wx.OK | wx.ICON_INFORMATION)

        # Ya no queda nada que deshacer
        self._ultima_ruta_memoria = None
        self.btn_deshacer.Hide()
        self.Layout()

    def _on_cancelar(self, evento):
        """Cierra el diálogo (confirma si hay enrutamiento en progreso)."""
        if self._enrutando:
            respuesta = wx.MessageBox(
                "Hay un enrutamiento en progreso.\n¿Desea cerrar de todas formas?",
                "Enrutamiento en Progreso",
                wx.YES_NO | wx.ICON_QUESTION
            )
            if respuesta != wx.YES:
                return
        self.EndModal(wx.ID_CANCEL)

    def _on_cerrar(self, evento):
        """Maneja el cierre de la ventana."""
        self._on_cancelar(evento)

    # ── Utilidades ───────────────────────────────────────────────────────────

    def _log(self, mensaje: str):
        """Agrega un mensaje al área de texto (seguro desde cualquier hilo)."""
        wx.CallAfter(
            self.texto_resultados.AppendText,
            mensaje + '\n' if not mensaje.endswith('\n') else mensaje
        )

    def _establecer_controles_habilitados(self, habilitado: bool):
        """Habilita o deshabilita los controles de configuración."""
        controles = [
            self._btn_abrir, self.campo_ancho, self.campo_clearance,
            self.campo_paso, self.campo_generaciones, self.campo_poblacion,
            self.check_ag, self.check_drc, self.btn_enrutar
        ]
        for control in controles:
            control.Enable(habilitado)
