"""Static checks: the script must stay runnable inside SharpCap's IronPython."""

import ast
import os

from conftest import ROOT, SCAN


def test_parses_as_python_3_4():
    """SharpCap 4.1 embeds IronPython 3.4, so the grammar must be <= 3.4."""
    src = open(SCAN).read()
    ast.parse(src, filename=SCAN, feature_version=(3, 4))


def test_has_a_single_entry_point():
    src = open(SCAN).read()
    assert "if START_UI:" in src
    assert src.count("if START_UI:") == 1
    # The old two-file layout is gone.
    assert not os.path.exists(os.path.join(ROOT, "nightscribe-ui.py"))


def test_no_fstrings():
    """f-strings are Python 3.6+; IronPython 3.4 does not have them."""
    tree = ast.parse(open(SCAN).read(), filename=SCAN)
    assert not any(isinstance(n, ast.JoinedStr) for n in ast.walk(tree))


def test_config_schema_matches_the_reference_file():
    """Every schema field must appear in docs/nightscribe-scan.cfg.reference."""
    from conftest import load_scan
    ns = load_scan(os.path.join(ROOT, "nightscribe-scan.cfg"))
    ref = open(os.path.join(ROOT, "docs", "nightscribe-scan.cfg.reference")).read()
    for section, entries in ns["CONFIG_SCHEMA"]:
        for name, _help in entries:
            assert name.lower() in ref, "{} missing from the reference".format(name)
