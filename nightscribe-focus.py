# NightScribe Focus — live focus aid for the SHG.
#
# A standalone utility, separate from the capture script: load this file in
# SharpCap's scripting console and Run. It attaches to the live camera, computes
# a few focus indicators on each frame and serves a small panel on
# http://127.0.0.1:8766/ with the live values and a max/min hold.
#
# It does not import or modify nightscribe-scan.py.
#
# The indicators follow the MLAstro SHG-700 focus routine:
#   1. Spectrograph (camera focus): the absorption lines must be as contrasted
#      and sharp as possible (H-alpha and the iron double line).
#   2. Collimator: the physical slit edge must be as sharp and straight as
#      possible. The camera and collimator focuses interact, so iterate.
#   3. Telescope: the surface detail must be as contrasted and "abundant" as
#      possible.
# Both contrast indicators are computed along both axes, so the panel does not
# depend on the camera orientation. The indicators are computed on the raw
# frame; use SharpCap's own histogram stretch to judge by eye.

import json
import socket
import time

import clr
clr.AddReference("System.Drawing")
from System.Drawing import Rectangle

HOST = "127.0.0.1"
PORT = 8766

# Grid of cell means (CutROI + GetStats). Finer = better edge resolution but
# more calls per frame.
GRID_X = 24
GRID_Y = 12
# Minimum seconds between two metric updates. The computation runs on the
# camera callback thread, so it must stay cheap.
REFRESH_S = 0.25
# A local maximum counts as a "structure" above this fraction of the profile's
# dynamic range.
PEAK_REL = 0.25


# ── Pure metric functions (no SharpCap; unit-tested with synthetic grids) ─────
def row_profile(grid):
    """Mean over X for each Y band -> profile along the dispersion axis."""
    nx = len(grid)
    ny = len(grid[0])
    return [sum(grid[i][j] for i in range(nx)) / float(nx) for j in range(ny)]


def col_profile(grid):
    """Mean over Y for each X band -> profile along the slit axis."""
    return [sum(row) / float(len(row)) for row in grid]


def contrast(profile):
    """Relative standard deviation (std/mean) of a profile."""
    n = len(profile)
    if n == 0:
        return 0.0
    m = sum(profile) / float(n)
    if m <= 0:
        return 0.0
    var = sum((v - m) ** 2 for v in profile) / float(n)
    return (var ** 0.5) / m


def gradient_energy(profile):
    """Mean squared step, normalised by the mean level (higher = sharper)."""
    n = len(profile)
    if n < 2:
        return 0.0
    m = sum(profile) / float(n)
    if m <= 0:
        return 0.0
    s = sum((profile[k + 1] - profile[k]) ** 2 for k in range(n - 1))
    return s / (m * m * (n - 1))


def count_peaks(profile, rel=PEAK_REL):
    """Number of local maxima above ``rel`` of the profile's dynamic range."""
    n = len(profile)
    if n < 3:
        return 0
    lo = min(profile)
    hi = max(profile)
    if hi <= lo:
        return 0
    thr = lo + rel * (hi - lo)
    c = 0
    for k in range(1, n - 1):
        if profile[k] >= profile[k - 1] and profile[k] > profile[k + 1] \
                and profile[k] > thr:
            c += 1
    return c


def _max_jump(seq):
    """(index, magnitude) of the largest absolute adjacent step."""
    best = 0.0
    idx = 0
    for k in range(len(seq) - 1):
        d = abs(seq[k + 1] - seq[k])
        if d > best:
            best = d
            idx = k
    return idx, best


def _std(values):
    n = len(values)
    if n == 0:
        return 0.0
    m = sum(values) / float(n)
    return (sum((v - m) ** 2 for v in values) / float(n)) ** 0.5


def detect_edge(grid):
    """Strongest edge in the grid: (sharp, flat, orientation).

    ``sharp`` is the strongest adjacent-cell jump normalised by the grid mean.
    ``flat`` is the standard deviation of the edge position along the
    perpendicular axis (0 = a perfectly straight edge). ``orientation`` is
    "vertical" (a step along X, e.g. the slit edge) or "horizontal".
    """
    nx = len(grid)
    ny = len(grid[0])
    m = sum(sum(row) for row in grid) / float(nx * ny)
    if m <= 0:
        return 0.0, 0.0, "vertical"
    best_v = 0.0
    pos_v = []
    for j in range(ny):
        col = [grid[i][j] for i in range(nx)]
        idx, mag = _max_jump(col)
        pos_v.append(idx)
        if mag > best_v:
            best_v = mag
    best_h = 0.0
    pos_h = []
    for i in range(nx):
        idx, mag = _max_jump(grid[i])
        pos_h.append(idx)
        if mag > best_h:
            best_h = mag
    if best_v >= best_h:
        return best_v / m, _std(pos_v), "vertical"
    return best_h / m, _std(pos_h), "horizontal"


def compute_metrics(grid):
    """All focus indicators from one grid of cell means."""
    rp = row_profile(grid)   # along Y -> absorption lines
    cp = col_profile(grid)   # along X -> surface detail
    sharp, flat, orient = detect_edge(grid)
    return {
        "line_contrast": contrast(rp),
        "line_gradient": gradient_energy(rp),
        "detail_contrast": contrast(cp),
        "detail_gradient": gradient_energy(cp),
        "detail_count": count_peaks(cp),
        "edge_sharp": sharp,
        "edge_flat": flat,
        "edge_orientation": orient,
    }


def frame_grid(frame, roi, nx=GRID_X, ny=GRID_Y):
    """Grid of cell means from one live frame (CutROI + GetStats)."""
    w = roi.Width
    h = roi.Height
    bw = max(1, w // nx)
    bh = max(1, h // ny)
    grid = []
    for i in range(nx):
        x = i * bw
        ww = bw if i < nx - 1 else (w - x)
        col = []
        for j in range(ny):
            y = j * bh
            hh = bh if j < ny - 1 else (h - y)
            col.append(frame.CutROI(Rectangle(x, y, ww, hh)).GetStats().Item1)
        grid.append(col)
    return grid


# ── Live handler, holds and HTTP panel ────────────────────────────────────────
state = {
    "enabled": True,
    "latest": None,
    "holds": {},
    "updated": 0.0,
}

HOLD_KEYS = ("line_contrast", "line_gradient", "detail_contrast",
             "detail_gradient", "detail_count", "edge_sharp", "edge_flat")


def _update_holds(metrics):
    holds = state["holds"]
    for key in HOLD_KEYS:
        value = metrics.get(key)
        if value is None:
            continue
        hi, lo = holds.get(key, (None, None))
        if hi is None or value > hi:
            hi = value
        if lo is None or value < lo:
            lo = value
        holds[key] = (hi, lo)


class FocusWatcher(object):
    """Attaches a FrameCaptured handler and refreshes the metrics."""

    def __init__(self, camera):
        self.camera = camera
        self.roi = camera.ROI
        self._active = False
        self._last = 0.0

    def start(self):
        self.camera.FrameCaptured += self._on_frame
        self._active = True

    def stop(self):
        if self._active:
            try:
                self.camera.FrameCaptured -= self._on_frame
            except:
                pass
            self._active = False

    def _on_frame(self, sender, args):
        if not self._active or not state["enabled"]:
            return
        now = time.time()
        if now - self._last < REFRESH_S:
            return
        self._last = now
        try:
            grid = frame_grid(args.Frame, self.roi)
        except:
            return
        metrics = compute_metrics(grid)
        state["latest"] = metrics
        state["updated"] = now
        _update_holds(metrics)


PANEL_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NightScribe Focus</title>
<style>
 body{margin:0;background:#12161c;color:#e8edf3;font:14px/1.4 system-ui,Segoe UI,Roboto,sans-serif;display:flex;justify-content:center;padding:16px}
 .app{width:100%;max-width:540px;background:#1b222c;border:1px solid #2a3441;border-radius:12px;overflow:hidden}
 header{display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid #2a3441}
 header h1{font-size:15px;margin:0;flex:1}
 .dot{width:9px;height:9px;border-radius:50%;background:#3fbf7f}
 .dot.off{background:#e0b341}
 main{padding:8px 14px 12px}
 h2{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#93a1b3;margin:12px 0 6px}
 .row{display:flex;justify-content:space-between;font-variant-numeric:tabular-nums;padding:3px 0}
 .k{color:#93a1b3}
 .v{font-weight:600}
 .hold{color:#6f7d8f;font-size:12px;margin-left:8px}
 footer{display:flex;gap:8px;padding:10px 14px;border-top:1px solid #2a3441;align-items:center}
 button{background:#222b36;color:#e8edf3;border:1px solid #2a3441;border-radius:8px;padding:6px 10px;cursor:pointer}
 .note{font-size:11px;color:#93a1b3;padding:0 14px 12px}
</style></head><body>
<div class="app">
 <header><span id="dot" class="dot"></span><h1>NightScribe Focus</h1><span id="age" class="k"></span></header>
 <main>
  <h2>Spectrograph - absorption lines</h2>
  <div class="row"><span class="k">contrast</span><span><span class="v" id="line_contrast">-</span><span class="hold" id="h_line_contrast"></span></span></div>
  <div class="row"><span class="k">gradient</span><span><span class="v" id="line_gradient">-</span><span class="hold" id="h_line_gradient"></span></span></div>
  <h2>Collimator - slit edge</h2>
  <div class="row"><span class="k">sharpness</span><span><span class="v" id="edge_sharp">-</span><span class="hold" id="h_edge_sharp"></span></span></div>
  <div class="row"><span class="k">flatness (lower is better)</span><span><span class="v" id="edge_flat">-</span><span class="hold" id="h_edge_flat"></span></span></div>
  <h2>Telescope - surface detail</h2>
  <div class="row"><span class="k">contrast</span><span><span class="v" id="detail_contrast">-</span><span class="hold" id="h_detail_contrast"></span></span></div>
  <div class="row"><span class="k">gradient</span><span><span class="v" id="detail_gradient">-</span><span class="hold" id="h_detail_gradient"></span></span></div>
  <div class="row"><span class="k">structures</span><span><span class="v" id="detail_count">-</span><span class="hold" id="h_detail_count"></span></span></div>
 </main>
 <footer>
  <button onclick="reset()">Reset max/min</button>
  <button id="toggle" onclick="toggle()">Pause</button>
  <span id="status" class="k"></span>
 </footer>
 <div class="note">Set the exposure before focusing: the indicators depend on brightness. Maximise contrast, sharpness and structures; minimise flatness. Use SharpCap's live-view stretch to judge by eye.</div>
</div>
<script>
const KEYS = ["line_contrast","line_gradient","edge_sharp","edge_flat","detail_contrast","detail_gradient","detail_count"];
function fmt(v){ return (v===null||v===undefined)?"-":(Math.abs(v)>=100?v.toFixed(0):v.toFixed(4)); }
async function refresh(){
  try{
    const r = await fetch("/api/focus"); const s = await r.json();
    document.getElementById("dot").className = "dot" + (s.enabled?"":" off");
    document.getElementById("age").textContent = (s.age===null||s.age===undefined)?"":"updated "+s.age.toFixed(1)+"s ago";
    document.getElementById("status").textContent = s.enabled?"measuring":"paused";
    const m = s.metrics||{}, h = s.holds||{};
    KEYS.forEach(k=>{
      document.getElementById(k).textContent = fmt(m[k]);
      const hold = h[k];
      document.getElementById("h_"+k).textContent = hold?("max "+fmt(hold[0])+" / min "+fmt(hold[1])):"";
    });
  }catch(e){ document.getElementById("status").textContent = "no server"; }
}
async function reset(){ await fetch("/api/reset",{method:"POST"}); refresh(); }
async function toggle(){ await fetch("/api/toggle",{method:"POST"}); refresh(); }
refresh(); setInterval(refresh, 300);
</script></body></html>"""


def _read_request(conn):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, _body = data.partition(b"\r\n\r\n")
    lines = head.decode("utf-8", "replace").split("\r\n")
    if not lines or not lines[0]:
        return None
    parts = lines[0].split(" ")
    method = parts[0] if parts else "GET"
    path = parts[1] if len(parts) > 1 else "/"
    return method, path


def _respond(conn, code, body, ctype="application/json"):
    if not isinstance(body, bytes):
        body = body.encode("utf-8")
    reason = {200: "OK", 404: "Not Found"}.get(code, "OK")
    header = ("HTTP/1.1 {} {}\r\nContent-Type: {}\r\nContent-Length: {}\r\n"
              "Connection: close\r\n\r\n").format(code, reason, ctype, len(body))
    conn.sendall(header.encode("utf-8") + body)


def _payload():
    age = (time.time() - state["updated"]) if state["updated"] else None
    return json.dumps({
        "enabled": state["enabled"],
        "metrics": state["latest"],
        "holds": state["holds"],
        "age": age,
    })


def _handle(conn):
    req = _read_request(conn)
    if req is None:
        _respond(conn, 404, json.dumps({"error": "bad request"}))
        return
    method, path = req
    if path in ("/", "/index.html"):
        _respond(conn, 200, PANEL_HTML, "text/html; charset=utf-8")
    elif path == "/api/focus":
        _respond(conn, 200, _payload())
    elif path == "/api/reset" and method in ("POST", "PUT"):
        state["holds"] = {}
        _respond(conn, 200, json.dumps({"ok": True}))
    elif path == "/api/toggle" and method in ("POST", "PUT"):
        state["enabled"] = not state["enabled"]
        _respond(conn, 200, json.dumps({"ok": True, "enabled": state["enabled"]}))
    else:
        _respond(conn, 404, json.dumps({"error": "not found"}))


def run_server():
    """Serve the focus panel until the console is closed."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(5)
    print("NightScribe focus panel: http://{}:{}/".format(HOST, PORT))
    while True:
        conn, _addr = srv.accept()
        try:
            _handle(conn)
        except Exception as e:
            print("Focus UI error: {}".format(e))
        finally:
            try:
                conn.close()
            except:
                pass


# ── Focus entry point ─────────────────────────────────────────────────────────
def main():
    camera = SharpCap.SelectedCamera
    if camera is None:
        raise RuntimeError(
            "No camera selected in SharpCap. Connect/select a camera and start "
            "Live View before running this script.")
    fps = camera.CurrentFrameRate
    if fps is None or fps != fps or fps <= 0:
        raise RuntimeError(
            "The camera is not delivering frames (CurrentFrameRate={}). Start "
            "Live View and wait a few seconds.".format(fps))
    watcher = FocusWatcher(camera)
    watcher.start()
    try:
        run_server()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        watcher.stop()


main()
