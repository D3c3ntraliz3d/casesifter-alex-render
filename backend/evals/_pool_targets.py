"""Child-process targets for worker pool tests (must be importable by spawn)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def _log(message_json: str, event: str) -> None:
    data = json.loads(message_json)
    out = Path(os.environ["POOL_TEST_LOG"])
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"job": data["job_id"], "attempt": data["attempt"], "event": event, "t": time.time(), "pid": os.getpid()}) + "\n")


def sleep_ok(message_json: str) -> None:
    _log(message_json, "start")
    time.sleep(1.0)
    _log(message_json, "end")


def always_fail(message_json: str) -> None:
    _log(message_json, "start")
    raise RuntimeError("boom")


def hang(message_json: str) -> None:
    _log(message_json, "start")
    time.sleep(60)
