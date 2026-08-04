"""Shared data contracts for the Apple Journal importer.

The parser owns the full in-memory entry, while plans and ledgers deliberately
carry only hashes and metadata.  Keeping those representations separate makes
it difficult to accidentally persist journal text in the local sync ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ResourceAttachment:
    """A referenced Apple Journal resource and its optional sidecar data."""

    resource_id: str
    kind: str
    filename: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    sidecar_type: str = ""
    timestamp: str = ""

    def summary(self) -> dict[str, Any]:
        """Return safe metadata suitable for a document and a plan review."""

        result: dict[str, Any] = {
            "id": self.resource_id,
            "kind": self.kind,
        }
        if self.filename:
            result["filename"] = self.filename
        if self.sidecar_type:
            result["sidecar_type"] = self.sidecar_type
        if self.timestamp:
            result["timestamp"] = self.timestamp
        return result


@dataclass
class JournalEntry:
    """A parsed entry before it is rendered into a Hindsight document."""

    source_path: str
    entry_date: str
    title: str
    body: str
    resources: list[ResourceAttachment] = field(default_factory=list)
    locations: list[dict[str, Any]] = field(default_factory=list)
    music: list[dict[str, Any]] = field(default_factory=list)
    workouts: list[dict[str, Any]] = field(default_factory=list)
    labels: list[Any] = field(default_factory=list)
    associations: list[Any] = field(default_factory=list)
    transcript: str = ""
    source_fingerprint: str = ""
    body_fingerprint: str = ""
    attachment_fingerprint: str = ""

    @property
    def attachment_summary(self) -> list[dict[str, Any]]:
        return [resource.summary() for resource in self.resources]


@dataclass
class RenderedDocument:
    """The complete payload needed for one Hindsight retain operation."""

    document_id: str
    content: str
    context: str
    tags: list[str]
    metadata: dict[str, Any]
    timestamp: str
    source_fingerprint: str
    body_fingerprint: str
    attachment_fingerprint: str
    source_path: str
    entry_date: str


@dataclass
class RemoteDocument:
    """The subset of a live Hindsight document used by reconciliation."""

    document_id: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def source_fingerprint(self) -> str:
        return str(
            self.metadata.get("source_fingerprint")
            or self.metadata.get("content_fingerprint")
            or ""
        )

    @property
    def body_fingerprint(self) -> str:
        return str(self.metadata.get("body_fingerprint") or "")

    @property
    def attachment_fingerprint(self) -> str:
        return str(self.metadata.get("attachment_fingerprint") or "")

    @property
    def source_path(self) -> str:
        return str(self.metadata.get("source_path") or "")


@dataclass
class SyncPlanEntry:
    """Content-free per-entry decision persisted in a plan file."""

    document_id: str
    source_path: str
    entry_date: str
    source_fingerprint: str
    body_fingerprint: str
    attachment_fingerprint: str
    attachment_types: list[str] = field(default_factory=list)
    action: str = "new"
    identity_conflict: bool = False
    identity_match: str = ""
    semantic_matches: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""


@dataclass
class SyncPlan:
    """Content-free plan plus the export and live-bank fingerprints."""

    schema_version: int
    parser_version: str
    bank: str
    endpoint: str
    export_root: str
    export_manifest: str
    entries: list[SyncPlanEntry] = field(default_factory=list)
    missing_source_documents: list[str] = field(default_factory=list)
    created_at: str = ""
