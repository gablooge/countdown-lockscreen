#!/usr/bin/env python3
"""Render countdown lock screen PNGs from config.yaml.

Usage:
    python make_lockscreen.py                 # render all outputs to out/
    python make_lockscreen.py --guides        # also write out/preview/*_guides.png with safe zones marked
    python make_lockscreen.py --today 2026-12-03   # preview a future day

Days remaining are counted in calendar days from today in the deadline's timezone.
The script exits non-zero if any text would land outside the safe zone.
"""
import argparse
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------- data

def tz_for(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        if name == "Asia/Jakarta":
            return timezone(timedelta(hours=7), "WIB")
        raise


def to_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def parse_target(value, tz):
    """Accept the quoted "YYYY-MM-DD HH:MM" string or a datetime that YAML parsed on its own."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
    return datetime.strptime(str(value), "%Y-%m-%d %H:%M").replace(tzinfo=tz)


def parse_now(override, tz):
    """`override` is None (real clock), a date (start of that day) or an ISO datetime
    such as "YYYY-MM-DD HH:MM", "YYYY-MM-DDTHH:MM:SS"; naive values are in `tz`."""
    if override is None:
        return datetime.now(tz)
    s = str(override).strip()
    if len(s) == 10:
        return datetime.combine(to_date(s), time.min, tzinfo=tz)
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def load_state(cfg, today_override=None):
    dl = cfg["deadline"]
    tz = tz_for(dl["timezone"])
    target = parse_target(dl["target"], tz)
    now = parse_now(today_override, tz)
    today = now.date()
    passed = now >= target

    ms = [{**m, "date": to_date(m["date"]), "title": str(m["title"]), "done_when": str(m.get("done_when") or "")}
          for m in (cfg.get("milestones") or [])]
    for a, b in zip(ms, ms[1:]):
        if b["date"] < a["date"]:
            sys.exit(f"config error: milestones out of order ({a['date']} then {b['date']})")
    if ms and ms[-1]["date"] != target.date():
        print(f"warning: last milestone ({ms[-1]['date']}) is not on the deadline ({target.date()})")

    nxt = None if passed else next((i for i, m in enumerate(ms) if m["date"] >= today), None)
    for t in [str(dl["title"])] + [m["title"] for m in ms] + [m["done_when"] for m in ms]:
        if "\N{EM DASH}" in t:
            sys.exit(f"config error: em dash in {t!r}")

    return {
        "title": str(dl["title"]),
        "target": target,
        "tzname": target.tzname() or dl["timezone"],
        "now": now,
        "today": today,
        "passed": passed,
        "days": -1 if passed else (target.date() - today).days,
        "milestones": ms,
        "next": nxt,
        "progress": cfg.get("progress") or {},
    }


def milestone_status(st, i):
    """"past", "next" or "future" for milestone i. Everything is past once the deadline passes."""
    if i == st["next"]:
        return "next"
    if st["passed"] or st["milestones"][i]["date"] < st["today"]:
        return "past"
    return "future"


# ---------------------------------------------------------------- fonts

class Fonts:
    def __init__(self, specs):
        for spec in specs:
            if Path(spec["path"]).exists():
                self.spec = spec
                break
        else:
            sys.exit("No usable font found. Add a local font under style.fonts in config.yaml.")
        self._cache = {}

    def get(self, size, bold=False):
        size = max(8, int(round(size)))
        key = (size, bold)
        if key not in self._cache:
            s = self.spec
            if "regular" in s:  # variable font with named instances
                f = ImageFont.truetype(s["path"], size)
                f.set_variation_by_name(s["bold"] if bold else s["regular"])
            elif "regular_index" in s:  # font collection
                f = ImageFont.truetype(s["path"], size, index=s["bold_index"] if bold else s["regular_index"])
            else:
                f = ImageFont.truetype(s.get("bold_path", s["path"]) if bold else s["path"], size)
            self._cache[key] = f
        return self._cache[key]


# ---------------------------------------------------------------- drawing context

class Ctx:
    """Wraps ImageDraw so every block can run in measure-only mode and records text boxes."""

    def __init__(self, img, fonts, colors, state):
        self.img = img
        self.d = ImageDraw.Draw(img)
        self.fonts = fonts
        self.c = colors
        self.st = state
        self.dry = True
        self.boxes = []

    def text(self, xy, s, font, fill, anchor="ls"):
        if self.dry:
            return
        self.d.text(xy, s, font=font, fill=fill, anchor=anchor)
        self.boxes.append((s, self.d.textbbox(xy, s, font=font, anchor=anchor)))

    def spaced(self, x, y, s, font, fill, tracking, align="left"):
        """Letter-spaced text on baseline y. Returns width."""
        w = sum(font.getlength(ch) for ch in s) + tracking * (len(s) - 1)
        if align == "right":
            x -= w
        cx = x
        for ch in s:
            self.text((cx, y), ch, font, fill)
            cx += font.getlength(ch) + tracking
        return w

    def shape(self, fn, xy, **k):
        if self.dry:
            return
        getattr(self.d, fn)(xy, **k)
        pts = [tuple(p) for p in xy] if isinstance(xy[0], (tuple, list)) else [tuple(xy[:2]), tuple(xy[2:])]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        self.boxes.append((f"<{fn}>", (min(xs), min(ys), max(xs), max(ys))))


def wrap(text, font, width):
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if font.getlength(trial) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def ellipsize(text, font, width):
    if font.getlength(text) <= width:
        return text
    while text and font.getlength(text + "...") > width:
        text = text[:-1]
    return text.rstrip() + "..."


def line_h(font):
    asc, desc = font.getmetrics()
    return asc + desc


def fmt_day(d):
    return f"{d:%b} {d.day}".upper()


# ---------------------------------------------------------------- blocks
# Each block draws at (x, y) within width w at scale sc and returns its height.

def b_header(cx, x, y, w, sc):
    st, c = cx.st, cx.c
    f = cx.fonts.get(30 * sc, bold=True)
    fm = cx.fonts.get(30 * sc)
    base = y + f.getmetrics()[0]
    cx.spaced(x, base, "DEADLINE", f, c["accent"], 6 * sc)
    t = st["target"]
    cx.text((x + w, base), f"{t.day} {t:%b} {t:%H:%M} {st['tzname']}".upper(), fm, c["muted"], anchor="rs")
    return line_h(f)


def b_title(cx, x, y, w, sc):
    f = cx.fonts.get(66 * sc, bold=True)
    lh = 66 * sc * 1.18
    asc = f.getmetrics()[0]
    lines = wrap(cx.st["title"], f, w)
    for i, ln in enumerate(lines):
        cx.text((x, y + asc + i * lh), ln, f, cx.c["text"])
    return asc + (len(lines) - 1) * lh + f.getmetrics()[1]


def b_count(cx, x, y, w, sc):
    st, c = cx.st, cx.c
    days = st["days"]
    if days > 0:
        num, l1, l2 = str(days), ("DAY" if days == 1 else "DAYS"), "LEFT"
    elif days == 0:
        num, l1, l2 = "0", "DUE", "TODAY"
    else:
        num, l1, l2 = "0", "DONE", ""
    size = 360 * sc
    while True:
        fn = cx.fonts.get(size, bold=True)
        fl = cx.fonts.get(size / 6, bold=True)
        nw = fn.getlength(num)
        gap = size * 0.1
        lw = max(fl.getlength(l1), fl.getlength(l2))
        if nw + gap + lw <= w or size < 40:
            break
        size *= 0.92
    top = -fn.getbbox(num, anchor="ls")[1]
    base = y + top
    cx.text((x - fn.getbbox(num, anchor="ls")[0], base), num, fn, c["text"])
    lx = x + nw + gap
    cx.text((lx, base), l2 or l1, fl, c["accent"])
    if l2:
        cx.text((lx, base - fl.size * 1.15), l1, fl, c["accent"])
    return top


def b_progress(cx, x, y, w, sc):
    p, c = cx.st["progress"], cx.c
    total = int(p.get("total", 0))
    if total <= 0:
        return 0
    cur = max(0, min(int(p.get("current", 0)), total))
    g = 10 * sc
    sh = 22 * sc
    sw = (w - g * (total - 1)) / total
    if sw >= 12 * sc:  # one segment per item
        for i in range(total):
            sx = x + i * (sw + g)
            cx.shape("rounded_rectangle", [sx, y, sx + sw, y + sh], radius=4 * sc,
                     fill=c["accent"] if i < cur else c["faint"])
    else:  # too many items for segments: plain bar
        cx.shape("rounded_rectangle", [x, y, x + w, y + sh], radius=4 * sc, fill=c["faint"])
        if cur:
            cx.shape("rounded_rectangle", [x, y, x + max(w * cur / total, 8 * sc), y + sh], radius=4 * sc,
                     fill=c["accent"])
    f = cx.fonts.get(30 * sc)
    base = y + sh + 22 * sc + f.getmetrics()[0]
    cx.text((x, base), f"{cur}/{total} {p.get('label', '')}".strip(), f, c["text"])
    cx.text((x + w, base), f"{total - cur} TO GO", f, c["muted"], anchor="rs")
    return sh + 22 * sc + line_h(f)


def b_divider(cx, x, y, w, sc):
    t = max(1, round(2 * sc))
    cx.shape("rectangle", [x, y, x + w, y + t], fill=cx.c["faint"])
    return t


def b_next(cx, x, y, w, sc):
    st, c = cx.st, cx.c
    fl = cx.fonts.get(30 * sc, bold=True)
    fm = cx.fonts.get(30 * sc)
    asc = fl.getmetrics()[0]
    cx.spaced(x, y + asc, "NEXT UP", fl, c["accent"], 6 * sc)
    h = line_h(fl) + 18 * sc
    if st["next"] is None:
        f = cx.fonts.get(50 * sc, bold=True)
        cx.text((x, y + h + f.getmetrics()[0]), "All milestones done", f, c["text"])
        return h + line_h(f)
    m = st["milestones"][st["next"]]
    dd = (m["date"] - st["today"]).days
    when = "TODAY" if dd == 0 else "TOMORROW" if dd == 1 else f"IN {dd} DAYS"
    cx.text((x + w, y + asc), f"{fmt_day(m['date'])}  {when}", fm, c["muted"], anchor="rs")

    ft = cx.fonts.get(50 * sc, bold=True)
    lh = 50 * sc * 1.2
    for ln in wrap(m["title"], ft, w):
        cx.text((x, y + h + ft.getmetrics()[0]), ln, ft, c["text"])
        h += lh
    if m.get("done_when"):
        fd = cx.fonts.get(30 * sc)
        lh2 = 30 * sc * 1.35
        h += 10 * sc
        for ln in wrap("Done when: " + m["done_when"], fd, w):
            cx.text((x, y + h + fd.getmetrics()[0]), ln, fd, c["muted"])
            h += lh2
    return h


def b_timeline(cx, x, y, w, sc):
    st, c = cx.st, cx.c
    ms = st["milestones"]
    if not ms:
        return 0
    row = 64 * sc
    f = cx.fonts.get(31 * sc)
    fb = cx.fonts.get(31 * sc, bold=True)
    r = 9 * sc
    dot_x = x + r * 1.9 + 2 * sc  # leave room for the ring around the next milestone
    date_x = x + 50 * sc
    title_x = date_x + f.getlength("DEC 15") + 26 * sc
    centers = [y + row * i + row / 2 for i in range(len(ms))]
    cx.shape("line", [(dot_x, centers[0]), (dot_x, centers[-1])], fill=c["faint"], width=max(1, round(3 * sc)))
    for i, (m, cy) in enumerate(zip(ms, centers)):
        status = milestone_status(st, i)
        if status == "next":
            R = r * 1.9
            cx.shape("ellipse", [dot_x - R, cy - R, dot_x + R, cy + R], outline=c["accent"], width=max(1, round(2 * sc)), fill=c["bg"])
            cx.shape("ellipse", [dot_x - r, cy - r, dot_x + r, cy + r], fill=c["accent"])
            dcol, tcol, tf = c["accent"], c["text"], fb
        elif status == "past":
            cx.shape("ellipse", [dot_x - r, cy - r, dot_x + r, cy + r], fill=c["muted"])
            dcol, tcol, tf = c["faint_text"], c["faint_text"], f
        else:
            cx.shape("ellipse", [dot_x - r, cy - r, dot_x + r, cy + r], fill=c["bg"], outline=c["muted"], width=max(1, round(3 * sc)))
            dcol, tcol, tf = c["muted"], c["text"], f
        cx.text((date_x, cy), fmt_day(m["date"]), f, dcol, anchor="lm")
        cx.text((title_x, cy), ellipsize(m["title"], tf, x + w - title_x), tf, tcol, anchor="lm")
    return row * len(ms)


# gap values are in base units (multiplied by scale)
PHONE_STACK = [(b_header, 26), (b_title, 60), (b_count, 56), (b_progress, 64),
               (b_divider, 56), (b_next, 60), (b_timeline, 0)]
DESKTOP_LEFT = [(b_header, 26), (b_title, 56), (b_count, 56), (b_progress, 0)]
DESKTOP_RIGHT = [(b_next, 48), (b_divider, 40), (b_timeline, 0)]


def stack(cx, blocks, x, y, w, sc):
    h = 0
    for fn, gap in blocks:
        bh = fn(cx, x, y + h, w, sc)
        h += bh + (gap * sc if bh else 0)
    return h


def fit_scale(cx, columns, zone_h, start):
    """Largest scale <= start at which every column fits in zone_h."""
    sc = start
    for _ in range(40):
        cx.dry = True
        tallest = max(stack(cx, blocks, 0, 0, w, sc) for blocks, w in columns)
        if tallest <= zone_h:
            return sc
        sc *= max(0.9, min(0.995, zone_h / tallest))
    sys.exit("could not fit layout into safe zone")


# ---------------------------------------------------------------- render

def hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def background(W, H, bg, glow, center, radius):
    img = Image.new("RGB", (W, H), bg)
    if glow == bg:
        return img
    k = 8
    mask = Image.new("L", (W // k, H // k), 0)
    md = ImageDraw.Draw(mask)
    cxp, cyp, rr = center[0] / k, center[1] / k, radius / k
    md.ellipse([cxp - rr, cyp - rr, cxp + rr, cyp + rr], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(rr * 0.6)).resize((W, H), Image.BICUBIC)
    img.paste(Image.new("RGB", (W, H), glow), (0, 0), mask)
    return img


def render(cfg, state, out, fonts, guides, out_dir=ROOT / "out"):
    s = cfg["style"]
    colors = {k: hex2rgb(s[k]) for k in ("background", "text", "muted", "faint", "accent")}
    colors["bg"] = colors["background"]
    colors["faint_text"] = mix(colors["muted"], colors["bg"], 0.35)
    glow = hex2rgb(s.get("background_glow", s["background"]))
    W, H = out["size"]
    sz = cfg["safe_zone"]
    phone = out["kind"] == "phone"
    top = H * (sz["phone_top"] if phone else sz["desktop_top"])
    bot = H * (1 - (sz["phone_bottom"] if phone else sz["desktop_bottom"]))

    if phone:
        base = W / 1179
        mx = 96 * base
        pad = 40 * base
        img = background(W, H, colors["bg"], glow, (W * 0.32, top + (bot - top) * 0.33), W * 0.55)
        cx = Ctx(img, fonts, colors, state)
        cw = W - 2 * mx
        sc = fit_scale(cx, [(PHONE_STACK, cw)], bot - top - 2 * pad, base)
        cx.dry = True
        h = stack(cx, PHONE_STACK, 0, 0, cw, sc)
        cx.dry = False
        stack(cx, PHONE_STACK, mx, top + (bot - top - h) / 2, cw, sc)
        safe = (mx, top, W - mx, bot)
    else:
        base = H / 1440 * 0.95
        mx = W * 0.09
        gutter = W * 0.07
        colw = (W - 2 * mx - gutter) / 2
        pad = 24 * base
        img = background(W, H, colors["bg"], glow, (mx + colw * 0.35, (top + bot) / 2), H * 0.5)
        cx = Ctx(img, fonts, colors, state)
        sc = fit_scale(cx, [(DESKTOP_LEFT, colw), (DESKTOP_RIGHT, colw)], bot - top - 2 * pad, base)
        for blocks, x in ((DESKTOP_LEFT, mx), (DESKTOP_RIGHT, mx + colw + gutter)):
            cx.dry = True
            h = stack(cx, blocks, 0, 0, colw, sc)
            cx.dry = False
            stack(cx, blocks, x, top + (bot - top - h) / 2, colw, sc)
        safe = (mx, top, W - mx, bot)

    # verify nothing escapes the safe zone
    bad = [(t, b) for t, b in cx.boxes
           if b[0] < safe[0] - 1 or b[1] < safe[1] - 1 or b[2] > safe[2] + 1 or b[3] > safe[3] + 1]

    out_dir = Path(out_dir)
    path = out_dir / f"{out['name']}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    if bad:  # keep the last good lock screen; the guides preview below still shows what went wrong
        print(f"NOT saved {path}: layout leaves the safe zone")
    else:
        img.save(path, optimize=True)
        print(f"wrote {path}  ({W}x{H}, scale {sc / base:.2f})")

    if guides:
        g = img.convert("RGBA")
        ov = Image.new("RGBA", g.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        od.rectangle([0, 0, W, top], fill=(255, 0, 0, 60))
        od.rectangle([0, bot, W, H], fill=(255, 0, 0, 60))
        od.rectangle(safe, outline=(255, 255, 0, 200), width=3)
        clock = fonts.get(H * (0.075 if phone else 0.09), bold=True)
        od.text((W / 2, top * 0.62), "9:41", font=clock, fill=(255, 255, 255, 200), anchor="mm")
        g = Image.alpha_composite(g, ov)
        gp = out_dir / "preview" / f"{out['name']}_guides.png"
        gp.parent.mkdir(parents=True, exist_ok=True)
        g.convert("RGB").save(gp)
        print(f"wrote {gp}")

    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=ROOT / "config.yaml")
    ap.add_argument("--today", help='override the clock for previews: "YYYY-MM-DD" or "YYYY-MM-DD HH:MM"')
    ap.add_argument("--out-dir", default=ROOT / "out", help="where to write PNGs (default: out/)")
    ap.add_argument("--guides", action="store_true", help="also write safe-zone preview images")
    ap.add_argument("--only", choices=["phone", "desktop"], help="render only one kind")
    a = ap.parse_args()

    cfg = yaml.safe_load(Path(a.config).read_text())
    state = load_state(cfg, a.today)
    fonts = Fonts(cfg["style"]["fonts"])
    left = "deadline passed" if state["passed"] else f"{state['days']} days left"
    print(f"{state['title']}: {left} (now {state['now']:%Y-%m-%d %H:%M} {state['tzname']})")

    failed = False
    for out in cfg["outputs"]:
        if a.only and out["kind"] != a.only:
            continue
        for text, box in render(cfg, state, out, fonts, a.guides, a.out_dir):
            failed = True
            print(f"  OUT OF SAFE ZONE in {out['name']}: {text!r} at {tuple(round(v) for v in box)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
