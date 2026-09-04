"""
action_plugin.py — Definicion de la clase del plugin para KiCad 10

Define AsistentePCBPlugin(pcbnew.ActionPlugin). El registro con KiCad
se realiza desde __init__.py (igual que KiCadRoutingTools) para evitar
que un import wx fallido bloquee el descubrimiento del plugin.
"""

import os
import sys
import pcbnew


# Directorio del plugin (contiene todos los modulos tras la instalacion)
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)


class AsistentePCBPlugin(pcbnew.ActionPlugin):

    def defaults(self):
        self.name = "Asistente Enrutamiento PCB (Tesis)"
        self.category = "Enrutamiento"
        self.description = (
            "Asistente de IA para enrutamiento de PCBs de una sola capa. "
            "Usa Algoritmo Genetico + A* para optimizar el enrutamiento. "
            "Proyecto de Tesis - KiCad 10."
        )
        self.show_toolbar_button = True

        self.icon_file_name = os.path.join(PLUGIN_DIR, "icono_asistente_64.png")
        # Mismo archivo para el tema oscuro: el icono ya tiene contraste
        # propio (naranja sobre fondo sólido), no necesita una variante.
        self.dark_icon_file_name = self.icon_file_name

    def Run(self):
        import wx
        try:
            self._ejecutar_asistente()
        except Exception as e:
            wx.MessageBox(
                f"Error al ejecutar el Asistente de Enrutamiento:\n\n{e}\n\n"
                f"Verifique que todos los modulos estan instalados correctamente.",
                "Error del Plugin",
                wx.OK | wx.ICON_ERROR
            )

    def _ejecutar_asistente(self):
        import wx
        from panel_asistente import PanelAsistente

        ventana_padre = None
        ventanas_top = wx.GetTopLevelWindows()
        if ventanas_top:
            ventana_padre = ventanas_top[0]

        tablero = pcbnew.GetBoard()
        ruta_tablero = ""
        if tablero is not None:
            ruta_tablero = tablero.GetFileName() or ""

        dialogo = PanelAsistente(ventana_padre, ruta_tablero_inicial=ruta_tablero)
        dialogo.ShowModal()
        dialogo.Destroy()

        pcbnew.Refresh()
