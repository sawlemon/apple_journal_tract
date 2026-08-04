from __future__ import annotations

import unittest

from apple_journal.models import JournalEntry, ResourceAttachment
from apple_journal.render import document_id_for_source, render_entry


def make_entry(body: str = "first-person text") -> JournalEntry:
    return JournalEntry(
        source_path="Entries/2024-01-02_😀.html",
        entry_date="2024-01-02",
        title="A synthetic title",
        body=body,
        resources=[
            ResourceAttachment(
                resource_id="resource-1",
                kind="audio",
                filename="recording.m4a",
                sidecar_type="transcript",
                timestamp="2024-01-02T01:02:03Z",
                metadata={"duration": 12},
            )
        ],
        transcript="synthetic transcript",
        source_fingerprint="source-fp-" + body,
        body_fingerprint="body-fp-" + body,
        attachment_fingerprint="attachment-fp",
    )


class RenderTests(unittest.TestCase):
    def test_identity_is_stable_for_body_edits(self) -> None:
        first = render_entry(make_entry("one"))
        changed = render_entry(make_entry("two"))
        self.assertEqual(first.document_id, changed.document_id)
        self.assertEqual(first.document_id, document_id_for_source("Entries/2024-01-02_😀.html"))
        self.assertNotEqual(first.metadata["body_fingerprint"], changed.metadata["body_fingerprint"])

    def test_render_contains_structured_metadata_and_tags(self) -> None:
        document = render_entry(make_entry())
        self.assertIn("source:apple-journal", document.tags)
        self.assertIn("type:journal-entry", document.tags)
        self.assertIn("year:2024", document.tags)
        self.assertIn("attachment:audio", document.tags)
        self.assertIn("attachment:transcript", document.tags)
        self.assertIn("synthetic transcript", document.content)
        self.assertEqual(document.metadata["entry_date"], "2024-01-02")
        self.assertEqual(document.timestamp, "2024-01-02")

    def test_reconciliation_can_override_identity_for_rename(self) -> None:
        document = render_entry(make_entry(), document_id="apple-journal:prior")
        self.assertEqual(document.document_id, "apple-journal:prior")


if __name__ == "__main__":
    unittest.main()
