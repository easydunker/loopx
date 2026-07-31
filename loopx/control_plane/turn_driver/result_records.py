"""Immutable result and reconciliation records for one governed LoopX Turn."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any

from .transaction import LOOPX_TURN_RESULT_SCHEMA_VERSION, LoopXTurnResultKind


TURN_RESULT_RECORD_SCHEMA_VERSION = "turn_result_record_v0"
TURN_RECONCILIATION_RECEIPT_SCHEMA_VERSION = "turn_reconciliation_receipt_v0"
TURN_RECONCILIATION_STATUSES = {
    "not_attempted",
    "not_required",
    "shadow_match",
    "shadow_conflict",
    "applied",
    "already_applied",
    "revision_conflict",
    "semantic_review_required",
    "rejected",
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _effect(
    *,
    turn_key: str,
    kind: str,
    target: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    effect = {
        "kind": kind,
        "target": target,
        "payload": dict(payload),
    }
    return {
        "effect_id": _canonical_hash({"turn_key": turn_key, **effect}),
        **effect,
    }


def _proposed_effects(
    *,
    turn_key: str,
    goal_id: str,
    agent_id: str,
    todo_id: str,
    result_kind: LoopXTurnResultKind,
    result: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if result_kind in {
        LoopXTurnResultKind.USER_ACTION_REQUIRED,
        LoopXTurnResultKind.WAIT,
    }:
        return []

    effects: list[dict[str, Any]] = []
    if result_kind in {
        LoopXTurnResultKind.REPAIR_REQUIRED,
        LoopXTurnResultKind.REPLAN_REQUIRED,
    }:
        effects.append(
            _effect(
                turn_key=turn_key,
                kind="todo_note_update",
                target=f"todo:{todo_id}",
                payload={
                    "agent_id": agent_id,
                    "result_kind": result_kind.value,
                    "summary": result.get("summary"),
                    "next_action": result.get("next_action"),
                },
            )
        )
    elif result_kind is LoopXTurnResultKind.VALIDATED_COMPLETION:
        effects.append(
            _effect(
                turn_key=turn_key,
                kind="todo_complete",
                target=f"todo:{todo_id}",
                payload={"agent_id": agent_id},
            )
        )

    effects.extend(
        [
            _effect(
                turn_key=turn_key,
                kind="goal_state_refresh",
                target=f"goal:{goal_id}",
                payload={
                    "agent_id": agent_id,
                    "classification": result.get("classification"),
                    "recommended_action": result.get("recommended_action"),
                    "next_action": result.get("next_action"),
                    "delivery_batch_scale": result.get("delivery_batch_scale"),
                    "delivery_outcome": result.get("delivery_outcome"),
                },
            ),
            _effect(
                turn_key=turn_key,
                kind="quota_spend",
                target=f"goal:{goal_id}",
                payload={"agent_id": agent_id, "slots": 1},
            ),
            _effect(
                turn_key=turn_key,
                kind="scheduler_reconcile",
                target=f"goal:{goal_id}",
                payload={"agent_id": agent_id},
            ),
        ]
    )
    return effects


def build_turn_result_record(
    plan: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a deterministic immutable record from one normalized host result."""

    transaction = _mapping(plan.get("transaction"))
    envelope = _mapping(plan.get("turn_envelope"))
    action = _mapping(envelope.get("action"))
    selected_todo = _mapping(action.get("selected_todo"))
    signature = _mapping(envelope.get("action_signature"))
    turn_key = str(transaction.get("turn_key") or "")
    goal_id = str(envelope.get("goal_id") or "")
    agent_id = str(envelope.get("agent_id") or "")
    todo_id = str(selected_todo.get("todo_id") or "")
    based_on_revision = str(
        signature.get("source_decision_hash") or signature.get("source_hash") or ""
    )
    source_hash = str(signature.get("source_hash") or "")
    candidate = dict(result)

    if candidate.get("schema_version") != LOOPX_TURN_RESULT_SCHEMA_VERSION:
        raise ValueError("Turn result record requires a normalized host result")
    if not turn_key or candidate.get("turn_key") != turn_key:
        raise ValueError("Turn result record requires matching turn lineage")
    if not all((goal_id, agent_id, todo_id, based_on_revision, source_hash)):
        raise ValueError(
            "Turn result record requires goal, agent, todo, and action revision lineage"
        )
    try:
        result_kind = LoopXTurnResultKind(str(candidate.get("result_kind") or ""))
    except ValueError as exc:
        raise ValueError("Turn result record has an unsupported result kind") from exc

    document = {
        "schema_version": TURN_RESULT_RECORD_SCHEMA_VERSION,
        "turn_key": turn_key,
        "lineage": {
            "goal_id": goal_id,
            "agent_id": agent_id,
            "todo_id": todo_id,
        },
        "based_on_revision": based_on_revision,
        "action_signature": {
            "coverage": signature.get("coverage"),
            "source_hash": source_hash,
        },
        "result_kind": result_kind.value,
        "candidate_result": candidate,
        "proposed_effects": _proposed_effects(
            turn_key=turn_key,
            goal_id=goal_id,
            agent_id=agent_id,
            todo_id=todo_id,
            result_kind=result_kind,
            result=candidate,
        ),
    }
    return {
        **document,
        "record_id": _canonical_hash(document),
    }


def validate_turn_result_record(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate immutable record identity without consulting mutable state."""

    record = dict(value)
    errors: list[str] = []
    if record.get("schema_version") != TURN_RESULT_RECORD_SCHEMA_VERSION:
        errors.append("unsupported Turn result record schema")
    record_id = str(record.pop("record_id", "") or "")
    if not record_id or record_id != _canonical_hash(record):
        errors.append("Turn result record_id does not match its canonical content")
    turn_key = str(value.get("turn_key") or "")
    lineage = _mapping(value.get("lineage"))
    signature = _mapping(value.get("action_signature"))
    candidate = _mapping(value.get("candidate_result"))
    result_kind_value = str(value.get("result_kind") or "")
    try:
        result_kind = LoopXTurnResultKind(result_kind_value)
    except ValueError:
        result_kind = None
        errors.append("Turn result record has an unsupported result kind")
    if (
        not turn_key
        or not all(
            str(lineage.get(key) or "") for key in ("goal_id", "agent_id", "todo_id")
        )
        or not str(value.get("based_on_revision") or "")
        or not str(signature.get("source_hash") or "")
    ):
        errors.append("Turn result record has incomplete lineage")
    if (
        candidate.get("schema_version") != LOOPX_TURN_RESULT_SCHEMA_VERSION
        or candidate.get("turn_key") != turn_key
        or candidate.get("result_kind") != result_kind_value
    ):
        errors.append("Turn result record candidate lineage is inconsistent")
    effects = value.get("proposed_effects")
    if not isinstance(effects, list):
        errors.append("Turn result proposed_effects must be a list")
    else:
        effect_ids = [str(_mapping(item).get("effect_id") or "") for item in effects]
        if not all(effect_ids) or len(set(effect_ids)) != len(effects):
            errors.append("Turn result proposed effect ids must be non-empty and unique")
        for item in effects:
            effect = _mapping(item)
            effect_id = str(effect.pop("effect_id", "") or "")
            if (
                not effect_id
                or not str(effect.get("kind") or "")
                or not str(effect.get("target") or "")
                or not isinstance(effect.get("payload"), Mapping)
                or effect_id != _canonical_hash({"turn_key": turn_key, **effect})
            ):
                errors.append("Turn result proposed effect identity is invalid")
                break
        if result_kind is not None and not errors:
            expected_effects = _proposed_effects(
                turn_key=turn_key,
                goal_id=str(lineage["goal_id"]),
                agent_id=str(lineage["agent_id"]),
                todo_id=str(lineage["todo_id"]),
                result_kind=result_kind,
                result=candidate,
            )
            if effects != expected_effects:
                errors.append("Turn result proposed effects do not match the candidate")
    return {
        "ok": not errors,
        "schema_version": TURN_RESULT_RECORD_SCHEMA_VERSION,
        "record_id": record_id or None,
        "errors": errors,
    }


def build_turn_reconciliation_receipt(
    result_record: Mapping[str, Any],
    *,
    status: str,
    observed_revision: str | None = None,
    applied_effect_ids: Sequence[str] = (),
    reason: str | None = None,
) -> dict[str, Any]:
    """Build one immutable reconciliation observation for a result record."""

    validation = validate_turn_result_record(result_record)
    if not validation["ok"]:
        raise ValueError("; ".join(validation["errors"]))
    if status not in TURN_RECONCILIATION_STATUSES:
        raise ValueError("unsupported Turn reconciliation status")

    proposed_effect_ids = [
        str(_mapping(item).get("effect_id") or "")
        for item in result_record.get("proposed_effects") or []
    ]
    applied = [str(item) for item in applied_effect_ids]
    if len(set(applied)) != len(applied):
        raise ValueError("reconciliation applied_effect_ids must be unique")
    if not set(applied).issubset(proposed_effect_ids):
        raise ValueError("reconciliation applied_effect_ids must reference proposed effects")
    if status == "not_attempted" and not proposed_effect_ids:
        raise ValueError("not_attempted reconciliation requires proposed effects")
    if status in {"applied", "already_applied"} and set(applied) != set(
        proposed_effect_ids
    ):
        raise ValueError("applied reconciliation must cover every proposed effect")
    if status == "not_required" and proposed_effect_ids:
        raise ValueError("not_required reconciliation cannot have proposed effects")

    document = {
        "schema_version": TURN_RECONCILIATION_RECEIPT_SCHEMA_VERSION,
        "result_record_id": result_record["record_id"],
        "turn_key": result_record["turn_key"],
        "expected_revision": result_record["based_on_revision"],
        "observed_revision": observed_revision,
        "status": status,
        "proposed_effect_ids": proposed_effect_ids,
        "applied_effect_ids": applied,
        "reason": reason,
    }
    return {
        **document,
        "receipt_id": _canonical_hash(document),
    }


def validate_turn_reconciliation_receipt(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate immutable receipt identity and its bounded status vocabulary."""

    receipt = dict(value)
    errors: list[str] = []
    if receipt.get("schema_version") != TURN_RECONCILIATION_RECEIPT_SCHEMA_VERSION:
        errors.append("unsupported Turn reconciliation receipt schema")
    if receipt.get("status") not in TURN_RECONCILIATION_STATUSES:
        errors.append("unsupported Turn reconciliation status")
    if not all(
        str(receipt.get(key) or "")
        for key in ("result_record_id", "turn_key", "expected_revision")
    ):
        errors.append("Turn reconciliation receipt has incomplete lineage")
    receipt_id = str(receipt.pop("receipt_id", "") or "")
    if not receipt_id or receipt_id != _canonical_hash(receipt):
        errors.append(
            "Turn reconciliation receipt_id does not match its canonical content"
        )
    proposed = receipt.get("proposed_effect_ids")
    applied = receipt.get("applied_effect_ids")
    if not isinstance(proposed, list) or not isinstance(applied, list):
        errors.append("Turn reconciliation effect ids must be lists")
    else:
        proposed_ids = [str(item) for item in proposed]
        applied_ids = [str(item) for item in applied]
        if (
            not all(proposed_ids)
            or len(set(proposed_ids)) != len(proposed_ids)
            or len(set(applied_ids)) != len(applied_ids)
        ):
            errors.append("Turn reconciliation effect ids must be non-empty and unique")
        if not set(applied_ids).issubset(proposed_ids):
            errors.append("Turn reconciliation applied effects were not proposed")
        status = receipt.get("status")
        if status == "not_attempted" and not proposed_ids:
            errors.append("not_attempted reconciliation requires proposed effects")
        if status in {"applied", "already_applied"} and set(applied_ids) != set(
            proposed_ids
        ):
            errors.append("applied reconciliation must cover every proposed effect")
        if status == "not_required" and proposed_ids:
            errors.append("not_required reconciliation cannot have proposed effects")
    return {
        "ok": not errors,
        "schema_version": TURN_RECONCILIATION_RECEIPT_SCHEMA_VERSION,
        "receipt_id": receipt_id or None,
        "errors": errors,
    }
