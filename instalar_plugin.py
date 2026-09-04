"""
instalar_plugin.py — Instalador del plugin KiCad para el Asistente PCB Tesis

Copia el plugin y todos los módulos necesarios al directorio de plugins de KiCad 10.

Destino de instalación:
    Se detecta automáticamente según el sistema operativo, probando las
    ubicaciones habituales de KiCad 10 (ver `candidatos_ruta_plugins`) y
    usando la primera que exista. Si ninguna existe, se crea la primera de
    la lista. Puede indicarse manualmente con `--ruta`.

El plugin quedará disponible en KiCad 10:
    Tools → External Plugins → Asistente Enrutamiento PCB (Tesis)

Uso:
    python instalar_plugin.py
    python instalar_plugin.py --ver             (solo mostrar qué se instalaría)
    python instalar_plugin.py --ruta RUTA       (forzar el directorio de destino)
"""

import os
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

import shutil
import platform
import argparse
from pathlib import Path
from typing import List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────────────────────────

NOMBRE_PLUGIN = "asistente_pcb_tesis"

# Módulos del proyecto que deben copiarse dentro del plugin
MODULOS_A_COPIAR = [
    "lector_pcb.py",
    "enrutador_astar.py",
    "optimizador_genetico.py",
    "escritor_pcb.py",
    "analisis_zona.py",
]

# Directorio del plugin fuente
DIR_PLUGIN_FUENTE = Path(__file__).parent / "plugin_kicad"
DIR_PROYECTO = Path(__file__).parent


# ─────────────────────────────────────────────────────────────────────────────
# Detección del directorio de plugins de KiCad
# ─────────────────────────────────────────────────────────────────────────────

def candidatos_ruta_plugins() -> List[Path]:
    """
    Ubicaciones habituales del directorio de plugins de KiCad 10, en orden de
    preferencia, según el sistema operativo detectado con `platform.system()`.

    No se verifica existencia acá — eso lo hace `detectar_ruta_plugins_kicad`,
    que recorre esta lista y devuelve la primera que exista.
    """
    home = Path.home()
    sistema = platform.system()

    if sistema == 'Windows':
        appdata = Path(os.environ.get('APPDATA', str(home / "AppData" / "Roaming")))
        return [
            home / "Documents" / "KiCad" / "10.0" / "scripting" / "plugins",
            home / "OneDrive" / "Documents" / "KiCad" / "10.0" / "scripting" / "plugins",
            home / "OneDrive" / "Documentos" / "KiCad" / "10.0" / "scripting" / "plugins",
            home / "Documentos" / "KiCad" / "10.0" / "scripting" / "plugins",
            appdata / "kicad" / "10.0" / "scripting" / "plugins",
        ]
    elif sistema == 'Darwin':
        return [
            home / "Documents" / "KiCad" / "10.0" / "scripting" / "plugins",
            home / "Library" / "Application Support" / "kicad" / "10.0" / "scripting" / "plugins",
        ]
    else:
        # Linux y cualquier otro *nix no contemplado explícitamente: se
        # prueban las dos rutas de KiCad en Linux antes de rendirse.
        return [
            home / ".local" / "share" / "kicad" / "10.0" / "scripting" / "plugins",
            home / ".kicad" / "10.0" / "scripting" / "plugins",
        ]


def detectar_ruta_plugins_kicad(ruta_manual: Optional[str] = None) -> Tuple[Path, bool]:
    """
    Determina el directorio de plugins de KiCad a usar.

    Args:
        ruta_manual: si se indica (viene de `--ruta`), se usa tal cual sin
            probar los candidatos automáticos — el usuario sabe mejor que
            nadie dónde está su instalación cuando ninguna ubicación
            habitual aplica.

    Returns:
        (ruta, existia): `existia` es False cuando ninguna ubicación
        candidata existía y se está por crear la primera de la lista —
        distinción que `instalar()` usa para avisar de la elección en vez
        de crearla en silencio.

    Entre los candidatos que EXISTEN, se prefiere el primero que además
    tenga contenido (algún plugin ya instalado) sobre uno vacío, antes de
    caer al orden declarado en `candidatos_ruta_plugins`. La detección
    "primera que exista" no alcanza: en Windows es común que quede una
    carpeta `Documents\\KiCad\\...` vacía —huérfana de una instalación
    vieja o de una redirección de OneDrive que ya no aplica— mientras la
    instalación real, con el plugin efectivamente cargado por KiCad, vive
    en otra de las rutas candidatas. Elegir la vacía por ir primera en la
    lista instalaría en un directorio que KiCad no lee. Caso real: en la
    máquina de desarrollo, `Documents\\KiCad\\...\\plugins` existe y está
    vacía; la instalación en uso está en
    `OneDrive\\Documentos\\KiCad\\...\\plugins`, tercera en el orden.
    """
    if ruta_manual:
        ruta = Path(ruta_manual).expanduser()
        return ruta, ruta.exists()

    candidatos = candidatos_ruta_plugins()
    existentes = [c for c in candidatos if c.exists()]
    for candidato in existentes:
        if any(candidato.iterdir()):
            return candidato, True
    if existentes:
        return existentes[0], True

    return candidatos[0], False


# ─────────────────────────────────────────────────────────────────────────────
# Lógica de instalación
# ─────────────────────────────────────────────────────────────────────────────

def verificar_entorno() -> bool:
    """
    Verifica que el entorno es compatible para la instalación.

    Comprueba:
      - El directorio fuente del plugin existe
      - Los módulos del proyecto existen
      - El directorio de plugins de KiCad existe (o se puede crear)

    Returns:
        True si todo está correcto, False si hay algún problema
    """
    ok = True

    print("[Verificar] Comprobando entorno...")

    if not DIR_PLUGIN_FUENTE.exists():
        print(f"  ERROR: No se encuentra la carpeta del plugin: {DIR_PLUGIN_FUENTE}")
        ok = False
    else:
        print(f"  OK: Carpeta plugin encontrada: {DIR_PLUGIN_FUENTE}")

    for modulo in MODULOS_A_COPIAR:
        ruta_mod = DIR_PROYECTO / modulo
        if not ruta_mod.exists():
            print(f"  ADVERTENCIA: Módulo no encontrado: {modulo}")
        else:
            print(f"  OK: Módulo encontrado: {modulo}")

    return ok


def instalar(ruta_plugins: Path, existia: bool,
            solo_verificar: bool = False) -> bool:
    """
    Realiza la instalación del plugin en KiCad 10.

    Pasos:
      1. Crear directorio de destino si no existe
      2. Copiar carpeta plugin_kicad/ al destino
      3. Copiar módulos Python (lector, enrutador, optimizador, escritor)
         dentro de la carpeta instalada para que sea auto-contenida

    Args:
        ruta_plugins: directorio de plugins de KiCad (de
            `detectar_ruta_plugins_kicad`, o forzado con `--ruta`).
        existia: si False, ninguna ubicación habitual existía y `ruta_plugins`
            es la elegida por defecto para crear — se avisa explícitamente
            en vez de crearla en silencio.
        solo_verificar: Si True, solo muestra qué se haría sin copiar nada

    Returns:
        True si la instalación fue exitosa
    """
    if not verificar_entorno():
        print("\nVerificación fallida. Corrija los errores antes de instalar.")
        return False

    if not existia:
        print(f"[Detección] Ninguna ubicación habitual de KiCad 10 fue "
              f"encontrada; se usará y creará:\n  {ruta_plugins}")

    # Directorio de destino
    dir_destino = ruta_plugins / NOMBRE_PLUGIN

    print(f"\n{'─'*60}")
    print(f"Instalación del Plugin:")
    print(f"  Origen:  {DIR_PLUGIN_FUENTE}")
    print(f"  Destino: {dir_destino}")
    print(f"{'─'*60}\n")

    if solo_verificar:
        print("[DRY-RUN] Los siguientes archivos serían copiados:")
        print(f"  {DIR_PLUGIN_FUENTE} → {dir_destino}/")
        for mod in MODULOS_A_COPIAR:
            print(f"  {DIR_PROYECTO / mod} → {dir_destino / mod}")
        print("\nEjecute sin --ver para instalar realmente.")
        return True

    try:
        # Crear directorio de destino si no existe (el aviso de que ninguna
        # ubicación habitual existía, si aplica, ya se mostró más arriba)
        ruta_plugins.mkdir(parents=True, exist_ok=True)
        dir_destino.mkdir(parents=True, exist_ok=True)

        # Limpiar __pycache__ del destino para evitar bytecode obsoleto
        cache_destino = dir_destino / "__pycache__"
        if cache_destino.exists():
            shutil.rmtree(cache_destino, ignore_errors=True)
            print("  Bytecode __pycache__ eliminado")

        # Copiar archivos de plugin_kicad/ uno por uno (evita lock de OneDrive)
        print(f"Copiando archivos del plugin:")
        ignorar = shutil.ignore_patterns('__pycache__', '*.pyc', '.git')
        for archivo in DIR_PLUGIN_FUENTE.iterdir():
            if archivo.suffix == '.pyc' or archivo.name == '__pycache__':
                continue
            destino_archivo = dir_destino / archivo.name
            shutil.copy2(archivo, destino_archivo)
            print(f"  ✓ {archivo.name}")

        # Copiar módulos Python dentro del plugin (para auto-contención)
        print("Copiando módulos del proyecto al plugin:")
        for nombre_modulo in MODULOS_A_COPIAR:
            origen_mod = DIR_PROYECTO / nombre_modulo
            destino_mod = dir_destino / nombre_modulo
            if origen_mod.exists():
                shutil.copy2(origen_mod, destino_mod)
                print(f"  ✓ {nombre_modulo}")
            else:
                print(f"  ✗ {nombre_modulo} — no encontrado, se omite")

        print(f"\n{'='*60}")
        print(f"✓ Plugin instalado exitosamente en:")
        print(f"  {dir_destino}")
        print(f"{'='*60}")
        print()
        print("Para usar el plugin en KiCad 10:")
        print("  1. Abra KiCad 10")
        print("  2. Abra un diseño PCB en Pcbnew")
        print("  3. Vaya a: Tools → External Plugins")
        print("  4. Haga clic en: Asistente Enrutamiento PCB (Tesis)")
        print()
        print("NOTA: Si KiCad ya estaba abierto, reinícielo para cargar el plugin.")

        return True

    except PermissionError:
        print(f"\nERROR: Permiso denegado al copiar archivos.")
        print(f"Intente ejecutar como Administrador o verifique los permisos de:")
        print(f"  {ruta_plugins}")
        return False

    except Exception as e:
        print(f"\nERROR durante la instalación: {e}")
        import traceback
        traceback.print_exc()
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Punto de entrada
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=f"Instalador del plugin KiCad '{NOMBRE_PLUGIN}'",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python instalar_plugin.py                 Instala el plugin (ruta autodetectada)
  python instalar_plugin.py --ver           Solo muestra qué se instalaría
  python instalar_plugin.py --ruta RUTA     Fuerza el directorio de destino
"""
    )
    parser.add_argument(
        '--ver', action='store_true',
        help='Solo verificar qué se instalaría (sin copiar)'
    )
    parser.add_argument(
        '--ruta', default=None,
        help='Directorio de plugins de KiCad a usar, si la autodetección no '
             'aplica a esta instalación (ej.: una ruta de red o un perfil '
             'de KiCad no estándar)'
    )
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  Instalador — Asistente Enrutamiento PCB (Tesis)")
    print(f"  Plataforma: {platform.system()} {platform.version()[:20]}")
    print(f"{'='*60}\n")

    ruta_plugins, existia = detectar_ruta_plugins_kicad(args.ruta)
    origen = "indicada con --ruta" if args.ruta else "autodetectada"
    print(f"[Detección] Directorio de plugins de KiCad ({origen}):")
    print(f"  {ruta_plugins}\n")

    exito = instalar(ruta_plugins, existia, solo_verificar=args.ver)
    sys.exit(0 if exito else 1)


if __name__ == "__main__":
    main()
