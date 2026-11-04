"""Test harness: run nightscribe-scan.py outside SharpCap.

``load_scan`` installs fake ``clr`` / ``System.Drawing`` / ``SharpCap`` objects
and executes the script up to its pre-flight block (just before the banner and
the entry point), so the tests get every function and global without starting
the capture, the HTTP server or writing session files.

Fixtures live in ``tests/fixtures`` and come from a real session:
``session.cfg`` and ``session-profiles.json`` (the user's profile) and
``scan_log.csv`` (the run that exposed the calibration bug).
"""

import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCAN = os.path.join(ROOT, "nightscribe-scan.py")
FIXTURES = os.path.join(HERE, "fixtures")
SESSION_CFG = os.path.join(FIXTURES, "session.cfg")

if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _install_modules():
    """Register the .NET modules the script imports at module level."""
    clr = types.ModuleType("clr")
    clr.AddReference = lambda name: None
    sys.modules["clr"] = clr

    drawing = types.ModuleType("System.Drawing")

    class Rectangle(object):
        def __init__(self, *args):
            pass

    drawing.Rectangle = Rectangle
    sys.modules["System.Drawing"] = drawing

    system = types.ModuleType("System")
    sys.modules["System"] = system

    threading = types.ModuleType("System.Threading")

    class ThreadStart(object):
        def __init__(self, fn):
            self.fn = fn

        def __call__(self):
            return self.fn()

    class Thread(object):
        def __init__(self, start=None):
            self._start = start

        def Start(self):
            if self._start is not None:
                self._start()

    threading.Thread = Thread
    threading.ThreadStart = ThreadStart
    sys.modules["System.Threading"] = threading


def load_scan(config_path=None, camera=None, mount=None):
    """Execute the script with SharpCap stubbed and return its namespace."""
    import sharpcap_stub

    _install_modules()
    if config_path:
        os.environ["NIGHTSCRIBE_CONFIG"] = config_path
    else:
        os.environ.pop("NIGHTSCRIBE_CONFIG", None)

    sharpcap = sharpcap_stub.SharpCap(
        camera=camera if camera is not None else sharpcap_stub.Camera(),
        mount=mount if mount is not None else sharpcap_stub.Mount())

    src = open(SCAN).read()
    # Cut just before the pre-flight banner: everything is defined, but the
    # banner, the session-meta write and the entry point are not run.
    cut = src.rfind("\n", 0, src.index("Pre-flight info"))
    ns = {"SharpCap": sharpcap, "__name__": "nightscribe_scan", "__file__": SCAN}
    exec(compile(src[:cut], SCAN, "exec"), ns)
    return ns


@pytest.fixture
def scan():
    """The script namespace loaded with the real session configuration."""
    return load_scan(SESSION_CFG)
