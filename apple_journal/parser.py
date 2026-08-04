"""Parser for Apple Journal's HTML export format.

Apple's export is deliberately treated as a small, tolerant document format
rather than as XML.  The generated HTML contains a large inline stylesheet,
Apple's whitespace marker spans, malformed nesting in some entries, and media
controls mixed into the attachment grid.  This module keeps only the semantic
entry areas and reads JSON sidecars; binary resources are never opened.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import unquote, urlparse

from .models import JournalEntry, ResourceAttachment


APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{1,2}-\d{1,2})\b")
DISPLAY_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\b"
)
MONTHS = {
    name.lower(): number
    for number, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}
IGNORED_TAGS = frozenset({"style", "script", "head", "title", "meta", "link"})
MEDIA_CLASSES = frozenset(
    {
        "mediaPlayIcon",
        "mediaTypeIcon",
        "audioPlayButton",
        "audioQuoteBubble",
        "durationText",
        "audioDuration",
    }
)
BLOCK_TAGS = frozenset({"p", "div", "section", "article", "li", "blockquote"})
VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    parent: "_Node | None" = None
    children: list["_Node"] = field(default_factory=list)
    data: list[str] = field(default_factory=list)
    items: list[Any] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return {part for part in self.attrs.get("class", "").split() if part}


class _TreeParser(HTMLParser):
    """Build a forgiving tree while autoclosing common HTML block tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("__root__")
        self.stack: list[_Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"p", "li"}:
            for index in range(len(self.stack) - 1, 0, -1):
                if self.stack[index].tag == tag:
                    del self.stack[index:]
                    break
        node = _Node(tag, {key.lower(): value or "" for key, value in attrs})
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        self.stack[-1].items.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag.lower():
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].data.append(data)
        self.stack[-1].items.append(data)


def _walk(node: _Node) -> Iterator[_Node]:
    for child in node.children:
        yield child
        yield from _walk(child)


def _has_ancestor(node: _Node, tag: str) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.tag == tag:
            return True
        parent = parent.parent
    return False


def _find_class(root: _Node, class_name: str) -> list[_Node]:
    return [node for node in _walk(root) if class_name in node.classes]


def _text(node: _Node, *, include_blocks: bool = False) -> str:
    """Render visible text under a semantic node."""

    if node.tag in IGNORED_TAGS or node.classes & MEDIA_CLASSES:
        return ""
    if node.tag == "br":
        return "\n"
    chunks: list[str] = []
    items = node.items or [*node.data, *node.children]
    for item in items:
        if isinstance(item, str):
            chunks.append(item.replace("\xa0", " "))
        else:
            chunks.append(_text(item, include_blocks=include_blocks))
            if include_blocks and item.tag in BLOCK_TAGS:
                chunks.append("\n")
    return "".join(chunks)


def _normalise_inline(value: str) -> str:
    value = unescape(value).replace("\xa0", " ")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[ \t\f\v]+", " ", value).strip()


def _normalise_body(value: str) -> str:
    value = unescape(value).replace("\xa0", " ")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in value.split("\n")]
    # Keep intentional line breaks, but do not carry whitespace-only HTML
    # indentation or a long run of empty lines into the memory.
    value = "\n".join(lines).strip()
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value


def _body_text(root: _Node) -> str:
    containers = _find_class(root, "bodyText")
    if not containers:
        return ""
    container = containers[0]
    paragraphs = [
        node
        for node in _walk(container)
        if node.tag == "p" and not _has_ancestor(node, "p")
    ]
    if paragraphs:
        rendered = [_normalise_body(_text(node)) for node in paragraphs]
        # Empty paragraphs represent a line break in the export. Preserve a
        # single empty separator without allowing surrounding HTML whitespace
        # to dominate the result.
        return _normalise_body("\n\n".join(rendered))
    return _normalise_body(_text(container))


def _class_fragment_from_raw(raw: str, class_name: str) -> str:
    """Extract a semantic div's raw region from malformed Cocoa HTML."""

    opening = re.search(
        rf"<div\b[^>]*\bclass\s*=\s*(['\"][^'\"]*\b{re.escape(class_name)}\b[^'\"]*['\"])\s*[^>]*>",
        raw,
        re.IGNORECASE,
    )
    if not opening:
        return ""
    start = opening.end()
    depth = 1
    tag_re = re.compile(r"</?div\b[^>]*>", re.IGNORECASE)
    for match in tag_re.finditer(raw, start):
        if match.group(0).lstrip().startswith("</"):
            depth -= 1
        else:
            depth += 1
        if depth == 0:
            return raw[start : match.start()]
    return raw[start:]


def _body_fragment_from_raw(raw: str) -> str:
    """Extract the bodyText div's raw region from malformed Cocoa HTML."""

    return _class_fragment_from_raw(raw, "bodyText")


def _body_text_from_raw(raw: str) -> str:
    fragment = _body_fragment_from_raw(raw)
    if not fragment:
        return ""
    parser = _TreeParser()
    parser.feed(f"<div class='bodyText'>{fragment}</div>")
    parser.close()
    return _body_text(parser.root)


def _title_text_from_raw(raw: str) -> str:
    fragment = _class_fragment_from_raw(raw, "title")
    if not fragment:
        return ""
    parser = _TreeParser()
    parser.feed(f"<div class='title'>{fragment}</div>")
    parser.close()
    nodes = _find_class(parser.root, "title")
    return _normalise_inline(_text(nodes[0])) if nodes else ""


def _date_from_text(value: str) -> str:
    value = _normalise_inline(value)
    match = ISO_DATE_RE.search(value)
    if match:
        try:
            return date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            pass
    match = DISPLAY_DATE_RE.search(value)
    if match:
        month = MONTHS.get(match.group(2).lower())
        if month:
            try:
                return date(int(match.group(3)), month, int(match.group(1))).isoformat()
            except ValueError:
                pass
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _date_from_filename(path: Path) -> str:
    match = re.match(r"(\d{4}-\d{2}-\d{2})", path.name)
    if not match:
        return ""
    try:
        return date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return ""


def parse_apple_timestamp(value: Any) -> str:
    """Decode a numeric Apple reference-epoch timestamp to UTC ISO-8601."""

    if value in (None, ""):
        return ""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        parsed = _date_from_text(text)
        if parsed and len(text) <= 32:
            # A date-only value is useful as-is; an ISO timestamp is normalized
            # below only when it parses as one.
            if "T" not in text and " " not in text:
                return parsed
        try:
            value = float(text)
        except ValueError:
            try:
                parsed_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return text
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
            return parsed_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    try:
        timestamp = APPLE_EPOCH + timedelta(seconds=float(value))
    except (OverflowError, ValueError):
        return ""
    return timestamp.isoformat().replace("+00:00", "Z")


def _time_key(key: str) -> bool:
    key = key.lower()
    return any(token in key for token in ("date", "time", "timestamp"))


def _decode_times(value: Any, parent_key: str = "") -> Any:
    if isinstance(value, dict):
        return {key: _decode_times(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_times(item, parent_key) for item in value]
    if _time_key(parent_key) and isinstance(value, (int, float, str)):
        decoded = parse_apple_timestamp(value)
        return decoded or value
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _resource_kind(classes: set[str]) -> str:
    for token, kind in (
        ("assetType_livePhoto", "live_photo"),
        ("assetType_photo", "photo"),
        ("assetType_video", "video"),
        ("assetType_audio", "audio"),
        ("assetType_music", "music"),
        ("assetType_multiPinMap", "location"),
        ("assetType_workoutIcon", "workout"),
        ("assetType_stateOfMind", "state_of_mind"),
        ("assetType_contact", "contact"),
    ):
        if token in classes:
            return kind
    return "attachment"


def _sidecar_type(obj: dict[str, Any]) -> str:
    keys = set(obj)
    if "transcript" in keys or "recordingDate" in keys:
        return "transcript"
    if {"song", "artistName"} & keys:
        return "music"
    if "visits" in keys or "placeName" in keys:
        return "location"
    if "labels" in keys or "associations" in keys:
        return "labels"
    if "caloriesBurnt" in keys or "groupWorkoutStartDates" in keys or "numberOfWorkouts" in keys:
        return "workout"
    if "name" in keys:
        return "contact"
    return "metadata"


def _read_sidecars(resources_dir: Path) -> dict[str, list[tuple[Path, dict[str, Any]]]]:
    by_stem: dict[str, list[tuple[Path, dict[str, Any]]]] = defaultdict(list)
    if not resources_dir.is_dir():
        return by_stem
    for path in sorted(resources_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        by_stem[path.stem.lower()].append((path, _json_safe(_decode_times(raw))))
    return by_stem


def _resource_nodes(root: _Node) -> list[_Node]:
    result: list[_Node] = []
    for node in _walk(root):
        if "gridItem" not in node.classes:
            continue
        resource_id = node.attrs.get("id", "")
        if resource_id:
            result.append(node)
    return result


def _resource_files(node: _Node) -> list[str]:
    files: list[str] = []
    for child in _walk(node):
        src = child.attrs.get("src", "")
        if not src:
            continue
        parsed = urlparse(src)
        name = Path(unquote(parsed.path)).name
        if name and name not in files and name.lower() not in {"mediaplayicon.heic", "musicicon.heic"}:
            files.append(name)
    return files


def _sidecar_time(obj: dict[str, Any]) -> str:
    for key in ("date", "recordingDate", "startTime", "endTime"):
        if key in obj:
            value = obj[key]
            if isinstance(value, list):
                value = value[0] if value else ""
            if isinstance(value, dict):
                continue
            return parse_apple_timestamp(value)
    return ""


def _append_sidecar_fields(
    resource_id: str,
    obj: dict[str, Any],
    locations: list[dict[str, Any]],
    music: list[dict[str, Any]],
    workouts: list[dict[str, Any]],
    labels: list[Any],
    associations: list[Any],
    transcripts: list[str],
) -> None:
    if "placeName" in obj or "visits" in obj:
        locations.append({"resource_id": resource_id, **obj})
    if "visits" in obj and isinstance(obj["visits"], list):
        for visit in obj["visits"]:
            if isinstance(visit, dict):
                locations.append({"resource_id": resource_id, **visit})
    if {"song", "artistName", "albumName"} & set(obj):
        music.append({"resource_id": resource_id, **obj})
    if "caloriesBurnt" in obj or "groupWorkoutStartDates" in obj or "numberOfWorkouts" in obj:
        workouts.append({"resource_id": resource_id, **obj})
    if "labels" in obj:
        labels.extend(obj["labels"] if isinstance(obj["labels"], list) else [obj["labels"]])
    if "associations" in obj:
        associations.extend(
            obj["associations"] if isinstance(obj["associations"], list) else [obj["associations"]]
        )
    transcript = obj.get("transcript")
    if isinstance(transcript, str) and transcript.strip():
        transcripts.append(transcript.strip())


def parse_entry(path: Path, export_root: Path, sidecars: dict[str, list[tuple[Path, dict[str, Any]]]] | None = None) -> JournalEntry:
    """Parse one entry HTML file without opening any binary attachment."""

    raw = path.read_text(encoding="utf-8", errors="replace")
    tree = _TreeParser()
    tree.feed(raw)
    tree.close()
    root = tree.root

    header_nodes = _find_class(root, "pageHeader")
    displayed_date = _date_from_text(_text(header_nodes[0]) if header_nodes else "")
    if not displayed_date:
        displayed_date = _date_from_filename(path)
    title_nodes = _find_class(root, "title")
    title = _normalise_inline(_text(title_nodes[0])) if title_nodes else ""
    title = title or _title_text_from_raw(raw)
    body = _body_text(root) or _body_text_from_raw(raw)

    sidecars = sidecars if sidecars is not None else _read_sidecars(export_root / "Resources")
    locations: list[dict[str, Any]] = []
    music: list[dict[str, Any]] = []
    workouts: list[dict[str, Any]] = []
    labels: list[Any] = []
    associations: list[Any] = []
    transcripts: list[str] = []
    resources: list[ResourceAttachment] = []
    matched_sidecar_stems: set[str] = set()

    for node in _resource_nodes(root):
        resource_id = node.attrs.get("id", "")
        filenames = _resource_files(node)
        filename = next((name for name in filenames if not name.lower().startswith(("mediaplayicon", "musicicon", "audioplaybutton", "quotebubble", "audiowave", "photos_icon"))), "")
        resource_sidecars = sidecars.get(resource_id.lower(), [])
        metadata: dict[str, Any] = {}
        sidecar_type = ""
        timestamp = ""
        for sidecar_path, obj in resource_sidecars:
            matched_sidecar_stems.add(sidecar_path.stem.lower())
            metadata.update(obj)
            sidecar_type = sidecar_type or _sidecar_type(obj)
            timestamp = timestamp or _sidecar_time(obj)
            _append_sidecar_fields(
                resource_id,
                obj,
                locations,
                music,
                workouts,
                labels,
                associations,
                transcripts,
            )
        resources.append(
            ResourceAttachment(
                resource_id=resource_id,
                kind=_resource_kind(node.classes),
                filename=filename,
                metadata=metadata,
                sidecar_type=sidecar_type,
                timestamp=timestamp,
            )
        )

    # A few Apple exports separate a media reference from its metadata sidecar
    # (notably recordings and location visits). When there is exactly one
    # unmatched sidecar of a semantic type on the displayed journal date, use
    # it; never guess among multiple same-day candidates.
    entry_day = displayed_date
    unmatched: list[tuple[Path, dict[str, Any]]] = []
    for values in sidecars.values():
        for sidecar_path, obj in values:
            if sidecar_path.stem.lower() not in matched_sidecar_stems:
                stamp = _sidecar_time(obj)
                if stamp.startswith(entry_day):
                    unmatched.append((sidecar_path, obj))
    by_type: dict[str, list[tuple[Path, dict[str, Any]]]] = defaultdict(list)
    for item in unmatched:
        by_type[_sidecar_type(item[1])].append(item)
    for kind, values in by_type.items():
        if len(values) != 1:
            continue
        sidecar_path, obj = values[0]
        synthetic_id = sidecar_path.stem
        matched_sidecar_stems.add(synthetic_id.lower())
        _append_sidecar_fields(
            synthetic_id,
            obj,
            locations,
            music,
            workouts,
            labels,
            associations,
            transcripts,
        )
        resources.append(
            ResourceAttachment(
                resource_id=synthetic_id,
                kind=kind,
                filename=sidecar_path.name,
                metadata=obj,
                sidecar_type=kind,
                timestamp=_sidecar_time(obj),
            )
        )

    attachment_payload = {
        "resources": [
            {
                "id": item.resource_id,
                "kind": item.kind,
                "filename": item.filename,
                "metadata": item.metadata,
                "sidecar_type": item.sidecar_type,
                "timestamp": item.timestamp,
            }
            for item in resources
        ],
        "locations": locations,
        "music": music,
        "workouts": workouts,
        "labels": labels,
        "associations": associations,
        "transcript": transcripts,
    }
    body_fingerprint = _fingerprint(body)
    attachment_fingerprint = _fingerprint(attachment_payload)
    source_fingerprint = _fingerprint(
        {
            "source_path": path.relative_to(export_root).as_posix(),
            "entry_date": displayed_date,
            "title": title,
            "body": body,
            "attachments": attachment_payload,
        }
    )
    return JournalEntry(
        source_path=path.relative_to(export_root).as_posix(),
        entry_date=displayed_date,
        title=title,
        body=body,
        resources=resources,
        locations=locations,
        music=music,
        workouts=workouts,
        labels=labels,
        associations=associations,
        transcript="\n\n".join(dict.fromkeys(transcripts)),
        source_fingerprint=source_fingerprint,
        body_fingerprint=body_fingerprint,
        attachment_fingerprint=attachment_fingerprint,
    )


class _IndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(unquote(href))


def index_entry_paths(export_root: Path) -> list[Path]:
    """Return exactly the entries linked by index.html, in index order."""

    index = export_root / "index.html"
    if not index.is_file():
        raise FileNotFoundError(f"Apple Journal index not found: {index}")
    parser = _IndexParser()
    parser.feed(index.read_text(encoding="utf-8", errors="replace"))
    result: list[Path] = []
    seen: set[Path] = set()
    entries_root = (export_root / "Entries").resolve()
    for href in parser.links:
        parsed = urlparse(href)
        if parsed.scheme or parsed.netloc:
            continue
        relative = Path(unquote(parsed.path))
        if not relative.parts or relative.parts[0] != "Entries":
            continue
        candidate = (export_root / relative).resolve()
        try:
            candidate.relative_to(entries_root)
        except ValueError:
            continue
        if candidate.suffix.lower() != ".html" or not candidate.is_file() or candidate in seen:
            continue
        seen.add(candidate)
        result.append(candidate)
    return result


def parse_export(export_root: str | Path) -> list[JournalEntry]:
    """Parse all index-linked entries from an Apple Journal export."""

    root = Path(export_root).expanduser().resolve()
    sidecars = _read_sidecars(root / "Resources")
    return [parse_entry(path, root, sidecars) for path in index_entry_paths(root)]


def export_manifest(entries: Iterable[JournalEntry]) -> str:
    """Hash the content-free identity/fingerprint set for stale-plan checks."""

    payload = [
        {
            "source_path": entry.source_path,
            "entry_date": entry.entry_date,
            "source_fingerprint": entry.source_fingerprint,
            "body_fingerprint": entry.body_fingerprint,
            "attachment_fingerprint": entry.attachment_fingerprint,
        }
        for entry in sorted(entries, key=lambda item: item.source_path)
    ]
    return _fingerprint(payload)
