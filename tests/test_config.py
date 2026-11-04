"""Configuration loading, schema sections and panel value application."""

import os

import pytest

from conftest import FIXTURES, ROOT, SESSION_CFG, load_scan


def test_session_config_is_applied():
    ns = load_scan(SESSION_CFG)
    assert ns["FOCAL_LENGTH"] == 969        # Takahashi FC76DCU + 1.7Q extender
    assert ns["PIXEL_SIZE"] == 2
    assert ns["ENABLE_MOSAIC"] is True
    # padded_arcsec = 0 falls back to the seconds-based padding.
    assert ns["PADDED_ARCSEC"] == 0
    assert ns["padded_seconds"] == ns["PADDED_DURATION"]


def test_enable_mosaic_lives_in_the_mosaic_section():
    """Regression: enable_mosaic was once written under [scan] and ignored."""
    ns = load_scan(SESSION_CFG)
    where = {}
    for section, entries in ns["CONFIG_SCHEMA"]:
        for name, _help in entries:
            where[name] = section
    assert where["ENABLE_MOSAIC"] == "mosaic"
    assert where["FOCAL_LENGTH"] == "optics"
    assert where["PADDED_ARCSEC"] == "scan"


def test_session_config_has_no_unknown_keys():
    """Regression: enable_mosaic was once written under [scan] and ignored."""
    ns = load_scan(SESSION_CFG)
    known = set()
    for section, entries in ns["CONFIG_SCHEMA"]:
        for name, _help in entries:
            known.add((section, name.lower()))
    data = ns["_parse_ini"](open(SESSION_CFG, encoding="utf-8").read())
    unknown = [key for key in data if key not in known]
    assert not unknown, "unknown config keys: {}".format(unknown)


def test_apply_values_coerces_types():
    ns = load_scan(SESSION_CFG)
    ns["_apply_values"]({
        "FOCAL_LENGTH": 714,          # int
        "USE_EPHEMERIS": False,       # bool
        "EXP_PERCENTILE": 0.9,        # float
        "SCAN_LOG_PATH": "/tmp/x",    # str
    })
    assert ns["FOCAL_LENGTH"] == 714 and isinstance(ns["FOCAL_LENGTH"], int)
    assert ns["USE_EPHEMERIS"] is False
    assert ns["EXP_PERCENTILE"] == pytest.approx(0.9)
    assert ns["SCAN_LOG_PATH"] == "/tmp/x"


def test_shipped_config_example_uses_the_right_sections():
    """The minimal template must not put enable_mosaic under [scan]."""
    path = os.path.join(ROOT, "docs", "nightscribe-scan.cfg.example")
    data = load_scan(SESSION_CFG)["_parse_ini"](open(path).read())
    assert ("mosaic", "enable_mosaic") in data
    assert ("scan", "enable_mosaic") not in data


def test_missing_config_uses_defaults():
    ns = load_scan(None)   # no NIGHTSCRIBE_CONFIG
    # The repo ships nightscribe-scan.cfg next to the script, so it is loaded;
    # the point is only that loading succeeds and the derived values exist.
    assert ns["PIXEL_SCALE"] > 0
    assert ns["SOLAR_DIAMETER"] > 0
