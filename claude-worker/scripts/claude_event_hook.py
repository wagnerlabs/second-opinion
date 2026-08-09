#!/usr/bin/env python3
"""Record Claude lifecycle hook events without storing hook payload contents."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", choices=("notification", "stop"), required=True)
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        payload = {}
    raw_path = os.environ.get("CLAUDE_WORKER_EVENT_LOG")
    if not raw_path:
        return 0
    path = Path(raw_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    record: Dict[str, Any] = {
        "event": args.event,
        "at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "session_id": payload.get("session_id"),
        "hook_event_name": payload.get("hook_event_name"),
        "notification_type": payload.get("notification_type"),
    }
    with path.open("a", encoding="utf-8") as handle:
        json.dump(record, handle, sort_keys=True)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
