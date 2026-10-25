"""Contract checks on the generated output.csv (skipped when the dataset or output is absent)."""

import csv
import unittest

from tests import helpers  # noqa: F401  (adds code/ to sys.path)
from ghostaudit.io_utils import DATASET_DIR, OUTPUT_PATH, load_csv
from ghostaudit.validation import validate_output


@unittest.skipUnless(DATASET_DIR.exists() and OUTPUT_PATH.exists(), "dataset/ and output.csv are required")
class OutputContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(OUTPUT_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            cls.header = reader.fieldnames
            cls.rows = list(reader)

    def test_header_and_row_count(self):
        self.assertEqual(self.header, ["case_id", "deletion_status", "steps_json"])
        self.assertEqual(len(self.rows), len(load_csv("deletion_requests.csv")))

    def test_contract(self):
        problems = validate_output(self.rows, load_csv("deletion_requests.csv"), load_csv("evidence_files.csv"))
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
