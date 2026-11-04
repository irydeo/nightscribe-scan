# NightScribe UI — load this file in SharpCap's scripting console and Run.
#
# It loads nightscribe-scan.py (without running a capture), starts a tiny HTTP
# server on 127.0.0.1:8765 and serves the compact HTML control panel. Open
# http://127.0.0.1:8765/ in any browser to manage config/profiles and to
# start/stop the capture.
#
# Nothing is installed: it uses SharpCap's IronPython and .NET. The server is a
# plain Python socket (no URL ACL / admin needed).

import clr
import json
import os
import socket
import time

clr.AddReference("System")
from System.Threading import Thread, ThreadStart

HOST = "127.0.0.1"
PORT = 8765


# ── Paths ─────────────────────────────────────────────────────────────────────
def _folder():
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
SCAN = os.path.join(FOLDER, "nightscribe-scan.py")
HTML = os.path.join(FOLDER, "nightscribe-ui.html")
CFG = os.path.join(FOLDER, "nightscribe-scan.cfg")
PROFILES = os.path.join(FOLDER, "nightscribe-profiles.json")

state = {"running": False, "message": "idle", "started": ""}


# ── File helpers ──────────────────────────────────────────────────────────────
def _read(path, default=""):
    try:
        with open(path, "r") as fh:
            return fh.read()
    except:
        return default


def _write(path, text):
    with open(path, "w") as fh:
        fh.write(text)


# ── Config schema / file ──────────────────────────────────────────────────────
def _schema(ns):
    defaults = ns.get("CONFIG_DEFAULTS", {})
    # Values from the .cfg file (so a reload reflects what is on disk).
    filevals = {}
    try:
        parse = ns.get("_parse_ini")
        for (_sec, key), raw in parse(_read(CFG, "")).items():
            filevals[key.upper()] = raw
    except:
        pass
    out = []
    for section, entries in ns["CONFIG_SCHEMA"]:
        fields = []
        for name, help_text in entries:
            val = ns.get(name)
            raw = filevals.get(name.upper())
            if raw is not None:
                try:
                    if isinstance(val, bool):
                        val = raw.strip().lower() in ("1", "true", "yes", "on")
                    elif isinstance(val, int) and not isinstance(val, bool):
                        val = int(float(raw))
                    elif isinstance(val, float):
                        val = float(raw)
                    else:
                        val = raw
                except:
                    pass
            if isinstance(val, bool):
                kind = "bool"
            elif isinstance(val, int):
                kind = "int"
            elif isinstance(val, float):
                kind = "float"
            else:
                kind = "str"
            fields.append({"name": name, "value": val, "help": help_text,
                           "type": kind, "default": defaults.get(name, val)})
        out.append({"section": section, "fields": fields})
    return out


def _write_cfg(ns, values):
    """Write nightscribe-scan.cfg with the schema's comments."""
    lines = ["# NightScribe configuration — written by the UI.", ""]
    for section, entries in ns["CONFIG_SCHEMA"]:
        lines.append("[{}]".format(section))
        for name, help_text in entries:
            lines.append("# {}".format(help_text))
            val = values.get(name, ns.get(name))
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


# ── Capture control ───────────────────────────────────────────────────────────
def _run_capture(ns):
    try:
        state["running"] = True
        state["message"] = "running"
        state["started"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        ns["_abort"][0] = False
        ns["apply_config_file"](verbose=False)
        ns["refresh_derived"]()
        ns["run_capture"]()
        state["message"] = "done"
    except Exception as e:
        state["message"] = "error: {}".format(e)
    finally:
        state["running"] = False


def _start(ns):
    if state["running"]:
        return
    t = Thread(ThreadStart(lambda: _run_capture(ns)))
    t.IsBackground = True
    t.Start()


# ── Minimal HTTP server over a Python socket ──────────────────────────────────
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


def _handle(ns, conn):
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
        _respond(conn, 200, json.dumps(_schema(ns)))
    elif path == "/api/config" and method in ("PUT", "POST"):
        try:
            values = json.loads(body.decode("utf-8") or "{}")
        except:
            values = {}
        _write_cfg(ns, values)
        _respond(conn, 200, json.dumps({"ok": True}))
    elif path == "/api/profiles" and method == "GET":
        _respond(conn, 200, json.dumps(_load_profiles()))
    elif path == "/api/profiles" and method in ("PUT", "POST"):
        _write(PROFILES, body.decode("utf-8") or "{}")
        _respond(conn, 200, json.dumps({"ok": True}))
    elif path == "/api/start" and method == "POST":
        _start(ns)
        _respond(conn, 200, json.dumps({"ok": True, "running": state["running"]}))
    elif path == "/api/stop" and method == "POST":
        ns["_abort"][0] = True
        _respond(conn, 200, json.dumps({"ok": True}))
    elif path == "/api/status":
        _respond(conn, 200, json.dumps(state))
    else:
        _respond(conn, 404, json.dumps({"error": "not found"}))


def run_ui(ns):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(5)
    print("NightScribe UI ready: http://{}:{}/".format(HOST, PORT))
    while True:
        conn, _addr = srv.accept()
        try:
            _handle(ns, conn)
        except Exception as e:
            print("UI error: {}".format(e))
        finally:
            try:
                conn.close()
            except:
                pass


# ── Load the capture engine and start the server ──────────────────────────────
def main():
    ns = {"SharpCap": SharpCap, "NIGHTSCRIBE_UI": True, "__file__": SCAN}
    ns["_run_ui_server"] = lambda: run_ui(ns)
    src = _read(SCAN)
    if not src:
        raise RuntimeError("nightscribe-scan.py not found next to nightscribe-ui.py")
    exec(compile(src, SCAN, "exec"), ns)


main()
