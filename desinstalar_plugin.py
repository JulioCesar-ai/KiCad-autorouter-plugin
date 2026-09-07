"""
desinstalar_plugin.py — Desinstalador del plugin KiCad para el Asistente PCB Tesis

Elimina el plugin del directorio de plugins de KiCad 10.

Uso:
    python desinstalar_plugin.py
    python desinstalar_plugin.py --forzar          (sin confirmación)
    python desinstalar_plugin.py --ruta RUTA       (forzar el directorio a usar)
"""

import os
import sys
import shutil
import argparse
from pathlib import Path

# Misma detección que instalar_plugin.py — no se duplica aquí, se importa,
# para que ambos scripts coincidan siempre en qué directorio es "el" de
# KiCad en esta máquina.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from instalar_plugin import detectar_ruta_plugins_kicad


NOMBRE_PLUGIN = "asistente_pcb_KiCad"


def desinstalar(ruta_plugins: Path, sin_confirmacion: bool = False) -> bool:
    """
    Elimina el plugin instalado del directorio de KiCad.

    Args:
        ruta_plugins: directorio de plugins de KiCad (de
            `detectar_ruta_plugins_kicad`, o forzado con `--ruta`).
        sin_confirmacion: Si True, no pide confirmación al usuario

    Returns:
        True si la desinstalación fue exitosa o el plugin no estaba instalado
    """
    dir_plugin = ruta_plugins / NOMBRE_PLUGIN

    print(f"\n{'='*60}")
    print(f"  Desinstalador — Asistente Enrutamiento PCB (KiCad)")
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
    parser.add_argument(
        '--ruta', default=None,
        help='Directorio de plugins de KiCad a usar, si la autodetección no '
             'aplica a esta instalación'
    )
    args = parser.parse_args()

    ruta_plugins, _ = detectar_ruta_plugins_kicad(args.ruta)
    origen = "indicada con --ruta" if args.ruta else "autodetectada"
    print(f"[Detección] Directorio de plugins de KiCad ({origen}):")
    print(f"  {ruta_plugins}")

    exito = desinstalar(ruta_plugins, sin_confirmacion=args.forzar)
    sys.exit(0 if exito else 1)


if __name__ == "__main__":
    main()
