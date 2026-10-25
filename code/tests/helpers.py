"""Shared fixtures for the test suite."""

import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def record(file_id, captured, system, resource_id, content, store="primary", text=""):
    """An evidence record as produced by ghostaudit.evidence."""
    return {"file_id": file_id, "captured_at": ts(captured) if captured else None, "system": system,
            "store": store, "resource_id": resource_id, "record": content, "text": text}


def log_record(text):
    return {"record": {"raw": text}, "text": text, "system": "unknown_service", "resource_id": ""}


@contextmanager
def temp_file(content, suffix):
    handle = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8")
    try:
        handle.write(content)
        handle.close()
        yield handle.name
    finally:
        os.unlink(handle.name)
