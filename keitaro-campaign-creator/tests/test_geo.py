"""Тесты разбора гео-кодов (чистый Python)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.geo import parse_geo  # noqa: E402


class ParseGeoTests(unittest.TestCase):
    def test_strict_normalizes(self):
        self.assertEqual(parse_geo("mx, AU;ro  au"), ["MX", "AU", "RO"])

    def test_strict_rejects(self):
        for bad in ("", "  ", "USA", "1A", "MX,X"):
            with self.subTest(geo=bad), self.assertRaises(ValueError):
                parse_geo(bad)

    def test_lenient_skips_partial_codes(self):
        self.assertEqual(parse_geo("MX,A", strict=False), ["MX"])
        self.assertEqual(parse_geo("", strict=False), [])
        self.assertEqual(parse_geo(None, strict=False), [])


if __name__ == "__main__":
    unittest.main()
