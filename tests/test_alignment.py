"""
Synthetic checks for the ALIGNMENT screen (no camera, no car): photos of the printed targets are rendered with a
known camera, known camber/toe and a known string-line error, then read back.

    python -m unittest discover -s tests -v
"""
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "aligndeck"))
import align_math as M  # noqa: E402

try:
    import cv2
    import numpy as np
    import align_vision as V
    HAVE = V.HAVE_CV
except ImportError:
    HAVE = False

W, H = 3000, 2000            # rendered photo size
F = 4190.0                   # ~ a 50 mm lens on a full-frame body, scaled to 3000 px wide


def look_at(cam, target):
    z = np.array(target, float) - cam
    z /= np.linalg.norm(z)
    x = np.cross(z, [0, 0, 1.0])       # world z is up; camera x to the right
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    return np.vstack([x, y, z])         # rows: camera axes in world coords (world -> camera)


def board_rot(z_into_page, x_dir):
    z = np.array(z_into_page, float)
    z /= np.linalg.norm(z)
    x = np.array(x_dir, float) - z * np.dot(x_dir, z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    return np.column_stack([x, y, z])   # board axes in world coords


def render(scene, K, noise=1.5, seed=0):
    """scene: [(which, center_world_m, R_board_world)] -> grayscale photo."""
    rng = np.random.default_rng(seed)
    out = np.full((H, W), 90, np.float32)
    for which, center, Rbw, Rcw, C in scene:
        img, s, (x0, y0) = V.target_image(which, dpi=200)
        cols, rows = V.WHEEL_SQUARES if which.startswith("wheel") else V.FLOOR_SQUARES
        bw, bh = cols * V.SQUARE_MM / 1000, rows * V.SQUARE_MM / 1000
        O = np.array(center) - Rbw @ np.array([bw / 2, bh / 2, 0])     # board origin (top-left corner) in world
        Mx = Rcw @ Rbw
        k = 1.0 / (s * 1000.0)          # sheet pixels -> metres, origin moved to the board corner
        A = np.column_stack([Mx[:, 0] * k, Mx[:, 1] * k, Rcw @ (O - C) - (Mx[:, 0] * x0 + Mx[:, 1] * y0) * k])
        Hm = K @ A
        # anti-alias: blur the sheet by about half its shrink factor (stands in for the lens and sensor)
        cx, cy = x0 + img.shape[1] * 0 + (cols * V.SQUARE_MM * s) / 2, y0 + (rows * V.SQUARE_MM * s) / 2
        p0 = Hm @ [cx, cy, 1.0]
        p1 = Hm @ [cx + 10, cy, 1.0]
        shrink = 10.0 / max(1e-6, np.linalg.norm(p1[:2] / p1[2] - p0[:2] / p0[2]))
        img = cv2.GaussianBlur(img, (0, 0), max(0.8, 0.5 * shrink))
        warped = cv2.warpPerspective(img.astype(np.float32), Hm, (W, H), flags=cv2.INTER_AREA)
        mask = cv2.warpPerspective(np.ones_like(img, np.float32), Hm, (W, H))
        out = out * (1 - mask) + warped * mask
    out = cv2.GaussianBlur(out, (0, 0), 0.8) + rng.normal(0, noise, out.shape)
    out = np.clip(out, 0, 255).astype(np.uint8)
    return out


def wheel_scene(wheel, camber, toe, yaw=0.0):
    """Wheel target on the given wheel with true camber/toe, floor target on a string yawed outward by yaw (deg),
    camera on a tripod outside the car looking at the wheel."""
    left = wheel in ("LF", "LR")
    side = 1.0 if left else -1.0
    c, t = math.radians(camber), math.radians(toe)
    n_out = np.array([math.sin(t) * math.cos(c), side * math.cos(t) * math.cos(c), -math.sin(c)])
    hub = np.array([0.0, side * 0.75, 0.32])
    w_center = hub + n_out * 0.08
    Rw = board_rot(-n_out, np.cross(n_out, [0, 0, 1.0]) * side)   # x along the wheel's heading
    b = math.radians(yaw)
    f_center = np.array([0.15, side * 1.25, 0.0])
    Rf = board_rot([0, 0, -1.0], [math.cos(b), side * math.sin(b), 0])
    C = np.array([0.35, side * 2.6, 0.75])
    Rcw = look_at(C, [0.05, side * 1.0, 0.2])
    return [("wheel", w_center, Rw, Rcw, C), ("floor", f_center, Rf, Rcw, C)]


def rodrigues(axis, deg):
    return cv2.Rodrigues(np.array(axis, float) / np.linalg.norm(axis) * math.radians(deg))[0]


def axle_scene(cams, toes, roll_deg, mount_err=((3, 1, 2), (-2, 2, 1)), rear=False):
    """Front view of one axle: wheel targets on lug frames (30 cm out on the spin axis, facing the camera, mounted
    a few degrees crooked), the car rolled forward by roll_deg of a wheel turn, floor target in front, camera in
    front (behind the car for the rear axle; then 'left' is still the car's left)."""
    out = []
    fwd = np.array([1.0, 0, 0]) * (-1 if rear else 1)      # toward the camera
    C = np.array([3.6, 0.0, 1.0]) * np.array([-1 if rear else 1, 1, 1])
    Rcw = look_at(C, [0.6 * (-1 if rear else 1), 0, 0.1])
    travel = 0.32 * math.radians(roll_deg)                 # the car moves as the wheel turns (radius 0.32 m)
    for which, side, cam, toe, err in (("wheel_l", 1, cams[0], toes[0], mount_err[0]),
                                       ("wheel_r", -1, cams[1], toes[1], mount_err[1])):
        c, t = math.radians(cam), math.radians(toe)
        a = np.array([math.sin(t) * math.cos(c), side * math.cos(t) * math.cos(c), -math.sin(c)])   # outward axis
        hub = np.array([travel, side * 0.75, 0.32])
        z_into = -(fwd - a * np.dot(fwd, a))               # page faces the camera
        R0 = board_rot(z_into, np.cross([0, 0, 1.0], z_into))
        R0 = rodrigues(err, np.linalg.norm(err)) @ R0      # the frame isn't square on the lugs
        Rw = rodrigues(a, -roll_deg * side) @ R0           # rolling forward turns the wheel about its axis
        out.append((which, hub + a * 0.30, Rw, Rcw, C))
    out.append(("floor", np.array([1.4 * (-1 if rear else 1), 0, 0.0]), board_rot([0, 0, -1.0], [1, 0, 0]), Rcw, C))
    return out


class MathTests(unittest.TestCase):
    def test_wheel_angles_all_wheels(self):
        for wheel in M.WHEELS:
            side = 1 if wheel in ("LF", "LR") else -1
            for cam, toe in ((-0.5, 0.2), (1.0, -0.3), (0.0, 0.0)):
                c, t = math.radians(cam), math.radians(toe)
                n = (math.sin(t) * math.cos(c), side * math.cos(t) * math.cos(c), -math.sin(c))
                got = M.wheel_angles(n, (0, 0, 1), (1, 0, 0), wheel)
                self.assertAlmostEqual(got[0], cam, places=9)
                self.assertAlmostEqual(got[1], toe, places=9)

    def test_floor_not_level_and_fwd_not_level(self):
        # the whole car and floor rolled 1 deg and pitched 0.5 deg: camber and toe must not change
        def rot(v, axis, deg):
            a = math.radians(deg)
            c, s = math.cos(a), math.sin(a)
            x, y, z = v
            if axis == "x":
                return (x, c * y - s * z, s * y + c * z)
            return (c * x + s * z, y, -s * x + c * z)
        t = math.radians(0.2)
        n = (math.sin(t), math.cos(t), 0.0)               # LF, 0 camber, 0.2 toe-in, before tilting
        tilt = lambda v: rot(rot(v, "x", 1.0), "y", 0.5)  # noqa: E731
        cam, toe = M.wheel_angles(tilt(n), tilt((0, 0, 1)), tilt((1, 0, 0)), "LF")
        self.assertAlmostEqual(cam, 0.0, places=9)
        self.assertAlmostEqual(toe, 0.2, places=9)

    def test_string_yaw(self):
        self.assertAlmostEqual(M.string_yaw(100, 100, 2500), 0.0)
        self.assertAlmostEqual(M.string_yaw(100, 100, 2500, 1500, 1460), math.degrees(math.atan2(20, 2500)))
        self.assertGreater(M.string_yaw(105, 100, 2500), 0)

    def test_turns(self):
        units, r, sign = M.adjuster_moves(-0.39, M.learned_rate(0.10, 0.22, 1.0))
        self.assertEqual(M.fmt_turns(r), "3 1/4")
        self.assertEqual(sign, "-")
        self.assertEqual(M.fmt_turns(0.0625), "0")      # rounds to the nearest 1/8
        self.assertEqual(M.fmt_angle(-0.25), "-0°15' (-0.25°)")

    def test_tire_diameter(self):
        self.assertAlmostEqual(M.tire_diameter_mm("P215/60R16"), 664.4, places=1)
        self.assertAlmostEqual(M.tire_diameter_mm("31x10.50R15"), 787.4, places=1)
        self.assertIsNone(M.tire_diameter_mm("hello"))


class SpecParseTests(unittest.TestCase):
    """Manual lines in the shapes the make DBs hold (CHARM table rows, FSM markdown, one-value-per-line pages)."""

    def specs(self, body, axle=None):
        import align_specs as S
        return {(q, a, b, u): (round(c, 4) if c is not None else None, lo, hi)
                for q, a, b, u, c, lo, hi, _ in S.parse_specs(body, axle)}

    def test_charm_table_row(self):
        got = self.specs("Camber Tolerance : +/- 0 deg 30' | Front | -0 deg 15'\nRear | -0 deg 35'\n"
                         "Toe-In | Front | 0 +/-3 mm Each Toe Angle : +/- 0 deg 09' When performing toe-in adjustment\n"
                         "Caster Common difference : +/- 0 deg 45' | Front | 2 deg 37'")
        self.assertEqual(got[("camber", "front", "each", "deg")], (-0.25, -0.5, 0.5))
        self.assertEqual(got[("camber", "rear", "each", "deg")][0], round(-35 / 60, 4))
        self.assertEqual(got[("toe", "front", "total", "mm")], (0.0, -3.0, 3.0))
        self.assertEqual(got[("toe", "front", "each", "deg")], (None, -0.15, 0.15))
        self.assertNotIn(("caster", "front", "", "deg"), got)       # 'difference' is not a tolerance

    def test_one_value_per_line(self):
        got = self.specs("Camber\nFront\n0°00' ± 45'\nRear\n- 0°45' ± 45'\nTotal toe\nFront\n0 ± 3 mm (0 ± 0.12 inch)")
        self.assertEqual(got[("camber", "rear", "each", "deg")], (-0.75, -0.75, 0.75))
        self.assertEqual(got[("toe", "front", "total", "mm")], (0.0, -3.0, 3.0))

    def test_prose_is_not_a_spec(self):
        self.assertEqual(self.specs("Moving the adjusting bolt by one scale graduation changes camber by "
                                    "approximately 0°10'.\nReset LF toe from 0.45 degrees to 0.10 degrees."), {})

    def test_rates_and_directions(self):
        import align_specs as S
        r = S.parse_rates("NOTE:\nMoving the adjusting bolt by one scale graduation changes camber by approximately "
                          "0^{°}10' .")
        self.assertEqual((r[0][0], r[0][1], round(r[0][2], 4), r[0][3]), ("camber", "graduation", 0.1667, "deg"))
        _, rules = S.parse_directions("Both the left and right tie-rods are right-hand threaded. To increase toe-in, "
                                      "turn both tie-rods clockwise equal amounts (as viewed from the inside of the "
                                      "vehicle).")
        self.assertEqual(rules[0][:4], ("toe", "increase", "cw", "inside"))


@unittest.skipUnless(HAVE, "OpenCV with aruco not installed")
class VisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.K = np.array([[F, 0, W / 2 + 7], [0, F, H / 2 - 5], [0, 0, 1]])
        cls.tmp = tempfile.mkdtemp(prefix="align_test_")
        # calibration photos: the wheel target held at many angles
        rng = np.random.default_rng(1)
        paths = []
        for i in range(20):
            C = np.array([0, 0, 0.0])
            Rcw = np.eye(3)
            ang = rng.uniform(-35, 35, 3)
            Rb = cv2.Rodrigues(np.radians(ang))[0] @ np.eye(3)
            z = rng.uniform(0.9, 1.4)   # spread over the whole frame, corners included
            center = np.array([rng.uniform(-0.3, 0.3) * z, rng.uniform(-0.18, 0.18) * z, z])
            img = render([("wheel", center, Rb, Rcw, C)], cls.K, seed=i)
            p = os.path.join(cls.tmp, f"cal_{i}.png")
            cv2.imwrite(p, img)
            paths.append(p)
        cls.cal = V.calibrate(paths)

    def test_calibration(self):
        K = np.array(self.cal["K"])
        self.assertLess(self.cal["rms"], 0.5)
        self.assertAlmostEqual(K[0, 0] / F, 1.0, delta=0.01)

    def test_measure(self):
        cases = [("LF", -0.25, 0.10, 0.0), ("RF", 0.40, -0.20, 0.0), ("LR", -0.58, 0.08, 0.5),
                 ("RR", 0.0, 0.30, -0.4)]
        worst = 0.0
        for i, (wheel, cam, toe, yaw) in enumerate(cases):
            img = render(wheel_scene(wheel, cam, toe, yaw), self.K, seed=100 + i)
            p = os.path.join(self.tmp, f"m_{wheel}.png")
            cv2.imwrite(p, img)
            r = V.measure_photo(p, wheel, self.cal)
            self.assertNotIn("error", r, r.get("error"))
            got_toe = r["toe"] - yaw            # the string-line correction the screen applies
            worst = max(worst, abs(r["camber"] - cam), abs(got_toe - toe))
            self.assertAlmostEqual(r["camber"], cam, delta=0.05, msg=f"{wheel} camber")
            self.assertAlmostEqual(got_toe, toe, delta=0.05, msg=f"{wheel} toe")
        print(f"\n  worst synthetic error {worst:.3f} deg")

    def test_front_view_axle(self):
        worst = 0.0
        for rear, cams, toes in ((False, (-0.25, -0.40), (0.10, -0.05)), (True, (-0.58, -0.50), (0.08, 0.15))):
            paths = []
            for i, roll in enumerate((-25, 0, 25)):
                img = render(axle_scene(cams, toes, roll, rear=rear), self.K, seed=200 + i)
                p = os.path.join(self.tmp, f"axle_{rear}_{i}.png")
                cv2.imwrite(p, img)
                paths.append(p)
            r = V.measure_axle(paths, self.cal)
            self.assertNotIn("error", r, r.get("error"))
            errs = (r["camber_l"] - cams[0], r["camber_r"] - cams[1], r["total_toe"] - sum(toes))
            worst = max(worst, *map(abs, errs))
            for e, what in zip(errs, ("camber L", "camber R", "total toe")):
                self.assertLess(abs(e), 0.05, f"{'rear' if rear else 'front'} {what} off by {e:.3f}")
        print(f"\n  front-view worst error {worst:.3f} deg (frames mounted 2-4 deg crooked)")


if __name__ == "__main__":
    unittest.main()
