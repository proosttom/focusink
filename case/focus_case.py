"""FocusInk case — Waveshare ESP32-S3-ePaper-1.54 + 45x30x8 mm LiPo, for PETG.

Run headless:   /Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd focus_case.py
or in FreeCAD:  Macro > Macros... > focus_case.py > Execute
Writes body / carrier / lid STLs (already oriented for printing) and focus_case.FCStd next to this file.

Coordinates are the board's own (from Waveshare's STEP model):
  PCB x 0..34, y -4..43.8, front face z=-1, back face z=0; display glass faces -z (z=-4.5).
  Side buttons stick out at x=-0.55; USB-C on the y=-4 edge; tallest back part = 2x5 header (z 9.0).

Parts:
  body     front + walls, display window, flex buttons, USB-C opening, retro wedge base. Print window-side down.
  carrier  plate the battery lies on, with 4 legs pressing the PCB at its mounting holes. Print plate-side down.
  lid      covers the whole back and carries the rear foot; held by 2 M2 countersunk screws (6 mm), one in each end wall in bosses in the long walls. Print inner face down.
Stack: front wall | display | PCB | carrier legs | carrier plate | battery | lid.
"""
import os

import FreeCAD as App
import Part
from FreeCAD import Vector as V

# ---------------------------------------------------------------- parameters (mm)
BAT_L, BAT_W, BAT_T = 45.0, 31.0, 8.0   # battery: length (along y), width (x), thickness (room for a 30 mm cell + slack)
BAT_GAP = 0.8                           # free space between battery and lid (pouch cells swell; no clamping)
WALL = 1.8                              # side walls
FRONT = 1.5                             # front wall (in front of the display)
LID_T = 1.6
FIT = 0.2                               # lid lip to wall gap per side (PETG)
WALL_END = 2.8                          # short end walls: thick enough to take the 2 lid screws inside the wall
SCREW_H = 5.5                           # pilot hole depth below the wall top (M2 x 6 countersunk)
PILOT_D = 1.7                           # M2 self-tapping into PETG; 1.6 = tighter, 1.8 = looser
NUB_GAP = 0.35                          # button nub to switch stem (0.15 kept the switches pressed)
TILT_DEG = 20                           # lean-back angle when standing
FOOT_L = 16.0                           # rear foot on the lid: how far it extends the footing back along the desk
FOOT_H = 12.0                           # how far up the lid the foot's root reaches
FOOT_T = 3.0                            # thickness of the foot's tip
BASE_INSET = 1.2                        # base is this much narrower than the case on each side

IN_X0, IN_X1 = -1.1, 34.6               # cavity: PCB + 0.55 to the button stems / 0.6 elsewhere
IN_Y0, IN_Y1 = -4.6, 44.4
STEM_X = -0.55                          # outer face of the side switch stems
IN_R = 2.7
OUT_R = IN_R + WALL                     # 4.5, same as the stock case

Z_FRONT_IN = -4.7                       # display glass is at -4.5: 0.2 slack so the PCB seats on its standoffs
Z_FRONT_OUT = Z_FRONT_IN - FRONT
HEADER_TOP = 9.0
PLATE_Z0 = HEADER_TOP + 0.3             # carrier plate
PLATE_T = 1.0
BAT_Z0 = PLATE_Z0 + PLATE_T
LID_Z0 = BAT_Z0 + BAT_T + BAT_GAP       # lid inner face = top of the walls
LID_Z1 = LID_Z0 + LID_T
TOP_Z = LID_Z1                          # back of the case (lid sits on top of the walls)

WIN_CX, WIN_CY = 17.0, 22.2             # display window centre (27 mm active area)
WIN_W, WIN_H = 28.6, 29.6               # window: a bit larger than the active area so the edge rows are readable
DISP_X0, DISP_X1, DISP_Y1 = 1.1, 32.9, 38.1   # display glass outline (bottom edge at y -0.52)
HOLES = [(3.0, -2.3), (31.0, -2.3), (3.0, 41.7), (31.0, 41.7)]   # PCB M2 holes
BUTTONS_Y = [5.5, 16.0]                 # side switch centres
BUTTON_Z = 1.4
USB_CX, USB_CZ, USB_W, USB_H = 16.5, -0.25, 14.0, 8.0   # opening in the end wall (plug's metal shell)
PLUG_W, PLUG_H = 16.0, 9.5             # tunnel through the base, for the cable plug's plastic body
BAT_CX, BAT_CY = (IN_X0 + IN_X1) / 2, (IN_Y0 + IN_Y1) / 2
SCREWS = [(BAT_CX, IN_Y0 - 1.0), (BAT_CX, IN_Y1 + 1.0)]   # 2 screws, centre of each end wall (nothing sticks into the cavity)
OX0, OX1, OY0, OY1 = IN_X0 - WALL, IN_X1 + WALL, IN_Y0 - WALL_END, IN_Y1 + WALL_END

HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()


# ---------------------------------------------------------------- helpers
def rbox(x0, x1, y0, y1, z0, z1, r):
    """Box with its four vertical edges rounded."""
    b = Part.makeBox(x1 - x0, y1 - y0, z1 - z0, V(x0, y0, z0))
    if r <= 0:
        return b
    edges = [e for e in b.Edges if abs(e.Vertexes[0].Point.z - e.Vertexes[1].Point.z) > 1e-6]
    return b.makeFillet(r, edges)


def rwire(x0, x1, y0, y1, z, r):
    f = rbox(x0, x1, y0, y1, z, z + 1, r)
    face = [fc for fc in f.Faces if abs(fc.BoundBox.ZMax - z) < 1e-6 and abs(fc.BoundBox.ZMin - z) < 1e-6][0]
    return face.OuterWire


def prism_yz(points, x0, x1):
    """Polygon in the y-z plane, extruded along x."""
    w = Part.makePolygon([V(x0, y, z) for y, z in points] + [V(x0, points[0][0], points[0][1])])
    return Part.Face(w).extrude(V(x1 - x0, 0, 0))


def prism_xz(points, y0, y1):
    w = Part.makePolygon([V(x, y0, z) for x, z in points] + [V(points[0][0], y0, points[0][1])])
    return Part.Face(w).extrude(V(0, y1 - y0, 0))


def cyl(r, x, y, z0, z1):
    return Part.makeCylinder(r, z1 - z0, V(x, y, z0))


def fuse(*shapes):
    s = shapes[0]
    for o in shapes[1:]:
        s = s.fuse(o)
    return s.removeSplitter()


# ---------------------------------------------------------------- button icons
# Drawn on the outside of the button wall (plane x = wall_x), raised ICON_H outwards (-x).
# Seen from outside with the case standing up: up = +y, right = +z (display side is on the left).
ICON_H = 0.6


def _glyph(wall_x, pts_zy):
    w = Part.makePolygon([V(wall_x + 0.1, y, z) for z, y in pts_zy] + [V(wall_x + 0.1, pts_zy[0][1], pts_zy[0][0])])
    return Part.Face(w).extrude(V(-(ICON_H + 0.1), 0, 0))


def _stroke(wall_x, a, b, w):
    """Thick line from a to b (z, y) with round caps."""
    import math
    (za, ya), (zb, yb) = a, b
    d = math.hypot(zb - za, yb - ya)
    nz, ny = -(yb - ya) / d * w / 2, (zb - za) / d * w / 2
    s = _glyph(wall_x, [(za + nz, ya + ny), (zb + nz, yb + ny), (zb - nz, yb - ny), (za - nz, ya - ny)])
    for z, y in (a, b):
        c = Part.makeCylinder(w / 2, ICON_H + 0.1, V(wall_x + 0.1, y, z), V(-1, 0, 0))
        s = s.fuse(c)
    return s


def icon_play_pause(wall_x, yc, zc):
    zc -= 1.4   # icon is 5.6 wide: triangle + gap + two bars
    tri = _glyph(wall_x, [(zc - 1.4, yc - 1.7), (zc + 1.2, yc), (zc - 1.4, yc + 1.7)])
    bar1 = _glyph(wall_x, [(zc + 1.9, yc - 1.7), (zc + 2.7, yc - 1.7), (zc + 2.7, yc + 1.7), (zc + 1.9, yc + 1.7)])
    bar2 = _glyph(wall_x, [(zc + 3.4, yc - 1.7), (zc + 4.2, yc - 1.7), (zc + 4.2, yc + 1.7), (zc + 3.4, yc + 1.7)])
    return fuse(tri, bar1, bar2)


def icon_check(wall_x, yc, zc):
    a, b, c = (zc - 2.0, yc + 0.1), (zc - 0.6, yc - 1.4), (zc + 2.2, yc + 1.8)
    return fuse(_stroke(wall_x, a, b, 0.9), _stroke(wall_x, b, c, 0.9))


# ---------------------------------------------------------------- body
def make_body():
    ox0, ox1, oy0, oy1 = OX0, OX1, OY0, OY1
    outer = rbox(ox0, ox1, oy0, oy1, Z_FRONT_OUT, LID_Z0, OUT_R)
    try:  # soften the front edge
        front_edges = [e for e in outer.Edges if e.BoundBox.ZMax < Z_FRONT_OUT + 1e-6]
        outer = outer.makeFillet(1.0, front_edges)
    except Exception:
        pass
    body = outer.cut(rbox(IN_X0, IN_X1, IN_Y0, IN_Y1, Z_FRONT_IN, TOP_Z + 1, IN_R))

    # display window, chamfered 45° towards the outside
    hw, hh = WIN_W / 2, WIN_H / 2
    win = rbox(WIN_CX - hw, WIN_CX + hw, WIN_CY - hh, WIN_CY + hh, Z_FRONT_OUT - 1, Z_FRONT_IN + 0.1, 1.0)
    cham = Part.makeLoft([rwire(WIN_CX - hw - 1.2, WIN_CX + hw + 1.2, WIN_CY - hh - 1.2, WIN_CY + hh + 1.2, Z_FRONT_OUT - 0.2, 2.2),
                          rwire(WIN_CX - hw, WIN_CX + hw, WIN_CY - hh, WIN_CY + hh, Z_FRONT_OUT + 1.0, 1.0)], True, True)
    body = body.cut(win).cut(cham)

    # PCB standoffs with locating pins (front face of PCB at z=-1)
    for x, y in HOLES:
        body = body.fuse(cyl(1.6, x, y, Z_FRONT_IN - 0.1, -1.0)).fuse(cyl(0.8, x, y, -1.0, -0.5))

    # display guide rails: shallow, with a lead-in chamfer, 0.25 mm off the glass (x 1.1..32.9, y -0.52..38.1).
    # Both long sides and the top end; none at the bottom, where the USB-C connector sits.
    g, rh, ch = 0.25, 1.0, 0.5
    z0, z1 = Z_FRONT_IN - 0.01, Z_FRONT_IN + rh
    dx0, dx1, dy1 = DISP_X0 - g, DISP_X1 + g, DISP_Y1 + g
    body = body.fuse(prism_xz([(IN_X0 - 0.01, z0), (dx0, z0), (dx0, z1 - ch), (dx0 - ch, z1), (IN_X0 - 0.01, z1)], 3.0, 35.0))
    body = body.fuse(prism_xz([(IN_X1 + 0.01, z0), (dx1, z0), (dx1, z1 - ch), (dx1 + ch, z1), (IN_X1 + 0.01, z1)], 3.0, 35.0))
    body = body.fuse(prism_yz([(IN_Y1 + 0.01, z0), (dy1, z0), (dy1, z1 - ch), (dy1 + ch, z1), (IN_Y1 + 0.01, z1)], 7.0, 27.0))

    # lid screw holes, inside the thick end walls
    for x, y in SCREWS:
        body = body.cut(cyl(PILOT_D / 2, x, y, LID_Z0 - SCREW_H, LID_Z0 + 1))

    # flex buttons: U-slot cantilever in the side wall, nub touching the switch stem
    slot = 0.6
    z_lo, z_hi = BUTTON_Z - 3.4, BUTTON_Z + 3.4
    for yc, anchor in ((BUTTONS_Y[0], -2.5), (BUTTONS_Y[1], 24.5)):
        free = yc + 2.5 if anchor < yc else yc - 2.5          # free end just past the switch
        ya, yb = sorted((anchor, free))
        ye0, ye1 = (free, free + slot) if anchor < yc else (free - slot, free)
        x0, x1 = ox0 - 1, IN_X0 + 0.01
        body = body.cut(Part.makeBox(x1 - x0, yb - ya + slot, slot, V(x0, min(ya, ye0), z_hi)))
        body = body.cut(Part.makeBox(x1 - x0, yb - ya + slot, slot, V(x0, min(ya, ye0), z_lo - slot)))
        body = body.cut(Part.makeBox(x1 - x0, slot, z_hi - z_lo + 2 * slot, V(x0, ye0, z_lo - slot)))
        # nub NUB_GAP off the stem, with a lead-in on the back side so the switch slides past it
        nt, z0, z1 = STEM_X - IN_X0 - NUB_GAP, BUTTON_Z - 1.5, BUTTON_Z + 1.5
        body = body.fuse(prism_xz([(IN_X0 - 0.01, z0), (IN_X0 + nt, z0), (IN_X0 + nt, z1 - 0.5), (IN_X0 - 0.01, z1)], yc - 1.5, yc + 1.5))

    # raised icons on the button tabs: PWR (lower, y=5.5) = play/pause, BOOT (y=16) = check
    body = body.fuse(icon_play_pause(ox0, BUTTONS_Y[0], BUTTON_Z))
    body = body.fuse(icon_check(ox0, BUTTONS_Y[1], BUTTON_Z))

    # Retro-terminal base: a wedge under the whole bottom, slightly inset from the sides, so the case
    # stands on a flat foot leaning back TILT_DEG (the whole underside of the base sits on the desk).
    import math
    t = math.tan(math.radians(TILT_DEG))
    reach = (TOP_Z + 0.3 - Z_FRONT_OUT) * t      # +0.3: the lid's back edge stays just off the desk
    inset = BASE_INSET
    # deepest at the front, zero at the back: the front lifts and the screen faces up.
    # It stops at the wall top; the lid covers the strip above it.
    profile = [(oy0 + 0.5, Z_FRONT_OUT), (oy0 + 0.5, LID_Z0), (oy0 - (TOP_Z + 0.3 - LID_Z0) * t, LID_Z0), (oy0 - reach, Z_FRONT_OUT)]
    base = prism_yz(profile, ox0 + inset, ox1 - inset)
    try:  # round the vertical-ish corners and soften the foot edges
        side = [e for e in base.Edges if abs(e.BoundBox.XMin - e.BoundBox.XMax) < 1e-6 and e.BoundBox.YMin < oy0 - 0.1]
        base = base.makeFillet(1.2, side)
    except Exception:
        pass
    body = body.fuse(base)
    # styling groove where the base meets the case (reads as a separate plinth)
    groove = Part.makeBox(ox1 - ox0 + 2, 0.6, TOP_Z - Z_FRONT_OUT + 2, V(ox0 - 1, oy0 + 0.5, Z_FRONT_OUT - 1))
    groove = groove.cut(rbox(ox0 + 0.5, ox1 - 0.5, oy0 - 1, oy1 + 1, Z_FRONT_OUT + 0.5, LID_Z0 + 1, OUT_R - 0.5))
    body = body.cut(groove)

    # USB-C opening, carried through the base so the cable plugs in from underneath
    body = body.cut(Part.makeBox(USB_W, WALL_END + 1.5, USB_H, V(USB_CX - USB_W / 2, oy0 - 0.5, USB_CZ - USB_H / 2)))
    body = body.cut(Part.makeBox(PLUG_W, reach + 2, PLUG_H, V(USB_CX - PLUG_W / 2, oy0 - reach - 2, USB_CZ - PLUG_H / 2)))
    return body.removeSplitter()


# ---------------------------------------------------------------- carrier
def make_carrier():
    c = 0.5   # clearance to the walls, so it drops in without forcing
    plate = rbox(IN_X0 + c, IN_X1 - c, IN_Y0 + c, IN_Y1 - c, PLATE_Z0, PLATE_Z0 + PLATE_T, IN_R - c)
    # cable notches at both short ends, and a pass-through slot in the middle
    for y0 in (IN_Y0, IN_Y1 - 4.4):
        plate = plate.cut(rbox(12.0, 22.0, y0, y0 + 4.4, PLATE_Z0 - 1, PLATE_Z0 + 2, 1.0))
    plate = plate.cut(rbox(11.0, 23.0, 17.0, 23.0, PLATE_Z0 - 1, PLATE_Z0 + 2, 2.0))
    legs = [cyl(1.8, x, y, 0.0, PLATE_Z0 + 0.01) for x, y in HOLES]
    return fuse(plate, *legs)


# ---------------------------------------------------------------- lid
def make_lid():
    ox0, ox1, oy0, oy1 = OX0, OX1, OY0, OY1
    lid = rbox(ox0, ox1, oy0, oy1, LID_Z0, LID_Z1, OUT_R)
    try:  # soften the outer edge
        top = [e for e in lid.Edges if e.BoundBox.ZMin > LID_Z1 - 1e-6]
        lid = lid.makeFillet(0.6, top)
    except Exception:
        pass
    # Rear foot: continues the desk plane of the base backwards, past the lid, so the battery's weight
    # can't tip the case over backwards. Same width as the base.
    import math
    a = math.radians(TILT_DEG)
    d = (math.sin(a), math.cos(a))                 # along the desk, towards the back (y, z)
    n = (math.cos(a), -math.sin(a))                # away from the desk
    sy, sz = oy0, TOP_Z + 0.3                      # where the base's desk plane crosses the lid's bottom edge
    cy, cz = sy + FOOT_L * d[0], sz + FOOT_L * d[1]   # foot tip, on the desk
    foot = prism_yz([(oy0, LID_Z1 - 0.8), (sy, sz), (cy, cz), (cy + FOOT_T * n[0], cz + FOOT_T * n[1]),
                     (oy0 + FOOT_H, LID_Z1 - 0.8)], ox0 + BASE_INSET, ox1 - BASE_INSET)
    try:  # round the outer corners of the foot
        side = [e for e in foot.Edges if abs(e.BoundBox.XMin - e.BoundBox.XMax) < 1e-6 and e.BoundBox.ZMin > LID_Z1]
        foot = foot.makeFillet(1.0, side)
    except Exception:
        pass
    lid = lid.fuse(foot)
    # No lip or battery rims: the lid prints inner face down so the foot needs no support. The 2 screws
    # align it; stick the battery to the carrier with thin double-sided tape.
    # countersunk M2 holes
    for x, y in SCREWS:
        lid = lid.cut(cyl(1.15, x, y, LID_Z0 - 2, LID_Z1 + 1))
        lid = lid.cut(Part.makeCone(1.15, 2.05, 0.9, V(x, y, LID_Z1 - 0.9)))
        lid = lid.cut(cyl(2.2, x, y, LID_Z1 - 0.001, LID_Z1 + 40))   # access channel through the foot for head + screwdriver
    return lid.removeSplitter()


# ---------------------------------------------------------------- build + export
def for_print(shape, flip=False):
    s = shape.copy()
    if flip:
        s.rotate(V(0, 0, 0), V(1, 0, 0), 180)
    bb = s.BoundBox
    s.translate(V(-bb.XMin, -bb.YMin, -bb.ZMin))
    return s


def export_stl(shape, path):
    import MeshPart
    mesh = MeshPart.meshFromShape(Shape=shape, LinearDeflection=0.01, AngularDeflection=0.1, Relative=False)
    mesh.write(path)


def main():
    parts = {"body": make_body(), "carrier": make_carrier(), "lid": make_lid()}
    doc = App.newDocument("FocusCase")
    for name, shape in parts.items():
        doc.addObject("Part::Feature", name).Shape = shape
    doc.recompute()
    doc.saveAs(os.path.join(HERE, "focus_case.FCStd"))

    oriented = {
        "body": for_print(parts["body"]),                 # window side on the bed
        "carrier": for_print(parts["carrier"], flip=True),  # plate on the bed, legs up
        "lid": for_print(parts["lid"]),                    # inner face on the bed, foot up
    }
    for name, shape in oriented.items():
        export_stl(shape, os.path.join(HERE, f"focus_case_{name}.stl"))
        bb = shape.BoundBox
        print(f"{name:8} valid={shape.isValid()} solids={len(shape.Solids)} "
              f"{bb.XLength:.1f} x {bb.YLength:.1f} x {bb.ZLength:.1f} mm  vol={shape.Volume / 1000:.1f} cm3")
    ob = parts["body"].BoundBox
    print(f"outer case incl. base: {ob.XLength:.1f} x {ob.YLength:.1f} x {TOP_Z - Z_FRONT_OUT:.1f} mm")


main()
