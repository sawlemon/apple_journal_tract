from __future__ import annotations

import tempfile
import unittest
import hashlib
from pathlib import Path

from apple_journal import config
from apple_journal.hindsight import normalize_rest_base
from apple_journal.ledger import FAILED, PENDING, STORED, Ledger
from apple_journal.models import JournalEntry, RemoteDocument, ResourceAttachment
from apple_journal.render import render_entry
from apple_journal.sync import (
    StalePlanError,
    _truncate_query,
    apply_plan,
    build_plan,
    retry_failed,
    refresh_status,
)


class FakeClient:
    endpoint = "http://hindsight.test"

    def __init__(self) -> None:
        self.documents: list[RemoteDocument] = []
        self.operations: dict[str, str] = {}
        self.retains = []
        self.recall_response = {"results": []}
        self.recall_queries: list[str] = []
        self.next_operation = 1
        self.retry_calls = 0
        self.bank = "default"

    def list_documents(self, bank: str):
        self.bank = bank
        return list(self.documents)

    def retain(self, document, *, bank: str):
        self.bank = bank
        self.retains.append(document)
        operation_id = f"op-{self.next_operation}"
        self.next_operation += 1
        self.operations[operation_id] = "pending"
        self.documents = [doc for doc in self.documents if doc.document_id != document.document_id]
        self.documents.append(
            RemoteDocument(
                document.document_id,
                {
                    "source_path": document.source_path,
                    "content_fingerprint": document.source_fingerprint,
                    "body_fingerprint": document.body_fingerprint,
                    "attachment_fingerprint": document.attachment_fingerprint,
                    "tags": document.tags,
                },
            )
        )
        return operation_id

    def operation(self, operation_id: str, *, bank: str):
        return {"operation_id": operation_id, "status": self.operations.get(operation_id, "not_found")}

    def retry_operation(self, operation_id: str, *, bank: str):
        self.retry_calls += 1
        new_id = f"retry-{self.retry_calls}"
        self.operations[new_id] = "pending"
        return new_id

    def recall(self, query: str, *, bank: str, max_tokens: int = 512):
        self.recall_queries.append(query)
        return self.recall_response


def make_entry(source_path: str = "Entries/2024-01-02.html", body: str = "body-v1") -> JournalEntry:
    body_fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()
    source_fingerprint = hashlib.sha256(f"{source_path}\0{body}".encode("utf-8")).hexdigest()
    attachment_fingerprint = hashlib.sha256(b"attachments-v1").hexdigest()
    return JournalEntry(
        source_path=source_path,
        entry_date="2024-01-02",
        title="synthetic",
        body=body,
        resources=[ResourceAttachment(resource_id="photo-1", kind="photo", filename="photo.png")],
        source_fingerprint=source_fingerprint,
        body_fingerprint=body_fingerprint,
        attachment_fingerprint=attachment_fingerprint,
    )


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "export"
        self.root.mkdir()
        (self.root / "index.html").write_text("", encoding="utf-8")
        self.ledger = Ledger(Path(self.temporary.name) / "ledger.json")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def plan(self, client: FakeClient, entries: list[JournalEntry], **kwargs):
        semantic = kwargs.pop("semantic", False)
        return build_plan(
            entries,
            export_root=self.root,
            bank="default",
            endpoint=client.endpoint,
            client=client,
            ledger=self.ledger,
            semantic=semantic,
            **kwargs,
        )

    def apply(self, client: FakeClient, plan, entries: list[JournalEntry], **kwargs):
        return apply_plan(
            plan,
            entries,
            export_root=self.root,
            bank="default",
            endpoint=client.endpoint,
            client=client,
            ledger=self.ledger,
            **kwargs,
        )

    def test_first_run_new_and_identical_completed_rerun_skips(self) -> None:
        client = FakeClient()
        entry = make_entry()
        first = self.plan(client, [entry])
        self.assertEqual(first.entries[0].action, "new")
        self.assertEqual(self.apply(client, first, [entry])["queued"], 1)
        operation_id = next(iter(client.operations))
        client.operations[operation_id] = "completed"
        self.assertEqual(refresh_status(client, self.ledger, bank="default")[STORED], 1)
        second = self.plan(client, [entry])
        self.assertEqual(second.entries[0].action, "skip")

    def test_changed_body_uses_replace_and_preserves_identity(self) -> None:
        client = FakeClient()
        original = make_entry(body="old")
        first = self.plan(client, [original])
        self.apply(client, first, [original])
        client.operations[next(iter(client.operations))] = "completed"
        refresh_status(client, self.ledger, bank="default")
        changed = make_entry(body="new")
        plan = self.plan(client, [changed])
        self.assertEqual(plan.entries[0].action, "changed")
        self.assertEqual(plan.entries[0].document_id, first.entries[0].document_id)
        self.assertEqual(self.apply(client, plan, [changed])["queued"], 1)
        self.assertEqual(client.retains[-1].document_id, first.entries[0].document_id)

    def test_pending_entry_is_not_requeued(self) -> None:
        client = FakeClient()
        entry = make_entry()
        first = self.plan(client, [entry])
        self.apply(client, first, [entry])
        pending = self.plan(client, [entry])
        self.assertEqual(pending.entries[0].action, PENDING)
        self.assertEqual(self.apply(client, pending, [entry])["queued"], 0)
        self.assertEqual(len(client.retains), 1)

    def test_failed_entry_requires_explicit_retry(self) -> None:
        client = FakeClient()
        entry = make_entry()
        first = self.plan(client, [entry])
        self.apply(client, first, [entry])
        operation_id = next(iter(client.operations))
        client.operations[operation_id] = "failed"
        refresh_status(client, self.ledger, bank="default")
        failed_plan = self.plan(client, [entry])
        self.assertEqual(failed_plan.entries[0].action, "failed")
        dry_run = retry_failed(client, self.ledger, bank="default", apply=False)
        self.assertEqual(len(dry_run), 1)
        self.assertEqual(client.retry_calls, 0)
        applied = retry_failed(client, self.ledger, bank="default", apply=True)
        self.assertEqual(applied[0]["status"], PENDING)
        self.assertEqual(client.retry_calls, 1)

    def test_missing_source_is_reported_but_not_deleted(self) -> None:
        client = FakeClient()
        client.documents = [
            RemoteDocument(
                "apple-journal:old",
                {"source_path": "Entries/old.html", "tags": ["source:apple-journal"]},
            )
        ]
        plan = self.plan(client, [make_entry()])
        self.assertEqual(plan.missing_source_documents, ["apple-journal:old"])
        self.assertEqual(len(client.documents), 1)

    def test_ambiguous_rename_blocks_import(self) -> None:
        client = FakeClient()
        renamed = make_entry("Entries/renamed.html", "new")
        metadata = {
            "source_path": "Entries/old-a.html",
            "body_fingerprint": renamed.body_fingerprint,
            "attachment_fingerprint": renamed.attachment_fingerprint,
            "content_fingerprint": "old-source-a",
            "tags": ["source:apple-journal"],
        }
        client.documents = [
            RemoteDocument("apple-journal:a", metadata),
            RemoteDocument("apple-journal:b", {**metadata, "source_path": "Entries/old-b.html"}),
        ]
        plan = self.plan(client, [renamed])
        self.assertTrue(plan.entries[0].identity_conflict)
        self.assertEqual(plan.entries[0].action, "identity_conflict")
        self.assertEqual(self.apply(client, plan, [renamed])["queued"], 0)

    def test_semantic_candidate_is_advisory_only(self) -> None:
        client = FakeClient()
        client.recall_response = {
            "results": [{"id": "apple-journal:similar", "score": 0.99}]
        }
        entry = make_entry()
        plan = self.plan(client, [entry], semantic=True)
        self.assertEqual(plan.entries[0].action, "new")
        self.assertEqual(plan.entries[0].semantic_matches[0]["document_id"], "apple-journal:similar")
        self.assertEqual(self.apply(client, plan, [entry])["queued"], 1)

    def test_recall_query_is_truncated_under_server_token_limit(self) -> None:
        client = FakeClient()
        long_body = " ".join(f"word-{index}" for index in range(config.RECALL_QUERY_MAX_TOKENS + 500))
        entry = make_entry(body=long_body)
        plan = self.plan(client, [entry], semantic=True)
        self.assertEqual(plan.entries[0].action, "new")
        self.assertEqual(len(client.recall_queries), 1)
        sent = client.recall_queries[0].split()
        self.assertLessEqual(len(sent), config.RECALL_QUERY_MAX_TOKENS)
        self.assertEqual(sent, long_body.split()[: config.RECALL_QUERY_MAX_TOKENS])

    def test_short_recall_query_is_left_intact(self) -> None:
        client = FakeClient()
        entry = make_entry(body="short synthetic body")
        self.plan(client, [entry], semantic=True)
        self.assertEqual(client.recall_queries, ["short synthetic body"])

    def test_truncate_query_caps_long_text(self) -> None:
        text = " ".join(f"word-{index}" for index in range(config.RECALL_QUERY_MAX_TOKENS + 100))
        truncated = _truncate_query(text)
        self.assertEqual(len(truncated.split()), config.RECALL_QUERY_MAX_TOKENS)
        self.assertEqual(_truncate_query("tiny"), "tiny")

    def test_stale_plan_rejects_export_change(self) -> None:
        client = FakeClient()
        entry = make_entry()
        plan = self.plan(client, [entry])
        with self.assertRaises(StalePlanError):
            self.apply(client, plan, [make_entry(body="edited")])

    def test_ledger_does_not_store_journal_body(self) -> None:
        client = FakeClient()
        entry = make_entry(body="private synthetic journal body")
        plan = self.plan(client, [entry])
        self.apply(client, plan, [entry])
        payload = self.ledger.path.read_text(encoding="utf-8")
        self.assertNotIn(entry.body, payload)
        self.assertIn(entry.body_fingerprint, payload)

    def test_mcp_url_normalization(self) -> None:
        self.assertEqual(normalize_rest_base("http://host:8888/mcp"), "http://host:8888")
        self.assertEqual(normalize_rest_base("http://host:8888/mcp/agents"), "http://host:8888")
        self.assertEqual(normalize_rest_base("https://host.example/base"), "https://host.example/base")


if __name__ == "__main__":
    unittest.main()
