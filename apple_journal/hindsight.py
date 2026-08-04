"""Small standard-library client for the Hindsight REST surface."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from . import config
from .models import RemoteDocument, RenderedDocument


class HindsightError(RuntimeError):
    """A transport or server-side Hindsight error."""


class HindsightHTTPError(HindsightError):
    def __init__(self, status: int, url: str, message: str) -> None:
        self.status = status
        self.url = url
        self.message = message
        super().__init__(f"Hindsight HTTP {status} for {url}: {message}")


def normalize_rest_base(url: str) -> str:
    """Normalize an MCP URL or REST URL to its Hindsight REST root."""

    value = (url or "").strip()
    if not value:
        raise ValueError("HINDSIGHT_MCP_URL is required for live operations")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("HINDSIGHT_MCP_URL must be an absolute http(s) URL")
    parts = [part for part in parsed.path.split("/") if part]
    lowered = [part.lower() for part in parts]
    if "mcp" in lowered:
        parts = parts[: lowered.index("mcp")]
    # A caller may provide the API namespace itself; preserve only the root so
    # the bank path is constructed in one place.
    path = "/" + "/".join(parts) if parts else ""
    return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", "")).rstrip("/")


def _json_response(data: bytes, url: str, status: int = 200) -> Any:
    if not data:
        return {}
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HindsightError(f"Hindsight returned non-JSON data from {url}") from exc


Transport = Callable[[str, str, Any | None, float], Any]


@dataclass
class HindsightClient:
    """REST client with optional injectable transport for tests."""

    endpoint: str
    bank: str = config.DEFAULT_BANK
    transport: Transport | None = None
    timeout: float = config.HTTP_TIMEOUT

    def __post_init__(self) -> None:
        self.endpoint = normalize_rest_base(self.endpoint)

    def _bank_url(self, suffix: str = "", bank: str | None = None) -> str:
        bank_id = quote(bank or self.bank, safe="")
        return f"{self.endpoint}/v1/default/banks/{bank_id}{suffix}"

    def request(self, method: str, url: str, payload: Any | None = None, *, timeout: float | None = None) -> Any:
        request_timeout = timeout if timeout is not None else self.timeout
        if self.transport is not None:
            try:
                result = self.transport(method, url, payload, request_timeout)
            except HindsightError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise HindsightError(str(exc)) from exc
            if isinstance(result, tuple) and len(result) == 2:
                status, data = result
                if int(status) >= 400:
                    raise HindsightHTTPError(int(status), url, str(data))
                return data
            return result

        body: bytes | None = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(url, data=body, headers=headers, method=method.upper())
        try:
            with urlopen(req, timeout=request_timeout) as response:
                return _json_response(response.read(), url, response.status)
        except HTTPError as exc:
            try:
                message = exc.read().decode("utf-8", errors="replace")
            except OSError:
                message = str(exc)
            raise HindsightHTTPError(exc.code, url, message[:1000]) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise HindsightError(f"request failed for {url}: {exc}") from exc

    def health(self) -> dict[str, Any]:
        response = self.request("GET", f"{self.endpoint}/health", timeout=15)
        return response if isinstance(response, dict) else {"response": response}

    def bank_info(self, bank: str | None = None) -> dict[str, Any]:
        response = self.request("GET", self._bank_url(bank=bank))
        return response if isinstance(response, dict) else {"response": response}

    def verify_bank(self, bank: str | None = None) -> dict[str, Any]:
        self.health()
        return self.bank_info(bank)

    @staticmethod
    def _items(response: Any, *keys: str) -> tuple[list[Any], int | None]:
        if isinstance(response, list):
            return response, None
        if not isinstance(response, dict):
            return [], None
        for key in keys:
            value = response.get(key)
            if isinstance(value, list):
                total = response.get("total")
                try:
                    total = int(total) if total is not None else None
                except (TypeError, ValueError):
                    total = None
                return value, total
            if isinstance(value, dict):
                return HindsightClient._items(value, "items", "documents", "data", "results")
        return [], None

    def list_documents(self, bank: str | None = None, *, page_size: int = 200) -> list[RemoteDocument]:
        documents: list[RemoteDocument] = []
        offset = 0
        while True:
            url = self._bank_url(f"/documents?limit={page_size}&offset={offset}", bank)
            response = self.request("GET", url)
            items, total = self._items(response, "items", "documents", "data", "results")
            if not items:
                break
            for raw in items:
                if not isinstance(raw, dict):
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
                if not document_id:
                    continue
                metadata = nested.get("metadata") or raw.get("metadata") or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError:
                        metadata = {"raw_metadata": metadata}
                if not isinstance(metadata, dict):
                    metadata = {"raw_metadata": metadata}
                documents.append(RemoteDocument(document_id, metadata))
            offset += len(items)
            if total is not None and offset >= total:
                break
            if len(items) < page_size:
                break
        return documents

    def retain(self, document: RenderedDocument, *, bank: str | None = None) -> str:
        item = {
            "content": document.content,
            "context": document.context,
            "document_id": document.document_id,
            "tags": document.tags,
            "metadata": document.metadata,
            "timestamp": document.timestamp,
            "update_mode": "replace",
        }
        response = self.request(
            "POST",
            self._bank_url("/memories", bank),
            {"items": [item], "async": True},
            timeout=max(self.timeout, 120),
        )
        if not isinstance(response, dict):
            raise HindsightError(f"retain returned an unexpected response: {response!r}")
        operation_id = response.get("operation_id")
        if not operation_id:
            operation_ids = response.get("operation_ids") or []
            if operation_ids:
                operation_id = operation_ids[0]
        if not operation_id:
            raise HindsightError("retain was accepted without an operation_id")
        return str(operation_id)

    def operation(self, operation_id: str, *, bank: str | None = None) -> dict[str, Any]:
        try:
            response = self.request("GET", self._bank_url(f"/operations/{quote(operation_id, safe='')}", bank))
        except HindsightHTTPError as exc:
            if exc.status == 404:
                return {"operation_id": operation_id, "status": "not_found"}
            raise
        if not isinstance(response, dict):
            return {"operation_id": operation_id, "status": "unknown", "response": response}
        return response

    def retry_operation(self, operation_id: str, *, bank: str | None = None) -> str:
        response = self.request(
            "POST",
            self._bank_url(f"/operations/{quote(operation_id, safe='')}/retry", bank),
            {},
            timeout=max(self.timeout, 120),
        )
        if isinstance(response, dict):
            new_id = response.get("operation_id")
            if not new_id and response.get("operation_ids"):
                new_id = response["operation_ids"][0]
            if new_id:
                return str(new_id)
        # Some Hindsight versions retry in place. Keeping the old ID is safe
        # because status polling still follows the server's operation row.
        return operation_id

    def recall(self, query: str, *, bank: str | None = None, max_tokens: int = 512) -> dict[str, Any]:
        response = self.request(
            "POST",
            self._bank_url("/memories/recall", bank),
            {"query": query, "budget": "low", "max_tokens": max_tokens},
            timeout=max(self.timeout, 120),
        )
        return response if isinstance(response, dict) else {"results": response}

