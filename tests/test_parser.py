from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from apple_journal.parser import parse_apple_timestamp, parse_entry, parse_export


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPPLIED_EXPORT = Path("/Users/sala/Downloads/AppleJournalEntries")


class ParserTests(unittest.TestCase):
    def test_synthetic_fixture_extracts_date_body_attachment_and_epoch(self) -> None:
        entries = parse_export(PROJECT_ROOT / "fixtures" / "minimal_export")
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.entry_date, "2024-01-02")
        self.assertEqual(entry.title, "First entry")
        self.assertEqual(entry.body, "Line one\nLine two\n\nSecond paragraph")
        self.assertEqual(len(entry.resources), 1)
        self.assertEqual(entry.resources[0].kind, "photo")
        self.assertEqual(entry.resources[0].timestamp, "2024-01-02T00:00:00Z")
        self.assertEqual(entry.resources[0].metadata["placeName"], "Example Place")

    def test_apple_epoch_conversion(self) -> None:
        self.assertEqual(parse_apple_timestamp(0), "2001-01-01T00:00:00Z")
        self.assertEqual(parse_apple_timestamp(86400), "2001-01-02T00:00:00Z")
        self.assertEqual(parse_apple_timestamp("2024-01-02"), "2024-01-02")

    def test_malformed_nesting_unicode_filename_untitled_and_missing_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Entries").mkdir()
            (root / "Resources").mkdir()
            filename = "2024-01-02_😀.html"
            (root / "index.html").write_text(
                f"<html><head><style>.secret {{ font-family: secret; }}</style></head>"
                f"<body><p><a href='Entries/{filename}'>2 Jan 2024</a></p></body></html>",
                encoding="utf-8",
            )
            (root / "Entries" / filename).write_text(
                """<html><body><div class='pageContainer'>
                <div class='pageHeader'>2 Jan 2024</div>
                <div class='title'></div>
                <div class='bodyText'><p>first<br>line</p><p>second</p></div>
                </div></body></html>""",
                encoding="utf-8",
            )
            entry = parse_export(root)[0]
            self.assertEqual(entry.title, "")
            self.assertEqual(entry.body, "first\nline\n\nsecond")
            self.assertEqual(entry.entry_date, "2024-01-02")
            self.assertFalse(entry.resources)

    def test_duplicate_displayed_dates_are_not_collapsed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Entries").mkdir()
            (root / "Resources").mkdir()
            links = []
            for suffix, marker in (("one", "one"), ("two", "two")):
                filename = f"2024-01-02_{suffix}.html"
                links.append(f"<a href='Entries/{filename}'>2 Jan 2024</a>")
                (root / "Entries" / filename).write_text(
                    f"<div class='pageHeader'>2 Jan 2024</div><div class='bodyText'><p>{marker}</p></div>",
                    encoding="utf-8",
                )
            (root / "index.html").write_text("<body>" + "".join(links) + "</body>", encoding="utf-8")
            entries = parse_export(root)
            self.assertEqual(len(entries), 2)
            self.assertEqual({entry.entry_date for entry in entries}, {"2024-01-02"})
            self.assertEqual({entry.body for entry in entries}, {"one", "two"})

    @unittest.skipUnless(SUPPLIED_EXPORT.is_dir(), "supplied Apple Journal export is not mounted")
    def test_supplied_export_parses_with_dates_and_bodies(self) -> None:
        entries = parse_export(SUPPLIED_EXPORT)
        # The live export grows with each re-export; only the original floor is stable.
        self.assertGreaterEqual(len(entries), 80)
        self.assertTrue(all(entry.entry_date for entry in entries))
        self.assertTrue(all(entry.body for entry in entries))
        self.assertFalse(any("font-family" in entry.body for entry in entries))
        self.assertFalse(any("pageContainer" in entry.body for entry in entries))


if __name__ == "__main__":
    unittest.main()
