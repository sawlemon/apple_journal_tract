"""Hash-only local ledger for queued Hindsight operations."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


PENDING = "pending"
STORED = "stored"
FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class LedgerEntry:
    document_id: str
    source_path: str = ""
    source_fingerprint: str = ""
    body_fingerprint: str = ""
    attachment_fingerprint: str = ""
    operation_id: str = ""
    status: str = PENDING
    error: str = ""
    attempts: int = 0
    updated_at: str = ""


class Ledger:
    """Persist only reconciliation facts, never the rendered journal body."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.entries: dict[str, LedgerEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        raw_entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
        if isinstance(raw_entries, list):
            raw_entries = {str(item.get("document_id")): item for item in raw_entries if isinstance(item, dict)}
        if not isinstance(raw_entries, dict):
            return
        fields = set(LedgerEntry.__dataclass_fields__)
        for key, raw in raw_entries.items():
            if not isinstance(raw, dict):
                continue
            data = {name: raw[name] for name in fields if name in raw}
            data.setdefault("document_id", str(key))
            data["document_id"] = str(data["document_id"])
            self.entries[data["document_id"]] = LedgerEntry(**data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        payload = {
            "schema_version": 1,
            "last_run": _now(),
            "counts": self.counts(),
            "entries": {key: asdict(value) for key, value in sorted(self.entries.items())},
        }
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[tuple[str, LedgerEntry]]:
        return iter(self.entries.items())

    def get(self, document_id: str) -> LedgerEntry | None:
        return self.entries.get(document_id)

    def by_source_path(self, source_path: str) -> list[LedgerEntry]:
        return [entry for entry in self.entries.values() if entry.source_path == source_path]

    def by_fingerprints(self, body_fingerprint: str, attachment_fingerprint: str) -> list[LedgerEntry]:
        return [
            entry
            for entry in self.entries.values()
            if entry.body_fingerprint == body_fingerprint
            and entry.attachment_fingerprint == attachment_fingerprint
        ]

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries.values():
            counts[entry.status] = counts.get(entry.status, 0) + 1
        return counts

    def unfinished(self) -> list[LedgerEntry]:
        return [entry for entry in self.entries.values() if entry.status in {PENDING, FAILED}]

    def mark_pending(
        self,
        document_id: str,
        *,
        source_path: str,
        source_fingerprint: str,
        body_fingerprint: str,
        attachment_fingerprint: str,
        operation_id: str,
    ) -> LedgerEntry:
        previous = self.entries.get(document_id)
        entry = LedgerEntry(
            document_id=document_id,
            source_path=source_path,
            source_fingerprint=source_fingerprint,
            body_fingerprint=body_fingerprint,
            attachment_fingerprint=attachment_fingerprint,
            operation_id=operation_id,
            status=PENDING,
            attempts=(previous.attempts + 1) if previous else 1,
            updated_at=_now(),
        )
        self.entries[document_id] = entry
        return entry

    def mark_stored(self, document_id: str) -> None:
        entry = self.entries.get(document_id)
        if entry is None:
            return
        entry.status = STORED
        entry.error = ""
        entry.updated_at = _now()

    def mark_failed(self, document_id: str, error: str) -> None:
        entry = self.entries.get(document_id)
        if entry is None:
            return
        entry.status = FAILED
        entry.error = (error or "unknown Hindsight operation failure")[:1000]
        entry.updated_at = _now()

    def retry_pending(self, document_id: str, operation_id: str) -> LedgerEntry | None:
        entry = self.entries.get(document_id)
        if entry is None:
            return None
        entry.operation_id = operation_id
        entry.status = PENDING
        entry.error = ""
        entry.attempts += 1
        entry.updated_at = _now()
        return entry

