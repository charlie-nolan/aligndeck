"""
End-to-end check of the standalone app through its web API, with synthetic photos (no camera, no car, no manual DB):
calibrate from a folder, measure the front axle from a folder, type the specs, read the results and the plan.

    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "aligndeck"))
sys.path.insert(0, HERE)
import test_alignment as T  # noqa: E402  (synthetic renderer)

if T.HAVE:
    import cv2
    import numpy as np
    from focus import Focus
    from liveview import LiveView
    from server import App, serve
    from session import Session
    import vlm as VLM


@unittest.skipUnless(T.HAVE, "OpenCV with aruco not installed")
class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="aligndeck_test_")
        K = np.array([[T.F, 0, T.W / 2 + 7], [0, T.F, T.H / 2 - 5], [0, 0, 1]])
        cal_dir, ax_dir = os.path.join(cls.tmp, "cal"), os.path.join(cls.tmp, "axle")
        os.makedirs(cal_dir)
        os.makedirs(ax_dir)
        rng = np.random.default_rng(1)
        for i in range(20):
            ang = rng.uniform(-35, 35, 3)
            Rb = cv2.Rodrigues(np.radians(ang))[0]
            z = rng.uniform(0.9, 1.4)
            center = np.array([rng.uniform(-0.3, 0.3) * z, rng.uniform(-0.18, 0.18) * z, z])
            cv2.imwrite(os.path.join(cal_dir, f"c{i:02d}.png"),
                        T.render([("wheel", center, Rb, np.eye(3), np.zeros(3))], K, seed=i))
        cls.cams, cls.toes = (-0.25, -0.40), (0.10, -0.05)
        for i, roll in enumerate((-25, 0, 25)):
            cv2.imwrite(os.path.join(ax_dir, f"a{i}.png"), T.render(T.axle_scene(cls.cams, cls.toes, roll), K, seed=200 + i))
        cls.cal_dir, cls.ax_dir = cal_dir, ax_dir
        s = Session("Testmake", "Testcar", "2002", cache_dir=cls.tmp)
        s.source = "files"
        cls.app = App(s, LiveView(), Focus(os.path.join(cls.tmp, "alignment")), VLM.Vlm(cls.tmp))
        cls.port = 18765 + os.getpid() % 1000
        cls.servers = serve(cls.app, ["127.0.0.1"], cls.port)

    @classmethod
    def tearDownClass(cls):
        for s in cls.servers:
            s.shutdown()

    def post(self, name, **body):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/{name}", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req, timeout=30))
        self.assertTrue(r["ok"], r.get("error"))
        self.wait()

    def wait(self):
        for _ in range(600):
            if not self.app.busy:
                return
            time.sleep(0.1)
        self.fail("job never finished")

    def state(self):
        return json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/state", timeout=30))

    def ensure_cal(self):
        if not self.app.s.cal:
            self.post("cal_add", folder=self.cal_dir)
            self.post("cal_solve")

    def test_calibrate_toe_adjuster(self):
        """Photos, turn the left tie rod 1/2 turn (synthetic: left toe +0.20), photos: 0.40 deg/turn for both sides,
        and the move list then says how far to turn each side."""
        self.ensure_cal()
        K = np.array([[T.F, 0, T.W / 2 + 7], [0, T.F, T.H / 2 - 5], [0, 0, 1]])
        d2 = os.path.join(self.tmp, "axle_after")
        os.makedirs(d2, exist_ok=True)
        moved = (self.toes[0] + 0.20, self.toes[1])
        for i, roll in enumerate((-25, 0, 25)):
            cv2.imwrite(os.path.join(d2, f"b{i}.png"), T.render(T.axle_scene(self.cams, moved, roll), K, seed=300 + i))
        self.post("calib_start", axle="front", side="left", units=0.5, unit="turn",
                  plus="clockwise viewed from inside the car")
        self.assertIn("Step 1", self.state()["calib"]["hint"])
        self.post("axle_photo", folder=self.ax_dir)
        self.post("axle_compute")
        self.assertIn("Step 2", self.state()["calib"]["hint"])
        self.post("axle_photo", folder=d2)
        self.post("axle_compute")
        st = self.state()
        self.assertFalse(st["calib"]["active"], st["log"][-4:])
        for w in ("LF", "RF"):
            lr = self.app.s.state["learned"][f"{w}|toe"]
            self.assertAlmostEqual(lr["rate"], 0.40, delta=0.06)
        self.post("type_spec", key="toe|front", center="0", tol="0.05", unit="deg", basis="each")
        plan = " ".join(t for _, t in self.state()["plan"])
        self.assertIn("clockwise viewed from inside the car", plan)
        self.assertIn("turn", plan)

    def test_flow(self):
        page = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10).read().decode()
        self.assertIn("WHEEL ALIGNMENT", page)
        self.ensure_cal()
        st = self.state()
        self.assertTrue(st["camera"]["calibrated"], st["log"][-3:])
        self.post("axle", axle="front")
        self.post("axle_photo", folder=self.ax_dir)
        self.post("axle_compute")
        self.post("type_spec", key="camber|front", center="-0.3", tol="0.5", unit="deg", basis="each")
        self.post("type_spec", key="toe|front", center="0", tol="3", unit="mm", basis="total")
        self.post("fields", tire="215/60R16")
        st = self.state()
        self.assertEqual(st["error"], "", st["log"][-5:])
        lf = self.app.s.readings["LF"]
        self.assertAlmostEqual(lf["camber"], self.cams[0], delta=0.05)
        self.assertAlmostEqual(lf["total_toe"], sum(self.toes), delta=0.05)
        rows = {r["wheel"]: r for r in st["results"]}
        self.assertIn("in spec", rows["LEFT FRONT (driver)"]["verdict"])
        self.assertTrue(any("change" in t for _, t in st["plan"]))
        self.assertTrue(st["preview"])
        urllib.request.urlopen(f"http://127.0.0.1:{self.port}/preview.jpg", timeout=10).read()


if __name__ == "__main__":
    unittest.main()


class FakeOllama:
    """Stands in for the PC's Ollama with a qwen2.5vl model."""
    def __init__(self, port):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _j(self, d):
                b = json.dumps(d).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                self._j({"models": [{"name": "qwen3:8b"}, {"name": "qwen2.5vl:3b"}]})

            def do_POST(self):
                d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert d["images"] and d["model"] == "qwen2.5vl:3b"
                self._j({"response": "P215/60 R16 95H" if "tire size" in d["prompt"] else
                         "- a floor jack is in front of the right target"})
        self.srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@unittest.skipUnless(T.HAVE, "OpenCV with aruco not installed")
class VlmTests(unittest.TestCase):
    def test_pc_backend_notes_and_tire(self):
        port = 19500 + os.getpid() % 400
        fake = FakeOllama(port)
        try:
            v = VLM.Vlm("/nonexistent", ollama=f"http://127.0.0.1:{port}")
            self.assertEqual(v.backend, ("pc", "qwen2.5vl:3b"))
            img = os.path.join(tempfile.mkdtemp(), "x.jpg")
            cv2.imwrite(img, np.full((300, 400, 3), 128, np.uint8))
            self.assertEqual(v.ai_check_photo(img), ["a floor jack is in front of the right target"])
            self.assertEqual(v.read_tire(img)[0], "215/60R16")
        finally:
            fake.srv.shutdown()

    def test_deck_backend_parses_demo_output(self):
        d = os.path.join(tempfile.mkdtemp(), VLM.DECK_DIR_NAME)
        os.makedirs(d)
        for n in ("v_rk3588.rknn", "l_rk3588.rkllm"):
            open(os.path.join(d, n), "w").close()
        demo = os.path.join(d, "demo")
        with open(demo, "w") as f:
            f.write("#!/bin/sh\nread line\necho 'rkllm init success'\necho '[0] <image>What is in the image?'\n"
                    "printf '\\nuser: robot: OK\\n\\nuser: '\n")
        os.chmod(demo, 0o755)
        v = VLM.Vlm(os.path.dirname(d), ollama="http://127.0.0.1:1")
        self.assertEqual(v.backend[0], "deck")
        img = os.path.join(d, "x.jpg")
        cv2.imwrite(img, np.full((300, 400, 3), 128, np.uint8))
        self.assertEqual(v.ai_check_photo(img), [])

    def test_tire_regex_rejects_junk(self):
        self.assertIsNone(VLM.TIRE_RE.search("NONE"))
        self.assertEqual(VLM.TIRE_RE.search("LT265/70R17").groups(), ("265", "70", "17"))
