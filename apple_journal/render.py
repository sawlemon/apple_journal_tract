"""Deterministic Hindsight document rendering."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

from . import config
from .models import JournalEntry, RenderedDocument, ResourceAttachment


def document_id_for_source(source_path: str) -> str:
    """Return the default stable identity for an entry filename.

    A rename can be assigned the prior remote identity by reconciliation after
    a unique body/attachment match. Ordinary body and title edits therefore do
    not change the identity.
    """

    normalized = unicodedata.normalize("NFC", source_path).replace("\\", "/")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"apple-journal:{digest}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _attachment_lines(resource: ResourceAttachment) -> list[str]:
    lines = [f"- {resource.kind}: {resource.resource_id}"]
    if resource.filename:
        lines.append(f"  file: {resource.filename}")
    if resource.sidecar_type:
        lines.append(f"  sidecar: {resource.sidecar_type}")
    if resource.timestamp:
        lines.append(f"  timestamp: {resource.timestamp}")
    if resource.metadata:
        lines.append(f"  metadata: {_json(resource.metadata)}")
    return lines


def _section(label: str, value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    return [f"{label}:", _json(value)]


def render_entry(
    entry: JournalEntry,
    *,
    document_id: str | None = None,
    parser_version: str = config.PARSER_VERSION,
) -> RenderedDocument:
    """Render one parsed entry into a complete async-retain payload."""

    title = entry.title or "(Untitled entry)"
    lines = [f"# {title}", f"Date: {entry.entry_date}", "", "Written text:"]
    lines.append(entry.body or "(No written text.)")

    if entry.resources:
        lines.extend(["", "Attachments:"])
        for resource in entry.resources:
            lines.extend(_attachment_lines(resource))
    lines.extend(_section("Locations", entry.locations))
    lines.extend(_section("Music", entry.music))
    lines.extend(_section("Workouts", entry.workouts))
    lines.extend(_section("Labels", entry.labels))
    lines.extend(_section("Associations", entry.associations))
    if entry.transcript:
        lines.extend(["", "Apple audio transcript:", entry.transcript])

    tags = [config.SOURCE_TAG, config.TYPE_TAG]
    if entry.entry_date:
        tags.append(f"year:{entry.entry_date[:4]}")
    attachment_kinds = {resource.kind for resource in entry.resources if resource.kind}
    attachment_kinds.update(
        resource.sidecar_type for resource in entry.resources if resource.sidecar_type
    )
    if entry.transcript:
        attachment_kinds.add("transcript")
    for kind in sorted(attachment_kinds):
        tags.append(f"attachment:{kind}")

    metadata: dict[str, Any] = {
        "source_path": entry.source_path,
        "entry_date": entry.entry_date,
        "parser_version": parser_version,
        "content_fingerprint": entry.source_fingerprint,
        "source_fingerprint": entry.source_fingerprint,
        "body_fingerprint": entry.body_fingerprint,
        "attachment_fingerprint": entry.attachment_fingerprint,
        "attachment_summary": entry.attachment_summary,
    }
    if entry.title:
        metadata["title"] = entry.title

    return RenderedDocument(
        document_id=document_id or document_id_for_source(entry.source_path),
        content="\n".join(lines).strip() + "\n",
        context=config.RETAIN_CONTEXT,
        tags=tags,
        metadata=metadata,
        timestamp=entry.entry_date,
        source_fingerprint=entry.source_fingerprint,
        body_fingerprint=entry.body_fingerprint,
        attachment_fingerprint=entry.attachment_fingerprint,
        source_path=entry.source_path,
        entry_date=entry.entry_date,
    )
