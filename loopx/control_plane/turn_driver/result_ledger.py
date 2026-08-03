"""Append-only storage for immutable LoopX Turn result records and receipts."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ...file_lock import exclusive_file_lock
from .result_records import (
    TURN_RECONCILIATION_RECEIPT_SCHEMA_VERSION,
    TURN_RESULT_RECORD_SCHEMA_VERSION,
    TURN_SEMANTIC_REVIEW_REQUEST_SCHEMA_VERSION,
    validate_turn_reconciliation_receipt,
    validate_turn_result_record,
    validate_turn_semantic_review_request,
)


TURN_RESULT_LEDGER_APPEND_SCHEMA_VERSION = "turn_result_ledger_append_v0"


def turn_result_ledger_path(runtime_root: Path, *, goal_id: str) -> Path:
    """Return the private runtime ledger shared by all Turns for one goal."""

    if not goal_id or "/" in goal_id or goal_id in {".", ".."}:
        raise ValueError("goal_id must be one bounded path segment")
    return runtime_root / "goals" / goal_id / "turn-result-ledger.jsonl"


def _record_identity(value: Mapping[str, Any]) -> tuple[str, str]:
    schema_version = str(value.get("schema_version") or "")
    if schema_version == TURN_RESULT_RECORD_SCHEMA_VERSION:
        validation = validate_turn_result_record(value)
        identity = str(value.get("record_id") or "")
    elif schema_version == TURN_RECONCILIATION_RECEIPT_SCHEMA_VERSION:
        validation = validate_turn_reconciliation_receipt(value)
        identity = str(value.get("receipt_id") or "")
    elif schema_version == TURN_SEMANTIC_REVIEW_REQUEST_SCHEMA_VERSION:
        validation = validate_turn_semantic_review_request(value)
        identity = str(value.get("request_id") or "")
    else:
        raise ValueError("Turn result ledger row has an unsupported schema")
    if not validation["ok"]:
        raise ValueError("; ".join(validation["errors"]))
    return schema_version, identity


def _complete_ledger_text(path: Path) -> tuple[str, int]:
    """Return newline-committed ledger text and its durable byte boundary."""

    payload = path.read_bytes()
    complete_size = len(payload)
    if payload and not payload.endswith(b"\n"):
        final_newline = payload.rfind(b"\n")
        complete_size = final_newline + 1
        payload = payload[:complete_size]
    try:
        return payload.decode("utf-8"), complete_size
    except UnicodeDecodeError as exc:
        raise ValueError("Turn result ledger contains invalid UTF-8") from exc


def read_turn_result_ledger(path: Path) -> list[dict[str, Any]]:
    """Read and validate every immutable row in one result ledger."""

    if not path.exists():
        return []
    text, _complete_size = _complete_ledger_text(path)
    rows: list[dict[str, Any]] = []
    identities: dict[tuple[str, str], dict[str, Any]] = {}
    result_record_ids: set[str] = set()
    receipts: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            raise ValueError(f"Turn result ledger row {line_number} is empty")
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Turn result ledger row {line_number} is not valid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise ValueError(f"Turn result ledger row {line_number} is not an object")
        row = dict(value)
        key = _record_identity(row)
        prior = identities.get(key)
        if prior is not None and prior != row:
            raise ValueError("Turn result ledger identity maps to conflicting content")
        if prior is not None:
            raise ValueError("Turn result ledger contains a duplicate immutable row")
        identities[key] = row
        if key[0] == TURN_RESULT_RECORD_SCHEMA_VERSION:
            result_record_ids.add(key[1])
        else:
            receipts.append(row)
        rows.append(row)
    for receipt in receipts:
        if str(receipt.get("result_record_id") or "") in result_record_ids:
            continue
        if (
            receipt.get("schema_version")
            == TURN_RECONCILIATION_RECEIPT_SCHEMA_VERSION
        ):
            raise ValueError("Turn result ledger receipt has no result record")
        raise ValueError("Turn semantic review request has no result record")
    return rows


def append_turn_result_ledger_records(
    path: Path,
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Append new immutable rows once and reuse byte-equivalent identities."""

    candidates = [dict(record) for record in records]
    if not candidates:
        raise ValueError("Turn result ledger append requires at least one record")
    candidate_keys: list[tuple[str, str]] = []
    candidate_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in candidates:
        key = _record_identity(candidate)
        prior = candidate_by_key.get(key)
        if prior is not None and prior != candidate:
            raise ValueError("Turn result ledger append has conflicting identities")
        if prior is not None:
            raise ValueError("Turn result ledger append repeats one immutable row")
        candidate_by_key[key] = candidate
        candidate_keys.append(key)

    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(path):
        if path.exists():
            _text, complete_size = _complete_ledger_text(path)
            if complete_size < path.stat().st_size:
                descriptor = os.open(path, os.O_WRONLY)
                try:
                    os.ftruncate(descriptor, complete_size)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        existing = read_turn_result_ledger(path)
        existing_by_key = {_record_identity(row): row for row in existing}
        known_result_ids = {
            identity
            for (schema_version, identity) in (
                set(existing_by_key) | set(candidate_by_key)
            )
            if schema_version == TURN_RESULT_RECORD_SCHEMA_VERSION
        }
        if any(
            str(candidate.get("result_record_id") or "") not in known_result_ids
            for candidate in candidates
            if candidate.get("schema_version")
            == TURN_RECONCILIATION_RECEIPT_SCHEMA_VERSION
        ):
            raise ValueError("Turn result ledger receipt has no result record")
        if any(
            str(candidate.get("result_record_id") or "") not in known_result_ids
            for candidate in candidates
            if candidate.get("schema_version")
            == TURN_SEMANTIC_REVIEW_REQUEST_SCHEMA_VERSION
        ):
            raise ValueError("Turn semantic review request has no result record")

        appended: list[tuple[str, str]] = []
        reused: list[tuple[str, str]] = []
        for key in candidate_keys:
            prior = existing_by_key.get(key)
            candidate = candidate_by_key[key]
            if prior is None:
                appended.append(key)
            elif prior == candidate:
                reused.append(key)
            else:
                raise ValueError(
                    "Turn result ledger identity maps to conflicting content"
                )

        if appended:
            payload = "".join(
                json.dumps(
                    candidate_by_key[key],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
                for key in appended
            ).encode("utf-8")
            descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("Turn result ledger append made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    return {
        "schema_version": TURN_RESULT_LEDGER_APPEND_SCHEMA_VERSION,
        "status": "appended" if appended else "reused",
        "appended_count": len(appended),
        "reused_count": len(reused),
        "row_count": len(existing) + len(appended),
        "record_ids": [identity for _schema, identity in candidate_keys],
        "path_recorded": False,
    }
