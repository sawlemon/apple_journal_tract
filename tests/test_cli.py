from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import apple_journal_tract
from apple_journal.models import RemoteDocument
from apple_journal.sync import load_plan


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeCLIClient:
    endpoint = "http://fake"

    def __init__(self) -> None:
        self.documents: list[RemoteDocument] = []
        self.operation_id = "op-cli"

    def list_documents(self, bank):
        return list(self.documents)

    def recall(self, query, *, bank, max_tokens=512):
        return {"results": []}

    def retain(self, document, *, bank):
        self.documents.append(
            RemoteDocument(
                document.document_id,
                {
                    "source_path": document.source_path,
                    "content_fingerprint": document.source_fingerprint,
                    "body_fingerprint": document.body_fingerprint,
                    "attachment_fingerprint": document.attachment_fingerprint,
                },
            )
        )
        return self.operation_id


class CLITests(unittest.TestCase):
    def test_plan_and_apply_cli_use_content_free_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path = root / "plan.json"
            ledger_path = root / "ledger.json"
            fake = FakeCLIClient()
            with patch.object(apple_journal_tract, "_client", return_value=fake):
                self.assertEqual(
                    apple_journal_tract.main(
                        [
                            "plan",
                            "--export",
                            str(PROJECT_ROOT / "fixtures" / "minimal_export"),
                            "--bank",
                            "default",
                            "--hindsight-url",
                            "http://fake",
                            "--output",
                            str(plan_path),
                            "--ledger",
                            str(ledger_path),
                            "--no-semantic",
                        ]
                    ),
                    0,
                )
                plan = load_plan(plan_path)
                self.assertEqual(len(plan.entries), 1)
                plan_text = plan_path.read_text(encoding="utf-8")
                self.assertNotIn("Line one", plan_text)
                self.assertNotIn('"body"', plan_text)
                self.assertEqual(
                    apple_journal_tract.main(
                        [
                            "apply",
                            "--plan",
                            str(plan_path),
                            "--hindsight-url",
                            "http://fake",
                            "--ledger",
                            str(ledger_path),
                        ]
                    ),
                    0,
                )
            self.assertEqual(len(fake.documents), 1)


if __name__ == "__main__":
    unittest.main()
