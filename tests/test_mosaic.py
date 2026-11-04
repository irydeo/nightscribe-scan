"""Mosaic geometry: slit strip, chord at a row's Dec and the row plan."""

import inspect
import math

import pytest

from conftest import SESSION_CFG, load_scan


def test_expected_chord_geometry():
    ns = load_scan(SESSION_CFG)
    chord = ns["expected_chord_arcsec"]
    diameter = ns["SOLAR_DIAMETER"]
    strip = 1444.0

    # Centred slit -> full diameter.
    assert chord(0.0, strip, diameter) == pytest.approx(diameter)
    # Still inside the slit half-length -> diameter.
    assert chord(0.5 * strip, strip, diameter) == pytest.approx(diameter)
    # Outside the dead zone -> shorter than the diameter.
    off = 0.5 * strip + 0.5 * (diameter / 2.0)
    assert chord(off, strip, diameter) < diameter
    # Symmetric in the sign of the offset.
    assert chord(-off, strip, diameter) == pytest.approx(chord(off, strip, diameter))
    # Slit fully off the disk -> zero.
    assert chord(2.0 * diameter, strip, diameter) == 0.0


def test_expected_chord_matches_the_circle():
    ns = load_scan(SESSION_CFG)
    chord = ns["expected_chord_arcsec"]
    diameter = 1920.0
    strip = 100.0        # short slit: e = |q| - 50
    q = 500.0
    e = q - strip / 2.0
    assert chord(q, strip, diameter) == pytest.approx(
        2.0 * math.sqrt((diameter / 2.0) ** 2 - e * e))


def test_mosaic_plan_is_consistent():
    ns = load_scan(SESSION_CFG)
    plan = ns["compute_mosaic_plan"](ns["mount"].Dec)
    assert plan["rows"] >= 1
    assert plan["strip"] > 0
    assert plan["step"] == pytest.approx(plan["strip"] * (1.0 - ns["MOSAIC_OVERLAP"]))
    assert len(plan["positions"]) == plan["rows"]
    # Rows are centred on the requested Dec as a group.
    mean = sum(plan["positions"]) / len(plan["positions"])
    assert mean == pytest.approx(ns["mount"].Dec)


def test_effective_slit_arcsec_uses_the_roi():
    ns = load_scan(SESSION_CFG)
    strip = ns["effective_slit_arcsec"]()
    roi_w = ns["cam"].ROI.Width
    assert 0 < strip <= roi_w * ns["PIXEL_SCALE"]


def test_scan_once_accepts_a_chord_override():
    """Mosaic rows size the scan to their chord via crossing_arcsec."""
    ns = load_scan(SESSION_CFG)
    params = inspect.signature(ns["scan_once"]).parameters
    assert "crossing_arcsec" in params
