"""Minimal SharpCap / IronPython stubs for the NightScribe tests.

The capture script only runs inside SharpCap's IronPython console, so the tests
cannot import it directly. Instead ``conftest.load_scan`` injects a fake
``SharpCap`` (plus the .NET modules the script touches) and executes the script
up to its pre-flight block. This module provides those fakes.

Only what the script reaches at import time and in the tested code paths is
implemented; the rest returns harmless defaults.
"""


class Stats(object):
    """Stands in for the .NET Tuple<float,float> from Frame.GetStats()."""
    def __init__(self, mean, stddev):
        self.Item1 = mean
        self.Item2 = stddev


class Frame(object):
    def __init__(self, mean=1000.0, stddev=50.0):
        self._mean = mean
        self._stddev = stddev

    def GetStats(self):
        return Stats(self._mean, self._stddev)

    def CutROI(self, rect):
        return Frame(self._mean, self._stddev)


class _Args(object):
    def __init__(self, frame):
        self.Frame = frame


class Event(object):
    """A .NET-style event: supports ``+=`` / ``-=`` and ``fire()``."""
    def __init__(self):
        self._handlers = []

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def __isub__(self, handler):
        try:
            self._handlers.remove(handler)
        except ValueError:
            pass
        return self

    def fire(self, frame):
        args = _Args(frame)
        for handler in list(self._handlers):
            handler(None, args)


class Control(object):
    def __init__(self, value):
        self.Value = value


class Controls(object):
    def __init__(self):
        self._by_name = {"Exposure": Control(10.0)}
        self.ColourSpace = Control("MONO16")

    def FindByName(self, name):
        return self._by_name[name]


class ROI(object):
    def __init__(self, width=3480, height=248):
        self.Width = width
        self.Height = height


class Camera(object):
    def __init__(self, fps=121.391052, roi=None):
        self.CurrentFrameRate = fps
        self.FrameCaptured = Event()
        self.Capturing = False
        self.CapturedFrameCount = 0
        self.ROI = roi or ROI()
        self.Controls = Controls()
        self.captures = 0

    def PrepareToCapture(self):
        pass

    def RunCapture(self):
        self.Capturing = True
        self.captures += 1

    def StopCapture(self):
        self.Capturing = False


class Mount(object):
    """Fake ASCOM mount.

    ``RA`` is in hours, ``Dec`` in degrees. ``MoveAxis(axis, rate)`` sets a
    sidereal-multiplier rate; call ``advance(seconds)`` to integrate it (the
    tests drive it from a fake frame watcher, mirroring how SharpCap pushes
    frames). ``sidereal`` is the RA-coordinate arcsec/s per multiplier unit, so
    a mount whose real rate is slow can be simulated by lowering it.
    """
    def __init__(self, ra=14.5, dec=-16.0, sidereal=15.04):
        self.RA = ra
        self.Dec = dec
        self.sidereal = sidereal
        self._ra_rate = 0.0
        self._dec_rate = 0.0

    def MoveAxis(self, axis, rate):
        if axis == 0:
            self._ra_rate = rate
        else:
            self._dec_rate = rate

    def Stop(self):
        self._ra_rate = 0.0
        self._dec_rate = 0.0

    def advance(self, seconds):
        # 1 hour of RA = 15 deg = 54000 arcsec.
        self.RA = (self.RA
                   + self._ra_rate * self.sidereal * seconds / 54000.0) % 24.0
        self.Dec = self.Dec + self._dec_rate * self.sidereal * seconds / 3600.0


class Mounts(object):
    def __init__(self, mount):
        self.SelectedMount = mount


class SharpCap(object):
    def __init__(self, camera=None, mount=None, capture_folder="/tmp"):
        self.SelectedCamera = camera if camera is not None else Camera()
        self.Mounts = Mounts(mount if mount is not None else Mount())
        self.TargetName = "Solar"
        self.CaptureFolder = capture_folder
