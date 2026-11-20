"""
Alignment specs, adjuster rates, adjuster directions and thread pitches for the car on Intake, read in Python
from that make's knowledge DB (kb/<make>.db: FSM and CHARM pages). Standard library only.

Nothing here is invented: every item carries the manual line it came from and its source (FSM page or CHARM
path). When a line can't be read with confidence it is still shown as a raw line, and the screen asks the user
to pick or type the value. Empty beats wrong.

    import sqlite3, align_specs
    db = sqlite3.connect("file:kb/<make>.db?mode=ro", uri=True)
    found = align_specs.lookup(db, model="Forester", year=2002)
    found["specs"], found["rates"], found["directions"], found["pitches"]
"""
import re

_MINUS = "[-−–]"
_NUM = r"\d+(?:\.\d+)?"
_DEG = r"(?:°|º|\^\{°\}|\^\{\\circ\}|\s?deg\b\.?|\s?degrees?\b)"
_MIN = r"(?:'|′|’|�|\s?min\b)"
_ANG = rf"(?:{_NUM}\s*{_DEG}\s*(?:{_NUM}\s*{_MIN})?|{_NUM}\s*{_MIN})"
_PM = r"(?:±|\+/-|\+ /-|\+-|\+/−)"

ANGLE_RE = re.compile(rf"(?P<sign>{_MINUS}|\+)?\s*(?P<a>{_ANG})(?:\s*{_PM}\s*(?P<tol>{_ANG}))?")
TOLONLY_RE = re.compile(rf"{_PM}\s*(?P<tol>{_ANG})")
ASYM_RE = re.compile(rf"\+\s*(?P<hi>{_ANG})\s*/\s*{_MINUS}\s*(?P<lo>{_ANG})")
MM_RE = re.compile(rf"(?P<sign>{_MINUS}|\+)?\s*(?P<v>{_NUM})\s*(?:{_PM}\s*(?P<tol>{_NUM}))?\s*mm\b", re.I)
IN_PAREN_RE = re.compile(r"\([^()]*\b(?:in|inch|inches)\b[^()]*\)", re.I)

QTY_WORDS = [("thrust", re.compile(r"\bthrust\s+angle", re.I)),
             ("toe", re.compile(r"\btoe(?:[- ]?(?:in|out))?\b", re.I)),
             ("camber", re.compile(r"\bcamber\b", re.I)),
             ("caster", re.compile(r"\bcaster\b", re.I))]
OTHER_QTY_RE = re.compile(r"king\s*pin|steering\s+axis|\bsai\b|wheel\s+arch|turning\s+angle|steering\s+angle|"
                          r"inner\s+wheel|outer\s+wheel|inside\s+wheel|outside\s+wheel|ride\s+height|trim\s+height|"
                          r"vehicle\s+height|stabili[sz]er|torque", re.I)
AXLE_RE = re.compile(r"\b(front|rear)\b", re.I)
SKIP_PATH_RE = re.compile(r"Description and Operation|Labor Times|Tools and Equipment|Timing Component|Headlamp|"
                          r"Diagrams|Connector", re.I)

PROSE_RE = re.compile(r"\b(?:chang\w*|mov(?:e|ing|ement)|rotat\w*|turn\w*|reset|found|loosen\w*|tighten\w*|keep|press|"
                      r"measur\w*|should|will|larger|less\s+than|or\s+less|more\s+than|approx\w*|increment\w*|graduation|"
                      r"scale|example\w*|shift)\b", re.I)
CUT_RE = re.compile(r"\s(?=(?:When|If|Note|NOTE|Refer|Try|Make sure)\b)")
RANGE_MM_RE = re.compile(rf"(?P<a>{_MINUS}?\s*{_NUM})\s*(?:—|–|to)\s*(?P<b>{_MINUS}?\s*{_NUM})\s*mm\b", re.I)

ANGLE_LIMIT = {"camber": 6.0, "caster": 15.0, "toe": 3.0, "thrust": 2.0}
TOL_LIMIT = {"camber": 3.0, "caster": 4.0, "toe": 1.5, "thrust": 1.5}
MM_LIMIT = 20.0


def angle_value(text):
    """'-0 deg 15'' -> -0.25, "0^{°}10'" -> 0.1667, "15'" -> 0.25, '1.2°' -> 1.2. None when not an angle."""
    m = ANGLE_RE.search(text)
    if not m:
        return None
    return _ang(m.group("a"), m.group("sign"))


def _ang(a, sign=None):
    nums = re.findall(_NUM, a)
    if re.search(_DEG, a):
        v = float(nums[0]) + (float(nums[1]) / 60.0 if len(nums) > 1 else 0.0)
    else:
        v = float(nums[0]) / 60.0
    return -v if sign and re.match(_MINUS, sign) else v


def _clean(line):
    line = IN_PAREN_RE.sub(" ", line)
    line = line.replace("\\text{", "").replace("$", " ").replace("{", "").replace("}", "")
    line = re.sub(r"<[^>]+>", " ", line)
    return line


def parse_specs(body, default_axle=None):
    """[(quantity, axle, basis, unit, center, tol_lo, tol_hi, raw_line)] from one page. unit is 'deg' or 'mm';
    center None = the line gives only a tolerance (filled in later only when that is safe). basis is 'each',
    'total' or '' (unknown). Lines saying 'difference' (left/right difference limits) are not tolerances."""
    out = []
    qty, axle, basis, pending_tol = None, default_axle, "", {}
    for raw in body.split("\n"):
        line = _clean(raw)
        if not line.strip():
            continue
        cells = [c for c in re.split(r"\|", line) if c.strip()] or [line]
        line_basis = ("total" if re.search(r"\btotal\b", line, re.I) else
                      "each" if re.search(r"\beach\b|per wheel|individual", line, re.I) else "")
        for cell in cells:
            cell = CUT_RE.split(cell)[0]
            found_q = None
            for q, rx in QTY_WORDS:
                if rx.search(cell):
                    found_q = q
                    break
            if found_q:
                if found_q != qty:
                    pending_tol.pop(qty, None)
                qty = found_q
                basis = line_basis
            elif OTHER_QTY_RE.search(cell):
                qty = None
            am = AXLE_RE.findall(cell)
            if len(am) == 1:
                axle = am[0].lower()
            if qty is None or PROSE_RE.search(cell):
                continue
            if re.search(r"difference", cell, re.I):
                cell = re.split(r"difference", cell, flags=re.I)[0]
                if not cell.strip():
                    continue
            rest = cell
            mt = re.search(rf"tolerance\s*:?\s*{_PM}\s*({_ANG})", rest, re.I)
            if mt:
                pending_tol[qty] = _ang(mt.group(1))
                rest = rest[:mt.start()] + " " + rest[mt.end():]
            me = re.search(rf"\beach\b[^:]*angle\s*:?\s*{_PM}\s*({_ANG})", rest, re.I)
            if me:
                t = _ang(me.group(1))
                out.append((qty, axle, "each", "deg", None, -t, t, raw.strip()))
                rest = rest[:me.start()] + " " + rest[me.end():]
            em = re.search(r"\beach\b[^:]*angle\s*:?", rest, re.I)
            if em:
                for m in ANGLE_RE.finditer(rest[em.end():]):
                    c = _ang(m.group("a"), m.group("sign"))
                    t = _ang(m.group("tol")) if m.group("tol") else pending_tol.get(qty)
                    if t is None:
                        continue
                    out.append((qty, axle, "each", "deg", c, -t, t, raw.strip()))
                rest = rest[:em.start()]
            for m in RANGE_MM_RE.finditer(rest):
                if qty != "toe":
                    break
                a, b2 = (float(x.replace(" ", "").replace("−", "-").replace("–", "-")) for x in (m.group("a"), m.group("b")))
                lo, hi = min(a, b2), max(a, b2)
                c = (lo + hi) / 2
                b = "each" if re.search(r"\beach\b|per wheel", rest[:m.start()], re.I) else "total"
                out.append((qty, axle, b, "mm", c, lo - c, hi - c, raw.strip()))
            rest = RANGE_MM_RE.sub(" ", rest)
            for m in MM_RE.finditer(rest):
                if qty != "toe" or not m.group("tol"):
                    continue
                v = float(m.group("v"))
                if m.group("sign") and re.match(_MINUS, m.group("sign")):
                    v = -v
                t = float(m.group("tol"))
                b = "each" if re.search(r"\beach\b|per wheel", rest[:m.start()], re.I) else "total"
                out.append((qty, axle, b, "mm", v, -t, t, raw.strip()))
            rest_deg = MM_RE.sub(" ", rest)
            ma = ASYM_RE.search(rest_deg)
            tol_spans = [(t.start(), t.end()) for t in TOLONLY_RE.finditer(rest_deg)]
            for m in ANGLE_RE.finditer(rest_deg):
                if ma and ma.start() <= m.start() < ma.end():
                    continue
                if not m.group("tol") and any(a <= m.start("a") < b for a, b in tol_spans):
                    continue
                c = _ang(m.group("a"), m.group("sign"))
                if m.group("tol"):
                    t = _ang(m.group("tol"))
                    lo, hi = -t, t
                elif ma and ma.start() >= m.end() - 1:
                    lo, hi = -_ang(ma.group("lo")), _ang(ma.group("hi"))
                else:
                    t = pending_tol.get(qty)
                    if t is None:
                        continue
                    lo, hi = -t, t
                b = "each" if qty == "camber" else (basis or line_basis)
                out.append((qty, axle, b, "deg", c, lo, hi, raw.strip()))
    return out


def _plausible(qty, unit, center, lo, hi):
    if unit == "mm":
        return all(x is None or abs(x) <= MM_LIMIT for x in (center, lo, hi))
    if center is not None and abs(center) > ANGLE_LIMIT.get(qty, 10):
        return False
    return all(x is None or abs(x) <= TOL_LIMIT.get(qty, 3) for x in (lo, hi))


_SENT_SPLIT = re.compile(r"(?<=[.;])\s+(?=[A-Z(])|\n\s*\n")
RATE_RE = re.compile(r"\b(?:one|1|each|a single)\s+(?:full\s+)?(?:scale\s+)?(turn|graduation|scale|notch|revolution|"
                     r"rotation|mark)s?\b", re.I)
CHANGE_RE = re.compile(r"chang|adjust|alter|increase|decrease|move|will", re.I)
DIR_RE = re.compile(r"(counter[- ]?clockwise|anti[- ]?clockwise|clockwise)", re.I)


def _sentences(body):
    text = _clean(body)
    text = re.sub(r"(?<![.:;\n])\n(?!\s*\n)", " ", text)
    return [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]


def parse_rates(body):
    """[(quantity, unit, value, value_unit, both_sides, raw)]: 'one scale graduation changes camber by approx 0°10''
    -> ('camber', 'graduation', 0.1667, 'deg', False, ...). value_unit 'deg' or 'mm'. both_sides=True when the
    sentence says the figure is for left and right moved together."""
    out = []
    for s in _sentences(body):
        um = RATE_RE.search(s)
        if not um or not CHANGE_RE.search(s):
            continue
        qty = None
        for q, rx in QTY_WORDS:
            if rx.search(s[um.start():]) or rx.search(s):
                qty = q
                break
        if not qty:
            continue
        unit = um.group(1).lower()
        unit = {"scale": "graduation", "mark": "graduation", "revolution": "turn", "rotation": "turn"}.get(unit, unit)
        tail = s[um.end():]
        both = bool(re.search(r"left and right|both|at the same time|same direction|equal", s, re.I))
        a = ANGLE_RE.search(tail)
        mm = MM_RE.search(tail)
        if a and (not mm or a.start() <= mm.start()):
            out.append((qty, unit, _ang(a.group("a"), a.group("sign")), "deg", both, s))
        elif mm:
            out.append((qty, unit, float(mm.group("v")), "mm", both, s))
    return out


def parse_directions(body):
    """[(quantity, raw sentence)] for sentences telling which way to turn an adjuster, plus a parsed reading
    where the sentence is plain: [(qty, 'increase'|'decrease', 'cw'|'ccw', view, raw)] in the second list."""
    raw_hits, parsed = [], []
    for s in _sentences(body):
        if not DIR_RE.search(s):
            continue
        qty = next((q for q, rx in QTY_WORDS if rx.search(s)), None)
        if not qty:
            continue
        raw_hits.append((qty, s))
        m = re.search(r"to\s+(increase|decrease)\s+(?:the\s+)?(toe[- ]?in|toe|camber|caster)[^.]*?\b"
                      r"(counter[- ]?clockwise|anti[- ]?clockwise|clockwise)", s, re.I)
        if m:
            view = ("inside" if re.search(r"view\w*\s+from\s+(the\s+)?inside|inner side|from inside", s, re.I) else
                    "outside" if re.search(r"view\w*\s+from\s+(the\s+)?outside|outer side", s, re.I) else "")
            cw = "cw" if m.group(3).lower() == "clockwise" else "ccw"
            parsed.append((qty, m.group(1).lower(), cw, view, s))
    return raw_hits, parsed


PITCH_RE = re.compile(r"\bM(\d{1,2})\s*[x×X]\s*(\d(?:\.\d{1,2})?)\b|thread\s+pitch\D{0,20}(\d(?:\.\d{1,2})?)\s*mm", re.I)


def parse_pitches(body):
    """[(pitch_mm, raw sentence)] for sentences about tie rods / adjusting sleeves that print a thread size."""
    out = []
    for s in _sentences(body):
        if not re.search(r"tie[- ]?rod|rack end|adjust\w*\s+(tube|sleeve|bolt)", s, re.I):
            continue
        for m in PITCH_RE.finditer(s):
            p = m.group(2) or m.group(3)
            if p:
                out.append((float(p), s))
    return out


def vehicle_ids(db, model, year, engine=None):
    if not model:
        return []
    sql, args = "SELECT id FROM vehicle WHERE upper(model)=upper(?)", [model]
    if year:
        sql += " AND year=?"
        args.append(int(year))
    if engine:
        sql += " AND engine=?"
        args.append(engine)
    return [r[0] for r in db.execute(sql, args)]


def _source(kind, title, path, page):
    if kind == "charm":
        return f"CHARM: {path}"
    return f"FSM {title}, {path}" + (f", p.{page}" if page else "")


def _sections(db, vids, where, args=(), limit=300):
    q = ",".join("?" * len(vids))
    return db.execute(
        "SELECT s.id, d.kind, d.title, s.path, s.page, s.body FROM section s JOIN doc d ON d.id=s.doc_id "
        f"WHERE s.id IN (SELECT section_id FROM section_vehicle WHERE vehicle_id IN ({q})) AND ({where}) "
        "ORDER BY d.kind='charm', s.path, s.part LIMIT ?", list(vids) + list(args) + [limit]).fetchall()


def lookup(db, model, year, engine=None):
    """Everything the ALIGNMENT screen can cite for this car. Returns dict(specs, rates, directions,
    direction_rules, pitches, pages) where each item has a 'source' string and its 'raw' manual line.
    specs: one dict per distinct reading, FSM first; 'pick' = True when it is the only reading for its
    quantity/axle (the screen pre-selects it), else the user chooses."""
    vids = vehicle_ids(db, model, year, engine)
    res = {"specs": [], "diameters": [], "rates": [], "directions": [], "direction_rules": [], "pitches": [], "pages": [],
           "vehicle_ids": vids}
    if not vids:
        return res
    rows = _sections(db, vids, "s.path LIKE '%lignment%' OR s.path LIKE '%Toe%' OR s.path LIKE '%Camber%'")
    seen_body = set()
    for sid, kind, title, path, page, body in rows:
        if SKIP_PATH_RE.search(path) or not re.search(r"camber|toe", body, re.I):
            continue
        key = hash(body)
        if key in seen_body:
            continue
        seen_body.add(key)
        src = _source(kind, title, path, page)
        tsb = bool(re.search(r"Technical Service Bulletin|Bulletin", path, re.I))
        res["pages"].append({"section_id": sid, "source": src, "tsb": tsb})
        default_axle = ("front" if re.search(r"\bFRONT\b", path) and not re.search(r"\bREAR\b", path) else
                        "rear" if re.search(r"\bREAR\b", path) else None)
        for qty, axle, basis, unit, c, lo, hi, raw in ([] if tsb else parse_specs(body, default_axle)):
            res["specs"].append({"quantity": qty, "axle": axle, "basis": basis, "unit": unit, "center": c,
                                 "tol_lo": lo, "tol_hi": hi, "raw": raw, "source": src, "section_id": sid,
                                 "tsb": tsb, "plausible": _plausible(qty, unit, c, lo, hi)})
        for qty, unit, val, vunit, both, raw in parse_rates(body):
            res["rates"].append({"quantity": qty, "unit": unit, "value": val, "value_unit": vunit,
                                 "both_sides": both, "raw": raw, "source": src})
        hits, parsed = parse_directions(body)
        res["directions"] += [{"quantity": q, "raw": s, "source": src} for q, s in hits]
        res["direction_rules"] += [{"quantity": q, "change": ch, "turn": cw, "view": v, "raw": s, "source": src}
                                   for q, ch, cw, v, s in parsed]
    for sid, kind, title, path, page, body in _sections(
            db, vids, "(s.path LIKE '%Tie Rod%' OR s.path LIKE '%Steering%' OR s.path LIKE '%lignment%') AND "
                      "(s.body LIKE '%pitch%' OR s.body LIKE '%M1_x%' OR s.body LIKE '%M1_ x%')", limit=200):
        for p, raw in parse_pitches(body):
            res["pitches"].append({"pitch_mm": p, "raw": raw, "source": _source(kind, title, path, page)})
    res["diameters"] = diameter_from_specs(res["specs"])
    res["specs"] = _merge_specs(res["specs"])
    res["rates"] = _dedupe(res["rates"], ("quantity", "unit", "value", "value_unit", "both_sides"))
    res["directions"] = _dedupe(res["directions"], ("quantity", "raw"))
    res["direction_rules"] = _dedupe(res["direction_rules"], ("quantity", "change", "turn", "view"))
    res["pitches"] = _dedupe(res["pitches"], ("pitch_mm", "raw"))
    return res


def _dedupe(items, keys):
    out, seen = [], {}
    for it in items:
        k = tuple(it[x] for x in keys)
        if k in seen:
            seen[k].setdefault("also", []).append(it["source"])
            continue
        seen[k] = it
        out.append(it)
    return out


def _merge_specs(specs):
    """One entry per distinct reading; a per-wheel toe angle with no center gets the center 0 only when the
    same axle's toe in mm is centered on 0 (then 0 mm = 0 deg); otherwise it is dropped."""
    specs = _dedupe(specs, ("quantity", "axle", "basis", "unit", "center", "tol_lo", "tol_hi"))
    out = []
    for s in specs:
        if s["center"] is None:
            zero_mm = any(o["quantity"] == s["quantity"] and o["axle"] == s["axle"] and o["unit"] == "mm"
                          and o["center"] == 0 for o in specs)
            if not zero_mm:
                continue
            s = dict(s, center=0.0, note="center 0 taken from the same line's 0 mm toe")
        out.append(s)
    groups = {}
    for s in out:
        if s["plausible"] and not s["tsb"]:
            groups.setdefault((s["quantity"], s["axle"], s["unit"], s["basis"]), []).append(s)
    for s in out:
        g = groups.get((s["quantity"], s["axle"], s["unit"], s["basis"]), [])
        s["pick"] = len(g) == 1 and g[0] is s and s["axle"] is not None and s["tol_hi"] is not None
    return out


def diameter_from_specs(specs):
    """Toe mm<->deg basis the manual itself implies, when one axle's toe is printed both ways (e.g. '0 +/-3 mm
    Each Toe Angle : +/- 0 deg 09''): per-wheel mm tolerance / tan(per-wheel angle tolerance). None if absent."""
    import math
    out = []
    for mm in specs:
        if mm["quantity"] != "toe" or mm["unit"] != "mm" or not mm["tol_hi"]:
            continue
        for d in specs:
            if (d["quantity"] == "toe" and d["unit"] == "deg" and d["axle"] == mm["axle"] and d["basis"] == "each"
                    and d["tol_hi"] and d["raw"] == mm["raw"]):
                per_wheel_mm = mm["tol_hi"] / (2.0 if mm["basis"] == "total" else 1.0)
                if any(o["raw"] == mm["raw"] for o in out):
                    continue
                out.append({"diameter_mm": per_wheel_mm / math.tan(math.radians(d["tol_hi"])), "axle": mm["axle"],
                            "raw": mm["raw"], "source": mm["source"]})
    return out
