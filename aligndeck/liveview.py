"""
Live view for aiming: the Nikon's live view through `gphoto2 --capture-movie --stdout` (a stream of small JPEGs),
or a webcam through OpenCV. Each frame gets the markers it can see drawn on it, colored by target, so you can aim
the tripod and check every target is visible before taking the real photos.

The camera can't stream and take a full photo at once: `paused()` stops the stream around a capture.
"""
import contextlib
import shutil
import subprocess
import threading
import time

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = np = None

import align_vision as V

COLORS = {"LEFT": (134, 174, 111), "RIGHT": (216, 145, 169), "FLOOR": (65, 164, 217)}   # BGR of the screen palette


def overlay(jpeg, detector):
    """Draw found markers on a live frame; returns (jpeg bytes, {"LEFT": n, "RIGHT": n, "FLOOR": n})."""
    counts = {"LEFT": 0, "RIGHT": 0, "FLOOR": 0}
    if cv2 is None or detector is None:
        return jpeg, counts
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jpeg, counts
    corners, ids, _ = detector.detectMarkers(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    if ids is not None:
        for c, i in zip(corners, ids.ravel()):
            name = "LEFT" if i in V.WHEEL_IDS else "RIGHT" if i in V.WHEEL_R_IDS else "FLOOR" if i in V.FLOOR_IDS \
                else None
            if not name:
                continue
            counts[name] += 1
            cv2.polylines(img, [c.astype(np.int32).reshape(-1, 1, 2)], True, COLORS[name], 2)
    y = 22
    for name, n in counts.items():
        cv2.putText(img, f"{name} {n}", (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 4)
        cv2.putText(img, f"{name} {n}", (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    COLORS[name] if n else (110, 110, 110), 2)
        y += 24
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return (buf.tobytes() if ok else jpeg), counts


class LiveView:
    def __init__(self):
        self.source = None           # "gphoto2" | "webcam" | None
        self.frame = None            # latest jpeg bytes (with overlay)
        self.counts = {}
        self.seq = 0
        self.error = ""
        self.cond = threading.Condition()
        self._stop = threading.Event()
        self._thread = None
        self._proc = None
        self._held = 0
        self.detector = None
        if V.HAVE_CV:
            self.detector = cv2.aruco.ArucoDetector(V._dict(), cv2.aruco.DetectorParameters())

    # ------------------------------------------------------------------ control
    def start(self, source):
        self.stop()
        self.source = source
        if source not in ("gphoto2", "webcam"):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(6)
        self._thread = self._proc = None

    @contextlib.contextmanager
    def paused(self):
        """Stop streaming while a full photo is taken, then carry on."""
        src, running = self.source, bool(self._thread)
        self._held += 1
        if running:
            self.stop()
            time.sleep(0.5)          # the camera needs a moment to leave live view
        try:
            yield
        finally:
            self._held -= 1
            if running and not self._held:
                self.start(src)

    def running(self):
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------ frames
    def _push(self, jpeg):
        out, counts = overlay(jpeg, self.detector)
        with self.cond:
            self.frame, self.counts, self.seq = out, counts, self.seq + 1
            self.cond.notify_all()

    def wait_frame(self, last_seq, timeout=2.0):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq or self._stop.is_set(), timeout)
            return self.seq, self.frame

    def _run(self):
        try:
            if self.source == "gphoto2":
                self._run_gphoto2()
            else:
                self._run_webcam()
        except Exception as e:          # shown on the screen; the rest of the app keeps working
            self.error = str(e)[:300]

    def _run_gphoto2(self):
        if not shutil.which("gphoto2"):
            self.error = "gphoto2 is not installed"
            return
        self.error = ""
        self._proc = subprocess.Popen(["gphoto2", "--capture-movie", "--stdout"], stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, bufsize=0)
        buf = b""
        last = 0.0
        while not self._stop.is_set():
            chunk = self._proc.stdout.read(65536)
            if not chunk:
                self.error = "live view stopped (camera busy, asleep or unplugged)"
                break
            buf += chunk
            while True:
                a = buf.find(b"\xff\xd8")
                b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                if a < 0 or b < 0:
                    buf = buf[a:] if a >= 0 else b""
                    break
                jpeg, buf = buf[a:b + 2], buf[b + 2:]
                if time.time() - last > 0.08:          # ~12 fps is plenty for aiming
                    last = time.time()
                    self._push(jpeg)

    def _run_webcam(self):
        if cv2 is None:
            self.error = "OpenCV is missing"
            return
        cap = cv2.VideoCapture(0)
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            while not self._stop.is_set():
                ok, img = cap.read()
                if not ok:
                    self.error = "webcam gave no frame"
                    break
                ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok:
                    self._push(buf.tobytes())
                time.sleep(0.05)
        finally:
            cap.release()
