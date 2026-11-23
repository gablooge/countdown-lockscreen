import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import make_reminders as mr  # noqa: E402


def generate(today):
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "r.ics"
        subprocess.run([sys.executable, str(ROOT / "make_reminders.py"), "--today", today, "--out", str(out)],
                       check=True, capture_output=True)
        return out.read_bytes()


class Ics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = generate("2026-11-22")

    def test_crlf_and_folding(self):
        self.assertEqual(self.raw.count(b"\n"), self.raw.count(b"\r\n"), "bare LF found")
        self.assertLessEqual(max(len(l) for l in self.raw.split(b"\r\n")), 75)

    def test_event_counts_and_times(self):
        text = self.raw.decode()
        self.assertEqual(text.count("BEGIN:VEVENT"), 24 + 6)
        self.assertIn("DTSTART:20261122T010000Z", text)  # 08:00 WIB
        self.assertIn("DTSTART:20261215T010000Z", text)
        self.assertNotIn("DTSTART:20261216T", text)
        self.assertEqual(text.count("DTSTART;VALUE=DATE:"), 6)

    def test_fold_keeps_utf8_intact(self):
        line = "SUMMARY:" + "x" * 66 + "é" * 20
        folded = mr.fold(line)
        parts = folded.split("\r\n")
        self.assertTrue(all(len(p.encode()) <= 75 for p in parts))
        self.assertEqual(parts[0] + "".join(p[1:] for p in parts[1:]), line)

    def test_parses_with_icalendar_if_available(self):
        try:
            from icalendar import Calendar
        except ImportError:
            self.skipTest("icalendar not installed")
        events = list(Calendar.from_ical(self.raw).walk("VEVENT"))
        self.assertEqual(len(events), 30)


if __name__ == "__main__":
    unittest.main()
