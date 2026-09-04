# Asistente de Inteligencia Artificial para la Optimización del Enrutamiento de PCBs de Una Sola Capa en KiCad

**Proyecto de Tesis** | KiCad 10

---

## Descripción

Este proyecto implementa un asistente de IA que automatiza el enrutamiento de pistas en PCBs (Printed Circuit Boards) de una sola capa (F.Cu) usando dos algoritmos:

1. **Algoritmo Genético (AG)** — Optimiza el *orden* en que se enrutan las conexiones para minimizar la longitud total y reducir cruces entre pistas.
2. **Algoritmo A\*** — Encuentra el camino más corto libre de obstáculos para cada conexión individual sobre una cuadrícula configurable (0.25mm por defecto).

Compatible exclusivamente con **KiCad 10** (formato de archivo versión 20260206). Las redes cubiertas por una zona de cobre (típicamente GND) se dan por conectadas a través del plano y no se enrutan con pistas individuales.

---

## Estructura del Proyecto

```
asistente_pcb_tesis/
├── lector_pcb.py           — Parser de archivos .kicad_pcb (KiCad 10)
├── enrutador_astar.py      — Algoritmo A* en Python puro, cuadrícula configurable
├── optimizador_genetico.py — Algoritmo Genético, cruce OX, mutación swap
├── analisis_zona.py        — Detecta si las pistas fragmentan el plano de cobre de
│                              la zona de masa (p. ej. GND), en Python puro y sin
│                              depender de pcbnew — funciona igual en la CLI headless
│                              que en el plugin
├── escritor_pcb.py         — Orquestador principal + escritura de salida
├── instalar_plugin.py      — Instala el plugin en KiCad 10
├── desinstalar_plugin.py   — Desinstala el plugin de KiCad 10
├── requirements.txt        — Dependencias (solo stdlib de Python)
├── README.md               — Este archivo
├── plugin_kicad/
│   ├── __init__.py             — Inicialización del paquete del plugin
│   ├── action_plugin.py        — ActionPlugin de KiCad (punto de entrada)
│   ├── panel_asistente.py      — GUI wxPython con controles de enrutamiento
│   └── icono_asistente_64.png  — Ícono del plugin en el menú de KiCad
└── casos_prueba/
    └── *.kicad_pcb          — PCBs de prueba (KiCad 10), sin enrutar y enrutadas
```

Ver también [Instrumentos de verificación y análisis](#instrumentos-de-verificación-y-análisis) — scripts adicionales en la raíz que no forman parte del asistente entregado.

---

## Requisitos

| Componente | Versión |
|-----------|---------|
| KiCad     | 10.0    |
| Python (uso como plugin) | El intérprete embebido de KiCad 10 (3.11 en la versión utilizada para el desarrollo) |
| Python (uso por línea de comandos) | Cualquier Python 3 reciente |
| Sistema operativo | Windows, macOS o Linux — los que soporta KiCad 10 |
| Dependencias | Solo biblioteca estándar de Python. La GUI usa wxPython, provisto por KiCad; solo hace falta instalarlo aparte para probar la GUI fuera de KiCad |

---

## Uso Rápido

### Desde la línea de comandos

```bash
# Enrutar un PCB con parámetros por defecto (orden MPS + A*; el AG se activa
# con --orden ag — la GUI del plugin, en cambio, lo trae activado por defecto)
python escritor_pcb.py casos_prueba/mi_pcb.kicad_pcb

# Con parámetros personalizados
python escritor_pcb.py mi_pcb.kicad_pcb --ancho 0.25 --clearance 0.15

# Con más generaciones del AG para mejor optimización
python escritor_pcb.py mi_pcb.kicad_pcb --generaciones 200 --poblacion 80

# Orden por MPS (Maximum Planar Subset) en vez del AG
python escritor_pcb.py mi_pcb.kicad_pcb --orden mps

# Best-of-N: corre el enrutado completo 3 veces y aplica solo el mejor
python escritor_pcb.py mi_pcb.kicad_pcb --intentos 3

# Ver todas las opciones
python escritor_pcb.py --help
```

Por defecto, el archivo de salida siempre es `<nombre>_enrutado.kicad_pcb` y se sobrescribe en cada corrida, para no acumular archivos mientras se iteran parámetros. `--con-timestamp` restaura el nombre con fecha/hora (`mi_pcb_enrutado_20260510_143022.kicad_pcb`); `--salida <ruta>` fija una ruta explícita.

### Como plugin en KiCad 10

```bash
# 1. Instalar el plugin
python instalar_plugin.py

# 2. Abrir KiCad 10
# 3. Abrir un PCB en Pcbnew
# 4. Tools → External Plugins → Asistente Enrutamiento PCB (Tesis)
```

El plugin admite dos modos de aplicar el resultado:

- **Modo archivo** — escribe `<nombre>_enrutado.kicad_pcb` en disco, sin tocar el tablero abierto. Es el modo por defecto cuando el archivo seleccionado no es el que está abierto en KiCad.
- **Modo memoria** — aplica los segmentos directamente sobre el tablero abierto en Pcbnew (sin escribir a disco), para poder revisar el resultado y guardarlo con Ctrl+S. Requiere siempre que el archivo seleccionado sea exactamente el que está abierto en KiCad; dado eso, se activa automáticamente si el archivo es un derivado (`*_enrutado.kicad_pcb`), o marcando la casilla "Aplicar sobre el tablero abierto" para trabajar directamente sobre el original. Incluye un botón "Deshacer última aplicación" que retira solo las pistas que agregó el asistente, sin tocar las trazadas a mano.

---

## Parámetros Configurables

| Parámetro | CLI | Defecto | Descripción |
|-----------|-----|---------|-------------|
| Ancho de pista | `--ancho` | 0.3 mm | Ancho de las pistas enrutadas |
| Ancho por red | `--ancho-red` | — | Ancho para una red específica, ej. `--ancho-red "GND=1.0"` (repetible) |
| Clearance nominal | `--clearance` | 0.2 mm | Separación mínima entre cobre |
| Clearance mínimo fabricable | `--clearance-minimo` | 0.127 mm | Piso al que se reintenta en clearance reducido; nunca se baja de aquí |
| Paso cuadrícula | `--paso` | 0.25 mm | Resolución del A* |
| Orden de enrutamiento | `--orden` | `mps` | `distancia` \| `mps` \| `ag` — quién decide el orden de las conexiones. La GUI del plugin usa AG por defecto (checkbox activado); la CLI usa MPS salvo que se pida `--orden ag` |
| Aptitud del AG | `--fitness` | `surrogate` | `surrogate` (A* grueso, fiel) \| `aproximada` (oclusión progresiva, rápida) — solo con `--orden ag` |
| Paso del surrogate | `--paso-surrogate` | 0.5 mm | Resolución de cuadrícula del fitness `surrogate` |
| Generaciones AG | `--generaciones` | 40 | Iteraciones del AG |
| Población AG | `--poblacion` | 24 | Individuos por generación |
| Intentos (best-of-N) | `--intentos` / `--repeticiones` | 1 | Corre el enrutado completo N veces con semillas distintas y aplica solo el mejor |
| Semilla | `--semilla` | Aleatoria (no fija) | Fija la semilla del AG para reproducir una corrida |
| Sin AG | `--sin-ag` | — | (Compatibilidad) Equivale a `--orden mps` |
| Sin DRC | `--sin-drc` | — | Deshabilitar verificación DRC básica |
| Salida | `--salida` | — | Ruta de archivo de salida explícita |
| Con timestamp | `--con-timestamp` | — | Agregar fecha/hora al nombre de salida en vez de sobrescribir |

---

## Arquitectura de Algoritmos

### Algoritmo A* (`enrutador_astar.py`)

```
Cuadrícula:  paso configurable (0.25mm por defecto)
Movimiento:  8 direcciones (H, V, 45°)
Heurística:  Chebyshev h(n) = max(|Δx|,|Δy|) + (√2-1)·min(|Δx|,|Δy|)
Obstáculos:  Pads + pistas previas + clearance
Giros:       Penalización configurable (COSTO_GIRO = 2.0)
```

Incluye rip-up and reroute acotado: una conexión que falla o que enruta con un desvío patológico (≥3.0× la distancia directa) puede disparar el rehecho completo de su red, dentro de un presupuesto de intentos limitado.

### Algoritmo Genético (`optimizador_genetico.py`)

```
Individuo:   Permutación de índices de conexiones
Fitness:     Secuencial y dependiente del orden — surrogate (A* grueso real)
             o aproximada (oclusión progresiva), nunca una suma que ignore
             el orden en que se enruta
Selección:   Torneo (k=3)
Cruce:       Order Crossover (OX)
Mutación:    Swap de dos posiciones (prob=0.15)
Elitismo:    El mejor individuo pasa directamente
Semillado:   La población inicial incluye el orden MPS y el de distancia
```

### Análisis de fragmentación del plano de cobre (`analisis_zona.py`)

Las redes con zona (GND, típicamente) no se enrutan con pistas — se asumen conectadas a través del vertido. Pero el plano solo conecta si sigue siendo una sola pieza: las pistas de las demás redes, con su clearance, pueden partirlo en regiones aisladas. Este módulo rasteriza la placa y cuenta componentes conexas del cobre que se rellenaría, para detectar esa fragmentación sin depender de `pcbnew` — corre igual en la CLI headless que dentro del plugin, y se usa como criterio de selección en el best-of-N.

### Flujo completo (`escritor_pcb.py`)

```
Archivo PCB
    │
    ├─→ lector_pcb.leer_pcb()
    │       └─→ DatosPCB (pads, redes, zonas de cobre, límites)
    │
    ├─→ obtener_conexiones_a_enrutar() [MST por red, excluye redes con zona]
    │
    ├─→ optimizar_orden_enrutamiento() [AG, si --orden ag]
    │       └─→ Conexiones ordenadas óptimamente
    │
    ├─→ enrutar_todos() [A* por conexión, con rip-up and reroute]
    │       └─→ Lista de segmentos {x1,y1,x2,y2,red,ancho}
    │
    ├─→ analizar_fragmentacion_zona() [por intento, si hay zonas]
    │
    ├─→ drc_basico() [Verificación DRC]
    │
    └─→ escribir_pcb_enrutado() → <nombre>_enrutado.kicad_pcb
```

Con `--intentos N > 1`, este flujo corre completo N veces con semillas distintas y solo se aplica/escribe el mejor resultado, según un criterio estrictamente lexicográfico (fallidas → redes incompletas → fragmentos del plano → clearance reducido → rutas patológicas → longitud).

---

## Compatibilidad con KiCad 10

Reglas críticas aplicadas en la escritura del archivo:

- ✅ Redes por **NOMBRE**: `(net "GND")` — sin ID numérico
- ✅ Capa entre comillas: `(layer "F.Cu")`
- ✅ UUID único por segmento: `(uuid "xxxxxxxx-...")`
- ✅ Sin punto y coma como comentarios dentro del archivo
- ✅ Formato S-expresión idéntico al que genera KiCad 10
- ✅ El cobre existente (enrutado manual del usuario) nunca se borra, solo se preserva como obstáculo y se completa alrededor

---

## Prueba del módulo individual

```bash
# Probar solo el lector
python lector_pcb.py casos_prueba/mi_pcb.kicad_pcb

# Probar solo el AG
python optimizador_genetico.py

# Probar solo el A*
python enrutador_astar.py

# Probar solo el análisis de fragmentación de zona
python analisis_zona.py casos_prueba/mi_pcb_enrutado.kicad_pcb

# Flujo completo
python escritor_pcb.py casos_prueba/mi_pcb.kicad_pcb
```

---

## Instrumentos de verificación y análisis

Scripts adicionales en la raíz del proyecto, usados durante el desarrollo y la validación para caracterizar el comportamiento del asistente. No forman parte del asistente entregado (ni de la GUI ni de `MODULOS_A_COPIAR` en el instalador) — son herramientas de diagnóstico de línea de comandos.

**Verificación del resultado**

- `verificar_conectividad.py` — comprueba que los pads de cada red queden unidos por el cobre resultante.
- `verificar_pads.py` — comprueba que los extremos de las pistas coincidan con los pads que deberían tocar.
- `verificar_enrutado.py` — compara el archivo enrutado contra el original.

**Análisis del Algoritmo Genético**

- `validar_surrogate.py` — evalúa la fidelidad del evaluador rápido de aptitud (`surrogate`) frente al enrutado real.
- `medir_sesgo_surrogate.py` — cuantifica la desviación del surrogate respecto al enrutado real, permutación por permutación.
- `test_regresion_aptitud.py` — previene la reaparición de un fallo detectado en desarrollo (una aptitud que no dependía del orden de las conexiones).

**Inspección**

- `analizar_angulos.py`, `analizar_rutas.py`, `ver_segmentos.py`, `comparar_final.py`.

---

## Instalación y desinstalación del plugin

```bash
# Instalar (detecta automáticamente el directorio de plugins de KiCad 10
# según el sistema operativo — Windows, macOS o Linux)
python instalar_plugin.py

# Verificar qué se instalaría (sin copiar)
python instalar_plugin.py --ver

# Forzar un directorio de destino distinto al detectado automáticamente
python instalar_plugin.py --ruta "/ruta/a/scripting/plugins"

# Desinstalar
python desinstalar_plugin.py

# Desinstalar sin confirmación
python desinstalar_plugin.py --forzar
```

La autodetección prueba, en orden, las ubicaciones habituales de `scripting/plugins` de KiCad 10 para el sistema operativo detectado, y usa la primera que exista (o la primera que tenga contenido, si hay varias ya creadas). Si ninguna existe, crea la primera de la lista e informa la ruta elegida. `--ruta` permite indicarla manualmente cuando la instalación no está en una ubicación estándar.

---

## Autores

- Proyecto de Tesis — Julio Fuentes
- Basado en el análisis del formato KiCad 10 (version 2026)
- Referencia técnica: [KiCadRoutingTools](https://github.com/drandyhaas/KiCadRoutingTools)
