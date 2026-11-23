import copy
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import make_lockscreen as ml  # noqa: E402

BASE_CFG = yaml.safe_load((ROOT / "config.yaml").read_text())


def cfg():
    return copy.deepcopy(BASE_CFG)


class CountdownState(unittest.TestCase):
    def test_days_left_from_start(self):
        st = ml.load_state(cfg(), "2026-11-22")
        self.assertEqual(st["days"], 23)
        self.assertEqual(st["next"], 0)

    def test_deadline_day_before_deadline_time(self):
        st = ml.load_state(cfg(), "2026-12-15 16:59")
        self.assertEqual(st["days"], 0)
        self.assertEqual(st["next"], 5)

    def test_deadline_time_passed_same_day(self):
        st = ml.load_state(cfg(), "2026-12-15 17:00")
        self.assertLess(st["days"], 0, "after the deadline time the screen must not say DUE TODAY")
        self.assertIsNone(st["next"])

    def test_day_after_deadline(self):
        st = ml.load_state(cfg(), "2026-12-16")
        self.assertLess(st["days"], 0)
        self.assertIsNone(st["next"])

    def test_target_parsed_by_yaml_as_datetime(self):
        text = (ROOT / "config.yaml").read_text().replace('"2026-12-15 17:00"', "2026-12-15 17:00:00")
        c = yaml.safe_load(text)
        st = ml.load_state(c, "2026-11-22")
        self.assertEqual((st["target"].hour, st["target"].minute), (17, 0))
        self.assertEqual(st["target"].utcoffset(), timedelta(hours=7))
        self.assertEqual(st["days"], 23)

    def test_timeline_marks_every_milestone_past_once_deadline_passes(self):
        st = ml.load_state(cfg(), "2026-12-15 17:00")
        self.assertEqual([ml.milestone_status(st, i) for i in range(6)], ["past"] * 6)
        st = ml.load_state(cfg(), "2026-12-15 16:59")
        self.assertEqual(ml.milestone_status(st, 5), "next")
        self.assertEqual(ml.milestone_status(st, 4), "past")

    def test_today_accepts_iso_variants(self):
        for s in ("2026-12-15T16:59", "2026-12-15 16:59:30", "2026-12-15T16:59:30"):
            self.assertEqual(ml.load_state(cfg(), s)["days"], 0, s)
        self.assertEqual(ml.load_state(cfg(), "2026-12-15T17:00:00")["days"], -1)

    def test_null_sections_and_non_string_titles(self):
        c = cfg()
        c["milestones"] = None
        c["progress"] = None
        st = ml.load_state(c, "2026-11-22")
        self.assertEqual((st["milestones"], st["next"], st["progress"]), ([], None, {}))
        c = cfg()
        c["milestones"][0]["title"] = 2026
        self.assertEqual(ml.load_state(c, "2026-11-22")["milestones"][0]["title"], "2026")

    def test_rejects_em_dash(self):
        c = cfg()
        c["deadline"]["title"] = "Apply \N{EM DASH} now"
        with self.assertRaises(SystemExit):
            ml.load_state(c, "2026-11-22")

    def test_rejects_out_of_order_milestones(self):
        c = cfg()
        c["milestones"][0]["date"], c["milestones"][1]["date"] = c["milestones"][1]["date"], c["milestones"][0]["date"]
        with self.assertRaises(SystemExit):
            ml.load_state(c, "2026-11-22")


class SafeZone(unittest.TestCase):
    def test_shapes_are_recorded_for_the_zone_check(self):
        from PIL import Image
        cx = ml.Ctx(Image.new("RGB", (100, 100)), None, {}, {})
        cx.dry = False
        cx.shape("ellipse", [10, 20, 30, 40], fill=(255, 0, 0))
        cx.shape("line", [(5, 6), (7, 90)], fill=(255, 0, 0), width=2)
        boxes = [b for _, b in cx.boxes]
        self.assertIn((10, 20, 30, 40), [tuple(b) for b in boxes])
        self.assertIn((5, 6, 7, 90), [tuple(b) for b in boxes])

    def test_every_day_and_progress_fits_every_output(self):
        c = cfg()
        fonts = ml.Fonts(c["style"]["fonts"])
        renders = 0
        with tempfile.TemporaryDirectory() as tmp:
            for offset in range(26):  # Nov 21 .. Dec 16
                day = date(2026, 11, 21) + timedelta(days=offset)
                for cur in (0, 7, 15):
                    c["progress"]["current"] = cur
                    st = ml.load_state(c, day.isoformat())
                    for out in c["outputs"]:
                        with redirect_stdout(io.StringIO()):
                            bad = ml.render(c, st, out, fonts, False, out_dir=Path(tmp))
                        self.assertEqual(bad, [], f"{out['name']} on {day} with {cur} sent")
                        renders += 1
            st = ml.load_state(c, "2026-12-15 18:00")
            for out in c["outputs"]:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(ml.render(c, st, out, fonts, False, out_dir=Path(tmp)), [])
        self.assertEqual(renders, 26 * 3 * len(c["outputs"]))

    def test_large_progress_total_renders(self):
        c = cfg()
        c["progress"] = {"total": 120, "current": 3}
        st = ml.load_state(c, "2026-11-22")
        fonts = ml.Fonts(c["style"]["fonts"])
        with tempfile.TemporaryDirectory() as tmp:
            for out in c["outputs"]:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(ml.render(c, st, out, fonts, False, out_dir=Path(tmp)), [], out["name"])

    def test_failed_render_keeps_previous_png(self):
        c = cfg()
        c["deadline"]["title"] = "Supercalifragilisticexpialidociousapplications"
        st = ml.load_state(c, "2026-11-22")
        fonts = ml.Fonts(c["style"]["fonts"])
        out = c["outputs"][0]
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / f"{out['name']}.png"
            png.write_bytes(b"previous good image")
            with redirect_stdout(io.StringIO()):
                bad = ml.render(c, st, out, fonts, False, out_dir=Path(tmp))
            self.assertTrue(bad)
            self.assertEqual(png.read_bytes(), b"previous good image")

    def test_cli_wording_after_deadline(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run([sys.executable, str(ROOT / "make_lockscreen.py"), "--today", "2026-12-15 18:00",
                                "--only", "phone", "--out-dir", tmp], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("deadline passed", r.stdout)
        self.assertNotIn("-1 days", r.stdout)

    def test_render_does_not_touch_repo_out_dir(self):
        c = cfg()
        fonts = ml.Fonts(c["style"]["fonts"])
        st = ml.load_state(c, "2026-11-22")
        with tempfile.TemporaryDirectory() as tmp:
            with redirect_stdout(io.StringIO()):
                ml.render(c, st, c["outputs"][0], fonts, True, out_dir=Path(tmp))
            self.assertTrue((Path(tmp) / f"{c['outputs'][0]['name']}.png").exists())
            self.assertTrue((Path(tmp) / "preview" / f"{c['outputs'][0]['name']}_guides.png").exists())


if __name__ == "__main__":
    unittest.main()
