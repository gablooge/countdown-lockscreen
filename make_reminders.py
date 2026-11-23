#!/usr/bin/env python3
"""Write reminders.ics from config.yaml.

- One daily 08:00 event (deadline timezone) from today through the deadline day,
  titled with the days left and the next milestone.
- One all-day event on each milestone date.

Import into Google Calendar: Settings > Import & export > Import > reminders.ics.
Re-running produces the same UIDs, so re-importing updates events instead of duplicating them.
"""
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from make_lockscreen import ROOT, load_state

REMIND_AT = (8, 0)


def esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    """RFC 5545: lines longer than 75 octets are folded with CRLF + space."""
    out, b = [], line.encode()
    while len(b) > 75:
        cut = 75
        while (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 sequence
            cut -= 1
        out.append(b[:cut].decode())
        b = b" " + b[cut:]
    out.append(b.decode())
    return "\r\n".join(out)


def utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=ROOT / "config.yaml")
    ap.add_argument("--today", help="override start date (YYYY-MM-DD)")
    ap.add_argument("--out", default=ROOT / "reminders.ics")
    a = ap.parse_args()

    cfg = yaml.safe_load(Path(a.config).read_text())
    st = load_state(cfg, a.today)
    tz, target, ms = st["target"].tzinfo, st["target"], st["milestones"]
    stamp = utc(datetime.now(timezone.utc))
    slug = "deadline-" + target.strftime("%Y%m%d")

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//countdown-lockscreen//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{esc(st['title'])}"]

    def event(uid, summary, desc, start, end, all_day=False, alarm=False):
        lines.extend(["BEGIN:VEVENT", f"UID:{uid}@countdown-lockscreen", f"DTSTAMP:{stamp}"])
        if all_day:
            lines.extend([f"DTSTART;VALUE=DATE:{start:%Y%m%d}", f"DTEND;VALUE=DATE:{end:%Y%m%d}",
                          "TRANSP:TRANSPARENT"])
        else:
            lines.extend([f"DTSTART:{utc(start)}", f"DTEND:{utc(end)}"])
        lines.extend([f"SUMMARY:{esc(summary)}", f"DESCRIPTION:{esc(desc)}"])
        if alarm:
            lines.extend(["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{esc(summary)}",
                          "TRIGGER:PT0M", "END:VALARM"])
        lines.append("END:VEVENT")

    n_daily = 0
    day = st["today"]
    while day <= target.date():
        left = (target.date() - day).days
        nxt = next((m for m in ms if m["date"] >= day), None)
        if left == 0:
            head, nxt = f"Due today {target:%H:%M}: {st['title']}", None
        else:
            head = f"{left} day{'s' if left != 1 else ''} left"
        summary = f"{head} | Next: {nxt['title']} ({nxt['date']:%b} {nxt['date'].day})" if nxt else head
        desc = f"Deadline: {st['title']}, {target:%a %d %b %Y %H:%M} {st['tzname']}."
        if nxt and nxt.get("done_when"):
            desc += f"\nNext milestone done when: {nxt['done_when']}"
        desc += "\nRe-render the lock screen: python make_lockscreen.py"
        start = datetime(day.year, day.month, day.day, *REMIND_AT, tzinfo=tz)
        event(f"{slug}-daily-{day:%Y%m%d}", summary, desc, start, start + timedelta(minutes=15), alarm=True)
        day += timedelta(days=1)
        n_daily += 1

    for i, m in enumerate(ms, 1):
        desc = f"Milestone {i} of {len(ms)} for: {st['title']}"
        if m.get("done_when"):
            desc += f"\nDone when: {m['done_when']}"
        event(f"{slug}-milestone-{i}", f"Milestone {i}/{len(ms)}: {m['title']}", desc,
              m["date"], m["date"] + timedelta(days=1), all_day=True)

    lines.append("END:VCALENDAR")
    Path(a.out).write_bytes(("\r\n".join(fold(l) for l in lines) + "\r\n").encode("utf-8"))
    print(f"wrote {Path(a.out).name}: {n_daily} daily reminders + {len(ms)} milestones")


if __name__ == "__main__":
    main()
