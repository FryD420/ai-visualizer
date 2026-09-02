#!/usr/bin/env python3
# ai-visualizer: give your AI agent a face.
# Copyright (C) 2026 Jared Rhodenizer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ai-visualizer server. Python standard library only, nothing to install.

Serves the face gallery at http://127.0.0.1:8790/ and exposes:

  /state   polled by the faces (~8x/sec):
           {"state":  "idle|listening|thinking|speaking",
            "level":  0.0-1.0,       voice loudness while speaking
            "samples": [64 floats],  raw waveform snapshot (0s when quiet)
            "alert":  bool,          optional attention signal
            "loading": bool,         true while the voice line plays its
                                     own thinking sound (we stay quiet)
            "alive":  bool,          the voice line's heartbeat is fresh
                                     (< 6 s old); false = dead or hung
            "activity": null | {"line": "Read: foo.py",  what the agent is
                                "age": 1.2,              doing; s since the
                                "turn_age": 14.8}}       line / turn began
  /events  Server-Sent Events push of the same payload as /state, sent the
           instant it changes instead of waiting on the next poll tick.
           This is what core.js prefers; /state keeps working unchanged
           underneath it as the fallback for older clients.
  /config  the merged ai-visualizer.json plus the list of installed
           faces, discovered by scanning the faces/ folder. Drop a new
           folder with an index.html into faces/ and it appears in the
           gallery. That is the whole plugin system.

READ-ONLY on the signal bus. The bus is a handful of tiny files written
by a voice line (backtalk writes them natively, github.com/jaredrhod/backtalk):

  .voice_state        idle | listening | thinking | speaking
  .voice_waveform     JSON {ts, samples: [64 floats]} while audio plays
  .voice_loading_pid  exists while the voice line plays a thinking sound
  .voice_heartbeat    unix time as text, rewritten every ~2 s while the
                      voice line is alive (missing/stale = dead or hung)
  .voice_activity     JSON {ts, turn_started, line} during a turn: what
                      the agent is doing right now; gone when it ends
  .voice_alert        optional: non-empty file = attention needed

Where the bus lives comes from "bus_dir" in ai-visualizer.json (default:
this folder). Point it at your backtalk folder, or point backtalk's
"signals_dir" here. Either direction works.

Run:
  python3 server.py             the real bus
  python3 server.py --mock speaking
                                no voice line needed: /state synthesizes
                                the chosen state (idle|listening|thinking
                                |speaking) so you can see a face perform
  python3 server.py --no-open   do not auto-open the browser
                                (or set "open_browser": false in
                                ai-visualizer.json to make it permanent)
Ctrl-C stops.
"""
import json
import math
import mimetypes
import queue
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATES = {"idle", "listening", "thinking", "speaking"}
WAVEFORM_STALE_S = 0.6
HEARTBEAT_STALE_S = 6.0      # the voice line beats every ~2 s
MOCK_ACTIVITY = ["Bash: running the build",
                 "Read: src/main/java/Tablet.java",
                 "Grep: mod_version in gradle.properties"]

DEFAULTS = {
    "name": "JARVIS",       # shown on the chip / headers, yours to change
    "badge": "",            # optional handle shown in some faces' chrome
    "face": "board",        # the default face the root URL opens
    "port": 8790,
    "bus_dir": "",          # where the .voice_* files live ("" = here)
    "thinking_sound": True, # play assets/thinking.wav while thinking
    "open_browser": True,   # auto-open a browser on startup; set false when
                            # something else already embeds the face (a
                            # desktop shell, an iframe panel) and a second
                            # window is just clutter
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        user = json.loads((HERE / "ai-visualizer.json").read_text())
        for k, v in user.items():
            cfg[k] = v
    except FileNotFoundError:
        pass
    except ValueError as e:
        print(f"[config] ai-visualizer.json is not valid JSON ({e}), "
              f"using defaults")
    return cfg


CFG = load_config()
BUS = Path(CFG["bus_dir"]).expanduser() if CFG.get("bus_dir") else HERE

MOCK = None
# Either switch suppresses it: the flag is the one-off, the config key is
# the standing preference. Never AND them — a config that says "don't open"
# must not be overridden by the absence of a flag.
NO_OPEN = "--no-open" in sys.argv or not CFG.get("open_browser", True)
if "--mock" in sys.argv:
    i = sys.argv.index("--mock")
    MOCK = sys.argv[i + 1] if len(sys.argv) > i + 1 else "speaking"
    if MOCK not in STATES:
        MOCK = "speaking"
PORT = int(CFG.get("port", 8790))
if "--port" in sys.argv:
    i = sys.argv.index("--port")
    PORT = int(sys.argv[i + 1])


def list_faces():
    faces = []
    fdir = HERE / "faces"
    if fdir.is_dir():
        for p in sorted(fdir.iterdir()):
            if p.is_dir() and (p / "index.html").exists():
                meta = {"id": p.name, "title": p.name.title(), "tagline": ""}
                try:
                    meta.update(json.loads((p / "face.json").read_text()))
                except (OSError, ValueError):
                    pass
                meta["id"] = p.name
                faces.append(meta)
    return faces


def mock_bus():
    t = time.time()
    level = 0.0
    samples = [0.0] * 64
    if MOCK == "speaking":
        level = abs(math.sin(t * 6.0)) * 0.85
        samples = [
            (math.sin(i * 0.55 + t * 9.0) * 0.6
             + math.sin(i * 1.7 - t * 13.0) * 0.4)
            * 9000.0 * (0.35 + 0.65 * abs(math.sin(t * 2.6)))
            for i in range(64)
        ]
    activity = None
    if MOCK in ("thinking", "speaking"):
        # a fake turn in flight: the line cycles every 4 s, the turn
        # clock wraps every 90 s, so every face's ticker has something
        activity = {"line": MOCK_ACTIVITY[int(t / 4) % len(MOCK_ACTIVITY)],
                    "age": round(t % 4, 2), "turn_age": round(t % 90, 2)}
    return {"state": MOCK, "level": level, "samples": samples,
            "alert": False, "loading": MOCK == "thinking",
            "alive": True, "activity": activity}


_last_activity = (None, 0.0)    # last good parse, for mid-write blips


def read_activity():
    """The .voice_activity file as {"line", "age", "turn_age"}, or None
    when absent. A torn read (the writer mid-rewrite) reuses the last
    good parse for up to a second instead of blinking the face."""
    global _last_activity
    now = time.time()
    try:
        text = (BUS / ".voice_activity").read_text()
    except OSError:
        _last_activity = (None, 0.0)
        return None
    try:
        a = json.loads(text)
        ts = float(a.get("ts") or now)
        started = float(a.get("turn_started") or ts)
        out = {"line": str(a.get("line") or ""),
               "age": round(max(0.0, now - ts), 2),
               "turn_age": round(max(0.0, now - started), 2)}
        _last_activity = (out, now)
        return out
    except (ValueError, TypeError, AttributeError):
        last, at = _last_activity
        return last if last and now - at < 1.0 else None


def read_alive():
    """True while the voice line's heartbeat is fresh."""
    try:
        beat = float((BUS / ".voice_heartbeat").read_text().strip())
        return (time.time() - beat) < HEARTBEAT_STALE_S
    except (OSError, ValueError, TypeError):
        return False


def read_bus():
    if MOCK:
        return mock_bus()
    try:
        state = (BUS / ".voice_state").read_text().strip().lower()
        if state not in STATES:
            state = "idle"
    except OSError:
        state = "idle"
    level = 0.0
    samples = [0.0] * 64
    try:
        payload = json.loads((BUS / ".voice_waveform").read_text())
        age = time.time() - float(payload.get("ts", 0))
        raw = payload.get("samples") or []
        if raw and age < WAVEFORM_STALE_S:
            # A fresh waveform IS speech, whatever the state file says.
            state = "speaking"
            samples = [float(s) for s in raw[:64]]
            mean = sum(abs(s) for s in samples) / len(samples)
            level = min(1.0, mean / 3000.0)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        alert = (BUS / ".voice_alert").stat().st_size > 0
    except OSError:
        alert = False
    loading = (BUS / ".voice_loading_pid").exists()
    return {"state": state, "level": level, "samples": samples,
            "alert": alert, "loading": loading,
            "alive": read_alive(), "activity": read_activity()}


BROADCAST_PERIOD_S = 0.03   # ~33/s sampling: fast enough that push beats
                            # the old 120ms poll on both worst case and
                            # average, cheap enough to run forever
SSE_HEARTBEAT_S = 15.0      # keeps idle connections alive through proxies
                            # / browsers that time out a quiet socket
SSE_WRITE_TIMEOUT_S = 20.0  # bounds how long a wedged client (TCP window
                            # full, never reading) can hold its thread

_subscribers = []           # list[queue.Queue] of connected /events clients
_subscribers_lock = threading.Lock()


def _broadcast_bus():
    """Background thread: samples read_bus() fast and fans out a frame to
    every connected /events client, but ONLY when the payload changed —
    an idle face produces zero SSE traffic, which is half the point of
    replacing the poll. Runs for the life of the process; daemonized so
    it never blocks shutdown."""
    last = None
    while True:
        try:
            encoded = json.dumps(read_bus())
            if encoded != last:
                last = encoded
                with _subscribers_lock:
                    subs = list(_subscribers)
                for q in subs:
                    try:
                        q.put_nowait(encoded)
                    except queue.Full:
                        pass  # a slow/wedged client falls behind rather
                              # than stalling the broadcaster for everyone
        except Exception:
            pass  # never let one bad sample kill the only broadcaster
        time.sleep(BROADCAST_PERIOD_S)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/state":
                self._send(json.dumps(read_bus()).encode(),
                           "application/json")
            elif path == "/events":
                self._events()
            elif path == "/config":
                out = {"name": CFG["name"], "badge": CFG["badge"],
                       "face": CFG["face"],
                       "thinking_sound": bool(CFG["thinking_sound"]),
                       "faces": list_faces()}
                self._send(json.dumps(out).encode(), "application/json")
            else:
                self._static(path)
        except BrokenPipeError:
            pass
        except Exception as e:
            body = json.dumps({"error": str(e)}).encode()
            self._send(body, "application/json", 500)

    def _static(self, path):
        if path == "/":
            path = "/index.html"
        target = (HERE / path.lstrip("/")).resolve()
        if target != HERE and HERE not in target.parents:
            self._send(b"not found", "text/plain", 404)
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._send(b"not found", "text/plain", 404)
            return
        ctype = mimetypes.guess_type(str(target))[0] or \
            "application/octet-stream"
        self._send(target.read_bytes(), ctype)

    def _events(self):
        """Long-lived SSE stream: one dedicated thread per connected client
        (ThreadingHTTPServer gives us that for free), parked in a queue.get
        loop so it does no work between frames. Handles its own exceptions
        end to end — a dropped client must not fall through to do_GET's
        error path and try to send a second HTTP response on a socket
        that's already answered once."""
        q = queue.Queue(maxsize=10)
        with _subscribers_lock:
            _subscribers.append(q)
        try:
            # bounds a wedged client (connected but never draining its
            # socket buffer) to one stuck write instead of a thread held
            # forever
            self.connection.settimeout(SSE_WRITE_TIMEOUT_S)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            # prime the new client immediately so it isn't blank until the
            # next state change — same first frame /state would give it
            self.wfile.write(f"data: {json.dumps(read_bus())}\n\n".encode())
            self.wfile.flush()
            last_sent = time.time()
            while True:
                try:
                    encoded = q.get(timeout=1.0)
                    self.wfile.write(f"data: {encoded}\n\n".encode())
                    self.wfile.flush()
                    last_sent = time.time()
                except queue.Empty:
                    if time.time() - last_sent >= SSE_HEARTBEAT_S:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_sent = time.time()
        except (BrokenPipeError, ConnectionResetError, socket.timeout,
                OSError):
            pass  # client gone or wedged: quietly stop, no console spam
        finally:
            with _subscribers_lock:
                try:
                    _subscribers.remove(q)
                except ValueError:
                    pass

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    mode = f"MOCK={MOCK}" if MOCK else f"bus: {BUS}"
    root = f"http://127.0.0.1:{PORT}/"
    # The browser opens on the configured face; the gallery stays at "/" for switching.
    face = CFG.get("face", "")
    url = f"{root}faces/{face}/" if face and (HERE / "faces" / face / "index.html").exists() else root
    print(f"ai-visualizer on {root}  opening {url}  ({mode})  Ctrl-C stops", flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.allow_reuse_address = True
    threading.Thread(target=_broadcast_bus, daemon=True).start()
    if not NO_OPEN:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
