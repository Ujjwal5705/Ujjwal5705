"""Checks for calendar accuracy and the SVG-as-image output contract."""

from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from fontTools.ttLib import TTFont


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("render_cards", ROOT / "scripts/render_cards.py")
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


def snapshot(counts, start=date(2026, 1, 1)):
    return {"schema_version": 1, "username": "tester", "days": [
        {"date": (start + timedelta(days=index)).isoformat(), "count": count}
        for index, count in enumerate(counts)]}


class CalendarTests(unittest.TestCase):
    def stats(self, counts, start=date(2026, 1, 1)):
        return renderer.statistics(renderer.load_days(snapshot(counts, start), "tester"))

    def test_streaks_active_days_and_latest_peak_tie(self):
        stats = self.stats([1, 4, 0, 4, 2, 3, 0])
        self.assertEqual((stats["total"], stats["active"], stats["streak"], stats["longest"]), (14, 5, 3, 3))
        self.assertEqual(stats["busiest"], (date(2026, 1, 4), 4))
        self.assertEqual(self.stats([1, 2, 3])["streak"], 3)
        self.assertEqual(self.stats([1, 0, 0])["streak"], 0)

    def test_zero_activity_and_single_day(self):
        stats = self.stats([0, 0, 0])
        self.assertEqual((stats["total"], stats["active"], stats["streak"], stats["longest"]), (0, 0, 0, 0))
        self.assertEqual(self.stats([1])["streak"], 1)
        self.assertEqual(self.stats([0])["streak"], 0)
        self.assertEqual(renderer.heat_level(0, 0), 0)

    def test_months_across_year_and_leap_day(self):
        self.assertEqual(self.stats([2, 3, 4], date(2025, 12, 31))["monthly"],
                         [("2025-12", 2), ("2026-01", 7)])
        self.assertEqual(self.stats([2, 3, 4], date(2024, 2, 28))["monthly"],
                         [("2024-02", 5), ("2024-03", 4)])

    def test_rejects_gaps_duplicates_wrong_user_and_bad_counts(self):
        for payload in [snapshot([]), snapshot([-1]), snapshot([True]), snapshot([1.5])]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                renderer.load_days(payload, "tester")
        for dates in (["2026-01-01", "2026-01-03"], ["2026-01-01", "2026-01-01"],
                      ["2026-01-02", "2026-01-01"]):
            payload = snapshot([1, 1])
            for row, day in zip(payload["days"], dates):
                row["date"] = day
            with self.subTest(dates=dates), self.assertRaises(ValueError):
                renderer.load_days(payload, "tester")
        with self.assertRaises(ValueError):
            renderer.load_days(snapshot([1]), "someone-else")

    def test_refresh_keeps_last_valid_snapshot_on_api_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contributions.json"
            path.write_text("previous valid snapshot")
            with patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}), \
                 patch.object(renderer, "urlopen", return_value=io.StringIO('{"errors":[{"message":"denied"}]}')):
                with self.assertRaises(ValueError):
                    renderer.refresh(path, "tester")
            self.assertEqual(path.read_text(), "previous valid snapshot")

    def test_refresh_normalizes_calendar_and_rejects_missing_days(self):
        today = datetime.now(timezone.utc).date()
        first = today - timedelta(days=364)
        days = [{"date": (first + timedelta(days=index)).isoformat(), "contributionCount": index % 7}
                for index in range(365)]
        def response(rows):
            return io.StringIO(json.dumps({"data": {"user": {"contributionsCollection": {
                "contributionCalendar": {"weeks": [{"contributionDays": rows}]}}}}}))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}):
            path = Path(directory) / "contributions.json"
            with patch.object(renderer, "urlopen", return_value=response(days)):
                renderer.refresh(path, "tester")
            previous = path.read_bytes()
            actual = renderer.load_days(json.loads(previous), "tester")
            self.assertEqual((len(actual), actual[0][0], actual[-1][0]), (365, first, today))
            with patch.object(renderer, "urlopen", return_value=response(days[:-1])):
                with self.assertRaises(ValueError):
                    renderer.refresh(path, "tester")
            self.assertEqual(path.read_bytes(), previous)


class ImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.animated = Path(cls.temp.name) / "animated"
        cls.static = Path(cls.temp.name) / "static"
        for static, directory in ((False, cls.animated), (True, cls.static)):
            subprocess.run([sys.executable, str(ROOT / "scripts/render_cards.py"), "--output", str(directory)],
                           env={**os.environ, "STATIC": "1" if static else "0"},
                           check=True, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_all_readme_images_exist_and_tiles_are_linked(self):
        class Images(HTMLParser):
            def __init__(self):
                super().__init__()
                self.images = []
                self.link = None
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "a":
                    self.link = attrs.get("href")
                if tag == "img":
                    self.images.append((attrs, self.link))
            def handle_endtag(self, tag):
                if tag == "a":
                    self.link = None
        parser = Images()
        parser.feed((ROOT / "README.md").read_text())
        self.assertEqual(len(parser.images), 11)
        tiles = [(attrs, link) for attrs, link in parser.images if attrs["width"] == "32.5%"]
        self.assertEqual(len(tiles), 3)
        self.assertTrue(all(link for _, link in tiles))
        for attrs, _ in parser.images:
            self.assertTrue((self.animated / Path(attrs["src"]).name).is_file())
            self.assertTrue(attrs.get("alt"))

    def test_static_has_only_final_frames_and_animated_has_reduced_motion(self):
        for path in self.animated.glob("*.svg"):
            animated = path.read_text()
            still = (self.static / path.name).read_text()
            self.assertIn("@keyframes", animated)
            self.assertIn("prefers-reduced-motion:reduce", animated)
            self.assertIn("stroke-dashoffset:0!important", animated)
            self.assertNotIn("@keyframes", still)
            self.assertNotIn("<animate", still)
            self.assertNotIn('stroke-dashoffset="1"', still)
        for name in ("hero.svg", "contributions.svg"):
            self.assertIn("<animate", (self.animated / name).read_text())

    def test_svgs_are_self_contained_with_valid_embedded_woff_fonts(self):
        import base64
        ns = {"s": renderer.SVG_NS}
        for path in self.animated.glob("*.svg"):
            svg = path.read_text()
            tree = ET.fromstring(svg)
            self.assertIsNotNone(tree.find("s:title", ns))
            self.assertIsNotNone(tree.find("s:desc", ns))
            self.assertEqual(len(tree.findall("s:style", ns)), 1)
            self.assertNotIn("foreignObject", svg)
            self.assertNotIn("<script", svg)
            self.assertNotRegex(svg, r'(?:href|src)="https?://')
            fonts = re.findall(r"font-family:'(TASA|Oxygen)'.*?base64,([A-Za-z0-9+/=]+)", svg)
            self.assertEqual(len(fonts), 2)
            for family, encoded in fonts:
                raw = base64.b64decode(encoded)
                self.assertEqual(raw[:4], b"wOFF")
                font = TTFont(io.BytesIO(raw))
                cmap = font.getBestCmap()
                for node in tree.findall(".//s:text", ns):
                    target = "TASA" if "heading" in node.get("class", "").split() else "Oxygen"
                    if target == family:
                        for char in "".join(node.itertext()):
                            self.assertIn(ord(char), cmap, f"{path.name}: {family} missing {char!r}")


if __name__ == "__main__":
    unittest.main()
