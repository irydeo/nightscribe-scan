# NightScribe — guía de usuario

**Español** · [🇬🇧 English → USER_GUIDE.md](USER_GUIDE.md)

Guía corta de `nightscribe-scan.py`: qué hace, por qué importa cada función y
cómo usarlas. La sección de *Profundización* (§9) explica la geometría y el
porqué del diseño; las herramientas de post-proceso están documentadas en la
cabecera de cada fichero.

---

## 1. Qué es NightScribe

Es un script de captura para espectroheliógrafo (SHG) de barrido rápido. Se
ejecuta dentro de la consola de SharpCap y hace todo el trabajo sobre el stream
de vídeo en vivo (sin ficheros temporales). Dos modos:

- `ENABLE_MOSAIC = False` → **un solo barrido**.
- `ENABLE_MOSAIC = True` → **mosaico multífila** con centrado en Dec por cuerdas.

NightScribe es un desarrollo nuevo, basado en el proyecto **SHGScan** de Patrick
Hsieh (FlankerOneTwo): <https://github.com/FlankerOneTwo/SHGScan>. Mantiene la
arquitectura dirigida por eventos y añade autocalibración, medida de la
geometría, controles de integridad y registros de sesión.

---

## 2. Puesta en marcha rápida

**Requisitos**

- SharpCap con la cámara y el monte conectados.
- Monte ASCOM que reporte RA/Dec y acepte `MoveAxis`.
- Carpeta de captura con espacio libre (el script lo comprueba).

**Ejecutar**

1. Copia el contenido de `nightscribe-scan.py` en la consola de scripting de
   SharpCap y ejecútalo. (O usa el panel de control, ver 2.1.)
2. La configuración ya **no se toca en el script**: vive en
   `nightscribe-scan.cfg` (ver 2.1). Si no existe, se usan los defectos.
3. Lee el banner de arranque. Lo importante:

   ```
   Config: loaded ...\nightscribe-scan.cfg (62 value(s)).
   Solar diameter: 1934.8" (ephemeris)  Expected disk: 3349 px
   Scan-rate calibration: ON (self-calibrates X sampling)
   Mosaic: slit=7.0mm  image scale=...  strip=... step=... rows=...
   ```

4. Al terminar, el script deja `scan_log.csv`, `scan_stats.jsonl` y
   `session_meta.json` junto a los SER.

### 2.1 Configuración externa y panel de control

**Fichero de configuración.** Todo lo ajustable está en `nightscribe-scan.cfg`
(INI, en la misma carpeta), editable a mano con cualquier editor de texto. El
proyecto incluye un `nightscribe-scan.cfg` **listo para usar**; solo hay que
fijar unos pocos valores, el resto tiene defecto:

```ini
[optics]
# Effective focal length at the sensor (mm). Sets the scan rate.
focal_length = 714
# Camera pixel size (um).
pixel_size = 2

# Solo para mosaico de disco completo:
[mosaic]
enable_mosaic = false
mosaic_slit_length_mm = 7
mosaic_scans_per_row = 5
```

La lista completa de ajustes (con sus defectos) está en
`docs/nightscribe-scan.cfg.reference`, y `docs/nightscribe-scan.cfg.example` es
una plantilla mínima por si quieres empezar de cero. Las claves que falten usan
el defecto; las líneas con `#` se ignoran. El banner dice de dónde se cargó.
También puedes fijar la ruta con la variable de entorno `NIGHTSCRIBE_CONFIG`.

**Panel de control (HTML).** Funciona en **dos modos**:

- **Con servidor** (recomendado para capturar): ejecuta `nightscribe-scan.py`
  con `START_UI = True` (por defecto) en la consola de scripting de SharpCap.
  Levanta un servidor local en `http://127.0.0.1:8765/`; ábrelo en el navegador.
  Aquí **Iniciar/Detener** están activos. Con `START_UI = False` el script
  captura directamente y no arranca ningún panel.
- **Como fichero**: abre `nightscribe-ui.html` directamente en Chrome/Edge
  (esquema embebido). Puedes ver y editar toda la configuración y los perfiles,
  abriendo/guardando `nightscribe-scan.cfg` y `nightscribe-profiles.json` con el
  botón correspondiente. **Iniciar/Detener quedan deshabilitados** (eso necesita
  SharpCap).

El panel es una tarjeta compacta (no ocupa toda la pantalla) con:

- **Sesión** (arriba): modo (un barrido/mosaico), tomas por fila, repeticiones y
  padding, a la vista.
- **Perfiles**: crea (pidiendo **datos del telescopio**: nombre, descripción,
  focal, apertura, cámara y notas), aplica (**Apply & save** escribe el `.cfg`
  en un paso), duplica, renombra, borra, importa y exporta. El perfil activo se
  marca; el JSON es v2 con bloques `meta` (telescopio) y `config`.
- **Configuración**: todos los campos del esquema, con su ayuda; «Save config»
  escribe `nightscribe-scan.cfg`.
- **Estado** y **conexión** (servidor o modo fichero).

El esquema embebido se regenera desde el script con `python3 tools/build_ui.py`.

---

## 3. Flujo de una sesión

```
encender cámara + monte → ajustar ROI y exposición base
        │
        ▼
NightScribe: alineado → centrado en Dec → auto-exposición
        │                    │
        │                    └─ mide extensión de la rendija (S_Y)
        ▼
mide FPS real → autocalibra la velocidad de barrido
        │
        ▼
barridos etiquetados rNN_sNN  (+ reintento si falla)
        │
        ▼
SER + scan_log.csv + scan_stats.jsonl + session_meta.json
```

En modo mosaico, además: calcula el plan de filas, avisa si no cubre el disco,
y recentra Dec entre barridos de una misma fila.

---

## 4. Funcionalidades, impacto y cómo usarlas

### 4.1 Autocalibración de la velocidad

- **Qué hace**: cruza el disco, mide el tiempo de cruce y ajusta el
  multiplicador del barrido para que el cielo se mueva exactamente
  `PIXEL_SCALE` por fotograma.
- **Impacto**: el muestreo en X es 1:1 aunque `FOCAL_LENGTH` o el FPS reportado
  no sean exactos. Es lo que evita el recon estirado y el apilado blando.
- **Cómo**: `CALIBRATE_SCAN_RATE = True` (por defecto). Mira en el banner
  `Scan calibration` y en el log `actual_xy` (debe quedar ≈1.000).

### 4.2 Diámetro solar por efeméride

- **Qué hace**: calcula el diámetro aparente del Sol a partir de la fecha
  (precisión ~0.01 %) en vez de usar un valor fijo.
- **Impacto**: la escala deja de arrastrar el ±3 % anual.
- **Cómo**: `USE_EPHEMERIS = True`. El banner indica la fuente
  (`ephemeris` / `override` / `fallback`). Para forzar un valor:
  `EPHEMERIS_DIAMETER_OVERRIDE`.

### 4.3 Escala del eje de la rendija medida en vivo

- **Qué hace**: con el disco centrado, mide la extensión iluminada a lo largo
  de la rendija y deduce `S_Y = diámetro / extensión_px`; de ahí actualiza el
  factor de escala que usa el plan del mosaico.
- **Impacto**: el paso de fila queda en las mismas unidades que el barrido, así
  que las filas encajan sin medir el factor a mano.
- **Cómo**: `MEASURE_SLIT_EXTENT = True`. Mira `effective slit` en el plan y
  `slit` en `session_meta.json`. Si no se pudo medir, avisa y usa el valor
  configurado.

### 4.4 Integridad de la captura

- **Guarda de disco**: antes de cada barrido estima el tamaño y comprueba el
  espacio (`CAPTURE_MIN_FREE_GB`). Aviso `CAPTURE SPACE` si no cabe.
- **Vigilancia del barrido**: si SharpCap deja de escribir durante
  `CAPTURE_STALL_SECONDS` o `cam.Capturing` cae, aborta con `SCAN PROBLEM` en
  vez de dejar un SER vacío.
- **Frames cortos**: compara lo escrito con `scan_duration * fps`; si falta más
  de `FRAMES_SHORT_TOL`, avisa `FRAMES SHORT` y lo registra.
- **Abortable**: Ctrl+C detiene el monte al instante.
- **Reintento**: un barrido fallido se repite `SCAN_RETRIES` veces realineando
  el limbo; el intento malo queda con su `problem`.

### 4.5 Consistencia y corrección de Dec

- **Consistencia**: avisa si `actual_xy` o la extensión de la rendija se desvían
  más de `CONSISTENCY_TOL_XY` / `CONSISTENCY_TOL_EXTENT` (3 %) entre barridos.
- **`cos(Dec)`**: `COS_DEC_CORRECTION = True` aplica la corrección en ambos
  modos, así la escala en X es la misma a cualquier declinación.
- **Dec estable**: en mosaico, recentra Dec entre barridos de la misma fila si
  se desvía más de `DEC_DRIFT_TOL_ARCSEC`.

### 4.6 Trazabilidad

- `scan_log.csv`: una fila por barrido (reescrito tras cada uno).
- `scan_stats.jsonl`: las mismas métricas por barrido, en JSON.
- `session_meta.json`: configuración efectiva, calibración y geometría de la
  rendija, más el plan del mosaico.

---

## 5. Criterios de aceptación

| Comprobación | Criterio |
| --- | --- |
| `actual_xy` en `scan_log.csv` | 0.97–1.03 |
| FPS medido vs usado | < 2 % |
| `frames_short_pct` | ≈ 0 |
| Avisos `CONSISTENCY` | ninguno |
| Dispersión de Dec por fila | < 10" |
| `session_meta.json` | con `calibration` aplicada y `slit` medido |

---

## 6. Cómo ayuda a arreglar problemas de escaneado

NightScribe no solo captura: cada función está pensada para **detectar y
corregir** un fallo concreto.

### 6.1 Síntoma → causa → qué hace → acción

| Síntoma | Causa típica | Qué hace NightScribe | Acción |
| --- | --- | --- | --- |
| Recon elíptico (X/Y ≠ 1) | Tasa de barrido o escala de rendija | Autocalibra X y mide `S_Y`; registra `actual_xy` | Mira `actual_xy`; corrige con `--aspect` en el mosaico |
| Filas del mosaico no encajan / hueco | Paso en Dec con escala equivocada | Mide la escala de rendija y guarda `dec_offset_measured_arcsec` | Usa `--offsets`/`--dec-offsets` del `scan_log.csv` |
| Una fila a otra escala (~1–4 %) | Cambio de FPS/escala entre barridos | Aviso `CONSISTENCY` | Corrige en captura o `--auto-scale` en el mosaico |
| Barrido sin datos / SER vacío | Disco lleno, cuelgue del driver | Guarda de disco + vigilancia de stall + `problem` | Revisa `CAPTURE SPACE`/`SCAN PROBLEM`; hay reintento |
| Faltan frames | USB / límite de frames | Compara con `duration*fps`; `FRAMES SHORT` | Baja la tasa o revisa el driver |
| Stack más blando que una toma | Deriva en Dec entre tomas | Recentrado de Dec + log | Revisa `dec_start`; sube la tesela del apilado |
| Costura de brillo en el mosaico | Exposición distinta entre filas | Fija la exposición una vez | Usa `--normalize gain` |
| Deriva de seguimiento | Alineación polar | Resumen de deriva RA/Dec al final | Ajusta azimut/altitud según la pista |

### 6.2 Leer los registros para diagnosticar

- **`scan_log.csv`**
  - `actual_xy` ≈ 1.000 → la tasa de barrido está bien. Si ≠ 1 → problema de tasa.
  - `frames_short_pct` > 0 → frames perdidos.
  - `problem` no vacío → ese barrido falló.
  - `dec_offset_measured_arcsec` → Dec real de la fila (para el mosaico).
  - `slit_extent_px` → extensión de la rendija (escala Y).
  - `calibration_factor` → corrección aplicada a la velocidad.
- **`session_meta.json`**: `calibration`, `slit` (extensión y `S_Y`),
  `solar_diameter_source`, `mosaic_plan`.
- **Banner**: `Solar diameter (...)`, `Scan calibration`, `effective slit`.

### 6.3 Árbol de decisión rápido

1. ¿`actual_xy` ≈ 1? **No** → es la tasa de barrido: revisa la calibración,
   el FPS (`CAPTURE_FPS`) o el driver.
2. ¿`actual_xy` ≈ 1 pero el disco sale elíptico? → es la **rendija** (eje Y):
   usa `--aspect auto` en el mosaico o revisa `S_Y` en `session_meta.json`.
3. ¿Las filas no encajan? → es el **paso en Dec**: usa los
   `dec_offset_measured_arcsec` del log (`--offsets`/`--dec-offsets`).
4. ¿Una fila distinta? → mira los avisos `CONSISTENCY`; corrige en captura o
   `--auto-scale`.
5. ¿Faltan datos? → `problem` / `frames_short_pct`: revisa disco, USB y
   `CAPTURE_STALL_SECONDS`.

---

## 7. Las herramientas

Dos scripts hacen el post-proceso fuera de SharpCap. Su documentación completa
está en el encabezado de cada fichero (en inglés).

### 7.1 `tools/align_stack_recon.py` — apilar una fila

**Qué hace.** Pre-alinea las tomas de una fila con **una transformación global
por imagen** (SIFT + RANSAC: traslación, rotación, escala), la refina localmente
si se pide y luego promedia. Es la alternativa al apilado por teselas de
JSol'Ex cuando quieres un alineado auditable y sin depender del modelo local.

**Entradas/salidas.** Lee TIF/PNG/FITS de la misma fila; escribe un TIF de 16
bits, un PNG de inspección opcional y un JSON con las transformaciones.

**Opciones principales**

| Opción | Defecto | Para qué |
| --- | ---: | --- |
| `--inputs` | — | imágenes de la fila, en orden |
| `--output` | — | TIF de salida |
| `--ref` | `0` | imagen de referencia |
| `--method` | `sift` | `ecc` si SIFT falla por poco contraste |
| `--canvas` | `union` | `ref` para conservar el tamaño de la referencia |
| `--local-tile` | `0` | `128` activa el refinado local; `0` lo desactiva |
| `--max-local-shift` | `48` | rechaza teselas con saltos absurdos |
| `--stack` | `sigma` | `mean`, `median` o `sigma` |
| `--normalize` | `gain` | `none` para no tocar el brillo |
| `--preview` | — | PNG de inspección |

**Ejemplo**

```bash
python3 tools/align_stack_recon.py \
  --inputs recon/0000_*.tif recon/0001_*.tif recon/0002_*.tif \
  --output stacks/row1_stacked.tif --stack sigma --local-tile 128
```

**Qué mirar.** `rms < 3 px`, `inliers ≥ 50`, escala entre tomas `< 1 %`, y que
el stack no sea más blando que la mejor toma individual.

**JSON.** `images[]` con `global` (escala, rotación, tx/ty, inliers, rms),
`local` y `gain`; `canvas`. Reproduce el stack sin repetir el ajuste.

### 7.2 `tools/mosaic_from_recon.py` — coser las filas

**Qué hace.** Compone el mosaico **por traslación** de los stacks de fila, con
igualado de brillo y mezcla en rampa. **No reescala paneles**: si dos filas
tienen escalas distintas, se arregla antes (captura o geometría común). Antes de
nada corrige la **anisotropía** del recon (estira Y) para que el disco quede
redondo y los offsets de Dec cuadren.

**Entradas/salidas.** Lee un TIF por fila; escribe el mosaico, un PNG opcional y
un JSON con posiciones, escalas y ganancias.

**Opciones principales**

| Opción | Defecto | Para qué |
| --- | ---: | --- |
| `--inputs` | — | un TIF por fila |
| `--output` | — | TIF del mosaico |
| `--offsets` | — | `dx,dy` por imagen (px), **vía fiable** |
| `--dec-offsets` | — | Dec de cada fila (arcsec) + `--pixel-scale` |
| `--pixel-scale` | — | escala de placa (arcsec/px, tras la anisotropía) |
| `--aspect` | `auto` | corrección de anisotropía |
| `--auto` / `--no-auto` | on | refinado por correlación de fase |
| `--min-response` | `0.15` | umbral para aplicar la corrección |
| `--scales` / `--auto-scale` | — | reescalado explícito (último recurso) |
| `--scale-axis` | `both` | `x` para diferencia de tasa, `both` de aumento |
| `--normalize` | `gain` | igualado de brillo |
| `--feather` | `80` | ancho de la rampa de mezcla (px) |
| `--crop` | off | recorta el borde sin datos |

**Ejemplos**

```bash
# offsets medidos
python3 tools/mosaic_from_recon.py \
  --inputs stacks/row1_stacked.tif stacks/row2_stacked.tif \
  --offsets 0,0 142,-1342 \
  --output mosaic/mosaic.tif --preview mosaic/mosaic.png --crop

# paneles centrados en el disco (stacks de JSol'Ex)
python3 tools/mosaic_from_recon.py \
  --inputs jsol/00_stacked.tif jsol/01_stacked.tif \
  --dec-offsets +630 -630 --pixel-scale 0.4257 \
  --output mosaic/mosaic.tif
```

**Cómo enlaza con NightScribe.** Saca los `--offsets`/`--dec-offsets` de
`dec_offset_measured_arcsec` del `scan_log.csv`, y la anisotropía de `slit`/`S_Y`
en `session_meta.json`.

**Qué mirar.** Diámetros de disco entre filas `< 2 %`; `resp ≥ 0.15`; residuo
vertical `< 3 px`. Si los discos difieren más, `--auto-scale` (asume una
interpolación extra).

**JSON.** `positions_px`, `scales`, `gains`, `measured_overlap`, `canvas`.

---

## 8. Post-proceso

```bash
# 1) SER → recon con JSol'Ex (geometría común, autocrop off)

# 2) apilar las tomas de cada fila
python3 tools/align_stack_recon.py \
  --inputs recon/0000_*.tif recon/0001_*.tif recon/0002_*.tif \
  --output stacks/row1_stacked.tif --stack sigma --local-tile 128

# 3) coser las filas
python3 tools/mosaic_from_recon.py \
  --inputs stacks/row1_stacked.tif stacks/row2_stacked.tif \
  --offsets 0,0 142,-1342 \
  --output mosaic/mosaic.tif --preview mosaic/mosaic.png --crop
```

Si los paneles están centrados en el disco (stacks de JSol'Ex), usa
`--dec-offsets` + `--pixel-scale`; si las filas difieren en escala,
`--auto-scale` (último recurso). Ver §9.6 para el proceso y los criterios.

---

## 9. Profundización: cómo funciona y por qué

Esta sección recoge la geometría y el porqué del diseño, para poder entender y
diagnosticar el comportamiento del script sin más documentos.

### 9.1 Geometría del barrido y muestreo en X

Un SHG reconstruye una columna de imagen por cada fotograma capturado. El monte
barre el disco solar a través de la rendija fija; después los fotogramas se
colocan uno junto a otro en el recon. Para que la reconstrucción sea **1:1** (un
píxel de cielo por fotograma), el cielo debe moverse exactamente un píxel por
fotograma:

```
velocidad_barrido ("/s) = PIXEL_SCALE ("/px) * fps (fotogramas/s)
multiplicador (x)       = velocidad_barrido / SIDEREAL   # 15.04"/s por unidad sidérea
```

`PIXEL_SCALE = 206265 * (PIXEL_SIZE_um / 1000) / FOCAL_LENGTH_mm`.

- El **eje X** (dirección de barrido) lo fija la velocidad del monte, así que
  depende de `FOCAL_LENGTH` y del FPS real.
- El **eje Y** (la rendija) lo fija la óptica del espectroheliógrafo, no el monte.

`FOCAL_LENGTH` debe ser la focal **efectiva** en el sensor, medida sobre un recon
**nativo** (no sobre un stack de JSol'Ex, que está remuestreado). Si está mal,
escala todo el recon en X: demasiado grande → el monte barre despacio y X sale
estirado; demasiado pequeña → X sale comprimido. Los 969 mm que a veces se ven en
JSol'Ex son una escala de **procesado** (JSol'Ex remuestrea su salida a ella), no
la focal física para la velocidad de barrido: no la copies en la configuración.

Como el monte se mueve en coordenadas RA, la velocidad real del cielo es
`velocidad * cos(Dec)`. Con `COS_DEC_CORRECTION = True` el multiplicador se
divide por `cos(Dec)` para que la escala en X sea la misma a cualquier
declinación.

### 9.2 Diámetro por efeméride y autocalibración de la velocidad

El diámetro aparente del Sol cambia ~3 % a lo largo del año. NightScribe lo
calcula a partir de la fecha con una efeméride de baja precisión (~0.01 % de
error) y registra la UTC de cada barrido para poder recalcular el valor exacto
después. El banner indica la fuente: `ephemeris`, `override` o `fallback`.

Al empezar la sesión, una vez fijada la exposición, el script mide el FPS real y
después **autocalibra la velocidad de barrido**: cruza el disco, mide el tiempo
de cruce contra el diámetro (de efeméride) y escala `scan_multiplier` para que
el cielo se mueva exactamente `PIXEL_SCALE` por fotograma. Así se elimina la
dependencia de que `FOCAL_LENGTH` y el FPS reportado sean exactos. El resultado
se registra por barrido como `actual_xy` (≈1.000 cuando es correcto).

### 9.3 Eje de la rendija y anisotropía del recon

En el recon, **X es el barrido** (el número de columnas es el número de
fotogramas) e **Y es la rendija**. Los dos ejes no comparten escala de placa: el
aumento de la imagen de la rendija no tiene por qué ser 1, así que el disco puede
salir notablemente más ancho que alto (X/Y ≈ 1.28 en el equipo de referencia; el
eje comprimido es Y). Es una propiedad del sistema, no de una toma.

Con el disco centrado, NightScribe mide la extensión iluminada a lo largo de la
rendija en un frame vivo y deduce `S_Y = diámetro / extensión_px`, actualizando la
escala de imagen de la rendija que usa el plan del mosaico
(`MEASURE_SLIT_EXTENT`). La corrección de la anisotropía es **estirar Y**, no
tocar la velocidad del monte.

### 9.4 Centrado en Dec: bucle izquierda/derecha vs método de la cuerda

**Bucle izquierda/derecha legacy** (`center_dec()`, ruta de un solo barrido):
parte el frame en dos mitades y compara su brillo medio,
`offset = (L-R)/(L+R)`. Solo es válido cuando el disco solar **cabe a lo largo de
la rendija**: en el centro del disco la iluminación es simétrica, y un
desplazamiento en Dec hace que una mitad sea más brillante. Cuando el disco
desborda el frame, las dos mitades están llenas de disco, la métrica pierde su
cruce por cero y el bucle puede sacar el Sol de la rendija — en ese régimen pon
`ENABLE_DEC_CENTERING = False` (aproximadamente `FOCAL_LENGTH > 825 mm` con un
sensor completo de 3840 px).

**Método de la cuerda** (ruta de mosaico, `center_dec_chord()`): el intervalo en
RA entre el limbo de entrada y el de salida es la **cuerda** del disco a la Dec
de la rendija. Con `R` el radio solar, `S` la longitud efectiva de la rendija y
`q` la distancia con signo del centro de la rendija al centro del disco:

```
e = max(0, |q| - S/2)          # borde de rendija más cercano al centro del disco
L = 2 * sqrt(R^2 - e^2)        # cuerda medida
|q| = e + S/2                  # fuera de la zona muerta
```

Mientras la rendija contiene el centro del disco (`|q| <= S/2`) la cuerda es el
diámetro completo y no cambia con el desplazamiento: es la **zona muerta**, un
límite geométrico, no un problema de ajuste. La rutina camina en +Dec en pasos
hasta que el borde más cercano queda a `MOSAIC_CENTER_TRUST_ARCSEC` (0.5 R) del
centro; después una sonda corta en +Dec resuelve de qué lado estaba la rendija
(la sonda está acotada por debajo del desplazamiento, así que no puede cruzar el
centro y el signo es fiable). Tras una corrección medida, un residuo dentro del
radio de confianza es ruido de cuerda, y el plan añade
`MOSAIC_CENTER_RESIDUAL_ARCSEC` (300") de margen por él.

El signo de `MoveAxis(1, +)` se aprende empíricamente a partir de `mount.Dec`, así
que no hace falta ninguna tabla de `SideOfPier`. Cuando el disco cabe en la
rendija (`S >= diámetro`), la rutina de cuerda delega en el bucle legacy, que es
más barato.

### 9.5 Geometría del mosaico

```
slit_px = SLIT_MM * M * 1000 / PIXEL_SIZE          # píxeles de sensor de la rendija
strip   = min(ROI_width, slit_px) * PIXEL_SCALE / M
step    = strip * (1 - MOSAIC_OVERLAP)
margin  = max(MOSAIC_MARGIN_ARCSEC, uncertainty)
rows    = 1                                              si strip >= diámetro
rows    = ceil((diámetro + 2*margin - strip) / step) + 1 en otro caso
```

`M` es el aumento de reimagen de la rendija (`MOSAIC_SLIT_IMAGE_SCALE`, medido en
vivo cuando es posible). Las filas se reparten simétricamente alrededor de la Dec
centrada:

```
Dec_i = Dec_centro + (i - (rows-1)/2) * step / 3600     (grados)
```

Cuando `strip >= diámetro` una fila ya contiene el disco entero y `rows` se
fuerza a 1. El plan se valida para cobertura y avisa si las filas extremas no
alcanzan el limbo. Una fila de margen que no encuentra disco se salta; si ocurre
después de haber capturado datos, se trata como el borde de salida y el mosaico
termina. Antes de cada fila se retrocede en RA más de un diámetro de disco para
que la búsqueda de limbo (que solo avanza) empiece por el lado de entrada.

### 9.6 Post-proceso y criterios

El flujo recomendado es:

```
SER (rNN_sNN) → recon (JSol'Ex: geometría común, autocrop off)
              → apilar por fila (tools/align_stack_recon.py)
              → coser filas    (tools/mosaic_from_recon.py)
```

Fuerza la **misma geometría** en JSol'Ex para todos los frames (elipse X/Y,
rotación, polinomio de distorsión, tesela de apilado 128, entrada = recon de 16
bits, no PNG estirado); si no, los frames salen a escalas distintas y el mosaico
tiene que reescalar. La herramienta de mosaico compone **solo por traslación** y
primero corrige la anisotropía del recon (estira Y); no reescala paneles por
defecto, porque un reescalado añade una segunda interpolación sobre un recon ya
anisótropo. Usa `--auto-scale` solo como último recurso.

Los criterios de aceptación de §5 resumen qué comprobar; §6 mapea síntomas a
causas y acciones.

---

## 10. Referencias

- `nightscribe-scan.py` — script de captura (bloque de configuración,
  `scan_once()`, `calibrate_scan_rate()`, `measure_slit_scale()`).
- `nightscribe-scan.cfg` (listo) / `docs/nightscribe-scan.cfg.example` / `docs/nightscribe-scan.cfg.reference` — configuración.
- `nightscribe-ui.html` — panel de control servido por el script (perfiles, config, iniciar/parar).
- `tools/build_ui.py` — regenera el esquema embebido del panel.
- `nightscribe-profiles.json` — perfiles guardados por el panel.
- `tools/align_stack_recon.py`, `tools/mosaic_from_recon.py` — apilado y mosaico.
- `tools/_imgio.py` — lectura/escritura de imágenes compartida por las herramientas.
- SHGScan (Patrick Hsieh) — <https://github.com/FlankerOneTwo/SHGScan>.
- Linaje upstream — <https://github.com/bennich/sharpcap-solar-scanning>.
