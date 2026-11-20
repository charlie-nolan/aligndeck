"""
aligndeck: camera wheel alignment.   python -m aligndeck [--make Subaru --model Forester --year 2002]

Run it on the machine the camera is plugged into (a laptop, a desktop, or an Orange Pi / RK3588 board). It prints
the address to open: on that machine (http://localhost:8765, or --open) or from another computer over Tailscale
(http://<hostname>:8765). It only listens on localhost and the Tailscale address, never on the local wifi.
It never starts by itself.
"""
import argparse
import atexit
import os
import shutil
import signal
import subprocess
import sys
import time
import webbrowser

from . import align_vision as V
from .focus import Focus
from .liveview import LiveView
from .server import App, serve
from .session import DATA, Session
from . import vlm as VLM


def tailscale_ip():
    if not shutil.which("tailscale"):
        return None
    try:
        r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
        ip = r.stdout.split()[0] if r.stdout.split() else None
        return ip
    except (OSError, subprocess.SubprocessError):
        return None


def main():
    ap = argparse.ArgumentParser(description="camera wheel alignment (web screen)")
    ap.add_argument("--make", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--year", default="")
    ap.add_argument("--kb", default="", help="optional spec database file (Open Diagnostic Cyberdeck make DB layout)")
    ap.add_argument("--kb-dir", default="", help="optional folder of <make>.db spec databases")
    ap.add_argument("--ollama", default=VLM.OLLAMA, help="Ollama URL for the photo helper (default %(default)s)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", action="append", help="address to listen on (default: localhost + Tailscale)")
    ap.add_argument("--camera", choices=("gphoto2", "webcam", "files"), default="gphoto2")
    ap.add_argument("--open", action="store_true", help="open the screen in this machine's browser")
    ap.add_argument("--resume", action="store_true", help="only resume programs a crashed run left paused, then exit")
    ap.add_argument("--data", default=DATA, help="state, photos, calibration and models (default %(default)s)")
    a = ap.parse_args()

    os.makedirs(a.data, exist_ok=True)
    focus = Focus(a.data)
    if a.resume:
        print("\n".join(focus.notes) or "nothing was left paused")
        return
    if V.HAVE_CV:
        import cv2
        cv2.setNumThreads(os.cpu_count() or 4)
    session = Session(a.make, a.model, a.year, a.kb, a.data, a.kb_dir)
    session.source = a.camera
    session.load_calibration()
    for n in focus.notes:
        session.say(n, "amber")
    live = LiveView()
    app = App(session, live, focus, VLM.Vlm(os.path.join(a.data, "models"), a.ollama))

    def shutdown(*_):
        n = focus.resume()
        live.stop()
        if n:
            print(f"resumed {n} paused program(s)")
        sys.exit(0)
    atexit.register(lambda: (focus.resume(), live.stop()))
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGHUP, shutdown)

    hosts = a.host or ["127.0.0.1"] + ([tailscale_ip()] if tailscale_ip() else [])
    if not serve(app, hosts, a.port):
        raise SystemExit("could not open the screen's port")
    print("WHEEL ALIGNMENT is running. Open:")
    for h in hosts:
        print(f"  http://{h}:{a.port}")
    if len(hosts) > 1:
        print(f"  (desktop: http://{os.uname().nodename}:{a.port} over Tailscale)")
    print("Ctrl+C here quits and resumes anything paused for full compute.")
    if a.make and a.model and a.year:
        app.run("loading specs", session.load_specs)
    if a.open:
        webbrowser.open(f"http://127.0.0.1:{a.port}")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
