"""CSV loading and timestamp parsing."""

import csv
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = PROJECT_ROOT / "dataset"
OUTPUT_PATH = PROJECT_ROOT / "output.csv"


def load_csv(path, dataset_dir=DATASET_DIR):
    """Read a CSV into a list of dicts; relative paths resolve against the dataset folder."""
    path = Path(path)
    if not path.is_absolute():
        path = Path(dataset_dir) / path
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def parse_ts(value):
    """Parse an ISO-8601 timestamp (with a trailing Z) into an aware datetime, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
