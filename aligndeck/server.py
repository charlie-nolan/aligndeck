"""
The alignment app's screen is a web page served by the machine the camera is plugged into (a laptop, desktop or RK3588 board), so
another computer can open it over Tailscale (http://<hostname>:8765) while the board's own screen shows it too.
Standard library only: no web framework to install.
"""
import glob
import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import align_vision as V
from session import SIDE_WORD

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
IMG_EXT = (".jpg", ".jpeg", ".png", ".tif", ".tiff")


class App:
    """Glues the session, live view, full-compute mode and the photo preview together; one slow job at a time."""

    def __init__(self, session, live, focus, vlm=None):
        self.s, self.live, self.focus, self.vlm = session, live, focus, vlm
        self.busy = ""
        self.error = ""
        self.scan = []
        self.scan_done = False
        self.preview = None          # jpeg bytes of the last photo with its markers drawn
        self.preview_name = ""
        self.camera_msg = ""
        self.tire = {"size": None, "raw": ""}
        self.job_lock = threading.Lock()

    # ------------------------------------------------------------------ jobs
    def run(self, label, fn):
        if not self.job_lock.acquire(blocking=False):
            raise RuntimeError(f"busy: {self.busy}")
        self.busy, self.error = label, ""

        def work():
            try:
                fn()
            except Exception as e:
                self.error = str(e).splitlines()[0][:300] if str(e) else type(e).__name__
                self.s.say(f"[!] {self.error}", "amber")
                traceback.print_exc()
            finally:
                self.busy = ""
                self.job_lock.release()
        threading.Thread(target=work, daemon=True).start()

    def camera(self):
        folder = os.path.join(self.s.cache, "photos")
        if self.s.source == "gphoto2":
            return V.Gphoto2Camera(folder)
        if self.s.source == "webcam":
            return V.WebcamCamera(folder)
        return None

    def take(self, n):
        cam = self.camera()
        if cam is None:
            raise ValueError("photo files: use the folder box instead")
        paths = []
        with self.live.paused():
            for _ in range(n):
                paths.append(cam.capture())
        self.make_preview(paths[-1])
        return paths

    def make_preview(self, path):
        import cv2
        img = cv2.imread(path)
        if img is None:
            return
        k = 1280.0 / max(img.shape[:2])
        small = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k < 1 else img
        ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            from liveview import overlay
            self.preview, _ = overlay(buf.tobytes(), self.live.detector)
            self.preview_name = os.path.basename(path)
        if self.vlm and self.vlm.ready():
            for note in self.vlm.check_photo(path):
                self.s.say(f"photo check: {note}", "amber")
        if self.vlm and self.vlm.ai_ready():
            threading.Thread(target=self._ai_check, args=(path,), daemon=True).start()

    def _ai_check(self, path):
        """Vision-model look at a photo, after the measuring work is done so it never slows a reading."""
        for _ in range(1200):
            if not self.busy:
                break
            time.sleep(0.5)
        try:
            notes = self.vlm.ai_check_photo(path)
            name = os.path.basename(path)
            if not notes:
                self.s.say(f"AI look at {name}: no problem seen", "muted")
            for n in notes:
                self.s.say(f"AI note on {name} (may be wrong, check it): {n}", "amber")
        except Exception as e:
            self.s.say(f"AI photo check failed: {str(e)[:200]}", "grey")

    @staticmethod
    def folder_photos(folder):
        folder = os.path.expanduser(folder or "")
        if not os.path.isdir(folder):
            raise ValueError(f"no such folder: {folder}")
        ps = sorted(p for p in glob.glob(os.path.join(folder, "*")) if p.lower().endswith(IMG_EXT))
        if not ps:
            raise ValueError(f"no photos in {folder}")
        return ps

    # ------------------------------------------------------------------ actions from the page
    def act(self, name, a):
        s = self.s
        if name == "car":
            s.set_car(a.get("make", ""), a.get("model", ""), a.get("year", ""))
            self.run("loading specs", s.load_specs)
        elif name == "load_specs":
            self.run("loading specs", s.load_specs)
        elif name == "pick_spec":
            s.pick_spec(a["key"], int(a["idx"]))
        elif name == "type_spec":
            s.type_spec(a["key"], a.get("center", ""), a.get("tol", ""), a.get("unit", "deg"), a.get("basis", "each"))
        elif name == "fields":
            s.set_fields(**{k: v for k, v in a.items() if isinstance(v, (str, int, float))})
        elif name == "source":
            s.source = a["source"]
            s.load_calibration()
            if self.live.running() or a.get("live"):
                self.live.start(s.source)
        elif name == "live":
            if a.get("on"):
                self.live.start(s.source)
            else:
                self.live.stop()
        elif name == "check_camera":
            cam = self.camera()

            def chk():
                with self.live.paused():
                    ok, msg = cam.detect()
                self.camera_msg = ("OK: " if ok else "[!] ") + msg
            if cam is None:
                self.camera_msg = "Photo files: type the folder the photos are in."
            else:
                self.run("checking camera", chk)
        elif name == "cal_add":
            if s.source == "files":
                self.run("reading calibration photos", lambda: s.add_cal_photos(self.folder_photos(a.get("folder"))))
            else:
                self.run("taking calibration photo", lambda: s.add_cal_photos(self.take(1)))
        elif name == "cal_solve":
            self.run("solving calibration", s.solve_calibration)
        elif name == "cal_reset":
            s.cal_photos = []
        elif name == "axle":
            s.axle_reset(a.get("axle"))
        elif name == "axle_photo":
            if not s.cal:
                raise ValueError("calibrate the camera first (CAMERA)")
            if s.source == "files":
                def from_folder():
                    s.axle_photos = self.folder_photos(a.get("folder"))
                    self.make_preview(s.axle_photos[-1])
                self.run("reading photos", from_folder)
            else:
                self.run("taking photo", lambda: s.axle_photos.extend(self.take(1)))
        elif name == "axle_compute":
            self.run("computing axle", s.compute_axle)
        elif name == "side_measure":
            wheel, flipped, shots = a["wheel"], bool(a.get("flipped")), max(1, min(9, int(a.get("shots", 3))))
            learn = a.get("_learn")
            if s.source == "files":
                self.run("measuring", lambda: s.measure_side(wheel, self.folder_photos(a.get("folder")), flipped,
                                                             learn))
            else:
                self.run("measuring", lambda: s.measure_side(wheel, self.take(shots), flipped, learn))
        elif name == "calib_start":
            s.calib_start(a.get("axle", "front"), a.get("side", "left"), a.get("units", 0.5), a.get("unit", "turn"),
                          a.get("plus", ""))
        elif name == "calib_cancel":
            s.calib_cancel()
        elif name == "adj":
            s.adj = {"wheel": a.get("wheel", s.adj["wheel"]), "qty": a.get("qty", s.adj["qty"])}
        elif name == "pick_rate":
            s.pick_rate(s.adj["wheel"], s.adj["qty"], a["id"])
        elif name == "plus":
            s.set_plus(s.adj["wheel"], s.adj["qty"], a.get("text", ""))
        elif name == "test_move":
            learn = s.start_test_move(s.adj["wheel"], s.adj["qty"], a.get("units", 0.25))
            if not s.pending_learn:          # side view: measure now and learn from it
                self.act("side_measure", {"wheel": s.adj["wheel"], "shots": a.get("shots", 3), "_learn": learn,
                                          "folder": a.get("folder")})
        elif name == "vlm":
            if self.vlm:
                if "on" in a:
                    self.vlm.enabled = bool(a["on"])
                if a.get("refind"):
                    self.vlm.find_backend()
        elif name == "read_tire":
            if not (self.vlm and self.vlm.backend):
                raise ValueError(self.vlm.backend_note if self.vlm else "no vision model")

            def rt():
                p = self.folder_photos(a.get("folder"))[-1] if s.source == "files" else self.take(1)[-1]
                size, raw = self.vlm.read_tire(p)
                self.tire = {"size": size, "raw": raw[:80]}
                s.say(f"tire size read from {os.path.basename(p)}: " + (f"{size} (press USE if that's right)"
                                                                        if size else f"not readable ({raw[:60]!r})"),
                      "ok" if size else "amber")
            self.run("reading the tire size", rt)
        elif name == "use_tire":
            if self.tire.get("size"):
                s.set_fields(tire=self.tire["size"])
                s.say(f"tire size {self.tire['size']} put in (read by the vision model, confirmed by you)")
                self.tire = {"size": None, "raw": ""}
        elif name == "focus_scan":
            def sc():
                self.scan = __import__("focus").scan()
                self.scan_done = True
            self.run("looking for busy programs", sc)
        elif name == "focus_pause":
            want = {int(p) for p in a.get("pids", [])}
            done = self.focus.pause([p for p in self.scan if p["pid"] in want])
            msg = self.focus.performance_governor() if a.get("governor") else ""
            s.say(f"full compute: paused {', '.join(done) or 'nothing'}" + (f"; {msg}" if msg else ""), "ok")
            self.scan, self.scan_done = [], False
        elif name == "focus_resume":
            n = self.focus.resume()
            s.say(f"resumed {n} program(s)", "ok")
        else:
            raise ValueError(f"unknown action {name}")

    def state(self):
        d = self.s.snapshot()
        d.update(busy=self.busy, error=self.error, camera_msg=self.camera_msg,
                 live={"on": self.live.running(), "error": self.live.error, "counts": self.live.counts},
                 preview=self.preview_name, focus=self.focus.status(), scan=self.scan, scan_done=self.scan_done,
                 vlm=self.vlm.status() if self.vlm else "off", side_words=SIDE_WORD, tire=self.tire,
                 vlm_on=bool(self.vlm and self.vlm.enabled), vlm_backend=bool(self.vlm and self.vlm.backend))
        return d


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            b = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                with open(os.path.join(WEB, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self._send(200, json.dumps(app.state()))
            if u.path == "/preview.jpg":
                return self._send(200, app.preview, "image/jpeg") if app.preview else self._send(404, b"")
            if u.path == "/live.mjpg":
                return self._stream()
            return self._send(404, b"not found", "text/plain")

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seq = -1
            try:
                while True:
                    seq, frame = app.live.wait_frame(seq)
                    if not app.live.running() and frame is None:
                        time.sleep(0.5)
                        continue
                    if frame:
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            u = urlparse(self.path)
            if not u.path.startswith("/api/"):
                return self._send(404, b"")
            n = int(self.headers.get("Content-Length") or 0)
            try:
                a = json.loads(self.rfile.read(n) or b"{}")
                a.pop("_learn", None)            # internal only
                app.act(u.path[5:], a)
                return self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e).splitlines()[0][:300]
                                                   if str(e) else type(e).__name__}))
    return H


def serve(app, hosts, port):
    servers = []
    for h in hosts:
        try:
            srv = ThreadingHTTPServer((h, port), make_handler(app))
        except OSError as e:
            print(f"could not listen on {h}:{port}: {e}")
            continue
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
    return servers
