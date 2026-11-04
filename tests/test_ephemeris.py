"""Low-precision ephemeris: solar diameter from the date."""

import math

import pytest

from conftest import SESSION_CFG, load_scan


def test_julian_date_reference_epoch():
    ns = load_scan(SESSION_CFG)
    jd = ns["_julian_date"](2000, 1, 1, 12, 0, 0)
    assert jd == pytest.approx(2451545.0, abs=1e-6)


def test_ephemeris_diameter_is_physical():
    ns = load_scan(SESSION_CFG)
    d = ns["ephemeris_solar_diameter"]()
    # Apparent diameter runs ~1880"-1960" over the year.
    assert 1880.0 < d < 1960.0


def test_ephemeris_matches_semidiameter_formula():
    ns = load_scan(SESSION_CFG)
    c = ns["_utc_components"]()
    assert c is not None
    jd = ns["_julian_date"](*c)
    g = math.radians((357.5291 + 0.98560028 * (jd - 2451545.0)) % 360.0)
    r = 1.00014 - 0.01671 * math.cos(g) - 0.00014 * math.cos(2.0 * g)
    assert ns["ephemeris_solar_diameter"]() == pytest.approx(2.0 * 959.63 / r)
