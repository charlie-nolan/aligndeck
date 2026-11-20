"""
Camera side of the ALIGNMENT screen: printed ChArUco targets, camera calibration, target pose, and the camber /
toe each photo shows. Needs OpenCV with the aruco module (pip install opencv-contrib-python-headless, 4.7+).

Two printed targets, told apart by their marker ids, can be in the same photo:
  WHEEL target  5x7 squares, portrait sheet, clamped flat against the wheel (parallel to the wheel face)
  FLOOR target  7x5 squares, landscape sheet, flat on the floor beside the tire, its long bottom edge on a string
                line parallel to the car, arrow toward the FRONT of the car
Angles don't depend on the printed size (only on the print being flat and not stretched), so printer scaling only
changes the distances shown, not camber or toe.

Cameras: a Nikon (or any libgphoto2 camera) over USB through the gphoto2 command, a UVC webcam through OpenCV,
or photo files (e.g. copied off the SD card).
"""
import glob
import json
import os
import shutil
import subprocess
import time

try:
    import cv2
    import numpy as np
    HAVE_CV = hasattr(cv2, "aruco") and hasattr(cv2.aruco, "CharucoDetector")
except ImportError:
    cv2 = np = None
    HAVE_CV = False

import align_math as M

INSTALL_HINT = ("OpenCV with ArUco is missing. Install it in the app's venv:\n"
                "  pip install opencv-contrib-python-headless\n"
                "(if 'opencv-python' is already there, uninstall it first: it hides the contrib build)")

SQUARE_MM = 36.0
MARKER_MM = 27.0
WHEEL_SQUARES = (5, 7)
FLOOR_SQUARES = (7, 5)
WHEEL_IDS = list(range(0, 17))
WHEEL_R_IDS = list(range(17, 34))
FLOOR_IDS = list(range(50, 67))
MIN_ROLL_DEG = 15
MIN_CORNERS = 12


def _dict():
    return cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_100)


def board(which, square_mm=SQUARE_MM, marker_mm=None):
    marker_mm = marker_mm or square_mm * MARKER_MM / SQUARE_MM
    size, ids = {"wheel": (WHEEL_SQUARES, WHEEL_IDS), "wheel_l": (WHEEL_SQUARES, WHEEL_IDS),
                 "wheel_r": (WHEEL_SQUARES, WHEEL_R_IDS), "floor": (FLOOR_SQUARES, FLOOR_IDS)}[which]
    return cv2.aruco.CharucoBoard(size, square_mm, marker_mm, _dict(), np.array(ids, dtype=np.int32))


def target_image(which, dpi=300, paper=(215.9, 279.4)):
    """Grayscale image of one target sheet at true size for the given dpi (Letter by default), with labels.
    Returns (image, px_per_mm, (x0, y0) pixel of the board's top-left corner)."""
    b = board(which)
    is_wheel = which.startswith("wheel")
    cols, rows = WHEEL_SQUARES if is_wheel else FLOOR_SQUARES
    pw, ph = paper if is_wheel else (paper[1], paper[0])
    s = dpi / 25.4
    W, H = int(round(pw * s)), int(round(ph * s))
    sq = int(round(SQUARE_MM * s))
    bw, bh = cols * sq, rows * sq
    img = np.full((H, W), 255, np.uint8)
    x0, y0 = (W - bw) // 2, (H - bh) // 2
    img[y0:y0 + bh, x0:x0 + bw] = b.generateImage((bw, bh), marginSize=0, borderBits=1)
    s_board = sq / SQUARE_MM
    font = cv2.FONT_HERSHEY_SIMPLEX
    fs = s * 0.2
    th = max(1, int(s * 0.25))
    if is_wheel:
        side = "RIGHT (passenger)" if which == "wheel_r" else "LEFT (driver)"
        cv2.putText(img, f"{side} WHEEL TARGET: on the lug frame, facing the camera", (x0, y0 - int(4 * s)),
                    font, fs, 0, th)
        cv2.putText(img, f"5 squares = {5 * sq / s:.0f} mm (print at 100%)",
                    (x0, y0 + bh + int(8 * s)), font, fs, 0, th)
    else:
        cv2.putText(img, "FLOOR TARGET: flat on the floor (side method: THIS EDGE on the string)",
                    (x0, y0 + bh + int(8 * s)), font, fs, 0, th)
        ay = y0 - int(7 * s)
        cv2.arrowedLine(img, (x0, ay), (x0 + bw, ay), 0, max(2, int(s * 0.6)), tipLength=0.04)
        cv2.putText(img, "FRONT OF CAR", (x0 + bw // 2 - int(15 * s), ay - int(2 * s)), font, fs * 1.2, 0, th + 1)
    return img, s_board, (x0, y0)


def write_targets(out_dir, dpi=300):
    """PNG files of both targets (print at 100% / 'actual size', no 'fit to page')."""
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for which in ("wheel_l", "wheel_r", "floor"):
        img, _, _ = target_image(which, dpi)
        p = os.path.join(out_dir, f"align_{which}_target_letter_{dpi}dpi.png")
        cv2.imwrite(p, img)
        paths.append(p)
    return paths


def detect(img, which, square_mm=SQUARE_MM):
    """(object points Nx3 mm, image points Nx2) of one target in a photo, or None."""
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    b = board(which, square_mm)
    det = cv2.aruco.CharucoDetector(b)
    cc, ci, _, _ = det.detectBoard(gray)
    if ci is None or len(ci) < 4:
        return None
    op, ip = b.matchImagePoints(cc, ci)
    if op is None or len(op) < 4:
        return None
    return op.reshape(-1, 3).astype(np.float64), ip.reshape(-1, 2).astype(np.float64)


def load(path):
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"can't read image {path}")
    return img


def calibrate(paths, which="wheel"):
    """Camera intrinsics from 10+ photos of one target at different angles and places in the frame.
    Returns dict(K, dist, size, rms, used, skipped)."""
    obj, imgp, size, used, skipped = [], [], None, [], []
    for p in paths:
        img = load(p)
        if size is None:
            size = img.shape[::-1]
        elif img.shape[::-1] != size:
            skipped.append((p, "different image size"))
            continue
        d = detect(img, which)
        if d is None or len(d[0]) < MIN_CORNERS:
            skipped.append((p, "target not found" if d is None else f"only {len(d[0])} corners"))
            continue
        obj.append(d[0].astype(np.float32))
        imgp.append(d[1].astype(np.float32))
        used.append(p)
    if len(used) < 6:
        raise ValueError(f"calibration needs at least 6 good photos, got {len(used)}")
    flags = cv2.CALIB_FIX_K3 if len(used) < 12 else 0
    rms, K, dist, _, _ = cv2.calibrateCamera(obj, imgp, size, None, None, flags=flags)
    return {"K": K.tolist(), "dist": dist.ravel().tolist(), "size": list(size), "rms": float(rms),
            "used": used, "skipped": skipped, "when": time.strftime("%Y-%m-%d %H:%M")}


def save_calibration(cal, path):
    with open(path, "w") as f:
        json.dump(cal, f, indent=1)


def load_calibration(path):
    with open(path) as f:
        return json.load(f)


def pose(img, which, cal):
    """Target pose in the camera frame: dict(R 3x3, t mm, normal toward the camera, x_axis, corners, rms px) or None."""
    d = detect(img, which)
    if d is None or len(d[0]) < MIN_CORNERS:
        return None
    K, dist = np.array(cal["K"]), np.array(cal["dist"])
    op, ip = d
    ok, rvec, tvec = cv2.solvePnP(op, ip, K, dist, flags=cv2.SOLVEPNP_IPPE)
    if not ok:
        return None
    rvec, tvec = cv2.solvePnPRefineLM(op, ip, K, dist, rvec, tvec)
    R, _ = cv2.Rodrigues(rvec)
    proj, _ = cv2.projectPoints(op, rvec, tvec, K, dist)
    rms = float(np.sqrt(np.mean(np.sum((proj.reshape(-1, 2) - ip) ** 2, axis=1))))
    t = tvec.ravel()
    n = R[:, 2]
    if np.dot(n, t) > 0:
        n = -n
    return {"R": R, "t": t, "normal": n, "x_axis": R[:, 0], "corners": len(op), "rms": rms}


def measure_photo(path, wheel, cal):
    """Camber and toe-in (deg) one photo shows for one wheel, read against the floor target in the same photo.
    Returns dict(camber, toe, ...) or dict(error=...)."""
    img = load(path)
    if cal and list(img.shape[::-1]) != list(cal["size"]):
        return {"error": f"photo is {img.shape[1]}x{img.shape[0]} but the calibration is "
                         f"{cal['size'][0]}x{cal['size'][1]}: same camera settings, or calibrate again"}
    w = pose(img, "wheel", cal)
    f = pose(img, "floor", cal)
    if w is None:
        return {"error": "wheel target not found (needs 12+ corners in focus)"}
    if f is None:
        return {"error": "floor target not found (needs 12+ corners in focus)"}
    camber, toe = M.wheel_angles(tuple(w["normal"]), tuple(f["normal"]), tuple(f["x_axis"]), wheel)
    return {"camber": camber, "toe": toe, "wheel_corners": w["corners"], "floor_corners": f["corners"],
            "rms": max(w["rms"], f["rms"]), "distance_mm": float(np.linalg.norm(w["t"]))}


def spin_axis(rotations):
    """Wheel spin axis (unit vector, camera frame) from a target's rotations at 2+ roll positions of the car.
    The target is rigid on the wheel, so R_j R_0^T is a rotation about the spin axis: the axis is the vector
    those rotations leave unchanged (least squares over all pairs). How the frame sits on the lugs drops out.
    Returns (axis, [roll angle deg of each photo vs the first], residual)."""
    R0 = rotations[0]
    rows, angles = [], [0.0]
    for Rj in rotations[1:]:
        Rr = Rj @ R0.T
        rows.append(Rr - np.eye(3))
        angles.append(float(np.degrees(np.arccos(np.clip((np.trace(Rr) - 1) / 2, -1, 1)))))
    A = np.vstack(rows)
    _, sv, vt = np.linalg.svd(A)
    return vt[-1], angles, float(sv[-1])


def measure_axle(paths, cal, square_mm=SQUARE_MM):
    """Front-view reading of one axle from photos taken with the car rolled to different positions (camera, floor
    target and steering wheel kept still). Both wheel targets and the floor target must be in every photo.
    Returns dict(camber_l, camber_r, total_toe, toe_l_rel, toe_r_rel, roll angles, notes) or dict(error=...)."""
    poses, notes = [], []
    for p in paths:
        img = load(p)
        if list(img.shape[::-1]) != list(cal["size"]):
            return {"error": f"{os.path.basename(p)} is {img.shape[1]}x{img.shape[0]}, the calibration is "
                             f"{cal['size'][0]}x{cal['size'][1]}"}
        got = {w: pose(img, w, cal) for w in ("wheel_l", "wheel_r", "floor")}
        missing = [k for k, v in got.items() if v is None]
        if missing:
            notes.append(f"{os.path.basename(p)}: {', '.join(missing)} target not found, photo skipped")
            continue
        poses.append(got)
    if len(poses) < 2:
        return {"error": "need 2+ photos with all three targets, the car rolled between them", "notes": notes}
    ax_l, ang_l, res_l = spin_axis([g["wheel_l"]["R"] for g in poses])
    ax_r, ang_r, res_r = spin_axis([g["wheel_r"]["R"] for g in poses])
    if max(ang_l) < MIN_ROLL_DEG or max(ang_r) < MIN_ROLL_DEG:
        return {"error": f"the wheels turned only {max(ang_l):.0f}° / {max(ang_r):.0f}° between photos: roll the car "
                         f"further (at least {MIN_ROLL_DEG}°, about 1/8 of a turn, is better)", "notes": notes}
    up = np.mean([g["floor"]["normal"] for g in poses], axis=0)
    up /= np.linalg.norm(up)
    left = np.mean([g["wheel_l"]["t"] - g["wheel_r"]["t"] for g in poses], axis=0)
    left -= up * np.dot(left, up)
    left /= np.linalg.norm(left)
    fwd = np.cross(left, up)
    if np.dot(ax_l, left) < 0:
        ax_l = -ax_l
    if np.dot(ax_r, left) > 0:
        ax_r = -ax_r
    cam_l, toe_l = M.wheel_angles(tuple(ax_l), tuple(up), tuple(fwd), "LF")
    cam_r, toe_r = M.wheel_angles(tuple(ax_r), tuple(up), tuple(fwd), "RF")
    rms = max(max(g[w]["rms"] for w in ("wheel_l", "wheel_r", "floor")) for g in poses)
    return {"camber_l": cam_l, "camber_r": cam_r, "total_toe": toe_l + toe_r, "toe_l_rel": toe_l, "toe_r_rel": toe_r,
            "roll_l": ang_l, "roll_r": ang_r, "axis_residual": max(res_l, res_r), "rms": rms, "photos": len(poses),
            "notes": notes}


class Gphoto2Camera:
    """A DSLR over USB through the gphoto2 command (Nikon D750 works with libgphoto2).
    Camera setup: lens on MF with the focus ring taped, fixed ISO/aperture, image quality JPEG (or RAW+JPEG)."""
    name = "gphoto2"

    def __init__(self, folder):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)

    @staticmethod
    def available():
        return shutil.which("gphoto2") is not None

    def detect(self):
        if not self.available():
            return False, "gphoto2 is not installed (sudo apt install gphoto2, or sudo pacman -S gphoto2)"
        r = subprocess.run(["gphoto2", "--auto-detect"], capture_output=True, text=True, timeout=20)
        lines = [l for l in r.stdout.splitlines()[2:] if l.strip()]
        return (True, lines[0].strip()) if lines else (False, "no camera found: USB cable in, camera switched on")

    def capture(self):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        tmpl = os.path.join(self.folder, f"shot_{stamp}_%n.%C")
        cmd = ["gphoto2", "--capture-image-and-download", "--force-overwrite", "--filename", tmpl]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0 and "claim" in (r.stderr + r.stdout).lower():
            subprocess.run(["pkill", "-f", "gvfs-gphoto2-volume-monitor"], capture_output=True)
            subprocess.run(["gio", "mount", "-s", "gphoto2"], capture_output=True)
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[-400:] or "gphoto2 capture failed")
        jpgs = sorted(glob.glob(os.path.join(self.folder, f"shot_{stamp}_*.jp*g")) +
                      glob.glob(os.path.join(self.folder, f"shot_{stamp}_*.JP*G")))
        if not jpgs:
            raise RuntimeError("the camera sent no JPEG: set image quality to JPEG fine (or RAW+JPEG)")
        return jpgs[-1]


class WebcamCamera:
    """A UVC webcam through OpenCV; autofocus is switched off (focus changes the calibration)."""
    name = "webcam"

    def __init__(self, folder, index=0):
        self.folder, self.index = folder, index
        os.makedirs(folder, exist_ok=True)

    @staticmethod
    def available():
        return HAVE_CV and bool(glob.glob("/dev/video*"))

    def detect(self):
        cap = cv2.VideoCapture(self.index)
        ok = cap.isOpened()
        cap.release()
        return ok, f"/dev/video{self.index}" if ok else "no webcam"

    def capture(self):
        cap = cv2.VideoCapture(self.index)
        try:
            cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 4096)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 2160)
            for _ in range(10):
                cap.read()
            ok, frame = cap.read()
        finally:
            cap.release()
        if not ok:
            raise RuntimeError("webcam gave no frame")
        p = os.path.join(self.folder, time.strftime("cam_%Y%m%d_%H%M%S.png"))
        cv2.imwrite(p, frame)
        return p


def cli(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="alignment camera tools")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("targets", help="write printable target PNGs")
    t.add_argument("out_dir")
    t.add_argument("--dpi", type=int, default=300)
    c = sub.add_parser("calibrate", help="calibrate from photos of the WHEEL target")
    c.add_argument("out_json")
    c.add_argument("photos", nargs="+")
    m = sub.add_parser("measure", help="camber/toe from photos")
    m.add_argument("cal_json")
    m.add_argument("wheel", choices=M.WHEELS)
    m.add_argument("photos", nargs="+")
    a = ap.parse_args(argv)
    if not HAVE_CV:
        raise SystemExit(INSTALL_HINT)
    if a.cmd == "targets":
        print("\n".join(write_targets(a.out_dir, a.dpi)))
    elif a.cmd == "calibrate":
        cal = calibrate(a.photos)
        save_calibration(cal, a.out_json)
        print(f"rms {cal['rms']:.3f} px from {len(cal['used'])} photos, skipped {len(cal['skipped'])}")
    else:
        cal = load_calibration(a.cal_json)
        for p in a.photos:
            r = measure_photo(p, a.wheel, cal)
            print(os.path.basename(p), r if "error" in r else
                  f"camber {M.fmt_angle(r['camber'])}  toe-in {M.fmt_angle(r['toe'])}  rms {r['rms']:.2f}px")


if __name__ == "__main__":
    cli()
