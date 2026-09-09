import copy
import tempfile
import unittest
from pathlib import Path

from scripts.config import ROOT, read_json, validate


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.baseline = read_json(ROOT / "configs/baseline.json")

    def test_baseline(self):
        validate(self.baseline)

    def test_invalid_values(self):
        cases = {
            "xlen": 64,
            "instruction_bits": 16,
            "fifo_depth": 3,
            "sram_bytes": 10000,
            "pre_pages": 17,
            "post_pages": 0,
            "source_count": 5,
            "target_clock_hz": 0,
            "retire_lanes": True,
            "board": "unqualified-board",
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                candidate = copy.deepcopy(self.baseline)
                candidate[key] = value
                with self.assertRaises(ValueError):
                    validate(candidate)

    def test_missing_and_unknown_fields(self):
        missing = copy.deepcopy(self.baseline)
        del missing["source_count"]
        extra = dict(self.baseline, sorce_count=4)
        for candidate in (missing, extra, []):
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    validate(candidate)

    def test_duplicate_json_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text('{"source_count": 4, "source_count": 1}')
            with self.assertRaisesRegex(ValueError, "duplicate key"):
                read_json(path)


if __name__ == "__main__":
    unittest.main()
