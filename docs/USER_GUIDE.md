# NightScribe — user guide

**English** · [🇪🇸 Español → GUIA_USUARIO.md](GUIA_USUARIO.md)

A short guide to `nightscribe-scan.py`: what it does, why each feature matters
and how to use it. The *Deep dive* section (§9) explains the geometry and the
reasoning behind the design; the post-processing tools are documented in their
own file headers.

---

## 1. What NightScribe is

It is a capture script for a fast-burst spectroheliograph (SHG). It runs inside
the SharpCap scripting console and does all the measurement work on the live
video stream (no temporary files). Two modes:

- `ENABLE_MOSAIC = False` → **single scan**.
- `ENABLE_MOSAIC = True` → **multi-row mosaic** with chord-based Dec centring.

NightScribe is a new development, based on Patrick Hsieh's (FlankerOneTwo)
**SHGScan** project: <https://github.com/FlankerOneTwo/SHGScan>. It keeps the
event-driven architecture and adds self-calibration, live geometry measurement,
integrity checks and session bookkeeping.

---

## 2. Quick start

**Requirements**

- SharpCap with the camera and mount connected.
- An ASCOM mount that reports RA/Dec and accepts `MoveAxis`.
- A capture folder with free space (the script checks it).

**Run**

1. Paste `nightscribe-scan.py` into the SharpCap scripting console and run it.
   (Or use the control panel, see 2.1.)
2. The configuration is no longer edited in the script: it lives in
   `nightscribe-scan.cfg` (see 2.1). If it is missing, the defaults are used.
3. Read the startup banner. The key lines:

   ```
   Config: loaded ...\nightscribe-scan.cfg (62 value(s)).
   Solar diameter: 1934.8" (ephemeris)  Expected disk: 3349 px
   Scan-rate calibration: ON (self-calibrates X sampling)
   Mosaic: slit=7.0mm  image scale=...  strip=... step=... rows=...
   ```

4. When it finishes it leaves `scan_log.csv`, `scan_stats.jsonl` and
   `session_meta.json` next to the SER files.

### 2.1 External configuration and control panel

**Config file.** Everything tunable is in `nightscribe-scan.cfg` (INI, in the
same folder), editable by hand with any text editor. The project ships a
**ready-to-use** `nightscribe-scan.cfg`; you only need to set a few values,
everything else has a default:

```ini
[optics]
# Effective focal length at the sensor (mm). Sets the scan rate.
focal_length = 714
# Camera pixel size (um).
pixel_size = 2

# Only for a full-disk mosaic:
[mosaic]
enable_mosaic = false
mosaic_slit_length_mm = 7
mosaic_scans_per_row = 5
```

The full list of settings (with their defaults) is in
`docs/nightscribe-scan.cfg.reference`, and `docs/nightscribe-scan.cfg.example`
is a minimal template if you want to start over. Missing keys use the default;
lines starting with `#` are ignored. The banner says where it was loaded from.
You can also point at a path with the `NIGHTSCRIBE_CONFIG` environment variable.

**Control panel (HTML).** It works in **two modes**:

- **With the server** (recommended for capturing): run `nightscribe-scan.py`
  with `START_UI = True` (the default) in the SharpCap scripting console. It
  starts a local server on `http://127.0.0.1:8765/`; open it in the browser.
  Here **Start/Stop** are enabled. With `START_UI = False` the script runs a
  capture immediately and no panel is started.
- **As a file**: open `nightscribe-ui.html` directly in Chrome/Edge (embedded
  schema). You can view and edit all the configuration and profiles, opening
  and saving `nightscribe-scan.cfg` and `nightscribe-profiles.json` with the
  corresponding button. **Start/Stop stay disabled** (that needs SharpCap).

The panel is a compact card (not full screen) with:

- **Session** (top): mode (single/mosaic), scans per row, cycles and padding,
  in plain sight. The control that does not apply to the selected mode is
  greyed out (scans per row for a single scan, cycles for a mosaic).
- **Profiles**: create (asking for the **telescope data**: name, description,
  focal length, aperture, camera and notes), apply (**Apply & save** writes the
  `.cfg` in one step), duplicate, rename, delete, import and export. The active
  profile is marked; the JSON is v2 with `meta` (telescope) and `config`
  blocks.
- **Configuration**: every schema field with its help; "Save config" writes
  `nightscribe-scan.cfg`.
- **Status** and **connection** (server or file mode).

**Start always uses the values currently shown in the panel**, so there is no
need to save first; **Save config** only persists them to `nightscribe-scan.cfg`
for the next session.

The embedded schema is regenerated from the script with
`python3 tools/build_ui.py`.

---

## 3. Session flow

```
power camera + mount → set ROI and base exposure
        │
        ▼
NightScribe: limb alignment → Dec centring → auto-exposure
        │                    │
        │                    └─ measures the slit extent (S_Y)
        ▼
measures real FPS → self-calibrates the scan rate
        │
        ▼
labelled scans rNN_sNN  (+ retry on failure)
        │
        ▼
SER + scan_log.csv + scan_stats.jsonl + session_meta.json
```

In mosaic mode it also plans the rows, warns if they do not cover the disk, and
re-centres Dec between scans of the same row.

---

## 4. Features, impact and how to use them

### 4.1 Scan-rate self-calibration

- **What it does**: crosses the disk, measures the crossing time and scales the
  mount rate so the sky moves exactly `PIXEL_SCALE` per frame.
- **Impact**: X sampling is 1:1 even if `FOCAL_LENGTH` or the reported FPS are
  not exact. This is what prevents a stretched recon and a soft stack.
- **How**: `CALIBRATE_SCAN_RATE = True` (default). Watch `Scan calibration` in
  the banner and `actual_xy` in the log (should stay ≈1.000).

### 4.2 Ephemeris solar diameter

- **What it does**: computes the apparent solar diameter from the date
  (~0.01% accurate) instead of a fixed value.
- **Impact**: the scale no longer carries the ±3% annual variation.
- **How**: `USE_EPHEMERIS = True`. The banner shows the source
  (`ephemeris` / `override` / `fallback`). To force a value:
  `EPHEMERIS_DIAMETER_OVERRIDE`.

### 4.3 Live slit-axis scale

- **What it does**: with the disk centred, measures the illuminated extent
  along the slit and derives `S_Y = diameter / extent_px`; it then updates the
  scale used by the mosaic plan.
- **Impact**: the row step ends up in the same units as the scan axis, so the
  rows tile without measuring the factor by hand.
- **How**: `MEASURE_SLIT_EXTENT = True`. Watch `effective slit` in the plan and
  `slit` in `session_meta.json`. If it could not be measured it warns and keeps
  the configured value.

### 4.4 Capture integrity

- **Disk guard**: before each scan it estimates the size and checks free space
  (`CAPTURE_MIN_FREE_GB`). Prints `CAPTURE SPACE` if it will not fit.
- **Scan watchdog**: if SharpCap stops writing for `CAPTURE_STALL_SECONDS` or
  `cam.Capturing` drops, it aborts with `SCAN PROBLEM` instead of leaving an
  empty SER.
- **Short frames**: compares what was written with `scan_duration * fps`; if
  more than `FRAMES_SHORT_TOL` is missing it warns `FRAMES SHORT` and records it.
- **Abortable**: Ctrl+C stops the mount immediately.
- **Retry**: a failed scan is retried `SCAN_RETRIES` times, re-aligning the
  limb; the bad attempt is kept with its `problem`.

### 4.5 Consistency and Dec correction

- **Consistency**: warns if `actual_xy` or the slit extent drifts more than
  `CONSISTENCY_TOL_XY` / `CONSISTENCY_TOL_EXTENT` (3%) between scans.
- **`cos(Dec)`**: `COS_DEC_CORRECTION = True` applies the correction in both
  modes, so the X scale is the same at any declination.
- **Stable Dec**: in mosaic mode it re-centres Dec between scans of the same row
  if it drifts more than `DEC_DRIFT_TOL_ARCSEC`.

### 4.6 Traceability

- `scan_log.csv`: one row per scan (rewritten after each scan).
- `scan_stats.jsonl`: the same metrics per scan, as JSON.
- `session_meta.json`: effective configuration, calibration and slit geometry,
  plus the mosaic plan.

---

## 5. Acceptance criteria

| Check | Criterion |
| --- | --- |
| `actual_xy` in `scan_log.csv` | 0.97–1.03 |
| Measured vs used FPS | < 2% |
| `frames_short_pct` | ≈ 0 |
| `CONSISTENCY` warnings | none |
| Dec spread per row | < 10" |
| `session_meta.json` | has calibration applied and slit measured |

---

## 6. How it helps fix scanning problems

NightScribe does not just capture: every feature is there to **detect and fix**
a specific failure.

### 6.1 Symptom → cause → what it does → action

| Symptom | Typical cause | What NightScribe does | Action |
| --- | --- | --- | --- |
| Elliptical recon (X/Y ≠ 1) | Scan rate or slit scale | Self-calibrates X, measures `S_Y`; records `actual_xy` | Check `actual_xy`; fix with `--aspect` in the mosaic |
| Mosaic rows do not line up / gap | Dec step with the wrong scale | Measures the slit scale and records `dec_offset_measured_arcsec` | Use `--offsets`/`--dec-offsets` from `scan_log.csv` |
| One row at another scale (~1–4%) | FPS/scale change between scans | `CONSISTENCY` warning | Fix in capture or `--auto-scale` in the mosaic |
| Scan with no data / empty SER | Full disk, driver hang | Disk guard + stall watchdog + `problem` | Check `CAPTURE SPACE`/`SCAN PROBLEM`; retry is automatic |
| Missing frames | USB / frame limit | Compares with `duration*fps`; `FRAMES SHORT` | Lower the rate or check the driver |
| Stack softer than a single frame | Dec drift between frames | Dec re-centring + log | Check `dec_start`; raise the stacking tile |
| Brightness seam in the mosaic | Exposure differing between rows | Fixes exposure once | Use `--normalize gain` |
| Tracking drift | Polar alignment | RA/Dec drift summary at the end | Adjust azimuth/altitude as hinted |

### 6.2 Reading the logs to diagnose

- **`scan_log.csv`**
  - `actual_xy` ≈ 1.000 → scan rate is fine. If ≠ 1 → rate problem.
  - `frames_short_pct` > 0 → dropped frames.
  - `problem` non-empty → that scan failed.
  - `dec_offset_measured_arcsec` → real Dec of the row (for the mosaic).
  - `slit_extent_px` → slit extent (Y scale).
  - `calibration_factor` → correction applied to the rate.
- **`session_meta.json`**: `calibration`, `slit` (extent and `S_Y`),
  `solar_diameter_source`, `mosaic_plan`.
- **Banner**: `Solar diameter (...)`, `Scan calibration`, `effective slit`.

### 6.3 Quick decision tree

1. Is `actual_xy` ≈ 1? **No** → it is the scan rate: check the calibration,
   the FPS (`CAPTURE_FPS`) or the driver.
2. `actual_xy` ≈ 1 but the disk is elliptical? → it is the **slit** (Y axis):
   use `--aspect auto` in the mosaic or check `S_Y` in `session_meta.json`.
3. Rows do not line up? → it is the **Dec step**: use the
   `dec_offset_measured_arcsec` from the log (`--offsets`/`--dec-offsets`).
4. One row different? → look at the `CONSISTENCY` warnings; fix in capture or
   `--auto-scale`.
5. Missing data? → `problem` / `frames_short_pct`: check disk, USB and
   `CAPTURE_STALL_SECONDS`.

---

## 7. The tools

Two scripts do the post-processing outside SharpCap. Their full documentation is
in each file's header (English).

### 7.1 `tools/align_stack_recon.py` — stack one row

**What it does.** Pre-aligns the frames of one row with **one global transform
per image** (SIFT + RANSAC: translation, rotation, scale), optionally refines it
locally and then averages. It is the alternative to JSol'Ex's tile stacking when
you want an auditable alignment that does not depend on the local model.

**Inputs / outputs.** Reads TIF/PNG/FITS of the same row; writes a 16-bit TIF,
an optional PNG preview and a JSON report with the transforms.

**Main options**

| Option | Default | What it is for |
| --- | ---: | --- |
| `--inputs` | — | the row's images, in order |
| `--output` | — | output TIF |
| `--ref` | `0` | reference image |
| `--method` | `sift` | `ecc` if SIFT fails on low contrast |
| `--canvas` | `union` | `ref` to keep the reference size |
| `--local-tile` | `0` | `128` enables the local refine; `0` disables it |
| `--max-local-shift` | `48` | reject tiles with absurd shifts |
| `--stack` | `sigma` | `mean`, `median` or `sigma` |
| `--normalize` | `gain` | `none` to leave brightness alone |
| `--preview` | — | PNG for inspection |

**Example**

```bash
python3 tools/align_stack_recon.py \
  --inputs recon/0000_*.tif recon/0001_*.tif recon/0002_*.tif \
  --output stacks/row1_stacked.tif --stack sigma --local-tile 128
```

**What to check.** `rms < 3 px`, `inliers ≥ 50`, scale between frames `< 1%`,
and a stack that is not softer than the best single frame.

**JSON.** `images[]` with `global` (scale, rotation, tx/ty, inliers, rms),
`local` and `gain`; plus `canvas`. Reproduces the stack without re-fitting.

### 7.2 `tools/mosaic_from_recon.py` — stitch the rows

**What it does.** Composes the mosaic by **translation** of the per-row stacks,
with brightness matching and feather blending. It **does not rescale panels**:
if two rows differ in scale, fix that earlier (capture or common geometry). It
first corrects the recon **anisotropy** (stretches Y) so the disk is round and
the Dec offsets line up.

**Inputs / outputs.** Reads one TIF per row; writes the mosaic, an optional PNG
and a JSON with positions, scales and gains.

**Main options**

| Option | Default | What it is for |
| --- | ---: | --- |
| `--inputs` | — | one TIF per row |
| `--output` | — | mosaic TIF |
| `--offsets` | — | `dx,dy` per image (px), **reliable path** |
| `--dec-offsets` | — | Dec of each row (arcsec) + `--pixel-scale` |
| `--pixel-scale` | — | plate scale (arcsec/px, after anisotropy) |
| `--aspect` | `auto` | anisotropy correction |
| `--auto` / `--no-auto` | on | phase-correlation refinement |
| `--min-response` | `0.15` | threshold to apply the correction |
| `--scales` / `--auto-scale` | — | explicit rescale (last resort) |
| `--scale-axis` | `both` | `x` for a rate difference, `both` for magnification |
| `--normalize` | `gain` | brightness matching |
| `--feather` | `80` | blend ramp width (px) |
| `--crop` | off | trim the empty border |

**Examples**

```bash
# measured offsets
python3 tools/mosaic_from_recon.py \
  --inputs stacks/row1_stacked.tif stacks/row2_stacked.tif \
  --offsets 0,0 142,-1342 \
  --output mosaic/mosaic.tif --preview mosaic/mosaic.png --crop

# disk-centred panels (JSol'Ex stacks)
python3 tools/mosaic_from_recon.py \
  --inputs jsol/00_stacked.tif jsol/01_stacked.tif \
  --dec-offsets +630 -630 --pixel-scale 0.4257 \
  --output mosaic/mosaic.tif
```

**How it links to NightScribe.** Take `--offsets`/`--dec-offsets` from
`dec_offset_measured_arcsec` in `scan_log.csv`, and the anisotropy from
`slit`/`S_Y` in `session_meta.json`.

**What to check.** Disk diameters between rows `< 2%`; `resp ≥ 0.15`; vertical
residual `< 3 px`. If the disks differ more, use `--auto-scale` (it assumes an
extra interpolation).

**JSON.** `positions_px`, `scales`, `gains`, `measured_overlap`, `canvas`.

---

## 8. Post-processing

```bash
# 1) SER → recon with JSol'Ex (common geometry, autocrop off)

# 2) stack the frames of each row
python3 tools/align_stack_recon.py \
  --inputs recon/0000_*.tif recon/0001_*.tif recon/0002_*.tif \
  --output stacks/row1_stacked.tif --stack sigma --local-tile 128

# 3) stitch the rows
python3 tools/mosaic_from_recon.py \
  --inputs stacks/row1_stacked.tif stacks/row2_stacked.tif \
  --offsets 0,0 142,-1342 \
  --output mosaic/mosaic.tif --preview mosaic/mosaic.png --crop
```

If the panels are disk-centred (JSol'Ex stacks), use `--dec-offsets` +
`--pixel-scale`; if the rows differ in scale, `--auto-scale` (last resort).
See §9.6 for the process and the acceptance criteria.

---

## 9. Deep dive: how it works and why

This section collects the geometry and the reasoning behind the design, so the
behaviour of the script can be understood and diagnosed without extra documents.

### 9.1 Scan geometry and the X sampling

An SHG reconstructs one image column per captured frame. The mount scans the
solar disk across the fixed slit; the frames are then placed side by side in the
recon. For the reconstruction to be **1:1** (one sky pixel per frame), the sky
must move exactly one pixel-width per frame:

```
scan_speed ("/s)    = PIXEL_SCALE ("/px) * fps (frames/s)
scan_multiplier (x) = scan_speed / SIDEREAL     # 15.04"/s per sidereal unit
```

`PIXEL_SCALE = 206265 * (PIXEL_SIZE_um / 1000) / FOCAL_LENGTH_mm`.

- The **X axis** (scan direction) is set by the mount rate, so it depends on
  `FOCAL_LENGTH` and the real frame rate.
- The **Y axis** (the slit) is set by the spectrograph optics, not the mount.

`FOCAL_LENGTH` must be the **effective** focal length at the sensor, measured on
a *native* recon (not on a resampled JSol'Ex stack). Getting it wrong scales the
whole recon in X: too large → the mount scans too slowly and X is stretched; too
small → X is compressed. The 969 mm sometimes seen in JSol'Ex is a
**processing** plate scale (JSol'Ex resamples its output to it), not the
physical focal length for the scan rate — do not copy it into the config.

Because the mount moves in RA coordinates, the true sky rate is
`rate * cos(Dec)`. With `COS_DEC_CORRECTION = True` the multiplier is divided by
`cos(Dec)` so the X scale is the same at any declination.

### 9.2 Ephemeris diameter and scan-rate self-calibration

The apparent solar diameter changes by ~3% over the year. NightScribe computes
it from the date with a low-precision ephemeris (~0.01% accurate) and records
the UTC of every scan so the exact value can be recomputed later. The banner
shows the source: `ephemeris`, `override` or `fallback`.

At session start, once the exposure is final, the script measures the real frame
rate and then **self-calibrates the scan rate**: it crosses the disk, measures
the crossing time against the (ephemeris) diameter, and scales
`scan_multiplier` so the sky moves exactly `PIXEL_SCALE` per frame. This removes
the dependence on `FOCAL_LENGTH` and the reported FPS being exact. The result is
logged per scan as `actual_xy` (≈1.000 when correct).

The calibration measures the sky rate while slewing at `MOSAIC_CHORD_SPEED`
(16x) and scales the science multiplier by the **rate per unit**
(`measured / MOSAIC_CHORD_SPEED`), not by `target / measured`; the latter is
wrong by the measurement speed and made the scan several times too slow. A guard
rejects a correction that would leave the sky rate more than 2x off the target,
so a single bad measurement cannot wreck a session.

### 9.3 Slit axis and recon anisotropy

In the recon, **X is the scan** (the number of columns equals the number of
frames) and **Y is the slit**. The two axes do not share the same plate scale:
the slit image magnification is not necessarily 1, so the disk can come out
noticeably wider than tall (X/Y ≈ 1.28 in the reference setup; the compressed
axis is Y). This is a property of the system, not of a single frame.

With the disk centred, NightScribe measures the illuminated extent along the
slit in a live frame and derives `S_Y = SOLAR_DIAMETER / extent_px`, updating the
slit image scale used by the mosaic plan (`MEASURE_SLIT_EXTENT`). The correction
for the anisotropy is to **stretch Y**, not to change the mount rate.

### 9.4 Dec centring: left/right loop vs chord method

**Legacy left/right loop** (`center_dec()`, single-scan path): split the frame in
two halves and compare their mean brightness, `offset = (L-R)/(L+R)`. It is
valid only when the solar disk **fits along the slit**: at the disk centre the
illumination is symmetric, and an offset in Dec makes one half brighter. When
the disk overfills the frame, both halves are full of disk, the metric loses its
zero crossing and the loop can push the Sun out of the slit — set
`ENABLE_DEC_CENTERING = False` in that regime (roughly `FOCAL_LENGTH > 825 mm`
with a full 3840-px sensor).

**Chord method** (mosaic path, `center_dec_chord()`): the RA interval between the
leading and trailing limb is the **chord** of the disk at the slit's Dec. With
`R` the solar radius, `S` the effective slit length and `q` the signed Dec offset
of the slit centre:

```
e = max(0, |q| - S/2)          # nearest slit edge to the disk centre
L = 2 * sqrt(R^2 - e^2)        # measured chord
|q| = e + S/2                  # outside the dead zone
```

While the slit spans the disk centre (`|q| <= S/2`) the chord is the full
diameter and does not change with offset: this is the **dead zone**, a geometric
limit, not a tuning problem. The routine walks +Dec in steps until the nearest
edge is `MOSAIC_CENTER_TRUST_ARCSEC` (0.5 R) from the centre; a short +Dec probe
then tells which side the slit was on (the probe is capped below the offset, so
it cannot cross the centre and the sign is reliable). After a measured
correction, a residual inside the trust radius is chord noise, and the plan adds
`MOSAIC_CENTER_RESIDUAL_ARCSEC` (300") of margin for it.

The sign of `MoveAxis(1, +)` is learned empirically from `mount.Dec`, so no
`SideOfPier` table is needed. When the disk fits the slit (`S >= diameter`), the
chord routine delegates to the cheaper legacy loop.

### 9.5 Mosaic geometry

```
slit_px = SLIT_MM * M * 1000 / PIXEL_SIZE          # sensor pixels of the slit
strip   = min(ROI_width, slit_px) * PIXEL_SCALE / M
step    = strip * (1 - MOSAIC_OVERLAP)
margin  = max(MOSAIC_MARGIN_ARCSEC, uncertainty)
rows    = 1                                              if strip >= diameter
rows    = ceil((diameter + 2*margin - strip) / step) + 1 otherwise
```

`M` is the slit re-imaging magnification (`MOSAIC_SLIT_IMAGE_SCALE`, measured
live when possible). Rows are placed symmetrically around the centred Dec:

```
Dec_i = Dec_centre + (i - (rows-1)/2) * step / 3600     (degrees)
```

When `strip >= diameter` one row already holds the whole disk and `rows` is
forced to 1. The plan is validated for coverage and warns if the outermost rows
do not reach the limb. A margin row that finds no disk is skipped; if that
happens after data has been captured it is treated as the trailing edge and the
mosaic ends. Before each row the mount is rewound more than a disk diameter in
RA so the forward-only limb search starts on the leading side.

### 9.6 Post-processing and acceptance

The recommended pipeline is:

```
SER (rNN_sNN) → recon (JSol'Ex: common geometry, autocrop off)
              → stack per row (tools/align_stack_recon.py)
              → stitch rows   (tools/mosaic_from_recon.py)
```

Force the **same geometry** in JSol'Ex for every frame (ellipse X/Y, rotation,
distortion polynomial, stacking tile 128, input = 16-bit recon, not stretched
PNG); otherwise the frames come out at different scales and the mosaic has to
rescale. The mosaic tool composes by **translation only** and corrects the recon
anisotropy (stretch Y) first; it does not rescale panels by default, because a
rescale adds a second interpolation on top of an already anisotropic recon. Use
`--auto-scale` only as a last resort.

The acceptance criteria in §5 summarise what to check; §6 maps symptoms to
causes and actions.

### 9.7 Capture edges: leading pre-load, adaptive stop and per-row duration

Three details keep the recorded SER close to the disk:

- **Leading pre-load** (`SCAN_PRELOAD_SECONDS`): a short forward move before the
  capture starts, to take up the RA backlash left by the reverse padding backup.
  Without it the first frames are recorded while the mount is still stationary,
  which shows up as a duplicated strip at the leading edge of the recon.
- **Adaptive trailing stop** (`TRAILING_LIMB_STOP`): the scan watches the live
  frame's stddev and finishes `TRAILING_MARGIN_ARCSEC` after the trailing limb
  leaves the slit, instead of always recording the full trailing padding. The
  time cap still applies if the live stream stops during the capture.
- **Per-row duration**: a mosaic row is sized to the **chord** at its Dec
  (`expected_chord_arcsec`), not to the full diameter, so outer rows do not
  record the missing part of the diameter as empty sky.

---

## 10. References

- `nightscribe-scan.py` — capture script (config block, `scan_once()`,
  `calibrate_scan_rate()`, `measure_slit_scale()`).
- `nightscribe-scan.cfg` (ready) / `docs/nightscribe-scan.cfg.example` / `docs/nightscribe-scan.cfg.reference` — configuration.
- `nightscribe-ui.html` — control panel served by the script (profiles, config, start/stop).
- `tools/build_ui.py` — regenerates the panel's embedded schema.
- `nightscribe-profiles.json` — profiles saved by the panel.
- `tools/align_stack_recon.py`, `tools/mosaic_from_recon.py` — stacking and mosaic.
- `tools/_imgio.py` — image I/O shared by the tools.
- SHGScan (Patrick Hsieh) — <https://github.com/FlankerOneTwo/SHGScan>.
- Upstream lineage — <https://github.com/bennich/sharpcap-solar-scanning>.
