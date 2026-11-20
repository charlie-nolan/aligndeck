"""
One alignment job, without any GUI: car, specs, camera calibration, readings, adjuster rates and the move plan.
The web screen (server.py) calls these methods and shows what they return. Saved per car in
<data>/<make_model_year>.json (data folder: ~/.local/share/aligndeck or $ALIGNDECK_HOME).

Rules: every spec, rate and direction names its manual source; a value the manual doesn't
give is typed by the user and marked as theirs; turns come from a rate learned on the car with a test move, or the
manual's own rate once the user picks it, or thread pitch + steering-arm length (marked as an estimate).
"""
import json
import os
import re
import sqlite3
import threading
import time

import align_math as M
import align_specs
import align_vision as V

DATA = os.path.expanduser(os.environ.get("ALIGNDECK_HOME", "~/.local/share/aligndeck"))
QTY_ROWS = [("camber", "front"), ("camber", "rear"), ("toe", "front"), ("toe", "rear")]
SIDE_WORD = {"LF": "LEFT FRONT (driver)", "RF": "RIGHT FRONT (passenger)", "LR": "LEFT REAR (driver)",
             "RR": "RIGHT REAR (passenger)"}
FIELDS = ("tire", "diameter", "wheelbase", "track_f", "track_r", "pitch", "arm", "threads",
          "str_left_front", "str_left_rear", "str_right_front", "str_right_rear")


def fmt_spec(s):
    """One spec as the manual states it, e.g. "-0°15' ±0°30' each" or '0 ±3 mm total'."""
    if s["unit"] == "mm":
        tol = f" ±{s['tol_hi']:g}" if s["tol_lo"] == -s["tol_hi"] else f" {s['tol_lo']:+g}/{s['tol_hi']:+g}"
        return f"{s['center']:g}{tol} mm {s['basis'] or '?'}"
    c = M.fmt_angle(s["center"]).split(" (")[0]
    if s["tol_lo"] == -s["tol_hi"]:
        tol = " ±" + M.fmt_angle(s["tol_hi"], signed=False).split(" (")[0]
    else:
        tol = f" {M.fmt_angle(s['tol_lo']).split(' (')[0]}/{M.fmt_angle(s['tol_hi']).split(' (')[0]}"
    return f"{c}{tol} {s['basis'] or '?'}"


def _num(x):
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return None


class Session:
    def __init__(self, make="", model="", year="", kb_db="", cache_dir=None, kb_dir=""):
        self.cache = cache_dir or DATA
        self.kb_dir = kb_dir
        os.makedirs(self.cache, exist_ok=True)
        self.lock = threading.RLock()
        self.make, self.model, self.year = make, model, str(year or "")
        self.kb_db = kb_db
        self.found = None
        self.specs_msg = "Pick the car, then load its specs. Each spec shows the manual line it came from."
        self.state = {}
        self.readings = {}
        self.fields = {k: "" for k in FIELDS}
        self.fields["threads"] = "1"
        self.spec_rows = {}
        self.cands = {}
        self.log = []
        self.source = "gphoto2"
        self.cal = None
        self.cal_photos = []
        self.axle = "front"
        self.axle_photos = []
        self.pending_learn = None
        self.calib = None
        self.adj = {"wheel": "LF", "qty": "toe"}
        self._load_state()
        self.load_calibration()

    def say(self, text, col="text"):
        with self.lock:
            self.log.append((col, f"{time.strftime('%H:%M:%S')}  {text}"))
            del self.log[:-300]

    def car_key(self):
        return "_".join(x.strip().lower().replace(" ", "-") for x in (self.make, self.model, self.year)) or "car"

    def car_label(self):
        return " ".join(x for x in (self.year, self.make, self.model) if x) or "no car picked"

    def _state_path(self):
        return os.path.join(self.cache, f"{self.car_key()}.json")

    def _load_state(self):
        try:
            with open(self._state_path()) as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = {}
        for k, v in self.state.get("fields", {}).items():
            if k in self.fields:
                self.fields[k] = str(v)
        self.readings = self.state.get("readings", {})

    def save_state(self):
        with self.lock:
            self.state["fields"] = dict(self.fields)
            self.state["readings"] = self.readings
            self.state["specs"] = {k: {"typed": r["choice"] < 0, "center": r["center"], "tol": r["tol"],
                                       "unit": r["unit"], "basis": r["basis"]} for k, r in self.spec_rows.items()}
            with open(self._state_path(), "w") as f:
                json.dump(self.state, f, indent=1)

    def set_fields(self, **kw):
        with self.lock:
            for k, v in kw.items():
                if k in self.fields:
                    self.fields[k] = str(v)
            if "tire" in kw:
                d = M.tire_diameter_mm(self.fields["tire"])
                if d and not (self.found and self.found["diameters"]):
                    self.fields["diameter"] = f"{d:.0f}"
            self.save_state()

    def _db(self):
        """Optional spec database: --kb FILE, or --kb-dir DIR holding <make>.db (the make databases of the Open
        Diagnostic Cyberdeck use this layout). Without one, specs are typed in on SPECS."""
        path = self.kb_db
        if (not path or not os.path.exists(path)) and self.kb_dir:
            path = os.path.join(os.path.expanduser(self.kb_dir), self.make.strip().lower().replace(" ", "_") + ".db")
        if not path or not os.path.exists(path):
            raise FileNotFoundError("no spec database for this make: type the specs in (or start with --kb / --kb-dir)")
        self.kb_db = path
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)

    def set_car(self, make, model, year):
        with self.lock:
            if make.strip() != self.make:
                self.kb_db = ""
            self.make, self.model, self.year = make.strip(), model.strip(), str(year).strip()
            self.found = None
            self.spec_rows, self.cands = {}, {}
            self.fields = {k: "" for k in FIELDS}
            self.fields["threads"] = "1"
            self._load_state()

    def load_specs(self):
        db = self._db()
        try:
            found = align_specs.lookup(db, self.model, int(self.year) if self.year.isdigit() else None)
        finally:
            db.close()
        with self.lock:
            self.found = found
            if not found["vehicle_ids"]:
                self.specs_msg = (f"{self.model} {self.year} is not in {os.path.basename(self.kb_db)}. Type the specs "
                                  "from the manual.")
            else:
                n = sum(1 for s in found["specs"] if s["plausible"])
                self.specs_msg = (f"{n} spec lines read from {os.path.basename(self.kb_db)}. A spec is pre-picked only "
                                  "when the manual gives one reading for it; otherwise pick the line for this car or "
                                  "type it.")
            saved = self.state.get("specs", {})
            for qty, axle in QTY_ROWS:
                key = f"{qty}|{axle}"
                cands = [s for s in found["specs"] if (s["quantity"], s["axle"]) == (qty, axle) and s["plausible"]]
                cands.sort(key=lambda s: (not s["pick"], s["unit"] != "deg", "CHARM" in s["source"]))
                self.cands[key] = cands
                sv = saved.get(key)
                if sv and sv.get("typed"):
                    self.spec_rows[key] = {"choice": -1, "center": sv.get("center", ""), "tol": sv.get("tol", ""),
                                           "unit": sv.get("unit", "deg"), "basis": sv.get("basis", "each")}
                else:
                    self.spec_rows[key] = {"choice": -1, "center": "", "tol": "", "unit": "deg", "basis": "each"}
                    if cands and cands[0]["pick"]:
                        self.pick_spec(key, 0, save=False)
            if found["diameters"] and not self.fields["diameter"]:
                self.fields["diameter"] = f"{found['diameters'][0]['diameter_mm']:.0f}"
            self.save_state()

    def pick_spec(self, key, idx, save=True):
        """idx into the manual lines for this row; -1 = type it myself."""
        with self.lock:
            row = self.spec_rows.setdefault(key, {"choice": -1, "center": "", "tol": "", "unit": "deg",
                                                  "basis": "each"})
            cands = self.cands.get(key, [])
            if idx < 0 or idx >= len(cands):
                row["choice"] = -1
            else:
                s = cands[idx]
                row.update(choice=idx, unit=s["unit"],
                           center=f"{s['center']:g}" if s["unit"] == "mm" else f"{s['center']:.4f}",
                           tol=f"{s['tol_hi']:g}" if s["unit"] == "mm" else f"{s['tol_hi']:.4f}",
                           basis=s["basis"] if s["basis"] in ("each", "total") else "each")
            if save:
                self.save_state()

    def type_spec(self, key, center, tol, unit, basis):
        """An edited value is the user's own, not the manual's."""
        with self.lock:
            self.spec_rows[key] = {"choice": -1, "center": str(center), "tol": str(tol),
                                   "unit": unit if unit in ("deg", "mm") else "deg",
                                   "basis": basis if basis in ("each", "total") else "each"}
            self.save_state()

    def spec_source(self, key):
        row = self.spec_rows.get(key)
        if not row:
            return ""
        if row["choice"] < 0:
            return "typed by you" if row["center"] else \
                "no spec picked: type it from the manual, or this quantity is shown without a verdict"
        s = self.cands[key][row["choice"]]
        note = f" ({s['note']})" if s.get("note") else ""
        warn = "" if s["basis"] else "  The manual line doesn't say each or total: check it and set Basis."
        return f"\"{s['raw'][:140]}\"{note} [{s['source']}]{warn}"

    def target(self, qty, wheel):
        """(center, tol) in degrees for one wheel, or (None, why)."""
        axle = "front" if wheel.endswith("F") else "rear"
        row = self.spec_rows.get(f"{qty}|{axle}")
        c, t = (_num(row["center"]), _num(row["tol"])) if row else (None, None)
        if c is None or t is None:
            return None, "no spec"
        if row["unit"] == "mm":
            d = _num(self.fields["diameter"])
            if d is None:
                return None, "toe spec is in mm: enter the toe diameter on SPECS"
            k = 2.0 if row["basis"] == "total" else 1.0
            return (M.mm_to_deg(c / k, d), M.mm_to_deg(t / k, d)), ""
        k = 2.0 if (qty == "toe" and row["basis"] == "total") else 1.0
        return (c / k, t / k), ""

    def manual_lines(self):
        f = self.found
        if not f:
            return []
        out = []
        if f["rates"]:
            out.append(("head", "ADJUSTER RATES IN THE MANUAL"))
            for r in f["rates"]:
                out.append(("text", f"• {r['quantity']}: one {r['unit']} = {r['value']:.3g} {r['value_unit']}"
                                    f"{' (left and right moved together)' if r['both_sides'] else ''}: "
                                    f"\"{r['raw'][:220]}\""))
                out.append(("src", f"  [{r['source']}]"))
        if f["directions"]:
            out.append(("head", "WHICH WAY TO TURN (manual's words)"))
            for d in f["directions"]:
                out.append(("text", f"• {d['quantity']}: \"{d['raw'][:260]}\""))
                out.append(("src", f"  [{d['source']}]"))
        if f["pitches"]:
            out.append(("head", "THREAD SIZES NAMED NEAR TIE RODS"))
            for p in f["pitches"]:
                out.append(("text", f"• pitch {p['pitch_mm']:g} mm: \"{p['raw'][:200]}\""))
                out.append(("src", f"  [{p['source']}]"))
        else:
            out.append(("muted", "No tie-rod thread pitch found in this car's manual pages. A rate learned with a "
                                 "test move on the car (ADJUST) needs none."))
        if f["pages"]:
            out.append(("head", "ALIGNMENT PAGES READ"))
            for p in f["pages"][:30]:
                out.append(("src", f"• {p['source']}" + ("  (bulletin: read it yourself, not used for specs)"
                                                         if p["tsb"] else "")))
        return out

    def cal_path(self, source=None):
        return os.path.join(self.cache, f"camera_{source or self.source}.json")

    def load_calibration(self):
        self.cal = None
        for src in ([self.source] + (["gphoto2"] if self.source == "files" else [])):
            try:
                self.cal = V.load_calibration(self.cal_path(src))
                break
            except (OSError, ValueError):
                pass

    def cal_status(self):
        if self.cal:
            c = self.cal
            return (f"Calibrated {c['when']}: {len(c['used'])} photos, {c['size'][0]}x{c['size'][1]}, error "
                    f"{c['rms']:.2f} px ({'good' if c['rms'] < 0.8 else 'high: redo with sharper photos'}). "
                    f"New photos for a recalibration: {len(self.cal_photos)}.")
        return (f"Not calibrated. Photos so far: {len(self.cal_photos)} (12-20 is good, 6 minimum). Hold the LEFT "
                "wheel target at different angles (up to ~40°) and in every part of the frame, corners too, at about "
                "the distance you'll shoot the wheels from.")

    def add_cal_photos(self, paths):
        for p in paths:
            d = V.detect(V.load(p), "wheel")
            ok = d is not None and len(d[0]) >= V.MIN_CORNERS
            self.say(f"{os.path.basename(p)}: " + (f"{len(d[0])} corners" if ok else "target not found, not used"),
                     "text" if ok else "amber")
            if ok:
                self.cal_photos.append(p)

    def solve_calibration(self):
        if len(self.cal_photos) < 6:
            raise ValueError(f"need at least 6 calibration photos, have {len(self.cal_photos)}")
        cal = V.calibrate(list(self.cal_photos))
        cal["camera"] = self.source
        V.save_calibration(cal, self.cal_path())
        self.cal, self.cal_photos = cal, []
        self.say(f"calibration solved: {cal['rms']:.2f} px from {len(cal['used'])} photos", "ok")

    def axle_reset(self, axle=None):
        if axle in ("front", "rear"):
            self.axle = axle
        self.axle_photos = []

    def axle_hint(self):
        n = len(self.axle_photos)
        if self.calib:
            return self.calib_hint()
        if self.pending_learn:
            return (f"TEST MOVE on {SIDE_WORD[self.pending_learn[0]]}: take the roll photos of this axle again "
                    f"(F7 each, {n} so far), then F8. The change on that side teaches the rate.")
        if not n:
            return (f"{self.axle.upper()} AXLE: steering wheel centered and held (front), targets on both lug frames "
                    "facing the camera, floor target flat between camera and car, tripod still. F7 = photo; roll the "
                    "car about a foot (1/8 wheel turn); F7 again; 3 photos (back, middle, forward) is best. Then F8.")
        return (f"{self.axle.upper()} AXLE: {n} photo(s). Roll the car about a foot and press F7 again"
                + (", or F8 to compute." if n >= 2 else "."))

    def compute_axle(self):
        if len(self.axle_photos) < 2:
            raise ValueError("need 2+ photos with the car rolled between them")
        if not self.cal:
            raise ValueError("calibrate the camera first (CAMERA)")
        axle, paths = self.axle, list(self.axle_photos)
        r = V.measure_axle(paths, self.cal)
        for n in r.get("notes", []):
            self.say(n, "amber")
        if "error" in r:
            raise ValueError(r["error"])
        wl, wr = ("LF", "RF") if axle == "front" else ("LR", "RR")
        half = r["total_toe"] / 2
        when = time.strftime("%H:%M:%S")
        with self.lock:
            for wheel, cam, rel in ((wl, r["camber_l"], r["toe_l_rel"]), (wr, r["camber_r"], r["toe_r_rel"])):
                prev = self.readings.get(wheel) or {}
                single = {"camber": cam, "toe": rel, "total": r["total_toe"]}
                self.readings[wheel] = {"camber": cam, "toe": half, "spread_c": 0.0, "spread_t": 0.0,
                                        "n": r["photos"], "when": when, "yaw": 0.0, "single": single, "first": single,
                                        "method": "front view", "total_toe": r["total_toe"]}
                pend = self.pending_learn
                if pend and pend[0] == wheel and prev.get("single"):
                    self.pending_learn = None
                    old, new = dict(prev["single"]), dict(single)
                    if pend[1] == "toe" and "total" in old:
                        old["toe"], new["toe"] = old["total"], new["total"]
                    self._learn(pend, old, new)
            self.say(f"{axle.upper()} AXLE ({r['photos']} photos, wheels turned up to {max(r['roll_l']):.0f}°): total "
                     f"toe-in {M.fmt_angle(r['total_toe'])} = {M.fmt_angle(half)} per side; camber left "
                     f"{M.fmt_angle(r['camber_l'])}, right {M.fmt_angle(r['camber_r'])}; worst reprojection "
                     f"{r['rms']:.2f} px", "ok")
            self.say(f"each side vs the line between the targets: left {r['toe_l_rel']:+.3f}°, right "
                     f"{r['toe_r_rel']:+.3f}°" + (". A big difference with the steering wheel straight means it is "
                                                  "off-center: set both sides to half the total; turning both tie "
                                                  "rods the same way moves the steering wheel." if axle == "front"
                                                  else " (thrust angle needs the side view with strings)."), "muted")
            self.axle_photos = []
            if self.calib and self.calib["axle"] == axle:
                self._calib_step(r["total_toe"])
            self.save_state()

    def string_correction(self, wheel):
        side = "left" if wheel in ("LF", "LR") else "right"
        df, dr = _num(self.fields[f"str_{side}_front"]), _num(self.fields[f"str_{side}_rear"])
        wb = _num(self.fields["wheelbase"])
        if None in (df, dr, wb):
            return 0.0, (f"no string distances for the {side} side: toe is relative to the floor target as placed "
                         "(right only if its edge is exactly parallel to the car)")
        tf, tr = _num(self.fields["track_f"]), _num(self.fields["track_r"])
        note = "" if (tf is not None and tr is not None) else " (front and rear track treated as equal)"
        return M.string_yaw(df, dr, wb, tf, tr), note

    def measure_side(self, wheel, paths, flipped, learn=None):
        if not self.cal:
            raise ValueError("calibrate the camera first (CAMERA)")
        res = [V.measure_photo(p, wheel, self.cal) for p in paths]
        good = [r for r in res if "error" not in r]
        for p, r in zip(paths, res):
            if "error" in r:
                self.say(f"{os.path.basename(p)}: {r['error']}", "amber")
        if not good:
            raise ValueError("no usable photo: see the log")
        yaw, note = self.string_correction(wheel)
        cam, sc = M.mean_and_spread(r["camber"] for r in good)
        toe, st = M.mean_and_spread(r["toe"] - yaw for r in good)
        with self.lock:
            prev = self.readings.get(wheel) or {}
            single = {"camber": cam, "toe": toe}
            entry = {"camber": cam, "toe": toe, "spread_c": sc, "spread_t": st, "n": len(good),
                     "when": time.strftime("%H:%M:%S"), "yaw": yaw, "single": single, "first": single}
            if flipped and prev.get("first") and not prev.get("flip_pair") and not prev.get("method"):
                a = prev["first"]
                entry.update(camber=(a["camber"] + cam) / 2, toe=(a["toe"] + toe) / 2, flip_pair=True, first=a,
                             clamp_error_c=(cam - a["camber"]) / 2, clamp_error_t=(toe - a["toe"]) / 2)
            elif flipped:
                self.say("(F6 needs an F5 reading of this wheel just before it: kept as a plain reading)", "amber")
            self.readings[wheel] = entry
            self.say(f"{SIDE_WORD[wheel]}: camber {M.fmt_angle(cam)} ±{sc:.3f}, toe-in {M.fmt_angle(toe)} ±{st:.3f} "
                     f"from {len(good)} photo(s), worst reprojection {max(r['rms'] for r in good):.2f} px"
                     + (f"; string angle {yaw:+.3f}° removed" if yaw else "") + (f"; {note}" if note else ""), "ok")
            if entry.get("flip_pair"):
                self.say(f"flip pair averaged: clamp tilt was {entry['clamp_error_c']:+.3f}° camber, "
                         f"{entry['clamp_error_t']:+.3f}° toe")
            if max(sc, st) > 0.05:
                self.say("photos disagree by more than 0.05°: check the tripod, focus and targets", "amber")
            if learn and prev.get("single"):
                self._learn(learn, prev["single"], single)
            self.save_state()

    def calib_start(self, axle, side, units, unit, plus):
        """Photos as it is, turn ONE side's toe adjuster a known amount, photos again: the change in total toe is
        that side's degrees per turn (or graduation), measured on this car. Used for both sides (mirror parts)."""
        units = float(units)
        if not units:
            raise ValueError("turn amount can't be 0")
        if not self.cal:
            raise ValueError("calibrate the camera first (CAMERA)")
        wheel = ("L" if side == "left" else "R") + ("F" if axle == "front" else "R")
        self.calib = {"step": 1, "axle": axle, "wheel": wheel, "step_units": units, "units": units,
                      "unit": unit or "turn", "plus": plus.strip() or "clockwise viewed from inside the car"}
        self.pending_learn = None
        self.axle_reset(axle)
        self.say(f"TOE ADJUSTER CALIBRATION started on {SIDE_WORD[wheel]}", "ok")

    def calib_cancel(self):
        self.calib = None

    def calib_hint(self):
        c = self.calib
        if not c:
            return ""
        n = len(self.axle_photos)
        what = "tie rod" if c["axle"] == "front" else "toe adjuster"
        if c["step"] == 1:
            return (f"Step 1 of 2: don't touch anything yet. Take the roll photos of the {c['axle']} axle as it is now "
                    f"(F7, roll the car about a foot, F7 again; 3 photos is best), then F8. Photos so far: {n}.")
        side = SIDE_WORD[c["wheel"]]
        return (f"Step 2 of 2: put a tape flag on the {side} {what} so you can count, loosen its lock nut, and turn it "
                f"{M.fmt_turns(c['step_units'])} {c['unit']} {c['plus']}. Leave the targets, frames and tripod alone. "
                f"Then the roll photos again (F7 x2-3) and F8. Photos so far: {n}.")

    def _calib_step(self, total):
        c = self.calib
        if c["step"] == 1:
            c.update(step=2, before=total)
            self.say(f"Calibration step 1 done: total toe-in {M.fmt_angle(total)}.", "ok")
            return
        change = total - c["before"]
        if abs(change) < 0.05:
            c["units"] += c["step_units"]
            self.say(f"Turning {M.fmt_turns(c['units'] - c['step_units'])} {c['unit']} changed toe only "
                     f"{change:+.3f}°, which is too close to the reading noise. Turn it another "
                     f"{M.fmt_turns(c['step_units'])} {c['unit']} the same way, then photos and F8 again.", "amber")
            return
        rate = change / c["units"]
        wheel = c["wheel"]
        other = {"LF": "RF", "RF": "LF", "LR": "RR", "RR": "LR"}[wheel]
        when = time.strftime("%Y-%m-%d %H:%M")
        for w in (wheel, other):
            self.state.setdefault("learned", {})[f"{w}|toe"] = {
                "rate": rate, "unit": c["unit"], "when": when, "before": c["before"], "after": total,
                "units": c["units"], "measured_on": wheel}
            self.state.setdefault("rate_choice", {})[f"{w}|toe"] = "learned"
            self.state.setdefault("plus", {})[f"{w}|toe"] = c["plus"]
        self.calib = None
        self.say(f"Calibrated: {M.fmt_turns(c['units'])} {c['unit']} {c['plus']} on the {SIDE_WORD[wheel]} changed "
                 f"total toe-in {c['before']:+.3f}° -> {total:+.3f}° = {abs(rate):.3f}° per {c['unit']} "
                 f"({'more' if rate > 0 else 'less'} toe-in that way). Used for the {SIDE_WORD[other]} too (mirror "
                 f"part). The move list is on ADJUST.", "ok")
        if abs(rate) > 3:
            self.say("That's a lot of toe per turn for a tie rod: check the turn count and that nothing else moved.",
                     "amber")

    def verdict(self, qty, wheel, val):
        tgt, why = self.target(qty, wheel)
        if tgt is None:
            return "-", why, None
        c, t = tgt
        ok = abs(val - c) <= t + 1e-9
        return (M.fmt_angle(c).split(" (")[0] + " ±" + M.fmt_angle(t, signed=False).split(" (")[0],
                "in spec" if ok else "OUT", ok)

    def results(self):
        rows = []
        for wheel in M.WHEELS:
            r = self.readings.get(wheel)
            if not r:
                continue
            cs, cv, cok = self.verdict("camber", wheel, r["camber"])
            ts, tv, tok = self.verdict("toe", wheel, r["toe"])
            rows.append({"wheel": SIDE_WORD[wheel], "camber": M.fmt_angle(r["camber"]), "camber_spec": cs,
                         "toe": M.fmt_angle(r["toe"]), "toe_spec": ts, "verdict": f"camber {cv} / toe {tv}",
                         "bad": (cok is False) or (tok is False),
                         "reading": f"{r['when']}, {r['n']} photo(s)" + (", flip pair" if r.get("flip_pair") else "")
                         + (f", front view: half of total {r['total_toe']:+.3f}°" if r.get("method") else "")})
        if all(w in self.readings and not self.readings[w].get("method") for w in ("LR", "RR")):
            th = M.thrust_angle(self.readings["LR"]["toe"], self.readings["RR"]["toe"])
            rows.append({"wheel": "Thrust angle", "camber": "", "camber_spec": "", "toe": M.fmt_angle(th),
                         "toe_spec": "", "verdict": "+ = rear tracks right", "bad": False, "reading": ""})
        return rows

    def rate_options(self, wheel, qty):
        """[{label, rate (deg per unit) or None, sign_known, unit, source}] for one adjuster."""
        opts = []
        learned = self.state.get("learned", {}).get(f"{wheel}|{qty}")
        if learned:
            opts.append({"label": f"learned on this car: {learned['rate']:+.4f}°/{learned['unit']} ({learned['when']})",
                         "rate": learned["rate"], "sign_known": True, "unit": learned["unit"],
                         "source": "test move on the car", "id": "learned"})
        axle_word = "front" if wheel.endswith("F") else "rear"
        for i, r in enumerate((self.found or {}).get("rates", [])):
            if r["quantity"] != qty:
                continue
            per = r["value"] / (2.0 if r["both_sides"] else 1.0)
            if r["value_unit"] == "mm":
                d = _num(self.fields["diameter"])
                if d is None:
                    continue
                deg = M.mm_to_deg(per, d)
            else:
                deg = per
            sign = self._manual_sign(qty, r, wheel)
            opts.append({"label": f"manual: one {r['unit']} = {r['value']:.3g} {r['value_unit']}"
                                  f"{' (both sides together)' if r['both_sides'] else ''} -> {deg:.3f}°/{r['unit']} "
                                  f"per wheel{'' if sign else ', direction from a test move'} [check it's the "
                                  f"{axle_word} adjuster]",
                         "rate": deg * (sign or 1), "sign_known": bool(sign), "unit": r["unit"],
                         "source": f"\"{r['raw'][:160]}\" [{r['source']}]", "id": f"manual{i}"})
        p, a = _num(self.fields["pitch"]), _num(self.fields["arm"])
        if p and a:
            th = 2 if self.fields.get("threads") == "2" else 1
            pr = M.rate_from_pitch(p, a, th)
            opts.append({"label": f"estimate from pitch {p:g} mm / arm {a:g} mm: {pr:.3f}°/turn, direction from a "
                                  "test move", "rate": pr, "sign_known": False, "unit": "turn",
                         "source": "your pitch + arm length", "id": "pitch"})
        opts.append({"label": "no rate yet: do a test move", "rate": None, "sign_known": False, "unit": "turn",
                     "source": "", "id": "none"})
        return opts

    def _manual_sign(self, qty, rate, wheel):
        """+1 when the manual says turning the adjuster clockwise (from the side it names) increases the angle,
        -1 for decreases, None when it doesn't say plainly. Only tie-rod/rack-end turns match a turn rate."""
        for d in (self.found or {}).get("direction_rules", []):
            if d["quantity"] != qty or rate["unit"] != "turn":
                continue
            if not re.search(r"tie[- ]?rod|rack end|adjusting tube", d["raw"], re.I):
                continue
            s = (1 if d["change"] == "increase" else -1) * (1 if d["turn"] == "cw" else -1)
            plus = f"clockwise viewed from the {d['view'] or '(side the manual names)'} of the car"
            self.state.setdefault("plus", {}).setdefault(f"{wheel}|{qty}", plus)
            return s
        return None

    def choice(self, wheel, qty, opts=None):
        """The rate the user picked for this adjuster (or the learned one); None = no rate yet. A manual rate is
        used only once picked, because the manual's rate may belong to another axle's adjuster."""
        opts = opts if opts is not None else self.rate_options(wheel, qty)
        want = self.state.get("rate_choice", {}).get(f"{wheel}|{qty}")
        for o in opts:
            if o["rate"] is not None and o["id"] == want:
                return o
        return next((o for o in opts if o["id"] == "learned"), None)

    def pick_rate(self, wheel, qty, opt_id):
        self.state.setdefault("rate_choice", {})[f"{wheel}|{qty}"] = opt_id
        self.save_state()

    def set_plus(self, wheel, qty, text):
        self.state.setdefault("plus", {})[f"{wheel}|{qty}"] = text
        self.save_state()

    def start_test_move(self, wheel, qty, units):
        """Front view: arm the learner for the next axle compute. Returns the learner for a side-view reading."""
        if not (self.readings.get(wheel) or {}).get("single"):
            raise ValueError("measure this wheel first, move the adjuster a known amount, then do the test move. "
                             "Don't touch the targets or tripod in between.")
        opt = self.choice(wheel, qty) or {"unit": "turn"}
        learn = (wheel, qty, float(units), opt["unit"])
        if (self.readings[wheel] or {}).get("method"):
            self.pending_learn = learn
            self.axle_reset("front" if wheel.endswith("F") else "rear")
        return learn

    def _learn(self, learn, old, new):
        wheel, qty, units, unit = learn
        rate = M.learned_rate(old[qty], new[qty], units)
        self.state.setdefault("learned", {})[f"{wheel}|{qty}"] = {
            "rate": rate, "unit": unit, "when": time.strftime("%Y-%m-%d %H:%M"), "before": old[qty],
            "after": new[qty], "units": units}
        self.state.setdefault("rate_choice", {})[f"{wheel}|{qty}"] = "learned"
        self.state.setdefault("plus", {}).setdefault(f"{wheel}|{qty}", "the same way as your test move")
        self.say(f"Learned {SIDE_WORD[wheel]} {qty}: {units:+g} {unit} moved it {old[qty]:+.3f}° -> {new[qty]:+.3f}° "
                 f"= {rate:+.4f}° per {unit}.", "ok")
        if abs(new[qty] - old[qty]) < 0.03:
            self.say("that change is close to the reading noise: use a bigger test move", "amber")

    def plan(self):
        out = [("head", "WHAT TO MOVE (center of the spec is the goal)")]
        any_line = False
        for wheel in M.WHEELS:
            r = self.readings.get(wheel)
            if not r:
                continue
            for qty in ("toe", "camber"):
                tgt, why = self.target(qty, wheel)
                if tgt is None:
                    out.append(("grey", f"{SIDE_WORD[wheel]} {qty}: {why}"))
                    continue
                delta, ok = M.correction(r[qty], tgt[0], tgt[1])
                opt = self.choice(wheel, qty)
                out.append(("ok" if ok else "amber", f"{SIDE_WORD[wheel]} {qty}: now {M.fmt_angle(r[qty])}, goal "
                            f"{M.fmt_angle(tgt[0])} -> change {delta:+.3f}° "
                            f"({'already in spec' if ok else 'OUT of spec'})"))
                any_line = True
                if not opt:
                    out.append(("muted", "   no rate picked: pick the manual's rate for this adjuster, or move it a "
                                         "known amount (e.g. 1/4 turn) and do a TEST MOVE"))
                    continue
                units, rounded, s = M.adjuster_moves(delta, opt["rate"])
                unit = opt["unit"]
                plus = self.state.get("plus", {}).get(f"{wheel}|{qty}", "") or ("+ direction" if opt["sign_known"]
                                                                                else "")
                many = "s" if abs(rounded or 0) > 1 else ""
                if not rounded:
                    out.append(("text", f"   less than 1/8 {unit}: leave it"))
                elif opt["sign_known"]:
                    way = plus if s == "+" else f"opposite of: {plus}"
                    out.append(("text", f"   move {M.fmt_turns(rounded)} {unit}{many} ({abs(units):.2f}) {way}"))
                else:
                    out.append(("text", f"   move {M.fmt_turns(rounded)} {unit}{many} ({abs(units):.2f}); direction "
                                        f"unknown: move 1/4 {unit} either way, TEST MOVE, and the screen will say "
                                        "which way"))
                if opt["source"]:
                    out.append(("src", f"   rate source: {opt['source']}"))
        if not any_line:
            out.append(("muted", "No readings yet: measure the wheels first."))
        out.append(("muted", "Re-measure after every move; tighten lock nuts to the manual's torque and measure once "
                             "more. This is a home tool: confirm with a shop alignment before trusting it for tire "
                             "wear."))
        return out

    def snapshot(self):
        with self.lock:
            specs = []
            for qty, axle in QTY_ROWS:
                key = f"{qty}|{axle}"
                row = self.spec_rows.get(key) or {"choice": -1, "center": "", "tol": "", "unit": "deg",
                                                  "basis": "each"}
                specs.append({"key": key, "label": f"{axle.capitalize()} {qty}", **row,
                              "options": [f"{fmt_spec(s)}   [{s['raw'][:60]}]" for s in self.cands.get(key, [])],
                              "source": self.spec_source(key)})
            w, q = self.adj["wheel"], self.adj["qty"]
            opts = self.rate_options(w, q)
            ch = self.choice(w, q, opts)
            return {
                "car": {"make": self.make, "model": self.model, "year": self.year, "label": self.car_label(),
                        "db": os.path.basename(self.kb_db or "")},
                "specs_msg": self.specs_msg, "specs": specs, "fields": self.fields,
                "diameter_note": (f"{self.found['diameters'][0]['diameter_mm']:.0f} mm is what the manual's own toe "
                                  f"line implies: \"{self.found['diameters'][0]['raw'][:80]}\" "
                                  f"[{self.found['diameters'][0]['source']}]" if self.found and self.found["diameters"]
                                  else "Toe in mm needs the diameter it's measured across (tire at hub height)."),
                "manual": self.manual_lines(),
                "camera": {"source": self.source, "cal": self.cal_status(), "calibrated": bool(self.cal),
                           "have_cv": V.HAVE_CV, "install_hint": V.INSTALL_HINT if not V.HAVE_CV else ""},
                "axle": {"axle": self.axle, "photos": [os.path.basename(p) for p in self.axle_photos],
                         "hint": self.axle_hint()},
                "results": self.results(),
                "calib": {"active": bool(self.calib), "hint": self.calib_hint(),
                          "step": self.calib["step"] if self.calib else 0},
                "adjust": {"wheel": w, "qty": q, "options": [{k: o[k] for k in ("id", "label")} for o in opts],
                           "choice": ch["id"] if ch else "none",
                           "plus": self.state.get("plus", {}).get(f"{w}|{q}", "")},
                "plan": self.plan(),
                "log": self.log[-80:],
                "wheels": [{"id": k, "name": SIDE_WORD[k]} for k in M.WHEELS],
            }
