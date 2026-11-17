# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in local telemetry: raw API responses with personal data removed.

Purpose: diagnose schema drift, capture shapes we have not seen yet (e.g.
pooled trips) and back-test ETA. Files stay on the local machine.

Personal fields keep their key (so the structure is preserved for schema
analysis) but their value is replaced. Coordinates are kept: they are needed
for ETA analysis; the files are local-only.

All functions here do blocking file I/O; call them from an executor.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REDACTED = "**REDACTED**"
PII_KEYS = frozenset(
    {
        "firstName", "lastName", "phone", "email", "comment", "commentTrip",
        "commentOperator", "customerRefNo", "externalCodesString",
        "streetName", "houseNo", "houseLetter", "floor", "door", "zipCode",
        "city", "fullAddress", "pOIName", "poiname", "password", "username",
        "ssn", "personalNumber",
    }
)


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: (REDACTED if k in PII_KEYS and v not in (None, "", 0) else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


class TelemetryWriter:
    def __init__(self, root: Path, retention_days: int = 30) -> None:
        self.root = root
        self.retention_days = retention_days
        self._last_hash: dict[str, str] = {}

    def record(
        self,
        now: datetime,
        endpoint: str,
        key: str,
        data: Any,
        *,
        http_status: int | None = None,
        note: str | None = None,
    ) -> bool:
        """Append a redacted response. Consecutive identical payloads are skipped.

        Returns True if a line was written.
        """
        clean = redact(data)
        body = json.dumps(clean, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(body.encode()).hexdigest()
        stream = f"{endpoint}:{key}"
        if self._last_hash.get(stream) == digest:
            return False
        self._last_hash[stream] = digest
        day_dir = self.root / now.date().isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)
        line = {
            "ts": now.isoformat(),
            "endpoint": endpoint,
            "key": key,
            "http_status": http_status,
            "data": clean,
        }
        if note:
            line["note"] = note
        with (day_dir / f"{endpoint}-{key}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        return True

    def prune(self, today: date) -> int:
        """Delete day folders older than the retention period. Returns count removed."""
        if not self.root.is_dir():
            return 0
        cutoff = today - timedelta(days=self.retention_days)
        removed = 0
        for child in self.root.iterdir():
            try:
                day = date.fromisoformat(child.name)
            except ValueError:
                continue
            if child.is_dir() and day < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        return removed
