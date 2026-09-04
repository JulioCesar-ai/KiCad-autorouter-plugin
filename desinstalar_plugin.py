"""
desinstalar_plugin.py — Desinstalador del plugin KiCad para el Asistente PCB Tesis

Elimina el plugin del directorio de plugins de KiCad 10.

Uso:
    python desinstalar_plugin.py
    python desinstalar_plugin.py --forzar   (sin confirmación)
"""

import os
import sys
import shutil
import argparse
from pathlib import Path


NOMBRE_PLUGIN = "asistente_pcb_tesis"

RUTA_PLUGINS_KICAD = Path(
    r"C:\Users\fuent\OneDrive\Documentos\KiCad\10.0\scripting\plugins"
)


def desinstalar(sin_confirmacion: bool = False) -> bool:
    """
    Elimina el plugin instalado del directorio de KiCad.

    Args:
        sin_confirmacion: Si True, no pide confirmación al usuario

    Returns:
        True si la desinstalación fue exitosa o el plugin no estaba instalado
    """
    dir_plugin = RUTA_PLUGINS_KICAD / NOMBRE_PLUGIN

    print(f"\n{'='*60}")
    print(f"  Desinstalador — Asistente Enrutamiento PCB (Tesis)")
    print(f"{'='*60}\n")

    if not dir_plugin.exists():
        print(f"El plugin no está instalado en:")
        print(f"  {dir_plugin}")
        print("\nNada que desinstalar.")
        return True

    print(f"Se eliminará el directorio:")
    print(f"  {dir_plugin}")
    print(f"\nContenido ({_contar_archivos(dir_plugin)} archivos):")
    for archivo in sorted(dir_plugin.iterdir()):
        print(f"  - {archivo.name}")

    if not sin_confirmacion:
        respuesta = input("\n¿Confirma la desinstalación? (s/N): ").strip().lower()
        if respuesta not in ('s', 'si', 'sí', 'yes', 'y'):
            print("\nDesinstalación cancelada.")
            return False

    try:
        shutil.rmtree(dir_plugin)
        print(f"\n✓ Plugin desinstalado correctamente.")
        print(f"\nEl plugin ya no aparecerá en:")
        print(f"  KiCad → Tools → External Plugins")
        print(f"\nNOTA: Reinicie KiCad si estaba abierto.")
        return True

    except PermissionError:
        print(f"\nERROR: Permiso denegado al eliminar {dir_plugin}")
        print("Intente ejecutar como Administrador.")
        return False

    except Exception as e:
        print(f"\nERROR: {e}")
        return False


def _contar_archivos(directorio: Path) -> int:
    """Cuenta el número de archivos en un directorio (recursivo)."""
    return sum(1 for _ in directorio.rglob('*') if _.is_file())


def main():
    parser = argparse.ArgumentParser(
        description=f"Desinstalador del plugin KiCad '{NOMBRE_PLUGIN}'"
    )
    parser.add_argument(
        '--forzar', '-f', action='store_true',
        help='Desinstalar sin pedir confirmación'
    )
    args = parser.parse_args()

    exito = desinstalar(sin_confirmacion=args.forzar)
    sys.exit(0 if exito else 1)


if __name__ == "__main__":
    main()
