#!/usr/bin/env python3
"""CLI for importing Apple Journal HTML exports into Hindsight."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from apple_journal import config
from apple_journal.hindsight import HindsightClient, HindsightError
from apple_journal.ledger import FAILED, PENDING, STORED, Ledger
from apple_journal.parser import parse_export
from apple_journal.sync import (
    SyncError,
    apply_plan,
    build_plan,
    load_plan,
    refresh_status,
    retry_failed,
    save_plan,
)


REPO_ROOT = Path(__file__).resolve().parent


def _path(value: str | None, default: Path) -> Path:
    if not value:
        return default
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else REPO_ROOT / candidate


def _endpoint(value: str | None) -> str:
    endpoint = value or config.hindsight_mcp_url(REPO_ROOT / ".env")
    if not endpoint:
        raise SyncError(
            "HINDSIGHT_MCP_URL is required for this live operation; set it in .env or the environment"
        )
    return endpoint


def _client(url: str, bank: str) -> HindsightClient:
    client = HindsightClient(url, bank=bank)
    client.verify_bank(bank)
    return client


def _counts(plan) -> Counter:
    return Counter(entry.action for entry in plan.entries)


def _print_plan_summary(plan, output: Path) -> None:
    counts = _counts(plan)
    print(
        "[plan] "
        + " ".join(
            f"{key}={counts.get(key, 0)}"
            for key in ("new", "changed", "skip", "pending", "failed", "identity_conflict")
        )
        + f" entries={len(plan.entries)}"
    )
    semantic_count = sum(bool(entry.semantic_matches) for entry in plan.entries)
    print(f"[plan] semantic_review_flags={semantic_count} missing_source_documents={len(plan.missing_source_documents)}")
    print(f"[plan] wrote content-free plan {output}")


def cmd_plan(args: argparse.Namespace) -> int:
    export_root = _path(args.export, Path("."))
    output = _path(args.output, REPO_ROOT / config.DEFAULT_OUTPUT_DIR / "sync-plan.json")
    ledger_path = _path(args.ledger, REPO_ROOT / config.DEFAULT_OUTPUT_DIR / config.DEFAULT_LEDGER_NAME)
    url = _endpoint(args.hindsight_url)
    client = _client(url, args.bank)
    entries = parse_export(export_root)
    plan = build_plan(
        entries,
        export_root=export_root,
        bank=args.bank,
        endpoint=client.endpoint,
        client=client,
        ledger=Ledger(ledger_path),
        semantic=not args.no_semantic,
    )
    save_plan(plan, output)
    _print_plan_summary(plan, output)
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    plan = load_plan(_path(args.plan, Path(".")))
    counts = _counts(plan)
    print(
        "[review] "
        + " ".join(f"{key}={counts.get(key, 0)}" for key in ("new", "changed", "skip", "pending", "failed", "identity_conflict"))
    )
    for entry in plan.entries:
        scores = ",".join(f"{float(match['score']):.3f}" for match in entry.semantic_matches)
        suffix = f" semantic_scores={scores}" if scores else ""
        print(
            f"[review] {entry.source_path} action={entry.action} document_id={entry.document_id} "
            f"source_fp={entry.source_fingerprint} body_fp={entry.body_fingerprint} "
            f"attachment_fp={entry.attachment_fingerprint}{suffix}"
        )
        if entry.reason:
            print(f"[review]   reason={entry.reason}")
    if plan.missing_source_documents:
        print(f"[review] preserved_missing_source_documents={len(plan.missing_source_documents)}")

    if args.show_content:
        export_root = _path(args.export, Path(plan.export_root))
        entries = parse_export(export_root)
        only = set(args.entry or [])
        for entry in entries:
            if only and entry.source_path not in only:
                continue
            print(f"\n===== {entry.source_path} =====")
            print(f"date={entry.entry_date} title={entry.title or '(untitled)'}")
            print(entry.body)
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    plan = load_plan(_path(args.plan, Path(".")))
    bank = args.bank or plan.bank
    if bank != plan.bank:
        raise SyncError(f"plan bank is {plan.bank!r}; refusing --bank {bank!r}")
    url = _endpoint(args.hindsight_url)
    client = _client(url, bank)
    export_root = Path(plan.export_root)
    entries = parse_export(export_root)
    ledger_path = _path(args.ledger, REPO_ROOT / config.DEFAULT_OUTPUT_DIR / config.DEFAULT_LEDGER_NAME)
    counts = apply_plan(
        plan,
        entries,
        export_root=export_root,
        bank=bank,
        endpoint=client.endpoint,
        client=client,
        ledger=Ledger(ledger_path),
    )
    print(
        "[apply] "
        + " ".join(f"{key}={counts[key]}" for key in ("queued", "skipped", "pending", "failed", "conflicts"))
    )
    print("[apply] queued means acknowledged by Hindsight; run status before calling entries stored")
    return 1 if counts["failed"] else 0


def cmd_status(args: argparse.Namespace) -> int:
    url = _endpoint(args.hindsight_url)
    client = _client(url, args.bank)
    ledger_path = _path(args.ledger, REPO_ROOT / config.DEFAULT_OUTPUT_DIR / config.DEFAULT_LEDGER_NAME)
    ledger = Ledger(ledger_path)
    counts = refresh_status(client, ledger, bank=args.bank)
    print(
        f"[status] bank={args.bank} pending={counts.get(PENDING, 0)} "
        f"stored={counts.get(STORED, 0)} failed={counts.get(FAILED, 0)} ledger={ledger_path}"
    )
    for entry in sorted(ledger.entries.values(), key=lambda item: item.document_id):
        if entry.status != STORED:
            print(
                f"[status] document_id={entry.document_id} operation_id={entry.operation_id} "
                f"state={entry.status}"
            )
    return 1 if counts.get(FAILED, 0) else 0


def cmd_retry_failed(args: argparse.Namespace) -> int:
    ledger_path = _path(args.ledger, REPO_ROOT / config.DEFAULT_OUTPUT_DIR / config.DEFAULT_LEDGER_NAME)
    ledger = Ledger(ledger_path)
    failed = [entry for entry in ledger.entries.values() if entry.status == FAILED]
    print(f"[retry-failed] candidates={len(failed)} apply={bool(args.apply)} bank={args.bank}")
    if not failed:
        return 0
    if not args.apply:
        for entry in failed:
            print(f"[retry-failed] document_id={entry.document_id} operation_id={entry.operation_id} status=failed")
        print("[retry-failed] dry-run only; pass --apply to call Hindsight retry")
        return 0
    client = _client(_endpoint(args.hindsight_url), args.bank)
    report = retry_failed(client, ledger, bank=args.bank, apply=True)
    for item in report:
        print(
            f"[retry-failed] document_id={item['document_id']} "
            f"operation_id={item.get('new_operation_id', item['operation_id'])} status={item['status']}"
        )
    return 1 if any("error" in item for item in report) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Incrementally import Apple Journal HTML exports into Hindsight")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="create a content-free live reconciliation plan")
    plan.add_argument("--export", required=True, help="Apple Journal export directory")
    plan.add_argument("--bank", required=True, help="explicit Hindsight bank id")
    plan.add_argument("--hindsight-url", default="", help="Hindsight MCP or REST URL")
    plan.add_argument("--output", default="", help="plan JSON path")
    plan.add_argument("--ledger", default="", help="hash-only ledger path")
    plan.add_argument("--no-semantic", action="store_true", help="skip advisory semantic recall checks")
    plan.set_defaults(func=cmd_plan)

    review = subparsers.add_parser("review", help="inspect plan decisions and advisory matches")
    review.add_argument("--plan", required=True, help="content-free plan JSON")
    review.add_argument("--export", default="", help="export path when --show-content is used")
    review.add_argument("--entry", action="append", help="source_path filter for --show-content")
    review.add_argument("--show-content", action="store_true", help="explicitly display journal text")
    review.set_defaults(func=cmd_review)

    apply_parser = subparsers.add_parser("apply", help="re-verify and queue new/changed entries")
    apply_parser.add_argument("--plan", required=True, help="unchanged content-free plan JSON")
    apply_parser.add_argument("--bank", default="", help="must match the plan when provided")
    apply_parser.add_argument("--hindsight-url", default="", help="Hindsight MCP or REST URL")
    apply_parser.add_argument("--ledger", default="", help="hash-only ledger path")
    apply_parser.set_defaults(func=cmd_apply)

    status = subparsers.add_parser("status", help="refresh queued operation states")
    status.add_argument("--bank", required=True, help="explicit Hindsight bank id")
    status.add_argument("--hindsight-url", default="", help="Hindsight MCP or REST URL")
    status.add_argument("--ledger", default="", help="hash-only ledger path")
    status.set_defaults(func=cmd_status)

    retry = subparsers.add_parser("retry-failed", help="list or explicitly retry failed operations")
    retry.add_argument("--bank", required=True, help="explicit Hindsight bank id")
    retry.add_argument("--hindsight-url", default="", help="Hindsight MCP or REST URL")
    retry.add_argument("--ledger", default="", help="hash-only ledger path")
    retry.add_argument("--apply", action="store_true", help="perform the remote retry")
    retry.set_defaults(func=cmd_retry_failed)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (SyncError, HindsightError, OSError, ValueError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
