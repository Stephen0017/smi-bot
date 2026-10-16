"""Builds the 9:16 news video (silent) in the style of big news-clip channels.

What it does (learned from analysing real news TikTok videos):
  * photos: ~4 s each, slow zoom-OUT, quick push-left slide transition, card never moves
  * the headline card is compact (~70% of width) and is placed where it hides the least:
    it avoids faces and busy/moving areas
  * vertical video / photos  -> fill the whole screen ("fill" layout), card floats on top
  * landscape / square video -> shown whole, full width, with the card BELOW it on a blurred
    background ("stack" layout) so the card never covers the small video
  * long videos: the card disappears after a few seconds and a small corner logo takes over
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

import layout

ROOT = Path(__file__).parent
W, H = 1080, 1920
FPS = 30

# ---- look & feel (edit freely) ---------------------------------------------
BRAND = os.getenv("BRAND_NAME", "ስሚ Ethiopia")
BRAND_COLOR = (10, 78, 163)
BAR_H = 104                            # blue brand bar height (px)
PAD_X, PAD_Y = 34, 28
RADIUS = 12
CARD_WIDTHS = (0.70, 0.76, 0.82)       # tries narrow first, widens only for long headlines
HEADLINE_SIZES = (50, 46, 42, 38)
PREFERRED_LINES = 4
MAX_LINES = 6

CARD_PREF_TOP = int(H * 0.641)         # DEFAULT card top = 64.1% of the height (measured on the Daily Mail reference video)
CARD_MIN_TOP = int(H * 0.12)           # keep out of the top (TikTok header)
CARD_MAX_BOTTOM = int(H * 0.841)       # default card never ends lower than this (= bottom edge of the Daily Mail card, measured)
MOVE_MAX_BOTTOM = int(H * 0.90)        # when moved by hand the card may go a little lower


def default_card_y(ch):
    """Daily Mail placement: card top at 64.1% of the height; taller cards (more lines) move up so they end by 86%."""
    return max(CARD_MIN_TOP, min(CARD_PREF_TOP, CARD_MAX_BOTTOM - ch))

SIDE_MARGIN = 40                       # px gap to the screen edge for left / right positions
# name -> (horizontal: left|center|right, vertical centre of the card as a fraction of the height)
POSITION_PRESETS = {
    "up_left":    ("left",   0.24), "up":     ("center", 0.24), "up_right":    ("right",  0.24),
    "left":       ("left",   0.50), "center": ("center", 0.55), "right":       ("right",  0.50),
    "down_left":  ("left",   0.74), "down":   ("center", 0.74), "down_right":  ("right",  0.74),
    # old names, kept so buttons from older messages still work
    "top": ("center", 0.20), "upper_mid": ("center", 0.38), "mid": ("center", 0.55),
    "lower_mid": ("center", 0.68), "bottom": ("center", 0.78),
}


# ---- step-by-step moving (each click nudges the card from where it is now) ---
STEP_Y = 0.25      # one vertical click = 25% of the way from the middle to the top/bottom limit (4 clicks to the edge)
STEP_X = 0.50      # one sideways click = 50% of the free side room (2 clicks to the edge; there is little side room)
MIN_STEP_X = 24    # px


def move_card(headline, state, direction):
    """state: {"x": px or None, "y": px or None} = where the card is now (None = centred / default height).
    direction: up, down, left, right, up_left, ... or center (reset).
    Returns (new_state, moved)."""
    with tempfile.TemporaryDirectory() as td:
        _, cw, ch = make_card(headline, Path(td) / "card.png")
    state = state or {}
    x0 = state.get("x"); y0 = state.get("y")
    x0 = (W - cw) // 2 if x0 is None else int(x0)
    y0 = default_card_y(ch) if y0 is None else int(y0)
    cx, cy = (W - cw) // 2, default_card_y(ch)
    if direction == "center":
        return {"x": cx, "y": cy}, (x0, y0) != (cx, cy)

    xmin, xmax = SIDE_MARGIN, W - cw - SIDE_MARGIN
    ymin = CARD_MIN_TOP
    ymax = min(H - ch - 20, MOVE_MAX_BOTTOM - ch)
    sy = max(1, int(STEP_Y * (ymax - ymin) / 2))
    sx = max(MIN_STEP_X, int(STEP_X * max(xmax - xmin, 0) / 2))

    x, y = x0, y0
    if "up" in direction and y0 > ymin:
        y = max(ymin, y0 - sy)
    if "down" in direction and y0 < ymax:
        y = min(ymax, y0 + sy)
    if "left" in direction and x0 > xmin:
        x = max(xmin, x0 - sx)
    if "right" in direction and x0 < xmax:
        x = min(xmax, x0 + sx)
    return {"x": x, "y": y}, (x, y) != (x0, y0)


def _write_meta(bg_cache_path, **kw):
    if not bg_cache_path:
        return
    p = Path(str(bg_cache_path) + ".json")
    try:
        d = json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        d = {}
    d.update(kw)
    try:
        p.write_text(json.dumps(d))
    except Exception:
        pass


def resolve_card_y(pos, card_h):
    """Resolve a position preset or ratio or int y into canvas pixel top."""
    if pos is None:
        return None
    if isinstance(pos, (tuple, list)):
        return int(pos[1])
    if isinstance(pos, str) and pos in POSITION_PRESETS:
        return int(H * POSITION_PRESETS[pos][1] - card_h / 2)
    try:
        val = float(pos)
        if 0.0 < val <= 1.0:
            return int(H * val - card_h / 2)
        return int(val)
    except (ValueError, TypeError):
        return None


def resolve_card_x(pos, card_w):
    """Left edge of the card in px (None = centred)."""
    if isinstance(pos, (tuple, list)):
        return int(pos[0])
    if isinstance(pos, str) and pos in POSITION_PRESETS:
        side = POSITION_PRESETS[pos][0]
        if side == "left":
            return SIDE_MARGIN
        if side == "right":
            return max(SIDE_MARGIN, W - card_w - SIDE_MARGIN)
    return None


SECONDS_PER_IMAGE = float(os.getenv("SECONDS_PER_IMAGE", "4"))
SINGLE_IMAGE_SECONDS = float(os.getenv("SINGLE_IMAGE_SECONDS", "8"))
TRANSITION = 0.35                      # push-left slide between photos
ZOOM_OUT = 1.10                        # photo starts 10% zoomed in, ends at 100%
MAX_PHOTOS = int(os.getenv("MAX_PHOTOS", "6"))

MAX_VIDEO_SECONDS = float(os.getenv("MAX_VIDEO_SECONDS", "60"))
CARD_SECONDS = float(os.getenv("CARD_SECONDS", "4"))               # on long videos the card shows this long
LONG_VIDEO_SECONDS = float(os.getenv("LONG_VIDEO_SECONDS", "15"))   # longer than this -> card is timed
KEEP_VIDEO_AUDIO = True               # original sound of the channel video is always kept

FILL_MAX_ASPECT = 0.75                 # videos with width/height above this get the "stack" layout
STACK_GAP = 36                         # space between video and card in stack layout
FORCE_LAYOUT = os.getenv("FORCE_LAYOUT", "auto")   # auto | fill | stack

ETH_FONT = ROOT / "fonts" / "NotoSansEthiopic-Bold.ttf"
LAT_FONT = ROOT / "fonts" / "NotoSans-Bold.ttf"


# ---- text helpers (Ethiopic + Latin mixed) ----------------------------------
def _is_eth(ch):
    o = ord(ch)
    return 0x1200 <= o <= 0x139F or 0x2D80 <= o <= 0x2DDF or 0xAB00 <= o <= 0xAB2F


def _runs(text, size):
    eth = ImageFont.truetype(str(ETH_FONT), size)
    lat = ImageFont.truetype(str(LAT_FONT), size)
    out, cur, cur_f = [], "", None
    for ch in text:
        f = eth if _is_eth(ch) else lat
        if cur_f is not None and f is not cur_f:
            out.append((cur, cur_f)); cur = ""
        cur += ch; cur_f = f
    if cur:
        out.append((cur, cur_f))
    return out


def text_width(text, size):
    return sum(f.getlength(t) for t, f in _runs(text, size))


def draw_text(draw, xy, text, size, fill):
    x, y = xy
    for t, f in _runs(text, size):
        draw.text((x, y), t, font=f, fill=fill)
        x += f.getlength(t)


def wrap(text, size, max_w):
    lines, cur = [], ""
    for w in text.split():
        trial = f"{cur} {w}".strip()
        if text_width(trial, size) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur); cur = w
    if cur:
        lines.append(cur)
    return lines


# ---- emoji removal (the card font cannot draw emoji, and they look wrong on a news card) --------------
import re as _re
_EMOJI = _re.compile(
    "[0-9#*]\uFE0F?\u20E3"                                   # keycaps  1️⃣ #️⃣
    "|[\U0001F1E6-\U0001F1FF]"                               # flag letters
    "|[\U0001F000-\U0001FAFF]"                               # pictographs, faces, symbols, objects, transport...
    "|[\U000E0020-\U000E007F]"                               # flag tag characters
    "|[\u2600-\u27BF]"                                       # misc symbols + dingbats (☀ ✔ ❗ ✈ ...)
    "|[\u2B00-\u2BFF]"                                       # arrows / stars (⬆ ⭐ ⭕ ...)
    "|[\u2300-\u23FF]"                                       # misc technical (⏰ ⌚ ⏳ ...)
    "|[\u25AA-\u25FE]|[\u2934\u2935\u203C\u2049\u24C2\u3030\u303D\u3297\u3299]"
    "|[\uFE0E\uFE0F\u200D\u20E3]"                          # variation selectors, joiner, keycap mark
)


def strip_emoji(text):
    """Remove emoji from the headline. Falls back to the original text if nothing else is left."""
    if not _EMOJI.search(text or ""):
        return text                      # no emoji: leave the headline exactly as it is
    cleaned = _EMOJI.sub("", text or "")
    cleaned = _re.sub(r"[ \t]+", " ", cleaned)
    cleaned = _re.sub(r"\s+([,.:;!?\u1362\u1363\u1364\u1365\u1366\u1367\u1368])", r"\1", cleaned).strip()
    return cleaned or (text or "").strip()


# ---- brand logo (logo.png next to this file; falls back to the plain text brand if it is missing) ---------
LOGO_PATH = Path(os.getenv("LOGO_FILE", str(ROOT / "logo.png")))
_LOGO = {}


def _load_logo():
    """Returns (logo image cropped to its artwork, background colour) or None."""
    if "v" in _LOGO:
        return _LOGO["v"]
    val = None
    try:
        import numpy as np
        im = Image.open(LOGO_PATH).convert("RGB")
        a = np.asarray(im).astype(int)
        edge = np.concatenate([a[:10].reshape(-1, 3), a[-10:].reshape(-1, 3),
                               a[:, :10].reshape(-1, 3), a[:, -10:].reshape(-1, 3)])
        bg = tuple(int(v) for v in np.median(edge, axis=0))
        mask = np.abs(a - np.array(bg)).max(axis=2) > 60
        ys, xs = np.where(mask)
        if len(xs):
            val = (im.crop((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)), bg)
    except Exception as e:
        print("logo not used:", e)
    _LOGO["v"] = val
    return val


# ---- card + watermark ---------------------------------------------------------
def make_card(headline, out_png):
    """Compact card: brand bar + white headline box.  Returns (path, width, height)."""
    headline = " ".join(strip_emoji(headline).split())
    chosen = None
    for frac in CARD_WIDTHS:
        cw = int(W * frac)
        inner = cw - 2 * PAD_X
        for size in HEADLINE_SIZES:
            lines = wrap(headline, size, inner)
            if len(lines) <= PREFERRED_LINES:
                chosen = (cw, size, lines); break
        if chosen:
            break
    if not chosen:                         # very long headline: widest card, smallest font
        cw = int(W * CARD_WIDTHS[-1]); size = HEADLINE_SIZES[-1]
        lines = wrap(headline, size, cw - 2 * PAD_X)
        if len(lines) > MAX_LINES:
            lines = lines[:MAX_LINES]; lines[-1] = lines[-1].rstrip() + "…"
        chosen = (cw, size, lines)
    cw, size, lines = chosen
    line_h = int(size * 1.5)
    ch = BAR_H + PAD_Y * 2 + line_h * len(lines)

    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, cw - 1, ch - 1), RADIUS, fill=(255, 255, 255, 255))
    logo = _load_logo()
    header_col = logo[1] if logo else BRAND_COLOR
    d.rounded_rectangle((0, 0, cw - 1, BAR_H + RADIUS), RADIUS, fill=tuple(header_col) + (255,))
    d.rectangle((0, BAR_H, cw - 1, BAR_H + RADIUS), fill=(255, 255, 255, 255))
    if logo:
        lim = logo[0]
        s = min((cw - 80) / lim.width, (BAR_H - 32) / lim.height)
        lw, lh = max(1, int(lim.width * s)), max(1, int(lim.height * s))
        img.paste(lim.resize((lw, lh), Image.LANCZOS), ((cw - lw) // 2, (BAR_H - lh) // 2))
    else:
        bs = 62
        bw = text_width(BRAND, bs)
        draw_text(d, ((cw - bw) / 2, (BAR_H - bs * 1.45) / 2 + 2), BRAND, bs, (255, 255, 255))
    y = BAR_H + PAD_Y
    for ln in lines:
        draw_text(d, (PAD_X, y), ln, size, (15, 15, 15))
        y += line_h
    img.save(out_png)
    return Path(out_png), cw, ch


WM_OPACITY = float(os.getenv("WATERMARK_OPACITY", "0.62"))   # 0 = invisible, 1 = solid white
WM_WIDTH = int(os.getenv("WATERMARK_WIDTH", "300"))          # width of the corner mark in px (video is 1080 wide)


def make_watermark(out_png):
    """Daily Mail-style corner mark: ONLY the white text of the logo (no blue box, no flag/line),
    semi-transparent, with a very soft shadow so it stays readable on bright footage."""
    try:
        import numpy as np
        im = Image.open(LOGO_PATH).convert("RGB")
        a = np.asarray(im).astype(float)
        bgmin = float(np.median(np.concatenate([a[:10].reshape(-1, 3), a[-10:].reshape(-1, 3)]).min(axis=1)))
        # white text = all three channels high; blue box, yellow line and flag colours have a low minimum channel
        white = np.clip((a.min(axis=2) - (bgmin + 40)) / (255 - bgmin - 40), 0, 1)
        ys, xs = np.where(white > 0.5)
        # keep only the text row: drop the line/flag strip that sits under the text
        rows = np.where((white > 0.5).sum(axis=1) > 0)[0]
        gaps = np.where(np.diff(rows) > 8)[0]
        if len(gaps):
            rows = rows[: gaps[0] + 1]
        y0, y1 = int(rows.min()), int(rows.max()) + 1
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        alpha = (white[y0:y1, x0:x1] * 255).astype("uint8")
        text = Image.new("RGBA", alpha.shape[::-1], (255, 255, 255, 0))
        text.putalpha(Image.fromarray(alpha))
        s = WM_WIDTH / text.width
        text = text.resize((WM_WIDTH, max(1, int(text.height * s))), Image.LANCZOS)
        al = text.getchannel("A").point(lambda v: int(v * WM_OPACITY))
        text.putalpha(al)
        m = 14
        canvas = Image.new("RGBA", (text.width + 2 * m, text.height + 2 * m), (0, 0, 0, 0))
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        sh_a = al.point(lambda v: int(v * 0.45))
        shadow.paste((0, 0, 0, 255), (m, m + 2), sh_a)
        shadow = shadow.filter(ImageFilter.GaussianBlur(3))
        canvas = Image.alpha_composite(canvas, shadow)
        canvas.alpha_composite(text, (m, m))
        canvas.save(out_png)
        return Path(out_png)
    except Exception as e:
        print("watermark from logo failed, using plain text:", e)
    parts = BRAND.split(" ", 1)
    size = 58
    wmax = int(max(text_width(p, size) for p in parts)) + 40
    hh = int(size * 1.5 * len(parts)) + 30
    base = Image.new("RGBA", (wmax, hh), (0, 0, 0, 0))
    for p_i, p in enumerate(parts):
        draw_text(ImageDraw.Draw(base), (16, 10 + p_i * int(size * 1.5)), p, size, (255, 255, 255, int(255 * WM_OPACITY)))
    base.save(out_png)
    return Path(out_png)


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + r.stderr[-2500:])


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height:format=duration", "-of", "default=nw=1", str(path)],
                       capture_output=True, text=True)
    d = dict(l.split("=") for l in r.stdout.split())
    return int(d["width"]), int(d["height"]), float(d["duration"])


def _even(v):
    return int(v) // 2 * 2


def _zoom_out_clip(src_img, dur, out_w, out_h, dst, zoom=ZOOM_OUT):
    """Slow zoom-out clip from an image already sized >= 2x the output (keeps it smooth)."""
    frames = int(round(dur * FPS))
    step = (1.0 - zoom) / frames
    zexpr = f"{zoom}+({step:.7f})*on"
    _run(["ffmpeg", "-y", "-loop", "1", "-i", str(src_img), "-vf",
          f"zoompan=z='{zexpr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={out_w}x{out_h}:fps={FPS},format=yuv420p",
          "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-an", str(dst)])


def _join_slide(segs, disp, dst):
    """Join clips with a push-left slide.  Each clip is disp+TRANSITION long."""
    if len(segs) == 1:
        shutil.copy(segs[0], dst); return
    cmd = ["ffmpeg", "-y"]
    for s in segs:
        cmd += ["-i", str(s)]
    chain, prev = [], "[0:v]"
    for i in range(1, len(segs)):
        out = f"[x{i}]"
        chain.append(f"{prev}[{i}:v]xfade=transition=slideleft:duration={TRANSITION}:offset={i * disp:.3f}{out}")
        prev = out
    cmd += ["-filter_complex", ";".join(chain), "-map", prev, "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "17", "-pix_fmt", "yuv420p", "-an", str(dst)]
    _run(cmd)


def _sample_bg(bg, n=12):
    return layout.sample_frames(bg, n) if layout.available() else []


def _has_audio(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(p.stdout.strip())


def _final(bg, card_png, card_y, out, card_until=None, wm_png=None, audio=False, card_x=None):
    """Overlay card (always, or only until `card_until` s, then the watermark).
    The result ALWAYS has an audio track (the original sound, or silence for photo posts), because Telegram
    shows a video without any audio track as a GIF ("Save GIF") instead of a normal video."""
    cx = "(W-w)/2" if card_x is None else str(int(card_x))
    cmd = ["ffmpeg", "-y", "-i", str(bg), "-i", str(card_png)]
    if card_until is not None and wm_png:
        cmd += ["-i", str(wm_png)]
        wx, wy = 70, 250
        fc = (f"[0:v][1:v]overlay=x={cx}:y={card_y}:enable='lte(t,{card_until})'[a];"
              f"[a][2:v]overlay=x={wx}:y={wy}:enable='gt(t,{card_until})',format=yuv420p[v]")
    else:
        fc = f"[0:v][1:v]overlay=x={cx}:y={card_y},format=yuv420p[v]"
    has_a = _has_audio(bg)
    if not has_a:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        silent_idx = cmd.count("-i") - 1
    cmd += ["-filter_complex", fc, "-map", "[v]"]
    if has_a:
        cmd += ["-map", "0:a", "-c:a", "copy"]
    else:
        cmd += ["-map", f"{silent_idx}:a", "-c:a", "aac", "-b:a", "64k", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-maxrate", "2500k", "-bufsize", "5000k", "-movflags", "+faststart", str(out)]
    _run(cmd)


# ============================ PHOTOS ===========================================
def _load_cv(path):
    if not layout.available():
        return None
    import cv2
    return cv2.imread(str(path))


def _analyse_image(path):
    im = Image.open(path)
    iw, ih = im.size
    faces = []
    bgr = _load_cv(path)
    if bgr is not None:
        faces = [layout.expand(b, iw, ih) for b in layout.detect_faces(bgr)]
    return {"path": Path(path), "w": iw, "h": ih, "aspect": iw / ih, "faces": faces}


def _fillable(info):
    """Can we crop this photo to 9:16 without cutting the people out?"""
    r = max(W / info["w"], H / info["h"])
    fu = layout.face_union(info["faces"])
    if fu:
        return (fu[2] - fu[0]) * r <= 0.92 * W
    return info["aspect"] <= 1.25


def _fill_cover(info, dst, scale=2.0):
    """Crop+resize the photo to cover (scale*W x scale*H), centred on the faces if any."""
    tw, th = int(W * scale), int(H * scale)
    im = Image.open(info["path"]).convert("RGB")
    r = max(tw / im.width, th / im.height)
    im = im.resize((int(im.width * r) + 1, int(im.height * r) + 1), Image.LANCZOS)
    fu = layout.face_union(info["faces"])
    if fu:
        cx = (fu[0] + fu[2]) / 2 * r
        cy = (fu[1] + fu[3]) / 2 * r
    else:
        cx, cy = im.width / 2, im.height / 2
    left = min(max(cx - tw / 2, 0), im.width - tw)
    top = min(max(cy - th * 0.38, 0), im.height - th) if fu else (im.height - th) / 2
    im.crop((int(left), int(top), int(left) + tw, int(top) + th)).save(dst, quality=95)


def _blur_bg(src, dst):
    im = Image.open(src).convert("RGB")
    r = max(W / im.width, H / im.height)
    im = im.resize((int(im.width * r) + 1, int(im.height * r) + 1))
    l, t = (im.width - W) // 2, (im.height - H) // 2
    im = im.crop((l, t, l + W, t + H)).filter(ImageFilter.GaussianBlur(40))
    im = Image.eval(im, lambda v: int(v * 0.72))
    im.save(dst, quality=90)


def render_images(images, headline, out, card_pos=None, bg_cache_path=None):
    out = Path(out)
    images = list(images)[:MAX_PHOTOS]
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        card_png, cw, ch = make_card(headline, td / "card.png")
        infos = [_analyse_image(p) for p in images]
        mode = FORCE_LAYOUT if FORCE_LAYOUT in ("fill", "stack") else (
            "fill" if all(_fillable(i) for i in infos) else "stack")
        disp = SECONDS_PER_IMAGE if len(images) > 1 else SINGLE_IMAGE_SECONDS
        dur = disp + (TRANSITION if len(images) > 1 else 0)
        segs = []

        if mode == "fill":
            for n, info in enumerate(infos):
                c = td / f"c{n}.jpg"; _fill_cover(info, c)
                s = td / f"s{n}.mp4"; _zoom_out_clip(c, dur, W, H, s)
                segs.append(s)
        else:
            boxh_max = H - ch - STACK_GAP - 220
            boxh = _even(min(boxh_max, max(W / i["aspect"] for i in infos)))
            boxh = max(boxh, 540)
            y0 = _even((H - (boxh + STACK_GAP + ch)) / 2 - 50)
            for n, info in enumerate(infos):
                bw = _even(min(W, boxh * info["aspect"])); bh = _even(bw / info["aspect"])
                bg = td / f"bg{n}.jpg"; _blur_bg(info["path"], bg)
                big = td / f"fg{n}.jpg"
                Image.open(info["path"]).convert("RGB").resize((bw * 2, bh * 2), Image.LANCZOS).save(big, quality=95)
                fg = td / f"fg{n}.mp4"; _zoom_out_clip(big, dur, bw, bh, fg, zoom=1.06)
                s = td / f"s{n}.mp4"
                _run(["ffmpeg", "-y", "-loop", "1", "-i", str(bg), "-i", str(fg), "-filter_complex",
                      f"[0:v][1:v]overlay=x={(W - bw) // 2}:y={y0 + (boxh - bh) // 2}:shortest=1,format=yuv420p",
                      "-frames:v", str(int(round(dur * FPS))), "-c:v", "libx264", "-preset", "veryfast",
                      "-crf", "17", "-an", str(s)])
                segs.append(s)

        bgvid = td / "bg.mp4"
        _join_slide(segs, disp, bgvid)

        if bg_cache_path:
            try:
                shutil.copy(bgvid, bg_cache_path)
            except Exception as e:
                print("Failed to cache bgvid:", e)

        custom_y = resolve_card_y(card_pos, ch)
        if custom_y is not None:
            card_y = max(CARD_MIN_TOP, min(custom_y, H - ch - 20))
        elif mode == "fill":
            card_y = default_card_y(ch)
        else:
            card_y = default_card_y(ch)   # always the Daily Mail spot (the card may overlap the photo, like the reference)
        _write_meta(bg_cache_path, timed=False, card_x=resolve_card_x(card_pos, cw) or (W - cw) // 2, card_y=card_y)
        if layout.DEBUG:
            print(f"[images] layout={mode} card {cw}x{ch} top={card_y}")
        _final(bgvid, card_png, card_y, out, card_x=resolve_card_x(card_pos, cw))
    return out


# ============================ VIDEO ============================================
def render_video(video, headline, out, card_pos=None, bg_cache_path=None):
    video, out = Path(video), Path(out)
    w, h, dur = probe(video)
    dur = min(dur, MAX_VIDEO_SECONDS)
    aspect = w / h
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        card_png, cw, ch = make_card(headline, td / "card.png")
        mode = FORCE_LAYOUT if FORCE_LAYOUT in ("fill", "stack") else (
            "fill" if aspect <= FILL_MAX_ASPECT else "stack")
        bgvid = td / "bg.mp4"
        base = ["ffmpeg", "-y", "-i", str(video), "-t", f"{dur:.2f}"]
        tail = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"] + \
               (["-map", "0:a?", "-c:a", "aac"] if KEEP_VIDEO_AUDIO else ["-an"]) + [str(bgvid)]

        if mode == "fill":
            if aspect >= W / H:                       # crop width: follow the faces
                sw, sh = _even(H * aspect), H
            else:                                     # crop height
                sw, sh = W, _even(W / aspect)
            cx = (sw - W) / 2
            cy = (sh - H) * 0.35                      # keep the upper part (heads), trim the bottom
            if layout.available() and sw > W:
                faces = []
                for f in layout.sample_frames(video, 8, (sw, sh)):
                    faces += [layout.expand(b, sw, sh) for b in layout.detect_faces(f)]
                fu = layout.face_union(faces)
                if fu:
                    cx = min(max((fu[0] + fu[2]) / 2 - W / 2, 0), sw - W)
            vf = (f"scale={sw}:{sh},crop={W}:{H}:{int(cx)}:{int(cy)},fps={FPS},format=yuv420p")
            _run(base + ["-map", "0:v:0", "-vf", vf] + tail)
            custom_y = resolve_card_y(card_pos, ch)
            if custom_y is not None:
                card_y = max(CARD_MIN_TOP, min(custom_y, H - ch - 20))
            else:
                card_y = default_card_y(ch)
            timed = dur > LONG_VIDEO_SECONDS
        else:
            boxh_max = H - ch - STACK_GAP - 220
            bh = _even(min(W / aspect, boxh_max)); bw = _even(bh * aspect)
            y0 = _even((H - (bh + STACK_GAP + ch)) / 2 - 50)
            fc = (f"[0:v]split[a][b];"
                  f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=40:6,eq=brightness=-0.12[bgv];"
                  f"[b]scale={bw}:{bh}[fg];[bgv][fg]overlay={(W - bw) // 2}:{y0 + 0}[o];[o]fps={FPS},format=yuv420p[v]")
            _run(base + ["-filter_complex", fc, "-map", "[v]"] + tail)
            custom_y = resolve_card_y(card_pos, ch)
            if custom_y is not None:
                card_y = max(CARD_MIN_TOP, min(custom_y, H - ch - 20))
            else:
                card_y = default_card_y(ch)   # always the Daily Mail spot (the card may overlap the video, like the reference)
            timed = False                               # card never covers the video: keep it all along

        if bg_cache_path:
            try:
                shutil.copy(bgvid, bg_cache_path)
            except Exception as e:
                print("Failed to cache bgvid:", e)

        _write_meta(bg_cache_path, timed=bool(timed), card_x=resolve_card_x(card_pos, cw) or (W - cw) // 2, card_y=card_y)
        if layout.DEBUG:
            print(f"[video] {w}x{h} layout={mode} card {cw}x{ch} top={card_y} timed={timed}")
        if timed:
            wm = make_watermark(td / "wm.png")
            _final(bgvid, card_png, card_y, out, card_until=CARD_SECONDS, wm_png=wm, audio=KEEP_VIDEO_AUDIO, card_x=resolve_card_x(card_pos, cw))
        else:
            _final(bgvid, card_png, card_y, out, audio=KEEP_VIDEO_AUDIO, card_x=resolve_card_x(card_pos, cw))
    return out


def re_render_fast(bgvid_path, headline, out, card_pos="mid"):
    """Instantly re-renders the final video from cached background video in 1-2 seconds."""
    out = Path(out)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        card_png, cw, ch = make_card(headline, td / "card.png")
        _y = resolve_card_y(card_pos, ch)
        card_y = default_card_y(ch) if _y is None else _y
        card_y = max(CARD_MIN_TOP, min(card_y, H - ch - 20))
        timed = False
        try:
            timed = json.loads(Path(str(bgvid_path) + ".json").read_text()).get("timed", False)
        except Exception:
            pass
        if timed:
            wm = make_watermark(td / "wm.png")
            _final(bgvid_path, card_png, card_y, out, card_until=CARD_SECONDS, wm_png=wm, audio=KEEP_VIDEO_AUDIO, card_x=resolve_card_x(card_pos, cw))
        else:
            _final(bgvid_path, card_png, card_y, out, audio=KEEP_VIDEO_AUDIO, card_x=resolve_card_x(card_pos, cw))
    return out
