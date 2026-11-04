#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regenerate the schema block embedded in nightscribe-ui.html.

The control panel works in two modes:
  - served by nightscribe-scan.py (schema comes from the live server);
  - opened directly as a file, where it needs the schema embedded.

This script reads CONFIG_SCHEMA (and the built-in defaults) from
nightscribe-scan.py and rewrites the JSON between the markers
``/* SCHEMA_START */`` and ``/* SCHEMA_END */`` in nightscribe-ui.html, so the
embedded copy never drifts from the source of truth.

Run it whenever the schema changes:

    python3 tools/build_ui.py

It only needs the standard library; it stubs the SharpCap-specific imports.
"""

import json
import os
import re
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCAN = os.path.join(ROOT, "nightscribe-scan.py")
HTML = os.path.join(ROOT, "nightscribe-ui.html")
CUT = "# ══════════════════════════════════════════════════════════════════════════════\n# DERIVED VALUES"


def _stub_sharpcap():
    clr = types.ModuleType("clr")
    clr.AddReference = lambda name: None
    sys.modules["clr"] = clr
    drawing = types.ModuleType("System.Drawing")

    class Rectangle(object):
        def __init__(self, *a):
            pass

    drawing.Rectangle = Rectangle
    sys.modules["System.Drawing"] = drawing
    sys.modules["System"] = types.ModuleType("System")


def load_schema():
    """Exec nightscribe-scan.py up to the derived-values block and read it."""
    _stub_sharpcap()
    src = open(SCAN, "r").read()
    idx = src.index(CUT)
    ns = {"__name__": "nightscribe_schema", "__file__": SCAN}
    exec(compile(src[:idx], SCAN, "exec"), ns)
    schema = ns["CONFIG_SCHEMA"]
    defaults = ns["CONFIG_DEFAULTS"]
    out = []
    for section, entries in schema:
        fields = []
        for name, help_text in entries:
            val = defaults.get(name)
            if isinstance(val, bool):
                kind = "bool"
            elif isinstance(val, int):
                kind = "int"
            elif isinstance(val, float):
                kind = "float"
            else:
                kind = "str"
            fields.append({"name": name, "type": kind, "default": val,
                           "help": help_text})
        out.append({"section": section, "fields": fields})
    return out


def main():
    schema = load_schema()
    text = open(HTML, "r").read()
    block = "/* SCHEMA_START */\nconst EMBEDDED_SCHEMA = " + \
            json.dumps(schema, indent=2) + ";\n/* SCHEMA_END */"
    new = re.sub(r"/\* SCHEMA_START \*/.*?/\* SCHEMA_END \*/", block, text,
                 flags=re.S)
    if new == text:
        raise SystemExit("markers /* SCHEMA_START */ ... /* SCHEMA_END */ "
                         "not found in " + HTML)
    open(HTML, "w").write(new)
    n = sum(len(s["fields"]) for s in schema)
    print("Embedded {} fields from {} sections into {}".format(n, len(schema), HTML))


if __name__ == "__main__":
    main()
