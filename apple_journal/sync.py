"""Incremental planning, applying, and operation reconciliation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import config
from .hindsight import HindsightClient, HindsightError
from .ledger import FAILED, PENDING, STORED, Ledger
from .models import JournalEntry, RemoteDocument, SyncPlan, SyncPlanEntry
from .render import document_id_for_source, render_entry
from .parser import export_manifest


class SyncError(RuntimeError):
    """A safe-to-report synchronization error."""


class StalePlanError(SyncError):
    """The export or destination changed since plan creation."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_journal_document(document: RemoteDocument) -> bool:
    if document.document_id.startswith("apple-journal:"):
        return True
    metadata = document.metadata
    if metadata.get("source") == "apple-journal":
        return True
    tags = metadata.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    return config.SOURCE_TAG in tags


def _score(raw: dict[str, Any]) -> float | None:
    for key in ("score", "similarity", "distance_score", "relevance"):
        value = raw.get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _semantic_flags(response: dict[str, Any], threshold: float) -> list[dict[str, Any]]:
    if isinstance(response, list):
        values = response
    else:
        values = response.get("results") or response.get("matches") or response.get("items") or []
    flags: list[dict[str, Any]] = []
    if not isinstance(values, list):
        return flags
    for raw in values:
        if not isinstance(raw, dict):
            continue
        score = _score(raw)
        if score is None or score < threshold:
            continue
        nested = raw.get("document") if isinstance(raw.get("document"), dict) else raw
        document_id = str(
            nested.get("document_id")
            or nested.get("documentId")
            or nested.get("id")
            or raw.get("document_id")
            or raw.get("id")
            or ""
        )
        if document_id:
            flags.append({"document_id": document_id, "score": score})
    return flags


def _source_path_matches(documents: Iterable[RemoteDocument], source_path: str) -> list[RemoteDocument]:
    return [doc for doc in documents if doc.source_path == source_path]


def _fingerprint_matches(
    documents: Iterable[RemoteDocument], body_fingerprint: str, attachment_fingerprint: str
) -> list[RemoteDocument]:
    if not body_fingerprint or not attachment_fingerprint:
        return []
    return [
        doc
        for doc in documents
        if doc.body_fingerprint == body_fingerprint
        and doc.attachment_fingerprint == attachment_fingerprint
    ]


def _plan_entry_as_dict(entry: SyncPlanEntry) -> dict[str, Any]:
    return asdict(entry)


def save_plan(plan: SyncPlan, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": plan.schema_version,
        "parser_version": plan.parser_version,
        "bank": plan.bank,
        "endpoint": plan.endpoint,
        "export_root": plan.export_root,
        "export_manifest": plan.export_manifest,
        "created_at": plan.created_at,
        "missing_source_documents": sorted(plan.missing_source_documents),
        "entries": [_plan_entry_as_dict(entry) for entry in plan.entries],
    }
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def load_plan(path: str | Path) -> SyncPlan:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SyncError(f"could not read plan {target}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SyncError(f"plan {target} is not a JSON object")
    entries: list[SyncPlanEntry] = []
    for raw in payload.get("entries", []):
        if not isinstance(raw, dict):
            continue
        fields = set(SyncPlanEntry.__dataclass_fields__)
        data = {key: value for key, value in raw.items() if key in fields}
        data.setdefault("attachment_types", [])
        data.setdefault("semantic_matches", [])
        data.setdefault("action", "new")
        data.setdefault("identity_conflict", False)
        data.setdefault("identity_match", "")
        data.setdefault("reason", "")
        entries.append(SyncPlanEntry(**data))
    return SyncPlan(
        schema_version=int(payload.get("schema_version", 0)),
        parser_version=str(payload.get("parser_version", "")),
        bank=str(payload.get("bank", "")),
        endpoint=str(payload.get("endpoint", "")),
        export_root=str(payload.get("export_root", "")),
        export_manifest=str(payload.get("export_manifest", "")),
        entries=entries,
        missing_source_documents=[str(value) for value in payload.get("missing_source_documents", [])],
        created_at=str(payload.get("created_at", "")),
    )


def _set_client_bank(client: Any, bank: str) -> None:
    if hasattr(client, "bank"):
        client.bank = bank


def build_plan(
    entries: list[JournalEntry],
    *,
    export_root: str | Path,
    bank: str,
    endpoint: str,
    client: HindsightClient,
    ledger: Ledger | None = None,
    parser_version: str = config.PARSER_VERSION,
    semantic: bool = True,
    semantic_threshold: float = config.REVIEW_THRESHOLD,
) -> SyncPlan:
    """Reconcile a fresh export against the live bank and ledger."""

    _set_client_bank(client, bank)
    root = Path(export_root).expanduser().resolve()
    ledger = ledger or Ledger(root / config.DEFAULT_OUTPUT_DIR / config.DEFAULT_LEDGER_NAME)
    try:
        live = client.list_documents(bank)
    except Exception as exc:  # noqa: BLE001
        raise SyncError(f"could not list live Hindsight documents: {exc}") from exc
    journal_docs = [doc for doc in live if _is_journal_document(doc)]
    by_id = {doc.document_id: doc for doc in journal_docs}
    plan_entries: list[SyncPlanEntry] = []
    selected_ids: set[str] = set()

    for entry in entries:
        default_id = document_id_for_source(entry.source_path)
        remote: RemoteDocument | None = by_id.get(default_id)
        identity_match = "document_id" if remote else ""
        identity_conflict = False
        reason = ""

        if remote is None:
            source_matches = _source_path_matches(journal_docs, entry.source_path)
            if len(source_matches) == 1:
                remote = source_matches[0]
                identity_match = "source_path"
            elif len(source_matches) > 1:
                identity_conflict = True
                reason = "multiple live documents share source_path"

        if remote is None and not identity_conflict:
            fingerprint_matches = _fingerprint_matches(
                journal_docs, entry.body_fingerprint, entry.attachment_fingerprint
            )
            if len(fingerprint_matches) == 1:
                remote = fingerprint_matches[0]
                identity_match = "body_attachment_fingerprint"
            elif len(fingerprint_matches) > 1:
                identity_conflict = True
                reason = "body/attachment fingerprint matches multiple live documents"

        # Some Hindsight document-list versions return IDs without metadata.
        # The hash-only ledger can still preserve a unique rename identity,
        # but only when that ledger document is also present in the live list.
        if remote is None and not identity_conflict:
            ledger_matches = [
                item
                for item in ledger.by_fingerprints(
                    entry.body_fingerprint, entry.attachment_fingerprint
                )
                if item.document_id in by_id
            ]
            if len(ledger_matches) == 1:
                remote = by_id[ledger_matches[0].document_id]
                identity_match = "ledger_body_attachment_fingerprint"
            elif len(ledger_matches) > 1:
                identity_conflict = True
                reason = "ledger body/attachment fingerprint matches multiple live documents"

        ledger_entry = None
        if remote is not None:
            ledger_entry = ledger.get(remote.document_id)
        if ledger_entry is None:
            ledger_entry = ledger.get(default_id)
        if ledger_entry is None:
            path_entries = ledger.by_source_path(entry.source_path)
            if len(path_entries) == 1:
                ledger_entry = path_entries[0]

        document_id = remote.document_id if remote is not None else default_id
        if identity_conflict:
            action = "identity_conflict"
        elif ledger_entry and ledger_entry.source_fingerprint == entry.source_fingerprint:
            if ledger_entry.status == PENDING:
                action = "pending"
                reason = "previous retain operation is still pending"
            elif ledger_entry.status == FAILED:
                action = "failed"
                reason = "previous retain operation failed; use retry-failed --apply"
            elif remote is not None and remote.source_fingerprint == entry.source_fingerprint:
                action = "skip"
                reason = "live source fingerprint matches"
            elif remote is None and ledger_entry.status == STORED:
                action = "new"
                reason = "ledger says stored but live document is absent"
            else:
                action = "changed"
        elif remote is not None and remote.source_fingerprint == entry.source_fingerprint:
            action = "skip"
            reason = "live source fingerprint matches"
        elif remote is not None:
            action = "changed"
            reason = "live document fingerprint differs"
        elif ledger_entry and ledger_entry.status == PENDING and ledger_entry.source_fingerprint == entry.source_fingerprint:
            action = "pending"
            reason = "previous retain operation is still pending"
        elif ledger_entry and ledger_entry.status == FAILED and ledger_entry.source_fingerprint == entry.source_fingerprint:
            action = "failed"
            reason = "previous retain operation failed; use retry-failed --apply"
        else:
            action = "new"
            reason = "no matching live journal document"

        semantic_matches: list[dict[str, Any]] = []
        if action == "new" and semantic:
            query = entry.body.strip() or entry.title.strip()
            if query:
                try:
                    semantic_matches = _semantic_flags(
                        client.recall(query, bank=bank), semantic_threshold
                    )
                except Exception as exc:  # noqa: BLE001
                    reason = f"{reason}; semantic recall unavailable: {type(exc).__name__}"

        if remote is not None:
            selected_ids.add(remote.document_id)
        plan_entries.append(
            SyncPlanEntry(
                document_id=document_id,
                source_path=entry.source_path,
                entry_date=entry.entry_date,
                source_fingerprint=entry.source_fingerprint,
                body_fingerprint=entry.body_fingerprint,
                attachment_fingerprint=entry.attachment_fingerprint,
                attachment_types=sorted(
                    {
                        resource.kind
                        for resource in entry.resources
                        if resource.kind
                    }
                    | {
                        resource.sidecar_type
                        for resource in entry.resources
                        if resource.sidecar_type
                    }
                    | ({"transcript"} if entry.transcript else set())
                ),
                action=action,
                identity_conflict=identity_conflict,
                identity_match=identity_match,
                semantic_matches=semantic_matches,
                reason=reason,
            )
        )

    missing = sorted(doc.document_id for doc in journal_docs if doc.document_id not in selected_ids)
    try:
        normalized_endpoint = client.endpoint
    except AttributeError:
        normalized_endpoint = endpoint.rstrip("/")
    return SyncPlan(
        schema_version=config.PLAN_SCHEMA_VERSION,
        parser_version=parser_version,
        bank=bank,
        endpoint=normalized_endpoint,
        export_root=str(root),
        export_manifest=export_manifest(entries),
        entries=plan_entries,
        missing_source_documents=missing,
        created_at=_now(),
    )


def verify_plan(
    plan: SyncPlan,
    entries: list[JournalEntry],
    *,
    export_root: str | Path,
    bank: str,
    endpoint: str,
    parser_version: str = config.PARSER_VERSION,
) -> None:
    current_root = str(Path(export_root).expanduser().resolve())
    current_endpoint = endpoint.rstrip("/")
    problems: list[str] = []
    if plan.schema_version != config.PLAN_SCHEMA_VERSION:
        problems.append("plan schema version changed")
    if plan.parser_version != parser_version:
        problems.append("parser version changed")
    if plan.bank != bank:
        problems.append("bank changed")
    if plan.endpoint.rstrip("/") != current_endpoint:
        problems.append("Hindsight endpoint changed")
    if plan.export_root != current_root:
        problems.append("export path changed")
    current_manifest = export_manifest(entries)
    if plan.export_manifest != current_manifest:
        problems.append("export manifest changed")
    if problems:
        raise StalePlanError("stale plan: " + "; ".join(problems))


def apply_plan(
    plan: SyncPlan,
    entries: list[JournalEntry],
    *,
    export_root: str | Path,
    bank: str,
    endpoint: str,
    client: HindsightClient,
    ledger: Ledger,
    parser_version: str = config.PARSER_VERSION,
) -> dict[str, int]:
    """Verify and asynchronously queue only new/changed plan entries."""

    verify_plan(
        plan,
        entries,
        export_root=export_root,
        bank=bank,
        endpoint=endpoint,
        parser_version=parser_version,
    )
    _set_client_bank(client, bank)
    by_path = {entry.source_path: entry for entry in entries}
    counts = {"queued": 0, "skipped": 0, "pending": 0, "failed": 0, "conflicts": 0}
    for planned in plan.entries:
        if planned.action in {"skip"}:
            counts["skipped"] += 1
            continue
        if planned.action == "pending":
            counts["pending"] += 1
            continue
        if planned.action == "failed":
            counts["failed"] += 1
            continue
        if planned.identity_conflict or planned.action == "identity_conflict":
            counts["conflicts"] += 1
            continue
        if planned.action not in {"new", "changed"}:
            continue
        entry = by_path.get(planned.source_path)
        if entry is None:
            raise StalePlanError(f"source entry disappeared: {planned.source_path}")
        rendered = render_entry(entry, document_id=planned.document_id, parser_version=parser_version)
        try:
            operation_id = client.retain(rendered, bank=bank)
        except Exception as exc:  # noqa: BLE001
            counts["failed"] += 1
            print(f"[apply] failed document_id={rendered.document_id} source_path={rendered.source_path} error={exc}")
            continue
        ledger.mark_pending(
            rendered.document_id,
            source_path=rendered.source_path,
            source_fingerprint=rendered.source_fingerprint,
            body_fingerprint=rendered.body_fingerprint,
            attachment_fingerprint=rendered.attachment_fingerprint,
            operation_id=operation_id,
        )
        ledger.save()
        counts["queued"] += 1
    ledger.save()
    return counts


def refresh_status(client: HindsightClient, ledger: Ledger, *, bank: str) -> dict[str, int]:
    """Poll operation rows once and update only server-confirmed statuses."""

    _set_client_bank(client, bank)
    for entry in list(ledger.entries.values()):
        if entry.status != PENDING or not entry.operation_id:
            continue
        try:
            result = client.operation(entry.operation_id, bank=bank)
        except Exception:
            continue
        status = str(result.get("status") or result.get("state") or "").lower()
        if status in {"completed", "complete", "success", "succeeded"}:
            ledger.mark_stored(entry.document_id)
        elif status in {"failed", "cancelled", "canceled", "error"}:
            error = str(result.get("error_message") or result.get("error") or status)
            ledger.mark_failed(entry.document_id, error)
    ledger.save()
    counts = ledger.counts()
    counts.setdefault(PENDING, 0)
    counts.setdefault(STORED, 0)
    counts.setdefault(FAILED, 0)
    return counts


def retry_failed(
    client: HindsightClient,
    ledger: Ledger,
    *,
    bank: str,
    apply: bool = False,
) -> list[dict[str, Any]]:
    """List failures by default; retry them only with explicit apply=True."""

    _set_client_bank(client, bank)
    failures = [entry for entry in ledger.entries.values() if entry.status == FAILED]
    report: list[dict[str, Any]] = []
    for entry in failures:
        item: dict[str, Any] = {
            "document_id": entry.document_id,
            "operation_id": entry.operation_id,
            "status": entry.status,
        }
        if apply:
            try:
                new_operation_id = client.retry_operation(entry.operation_id, bank=bank)
            except Exception as exc:  # noqa: BLE001
                item["error"] = type(exc).__name__
            else:
                ledger.retry_pending(entry.document_id, new_operation_id)
                item["new_operation_id"] = new_operation_id
                item["status"] = PENDING
        report.append(item)
    if apply:
        ledger.save()
    return report
