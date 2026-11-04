# NightScribe-Scan

NightScribe-Scan is a capture script for a fast-burst **spectroheliograph
(SHG)**. It runs inside the [SharpCap](https://www.sharpcap.co.uk/) scripting
console and does all the measurement work on the live video stream — no
temporary files, no PNG round-trips. It is a new development based on Patrick
Hsieh's (FlankerOneTwo) [SHGScan](https://github.com/FlankerOneTwo/SHGScan),
keeping its event-driven architecture and adding self-calibration, live
geometry measurement, capture-integrity checks and session bookkeeping.

Two capture modes:

- **Single scan** (`ENABLE_MOSAIC = False`) — one strip per cycle.
- **Multi-row mosaic** (`ENABLE_MOSAIC = True`) — full-disk coverage with
  automatic chord-based Dec centring.

The script only captures. Stacking the scans of a row and stitching the rows
are done outside SharpCap, either with JSol'Ex or with the two tools included
here.

## What it does

- **Self-calibrates the scan rate** at session start from the measured
  limb-crossing time, so the X sampling is 1:1 even if the focal length or the
  reported frame rate are not exact.
- **Computes the apparent solar diameter** from the date (ephemeris, ~0.01%)
  instead of a fixed value, and records the UTC of every scan.
- **Measures the slit-axis plate scale live** and uses it for the mosaic step.
- **Monitors the scan while it runs**: abortable sleep, capture-stall
  detection, free-disk guard and frame-loss accounting, with one retry.
- **Cross-scan consistency checks** on the rate and the slit extent.
- **Writes a scan log and session metadata** after every scan
  (`scan_log.csv`, `scan_stats.jsonl`, `session_meta.json`).
- **Built-in HTML control panel** for profiles, configuration and
  start/stop (`nightscribe-ui.html`, served by the script).
- **Optional focus aid** (`nightscribe-focus.py`): a separate utility with a
  live panel for the spectrograph, collimator and telescope focus.
- **Two post-processing tools**: `tools/align_stack_recon.py` (stack one row)
  and `tools/mosaic_from_recon.py` (stitch the rows).

## Requirements

- SharpCap 4.1 or later (IronPython 3.4), with a camera and mount connected.
- An ASCOM mount that reports RA/Dec and accepts `MoveAxis`.
- A capture folder with free space (checked before each scan).
- For the post-processing tools: Python 3.10+, `numpy` and `opencv-python`
  (optional: `astropy` for FITS, `tifffile` for TIF).

## Quick start

1. Edit `nightscribe-scan.cfg` (shipped ready to use): you normally only need
   to set `focal_length` and `pixel_size` under `[optics]` (plus the `[mosaic]`
   keys if you capture a full-disk mosaic); every other setting has a default.
   The complete list is in `docs/nightscribe-scan.cfg.reference`.
2. Connect the camera and mount in SharpCap, start Live View and set the ROI.
3. Load `nightscribe-scan.py` in the SharpCap scripting console and run it. It
   opens the control panel at <http://127.0.0.1:8765/> by default; use it to
   Start/Stop. Set `START_UI = False` to run a capture directly instead.
4. Read the startup banner: check the expected disk diameter, the scan-rate
   calibration and (in mosaic mode) the row plan.
5. When the session finishes, the SER files plus `scan_log.csv`,
   `scan_stats.jsonl` and `session_meta.json` are left in the capture folder.

## Documentation

- User guide (English): [docs/USER_GUIDE.md](docs/USER_GUIDE.md)
- Guía de usuario (español): [docs/GUIA_USUARIO.md](docs/GUIA_USUARIO.md)
- Configuration template: [docs/nightscribe-scan.cfg.example](docs/nightscribe-scan.cfg.example)
- Full configuration reference: [docs/nightscribe-scan.cfg.reference](docs/nightscribe-scan.cfg.reference)
- Focus aid: `nightscribe-focus.py` (see the user guide)
- Post-processing tools: [tools/](tools/)

The user guide covers the quick start, every feature and, in its *Deep dive*
section, the geometry and the reasons behind the design.

## Development

The test suite runs outside SharpCap by stubbing the SharpCap API (see
`tests/conftest.py` and `tests/sharpcap_stub.py`):

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```

The fixtures in `tests/fixtures/` are real session files used as regression
data — for example the `scan_log.csv` that exposed the scan-rate calibration
bug (`actual_xy` ≈ 4.1 instead of ≈ 1).

## Credits

NightScribe-Scan is based on the **SHGScan** project by Patrick Hsieh
(FlankerOneTwo): <https://github.com/FlankerOneTwo/SHGScan>. The event-driven
measurement approach (`FrameCaptured` + `Frame.GetStats()` + `Frame.CutROI`)
comes from there; everything else is NightScribe's own. The wider upstream
lineage also includes <https://github.com/bennich/sharpcap-solar-scanning>.

## License

MIT — see [LICENSE](LICENSE).
