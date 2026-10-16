"""Smart card placement: finds faces and busy/moving areas, then picks the vertical
position for the headline card where it hides the least.  Needs opencv (optional:
without it the card simply uses its default position)."""
import os

import numpy as np

try:
    import cv2
except ImportError:          # the bot still works, just without face awareness
    cv2 = None

DEBUG = os.getenv("DEBUG_LAYOUT", "0") == "1"
_backend = None          # ("cv2", (front, profile)) | ("skimage", cascade) | ("none", None)
LBP_XML = os.path.join(os.path.dirname(__file__), "models", "lbpcascade_frontalface.xml")


def available():
    return cv2 is not None


def _get_backend():
    """Pick the best face detector that works on this PC."""
    global _backend
    if _backend is not None:
        return _backend
    _backend = ("none", None)
    if cv2 is not None and hasattr(cv2, "CascadeClassifier"):          # OpenCV 4.x (recommended)
        try:
            d = getattr(getattr(cv2, "data", None), "haarcascades", "")
            front = cv2.CascadeClassifier(d + "haarcascade_frontalface_alt2.xml")
            prof = cv2.CascadeClassifier(d + "haarcascade_profileface.xml")
            if front.empty():
                front = cv2.CascadeClassifier(LBP_XML)
            if not front.empty():
                _backend = ("cv2", (front, prof if not prof.empty() else None))
        except Exception as e:
            print("cv2 face detector unavailable:", e)
    if _backend[0] == "none":
        try:
            from skimage.feature import Cascade
            _backend = ("skimage", Cascade(LBP_XML))
        except Exception:
            pass
    if DEBUG:
        print("face detector backend:", _backend[0])
    return _backend


def _merge(boxes):
    boxes = sorted(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
    keep = []
    for b in boxes:
        a = (b[2] - b[0]) * (b[3] - b[1])
        dup = False
        for k in keep:
            ix = max(0, min(b[2], k[2]) - max(b[0], k[0]))
            iy = max(0, min(b[3], k[3]) - max(b[1], k[1]))
            if ix * iy > 0.4 * a:
                dup = True; break
        if not dup:
            keep.append(b)
    return keep


def detect_faces(bgr):
    """Faces as (x0, y0, x1, y1) in the pixel coordinates of `bgr`. Never raises."""
    if cv2 is None:
        return []
    try:
        kind, det = _get_backend()
        if kind == "none":
            return []
        h, w = bgr.shape[:2]
        s = 640 / w if w > 640 else (480 / w if w < 360 else 1.0)    # work at a sane size
        img = cv2.resize(bgr, (int(w * s), int(h * s))) if s != 1.0 else bgr
        g = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
        gw = g.shape[1]
        minsz = max(24, int(gw * 0.07))
        boxes = []
        if kind == "cv2":
            front, prof = det
            boxes += [(x, y, x + ww, y + hh) for x, y, ww, hh in front.detectMultiScale(g, 1.1, 5, minSize=(minsz, minsz))]
            if prof is not None:
                for flip in (False, True):
                    gg = cv2.flip(g, 1) if flip else g
                    for x, y, ww, hh in prof.detectMultiScale(gg, 1.1, 5, minSize=(minsz, minsz)):
                        if flip:
                            x = gw - x - ww
                        boxes.append((x, y, x + ww, y + hh))
        else:                                                         # skimage backend
            res = det.detect_multi_scale(g.astype("float32") / 255.0, scale_factor=1.2, step_ratio=1,
                                         min_size=(minsz, minsz), max_size=(gw, gw), min_neighbor_number=4)
            boxes += [(d["c"], d["r"], d["c"] + d["width"], d["r"] + d["height"]) for d in res]
        return [(x0 / s, y0 / s, x1 / s, y1 / s) for x0, y0, x1, y1 in _merge(boxes)]
    except Exception as e:
        if DEBUG:
            print("face detection failed:", e)
        return []


def expand(b, img_w, img_h):
    """Grow a face box to include hair and chin so the card keeps clear of them."""
    x0, y0, x1, y1 = b
    w, h = x1 - x0, y1 - y0
    return (max(0, x0 - 0.25 * w), max(0, y0 - 0.45 * h), min(img_w, x1 + 0.25 * w), min(img_h, y1 + 0.30 * h))


def face_union(boxes):
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def sample_frames(video_path, n=12, size=(540, 960)):
    """n evenly spaced frames from a video, resized to `size`."""
    cap = cv2.VideoCapture(str(video_path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    out = []
    for i in range(n):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int((i + 0.5) * total / n))
        ok, fr = cap.read()
        if ok:
            out.append(cv2.resize(fr, size))
    cap.release()
    return out


def _energy(fr):
    g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    m = cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))
    return cv2.GaussianBlur(m, (0, 0), 6)


def choose_card_top(frames, card_w, card_h, pref_top, y_min, y_max_bottom, canvas=(1080, 1920)):
    """frames: BGR frames of the finished background (any size, same aspect as canvas).
    Returns the best top-y of the card in canvas pixels."""
    if cv2 is None or not frames:
        return pref_top
    CW, CH = canvas
    fh, fw = frames[0].shape[:2]
    s = fw / CW
    cw, ch = card_w * s, card_h * s
    x0 = (fw - cw) / 2
    lo, hi = int(y_min * s), int((y_max_bottom - card_h) * s)
    if hi <= lo:
        return max(y_min, min(pref_top, y_max_bottom - card_h))

    faces = [[expand(b, fw, fh) for b in detect_faces(f)] for f in frames]
    en = [_energy(f) for f in frames]
    mo = [cv2.GaussianBlur(cv2.absdiff(cv2.cvtColor(frames[i], cv2.COLOR_BGR2GRAY),
                                       cv2.cvtColor(frames[i + 1], cv2.COLOR_BGR2GRAY)).astype(np.float32), (0, 0), 6)
          for i in range(len(frames) - 1)]
    en_mean = float(np.mean([e.mean() for e in en])) + 1e-3
    mo_mean = float(np.mean([m.mean() for m in mo])) + 1e-3 if mo else 1.0
    ints_e = [cv2.integral(e) for e in en]
    ints_m = [cv2.integral(m) for m in mo]

    def rect_mean(ii, y):
        a, b = int(x0), int(x0 + cw)
        c, d = int(y), int(y + ch)
        return (ii[d, b] - ii[c, b] - ii[d, a] + ii[c, a]) / max(1, (b - a) * (d - c))

    best, best_cost, dbg = None, 1e9, []
    pref_c = (pref_top + card_h / 2) / CH
    for y in range(lo, hi + 1, max(2, int(6 * s / 0.5))):
        per = []
        for fl in faces:
            p = 0.0
            for (a0, b0, a1, b1) in fl:
                ix = max(0, min(a1, x0 + cw) - max(a0, x0))
                iy = max(0, min(b1, y + ch) - max(b0, y))
                p += min(1.0, ix * iy / max(1.0, (a1 - a0) * (b1 - b0)))
            per.append(p)
        face_pen = 0.5 * np.mean(per) + 0.5 * np.max(per)
        detail = min(3.0, np.mean([rect_mean(i, y) for i in ints_e]) / en_mean)
        motion = min(3.0, np.mean([rect_mean(i, y) for i in ints_m]) / mo_mean) if ints_m else 0.0
        pref = abs((y + ch / 2) / fh - pref_c)
        cost = 20 * face_pen + 1.0 * detail + 0.6 * motion + 2.0 * pref
        dbg.append((y, round(face_pen, 2), round(detail, 2), round(motion, 2), round(cost, 2)))
        if cost < best_cost:
            best, best_cost = y, cost
    if DEBUG:
        print("faces per frame:", [len(f) for f in faces], "-> best top", round(best / s), "cost", round(best_cost, 2))
    return int(round(best / s))
