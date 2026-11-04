#!/usr/bin/env python3
"""Universal quick-print lug frame for the front-view alignment method.

Fits 4, 5, 6 and 8 lug wheels, bolt circles about 60-200 mm, any lug nut size (posts sit on the END of the nut or
stud, held by a magnet, so hex size and seat type don't matter). Three flat arms pivot on one center bolt, so the
arm angles match any lug pattern; each post slides in its arm's slot to match the bolt circle.

All parts print flat, no supports. Needs: pip install trimesh manifold3d
  python make_lug_frame.py            -> STLs for 1/2" PVC pipe mast (21.34 mm OD)
  python make_lug_frame.py --mast 19.05   (3/4" wood dowel) or any rod diameter in mm
"""
import argparse, os
import numpy as np
from manifold3d import Manifold
import trimesh

SEG = 64
CLR = 0.4                      # printing clearance on holes

def cyl(h, r, z0=0.0, r2=None):
    return Manifold.cylinder(h, r, r if r2 is None else r2, SEG).translate((0, 0, z0))

def box(x, y, z, cx=0.0, cy=0.0, z0=0.0):
    return Manifold.cube((x, y, z), True).translate((cx, cy, z0 + z / 2))

def hexagon(af, h, z0=0.0):
    return Manifold.cylinder(h, af / np.sqrt(3), af / np.sqrt(3), 6).translate((0, 0, z0))

def save(m, path):
    mesh = m.to_mesh()
    t = trimesh.Trimesh(mesh.vert_properties[:, :3], mesh.tri_verts, process=True)
    assert t.is_watertight, path
    t.export(path)
    return t.volume / 1000.0   # cm^3

# ---------------------------------------------------------------- arm (print 3 per wheel)
ARM_T, ARM_W = 6.0, 22.0
SLOT_R0, SLOT_R1 = 28.0, 102.0      # post center radius range -> bolt circles 56-204 mm
ARM_L = 112.0

def arm():
    a = cyl(ARM_T, ARM_W / 2 + 3) + box(ARM_L, ARM_W, ARM_T, cx=ARM_L / 2) + cyl(ARM_T, ARM_W / 2).translate((ARM_L, 0, 0))
    a -= cyl(ARM_T + 2, 4.0 + CLR / 2, -1)                                    # M8 pivot
    slot = box(SLOT_R1 - SLOT_R0, 6.0 + CLR, ARM_T + 2, cx=(SLOT_R0 + SLOT_R1) / 2, z0=-1)
    slot += cyl(ARM_T + 2, 3.0 + CLR / 2, -1).translate((SLOT_R0, 0, 0)) + cyl(ARM_T + 2, 3.0 + CLR / 2, -1).translate((SLOT_R1, 0, 0))
    a -= slot
    a -= box(4.0, 12.0, ARM_T + 2, cx=ARM_L + 3, z0=-1)                        # strap / zip-tie / bungee slot
    return a

# ---------------------------------------------------------------- post (one of each length per wheel)
POST_D = 20.0
MAG_D, MAG_H = 12.0, 3.0         # 12 x 3 mm neodymium disc, glued in
def post(length, rings):
    p = cyl(length, POST_D / 2)
    p -= hexagon(10.0 + CLR, 5.5, length - 5.5)                                # M6 nut trap (top, under the arm)
    p -= cyl(length, 3.0 + CLR / 2, 4.0)                                       # M6 bolt clearance
    p -= cyl(2.5, 7.75)                                                         # 15.5 mm recess centers on the nut dome / stud end
    p -= cyl(MAG_H + 0.2, MAG_D / 2 + 0.15, 2.5)                                # magnet pocket
    for i in range(rings):                                                      # 1/2/3 grooves = stack position
        z = length - 10 - i * 3.0
        p -= (cyl(1.2, POST_D / 2 + 1, z) - cyl(1.2, POST_D / 2 - 0.8, z))
    return p

# ---------------------------------------------------------------- hub: M8 knob + mast socket
HUB_R = 18.0          # 36 mm flange: two hubs fit side by side beside the arms on a 220 bed

def hub(mast_d):
    bore = mast_d / 2 + CLR / 2
    wall = 4.0
    flange_h, sock_h = 10.0, 45.0
    h = cyl(flange_h, HUB_R) + cyl(sock_h, bore + wall, flange_h - 0.01)
    for k in range(4):                                                          # ribs
        h += box(22.0, 3.0, 22.0, cx=bore + wall + 8, z0=flange_h - 1).rotate((0, 0, 45 + 90 * k)) \
             ^ cyl(flange_h + 22, HUB_R, 0, r2=bore + wall)
    h -= hexagon(13.0 + CLR, 6.8, -0.01)                                        # captive M8 nut: turning the hub tightens it
    h -= cyl(flange_h + 2, 4.0 + CLR / 2, -1)
    h -= cyl(sock_h + 1, bore, flange_h + 2.0)                                  # mast socket, 2 mm floor
    for k in range(2):                                                          # slits so a zip tie / hose clamp grips the mast
        h -= box(2.0, 2 * (bore + wall) + 2, 25.0, z0=flange_h + sock_h - 25.0 + 0.01).rotate((0, 0, 90 * k))
    h -= (cyl(5.0, bore + wall + 2, flange_h + sock_h - 9) - cyl(5.0, bore + wall - 1.2, flange_h + sock_h - 9))
    for k in range(6):                                                          # grip knurls on the flange
        h -= cyl(12, 2.5, -1).translate((HUB_R * np.cos(k * np.pi / 3), HUB_R * np.sin(k * np.pi / 3), 0))
    return h

# ---------------------------------------------------------------- target mount: pad + horizontal mast socket
PAD_X, PAD_Y = 70.0, 60.0   # pad along the mast x across; sized so both sets fit one 220 bed

def target_mount(mast_d):
    bore = mast_d / 2 + CLR / 2
    wall = 3.2
    pad = box(PAD_X, PAD_Y, 4.0)
    for sx in (-PAD_X / 2 + 7, PAD_X / 2 - 7):
        for sy in (-PAD_Y / 2 + 6, PAD_Y / 2 - 6):
            pad -= cyl(6, 2.25, -1).translate((sx, sy, 0))                      # M4 or zip ties into the target board
    x0 = -PAD_X / 2
    L = PAD_X / 2 + 12.0                                                        # mast goes in 12 mm past the pad center
    zc = 4.0 + bore + wall - 0.5
    tube = Manifold.cylinder(L, bore + wall, bore + wall, SEG).rotate((0, 90, 0)).translate((x0, 0, zc))
    tube += box(L, 2 * (bore + wall), zc, cx=x0 + L / 2, z0=0)                # fill down to the pad
    # teardrop bore so it prints without supports
    tip = Manifold.cylinder(L + 2, 0.3, 0.3, 8).translate((-bore * 1.4142, 0, 0))   # -x here becomes +z (up) below
    tear = Manifold.batch_hull([Manifold.cylinder(L + 2, bore, bore, SEG), tip])
    tear = tear ^ Manifold.cube((2 * bore + 2 * wall - 2.4, 4 * bore, L + 2), True).translate((0, 0, (L + 2) / 2))
    tear = tear.rotate((0, 90, 0)).translate((x0 - 1, 0, zc))
    m = pad + tube - tear
    m -= box(4.0, PAD_Y + 20, 2 * (bore + wall) + 10, cx=x0 + 12, z0=zc - (bore + wall) - 5 + 0.5) - pad  # zip-tie slot through tube walls
    return m

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mast", type=float, default=21.34, help="mast rod OD in mm (1/2in PVC 21.34, 3/4in dowel 19.05)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "stl"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    tag = f"{a.mast:.2f}mm".replace(".", "p")
    vols = {}
    vols["arm.stl (print 3)"] = save(arm(), os.path.join(a.out, "arm.stl"))
    for i, extra in enumerate((0, 6, 12)):
        vols[f"post_{i+1}.stl"] = save(post(25.0 + extra, i + 1), os.path.join(a.out, f"post_{i+1}_{25+extra}mm.stl"))
    vols["post_long.stl (optional, for wheels whose center cap sticks out)"] = save(post(45.0, 0), os.path.join(a.out, "post_extra_45mm.stl"))
    vols["hub.stl"] = save(hub(a.mast), os.path.join(a.out, f"hub_mast_{tag}.stl"))
    vols["target_mount.stl"] = save(target_mount(a.mast), os.path.join(a.out, f"target_mount_mast_{tag}.stl"))
    for k, v in vols.items():
        print(f"{k:60s} {v:6.1f} cm3 solid")

if __name__ == "__main__":
    main()
