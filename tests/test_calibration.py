"""Regression tests for the scan-rate self-calibration.

The bug fixed here: ``calibrate_scan_rate`` measured the sky rate while slewing
at ``MOSAIC_CHORD_SPEED`` (16x) but then applied ``target / measured_rate``
directly to the science multiplier. That ignored the measurement speed and made
the scan ~4.6x too slow (``actual_xy`` ~4.1 instead of ~1, huge SER files and a
recon stretched in X). The session in ``fixtures/`` is the one that exposed it.
"""

import csv
import math
import os

import pytest

from conftest import FIXTURES, SESSION_CFG, load_scan


def _session():
    return load_scan(SESSION_CFG)


def test_factor_normalises_by_measure_speed():
    ns = _session()
    factor_fn = ns["scan_rate_calibration_factor"]
    per_unit = 13.35          # the mount's real arcsec/s per multiplier unit
    measure_speed = 16.0      # MOSAIC_CHORD_SPEED
    measured = measure_speed * per_unit
    target = 51.70
    mult = 3.437
    factor = factor_fn(measured, measure_speed, target, mult)
    assert factor == pytest.approx((target / per_unit) / mult, rel=1e-9)
    # Applying the factor makes the sky move exactly at the target rate.
    assert mult * factor * per_unit == pytest.approx(target, rel=1e-9)


def test_factor_none_for_invalid_input():
    ns = _session()
    factor_fn = ns["scan_rate_calibration_factor"]
    assert factor_fn(0.0, 16.0, 51.7, 3.4) is None
    assert factor_fn(200.0, 0.0, 51.7, 3.4) is None
    assert factor_fn(200.0, 16.0, 51.7, 0.0) is None


def test_old_formula_is_off_by_the_measurement_speed():
    """The old ``target/measured`` was ~4.6x too small for real numbers."""
    target, measured, measure_speed, sidereal = 51.70, 213.7, 16.0, 15.04
    mult = target / sidereal
    old = target / measured
    new = (target / (measured / measure_speed)) / mult
    assert old < 0.5                       # would have been rejected by the guard
    assert new > 1.0
    assert new / old == pytest.approx(measure_speed * sidereal / target, rel=0.01)


def test_guard_rejects_the_old_factor():
    ns = _session()
    target = ns["PIXEL_SCALE"] * ns["fps"]
    old_factor = target / 213.7            # the buggy value
    corrected = ns["scan_multiplier"] * old_factor * ns["SIDEREAL"]
    assert not (0.5 * target <= corrected <= 2.0 * target)


def test_session_log_shows_the_bug():
    """The fixture log is the one that exposed the bug (actual_xy ~4.1)."""
    path = os.path.join(FIXTURES, "scan_log.csv")
    with open(path) as fh:
        rows = list(csv.DictReader(fh))
    assert rows
    xy = [float(r["actual_xy"]) for r in rows if r["actual_xy"]]
    assert xy and max(xy) > 3.0
    factors = [float(r["calibration_factor"]) for r in rows if r["calibration_factor"]]
    assert factors and min(factors) < 0.5


def test_actual_xy_uses_the_calibrated_rate():
    """actual_xy must reflect the measured rate per unit, not nominal sidereal."""
    ns = _session()
    assert ns["sky_rate_per_unit"]() == ns["SIDEREAL"]
    ns["_calibration"][0] = {"applied": True, "measured_per_unit": 13.35}
    assert ns["sky_rate_per_unit"]() == pytest.approx(13.35)


def test_calibrate_scan_rate_targets_the_rate():
    """End-to-end: drive calibrate_scan_rate() with a simulated mount."""
    ns = _session()
    target = ns["PIXEL_SCALE"] * ns["fps"]
    mount = ns["mount"]
    mount.sidereal = 13.35                 # the mount's real rate per unit
    per_unit = mount.sidereal * math.cos(math.radians(mount.Dec))

    class FakeClock(object):
        """Deterministic clock; sleep() just advances it."""
        def __init__(self):
            self.t = 1000.0

        def time(self):
            return self.t

        def sleep(self, seconds):
            self.t += seconds

    clock = FakeClock()

    class FakeWatcher(object):
        """Feeds frames and advances the mount with the clock."""
        def __init__(self, camera):
            self.mount = ns["mount"]
            self._last = None
            self._n = 0
            self._latest = None

        @property
        def latest(self):
            now = clock.time()
            if self._last is not None:
                self.mount.advance(now - self._last)
            self._last = now
            self._n += 1
            if self._n < 6:
                self._latest = (1000.0, 50000.0)   # on disk
            else:
                self._latest = (1000.0, 50.0)      # off disk (trailing limb)
            return self._latest

        def start(self):
            pass

        def stop(self):
            pass

        def wait_first(self, timeout=3.0):
            return True

    ns["time"] = clock
    ns["find_solar_limb"] = lambda max_search=None: True
    ns["FrameWatcher"] = FakeWatcher

    factor = ns["calibrate_scan_rate"]()
    assert factor is not None
    assert ns["_calibration"][0]["applied"] is True
    # The corrected multiplier must make the sky move at the target rate, and
    # scan_speed (used for the durations) must be that real rate, not nominal.
    sky_rate = ns["scan_multiplier"] * per_unit
    assert sky_rate == pytest.approx(target, rel=0.03)
    assert ns["scan_speed"] == pytest.approx(target, rel=0.03)
