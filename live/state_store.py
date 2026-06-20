"""Lightweight, dependency-free state log.

Each run produces:
  - run-<timestamp>.json   one file PER run (committed to the repo's runs/ folder)
  - cycles.jsonl           consolidated append-only history (for the report tool)
  - latest.json            most recent cycle snapshot

When AIHF_STATE_DIR points at the repo's `runs/` folder, the scheduler commits
these files each cycle, so the repo itself is the durable, versioned history.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _filename_stamp(iso_ts: str) -> str:
    """Turn an ISO timestamp into a sortable, filename-safe stamp."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except Exception:
        dt = datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def record_cycle(settings, record: Dict[str, Any]) -> str:
    os.makedirs(settings.state_dir, exist_ok=True)
    record.setdefault("timestamp", _now_iso())
    record.setdefault("environment", settings.environment)
    record.setdefault("dry_run", settings.dry_run)

    # Per-run file (one committed log per run).
    stamp = _filename_stamp(record["timestamp"])
    run_path = os.path.join(settings.state_dir, f"run-{stamp}-{uuid.uuid4().hex[:6]}.json")
    with open(run_path, "w") as f:
        json.dump(record, f, indent=2, default=str)

    # Consolidated append-only history.
    jsonl_path = os.path.join(settings.state_dir, "cycles.jsonl")
    with open(jsonl_path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")

    # Latest snapshot.
    latest_path = os.path.join(settings.state_dir, "latest.json")
    with open(latest_path, "w") as f:
        json.dump(record, f, indent=2, default=str)

    return run_path
