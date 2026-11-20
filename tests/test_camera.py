"""gphoto2 capture: waits while the camera is still busy, never kills another program, says how to free it."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aligndeck import align_vision as V  # noqa: E402


class Result:
    def __init__(self, code, err=""):
        self.returncode, self.stderr, self.stdout = code, err, ""


class Gphoto2Busy(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cam = V.Gphoto2Camera(self.dir)
        self.cam.busy_wait = 2.0
        self.calls = []

    def fake(self, replies):
        def run(cmd, **kw):
            self.calls.append(cmd)
            r = replies.pop(0) if len(replies) > 1 else replies[0]
            if r.returncode == 0:
                tmpl = cmd[cmd.index("--filename") + 1]
                open(tmpl.replace("%n", "1").replace("%C", "jpg"), "w").close()
            return r
        return run

    def test_retries_while_live_view_lets_go(self):
        busy = Result(1, "*** Error: Could not claim the USB device ***")
        with mock.patch.object(V.subprocess, "run", self.fake([busy, busy, Result(0)])), \
                mock.patch.object(V.time, "sleep"):
            path = self.cam.capture()
        self.assertTrue(path.endswith(".jpg"))
        self.assertEqual(len(self.calls), 3)

    def test_held_camera_is_reported_not_killed(self):
        busy = Result(1, "Could not lock the device")
        with mock.patch.object(V.subprocess, "run", self.fake([busy])), mock.patch.object(V.time, "sleep"), \
                mock.patch.object(V.time, "monotonic", side_effect=[0.0, 1.0, 3.0, 5.0]):
            with self.assertRaises(RuntimeError) as e:
                self.cam.capture()
        self.assertIn("holding the camera", str(e.exception))
        self.assertTrue(all(c[0] == "gphoto2" for c in self.calls), self.calls)

    def test_other_errors_are_not_retried(self):
        with mock.patch.object(V.subprocess, "run", self.fake([Result(1, "No camera found")])):
            with self.assertRaises(RuntimeError) as e:
                self.cam.capture()
        self.assertIn("No camera found", str(e.exception))
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
