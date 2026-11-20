#!/usr/bin/env python3
"""Two full lug-frame sets (both wheels: 6 arms, 6 posts, 2 hubs, 2 target mounts) on one 220 x 220 bed
(stock Ender 3 Pro), parts flat as exported, no supports.   python make_plates.py [--mast 19.05]"""
import argparse
import os
from manifold3d import Manifold
import make_lug_frame as L

GAP = 3.0


def place(m, x, y):
    """Put a part's bounding-box corner at (x, y) on the bed, sitting at z=0."""
    b = m.bounding_box()
    return m.translate((x - b[0], y - b[1], -b[2]))


def size(m):
    b = m.bounding_box()
    return b[3] - b[0], b[4] - b[1]


def plate(items, bed):
    out = None
    for m, x, y in items:
        p = place(m, x, y)
        b = p.bounding_box()
        assert b[3] <= bed[0] + 1e-6 and b[4] <= bed[1] + 1e-6, f"part off the bed at {x},{y}: {b}"
        out = p if out is None else out + p
    assert len(out.decompose()) == len(items), "parts overlap"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mast", type=float, default=21.34)
    a = ap.parse_args()
    tag = f"{a.mast:.2f}mm".replace(".", "p")
    arm = L.arm()
    posts = [L.post(25.0 + e, i + 1) for i, e in enumerate((0, 6, 12))] * 2
    hub, mount = L.hub(a.mast), L.target_mount(a.mast)
    aw, ah = size(arm)
    mw, mh = size(mount)
    hw, _ = size(hub)
    pw, _ = size(posts[0])
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stl")

    BED = 220.0
    items = [(arm, 0, i * (ah + GAP)) for i in range(6)]
    x2 = aw + GAP
    assert x2 + max(mw, 2 * hw + GAP, 3 * pw + 2 * GAP) <= BED, "right strip too narrow"
    items += [(mount, x2, 0), (mount, x2, mh + GAP)]
    y = 2 * (mh + GAP)
    items += [(hub, x2, y), (hub, x2 + hw + GAP, y)]
    y += hw + GAP
    items += [(p, x2 + (i % 3) * (pw + GAP), y + (i // 3) * (pw + GAP)) for i, p in enumerate(posts)]
    L.save(plate(items, (BED, BED)), os.path.join(out, f"plate_2sets_ender3_220mm_mast_{tag}.stl"))
    print("ok")


if __name__ == "__main__":
    main()
