"""
Wheel alignment geometry for the ALIGNMENT screen. Standard library only (no numpy), so it is easy to test.

Conventions (all angles in degrees):
  * up    = floor normal (from the floor target lying flat where the tires stand)
  * fwd   = the floor target's FRONT-OF-CAR arrow, set along a string line parallel to the car's centerline
  * n     = the wheel target's normal, pointing OUT of the car (toward the camera)
  * camber > 0 : top of the wheel leans out           camber = asin(-n.up)
  * toe    > 0 : toe-IN (front of the wheel points in)  toe = atan2(n.fwd, n.out), the same formula on all 4 wheels
Wheels: LF RF LR RR (left = driver side on a left-hand-drive car; the screen also names the passenger side).
"""
import math

WHEELS = ("LF", "RF", "LR", "RR")
WHEEL_NAMES = {"LF": "left front", "RF": "right front", "LR": "left rear", "RR": "right rear"}


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a):
    m = math.sqrt(dot(a, a))
    return tuple(x / m for x in a)


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def scale(a, k):
    return tuple(x * k for x in a)


def floor_frame(up, fwd):
    """Orthonormal (up, fwd, left) from the floor target; fwd is made exactly level."""
    up = norm(up)
    fwd = norm(sub(fwd, scale(up, dot(fwd, up))))
    left = cross(up, fwd)
    return up, fwd, left


def wheel_angles(n_wheel, up, fwd, wheel):
    """(camber, toe_in) in degrees for one wheel, from its outward normal and the floor frame."""
    up, fwd, left = floor_frame(up, fwd)
    n = norm(n_wheel)
    out = left if wheel in ("LF", "LR") else scale(left, -1)
    if dot(n, out) < 0:
        n = scale(n, -1)
    camber = math.degrees(math.asin(max(-1.0, min(1.0, -dot(n, up)))))
    toe = math.degrees(math.atan2(dot(n, fwd), dot(n, out)))
    return camber, toe


def string_yaw(d_front, d_rear, wheelbase, track_front=None, track_rear=None):
    """Angle (deg) the string line points OUTWARD toward the front, from string-to-hub-center distances.
    A string parallel to the centerline has d_front - d_rear = (track_rear - track_front) / 2.
    Subtract the result from every toe read against that string (positive yaw makes wheels look toed in)."""
    tf = track_front or 0.0
    tr = track_rear if track_rear is not None else tf
    return math.degrees(math.atan2((d_front + tf / 2.0) - (d_rear + tr / 2.0), wheelbase))


def thrust_angle(toe_lr, toe_rr):
    """Thrust angle (deg) from the rear wheels' toe-in, positive = thrust line points to the LEFT of the centerline
    (the car's rear tracks to the right).
    Same as the factory formula r = (alpha - beta) / 2 with alpha = right rear toe-in, beta = left rear toe-in."""
    return (toe_rr - toe_lr) / 2.0


def mm_to_deg(mm, diameter_mm):
    """Toe in mm (at the given diameter, one wheel) -> degrees."""
    return math.degrees(math.atan2(mm, diameter_mm))


def deg_to_mm(deg, diameter_mm):
    return math.tan(math.radians(deg)) * diameter_mm


def tire_diameter_mm(size):
    """'215/60R16' or 'P215/60R16' -> outside diameter in mm; '31x10.50R15' -> 31 in. None if not a tire size."""
    import re
    s = (size or "").upper().replace(" ", "")
    m = re.search(r"(\d{3})/(\d{2})Z?R?-?(\d{2}(?:\.5)?)", s)
    if m:
        w, ar, rim = int(m.group(1)), int(m.group(2)), float(m.group(3))
        return 2 * w * ar / 100.0 + rim * 25.4
    m = re.search(r"(\d{2}(?:\.\d)?)X\d{1,2}(?:\.\d+)?R?(\d{2})", s)
    if m:
        return float(m.group(1)) * 25.4
    return None


def fmt_angle(deg, signed=True):
    """-0.25 -> "-0°15'" (degrees and minutes, the way manuals print them) plus decimal."""
    sign = "-" if deg < 0 else ("+" if signed and deg > 0 else "")
    a = abs(deg)
    d = int(a)
    m = round((a - d) * 60)
    if m == 60:
        d, m = d + 1, 0
    return f"{sign}{d}°{m:02d}' ({deg:+.2f}°)" if signed else f"{sign}{d}°{m:02d}' ({deg:.2f}°)"


def fmt_turns(t, step=0.125):
    """3.27 -> '3 1/4' (rounded to the nearest 1/8 by default)."""
    from fractions import Fraction
    q = round(abs(t) / step) * step
    whole = int(q)
    frac = Fraction(q - whole).limit_denominator(16)
    if q == 0:
        return "0"
    if frac == 0:
        return f"{whole}"
    return f"{frac}" if whole == 0 else f"{whole} {frac}"


def correction(measured, target_center, tol=None):
    """Change needed (deg) to reach the spec's center, and whether the reading is already in tolerance."""
    delta = target_center - measured
    ok = None if tol is None else abs(delta) <= tol
    return delta, ok


def adjuster_moves(delta_deg, rate_deg_per_unit, step=0.125):
    """How many adjuster units (turns, graduations...) to move. rate is signed: positive = one unit in the
    adjuster's '+' direction INCREASES the angle. Returns (units signed, rounded units, sign word)."""
    if not rate_deg_per_unit:
        return None
    units = delta_deg / rate_deg_per_unit
    r = round(units / step) * step
    return units, r, ("+" if r > 0 else "-" if r < 0 else "")


def learned_rate(before_deg, after_deg, units_moved):
    """Rate (deg per unit) from a test move: units_moved in the '+' direction, angle went before -> after."""
    if not units_moved:
        raise ValueError("test move of 0 units")
    return (after_deg - before_deg) / units_moved


def rate_from_pitch(pitch_mm, arm_mm, threads=1):
    """Estimate: toe change (deg, one wheel) per turn of a tie rod from thread pitch and steering-arm length
    (steering axis to the outer tie-rod ball-joint center). threads=2 for a left/right-hand sleeve.
    Sign unknown: it depends on which side of the axle the tie rod sits; confirm with a test turn."""
    return math.degrees(math.atan2(pitch_mm * threads, arm_mm))


def mean_and_spread(values):
    """Mean and half the max-min range (for 'reading is steady to within +-x')."""
    v = list(values)
    if not v:
        return None, None
    return sum(v) / len(v), (max(v) - min(v)) / 2.0
