"""Lightweight, dependency-free state log. Appends each cycle to a JSONL file and
keeps a `latest.json` snapshot for quick dashboards / review.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict

from live.config import Settings


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_cycle(settings: Settings, record: Dict[str, Any]) -> str:
    os.makedirs(settings.state_dir, exist_ok=True)
    record.setdefault("timestamp", _now_iso())
    record.setdefault("environment", settings.environment)
    record.setdefault("dry_run", settings.dry_run)

    jsonl_path = os.path.join(settings.state_dir, "cycles.jsonl")
    with open(jsonl_path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")

    latest_path = os.path.join(settings.state_dir, "latest.json")
    with open(latest_path, "w") as f:
        json.dump(record, f, indent=2, default=str)

    return jsonl_path
