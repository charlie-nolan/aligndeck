"""
Photo helper. It never measures and never changes a reading or a spec; it only adds notes and suggestions.

1. OpenCV checks on every photo (always on, fast): blurry targets, glare, targets cut off by the frame edge.
2. A local vision model, Qwen2.5-VL-3B (optional, slower, can be wrong, so its notes are labelled as such):
   - looks at each photo for things OpenCV can't judge: a target blocked by something, a lug frame hanging loose
     or far off the wheel, a person or tool in the way;
   - reads the tire size off a sidewall photo. The size is only offered: you press USE before it goes in.
   Where it runs, first one found:
     NPU   the RKLLM multimodal demo on an RK3588 NPU: models/qwen2.5-vl-3b/ with demo, lib/, *.rknn (vision) and
           *.rkllm (language), in the app's data folder. Build them with tools/export_qwen25vl_rkllm.sh (x86 PC)
           and tools/build_vlm_runner_rk3588.sh (on the RK3588 board).
     Ollama (this machine or another one, --ollama URL) with a qwen2.5vl model (ollama pull qwen2.5vl:7b).
   It waits until the measuring work is idle, so it never slows a reading down.
"""
import base64
import glob
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.request

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = np = None

from . import align_math as M
from . import align_vision as V

OLLAMA = os.environ.get("ALIGNDECK_OLLAMA", "http://localhost:11434")
DECK_DIR_NAME = "qwen2.5-vl-3b"
TIRE_RE = re.compile(r"(?:P|LT)?(\d{3})/(\d{2})Z?R(\d{2}(?:\.\d)?)", re.I)

PHOTO_PROMPT = ("This photo is for a camera wheel alignment. Black and white checkerboard targets are mounted on "
                "frames bolted to the wheels and one lies flat on the floor. List only real problems, one per line "
                "starting with '- ': a target blocked by something, a target bent or not flat, a wheel frame hanging "
                "loose, a person or tool in front of a target. If you see no problem, answer only: OK")
TIRE_PROMPT = ("Read the tire size molded on this tire sidewall, for example 215/60R16. Answer with only the size. "
               "If you cannot read it clearly, answer only: NONE")


class Vlm:
    def __init__(self, models_dir, ollama=OLLAMA):
        self.models_dir = models_dir
        self.ollama = ollama
        self.enabled = True
        self.backend, self.backend_note = None, ""
        self.ai_lock = threading.Lock()
        self.last = ""
        self.find_backend()

    def find_backend(self):
        d = os.path.join(self.models_dir, DECK_DIR_NAME)
        demo = os.path.join(d, "demo")
        rknn = sorted(glob.glob(os.path.join(d, "*.rknn")))
        rkllm = sorted(glob.glob(os.path.join(d, "*.rkllm")))
        if os.access(demo, os.X_OK) and rknn and rkllm:
            self.backend = ("deck", demo, rknn[0], rkllm[0])
            self.backend_note = f"Qwen2.5-VL-3B on this machine's NPU ({os.path.basename(rkllm[0])})"
            return
        try:
            with urllib.request.urlopen(self.ollama + "/api/tags", timeout=3) as r:
                names = [m["name"] for m in json.load(r).get("models", [])]
            vl = sorted((n for n in names if "qwen2.5vl" in n or "qwen2.5-vl" in n), key=lambda n: ("3b" not in n, n))
            if vl:
                self.backend = ("pc", vl[0])
                self.backend_note = f"{vl[0]} on Ollama ({self.ollama})"
                return
            self.backend_note = (f"no vision model: Ollama has no qwen2.5vl (ollama pull qwen2.5vl:7b), and "
                                 f"{d} is not built")
        except (OSError, ValueError):
            self.backend_note = f"no vision model: {d} is not built and Ollama is not reachable"
        self.backend = None

    def ask(self, image_path, prompt, max_tokens=120):
        """The model's answer as text, or raise."""
        if not self.backend:
            raise RuntimeError(self.backend_note)
        with self.ai_lock, tempfile.TemporaryDirectory() as tmp:
            small = os.path.join(tmp, "in.jpg")
            img = cv2.imread(image_path)
            if img is None:
                raise RuntimeError("photo could not be read")
            k = 1024.0 / max(img.shape[:2])
            if k < 1:
                img = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
            cv2.imwrite(small, img, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if self.backend[0] == "deck":
                _, demo, rknn, rkllm = self.backend
                d = os.path.dirname(demo)
                env = dict(os.environ, LD_LIBRARY_PATH=os.path.join(d, "lib"))
                cmd = [demo, small, rknn, "", "", rkllm, str(max_tokens), "2048", "3", "rk3588",
                       "<|vision_start|>", "<|vision_end|>", "<|image_pad|>"]
                r = subprocess.run(cmd, input=f"<image>{prompt}\nexit\n", capture_output=True, text=True,
                                   cwd=tmp, env=env, timeout=300)
                out = r.stdout
                if "robot:" not in out:
                    raise RuntimeError((r.stderr or out).strip()[-300:] or "vision model gave no answer")
                return out.rsplit("robot:", 1)[1].split("user:")[0].strip()
            with open(small, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            body = json.dumps({"model": self.backend[1], "prompt": prompt, "images": [b64], "stream": False,
                               "options": {"temperature": 0, "num_predict": max_tokens}}).encode()
            req = urllib.request.Request(self.ollama + "/api/generate", body, {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r).get("response", "").strip()

    def ready(self):
        return cv2 is not None and V.HAVE_CV

    def ai_ready(self):
        return self.enabled and self.backend is not None

    def status(self):
        ai = ("vision model: " + self.backend_note + ("" if self.enabled else " (switched off)")) if self.backend \
            else self.backend_note
        return "photo checks: OpenCV (sharpness, glare, cut-off targets). " + ai + (f". Last: {self.last}"
                                                                                   if self.last else "")

    def check_photo(self, path):
        """Fast OpenCV notes for one photo."""
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return [f"{os.path.basename(path)} could not be read"]
        notes = []
        h, w = img.shape
        det = cv2.aruco.ArucoDetector(V._dict(), cv2.aruco.DetectorParameters())
        corners, ids, _ = det.detectMarkers(img)
        if ids is None:
            return ["no target markers found in the photo"]
        groups = {"left target": V.WHEEL_IDS, "right target": V.WHEEL_R_IDS, "floor target": V.FLOOR_IDS}
        for name, idset in groups.items():
            cs = [c.reshape(-1, 2) for c, i in zip(corners, ids.ravel()) if i in idset]
            if not cs:
                continue
            pts = np.vstack(cs)
            x0, y0 = pts.min(0).astype(int)
            x1, y1 = pts.max(0).astype(int)
            if x0 < 0.02 * w or y0 < 0.02 * h or x1 > 0.98 * w or y1 > 0.98 * h:
                notes.append(f"{name} touches the edge of the frame: move the camera so it is fully inside")
            roi = img[max(0, y0):y1, max(0, x0):x1]
            if roi.size:
                sharp = cv2.Laplacian(roi, cv2.CV_64F).var()
                if sharp < 60:
                    notes.append(f"{name} looks soft (sharpness {sharp:.0f}): refocus or use a faster shutter")
                if (roi > 250).mean() > 0.05:
                    notes.append(f"{name} has glare: tilt it or move the light")
            if len(cs) < 6:
                notes.append(f"only {len(cs)} markers seen on the {name}")
        return notes

    def ai_check_photo(self, path):
        """Vision-model notes for one photo ([] when it sees no problem)."""
        t = time.time()
        ans = self.ask(path, PHOTO_PROMPT)
        self.last = f"photo check took {time.time() - t:.0f} s"
        if re.fullmatch(r"\W*ok\W*", ans, re.I):
            return []
        lines = [l.strip(" -*•\t") for l in ans.splitlines() if l.strip(" -*•\t")]
        return [l for l in lines if not re.fullmatch(r"ok\.?", l, re.I)][:4]

    def read_tire(self, path):
        """(size like '215/60R16', raw answer) or (None, raw answer). Only offered to the user, never filled in."""
        t = time.time()
        ans = self.ask(path, TIRE_PROMPT, max_tokens=24)
        self.last = f"tire read took {time.time() - t:.0f} s"
        m = TIRE_RE.search(ans.replace(" ", ""))
        if not m:
            return None, ans
        size = f"{m.group(1)}/{m.group(2)}R{m.group(3)}"
        return (size if M.tire_diameter_mm(size) else None), ans
