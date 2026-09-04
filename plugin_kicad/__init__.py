"""
plugin_kicad — Plugin KiCad para el Asistente de Enrutamiento PCB

KiCad descubre este paquete en scripting/plugins/ e importa __init__.py.
Este archivo agrega el directorio al sys.path, importa la clase del plugin
y llama a .register() — el mismo patron que usa KiCadRoutingTools.
"""

import os
import sys

_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

try:
    from action_plugin import AsistentePCBPlugin
    AsistentePCBPlugin().register()
except Exception as _e:
    import logging
    logging.getLogger("AsistentePCB").error(f"Error al registrar el plugin: {_e}")
