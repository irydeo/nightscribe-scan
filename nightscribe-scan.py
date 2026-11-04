# NightScribe — solar SHG fast-burst scanner
# Event-driven limb/Dec/exposure detection via GetStats on live frames.
# No mode switching, no PNG round-trip, no temp files.
#
# NightScribe is a new development based on Patrick Hsieh's (FlankerOneTwo)
# SHGScan project:
#     https://github.com/FlankerOneTwo/SHGScan
# It keeps the event-driven architecture (MONO16 throughout, a FrameCaptured
# handler on the live stream, Frame.GetStats().Item2 for the limb signal and
# Frame.CutROI for sub-region measurements) and adds the geometry, calibration
# and session bookkeeping described below. A single-scan mode is available with
# ENABLE_MOSAIC = False.
#
# What NightScribe adds over SHGScan:
#   - self-calibration of the scan rate at session start (uses the solar
#     diameter and the measured limb-crossing time, so the X sampling no
#     longer depends on trusting FOCAL_LENGTH and the reported FPS);
#   - the apparent solar diameter is computed from the date (ephemeris), not
#     hard-coded, and recorded per scan with a UTC timestamp;
#   - the scan is monitored while it runs: abortable sleep, capture-stall
#     detection, free-disk guard and frame-loss accounting, with one retry;
#   - cross-scan consistency checks (rate, X/Y and slit extent);
#   - the scan log is written after every scan and a session_meta.json records
#     the effective configuration, the calibration and the mosaic plan;
#   - the mosaic plan is validated (coverage + measured slit scale) and each
#     row records its measured Dec offset, not just the nominal one.
#
# The chord method, the geometry and the reasoning behind the design are
# explained in the user guide's "Deep dive" section.
#
# User guide: docs/USER_GUIDE.md (English) / docs/GUIA_USUARIO.md (Spanish).
#
# Configuration lives in nightscribe-scan.cfg (INI, next to this file): edit it
# by hand or with the built-in HTML control panel. This single script is the
# only entry point: with START_UI = True (default) it serves the panel
# (nightscribe-ui.html) and starts/stops the capture; with START_UI = False it
# runs a capture immediately. Nothing else needs editing to change a setting.
#
# The event-driven measurement approach (FrameCaptured handler, GetStats().Item2
# for the limb signal, Frame.CutROI for sub-regions) is the core idea borrowed
# from SHGScan; everything else here is NightScribe's own.
#

import time
import math
import os
import json
import socket
import clr
# SharpCap runs IronPython, which can import .NET types via `clr`. We need
# System.Drawing.Rectangle because that's the type SharpCap's Frame.CutROI
# expects when picking a sub-region of a frame.
clr.AddReference("System.Drawing")
from System.Drawing import Rectangle

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — everything you may need to change lives in this block.
#
# Usual changes: FOCAL_LENGTH (optics), ENABLE_CAPTURE / NUM_CYCLES (session),
# ENABLE_MOSAIC / MOSAIC_SLIT_LENGTH_MM / MOSAIC_SCANS_PER_ROW (mosaic).
# Rationale, geometry and recommended values: the user guide (Deep dive).
# ══════════════════════════════════════════════════════════════════════════════

# ── 0. Run mode ───────────────────────────────────────────────────────────────
# True  = start the built-in HTML control panel (open http://127.0.0.1:8765/
#         to Start/Stop the capture). This is the default single entry point.
# False = run the capture immediately, without the panel.
START_UI = True

# ── 1. Optics and mount ───────────────────────────────────────────────────────
# Together these define the image scale: arcsec/pixel = 206265 * (PIXEL_SIZE/1000) / FOCAL_LENGTH.
# SOLAR_DIAMETER varies through the year by ~3% — 1920" is a safe annual mean.
# SIDEREAL is the ASCOM unit conversion: a "1.0x" mount slew = 15.04"/s of axis motion.
#
# FOCAL_LENGTH must be the EFFECTIVE focal length at the sensor, not the
# nominal one on the tube. It is the single most important number in this
# script: it sets the scan rate. If it is too large the mount scans too slowly
# and the recon comes out stretched in X (and vice versa).
#
# How to measure it (2026-11-01, G3M678M, ROI 3480x248, MLAStro SHG-700):
#   solar diameter in the NATIVE recon (before any resampling) ... ~3620 px
#   apparent solar diameter on that date ........................ ~1930"
#   scale = 1930 / 3620 ........................................ 0.533"/px
#   FL = 206265 * 0.002 / 0.533 ................................ ~774 mm
# The nominal 714 mm is within 8 %, which is the right ballpark. The value
# 969 mm that JSol'Ex uses is a PROCESSING scale (JSol'Ex resamples its output
# to that plate scale), NOT the physical focal length for the scan rate. Do
# not copy it here: at 969 the scan would run ~36 % too slow.
#
# If you change optics, measure the disk diameter in the NATIVE recon (not in
# a JSol'Ex stack, which is resampled) and recompute:
#   FL = 206265 * PIXEL_SIZE / 1000 * D_px / D_arcsec
# The startup banner prints the expected disk diameter for a sanity check.
FOCAL_LENGTH       = 714     # mm (effective, native recon; not JSol'Ex 969)
PIXEL_SIZE         = 2       # um (G3M678M)
SIDEREAL           = 15.04   # arcsec/s per sidereal multiplier unit

# Apparent solar diameter. It changes by ~3% over the year, so NightScribe
# computes it from the date (low-precision ephemeris, good to ~0.01%) and
# records the UTC time of every scan so the exact value can be recomputed in
# post-processing. Set USE_EPHEMERIS = False to use the fixed fallback, or set
# EPHEMERIS_DIAMETER_OVERRIDE > 0 to force a value.
USE_EPHEMERIS               = True
EPHEMERIS_DIAMETER_OVERRIDE = 0.0    # arcsec; 0 = compute
SOLAR_DIAMETER              = 1920.0 # arcsec (fallback / when ephemeris off)


# ── Apparent solar diameter from the date (low-precision ephemeris) ───────────
# Good to ~0.01%, far better than the ~3% annual variation. The exact UTC of
# every scan is recorded so the value can be recomputed in post-processing.
def _julian_date(y, mo, d, h, mi, s):
    """Julian date from UTC calendar components (Gregorian)."""
    if mo <= 2:
        y -= 1
        mo += 12
    a = y // 100
    b = 2 - a + a // 4
    jd = (int(365.25 * (y + 4716)) + int(30.6001 * (mo + 1)) + d + b
          - 1524.5)
    return jd + (h + mi / 60.0 + s / 3600.0) / 24.0


def _utc_components():
    """(y, mo, d, h, mi, s) in UTC, or None if unavailable."""
    try:
        t = time.gmtime()
        return (t.tm_year, t.tm_mon, t.tm_mday,
                t.tm_hour, t.tm_min, t.tm_sec)
    except:
        pass
    try:
        import System
        n = System.DateTime.UtcNow
        return (n.Year, n.Month, n.Day, n.Hour, n.Minute, n.Second)
    except:
        return None


def ephemeris_solar_diameter():
    """Apparent solar diameter in arcsec for 'now', or 0.0 if unavailable."""
    c = _utc_components()
    if c is None:
        return 0.0
    jd = _julian_date(*c)
    # Mean anomaly of the Earth and the Earth-Sun distance in AU.
    g = math.radians((357.5291 + 0.98560028 * (jd - 2451545.0)) % 360.0)
    r = 1.00014 - 0.01671 * math.cos(g) - 0.00014 * math.cos(2.0 * g)
    # 959.63" is the solar semidiameter at 1 AU.
    return 2.0 * 959.63 / r


_EPHEMERIS_DIAMETER = ephemeris_solar_diameter() if USE_EPHEMERIS else 0.0
if EPHEMERIS_DIAMETER_OVERRIDE > 0:
    SOLAR_DIAMETER = EPHEMERIS_DIAMETER_OVERRIDE
elif _EPHEMERIS_DIAMETER > 0:
    SOLAR_DIAMETER = _EPHEMERIS_DIAMETER

# ── 2. Session and scanning ───────────────────────────────────────────────────
# RETURN_MULTIPLIER and LIMB_SEARCH_SPEED below are sidereal multipliers — the
# value passed to mount.MoveAxis(). 32x sidereal ≈ 481"/s, fast enough to make
# repositioning between cycles negligible without exceeding most mount limits.
ENABLE_CAPTURE     = True
ENABLE_AUTO_EXP    = True
NUM_CYCLES         = 1
PADDED_DURATION    = 3       # seconds of sky captured before/after the disk
# If > 0, overrides PADDED_DURATION with a FIXED amount of sky on each side of
# the disk. A fixed arcsec padding is independent of the FPS and therefore
# makes repeated scans more repeatable. 100" is a good default; set to 0 to
# keep the historical seconds-based behaviour.
PADDED_ARCSEC      = 100.0   # arcsec of sky padding on each side

# Forward pre-load before the capture starts: takes up the RA gear backlash
# left by the reverse backup, so the first recorded frames are not a stationary
# (duplicated) strip at the leading edge of the recon. 0 disables it.
SCAN_PRELOAD_SECONDS = 0.3

# Adaptive stop: finish the capture shortly after the trailing limb leaves the
# slit (stddev drop), instead of always recording the full trailing padding.
TRAILING_LIMB_STOP       = True
TRAILING_MARGIN_ARCSEC   = 50.0   # sky kept after the trailing limb
TRAILING_STDDEV_FRACTION = 0.15   # threshold vs the peak stddev
RETURN_MULTIPLIER  = 32.0    # fast slew rate (no capture)

# ── 2b. Capture timing (FPS) ──────────────────────────────────────────────────
# The scan rate is PIXEL_SCALE * fps / SIDEREAL. Reading the FPS once at script
# start is wrong: auto-exposure changes the exposure time, and with it the
# frame rate. On the 2026-11-01 run the preview ran at ~154 fps while the
# capture was written at 121.56 fps, so the mount scanned 27% too fast and the
# recon came out elliptical (X/Y = 0.79). The rate is now re-measured AFTER the
# exposure is final.
#
# CAPTURE_FPS > 0 forces a value and skips measurement (use it when SharpCap's
# reported rate is unreliable and you know the real one).
CAPTURE_FPS          = 0.0   # frames/s; 0 = measure
FPS_SAMPLES          = 5     # readings of CurrentFrameRate to median
FPS_SAMPLE_INTERVAL  = 0.2   # seconds between readings
# > 0 = additionally measure from a real capture of this length (writes a
# small SER labelled *_fpsprobe that you can delete). 0 = use CurrentFrameRate.
FPS_PROBE_SECONDS    = 0.0
# Wait for cam.Capturing / CapturedFrameCount before moving the mount, so the
# scan starts with the file already recording instead of losing the first
# frames (and with them the leading limb).
CAPTURE_START_TIMEOUT = 5.0  # seconds
CAPTURE_START_SETTLE  = 0.10 # seconds after the first frame before moving
# While the scan runs, abort if SharpCap stops writing frames for this long or
# if the capture flag drops. 0 disables the stall check.
CAPTURE_STALL_SECONDS = 3.0
# Warn (and record it) if the frames actually written are more than this
# fraction below scan_duration * fps. A short scan means dropped frames.
FRAMES_SHORT_TOL      = 0.10
# Refuse to start a scan if the capture drive has less than this many GB free.
# The scan itself is checked against its own estimate with 20% headroom.
CAPTURE_MIN_FREE_GB   = 1.0

# ── 2c. Per-scan logging ──────────────────────────────────────────────────────
# CSV with one row per scan: FPS used vs measured, frames, duration, Dec error.
# It is rewritten after every scan, so a crash mid-session still leaves the
# scans done so far. Empty = try SharpCap.CaptureFolder; if that fails, use the
# current directory.
SCAN_LOG_PATH        = ""
# JSON with the effective configuration and the mosaic plan, for reproducing a
# session. Empty = next to the scan log.
SESSION_META_PATH    = ""
DEC_DRIFT_TOL_ARCSEC = 10.0  # recentre Dec between scans if it drifts more

# ── 3. Limb detection (stddev-based) ──────────────────────────────────────────
LIMB_SEARCH_SPEED  = 4.0     # forward search multiplier
LIMB_SAMPLE_INTERVAL = 0.05  # how often to poll the watcher (s)
LIMB_MAX_SEARCH    = 120     # seconds before giving up
# Absolute stddev floor that counts as "on disk". At MONO16, sensor dark
# noise stddev is ~20-100, on-disk stddev is typically 10000+.
LIMB_STDDEV_MIN    = 500
# Stddev above this = clearly on disk (used to decide whether to back off first).
ON_DISK_STDDEV     = 2000
# Dynamic threshold multiplier over observed dark baseline.
LIMB_DARK_MULT     = 5.0

# ── 4. Dec centering (legacy CutROI left/right mean) ─────────────────────────
# MASTER SWITCH — single scan vs mosaic.
#
#   True  = use the left/right (CutROI) Dec-centering loop.
#   False = never move the Dec axis during alignment. Limb search, exposure,
#           scan and return are unchanged; only center_dec() is skipped.
#
# Set it to False when the solar disk does not fit along the slit axis in the
# live frame (roughly FOCAL_LENGTH > 825 mm with a full 3840-px sensor and
# PIXEL_SIZE = 2 um, or proportionally less with a cropped ROI), or when the
# slit is already centred in Dec. In that regime the left/right metric loses
# its zero-crossing and the loop can push the Sun out of the slit.
#
# This switch applies to the single-sequence path (ENABLE_MOSAIC = False). The
# mosaic path has its own MOSAIC_AUTO_CENTER_DEC switch, because it can centre
# reliably even when the disk overfills the slit. See docs/USER_GUIDE.md, 9.4.
ENABLE_DEC_CENTERING = True

DEC_INTERVAL       = 2       # re-center every N cycles
DEC_CENTER_TOLERANCE = 0.03  # (L-R)/(L+R) within this = centered
DEC_MAX_CORRECTIONS = 8
DEC_NUDGE_SPEED    = 3.0
DEC_SETTLE_TIME    = 0.3

# ── 5. Mosaic (optional) ──────────────────────────────────────────────────────
# 5a. Geometry and session
# ENABLE_MOSAIC = False -> single-scan behaviour (one strip per cycle).
# ENABLE_MOSAIC = True  -> multi-row mosaic with automatic Dec centring.
# The script only captures; stacking and stitching are done outside SharpCap.
ENABLE_MOSAIC          = False

# Physical slit length in mm. This — not the sensor — is the hard limit on the
# spatial axis. MLAStro SHG-700 ships with 7 mm (10 mm upgrade available).
MOSAIC_SLIT_LENGTH_MM  = 7.0

# Sensor pixels per mm of slit (spectrograph re-imaging magnification).
# 1.0 assumes 1:1 re-imaging. If the slit occupies a different pixel count,
# set this to slit_pixels * PIXEL_SIZE / (slit_mm * 1000).
#
# IMPORTANT: the mosaic row step is a Dec displacement, which lands on the SLIT
# axis (Y). The slit axis does not necessarily have the same plate scale as the
# scan axis (X): in the 2026-11 reference data the reconstructed disk is ~1.28x
# wider than tall, i.e. the slit axis is compressed. Measure it once from a
# recon (X_diameter / Y_diameter of the disk, both in the same image) and set
# this factor so effective_slit_arcsec() and the row step are in the same units
# as the scan axis. With the factor at 1.0 the row step will be off by that
# ratio and the rows will not tile correctly.
MOSAIC_SLIT_IMAGE_SCALE = 1.0

MOSAIC_OVERLAP         = 0.15   # fractional overlap between adjacent rows
MOSAIC_MARGIN_ARCSEC   = 100.0  # extra sky above/below the disk
MOSAIC_SCANS_PER_ROW   = 5      # full scans per row, each a separate SER

# 5b. Automatic Dec centring by the chord method
# False = the slit was centred by hand; skip the calibration scans.
MOSAIC_AUTO_CENTER_DEC = True

MOSAIC_CENTER_MAX_ITER = 3      # measurement/probe/correction rounds
# The chord cannot resolve |offset| < S/2 (dead zone). 0.5R is ~3 sigma away
# from "full diameter" at 50" of chord noise; the walk step stays below it.
MOSAIC_CENTER_TRUST_ARCSEC = 0.5 * (SOLAR_DIAMETER / 2.0)
MOSAIC_CENTER_STEP_ARCSEC  = 0.3 * (SOLAR_DIAMETER / 2.0)
# Residual assumed after a measured correction; used as extra plan margin.
MOSAIC_CENTER_RESIDUAL_ARCSEC = 300.0
MOSAIC_MIN_CHORD_ARCSEC = 0.15 * SOLAR_DIAMETER  # below this, no usable disk

# 5c. Dec movement between rows and during calibration
MOSAIC_DEC_SPEED       = 3.0    # sidereal multiplier (3x ~= 45"/s)
MOSAIC_DEC_BACKLASH    = 30.0   # arcsec taken up before each final approach
MOSAIC_DEC_SETTLE      = 0.5    # seconds to settle after a Dec move
MOSAIC_CHORD_SPEED     = 16.0   # sidereal multiplier for calibration traverses

# ── 6. Auto-exposure (histogram percentile) ───────────────────────────────────
# Drive the specified percentile of the histogram to a target fraction of full
# scale. Prevents continuum-peak clipping that a mean-based target can't see.
# 0.995 = look at the 99.5th percentile (ignores a few hot pixels).
# 0.88  = aim for that percentile at 88% of full scale (leaves headroom).
EXP_PERCENTILE     = 0.995
EXP_TARGET_LEVEL   = 0.88
EXP_TOLERANCE      = 0.04
EXP_MAX_ATTEMPTS   = 6

# ── 7. Calibration, consistency and bookkeeping (NightScribe) ─────────────────
# Self-calibrate the scan rate at session start from the measured limb-crossing
# time and the (ephemeris) solar diameter. This removes the dependence on
# trusting FOCAL_LENGTH and the reported FPS for the X sampling: the script
# measures how long the Sun takes to cross the slit and scales the mount rate so
# that the sky moves exactly PIXEL_SCALE per frame.
CALIBRATE_SCAN_RATE    = True
CALIBRATION_SAMPLES    = 1      # traverses to average (>= 1)
# Cross-scan consistency: warn if the measured X/Y or the slit extent drifts by
# more than this fraction between scans.
CONSISTENCY_TOL_XY     = 0.03
CONSISTENCY_TOL_EXTENT = 0.03
# Retry a scan that failed (stall / short / limb lost) this many times before
# giving up on it. The failed attempt is kept in the log with a `problem` tag.
SCAN_RETRIES           = 1
# Apply the 1/cos(Dec) RA-rate correction in BOTH modes (mosaic and single), so
# the X scale is the same at any declination. Set False for the plain
# single-scan motion (no cos(Dec) correction).
COS_DEC_CORRECTION     = True
# Measure the illuminated extent along the slit axis in the live frame. It is
# used to estimate the slit-axis plate scale (and therefore the mosaic step) and
# to flag a scan whose slit missed the disk.
MEASURE_SLIT_EXTENT    = True
SLIT_EXTENT_BANDS      = 48
# Per-scan statistics (JSON lines). Empty = next to the scan log.
SCAN_STATS_PATH        = ""


# ══════════════════════════════════════════════════════════════════════════════
# EXTERNAL CONFIGURATION — nightscribe-scan.cfg
# ══════════════════════════════════════════════════════════════════════════════
# The constants above are the DEFAULTS. If a nightscribe-scan.cfg file is found
# (next to the script, in the working directory, or at $NIGHTSCRIBE_CONFIG) its
# values override them. The file is plain INI text:
#
#     [optics]
#     focal_length = 714      # effective focal length (mm)
#
# Keys are the constant names in lower case; lines starting with # are ignored;
# missing keys keep the default. This is what the HTML UI edits, so the user
# never has to touch this script.
CONFIG_FILE_NAME = "nightscribe-scan.cfg"

# section -> [(CONSTANT_NAME, help)]. Drives the UI form and the file template.
CONFIG_SCHEMA = [
    ("optics", [
        ("FOCAL_LENGTH", "Effective focal length at the sensor (mm)."),
        ("PIXEL_SIZE", "Camera pixel size (um)."),
        ("SIDEREAL", "Arcsec/s per sidereal multiplier unit (15.04)."),
        ("USE_EPHEMERIS", "Compute the solar diameter from the date."),
        ("EPHEMERIS_DIAMETER_OVERRIDE", "Force the solar diameter (arcsec); 0 = auto."),
        ("SOLAR_DIAMETER", "Fallback solar diameter (arcsec) when no ephemeris."),
    ]),
    ("scan", [
        ("ENABLE_CAPTURE", "Write SER files (False = dry run)."),
        ("ENABLE_AUTO_EXP", "Run the auto-exposure loop."),
        ("NUM_CYCLES", "Cycles in the single-scan mode."),
        ("PADDED_DURATION", "Seconds of sky padding per side (if PADDED_ARCSEC=0)."),
        ("PADDED_ARCSEC", "Fixed arcsec padding per side (0 = use PADDED_DURATION)."),
        ("SCAN_PRELOAD_SECONDS", "Forward pre-load (s) before capturing (0 = off)."),
        ("TRAILING_LIMB_STOP", "Stop the capture after the trailing limb."),
        ("TRAILING_MARGIN_ARCSEC", "Sky kept after the trailing limb (arcsec)."),
        ("TRAILING_STDDEV_FRACTION", "Trailing threshold vs the peak stddev."),
        ("RETURN_MULTIPLIER", "Fast slew multiplier for repositioning."),
    ]),
    ("fps", [
        ("CAPTURE_FPS", "Force the capture FPS; 0 = measure."),
        ("FPS_SAMPLES", "Readings of CurrentFrameRate to median."),
        ("FPS_SAMPLE_INTERVAL", "Seconds between FPS readings."),
        ("FPS_PROBE_SECONDS", "Measure the real capture FPS for this many seconds; 0 = off."),
        ("CAPTURE_START_TIMEOUT", "Seconds to wait for the capture to start."),
        ("CAPTURE_START_SETTLE", "Seconds to settle after the first frame."),
        ("CAPTURE_STALL_SECONDS", "Abort if no frames are written for this long."),
        ("FRAMES_SHORT_TOL", "Warn if this fraction of frames is missing."),
        ("CAPTURE_MIN_FREE_GB", "Minimum free disk space to start a scan (GB)."),
    ]),
    ("limb", [
        ("LIMB_SEARCH_SPEED", "Multiplier for the limb search."),
        ("LIMB_SAMPLE_INTERVAL", "Seconds between limb samples."),
        ("LIMB_MAX_SEARCH", "Seconds before giving up the limb search."),
        ("LIMB_STDDEV_MIN", "Minimum stddev to call it on-disk."),
        ("ON_DISK_STDDEV", "Stddev that clearly means on-disk."),
        ("LIMB_DARK_MULT", "Threshold = dark stddev x this."),
    ]),
    ("dec", [
        ("ENABLE_DEC_CENTERING", "Use the left/right Dec centring loop."),
        ("DEC_INTERVAL", "Re-centre every N cycles."),
        ("DEC_CENTER_TOLERANCE", "Centred when |L-R|/(L+R) is below this."),
        ("DEC_MAX_CORRECTIONS", "Max Dec nudges per centring."),
        ("DEC_NUDGE_SPEED", "Dec nudge multiplier."),
        ("DEC_SETTLE_TIME", "Seconds to settle after a nudge."),
        ("DEC_DRIFT_TOL_ARCSEC", "Re-centre Dec between scans if it drifts more."),
    ]),
    ("mosaic", [
        ("ENABLE_MOSAIC", "True = multi-row mosaic; False = single scan."),
        ("MOSAIC_SLIT_LENGTH_MM", "Physical slit length (mm)."),
        ("MOSAIC_SLIT_IMAGE_SCALE", "Slit re-imaging magnification (measured live if possible)."),
        ("MOSAIC_OVERLAP", "Fractional overlap between rows."),
        ("MOSAIC_MARGIN_ARCSEC", "Extra sky above/below the disk (arcsec)."),
        ("MOSAIC_SCANS_PER_ROW", "Scans per row."),
        ("MOSAIC_AUTO_CENTER_DEC", "Auto Dec centring by chords."),
        ("MOSAIC_CENTER_MAX_ITER", "Chord centring iterations."),
        ("MOSAIC_CENTER_RESIDUAL_ARCSEC", "Assumed residual after centring."),
        ("MOSAIC_DEC_SPEED", "Dec move multiplier between rows."),
        ("MOSAIC_DEC_BACKLASH", "Backlash taken up before each approach (arcsec)."),
        ("MOSAIC_DEC_SETTLE", "Seconds to settle after a Dec move."),
        ("MOSAIC_CHORD_SPEED", "Multiplier for chord/calibration traverses."),
    ]),
    ("exposure", [
        ("EXP_PERCENTILE", "Histogram percentile to target (0.995)."),
        ("EXP_TARGET_LEVEL", "Target level as a fraction of full scale."),
        ("EXP_TOLERANCE", "Convergence window as a fraction of full scale."),
        ("EXP_MAX_ATTEMPTS", "Max auto-exposure iterations."),
    ]),
    ("calibration", [
        ("CALIBRATE_SCAN_RATE", "Self-calibrate the scan rate at session start."),
        ("CALIBRATION_SAMPLES", "Calibration traverses to average."),
        ("CONSISTENCY_TOL_XY", "Warn if X/Y drifts more than this between scans."),
        ("CONSISTENCY_TOL_EXTENT", "Warn if the slit extent drifts more than this."),
        ("SCAN_RETRIES", "Retries per failed scan."),
        ("COS_DEC_CORRECTION", "Apply 1/cos(Dec) in both modes."),
        ("MEASURE_SLIT_EXTENT", "Measure the slit extent in a live frame."),
        ("SLIT_EXTENT_BANDS", "Bands for the slit extent profile."),
    ]),
    ("logging", [
        ("SCAN_LOG_PATH", "CSV path; empty = capture folder."),
        ("SCAN_STATS_PATH", "JSONL path; empty = capture folder."),
        ("SESSION_META_PATH", "Session JSON path; empty = capture folder."),
    ]),
]


def config_search_paths():
    """Where to look for nightscribe-scan.cfg, in order."""
    paths = []
    try:
        env = os.environ.get("NIGHTSCRIBE_CONFIG")
    except:
        env = None
    if env:
        paths.append(env)
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        here = None
    if here:
        paths.append(os.path.join(here, CONFIG_FILE_NAME))
    try:
        paths.append(os.path.join(os.getcwd(), CONFIG_FILE_NAME))
    except:
        pass
    return paths


def _parse_ini(text):
    """Parse the simple INI text into {(section, key): value_string}."""
    out = {}
    section = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        sep = "=" if "=" in line else (":" if ":" in line else None)
        if sep is None:
            continue
        key, value = line.split(sep, 1)
        out[(section, key.strip().lower())] = value.strip()
    return out


def _coerce(value, template):
    """Convert a config string to the type of the default value."""
    if isinstance(template, bool):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(template, int) and not isinstance(template, bool):
        return int(float(value))
    if isinstance(template, float):
        return float(value)
    return value


def _apply_values(values):
    """Apply {CONSTANT_NAME: value} to the module globals, coercing the type.

    Used by Start (so the capture runs with exactly what the panel shows) and
    by Save (so the file and the live values stay in agreement).
    """
    if not values:
        return
    for section, entries in CONFIG_SCHEMA:
        for name, _help in entries:
            if name in values and name in globals():
                try:
                    globals()[name] = _coerce(str(values[name]),
                                              globals()[name])
                except:
                    pass


def apply_config_file(verbose=True):
    """Override the module constants from nightscribe-scan.cfg (if present)."""
    global _EPHEMERIS_DIAMETER, SOLAR_DIAMETER
    path = None
    for candidate in config_search_paths():
        if candidate and os.path.isfile(candidate):
            path = candidate
            break
    if path is None:
        if verbose:
            print("Config: no {} found; using defaults.".format(CONFIG_FILE_NAME))
            print("        A ready {} ships with the project; see docs/{}.example "
                  "to restore it.".format(CONFIG_FILE_NAME, CONFIG_FILE_NAME))
        return None
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        # The config may have been written by the panel in the system encoding
        # (cp1252 on Windows); decode tolerantly so a stray non-ASCII byte in a
        # comment cannot make the whole file unreadable.
        try:
            text = raw.decode("utf-8")
        except Exception:
            text = raw.decode("latin-1")
        data = _parse_ini(text)
    except Exception as e:
        print("Config: could not read {} ({})".format(path, e))
        return None

    applied, unknown = 0, []
    known = set()
    g = globals()
    for section, entries in CONFIG_SCHEMA:
        for name, _help in entries:
            known.add((section, name.lower()))
            key = (section, name.lower())
            if key not in data:
                continue
            if name not in g:
                continue
            try:
                g[name] = _coerce(data[key], g[name])
                applied += 1
            except Exception as e:
                print("Config: bad value for {}.{} ({}); kept default".format(
                    section, name.lower(), e))
    for key in data:
        if key not in known:
            unknown.append("{}.{}".format(key[0], key[1]))
    if verbose:
        print("Config: loaded {} ({} value(s){}).".format(
            path, applied, ", unknown: " + ", ".join(unknown) if unknown else ""))
    return path


# Apply the external config, then refresh the values that derive from it.
# Keep a copy of the built-in defaults first (the UI uses it for "new profile").
CONFIG_DEFAULTS = {}
for _section, _entries in CONFIG_SCHEMA:
    for _name, _help in _entries:
        if _name in globals():
            CONFIG_DEFAULTS[_name] = globals()[_name]

apply_config_file()
_EPHEMERIS_DIAMETER = ephemeris_solar_diameter() if USE_EPHEMERIS else 0.0
if EPHEMERIS_DIAMETER_OVERRIDE > 0:
    SOLAR_DIAMETER = EPHEMERIS_DIAMETER_OVERRIDE
elif _EPHEMERIS_DIAMETER > 0:
    SOLAR_DIAMETER = _EPHEMERIS_DIAMETER
MOSAIC_CENTER_TRUST_ARCSEC = 0.5 * (SOLAR_DIAMETER / 2.0)
MOSAIC_CENTER_STEP_ARCSEC = 0.3 * (SOLAR_DIAMETER / 2.0)
MOSAIC_MIN_CHORD_ARCSEC = 0.15 * SOLAR_DIAMETER

# ══════════════════════════════════════════════════════════════════════════════
# DERIVED VALUES — do not edit. They follow from the configuration above.
# ══════════════════════════════════════════════════════════════════════════════

# 206265 = arcseconds per radian. Multiplied by (pixel size / focal length) in
# matching length units, this gives the angular size each pixel subtends.
PIXEL_SCALE = 206265 * (PIXEL_SIZE / 1000.0) / FOCAL_LENGTH

# `SharpCap` is a host-injected global available inside the SharpCap scripting
# console. Both are None if the user has not connected a mount/camera; fail
# immediately with a clear message instead of an AttributeError deep inside.
mount = SharpCap.Mounts.SelectedMount
cam   = SharpCap.SelectedCamera

# Fail with a useful message instead of an AttributeError deep inside the
# script. SharpCap returns None when nothing is selected/connected.
if cam is None:
    raise RuntimeError(
        "No camera selected in SharpCap. Connect/select a camera and start "
        "Live View before running this script.")
if mount is None:
    raise RuntimeError(
        "No mount selected in SharpCap. Connect/select the mount before "
        "running this script.")

# Auto-calculate scan speed from the live fps. For a 1:1 reconstruction the
# slit must traverse one pixel-width of sky per captured frame:
#   scan_speed (arcsec/s) = PIXEL_SCALE (arcsec/px) * fps (frames/s)
# and the mount multiplier is that speed divided by SIDEREAL.
#
# IMPORTANT: this first value uses the *preview* frame rate and is only used
# to print the pre-flight banner and estimate durations. The real rate is
# measured again after the exposure is final (see measure_capture_fps()) and
# applies to every scan, so all rows share one scale.
fps = cam.CurrentFrameRate

# CurrentFrameRate is 0 when the camera is open but Live View has not produced
# frames yet. Without a real FPS the scan speed and duration are meaningless.
if fps is None or fps != fps or fps <= 0:
    raise RuntimeError(
        "The camera is not delivering frames (CurrentFrameRate={}). Start "
        "Live View and wait a few seconds before running the script.".format(fps))

scan_multiplier = PIXEL_SCALE * fps / SIDEREAL
scan_speed      = scan_multiplier * SIDEREAL
crossing_time   = SOLAR_DIAMETER / scan_speed
# scan_duration includes the padding on each side of the disk, captured at the
# same scan_speed. padded_seconds is PADDED_DURATION or PADDED_ARCSEC/speed.
# This first value uses the preview FPS; recompute_scan_parameters() recalculates
# it after auto-exposure so the captured sky padding is the same at any FPS.
if PADDED_ARCSEC > 0:
    padded_seconds = PADDED_ARCSEC / scan_speed
else:
    padded_seconds = float(PADDED_DURATION)
scan_duration   = 2.0 * padded_seconds + crossing_time


def recompute_scan_parameters(fps_value, source="preview"):
    """Recompute scan rate/duration from a new FPS value.

    Called once the exposure is fixed, because auto-exposure changes the
    frame rate. Keeps one single rate for the whole session so the X scale is
    identical in every scan (that is what makes stacking work).
    """
    global fps, scan_multiplier, scan_speed, crossing_time
    global padded_seconds, scan_duration

    if fps_value is None or fps_value != fps_value or fps_value <= 0:
        raise RuntimeError(
            "Invalid frame rate ({}) from {}.".format(fps_value, source))

    fps = float(fps_value)
    scan_multiplier = PIXEL_SCALE * fps / SIDEREAL
    scan_speed      = scan_multiplier * SIDEREAL
    crossing_time   = SOLAR_DIAMETER / scan_speed
    if PADDED_ARCSEC > 0:
        padded_seconds = PADDED_ARCSEC / scan_speed
    else:
        padded_seconds = float(PADDED_DURATION)
    scan_duration = 2.0 * padded_seconds + crossing_time
    return fps


def refresh_derived():
    """Recompute the values that depend on the configuration.

    Called after a live config change (e.g. from the UI) so the derived values
    follow the new constants. Safe to call any time after the camera exists.
    """
    global PIXEL_SCALE, SOLAR_DIAMETER, _EPHEMERIS_DIAMETER
    global MOSAIC_CENTER_TRUST_ARCSEC, MOSAIC_CENTER_STEP_ARCSEC
    global MOSAIC_MIN_CHORD_ARCSEC
    PIXEL_SCALE = 206265 * (PIXEL_SIZE / 1000.0) / FOCAL_LENGTH
    _EPHEMERIS_DIAMETER = ephemeris_solar_diameter() if USE_EPHEMERIS else 0.0
    if EPHEMERIS_DIAMETER_OVERRIDE > 0:
        SOLAR_DIAMETER = EPHEMERIS_DIAMETER_OVERRIDE
    elif _EPHEMERIS_DIAMETER > 0:
        SOLAR_DIAMETER = _EPHEMERIS_DIAMETER
    MOSAIC_CENTER_TRUST_ARCSEC = 0.5 * (SOLAR_DIAMETER / 2.0)
    MOSAIC_CENTER_STEP_ARCSEC = 0.3 * (SOLAR_DIAMETER / 2.0)
    MOSAIC_MIN_CHORD_ARCSEC = 0.15 * SOLAR_DIAMETER
    try:
        v = cam.CurrentFrameRate
    except:
        v = None
    if v is not None and v == v and v > 0:
        recompute_scan_parameters(v, "refresh")
    return PIXEL_SCALE


def apply_scan_calibration(factor):
    """Scale the scan multiplier by ``factor`` and recompute derived values.

    Used by calibrate_scan_rate(): the factor is the ratio between the sky rate
    we want (PIXEL_SCALE * fps) and the rate the mount actually produced. Keeps
    one single rate for the whole session, so the X scale is identical in every
    scan.
    """
    global scan_multiplier, scan_speed, crossing_time, padded_seconds
    global scan_duration
    scan_multiplier *= factor
    scan_speed = scan_multiplier * SIDEREAL
    crossing_time = SOLAR_DIAMETER / scan_speed
    if PADDED_ARCSEC > 0:
        padded_seconds = PADDED_ARCSEC / scan_speed
    else:
        padded_seconds = float(PADDED_DURATION)
    scan_duration = 2.0 * padded_seconds + crossing_time
    return scan_multiplier


def sky_rate_per_unit():
    """Sky arcsec/s per multiplier unit: the calibrated value when known.

    The self-calibration measures the mount's real rate per unit at
    MOSAIC_CHORD_SPEED; using it (instead of the nominal SIDEREAL) makes the
    logged actual_xy reflect the true X sampling, not the mount's deviation
    from the nominal sidereal rate.
    """
    cal = _calibration[0]
    if cal.get("applied") and cal.get("measured_per_unit"):
        return cal["measured_per_unit"]
    return SIDEREAL


def _median_fps(samples=None, interval=None):
    """Median of several CurrentFrameRate readings; None if unavailable."""
    samples = samples or FPS_SAMPLES
    interval = interval if interval is not None else FPS_SAMPLE_INTERVAL
    vals = []
    for _ in range(max(1, samples)):
        check_abort()
        try:
            v = cam.CurrentFrameRate
        except:
            v = None
        if v is not None and v == v and v > 0:
            vals.append(float(v))
        time.sleep(interval)
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def _capture_frame_count():
    """Frames written so far, or None if the property is unavailable."""
    try:
        return int(cam.CapturedFrameCount)
    except:
        return None


def wait_capture_start(base_frames=None, timeout=None):
    """Block until SharpCap is actually writing frames.

    Moving the mount immediately after RunCapture() loses the first frames
    while the file is being opened. At ~50"/s that is several pixels of sky,
    and it is one of the reasons repeated scans do not start at the same RA.

    SharpCap builds differ on CapturedFrameCount: some keep it cumulative
    across captures, others reset it to 0 when a new capture starts. This
    routine accepts either: it waits for the counter to move from the value
    seen right after RunCapture(), or to exceed ``base_frames`` (the value read
    before RunCapture()).
    """
    timeout = timeout if timeout is not None else CAPTURE_START_TIMEOUT
    t0 = time.time()
    initial = None
    while (time.time() - t0) < timeout:
        check_abort()
        try:
            capturing = bool(cam.Capturing)
        except:
            capturing = False
        if capturing:
            now = _capture_frame_count()
            if now is None:
                # No counter available: trust the capture flag.
                time.sleep(CAPTURE_START_SETTLE)
                return True
            if initial is None:
                initial = now
            if now != initial:
                time.sleep(CAPTURE_START_SETTLE)
                return True
            if base_frames is not None and now > base_frames:
                time.sleep(CAPTURE_START_SETTLE)
                return True
        time.sleep(0.02)
    return False


def estimate_scan_gb():
    """Rough size of one scan in GB, or None if the ROI is unknown."""
    try:
        roi = cam.ROI
        bpp = 2 if full_scale() > 255 else 1
        return (roi.Width * roi.Height * bpp * fps * scan_duration) / 1e9
    except:
        return None


def check_capture_space(label=None):
    """Refuse a scan that clearly will not fit on the capture drive.

    Uses the SharpCap capture folder when available; if the free space cannot
    be read, it does not block (best effort).
    """
    free = free_disk_gb()
    need = estimate_scan_gb()
    if free is None:
        return True
    if need is None:
        need = 0.0
    if free < max(CAPTURE_MIN_FREE_GB, need * 1.2):
        print("  CAPTURE SPACE: only {:.1f} GB free, this scan needs ~{:.1f} GB"
              " (label {})".format(free, need, label))
        return False
    return True


def _probe_capture_fps(seconds):
    """Measure the real capture rate from a short stationary capture.

    Writes a small SER labelled *_fpsprobe; delete it after the session. This
    is the only way to be sure when the driver reports a frame rate that does
    not match what is written to disk.
    """
    if not ENABLE_CAPTURE or seconds is None or seconds <= 0:
        return None
    print("  Probing capture FPS for {:.1f}s...".format(seconds))
    set_capture_label("fpsprobe")
    base = _capture_frame_count()
    base = base if base is not None else 0
    cam.PrepareToCapture()
    cam.RunCapture()
    if not wait_capture_start(base):
        try:
            cam.StopCapture()
        except:
            pass
        return None
    t0 = time.time()
    c0 = _capture_frame_count()
    c0 = c0 if c0 is not None else base
    time.sleep(seconds)
    c1 = _capture_frame_count()
    c1 = c1 if c1 is not None else c0
    t1 = time.time()
    try:
        cam.StopCapture()
    except:
        pass
    set_capture_label("")
    dt = t1 - t0
    if dt <= 0 or c1 <= c0:
        return None
    return (c1 - c0) / dt


def measure_capture_fps(context="after exposure"):
    """Fix the scan rate using the real capture FPS.

    Priority: CAPTURE_FPS override > real capture probe > median of
    CurrentFrameRate. The value is applied to the whole session.
    """
    print("\n  Frame rate check ({}):".format(context))
    if CAPTURE_FPS > 0:
        recompute_scan_parameters(CAPTURE_FPS, "CAPTURE_FPS")
        print("    using configured CAPTURE_FPS = {:.2f}".format(CAPTURE_FPS))
        return fps

    if FPS_PROBE_SECONDS > 0 and ENABLE_CAPTURE:
        v = _probe_capture_fps(FPS_PROBE_SECONDS)
        if v is not None:
            old = fps
            recompute_scan_parameters(v, "capture probe")
            print("    capture probe: {:.2f} fps (preview was {:.2f}); "
                  "scan {:.2f}x".format(fps, old, scan_multiplier))
            return fps
        print("    capture probe failed; falling back to CurrentFrameRate")

    v = _median_fps()
    if v is None:
        print("    no reading; keeping {:.2f} fps".format(fps))
        return fps
    old = fps
    recompute_scan_parameters(v, "CurrentFrameRate")
    print("    CurrentFrameRate = {:.2f} fps (was {:.2f}); "
          "scan {:.2f}x = {:.1f}\"/s".format(fps, old, scan_multiplier, scan_speed))
    return fps

# IronPython has no `nonlocal`, so we use a one-element list as a mutable
# closure cell. Ctrl+C in the SharpCap console raises KeyboardInterrupt at
# the `try:` block far below; we set the flag there and check it in every
# wait loop so handlers and watchers can exit cleanly.
_abort = [False]

def check_abort():
    if _abort[0]:
        raise KeyboardInterrupt()


def _sleep_abortable(seconds, poll=0.05):
    """time.sleep() that reacts to Ctrl+C within ``poll`` seconds.

    Long plain sleeps (a full scan is ~40 s) would otherwise ignore the abort
    until they finish, with the mount still moving.
    """
    t0 = time.time()
    while True:
        check_abort()
        remaining = seconds - (time.time() - t0)
        # The epsilon also stops a float-precision spin when the remaining time
        # rounds to 0 but stays positive.
        if remaining <= 1e-3:
            return
        time.sleep(min(poll, remaining))

def ra_diff_arcsec(ra1, ra2):
    """Signed RA difference in arcsec, handling the 0/24h wrap-around.

    `mount.RA` returns hours in [0, 24). A naive subtraction of 23.9h - 0.1h
    would give 23.8h (~360°), but the actual angular difference is 0.2h.
    Anything beyond ±12h crosses the wrap, so add/subtract 24 to bring it
    back into the short-arc range. 1 hour of RA = 15° = 54000".
    """
    diff = ra1 - ra2
    if diff > 12:
        diff -= 24
    elif diff < -12:
        diff += 24
    return diff * 15 * 3600

def full_scale():
    """Detect bit depth from current color space. MONO16 => 65535, MONO8 => 255."""
    try:
        cs = str(cam.Controls.ColourSpace.Value).upper()
        if "16" in cs:
            return 65535.0
        if "8" in cs:
            return 255.0
    except:
        pass
    return 65535.0

# ── FrameCaptured watcher: latches stats from the live stream ─────────────────
# The FrameCaptured + Frame.GetStats() pattern used here is borrowed from
# Patrick Hsieh's SHGScan (https://github.com/FlankerOneTwo/SHGScan).
#
# SharpCap fires the camera's FrameCaptured event for every frame in the live
# preview. We attach a handler, let it run continuously, and latch only the
# most recent (mean, stddev) pair so the rest of the script can read "current
# brightness" at any moment without ever stopping the stream. This is the key
# trick that makes the event-driven architecture work.
class FrameWatcher:
    """Attaches a FrameCaptured handler and latches the most recent (mean, stddev)."""
    def __init__(self, camera):
        self.camera = camera
        self.latest = None
        # Guards against a frame arriving after stop() has begun unhooking.
        self._active = False

    def start(self):
        # `+=` on a .NET event subscribes the handler. SharpCap will then call
        # _on_frame on its capture thread for every frame until we unsubscribe.
        self.camera.FrameCaptured += self._on_frame
        self._active = True

    def stop(self):
        if self._active:
            try:
                self.camera.FrameCaptured -= self._on_frame
            except:
                # Some SharpCap builds throw if the handler was already detached
                # or the camera was disconnected — non-fatal, swallow it.
                pass
            self._active = False

    def _on_frame(self, sender, args):
        if not self._active:
            return
        try:
            # Frame.GetStats() returns a .NET Tuple<float, float>:
            #   Item1 = mean pixel value, Item2 = standard deviation.
            # IronPython exposes these as attributes (not [0] / [1] indices).
            s = args.Frame.GetStats()
            self.latest = (s.Item1, s.Item2)
        except:
            # Don't take down the live stream if a single frame's stats fail.
            pass

    def wait_first(self, timeout=3.0):
        """Block until the first frame has populated `latest`, or time out."""
        t0 = time.time()
        while self.latest is None:
            if time.time() - t0 > timeout:
                return False
            check_abort()
            time.sleep(0.05)
        return True

# ── One-shot frame grab: left/right mean via CutROI ───────────────────────────
# The Frame.CutROI(Rectangle(...)) approach for measuring sub-regions of a
# live frame (instead of writing the frame to disk and reading it back) is
# borrowed from Patrick Hsieh's SHGScan.
#
# Unlike FrameWatcher (which keeps running and is read by polling), this
# helper attaches a handler, takes the *next* frame that arrives, computes
# left/right half-frame means from it, and detaches. We do this on a single
# fresh frame (rather than reading FrameWatcher.latest) because Dec centering
# needs a stable measurement after the mount has settled, not whichever frame
# happened to be cached during the previous nudge.
def measure_left_right_means(timeout=3.0):
    """Capture one live frame, split into left and right halves, return means."""
    # One-element list = mutable closure cell (IronPython 2.7 has no nonlocal).
    result = [None]

    def on_frame(sender, args):
        # Only act on the first frame after subscription; later frames are
        # ignored so we don't keep doing work after the result is in.
        if result[0] is not None:
            return
        try:
            f = args.Frame
            # cam.ROI is the camera's currently-set sensor ROI; the live frame
            # has these dimensions. Splitting at the midpoint gives us two
            # halves to compare for Dec centering.
            roi = cam.ROI
            w = roi.Width
            h = roi.Height
            mid = w // 2
            # Frame.CutROI(Rectangle(x, y, w, h)) returns a sub-frame view.
            # GetStats() on each half gives us per-half (mean, stddev).
            left  = f.CutROI(Rectangle(0,   0, mid,     h))
            right = f.CutROI(Rectangle(mid, 0, w - mid, h))
            ls = left.GetStats()
            rs = right.GetStats()
            result[0] = (ls.Item1, rs.Item1)  # means only — caller compares L vs R
        except Exception as e:
            # Smuggle the error back across the closure boundary as a tagged
            # tuple; the caller unpacks and prints it after detaching.
            result[0] = ("error", str(e))

    cam.FrameCaptured += on_frame
    try:
        t0 = time.time()
        while result[0] is None:
            if time.time() - t0 > timeout:
                return None
            check_abort()
            time.sleep(0.05)
    finally:
        # Always detach, even on timeout / abort, so we don't leak handlers.
        try:
            cam.FrameCaptured -= on_frame
        except:
            pass

    if isinstance(result[0], tuple) and result[0][0] == "error":
        print("  CutROI error: {}".format(result[0][1]))
        return None
    return result[0]

# ── Slit-axis extent (NightScribe) ────────────────────────────────────────────
# The live frame is the spectrum: the slit runs along one axis (the ROI width)
# and the dispersion along the other (the ROI height). When the slit is on the
# disk, the illuminated extent along the slit axis is the solar chord at that
# RA; at the disk centre it is the full diameter. Measuring it in a live frame
# gives the slit-axis plate scale directly (S_Y = SOLAR_DIAMETER / extent_px),
# which is what the mosaic step needs. It is measured in bands (a handful of
# CutROI calls) rather than per column, so it is cheap.
def measure_slit_extent_px(bands=None, timeout=3.0):
    """Illuminated extent along the slit axis, in sensor pixels, or None.

    Returns (extent_px, roi_width). The measurement is coarse (band width =
    roi_width / bands) but enough to estimate the slit scale to a few percent
    and to flag a frame whose slit is off the disk.
    """
    bands = bands or SLIT_EXTENT_BANDS
    result = [None]

    def on_frame(sender, args):
        if result[0] is not None:
            return
        try:
            roi = cam.ROI
            w, h = roi.Width, roi.Height
            band = max(1, int(w / float(max(1, bands))))
            means = []
            for x in range(0, w - band + 1, band):
                sub = args.Frame.CutROI(Rectangle(x, 0, band, h))
                means.append(sub.GetStats().Item1)
            result[0] = (means, w, band)
        except Exception as e:
            result[0] = ("error", str(e))

    cam.FrameCaptured += on_frame
    try:
        t0 = time.time()
        while result[0] is None:
            if (time.time() - t0) > timeout:
                return None
            check_abort()
            time.sleep(0.05)
    finally:
        try:
            cam.FrameCaptured -= on_frame
        except:
            pass

    if isinstance(result[0], tuple) and result[0] and result[0][0] == "error":
        print("  Slit extent: {}".format(result[0][1]))
        return None
    means, w, band = result[0]
    if not means:
        return None
    lo = min(means)
    hi = max(means)
    if hi <= lo:
        return None
    thr = lo + 0.25 * (hi - lo)
    on = [i for i, v in enumerate(means) if v > thr]
    if not on:
        return None
    extent = (on[-1] - on[0] + 1) * band
    return (float(min(extent, w)), w)


# ── Limb detection ────────────────────────────────────────────────────────────
# Using stddev (GetStats().Item2), not mean brightness, as the on-disk vs
# off-disk signal is an idea taken from Patrick Hsieh's SHGScan. The dynamic
# dark-baseline thresholding around it is local to this script.
#
# Why stddev and not mean: the SHG slit projects a spectrum onto the sensor.
# When the slit is on the solar disk that spectrum has dark Fraunhofer lines
# crossing it, so the per-frame stddev is enormous (typ. 10000+ at MONO16).
# Off-disk the frame is near-uniform sensor noise, so stddev is tiny (10s).
# The signal is robust whichever way the spectral axis is oriented and does
# not depend on a particular brightness target.
def find_solar_limb(max_search=None):
    # Optional shorter search window: chord calibration calls this many times
    # and must fail fast if the slit is too far off the disk in Dec.
    search_limit = LIMB_MAX_SEARCH if max_search is None else max_search

    # mount.MoveAxis(0, ...) is the RA axis; positive multiplier slews "east"
    # in the apparent-sky sense (sun appears to drift west across the slit
    # when the mount moves slower than sidereal in this direction).
    watcher = FrameWatcher(cam)
    watcher.start()
    try:
        if not watcher.wait_first():
            print("  No frames from camera")
            return False

        initial_stddev = watcher.latest[1]

        # If we started already on disk, find the leading limb by reversing
        # off the disk first, then resuming the forward search. This keeps the
        # rest of the routine simple — it only has to handle "we're in dark
        # sky and need to find the rising edge".
        if initial_stddev > ON_DISK_STDDEV:
            print("  On disk (stddev={:.0f}) - backing up...".format(initial_stddev), end="")
            mount.MoveAxis(0, -RETURN_MULTIPLIER)
            t0 = time.time()
            off_disk = False
            while (time.time() - t0) < search_limit:
                check_abort()
                if watcher.latest[1] < LIMB_STDDEV_MIN:
                    mount.Stop()
                    off_disk = True
                    print(" off after {:.1f}s".format(time.time() - t0))
                    time.sleep(0.3)  # let the mount settle before sampling dark
                    break
                time.sleep(LIMB_SAMPLE_INTERVAL)
            if not off_disk:
                mount.Stop()
                print(" FAILED")
                return False

        # Sample the actual dark-sky stddev and build a threshold relative to it,
        # rather than using a fixed absolute number. Hot pixels, gain settings,
        # and exposure time all change the noise floor; a relative threshold
        # adapts. We require all three: at least LIMB_DARK_MULT× the floor, at
        # least floor+300, and at least the absolute LIMB_STDDEV_MIN — whichever
        # is largest wins, so noisy darks don't trigger spurious "limb found".
        time.sleep(0.2)
        dark = watcher.latest[1]
        threshold = max(dark * LIMB_DARK_MULT, dark + 300.0, LIMB_STDDEV_MIN)

        # Slew forward at LIMB_SEARCH_SPEED (4× sidereal) and watch for the
        # stddev jump that marks the leading limb crossing into the slit.
        mount.MoveAxis(0, LIMB_SEARCH_SPEED)
        t0 = time.time()
        while (time.time() - t0) < search_limit:
            check_abort()
            s = watcher.latest[1]
            if s > threshold:
                mount.Stop()
                print("  Limb found stddev={:.0f} (dark={:.0f}, thr={:.0f}, {:.1f}s)".format(
                      s, dark, threshold, time.time() - t0))
                return True
            time.sleep(LIMB_SAMPLE_INTERVAL)

        mount.Stop()
        print("  Limb not found (dark={:.0f}, thr={:.0f})".format(dark, threshold))
        return False
    finally:
        watcher.stop()

# ── Dec centering ─────────────────────────────────────────────────────────────
def center_dec():
    """Nudge Dec until left-half and right-half means are balanced.

    Strategy: at the disk centre the slit is illuminated symmetrically along
    its length, so the left and right halves of the frame should have equal
    mean brightness. If the sun is offset in Dec, one half is brighter than
    the other; we nudge Dec toward the dimmer side and re-measure.

    Note: this function is only called when ENABLE_DEC_CENTERING is True.
    Its metric assumes the solar disk fits along the slit axis; when it does
    not, set ENABLE_DEC_CENTERING = False instead of relying on this loop.
    See the switch documentation in CONFIGURATION and docs/USER_GUIDE.md, 9.4.
    """
    for iteration in range(1, DEC_MAX_CORRECTIONS + 1):
        check_abort()
        means = measure_left_right_means()
        if means is None:
            print("  Dec: frame read failed")
            return False
        l, r = means
        denom = l + r
        if denom <= 0:
            # Both halves dark — likely off-disk or very over-exposed black.
            # Nothing useful to do; abort centering rather than guess.
            print("  Dec: no signal")
            return False
        # Normalised imbalance in [-1, +1]. Sign tells us which half is brighter,
        # magnitude how far off-centre we are. Dividing by (L+R) cancels overall
        # brightness, so the value doesn't depend on exposure.
        offset = (l - r) / denom  # + => sun pushed left, - => pushed right

        print("  Dec {}: L={:.0f} R={:.0f} offset={:+.3f}".format(
              iteration, l, r, offset), end="")

        if abs(offset) < DEC_CENTER_TOLERANCE:
            print("  OK")
            return True

        # Proportional control: nudge time scales with how far off-centre we
        # are. Cap the upper end so a misread doesn't trigger a long slew, and
        # the lower end so a tiny nudge still actually moves the mount past
        # static friction / gear backlash.
        nudge_time = min(abs(offset) * 8.0, 1.5)
        nudge_time = max(nudge_time, 0.1)
        # +offset means left half brighter => sun is pushed left in the frame
        # => nudge Dec in the direction that moves the sun right (toward center).
        direction = -1 if offset > 0 else 1

        # MoveAxis(1, ...) is the Dec axis (axis index 1); axis 0 is RA.
        mount.MoveAxis(1, direction * DEC_NUDGE_SPEED)
        time.sleep(nudge_time)
        mount.Stop()
        # Brief settle pause so the next measure_left_right_means() reads a
        # mechanically-still frame rather than one mid-vibration.
        time.sleep(DEC_SETTLE_TIME)
        print("  nudge {:.2f}s {}".format(nudge_time, "+" if direction > 0 else "-"))

    print("  Dec: max corrections")
    return False

# ── Histogram percentile measurement ──────────────────────────────────────────
def measure_percentile_pixel(pct, timeout=3.0):
    """Capture one frame, return pixel value at the given cumulative percentile.

    For pct=0.99, this returns the bin index N such that 99% of all pixels in
    the frame have a value <= N. We use this for auto-exposure: targeting the
    99.5th percentile is robust against a few hot pixels but still tracks the
    bright continuum peak of the spectrum, which is what we don't want to clip.
    """
    result = [None]

    def on_frame(sender, args):
        if result[0] is not None:
            return
        try:
            # CalculateHistogram() returns a Histogram object with a Values
            # property that is a list-of-channel-arrays. For mono cameras
            # there is one channel; Values[0] is its bin array. Each bin
            # holds the pixel count for that pixel value (0..255 for MONO8,
            # 0..65535 for MONO16).
            h = args.Frame.CalculateHistogram()
            bins = h.Values[0]
            nbins = len(bins)
            # Total pixel count = sum of all bins. Walk from bin 0 upward and
            # find the first bin where the running cumulative count crosses
            # `pct` of the total — that bin index is the percentile pixel value.
            total = 0
            for i in range(nbins):
                total += bins[i]
            if total <= 0:
                result[0] = 0
                return
            target_count = total * pct
            cum = 0
            for i in range(nbins):
                cum += bins[i]
                if cum >= target_count:
                    result[0] = i
                    return
            result[0] = nbins - 1
        except Exception as e:
            result[0] = ("error", str(e))

    cam.FrameCaptured += on_frame
    try:
        t0 = time.time()
        while result[0] is None:
            if time.time() - t0 > timeout:
                return None
            check_abort()
            time.sleep(0.05)
    finally:
        try:
            cam.FrameCaptured -= on_frame
        except:
            pass

    if isinstance(result[0], tuple) and result[0][0] == "error":
        print("  Histogram error: {}".format(result[0][1]))
        return None
    return result[0]

# ── Auto-exposure ─────────────────────────────────────────────────────────────
def auto_exposure():
    """Iteratively scale the exposure so the EXP_PERCENTILE pixel sits at
    EXP_TARGET_LEVEL of full scale.

    The signal we care about for SHG reconstruction is the continuum peak of
    the spectrum, which is well above the mean. A mean-based AE would happily
    drive that peak up into the clipping rail. Instead we look at the high
    percentile of the histogram and aim it at ~88% of full scale, leaving
    room above for atmospheric bumps without losing any line detail.
    """
    fs = full_scale()
    target_val = EXP_TARGET_LEVEL * fs  # absolute pixel value we're aiming for
    tol_val    = EXP_TOLERANCE * fs     # ± window that counts as "converged"

    # cam.Controls.FindByName returns the named control object; its .Value
    # property is read/write and is the exposure in milliseconds for most
    # ZWO/QHY/Touptek drivers under SharpCap.
    exp_ctrl = cam.Controls.FindByName("Exposure")
    entry_exp = exp_ctrl.Value  # remember starting value so we can revert on failure

    for attempt in range(EXP_MAX_ATTEMPTS):
        check_abort()
        p = measure_percentile_pixel(EXP_PERCENTILE)
        if p is None:
            print("  Auto-exp: measurement failed, reverting to {:.2f}ms".format(entry_exp))
            exp_ctrl.Value = entry_exp
            return False

        current_exp = exp_ctrl.Value
        print("  Exp {:.2f}ms  p{:.1f}={:.0f}/{:.0f} ({:.0%})".format(
              current_exp, EXP_PERCENTILE * 100, p, fs, p / fs), end="")

        if abs(p - target_val) < tol_val:
            print("  OK")
            return True

        if p > 0:
            # Linear approximation: brightness scales roughly with exposure,
            # so new_exp = current_exp × (target / measured). Clamp the
            # multiplier to [0.5, 2.0] so a single noisy/blocked frame can't
            # send the exposure to an extreme; we'll converge over a few
            # iterations instead.
            ratio = target_val / p
            ratio = max(0.5, min(2.0, ratio))
            new_exp = current_exp * ratio
            exp_ctrl.Value = new_exp
            # Wait long enough for the new exposure to take effect on at
            # least one frame before we measure again.
            time.sleep(0.3)
            print("  -> {:.2f}ms".format(new_exp))
        else:
            # Zero percentile = no signal at all, so the multiplier would
            # be infinite. Bail rather than ramp exposure into oblivion.
            print("  (no signal)")
            exp_ctrl.Value = entry_exp
            return False

    # Fell through without converging — don't leave a possibly-bad value.
    print("  Auto-exp: max attempts, reverting to {:.2f}ms".format(entry_exp))
    exp_ctrl.Value = entry_exp
    return False

# ── Alignment routines ────────────────────────────────────────────────────────
# do_full_alignment: re-find the limb, walk to disk centre, re-centre Dec, set
# exposure, then walk back to the limb. This is the slow path, used on cycle 0
# and every DEC_INTERVAL cycles. Returns the RA of the leading limb.
#
# do_quick_realign: just re-find the leading limb. Used on intermediate cycles
# where Dec/exposure are assumed not to have drifted enough to need re-tuning.
def do_full_alignment():
    print("\n  Full alignment...")
    if not find_solar_limb():
        return False
    limb_ra = mount.RA  # remembered so we can return here later

    # Time to walk half a solar diameter at RETURN_MULTIPLIER × sidereal.
    # This puts us roughly at the centre of the disk for Dec measurement.
    half_t = (SOLAR_DIAMETER / 2.0) / (RETURN_MULTIPLIER * SIDEREAL)
    mount.MoveAxis(0, RETURN_MULTIPLIER)
    time.sleep(half_t)
    mount.Stop()
    time.sleep(0.2)

    if ENABLE_DEC_CENTERING:
        print("  Dec centering...")
        center_dec()
    else:
        # Switch: skip the Dec loop entirely. The mount stays at whatever
        # Dec it had after find_solar_limb(); auto-exposure and the scan are
        # unaffected. This is the escape hatch for setups where the solar
        # disk overfills the frame along the slit axis.
        print("  Dec centering: OFF (ENABLE_DEC_CENTERING=False)")

    # Let the mount fully damp before sampling brightness. The per-iteration
    # DEC_SETTLE_TIME inside center_dec is too short for the SAL-33 with a
    # 4" OTA — consecutive nudges accumulate mechanical oscillation.
    time.sleep(1.0)

    if ENABLE_AUTO_EXP:
        print("  Auto-exposure...")
        auto_exposure()

    # We are at the disk centre here: measure the illuminated extent along the
    # slit axis to estimate the slit-axis plate scale (NightScribe).
    measure_slit_scale()

    # Walk back to the leading limb so the actual scan starts in the right place.
    mount.MoveAxis(0, -RETURN_MULTIPLIER)
    time.sleep(half_t)
    mount.Stop()
    time.sleep(0.2)

    return limb_ra

def do_quick_realign(max_search=None):
    print("  Quick realign...")
    if not find_solar_limb(max_search=max_search):
        return False
    return mount.RA

# ── Chord-based Dec centring ──────────────────────────────────────────────────
# Used when the solar disk does not fit along the slit. The live image then has
# no usable left/right asymmetry: wherever the slit is inside the disk, both
# halves are equally illuminated. The scan direction still carries information:
# the RA interval between the leading and trailing limb is the chord of the disk
# at the slit's Dec.
#
# Geometry, with R = solar radius, S = effective slit length and q = signed Dec
# offset of the slit centre from the disk centre:
#
#   nearest slit edge distance to the disk centre: e = max(0, |q| - S/2)
#   measured chord:                               L = 2 * sqrt(R^2 - e^2)
#
# Outside the geometric dead zone (|q| > S/2) the magnitude is |q| = e + S/2.
# A short +Dec probe then tells which side: if the new edge distance grows, the
# slit was north; if it shrinks, it was south. The probe is capped below the
# offset so it cannot cross the disk centre, which makes the sign reliable.
#
# Inside the dead zone the chord is the diameter regardless of q, so an offset
# cannot be measured there. center_dec_chord() walks north in steps until the
# chord is reliably shorter than the diameter, which leaves the dead zone on
# the north side, then uses the probe above. This costs a few minutes but keeps
# the mosaic plan at its nominal row count. The alternative — leave Dec alone
# and widen the plan by the dead-zone width — was rejected because a near-
# centred start is the common case and it doubled the row count at 1000 mm.
#
# The measured chord is converted from RA coordinate to true sky angle with
# cos(Dec).
#
# If the disk fits the slit (S >= solar diameter), the legacy left/right loop
# is both valid and cheaper, so center_dec_chord() delegates to it.
_dec_sign = [None]  # learned sign of MoveAxis(1): +1 or -1
# How far the slit centre may still be from the disk centre after centring.
# compute_mosaic_plan() adds this to the requested margin.
mosaic_center_uncertainty = [MOSAIC_CENTER_RESIDUAL_ARCSEC]

# ── NightScribe runtime state ─────────────────────────────────────────────────
# Slit re-imaging scale. Starts at the configured value and can be replaced by a
# live measurement (measure_slit_extent_px + the ephemeris diameter).
_slit_scale = [MOSAIC_SLIT_IMAGE_SCALE]
# Last measured slit-axis quantities: (extent_px, roi_width_px, S_Y_arcsec_px).
_slit_extent = [None]
# Scan-rate calibration: factor applied, measured rate ("/s) and target ("/s).
_calibration = [{"applied": False, "factor": 1.0,
                 "measured_rate": 0.0, "target_rate": 0.0}]

def effective_slit_arcsec():
    """Angular length of the slit actually sampled by the current ROI."""
    roi = cam.ROI
    if roi is None:
        raise RuntimeError("Camera ROI is not available; cannot compute the mosaic geometry.")
    scale = _slit_scale[0]
    # slit_px = sensor pixels occupied by the full slit. Dividing the sampled
    # pixel count by the magnification converts sensor pixels back to sky
    # angle at the telescope focal plane.
    slit_px = (MOSAIC_SLIT_LENGTH_MM * scale * 1000.0 / PIXEL_SIZE)
    return min(float(roi.Width), slit_px) * PIXEL_SCALE / scale


def expected_chord_arcsec(dec_offset_arcsec, slit_arcsec, solar_diameter):
    """On-disk RA span (chord) of the disk at a slit offset.

    MOSAIC geometry: with q the signed Dec offset of the slit centre, S the
    effective slit length and R the solar radius,

        e = max(0, |q| - S/2)          # nearest slit edge to the disk centre
        chord = 2 * sqrt(R^2 - e^2)

    Returns the chord in arcsec (0 if the slit is off the disk). This is the
    span a mosaic row actually crosses, which can be much shorter than the
    diameter near the poles.
    """
    R = solar_diameter / 2.0
    e = max(0.0, abs(dec_offset_arcsec) - slit_arcsec / 2.0)
    return 2.0 * math.sqrt(max(R * R - e * e, 0.0))

def _init_dec_sign():
    """Learn whether MoveAxis(1, +) increases or decreases mount.Dec."""
    if _dec_sign[0] is not None:
        return
    before = mount.Dec
    mount.MoveAxis(1, MOSAIC_DEC_SPEED)
    # Keep moving until Dec actually changes by 5": a single fixed-duration
    # move can be swallowed entirely by Dec backlash.
    t0 = time.time()
    delta = 0.0
    while (time.time() - t0) < 6.0:
        check_abort()
        delta = mount.Dec - before
        if abs(delta) >= 5.0 / 3600.0:
            break
        time.sleep(0.05)
    mount.Stop()
    time.sleep(MOSAIC_DEC_SETTLE)
    delta = mount.Dec - before
    if abs(delta) < 1.0 / 3600.0:
        raise RuntimeError(
            "mount.Dec does not change while MoveAxis(1) runs. Automatic Dec "
            "centring needs a mount that reports Dec while moving.")
    _dec_sign[0] = 1 if delta > 0 else -1
    print("  Dec axis: MoveAxis(1, +) {} Dec (sign {})".format(
          "increases" if delta > 0 else "decreases", _dec_sign[0]))
    # Return to the starting Dec: the sign test is an implementation detail and
    # must not be counted as part of the caller's requested move.
    _dec_jog((before - mount.Dec) * 3600.0)

def _dec_jog(delta_arcsec):
    """One closed-loop Dec move. Stops at the target or after a bounded time;
    never reverses (so it can be used as a final approach)."""
    if abs(delta_arcsec) < 0.5:
        return
    direction = 1 if delta_arcsec > 0 else -1
    target = mount.Dec + delta_arcsec / 3600.0
    t_expected = abs(delta_arcsec) / (MOSAIC_DEC_SPEED * SIDEREAL)
    mount.MoveAxis(1, direction * MOSAIC_DEC_SPEED * _dec_sign[0])
    t0 = time.time()
    timeout = t_expected * 1.25 + 1.0
    while (time.time() - t0) < timeout:
        check_abort()
        if (mount.Dec - target) * direction >= 0:
            break
        time.sleep(0.03)
    mount.Stop()
    time.sleep(MOSAIC_DEC_SETTLE)

def move_dec_by(delta_arcsec):
    """Move Dec by delta_arcsec, finishing with a +Dec approach so backlash is
    repeatable regardless of the direction requested."""
    if abs(delta_arcsec) < 0.5:
        return
    _init_dec_sign()
    target = mount.Dec + delta_arcsec / 3600.0
    approach = target - MOSAIC_DEC_BACKLASH / 3600.0
    # First reach a point below the target, then come up onto it.
    _dec_jog((approach - mount.Dec) * 3600.0)
    for _ in range(3):
        err = (target - mount.Dec) * 3600.0
        if abs(err) < 2.0:
            return
        if err < 0:
            # Overshot above the target: drop below and approach again.
            _dec_jog((approach - mount.Dec) * 3600.0)
        else:
            _dec_jog(err)

def measure_chord(rewind=False):
    """Measure the on-disk RA chord at the current Dec.

    Returns (chord_arcsec, dec_degrees) or None. The slit must be inside the
    disk in Dec; if it is not, find_solar_limb() times out and returns None.
    On success the mount is back just before the leading limb, ready for the
    next measurement, so `rewind` is only needed to recover after a probe that
    lost the disk (the mount may then be past the trailing limb).
    """
    if rewind:
        rewind_before_disk()
    if not find_solar_limb(max_search=40):
        return None
    dec_meas = mount.Dec
    ra_start = mount.RA

    watcher = FrameWatcher(cam)
    watcher.start()
    try:
        if not watcher.wait_first():
            print("  Chord: no frames from camera")
            return None

        peak = watcher.latest[1]
        below = 0
        ra_end = None
        t0 = time.time()
        # RA coordinate span to cover the whole disk plus margin at this
        # declination. The mount moves in RA coordinates, so divide by cos(Dec).
        travel = (SOLAR_DIAMETER + 3 * MOSAIC_MARGIN_ARCSEC) / \
                 max(0.5, math.cos(math.radians(dec_meas)))
        timeout = travel / (MOSAIC_CHORD_SPEED * SIDEREAL) + 8.0

        mount.MoveAxis(0, MOSAIC_CHORD_SPEED)
        while (time.time() - t0) < timeout:
            check_abort()
            s = watcher.latest[1]
            if s > peak:
                peak = s
            threshold = max(LIMB_STDDEV_MIN, peak * 0.15)
            # Require a genuine on-disk peak before trusting a fall below the
            # threshold; otherwise the dark baseline itself would trigger it.
            if peak > ON_DISK_STDDEV and s < threshold:
                below += 1
                if below >= 2:
                    mount.Stop()
                    time.sleep(0.2)
                    ra_end = mount.RA
                    break
            else:
                below = 0
            time.sleep(LIMB_SAMPLE_INTERVAL)

        if ra_end is None:
            mount.Stop()
            print("  Chord: trailing limb not found (peak={:.0f})".format(peak))
            return None

        ra_delta = abs(ra_diff_arcsec(ra_end, ra_start))
        # RA coordinate -> true sky angle at this declination.
        chord = ra_delta * math.cos(math.radians(dec_meas))
        print("  Chord: {:.0f}\" at Dec {:.2f} deg".format(chord, dec_meas))

        # Walk back to the leading limb, plus a small margin so the next
        # find_solar_limb() starts outside the disk.
        back_s = ra_delta / (MOSAIC_CHORD_SPEED * SIDEREAL) + 0.15
        mount.MoveAxis(0, -MOSAIC_CHORD_SPEED)
        time.sleep(back_s)
        mount.Stop()
        time.sleep(0.2)
        return (chord, dec_meas)
    finally:
        try:
            mount.Stop()
        except:
            pass
        watcher.stop()

def scan_rate_calibration_factor(measured_rate, measure_speed, target,
                                 scan_multiplier):
    """Factor that scales scan_multiplier so the sky rate becomes ``target``.

    ``measured_rate`` is the sky rate (arcsec/s) measured while slewing at
    ``measure_speed`` (sidereal multiplier units), so the mount's rate per unit
    is ``measured_rate / measure_speed``. The multiplier needed for ``target``
    is ``target / per_unit``; the factor is that divided by the current
    multiplier. Returns None for an unusable measurement.

    This is the piece that was wrong: the old code used
    ``target / measured_rate`` directly, ignoring that the measurement is taken
    at ``measure_speed`` (16x), not at the science rate. That made the scan
    ~4.6x too slow and the recon stretched in X (actual_xy ~4.1 instead of ~1).
    """
    if measured_rate <= 0 or measure_speed <= 0 or scan_multiplier <= 0:
        return None
    per_unit = measured_rate / float(measure_speed)
    if per_unit <= 0:
        return None
    return (target / per_unit) / scan_multiplier


def calibrate_scan_rate():
    """Self-calibrate the scan multiplier from a disk traversal.

    Requires the slit to be near the disk centre in Dec (after Dec centring).
    Crosses the disk at MOSAIC_CHORD_SPEED, measures the true sky rate from the
    mount RA delta and the elapsed time, and scales scan_multiplier so the sky
    moves exactly PIXEL_SCALE * fps per frame (1:1 sampling). Returns the
    correction factor, or None if it could not be measured.
    """
    if not CALIBRATE_SCAN_RATE:
        return None
    print("\nScan-rate calibration (target {:.2f}\"/s = {:.3f}\"/px x {:.2f} fps):".format(
          PIXEL_SCALE * fps, PIXEL_SCALE, fps))
    rates = []
    for k in range(max(1, CALIBRATION_SAMPLES)):
        if not find_solar_limb(max_search=40):
            print("  Calibration: limb not found")
            return None
        dec_cal = mount.Dec
        ra_start = mount.RA
        watcher = FrameWatcher(cam)
        watcher.start()
        t1 = None
        try:
            if not watcher.wait_first():
                print("  Calibration: no frames")
                return None
            peak = watcher.latest[1]
            below = 0
            ra_end = None
            t0 = time.time()
            travel = (SOLAR_DIAMETER + 3 * MOSAIC_MARGIN_ARCSEC) / \
                     max(0.5, math.cos(math.radians(dec_cal)))
            timeout = travel / (MOSAIC_CHORD_SPEED * SIDEREAL) + 8.0
            mount.MoveAxis(0, MOSAIC_CHORD_SPEED)
            while (time.time() - t0) < timeout:
                check_abort()
                s = watcher.latest[1]
                if s > peak:
                    peak = s
                threshold = max(LIMB_STDDEV_MIN, peak * 0.15)
                if peak > ON_DISK_STDDEV and s < threshold:
                    below += 1
                    if below >= 2:
                        mount.Stop()
                        # Take t1 the moment the mount stops: the settle sleep
                        # below is only to let mount.RA read cleanly and must
                        # not inflate the traverse time (it biased the measured
                        # rate low).
                        t1 = time.time()
                        time.sleep(0.2)
                        ra_end = mount.RA
                        break
                else:
                    below = 0
                time.sleep(LIMB_SAMPLE_INTERVAL)
            if ra_end is None or t1 is None:
                print("  Calibration: trailing limb not found")
                return None
            dt = t1 - t0
            ra_delta = abs(ra_diff_arcsec(ra_end, ra_start))
            sky_arcsec = ra_delta * math.cos(math.radians(dec_cal))
            rate = sky_arcsec / dt if dt > 0 else 0.0
            if rate <= 0:
                print("  Calibration: invalid rate")
                return None
            rates.append(rate)
            print("  pass {}: {:.0f}\" in {:.2f}s -> {:.2f}\"/s".format(
                  k + 1, sky_arcsec, dt, rate))
            # Walk back to just before the leading limb.
            back_s = ra_delta / (MOSAIC_CHORD_SPEED * SIDEREAL) + 0.15
            mount.MoveAxis(0, -MOSAIC_CHORD_SPEED)
            time.sleep(back_s)
            mount.Stop()
            time.sleep(0.2)
        finally:
            try:
                mount.Stop()
            except:
                pass
            watcher.stop()

    rates.sort()
    rate = rates[len(rates) // 2]
    target = PIXEL_SCALE * fps
    factor = scan_rate_calibration_factor(rate, MOSAIC_CHORD_SPEED, target,
                                          scan_multiplier)
    if factor is None:
        print("  Calibration: invalid measurement; keeping {:.2f}x".format(
              scan_multiplier))
        return None
    # Sanity guard: a bad measurement must not be allowed to wreck the scan.
    # With the corrected formula the factor is a small correction (~1); the old
    # buggy formula (target/rate) produced ~0.24 here and is rejected.
    corrected = scan_multiplier * factor * SIDEREAL
    if not (0.5 * target <= corrected <= 2.0 * target):
        print("  Calibration: correction out of range ({:.2f}\"/s vs target "
              "{:.2f}\"/s); keeping {:.2f}x".format(
                  corrected, target, scan_multiplier))
        return None
    apply_scan_calibration(factor)
    _calibration[0] = {"applied": True, "factor": factor,
                       "measured_rate": rate, "target_rate": target,
                       "measured_per_unit": rate / MOSAIC_CHORD_SPEED}
    print("  measured {:.2f}\"/s at {:.1f}x -> {:.2f}\"/s per unit".format(
          rate, MOSAIC_CHORD_SPEED, rate / MOSAIC_CHORD_SPEED))
    print("  target {:.2f}\"/s -> factor {:.4f}".format(target, factor))
    print("  new scan: {:.2f}x ({:.1f}\"/s), duration {:.1f}s".format(
          scan_multiplier, scan_speed, scan_duration))
    return factor


def measure_slit_scale():
    """Estimate the slit-axis plate scale from the live frame, best effort.

    Measures the illuminated extent along the slit axis at the (centred) slit
    and derives S_Y = SOLAR_DIAMETER / extent_px. Updates the scale used by
    effective_slit_arcsec() only when the measurement looks unclipped and on the
    disk. Returns S_Y ("/px) or None.
    """
    if not MEASURE_SLIT_EXTENT:
        return None
    m = measure_slit_extent_px()
    if m is None:
        return None
    extent, roi_w = m
    if extent <= 0:
        return None
    s_y = SOLAR_DIAMETER / extent
    _slit_extent[0] = (extent, roi_w, s_y)
    print("  Slit extent: {:.0f}/{:.0f} px -> S_Y = {:.3f}\"/px".format(
          extent, roi_w, s_y))
    # If the slit is nearly full or barely lit, the chord is clipped or off the
    # disk: record it but do not trust it for the scale.
    if 0.2 * roi_w < extent < 0.97 * roi_w:
        new_scale = PIXEL_SCALE / s_y
        if 0.3 < new_scale < 3.0:
            _slit_scale[0] = new_scale
            print("  Slit image scale: {:.4f} (measured; was {:.4f})".format(
                  new_scale, MOSAIC_SLIT_IMAGE_SCALE))
    else:
        print("  Slit extent looks clipped/off-disk; keeping scale {:.4f}".format(
              _slit_scale[0]))
    return s_y


def center_dec_chord():
    """Centre the slit in Dec using the chord method (or the legacy loop when
    the disk fits the slit). Returns True on success.

    The chord method has a dead zone of +/-S/2 around the disk centre: while
    the slit spans the centre, the measured chord is the full diameter and does
    not change with offset. That is a geometric limit, not a tuning problem.

    Leaving the dead zone is enough to resolve the offset:
    - walk +Dec in MOSAIC_CENTER_STEP_ARCSEC steps until the nearest slit edge
      is MOSAIC_CENTER_TRUST_ARCSEC from the disk centre. The step is shorter
      than that radius, so the walk stops on the north side and never reaches
      the limb;
    - outside the trust radius |q| = e + S/2 and a short +Dec probe resolves the
      sign. The probe is shorter than |q|, so it cannot cross the centre.
    Once a measured correction has been applied, a residual inside the trust
    radius is chord noise (tens of arcsec), not dead-zone width.
    """
    R = SOLAR_DIAMETER / 2.0
    S = effective_slit_arcsec()
    trust = MOSAIC_CENTER_TRUST_ARCSEC
    step = MOSAIC_CENTER_STEP_ARCSEC

    if S >= SOLAR_DIAMETER:
        print("  Dec centring: disk fits the slit (S={:.0f}\"); using legacy loop".format(S))
        mosaic_center_uncertainty[0] = 0.0
        return center_dec()

    print("  Dec centring: chord method (slit={:.0f}\", disk={:.0f}\", dead zone +/-{:.0f}\")".format(
          S, SOLAR_DIAMETER, S / 2.0))

    m = measure_chord()
    if m is None:
        print("  Chord: first measurement failed")
        return False
    chord, dec0 = m
    if chord < MOSAIC_MIN_CHORD_ARCSEC:
        print("  Chord: too short ({:.0f}\")".format(chord))
        return False
    e1 = math.sqrt(max(R * R - (chord / 2.0) ** 2, 0.0))
    print("  Chord: chord={:.0f}\" -> nearest slit edge {:.0f}\" from centre".format(
          chord, e1))

    if e1 < trust:
        # Walk north until the chord is reliably shorter than the diameter.
        # From the centre the north edge of the dead zone is S/2+trust away;
        # starting at the south edge doubles that, so S+2*trust is enough.
        max_steps = int(math.ceil((S + 2.0 * trust) / step)) + 2
        print("  Chord: inside the dead zone; stepping +Dec (max {} steps)".format(
              max_steps))
        steps = 0
        while e1 < trust:
            if steps >= max_steps:
                print("  Chord: still inside the dead zone after {} steps".format(
                      steps))
                return False
            move_dec_by(step)
            steps += 1
            m = measure_chord()
            if m is None:
                print("  Chord: lost the disk while stepping +Dec")
                return False
            chord, dec0 = m
            if chord < MOSAIC_MIN_CHORD_ARCSEC:
                print("  Chord: too short ({:.0f}\")".format(chord))
                return False
            e1 = math.sqrt(max(R * R - (chord / 2.0) ** 2, 0.0))
            print("  Chord step {}: chord={:.0f}\" -> edge {:.0f}\"".format(
                  steps, chord, e1))

    corrected = False
    last_e = e1
    recovery = False  # a failed probe may leave the mount past the trailing limb
    for iteration in range(1, MOSAIC_CENTER_MAX_ITER + 1):
        if iteration > 1:
            m = measure_chord(rewind=recovery)
            recovery = False
            if m is None:
                print("  Chord: measurement failed")
                return False
            chord, dec0 = m
            if chord < MOSAIC_MIN_CHORD_ARCSEC:
                print("  Chord: too short ({:.0f}\")".format(chord))
                return False
            e1 = math.sqrt(max(R * R - (chord / 2.0) ** 2, 0.0))
            last_e = e1
            print("  Chord {}: chord={:.0f}\" -> nearest slit edge {:.0f}\"".format(
                  iteration, chord, e1))

        if corrected and e1 < trust:
            # The correction came from a trusted (outside-dead-zone) chord
            # measurement, so this residual is measurement noise.
            print("  Chord: corrected; residual nearest edge {:.0f}\"".format(e1))
            mosaic_center_uncertainty[0] = MOSAIC_CENTER_RESIDUAL_ARCSEC
            return True
        if e1 < trust:
            # The walk already left this state; getting here means Dec did not
            # move as commanded or the chord is unusable.
            print("  Chord: measurement is back inside the dead zone")
            return False

        # Magnitude is known from e1; a small +Dec probe tells us the side.
        q_abs = e1 + S / 2.0
        p = min(0.3 * R, max(0.15 * R, 0.6 * (R - e1)))
        move_dec_by(p)
        m2 = measure_chord()
        if m2 is None:
            # Probing +Dec lost the disk => the slit was north of centre.
            q = q_abs
            e2 = None
            recovery = True
        else:
            e2 = math.sqrt(max(R * R - (m2[0] / 2.0) ** 2, 0.0))
            q = q_abs if e2 > e1 else -q_abs
        print("  Chord {}: probe {:+.0f}\" -> offset estimate {:+.0f}\"".format(
              iteration, p, q))

        target_dec = dec0 - q / 3600.0
        move_dec_by((target_dec - mount.Dec) * 3600.0)
        corrected = True

    # The last correction has not been measured yet. Verify it once.
    m = measure_chord(rewind=recovery)
    if m is not None:
        e = math.sqrt(max(R * R - (m[0] / 2.0) ** 2, 0.0))
        last_e = e
        if e < trust:
            print("  Chord: max iterations, final nearest edge {:.0f}\" (accepted)".format(e))
            mosaic_center_uncertainty[0] = MOSAIC_CENTER_RESIDUAL_ARCSEC
            return True

    print("  Chord: could not centre Dec (last e={:.0f}\")".format(last_e))
    return False

# ── Mosaic planning and execution ─────────────────────────────────────────────
# The slit's angular length is set by the focal length; the camera only samples
# part of it. The effective strip is therefore min(slit, ROI width) in arcsec.
def compute_mosaic_plan(center_dec_deg, uncertainty=None):
    """Return strip/step/rows and the signed Dec of every row.

    `uncertainty` is the known bound on how far the assumed centre may be from
    the true disk centre. If omitted, the value recorded by the Dec centring
    routine is used.
    """
    if uncertainty is None:
        uncertainty = mosaic_center_uncertainty[0]
    roi = cam.ROI
    if roi is None or roi.Width <= 0 or roi.Height <= 0:
        raise RuntimeError("Camera ROI is not available; cannot plan the mosaic.")
    strip = effective_slit_arcsec()
    step = strip * (1.0 - MOSAIC_OVERLAP)
    if step <= 0:
        raise RuntimeError("MOSAIC_OVERLAP must be below 1.0.")

    # After chord centring the assumed centre can still be off by the residual
    # measurement noise. Extend the covered span by that amount on each side
    # (or by the user margin if larger) so the outermost rows still include the
    # limb.
    margin = max(MOSAIC_MARGIN_ARCSEC, uncertainty)

    if strip >= SOLAR_DIAMETER:
        # One strip already covers the whole disk. Centring uncertainty is
        # irrelevant here, so do not add rows just to satisfy the margin.
        rows = 1
    else:
        span = SOLAR_DIAMETER + 2.0 * margin
        rows = int(math.ceil((span - strip) / step)) + 1
        rows = max(1, rows)

    positions = [center_dec_deg + ((i - (rows - 1) / 2.0) * step) / 3600.0
                 for i in range(rows)]
    return {"strip": strip, "step": step, "rows": rows, "positions": positions,
            "margin": margin, "roi_w": roi.Width, "roi_h": roi.Height}

# Capture labels. SharpCap's filename template must contain {TargetName} for
# these to reach the SER filenames; the script only changes the target name.
_target_name_original = [None]

def set_capture_label(label):
    try:
        if _target_name_original[0] is None:
            _target_name_original[0] = SharpCap.TargetName
        base = _target_name_original[0] or "Solar"
        SharpCap.TargetName = "{}_{}".format(base, label)
    except Exception as e:
        print("  TargetName: {}".format(e))

def restore_capture_label():
    if _target_name_original[0] is not None:
        try:
            SharpCap.TargetName = _target_name_original[0]
        except:
            pass


def capture_folder():
    """Best-effort location where SharpCap writes the SER files.

    Falls back to the scan-log folder or the current directory when SharpCap
    does not expose a capture path, so the CSV/JSON can still be written.
    """
    for attr in ("CaptureFolder", "CapturePath", "OutputFolder"):
        try:
            v = getattr(SharpCap, attr)
        except:
            continue
        if v:
            return str(v)
    if SCAN_LOG_PATH:
        return os.path.dirname(os.path.abspath(SCAN_LOG_PATH))
    try:
        return os.getcwd()
    except:
        return None


def free_disk_gb(path=None):
    """Free space in GB for the capture folder, or None if unknown."""
    try:
        import System.IO
        if path is None:
            path = capture_folder()
        if not path:
            return None
        root = System.IO.Path.GetPathRoot(path)
        drive = System.IO.DriveInfo(root)
        return float(drive.AvailableFreeSpace) / 1e9
    except:
        return None


def write_scan_log(verbose=True):
    """Write one CSV row per scan next to the SER files. Best effort.

    The CSV is the link between the capture and the stacking/mosaic tools:
    it records the rate that was actually used, the frames written and the
    Dec at the start of each scan, which is what the mosaic needs to place
    each row. It is rewritten after every scan (``verbose=False``) so a crash
    mid-session still leaves the scans done so far.
    """
    if not scan_records:
        return None
    path = SCAN_LOG_PATH
    if not path:
        folder = capture_folder()
        if not folder:
            print("  Scan log: no usable folder; not written.")
            return None
        path = os.path.join(folder, "scan_log.csv")
    columns = [
        ("label", "label"),
        ("utc", "utc"),
        ("local", "local"),
        ("dec_start_deg", "dec_start"),
        ("dec_end_deg", "dec_end"),
        ("dec_offset_measured_arcsec", "dec_offset_measured_arcsec"),
        ("ra_start_h", "ra_start"),
        ("ra_end_h", "ra_end"),
        ("displacement_arcsec", "displacement"),
        ("fps_used", "fps_used"),
        ("scan_multiplier", "scan_multiplier"),
        ("scan_speed_arcsec_s", "scan_speed"),
        ("cos_dec", "cos_dec_correction"),
        ("calibration_factor", "calibration_factor"),
        ("padded_seconds", "padded_seconds"),
        ("scan_duration_s", "scan_duration"),
        ("frames", "frames"),
        ("frames_expected", "frames_expected"),
        ("frames_short_pct", "frames_short_pct"),
        ("move_seconds", "move_seconds"),
        ("actual_fps", "actual_fps"),
        ("actual_xy", "actual_xy"),
        ("slit_extent_px", "slit_extent_px"),
        ("problem", "problem"),
    ]
    try:
        with open(path, "w") as fh:
            fh.write(",".join(c[0] for c in columns) + "\n")
            for rec in scan_records:
                vals = []
                for _, key in columns:
                    v = rec.get(key, "")
                    if isinstance(v, float):
                        vals.append("{:.6f}".format(v))
                    elif v is None:
                        vals.append("")
                    else:
                        vals.append(str(v))
                fh.write(",".join(vals) + "\n")
        if verbose:
            print("  Scan log: {}".format(path))
        return path
    except Exception as e:
        print("  Scan log: could not write {} ({})".format(path, e))
        return None


def _session_meta():
    """Effective configuration + mosaic plan, as a JSON-serialisable dict."""
    meta = {
        "script": "nightscribe-scan.py",
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "optics": {
            "focal_length_mm": FOCAL_LENGTH,
            "pixel_size_um": PIXEL_SIZE,
            "pixel_scale_arcsec_px": PIXEL_SCALE,
            "solar_diameter_arcsec": SOLAR_DIAMETER,
            "solar_diameter_source": (
                "override" if EPHEMERIS_DIAMETER_OVERRIDE > 0 else
                ("ephemeris" if _EPHEMERIS_DIAMETER > 0 else "fallback")),
            "sidereal_arcsec_s": SIDEREAL,
        },
        "scan": {
            "fps": fps,
            "scan_multiplier": scan_multiplier,
            "scan_speed_arcsec_s": scan_speed,
            "crossing_time_s": crossing_time,
            "padded_seconds": padded_seconds,
            "scan_duration_s": scan_duration,
            "capture_fps_override": CAPTURE_FPS,
            "fps_probe_seconds": FPS_PROBE_SECONDS,
            "cos_dec_correction": COS_DEC_CORRECTION,
        },
        "calibration": {
            "scan_rate": dict(_calibration[0]),
        },
        "slit": {
            "length_mm": MOSAIC_SLIT_LENGTH_MM,
            "image_scale_configured": MOSAIC_SLIT_IMAGE_SCALE,
            "image_scale_used": _slit_scale[0],
            "measured_extent_px": (
                _slit_extent[0][0] if _slit_extent[0] else None),
            "measured_S_Y_arcsec_px": (
                _slit_extent[0][2] if _slit_extent[0] else None),
        },
        "mosaic": {
            "enabled": ENABLE_MOSAIC,
            "effective_slit_arcsec": effective_slit_arcsec()
            if ENABLE_MOSAIC else None,
            "overlap": MOSAIC_OVERLAP,
            "margin_arcsec": MOSAIC_MARGIN_ARCSEC,
            "scans_per_row": MOSAIC_SCANS_PER_ROW,
            "auto_center_dec": MOSAIC_AUTO_CENTER_DEC,
        },
    }
    return meta


def write_session_meta(extra=None, verbose=True):
    """Write session_meta.json with the effective configuration and the plan."""
    path = SESSION_META_PATH
    if not path:
        folder = capture_folder()
        if not folder:
            return None
        path = os.path.join(folder, "session_meta.json")
    meta = _session_meta()
    if extra:
        meta.update(extra)
    try:
        import json
        with open(path, "w") as fh:
            json.dump(meta, fh, indent=2, sort_keys=True)
        if verbose:
            print("  Session meta: {}".format(path))
        return path
    except Exception as e:
        print("  Session meta: could not write {} ({})".format(path, e))
        return None

scan_records = []

def scan_once(limb_ra, label=None, mosaic_mode=False, crossing_arcsec=None):
    """Back up padding, optional capture, scan across the disk, stop.

    Returns a dict with the scan record (displacement, timings, frame counts
    and the FPS actually achieved) and appends it to ``scan_records``.

    With mosaic_mode=False this is the single-scan motion. In both modes:

    - The mount's RA rate is a coordinate rate; the true sky rate is
      rate*cos(Dec). Dividing the multiplier by cos(Dec) (COS_DEC_CORRECTION)
      keeps one pixel per frame and the X:Y ratio at 1.000 at any declination.
    - The padding backup uses the same rate as the scan, so the padding is
      symmetric and exact (the old single-scan backup at LIMB_SEARCH_SPEED was
      longer than the padding budget and could clip the trailing limb).
    - A short forward pre-load (SCAN_PRELOAD_SECONDS) takes up the RA backlash
      before the capture starts, so the first recorded frames are not a
      stationary (duplicated) strip at the leading edge.

    ``crossing_arcsec`` overrides the on-disk span used to size the scan
    (default: the full solar diameter). Mosaic rows pass the chord at their
    Dec, so an outer row does not record the missing part of the diameter as
    empty sky.

    The mount only starts moving once SharpCap confirms that frames are being
    written, so the leading limb is not lost during file creation.
    """
    if mosaic_mode or COS_DEC_CORRECTION:
        cosd = max(0.5, math.cos(math.radians(mount.Dec)))
        scan_rate = scan_multiplier / cosd
    else:
        cosd = 1.0
        scan_rate = scan_multiplier
    backup_rate = -scan_rate

    # On-disk span of this scan: the full diameter for a single scan, the chord
    # at the row's Dec for a mosaic row. Duration = padding + span + padding.
    if crossing_arcsec is not None and crossing_arcsec > 0:
        crossing = crossing_arcsec
    else:
        crossing = SOLAR_DIAMETER
    duration = 2.0 * padded_seconds + crossing / scan_speed
    trail_margin_s = (TRAILING_MARGIN_ARCSEC / scan_speed
                      if scan_speed > 0 else 0.0)

    # Fixed arcsec padding (padded_seconds) rather than a fixed time in the
    # non-mosaic path, so the captured sky is the same at any FPS.
    mount.MoveAxis(0, backup_rate)
    _sleep_abortable(padded_seconds)
    mount.Stop()
    time.sleep(0.2)

    # Forward pre-load: take up the RA gear backlash left by the reverse backup
    # so the scan starts moving straight away. It eats into the leading padding
    # by at most SCAN_PRELOAD_SECONDS, never more than half of it.
    if SCAN_PRELOAD_SECONDS > 0:
        mount.MoveAxis(0, scan_rate)
        _sleep_abortable(min(SCAN_PRELOAD_SECONDS, 0.5 * padded_seconds))
        mount.Stop()
        time.sleep(0.1)

    ra_start = mount.RA
    dec_start = mount.Dec
    frames0 = _capture_frame_count() if ENABLE_CAPTURE else None
    t_capture0 = None

    if ENABLE_CAPTURE:
        if not check_capture_space(label):
            raise RuntimeError(
                "Not enough free space for scan {} (label {}).".format(
                    label, label))
        if label is not None:
            set_capture_label(label)
        cam.PrepareToCapture()
        cam.RunCapture()
        if not wait_capture_start(frames0):
            try:
                cam.StopCapture()
            except:
                pass
            raise RuntimeError(
                "Capture did not start within {:.1f}s (label {}). Check the "
                "capture folder and free disk space.".format(
                    CAPTURE_START_TIMEOUT, label))
        t_capture0 = time.time()
        frames_move0 = _capture_frame_count()
        if frames_move0 is None:
            frames_move0 = frames0
    else:
        frames_move0 = None

    # Adaptive trailing-limb stop: watch the live frame's stddev and finish a
    # little after the disk has left the slit, instead of always recording the
    # full trailing padding. Falls back to the time cap if the live stream is
    # not delivering frames during the capture.
    watcher = None
    if TRAILING_LIMB_STOP:
        watcher = FrameWatcher(cam)
        watcher.start()
        watcher.wait_first(1.0)

    # ── The scan itself ──────────────────────────────────────────────────────
    # Sleep in small steps so Ctrl+C stops the mount immediately, and watch the
    # capture so a stall (disk full, driver hang, frame limit) aborts the scan
    # instead of silently producing an empty SER.
    t_move0 = time.time()
    mount.MoveAxis(0, scan_rate)
    frames_prev = frames_move0
    last_progress = t_move0
    scan_problem = None
    peak = 0.0
    trail_start = None
    trailing_stop = False
    try:
        while True:
            check_abort()
            elapsed = time.time() - t_move0
            remaining = duration - elapsed
            if remaining <= 1e-3:
                break
            time.sleep(min(0.05, remaining))
            if watcher is not None and watcher.latest is not None:
                s = watcher.latest[1]
                if s > peak:
                    peak = s
                if peak > ON_DISK_STDDEV and s < max(
                        LIMB_STDDEV_MIN, peak * TRAILING_STDDEV_FRACTION):
                    if trail_start is None:
                        trail_start = time.time()
                    elif (time.time() - trail_start) >= trail_margin_s:
                        trailing_stop = True
                        break
                else:
                    trail_start = None
            if ENABLE_CAPTURE:
                try:
                    if not cam.Capturing:
                        scan_problem = "capture stopped mid-scan"
                        break
                except:
                    pass
                now = _capture_frame_count()
                if now is not None and frames_prev is not None:
                    if now != frames_prev:
                        frames_prev = now
                        last_progress = time.time()
                    elif CAPTURE_STALL_SECONDS > 0 and \
                            (time.time() - last_progress) > CAPTURE_STALL_SECONDS:
                        scan_problem = "no frames written for {:.1f}s".format(
                            CAPTURE_STALL_SECONDS)
                        break
    finally:
        if watcher is not None:
            watcher.stop()
    mount.Stop()
    t_move1 = time.time()

    frames_end = None
    actual_fps = 0.0
    if ENABLE_CAPTURE:
        try:
            cam.StopCapture()
        except:
            pass
        time.sleep(0.1)
        frames_end = _capture_frame_count()
        if frames_end is not None and frames_move0 is not None:
            elapsed = t_move1 - t_move0
            if elapsed > 0:
                actual_fps = (frames_end - frames_move0) / elapsed

    if trailing_stop:
        print("  Trailing limb: stopped after {:.0f}\" margin.".format(
              TRAILING_MARGIN_ARCSEC))

    if scan_problem is not None:
        print("  SCAN PROBLEM ({}): {}".format(label, scan_problem))
        record = _make_scan_record(label, limb_ra, ra_start, dec_start, cosd,
                                   scan_rate, t_move0, t_move1, t_capture0,
                                   frames_move0, frames_end, actual_fps,
                                   problem=scan_problem, duration=duration)
        scan_records.append(record)
        write_scan_log(verbose=False)
        write_scan_stats(record)
        raise RuntimeError(
            "Scan {} aborted: {}".format(label, scan_problem))

    disp = abs(ra_diff_arcsec(mount.RA, limb_ra))
    record = _make_scan_record(label, limb_ra, ra_start, dec_start, cosd,
                               scan_rate, t_move0, t_move1, t_capture0,
                               frames_move0, frames_end, actual_fps,
                               duration=duration)
    scan_records.append(record)
    _check_consistency(record)
    write_scan_log(verbose=False)   # keep the CSV current even if we crash later
    write_scan_stats(record)
    return record


def _make_scan_record(label, limb_ra, ra_start, dec_start, cosd, scan_rate,
                      t_move0, t_move1, t_capture0, frames_move0, frames_end,
                      actual_fps, problem=None, duration=None):
    """Build the per-scan record (also used for a scan that had a problem)."""
    if duration is None:
        duration = scan_duration
    frames = (frames_end - frames_move0
              if frames_end is not None and frames_move0 is not None else None)
    expected = duration * fps if fps > 0 else 0.0
    short_pct = 0.0
    if frames is not None and expected > 0:
        short_pct = max(0.0, (expected - frames) / expected)
        if short_pct > FRAMES_SHORT_TOL:
            print("  FRAMES SHORT: {} wrote {} of ~{:.0f} expected ({:.0f}% "
                  "missing)".format(label, frames, expected, short_pct * 100.0))
    disp = abs(ra_diff_arcsec(mount.RA, limb_ra))
    now_utc = _utc_components()
    return {
        "label": label or "",
        "ra_start": ra_start,
        "dec_start": dec_start,
        "ra_end": mount.RA,
        "dec_end": mount.Dec,
        "utc": ("{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}Z".format(*now_utc)
                if now_utc else ""),
        "local": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "displacement": disp,
        "scan_rate": scan_rate,
        "scan_speed": scan_rate * SIDEREAL,
        "cos_dec_correction": cosd,
        "fps_used": fps,
        "scan_multiplier": scan_multiplier,
        "scan_duration": duration,
        "padded_seconds": padded_seconds,
        "t_move0": t_move0,
        "t_move1": t_move1,
        "capture_seconds": (t_move1 - t_capture0) if t_capture0 else 0.0,
        "frames": frames,
        "frames_expected": round(expected, 1),
        "frames_short_pct": round(short_pct * 100.0, 2),
        "move_seconds": t_move1 - t_move0,
        "actual_fps": actual_fps,
        "calibration_factor": _calibration[0].get("factor", 1.0),
        "slit_extent_px": (_slit_extent[0][0] if _slit_extent[0] else None),
        "problem": problem or "",
        # X/Y of the reconstructed disk: the sky rate is the RA rate times
        # cos(Dec), so the ratio is just actual_fps / fps_used (1.000 = the
        # scan moved exactly one pixel-width of sky per frame).
        "actual_xy": (PIXEL_SCALE /
                      (scan_rate * sky_rate_per_unit() * cosd / actual_fps)
                      if actual_fps > 0 else 0.0),
    }

_consistency = [{"actual_xy": None, "extent": None}]


def _check_consistency(record):
    """Warn if a scan's rate/scale drifts from the first good scan.

    A row captured at a different scale than the others is what makes the
    mosaic need a rescale; catching it at capture time avoids the surprise.
    """
    xy = record.get("actual_xy", 0.0)
    ext = record.get("slit_extent_px")
    ref = _consistency[0]
    if xy and xy > 0:
        if ref["actual_xy"] is None:
            ref["actual_xy"] = xy
        else:
            dev = abs(xy - ref["actual_xy"]) / ref["actual_xy"]
            if dev > CONSISTENCY_TOL_XY:
                print("  CONSISTENCY: {} X/Y {:.3f} vs {:.3f} ({:+.1f}%)".format(
                    record.get("label"), xy, ref["actual_xy"],
                    (xy - ref["actual_xy"]) / ref["actual_xy"] * 100.0))
    if ext and ref["extent"] is not None:
        dev = abs(ext - ref["extent"]) / ref["extent"]
        if dev > CONSISTENCY_TOL_EXTENT:
            print("  CONSISTENCY: {} slit extent {:.0f} vs {:.0f} px ({:+.1f}%)".format(
                record.get("label"), ext, ref["extent"],
                (ext - ref["extent"]) / ref["extent"] * 100.0))
    elif ext:
        ref["extent"] = ext


def scan_stats_path():
    """Where the per-scan statistics JSON-lines file lives."""
    if SCAN_STATS_PATH:
        return SCAN_STATS_PATH
    folder = capture_folder()
    if not folder:
        return None
    return os.path.join(folder, "scan_stats.jsonl")


def write_scan_stats(record):
    """Append one JSON line of per-scan statistics (best effort)."""
    path = scan_stats_path()
    if not path:
        return None
    try:
        import json
        line = json.dumps({
            "label": record.get("label"),
            "utc": record.get("utc"),
            "local": record.get("local"),
            "dec_start": record.get("dec_start"),
            "frames": record.get("frames"),
            "frames_expected": record.get("frames_expected"),
            "frames_short_pct": record.get("frames_short_pct"),
            "actual_fps": record.get("actual_fps"),
            "actual_xy": record.get("actual_xy"),
            "slit_extent_px": record.get("slit_extent_px"),
            "calibration_factor": record.get("calibration_factor"),
            "problem": record.get("problem"),
        }, sort_keys=True)
        with open(path, "a") as fh:
            fh.write(line + "\n")
        return path
    except Exception as e:
        print("  Scan stats: could not write {} ({})".format(path, e))
        return None


def scan_with_retry(realign_fn, label, mosaic_mode=False):
    """Run scan_once, retrying up to SCAN_RETRIES times on failure.

    ``realign_fn`` is called before each attempt to (re)locate the leading limb
    and must return the RA of the leading limb, or False/None on failure.
    Returns the scan record, or None if every attempt failed.
    """
    attempts = 1 + max(0, SCAN_RETRIES)
    last_err = None
    for k in range(attempts):
        limb_ra = realign_fn()
        if limb_ra is False or limb_ra is None:
            last_err = "limb not found"
            print("  {}: attempt {}/{} - {}".format(label, k + 1, attempts, last_err))
            continue
        try:
            return scan_once(limb_ra, label, mosaic_mode=mosaic_mode)
        except RuntimeError as e:
            last_err = str(e)
            print("  {}: attempt {}/{} failed - {}".format(
                label, k + 1, attempts, last_err))
    print("  {}: giving up after {} attempt(s) ({})".format(
        label, attempts, last_err))
    return None


def rewind_before_disk(margin_arcsec=600.0):
    """Slew backwards in RA by more than a disk diameter.

    find_solar_limb() searches forward only, so it needs to start on the
    leading side. This is called once per mosaic row, after the Dec move, to
    restore that precondition regardless of where the previous row ended.
    """
    dec = mount.Dec
    span = (SOLAR_DIAMETER + margin_arcsec) / max(0.5, math.cos(math.radians(dec)))
    t = span / (RETURN_MULTIPLIER * SIDEREAL)
    mount.MoveAxis(0, -RETURN_MULTIPLIER)
    time.sleep(t)
    mount.Stop()
    time.sleep(0.2)

def run_sequence():
    """The single-sequence path. Uses scan_once() like the mosaic path."""
    global return_time
    for cycle in range(NUM_CYCLES):
        cycle_start = time.time()
        print("\n-- CYCLE {} of {} --".format(cycle + 1, NUM_CYCLES))

        # Re-do the full alignment (limb + Dec + exposure) on the first cycle
        # and every DEC_INTERVAL cycles thereafter. Intermediate cycles only
        # need to re-find the leading limb, which is much cheaper.
        need_full = (cycle == 0) or (cycle % DEC_INTERVAL == 0)

        if need_full:
            limb_ra = do_full_alignment()
            if cycle == 0:
                # Exposure is now final: fix the scan rate with the real FPS,
                # then self-calibrate it against the measured limb crossing.
                measure_capture_fps("after auto-exposure (cycle 0)")
                calibrate_scan_rate()
        else:
            limb_ra = do_quick_realign()

        if limb_ra is False:
            print("ABORTING: Could not find solar limb.")
            break

        # Log (cycle, t, RA, Dec) of the leading limb each cycle so we can
        # report drift against cycle 0 at the end of the session.
        limb_dec = mount.Dec
        drift_log.append((cycle + 1, time.time(), limb_ra, limb_dec))

        if len(drift_log) > 1:
            ref = drift_log[0]
            dt  = drift_log[-1][1] - ref[1]
            dra = ra_diff_arcsec(drift_log[-1][2], ref[2])
            # mount.Dec is in degrees; ×3600 → arcsec.
            ddec = (drift_log[-1][3] - ref[3]) * 3600
            if dt > 0:
                print("  Drift: RA {:+.1f}\"  Dec {:+.1f}\"  ({:.0f}s)".format(
                      dra, ddec, dt))

        record = scan_with_retry(do_quick_realign, None, mosaic_mode=False)
        if record is None:
            print("ABORTING: scan failed after retries.")
            break
        displacement = record["displacement"]
        return_time  = displacement / (RETURN_MULTIPLIER * SIDEREAL)

        cycle_elapsed = time.time() - cycle_start
        print("  {} {:.0f}\" moved. Cycle: {:.1f}s".format(
              "Scan done." if ENABLE_CAPTURE else "Dry run done.",
              displacement, cycle_elapsed))
        if record["actual_fps"] > 0:
            print("  Timing: used {:.2f} fps, measured {:.2f} fps  ->  X/Y {:.3f}".format(
                  record["fps_used"], record["actual_fps"], record["actual_xy"]))

        # Fast return to start position; skipped on the last cycle since the
        # post-loop "Repositioning to disk center" block handles that case.
        if cycle < NUM_CYCLES - 1:
            mount.MoveAxis(0, -RETURN_MULTIPLIER)
            time.sleep(return_time)
            mount.Stop()
            time.sleep(0.5)
    return True

mosaic_log = []
mosaic_center_dec = [None]

def run_mosaic():
    """One alignment, then rows x scans with fixed exposure and labelled SERs."""
    global return_time
    print("\nMosaic: initial alignment...")
    # Guarantee a leading-side start even if the previous session left the
    # mount past the trailing limb.
    rewind_before_disk()
    if not find_solar_limb():
        return False

    if MOSAIC_AUTO_CENTER_DEC:
        if not center_dec_chord():
            print("ABORTING: could not centre Dec.")
            return False
    else:
        print("  Dec centring: manual (MOSAIC_AUTO_CENTER_DEC=False)")
        # The user positioned the slit; trust it and do not inflate the plan.
        mosaic_center_uncertainty[0] = 0.0

    centre_dec = mount.Dec
    mosaic_center_dec[0] = centre_dec

    # Exposure is measured once at the disk centre and then held fixed for every
    # row, so the strips match and can be stitched without brightness steps.
    if ENABLE_AUTO_EXP:
        half_t = (SOLAR_DIAMETER / 2.0) / (RETURN_MULTIPLIER * SIDEREAL)
        mount.MoveAxis(0, RETURN_MULTIPLIER)
        time.sleep(half_t)
        mount.Stop()
        time.sleep(1.0)
        print("  Auto-exposure...")
        auto_exposure()
        # Still at the disk centre: measure the slit-axis extent for the scale.
        measure_slit_scale()
        mount.MoveAxis(0, -RETURN_MULTIPLIER)
        time.sleep(half_t)
        mount.Stop()
        time.sleep(0.2)

    # The exposure is final here, so this is the right moment to measure the
    # real capture FPS. The value applies to every row and scan, which keeps
    # the X scale identical across the whole mosaic.
    measure_capture_fps("after auto-exposure")

    # Self-calibrate the scan rate against the measured limb crossing, so the X
    # sampling is 1:1 regardless of the configured focal length or reported FPS.
    calibrate_scan_rate()

    plan = compute_mosaic_plan(centre_dec)
    print("  Plan: strip={:.0f}\" step={:.0f}\" rows={} scans/row={} margin={:.0f}\"".format(
          plan["strip"], plan["step"], plan["rows"], MOSAIC_SCANS_PER_ROW,
          plan["margin"]))
    print("        (effective slit {:.0f}\", slit image scale {:.4f}; the "
          "row step is along the slit axis)".format(
              effective_slit_arcsec(), _slit_scale[0]))
    if _slit_extent[0] is None:
        print("        WARNING: slit extent could not be measured; the plan "
              "uses the configured slit image scale {:.4f}.".format(
                  _slit_scale[0]))
    print("        (margin covers slit span and centring uncertainty)")
    for i, dec in enumerate(plan["positions"]):
        print("    row {:02d}: Dec {:+.2f} deg ({:+.0f}\" from centre)".format(
              i + 1, dec, (dec - centre_dec) * 3600.0))

    # Coverage check: the outermost rows must reach the poles of the disk (plus
    # margin) or part of the Sun will be missing from the mosaic.
    if plan["positions"]:
        R = SOLAR_DIAMETER / 2.0
        north = max((d - centre_dec) * 3600.0 for d in plan["positions"])
        south = min((d - centre_dec) * 3600.0 for d in plan["positions"])
        need = R + MOSAIC_MARGIN_ARCSEC
        if north < need or -south < need:
            print("  WARNING: rows cover {:+.0f}\"/{:+.0f}\" but the disk "
                  "(R={:.0f}\") plus margin needs +/-{:.0f}\". Part of the Sun "
                  "may be missing.".format(north, south, R, need))

    # Record the effective configuration and the plan for reproducibility.
    write_session_meta(extra={"mosaic_plan": {
        "centre_dec_deg": centre_dec,
        "strip_arcsec": plan["strip"],
        "step_arcsec": plan["step"],
        "rows": plan["rows"],
        "positions_dec_deg": list(plan["positions"]),
        "margin_arcsec": plan["margin"],
    }}, verbose=False)

    scanned_any = False
    stop_rows = False
    for row, target_dec in enumerate(plan["positions"]):
        move_dec_by((target_dec - mount.Dec) * 3600.0)
        # Start every row from a known side of the disk. A margin row may sit
        # far enough out that the slit misses the disk; if the RA is then left
        # on the trailing side, a forward limb search would run away. Rewinding
        # more than a disk diameter makes the search deterministic.
        rewind_before_disk()

        row_started = False
        for scan in range(MOSAIC_SCANS_PER_ROW):
            check_abort()
            # The first scan of a row starts from the rewind point, a disk
            # diameter plus 600" before the leading limb; at 4x sidereal that
            # is ~45 s, so allow 90 s. Later scans start next to the limb.
            if not do_quick_realign(max_search=(90 if scan == 0 else None)):
                if row_started:
                    print("ABORTING: limb lost in row {}, scan {}.".format(
                          row + 1, scan + 1))
                    return False
                if scanned_any:
                    # Rows are ordered; an off-disk row after real data can only
                    # be the trailing margin. The 15 % overlap guarantees the
                    # disk is already covered.
                    print("  Row {}: no disk in the slit; trailing margin reached.".format(
                          row + 1))
                    stop_rows = True
                    break
                print("  Row {}: no disk in the slit; skipping leading margin row.".format(
                      row + 1))
                break
            row_started = True
            scanned_any = True

            # Keep the slit on the row's Dec. Drift between scans of the same
            # row showed up as ~40" in Y (~100 px) in the 2026-11-01 data,
            # which is enough to blur a stack.
            if scan > 0:
                dec_err = (target_dec - mount.Dec) * 3600.0
                if abs(dec_err) > DEC_DRIFT_TOL_ARCSEC:
                    print("  Dec drift {:+.0f}\"; recentring to {:.0f}\"...".format(
                          -dec_err, target_dec * 3600.0))
                    move_dec_by(dec_err)

            limb_ra = mount.RA
            label = "r{:02d}_s{:02d}".format(row + 1, scan + 1)
            print("\n-- MOSAIC row {}/{} scan {}/{} ({}) --".format(
                  row + 1, plan["rows"], scan + 1, MOSAIC_SCANS_PER_ROW, label))

            # On-disk span at this row's Dec: the chord at the nearest slit
            # edge (MOSAIC geometry). Sizing the scan to the chord instead of
            # the full diameter stops outer rows from recording the missing
            # part of the diameter as empty sky.
            row_chord = expected_chord_arcsec(
                (mount.Dec - centre_dec) * 3600.0, effective_slit_arcsec(),
                SOLAR_DIAMETER)

            # Retry the scan once on a capture problem before giving up on it.
            record = None
            attempts = 1 + max(0, SCAN_RETRIES)
            for k in range(attempts):
                if k == 0:
                    attempt_limb = limb_ra
                else:
                    attempt_limb = do_quick_realign()
                if attempt_limb is False or attempt_limb is None:
                    print("  {}: realign failed on attempt {}/{}".format(
                          label, k + 1, attempts))
                    continue
                try:
                    record = scan_once(attempt_limb, label, mosaic_mode=True,
                                       crossing_arcsec=row_chord)
                    break
                except RuntimeError as e:
                    print("  {}: attempt {}/{} failed - {}".format(
                          label, k + 1, attempts, e))
            if record is None:
                print("ABORTING: scan {} failed after {} attempt(s).".format(
                      label, attempts))
                return False

            # Record the measured Dec offset of the row (from the mount, not the
            # nominal plan) so the mosaic can use the real geometry.
            record["dec_offset_measured_arcsec"] = (
                (record["dec_start"] - centre_dec) * 3600.0)
            disp = record["displacement"]
            return_time = disp / (RETURN_MULTIPLIER * SIDEREAL)
            mosaic_log.append((row + 1, scan + 1, label, limb_ra,
                               record["dec_start"], disp))
            print("  {:.0f}\" moved.  Dec offset {:+.0f}\" (measured)".format(
                  disp, record["dec_offset_measured_arcsec"]))
            if record["actual_fps"] > 0:
                print("  Timing: used {:.2f} fps, measured {:.2f} fps  ->  X/Y {:.3f}".format(
                      record["fps_used"], record["actual_fps"], record["actual_xy"]))

            is_last = (row == plan["rows"] - 1 and
                       scan == MOSAIC_SCANS_PER_ROW - 1)
            if not is_last:
                mount.MoveAxis(0, -RETURN_MULTIPLIER)
                time.sleep(return_time)
                mount.Stop()
                time.sleep(0.5)

        if stop_rows:
            break

    # Put Dec back at the disk centre; the common post-run block centres RA.
    if mosaic_center_dec[0] is not None:
        print("\nReturning Dec to disk centre...")
        move_dec_by((mosaic_center_dec[0] - mount.Dec) * 3600.0)
    return True
# ── Pre-flight info ───────────────────────────────────────────────────────────
if ENABLE_MOSAIC:
    if MOSAIC_SCANS_PER_ROW < 1:
        raise RuntimeError("MOSAIC_SCANS_PER_ROW must be at least 1.")
    if not (0.0 <= MOSAIC_OVERLAP < 1.0):
        raise RuntimeError("MOSAIC_OVERLAP must be in [0, 1).")
    if MOSAIC_SLIT_LENGTH_MM <= 0 or MOSAIC_SLIT_IMAGE_SCALE <= 0:
        raise RuntimeError(
            "MOSAIC_SLIT_LENGTH_MM and MOSAIC_SLIT_IMAGE_SCALE must be > 0.")

    # The plan after centring uses the residual uncertainty; use the same value
    # here so the duration warning matches the real plan.
    strip_pre = effective_slit_arcsec()
    if MOSAIC_AUTO_CENTER_DEC and strip_pre < SOLAR_DIAMETER:
        unc_pre = MOSAIC_CENTER_RESIDUAL_ARCSEC
    else:
        unc_pre = 0.0
    mplan_pre = compute_mosaic_plan(mount.Dec, uncertainty=unc_pre)
    total_scans = mplan_pre["rows"] * MOSAIC_SCANS_PER_ROW
    # Each calibration chord = limb search + two traverses of the disk.
    chord_cost = 2.0 * SOLAR_DIAMETER / (MOSAIC_CHORD_SPEED * SIDEREAL) + 6.0
    if MOSAIC_AUTO_CENTER_DEC and strip_pre < SOLAR_DIAMETER:
        # Upper bound: walk out of the dead zone + probe/correct iterations.
        stair_steps = int(math.ceil(
            (strip_pre + 2.0 * MOSAIC_CENTER_TRUST_ARCSEC)
            / MOSAIC_CENTER_STEP_ARCSEC)) + 2
        cal_chords = stair_steps + 2 * MOSAIC_CENTER_MAX_ITER + 1
    else:
        cal_chords = 0
    est_total = (cal_chords * chord_cost
                 + total_scans * (padded_seconds + scan_duration + 8.0)
                 + mplan_pre["rows"] * mplan_pre["step"] /
                   (MOSAIC_DEC_SPEED * SIDEREAL))
else:
    est_first = 15 + padded_seconds + scan_duration + 6
    est_fast  = 3 + padded_seconds + scan_duration + 6
    est_total = est_first + (NUM_CYCLES - 1) * est_fast

print("=" * 60)
print("NightScribe — solar SHG fast-burst scanner")
print("Mode: {}".format("multi-row mosaic" if ENABLE_MOSAIC
                        else "single sequence"))
if not ENABLE_CAPTURE or not ENABLE_AUTO_EXP:
    flags = []
    if not ENABLE_CAPTURE:
        flags.append("CAPTURE OFF")
    if not ENABLE_AUTO_EXP:
        flags.append("AUTO-EXP OFF")
    print("*** DRY RUN: {} ***".format(", ".join(flags)))
print("=" * 60)
print("FPS: {:.1f}  Scale: {:.3f}\"/px  Scan: {:.2f}x ({:.1f}\"/s)".format(
      fps, PIXEL_SCALE, scan_multiplier, scan_speed))
print("Crossing: {:.1f}s  Duration: {:.1f}s  X/Y: {:.3f}".format(
      crossing_time, scan_duration,
      PIXEL_SCALE / (scan_speed / fps)))
print("Solar diameter: {:.1f}\" ({})  Expected disk: {:.0f} px".format(
      SOLAR_DIAMETER,
      "override" if EPHEMERIS_DIAMETER_OVERRIDE > 0 else
      ("ephemeris" if _EPHEMERIS_DIAMETER > 0 else "fallback"),
      SOLAR_DIAMETER / PIXEL_SCALE))
print("Padding: {:.1f}s/side ({:.0f}\")".format(
      padded_seconds, padded_seconds * scan_speed))
if PADDED_ARCSEC > 0:
    print("Padding source: fixed {:.0f}\" (FPS-independent)".format(PADDED_ARCSEC))
else:
    print("Padding source: {}s per side (FPS-dependent; set PADDED_ARCSEC "
          "for repeatability)".format(PADDED_DURATION))
print("FPS will be re-measured after auto-exposure "
      "(CAPTURE_FPS={:g}, probe={:g}s)".format(CAPTURE_FPS, FPS_PROBE_SECONDS))
print("Scan-rate calibration: {}".format(
      "ON (self-calibrates X sampling)" if CALIBRATE_SCAN_RATE else "OFF"))
print("Capture guards: start<={:.1f}s  stall>{:.1f}s  frames short>{:.0f}%  "
      "min free={:.1f} GB  retries={}".format(
          CAPTURE_START_TIMEOUT, CAPTURE_STALL_SECONDS, FRAMES_SHORT_TOL * 100.0,
          CAPTURE_MIN_FREE_GB, SCAN_RETRIES))
if ENABLE_MOSAIC:
    print("Mosaic: slit={:.1f}mm  image scale={:.4f}  strip={:.0f}\"  step={:.0f}\"  "
          "rows={}  scans/row={}".format(
              MOSAIC_SLIT_LENGTH_MM, _slit_scale[0], mplan_pre["strip"],
              mplan_pre["step"], mplan_pre["rows"], MOSAIC_SCANS_PER_ROW))
    print("Mosaic scans: {}  Dec centring: {}".format(
          total_scans,
          "automatic (chord / legacy)" if MOSAIC_AUTO_CENTER_DEC else "manual"))
    if cal_chords:
        print("Chord calibration: up to {} chords (~{:.0f} min; usually less)".format(
              cal_chords, cal_chords * chord_cost / 60.0))
    bpp = 2 if full_scale() > 255 else 1
    data_gb = (mplan_pre["roi_w"] * mplan_pre["roi_h"] * bpp *
               fps * scan_duration * total_scans) / 1e9
    print("Est. data: ~{:.1f} GB  (ROI {}x{}, {} bit)".format(
          data_gb, mplan_pre["roi_w"], mplan_pre["roi_h"], bpp * 8))

    # Free space is only knowable through .NET; on some installs the capture
    # folder property is missing, in which case we just skip the check.
    free_gb = free_disk_gb()
    folder = capture_folder()
    if folder:
        print("Capture folder: {}".format(folder))
    if free_gb is not None:
        print("Free space: {:.1f} GB".format(free_gb))
        if free_gb < data_gb * 1.2:
            print("*** WARNING: less than 20% headroom over the estimated data. ***")
    if FPS_PROBE_SECONDS > 0:
        print("FPS probe: a small *_fpsprobe SER will be written and can be deleted.")
else:
    print("Cycles: {}  Return: {:.0f}x  Dec every: {} cycles".format(
          NUM_CYCLES, RETURN_MULTIPLIER, DEC_INTERVAL))
    print("Dec centering: {}".format(
          "ON (left/right loop)" if ENABLE_DEC_CENTERING else
          "OFF (ENABLE_DEC_CENTERING=False)"))
print("Full scale: {:.0f} ADU  Exp: p{:.1f} -> {:.0%}".format(
      full_scale(), EXP_PERCENTILE * 100, EXP_TARGET_LEVEL))
print("Est. total: {:.0f}s ({:.1f} min)  (Ctrl+C to abort)".format(
      est_total, est_total / 60))
print("=" * 60)

# Record the effective configuration before the session starts (the mosaic path
# overwrites it later with the full plan).
write_session_meta(verbose=True)

# ── Main loop ─────────────────────────────────────────────────────────────────
drift_log = []
session_start = time.time()
return_time = 0
def run_capture():
    """Run the capture session (single scan or mosaic)."""
    global session_start
    session_start = time.time()
    del drift_log[:]
    del scan_records[:]
    del mosaic_log[:]
    session_ok = True

    try:
        if ENABLE_MOSAIC:
            session_ok = run_mosaic()
        else:
            run_sequence()

        if session_ok:
            # After all scans: park on disk centre. We were at the trailing-edge
            # end of the last scan; reverse for return_time, then re-find the
            # leading limb and step half a disk-diameter forward to the centre.
            print("\nRepositioning to disk center...")
            mount.MoveAxis(0, -RETURN_MULTIPLIER)
            time.sleep(return_time)
            mount.Stop()
            time.sleep(0.5)

            if find_solar_limb():
                half_t = (SOLAR_DIAMETER / 2.0) / (RETURN_MULTIPLIER * SIDEREAL)
                mount.MoveAxis(0, RETURN_MULTIPLIER)
                time.sleep(half_t)
                mount.Stop()
                print("Centered on solar disk.")
        else:
            print("\nSkipping final repositioning (session stopped early).")

        mount.Stop()

    except KeyboardInterrupt:
        # Set the abort flag so any handlers/watchers still spinning will exit
        # their wait loops on the next check_abort(). Then make a best-effort
        # attempt to stop the mount and any in-progress capture — wrapped in
        # try/except because we may be in any state (mount disconnected,
        # capture not actually running, etc.) and we never want the cleanup
        # itself to throw.
        print("\n\n*** ABORTED BY USER ***")
        _abort[0] = True
        session_ok = False
        try:
            mount.Stop()
        except:
            pass
        try:
            if cam.Capturing:
                cam.StopCapture()
        except:
            pass
        restore_capture_label()

    except Exception:
        # Unexpected error: leave the mount and capture in a safe state, restore
        # the target name, then re-raise so the SharpCap console shows the real
        # traceback.
        _abort[0] = True
        try:
            mount.Stop()
        except:
            pass
        try:
            if cam.Capturing:
                cam.StopCapture()
        except:
            pass
        restore_capture_label()
        raise

    restore_capture_label()

    # ── Session summary ───────────────────────────────────────────────────────────
    total_time = time.time() - session_start

    print("")
    print("=" * 60)
    print("SESSION COMPLETE" if session_ok else "SESSION STOPPED EARLY")
    print("=" * 60)

    # NightScribe: calibration and slit-geometry summary (both modes).
    _cal = _calibration[0]
    if _cal.get("applied"):
        print("Scan calibration: measured {:.2f}\"/s vs target {:.2f}\"/s "
              "(factor {:.4f})".format(
                  _cal["measured_rate"], _cal["target_rate"], _cal["factor"]))
    if _slit_extent[0]:
        _ext, _roi_w, _sy = _slit_extent[0]
        print("Slit: extent {:.0f}/{:.0f} px  S_Y={:.3f}\"/px  scale={:.4f}".format(
              _ext, _roi_w, _sy, _slit_scale[0]))

    if ENABLE_MOSAIC:
        rows_done = len(set([e[0] for e in mosaic_log]))
        print("Scans: {}  Rows: {}  Total: {:.0f}s ({:.1f} min)".format(
              len(mosaic_log), rows_done, total_time, total_time / 60))
        if mosaic_log:
            print("")
            print("MOSAIC LAYOUT")
            for (row, scan, label, ra, dec, disp) in mosaic_log:
                if mosaic_center_dec[0] is not None:
                    rel = (dec - mosaic_center_dec[0]) * 3600.0
                else:
                    rel = 0.0
                print("  row {:02d} scan {:02d}  Dec {:+.0f}\" from centre  {}  {:.0f}\" scanned".format(
                      row, scan, rel, label, disp))
            print("")
            print("Stack the scans of each row (tools/align_stack_recon.py), then")
            print("stitch the rows (tools/mosaic_from_recon.py). See docs/USER_GUIDE.md.")
    else:
        print("Cycles: {}  Total: {:.0f}s ({:.1f} min)  Avg: {:.1f}s/cycle".format(
              len(drift_log), total_time, total_time / 60,
              total_time / len(drift_log) if drift_log else 0))

        if len(drift_log) > 1:
            ref  = drift_log[0]
            last = drift_log[-1]
            dt   = last[1] - ref[1]
            dra  = ra_diff_arcsec(last[2], ref[2])
            ddec = (last[3] - ref[3]) * 3600

            print("")
            print("POLAR ALIGNMENT DRIFT")
            print("RA  : {:+.1f}\"  ({:+.1f}\"/min)".format(dra, dra / dt * 60 if dt > 0 else 0))
            print("Dec : {:+.1f}\"  ({:+.1f}\"/min)".format(ddec, ddec / dt * 60 if dt > 0 else 0))

            # If drift exceeds 5"/min in either axis, suggest a polar-alignment
            # tweak. Large RA drift = azimuth error; large Dec drift = altitude
            # error. The hints are for the northern hemisphere — flip in the south.
            if dt > 0 and (abs(dra / dt * 60) > 5 or abs(ddec / dt * 60) > 5):
                if abs(dra) > abs(ddec):
                    print("-> Adjust azimuth slightly {}".format(
                          "east" if dra > 0 else "west"))
                else:
                    print("-> Adjust altitude slightly {}".format(
                          "up" if ddec > 0 else "down"))
            else:
                print("-> Alignment is good for solar work.")

    # ── Timing summary + CSV ─────────────────────────────────────────────────────
    if scan_records:
        measured = [r for r in scan_records if r["actual_fps"] > 0]
        print("")
        print("SCAN TIMING")
        if measured:
            fps_list = [r["actual_fps"] for r in measured]
            used = measured[0]["fps_used"]
            mean_fps = sum(fps_list) / len(fps_list)
            xy = [r["actual_xy"] for r in measured if r["actual_xy"] > 0]
            mean_xy = sum(xy) / len(xy) if xy else 0.0
            print("  used {:.2f} fps   measured {:.2f} fps (min {:.2f}, max {:.2f})".format(
                  used, mean_fps, min(fps_list), max(fps_list)))
            print("  resulting X/Y ~ {:.3f}  (1.000 = circular recon)".format(mean_xy))
            dev = abs(mean_fps - used) / used * 100.0 if used > 0 else 0.0
            if dev > 2.0:
                print("  WARNING: the capture rate differs {:.1f}% from the value used.".format(dev))
                print("  -> Fix the exposure and set CAPTURE_FPS (or enable FPS_PROBE_SECONDS).")
            decs = [r["dec_start"] for r in scan_records]
            if len(decs) > 1:
                spread = (max(decs) - min(decs)) * 3600.0
                print("  Dec spread across scans: {:.0f}\" ({:.0f} px at {:.3f}\"/px)".format(
                      spread, abs(spread) / PIXEL_SCALE, PIXEL_SCALE))
        else:
            print("  no frame counts available (dry run or capture disabled).")

        # Frame-loss and failed-scan report.
        short = [r for r in scan_records
                 if r.get("frames_short_pct", 0.0) > FRAMES_SHORT_TOL * 100.0]
        problems = [r for r in scan_records if r.get("problem")]
        if short:
            print("  FRAMES SHORT in {} scan(s):".format(len(short)))
            for r in short:
                print("    {}  {} of {} frames ({:.0f}% missing)".format(
                      r["label"], r["frames"], r["frames_expected"],
                      r["frames_short_pct"]))
        if problems:
            print("  SCANS WITH PROBLEMS: {}".format(
                  ", ".join("{}: {}".format(r["label"], r["problem"])
                            for r in problems)))
        write_scan_log()

    # Final session metadata (totals + configuration) for reproducibility.
    write_session_meta(extra={"session": {
        "total_seconds": round(total_time, 1),
        "scans": len(scan_records),
        "completed": session_ok,
    }}, verbose=False)

    print("=" * 60)


# ── HTML control panel (served by this script) ────────────────────────────────
# One script, one entry point: with START_UI = True it serves a compact panel
# on http://127.0.0.1:8765/ to manage config/profiles and start/stop the
# capture; with START_UI = False it runs the capture directly. Nothing is
# installed: it uses a plain Python socket and serves nightscribe-ui.html.
clr.AddReference("System")
from System.Threading import Thread, ThreadStart

HOST = "127.0.0.1"
PORT = 8765


def _folder():
    """Folder that holds the script, its config, the HTML and the profiles."""
    candidates = []
    try:
        candidates.append(os.environ.get("NIGHTSCRIBE_HOME"))
    except:
        pass
    try:
        candidates.append(os.path.dirname(os.path.abspath(__file__)))
    except:
        pass
    try:
        candidates.append(os.getcwd())
    except:
        pass
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    return "."


FOLDER = _folder()
CFG = os.path.join(FOLDER, "nightscribe-scan.cfg")
PROFILES = os.path.join(FOLDER, "nightscribe-profiles.json")
HTML = os.path.join(FOLDER, "nightscribe-ui.html")

state = {"running": False, "message": "idle", "started": ""}


def _read(path, default=""):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        try:
            return raw.decode("utf-8")
        except Exception:
            return raw.decode("latin-1")
    except:
        return default


def _write(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def _schema():
    """The CONFIG_SCHEMA fields with their current (live) values."""
    out = []
    for section, entries in CONFIG_SCHEMA:
        fields = []
        for name, help_text in entries:
            val = globals().get(name)
            if isinstance(val, bool):
                kind = "bool"
            elif isinstance(val, int):
                kind = "int"
            elif isinstance(val, float):
                kind = "float"
            else:
                kind = "str"
            fields.append({"name": name, "value": val, "help": help_text,
                           "type": kind,
                           "default": CONFIG_DEFAULTS.get(name, val)})
        out.append({"section": section, "fields": fields})
    return out


def _write_cfg(values):
    """Write nightscribe-scan.cfg with the schema's comments."""
    lines = ["# NightScribe configuration - written by the UI.", ""]
    for section, entries in CONFIG_SCHEMA:
        lines.append("[{}]".format(section))
        for name, help_text in entries:
            lines.append("# {}".format(help_text))
            val = values.get(name, globals().get(name))
            if isinstance(val, bool):
                val = "true" if val else "false"
            lines.append("{} = {}".format(name.lower(), val))
        lines.append("")
    _write(CFG, "\n".join(lines))


def _load_profiles():
    try:
        return json.loads(_read(PROFILES, '{"version": 1, "profiles": {}}'))
    except:
        return {"version": 1, "profiles": {}}


def _run_capture_thread():
    """Run a capture session on the server thread (used by Start)."""
    try:
        run_capture()
        state["message"] = "done"
    except Exception as e:
        state["message"] = "error: {}".format(e)
    finally:
        state["running"] = False


def _start(values=None):
    """Apply the panel's values and run a capture session.

    The values come straight from the form, so Start always uses what is on
    screen; saving to disk is a separate, explicit action.
    """
    if state["running"]:
        return False
    _apply_values(values)
    refresh_derived()
    _abort[0] = False
    state["running"] = True
    state["message"] = "running"
    state["started"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    t = Thread(ThreadStart(lambda: _run_capture_thread()))
    t.IsBackground = True
    t.Start()
    return True


def _read_request(conn):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("utf-8", "replace").split("\r\n")
    if not lines or not lines[0]:
        return None
    parts = lines[0].split(" ")
    method = parts[0] if parts else "GET"
    path = parts[1] if len(parts) > 1 else "/"
    length = 0
    for line in lines[1:]:
        if line.lower().startswith("content-length:"):
            try:
                length = int(line.split(":", 1)[1].strip())
            except:
                length = 0
    while len(body) < length:
        chunk = conn.recv(4096)
        if not chunk:
            break
        body += chunk
    return method, path, body[:length]


def _respond(conn, code, body, ctype="application/json"):
    if not isinstance(body, bytes):
        body = body.encode("utf-8")
    reason = {200: "OK", 404: "Not Found", 500: "Internal Server Error"}.get(
        code, "OK")
    header = ("HTTP/1.1 {} {}\r\nContent-Type: {}\r\nContent-Length: {}\r\n"
              "Connection: close\r\n\r\n").format(code, reason, ctype, len(body))
    conn.sendall(header.encode("utf-8") + body)


def _handle(conn):
    req = _read_request(conn)
    if req is None:
        _respond(conn, 404, json.dumps({"error": "bad request"}))
        return
    method, path, body = req
    if path in ("/", "/index.html"):
        _respond(conn, 200,
                 _read(HTML, "<h1>nightscribe-ui.html not found</h1>"),
                 "text/html; charset=utf-8")
    elif path == "/api/schema" or (path == "/api/config" and method == "GET"):
        _respond(conn, 200, json.dumps(_schema()))
    elif path == "/api/config" and method in ("PUT", "POST"):
        try:
            values = json.loads(body.decode("utf-8") or "{}")
        except:
            values = {}
        _write_cfg(values)
        _apply_values(values)
        _respond(conn, 200, json.dumps({"ok": True}))
    elif path == "/api/profiles" and method == "GET":
        _respond(conn, 200, json.dumps(_load_profiles()))
    elif path == "/api/profiles" and method in ("PUT", "POST"):
        _write(PROFILES, body.decode("utf-8") or "{}")
        _respond(conn, 200, json.dumps({"ok": True}))
    elif path == "/api/start" and method == "POST":
        try:
            values = json.loads(body.decode("utf-8") or "{}")
        except:
            values = {}
        _start(values)
        _respond(conn, 200, json.dumps({"ok": True, "running": state["running"]}))
    elif path == "/api/stop" and method == "POST":
        _abort[0] = True
        _respond(conn, 200, json.dumps({"ok": True}))
    elif path == "/api/status":
        _respond(conn, 200, json.dumps(state))
    else:
        _respond(conn, 404, json.dumps({"error": "not found"}))


def run_ui_server():
    """Serve the control panel until the console is closed."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(5)
    print("NightScribe control panel ready: http://{}:{}/".format(HOST, PORT))
    while True:
        conn, _addr = srv.accept()
        try:
            _handle(conn)
        except Exception as e:
            print("UI error: {}".format(e))
        finally:
            try:
                conn.close()
            except:
                pass


# ── Entry point ───────────────────────────────────────────────────────────────
# One script, one entry point. START_UI = True (default) serves the HTML
# control panel and drives the capture on demand; START_UI = False runs a
# capture immediately.
if START_UI:
    run_ui_server()
else:
    run_capture()
