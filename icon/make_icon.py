"""Renders the FocusInk app icon from the real case mesh (case/focus_case_body.stl).

The case is stood on its wedge base (screen tilted up 15°) and drawn as a flat side profile:
the case silhouette, the wedge base in a darker tone, and a dark sliver for the display on the
front face. Supersampled, on a macOS-style rounded-square background. Writes icon/FocusInk.icns.
(render() is the older 3/4 shaded view, kept for reference.)

Run:  ../.venv/bin/python make_icon.py
"""
import math
import struct
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
STL = HERE.parent / "case" / "focus_case_body.stl"

# Case geometry, in the STL's (print-oriented) coordinates — see case/focus_case.py
TILT = math.radians(15)
H = 26.8                       # front face to back rim
FOOT_BACK = np.array([7.2, 27.1])   # (y, z) where the foot meets the back
WIN_C = (20.2, 35.6)           # display window centre (x, y)
SCREEN_Z = 1.8                 # display glass, just behind the front wall
SCREEN = 28.0                  # fills the window opening

YAW = math.radians(-32)        # turn so the front (screen) and the button side show
PITCH = math.radians(5)        # look down a little
SS = 2048                      # supersampled canvas


def load_stl(path):
    d = path.read_bytes()
    n = struct.unpack("<I", d[80:84])[0]
    rec = np.frombuffer(d[84:84 + n * 50], dtype=np.dtype([("n", "<3f4"), ("v", "<9f4"), ("a", "<u2")]))
    return rec["v"].reshape(-1, 3, 3).astype(np.float64)


def to_standing(p):
    """STL coords -> desk coords: X sideways, U depth (towards the back), V up."""
    y, z = p[..., 1] - FOOT_BACK[0], p[..., 2] - FOOT_BACK[1]
    # foot line runs from the back-bottom corner towards the front; rotate it onto the desk
    ang = math.atan2(-H, -H * math.tan(TILT))           # direction of the foot line in (y, z)
    c, s = math.cos(-ang), math.sin(-ang)
    u = y * c - z * s
    v = y * s + z * c
    return np.stack([-p[..., 0], -u, v], axis=-1)    # X mirrored to keep it right-handed


def view(p):
    x, u, v = p[..., 0], p[..., 1], p[..., 2]
    cy, sy = math.cos(YAW), math.sin(YAW)
    x2, d = x * cy - u * sy, x * sy + u * cy           # yaw around the vertical
    cp, sp = math.cos(PITCH), math.sin(PITCH)
    v2, d2 = v * cp - d * sp, v * sp + d * cp           # pitch
    return np.stack([x2, v2, d2], axis=-1)              # screen x, screen y (up), depth (away)


def shade(normals, base, light):
    lam = np.clip(normals @ light, 0, 1)
    k = 0.42 + 0.58 * lam
    return np.clip(np.outer(k, base), 0, 255).astype(int)


def render():
    raw = load_stl(STL)
    nrm = np.cross(raw[:, 1] - raw[:, 0], raw[:, 2] - raw[:, 0])
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-12
    o = view(to_standing(np.zeros((1, 3))))[0]
    n = view(to_standing(nrm)) - o                        # outward normals in view space
    tris = view(to_standing(raw))
    sv = view(to_standing(np.array([[[WIN_C[0] - SCREEN / 2, WIN_C[1] - SCREEN / 2, SCREEN_Z],
                                     [WIN_C[0] + SCREEN / 2, WIN_C[1] - SCREEN / 2, SCREEN_Z],
                                     [WIN_C[0] + SCREEN / 2, WIN_C[1] + SCREEN / 2, SCREEN_Z],
                                     [WIN_C[0] - SCREEN / 2, WIN_C[1] + SCREEN / 2, SCREEN_Z]]])))[0]

    pts = tris[..., :2]
    lo, hi = pts.reshape(-1, 2).min(0), pts.reshape(-1, 2).max(0)
    scale = 0.66 * SS / (hi - lo).max()
    off = np.array([SS / 2, SS * 0.53]) - scale * (lo + hi) / 2 * np.array([1, -1])

    def px(p):
        return np.stack([p[..., 0] * scale + off[0], -p[..., 1] * scale + off[1]], axis=-1)

    facing = n[:, 2] < 0                                  # towards the camera (depth axis points away)
    light = np.array([-0.5, 0.7, -0.5]); light /= np.linalg.norm(light)   # upper left, in front
    cols = shade(n, np.array([226, 214, 190]), light)    # warm retro beige

    img = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # soft contact shadow under the foot
    shadow = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    base_y = px(tris[..., :2]).reshape(-1, 2)[:, 1].max()
    cx = SS / 2
    sd.ellipse([cx - SS * 0.34, base_y - SS * 0.035, cx + SS * 0.34, base_y + SS * 0.035], fill=(0, 0, 0, 110))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(SS * 0.02)))

    order = np.argsort(-tris[:, :, 2].mean(1))           # far to near
    P = px(tris[..., :2])
    for i in order:
        if not facing[i]:
            continue
        r, g, b = cols[i]
        draw.polygon([tuple(q) for q in P[i]], fill=(r, g, b, 255))

    # e-paper screen seen through the window: paper white with a tiny focus-timer UI
    S = px(sv[:, :2])
    draw.polygon([tuple(q) for q in S], fill=(238, 238, 230, 255))

    def lerp(a, b, t):
        return a + (b - a) * t

    def quad(u0, v0, u1, v1, fill):
        # u along the screen's top edge (S0->S1), v down the screen (S3->S0 reversed)
        tr, tl, bl, br = S[3], S[2], S[1], S[0]           # screen's top is the +y side in case coords
        def at(u, v):
            top = lerp(tl, tr, u); bot = lerp(bl, br, u)
            return tuple(lerp(top, bot, v))
        draw.polygon([at(u0, v0), at(u1, v0), at(u1, v1), at(u0, v1)], fill=fill)

    ink = (28, 28, 30, 255)
    quad(0.0, 0.0, 1.0, 0.13, ink)                        # header bar
    quad(0.08, 0.22, 0.80, 0.30, ink)                     # task name
    quad(0.08, 0.35, 0.55, 0.43, ink)
    quad(0.22, 0.52, 0.78, 0.70, ink)                     # big timer
    quad(0.06, 0.78, 0.94, 0.83, ink)                     # progress bar
    quad(0.07, 0.79, 0.60, 0.82, (238, 238, 230, 255))
    quad(0.08, 0.79, 0.59, 0.82, ink)
    quad(0.08, 0.89, 0.66, 0.95, (120, 120, 120, 255))    # next task
    return img


BASE_Y = 7.7                   # STL y below which the mesh is the wedge base (under the groove)


def render_side():
    raw = load_stl(STL)
    st = to_standing(raw)                                  # X sideways, U depth (front < 0), V up
    prof = np.stack([-st[..., 1], st[..., 2]], axis=-1)   # front faces right
    lo, hi = prof.reshape(-1, 2).min(0), prof.reshape(-1, 2).max(0)
    scale = 0.64 * SS / (hi - lo).max()
    off = np.array([SS / 2, SS * 0.52]) - scale * (lo + hi) / 2 * np.array([1, -1])

    def px(p):
        return np.stack([p[..., 0] * scale + off[0], -p[..., 1] * scale + off[1]], axis=-1)

    img = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))

    # soft contact shadow under the foot
    P = px(prof)
    floor = P.reshape(-1, 2)[:, 1].max()
    x0, x1 = P.reshape(-1, 2)[:, 0].min(), P.reshape(-1, 2)[:, 0].max()
    sh = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse([x0 - SS * 0.03, floor - SS * 0.02, x1 + SS * 0.03, floor + SS * 0.025], fill=(0, 0, 0, 120))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(SS * 0.015)))

    draw = ImageDraw.Draw(img)
    is_base = raw[..., 1].mean(1) < BASE_Y
    body_col, base_col = (228, 216, 192, 255), (186, 172, 148, 255)
    for i in np.where(~is_base)[0]:
        draw.polygon([tuple(q) for q in P[i]], fill=body_col)
    for i in np.where(is_base)[0]:
        draw.polygon([tuple(q) for q in P[i]], fill=base_col)

    # display: a dark sliver on the front face where the window is
    y0, y1 = WIN_C[1] - SCREEN / 2, WIN_C[1] + SCREEN / 2
    quad = np.array([[0, y0, -0.05], [0, y1, -0.05], [0, y1, 1.1], [0, y0, 1.1]], dtype=float)
    q = px((lambda s_: np.stack([-s_[:, 1], s_[:, 2]], axis=-1))(to_standing(quad)))
    draw.polygon([tuple(v) for v in q], fill=(34, 36, 40, 255))
    return img


def background():
    bg = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    grad = Image.linear_gradient("L").resize((SS, SS))
    top, bottom = np.array([58, 78, 112]), np.array([22, 30, 48])
    g = np.asarray(grad, dtype=np.float64)[..., None] / 255.0
    rgb = (top * (1 - g) + bottom * g).astype(np.uint8)
    fill = Image.fromarray(np.dstack([rgb, np.full((SS, SS), 255, np.uint8)]), "RGBA")
    mask = Image.new("L", (SS, SS), 0)
    m = SS * 0.098                                        # Big Sur grid: 824/1024 rounded square
    ImageDraw.Draw(mask).rounded_rectangle([m, m, SS - m, SS - m], radius=SS * 0.18, fill=255)
    bg.paste(fill, (0, 0), mask)
    return bg


def main():
    icon = background()
    icon.alpha_composite(render_side())
    icon = icon.resize((1024, 1024), Image.LANCZOS)
    icon.save(HERE / "FocusInk.png")
    with tempfile.TemporaryDirectory() as tmp:
        iset = Path(tmp) / "FocusInk.iconset"
        iset.mkdir()
        for s in (16, 32, 128, 256, 512):
            icon.resize((s, s), Image.LANCZOS).save(iset / f"icon_{s}x{s}.png")
            icon.resize((s * 2, s * 2), Image.LANCZOS).save(iset / f"icon_{s}x{s}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iset), "-o", str(HERE / "FocusInk.icns")], check=True)
    print("wrote", HERE / "FocusInk.icns")


if __name__ == "__main__":
    main()
