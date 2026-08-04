from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from apple_journal.hindsight import HindsightClient
from apple_journal.models import JournalEntry
from apple_journal.render import render_entry


class _FakeHindsightHandler(BaseHTTPRequestHandler):
    received_posts: list[tuple[str, dict]] = []

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _send(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/health":
            self._send({"status": "ok"})
        elif path == "/v1/default/banks/default":
            self._send({"id": "default"})
        elif path == "/v1/default/banks/default/documents":
            self._send(
                {
                    "items": [
                        {
                            "id": "apple-journal:existing",
                            "metadata": {"source_path": "Entries/existing.html"},
                        }
                    ],
                    "total": 1,
                }
            )
        elif path == "/v1/default/banks/default/operations/op-1":
            self._send({"operation_id": "op-1", "status": "completed"})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        payload = json.loads(body.decode("utf-8")) if body else {}
        path = urlsplit(self.path).path
        self.received_posts.append((path, payload))
        if path == "/v1/default/banks/default/memories":
            self._send({"success": True, "operation_id": "op-1"})
        elif path.endswith("/operations/op-1/retry"):
            self._send({"operation_id": "op-2"})
        elif path == "/v1/default/banks/default/memories/recall":
            self._send({"results": [{"id": "apple-journal:existing", "score": 0.9}]})
        else:
            self._send({"error": "not found"}, 404)


class HindsightHTTPClientTests(unittest.TestCase):
    def test_fake_server_exercises_rest_operations(self) -> None:
        _FakeHindsightHandler.received_posts = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHindsightHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            client = HindsightClient(f"http://{host}:{port}/mcp/agents", bank="default")
            self.assertEqual(client.endpoint, f"http://{host}:{port}")
            self.assertEqual(client.health()["status"], "ok")
            self.assertEqual(client.bank_info()["id"], "default")
            self.assertEqual(client.list_documents()[0].document_id, "apple-journal:existing")
            document = render_entry(
                JournalEntry(
                    source_path="Entries/new.html",
                    entry_date="2024-01-02",
                    title="synthetic",
                    body="synthetic body",
                    source_fingerprint="a" * 64,
                    body_fingerprint="b" * 64,
                    attachment_fingerprint="c" * 64,
                )
            )
            self.assertEqual(client.retain(document), "op-1")
            self.assertEqual(client.operation("op-1")["status"], "completed")
            self.assertEqual(client.retry_operation("op-1"), "op-2")
            self.assertEqual(client.recall("synthetic")["results"][0]["score"], 0.9)
            retain_posts = [payload for path, payload in _FakeHindsightHandler.received_posts if path.endswith("/memories")]
            self.assertTrue(retain_posts[0]["async"])
            self.assertEqual(retain_posts[0]["items"][0]["update_mode"], "replace")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
