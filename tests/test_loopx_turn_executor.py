from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Event, Lock

import pytest

from loopx.control_plane.turn_driver import (
    LOOPX_TURN_RESULT_SCHEMA_VERSION,
    TURN_RECONCILIATION_RECEIPT_SCHEMA_VERSION,
    TURN_RESULT_LEDGER_APPEND_SCHEMA_VERSION,
    TURN_RESULT_RECORD_SCHEMA_VERSION,
    TURN_SEMANTIC_REVIEW_REQUEST_SCHEMA_VERSION,
    append_turn_result_ledger_records,
    build_loopx_turn_plan,
    build_turn_reconciliation_receipt,
    build_turn_result_record,
    build_turn_semantic_review_request,
    build_turn_shadow_reconciliation_receipt,
    load_loopx_turn_plan_from_journal,
    read_turn_result_ledger,
    run_loopx_turn_once,
    validate_loopx_turn_host_result,
    validate_turn_reconciliation_receipt,
    validate_turn_result_record,
    validate_turn_semantic_review_request,
)
from loopx.control_plane.turn_driver.executor import BuiltInHostError, _run_host_runner


def _content_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _plan(*, turn_instance_id: str | None = None) -> dict[str, object]:
    return build_loopx_turn_plan(
        {
            "ok": True,
            "schema_version": "loopx_turn_envelope_v0",
            "goal_id": "fixture-goal",
            "agent_id": "codex-fixture",
            "should_run": True,
            "effective_action": "normal_run",
            "action": {
                "must_attempt": True,
                "delivery_allowed": True,
                "quiet_noop_allowed": False,
                "selected_todo": {
                    "todo_id": "todo_fixture0001",
                    "text": "Advance one public fixture",
                },
            },
            "user": {
                "action_required": False,
                "open_count": 0,
                "notify": "DONT_NOTIFY",
            },
            "writeback": {"spend_after_validation": True},
            "scheduler": {"action": "run_now"},
            "action_signature": {
                "matches": True,
                "source_hash": "sha256:fixture",
                "envelope_hash": "sha256:fixture",
            },
            "compaction": {"within_budget": True},
        },
        host="generic-cli",
        execution_mode="isolated-headless",
        turn_instance_id=turn_instance_id,
    )


def _host_result(
    plan: dict[str, object], *, kind: str = "validated_progress"
) -> dict[str, object]:
    transaction = plan["transaction"]
    assert isinstance(transaction, dict)
    result: dict[str, object] = {
        "schema_version": LOOPX_TURN_RESULT_SCHEMA_VERSION,
        "turn_key": transaction["turn_key"],
        "result_kind": kind,
        "completed_phases": ["host_execute", "typed_result"],
    }
    if kind == "validated_progress":
        result.update(
            classification="fixture_progress",
            recommended_action="Continue the public fixture",
            next_action="Run the next public fixture check",
            delivery_batch_scale="implementation",
            delivery_outcome="outcome_progress",
            vision_unchanged_reason="The fixture objective is unchanged after validated progress.",
            summary="One public fixture advanced.",
        )
    return result


def _host_argv(result_path: Path, count_path: Path) -> list[str]:
    script = """
import json
import pathlib
import sys
request = json.load(sys.stdin)
result = json.loads(pathlib.Path(sys.argv[1]).read_text())
result["turn_key"] = request["turn_key"]
count = pathlib.Path(sys.argv[2])
count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))
json.dump(result, sys.stdout)
"""
    return [sys.executable, "-c", script, str(result_path), str(count_path)]


def _callbacks(calls: dict[str, int]):
    def writeback(_result: dict[str, object]) -> dict[str, object]:
        calls["writeback"] += 1
        return {"ok": True, "appended": True, "classification": "fixture_progress"}

    def spend() -> dict[str, object]:
        calls["spend"] += 1
        return {"ok": True, "appended": True, "slots": 1}

    def scheduler(_spend: dict[str, object]) -> dict[str, object]:
        calls["scheduler"] += 1
        return {"completed": True, "acknowledged": False, "disposition": "not_required"}

    return writeback, spend, scheduler


def _journal(runtime_root: Path) -> dict[str, object]:
    journal_paths = list(
        (runtime_root / "goals" / "fixture-goal" / "turns").glob("*.json")
    )
    assert len(journal_paths) == 1
    return json.loads(journal_paths[0].read_text(encoding="utf-8"))


def _passing_validator(
    _plan: dict[str, object],
    _result: dict[str, object],
) -> dict[str, object]:
    return {
        "status": "passed",
        "validator_kind": "fixture",
        "summary": "independent fixture postconditions passed",
    }


def test_host_result_requires_bounded_public_material_fields() -> None:
    plan = _plan()
    result = _host_result(plan)
    result["raw_trajectory"] = "not allowed"

    validation = validate_loopx_turn_host_result(plan, result)

    assert validation["ok"] is False
    assert "unsupported host result fields" in " ".join(validation["errors"])


def test_result_and_reconciliation_records_have_stable_content_identity() -> None:
    plan = _plan()
    result = _host_result(plan)

    record = build_turn_result_record(plan, result)
    repeated = build_turn_result_record(plan, result)
    receipt = build_turn_reconciliation_receipt(
        record,
        status="not_attempted",
        reason="legacy_direct_writeback_not_reconciled",
    )

    assert record == repeated
    assert record["schema_version"] == TURN_RESULT_RECORD_SCHEMA_VERSION
    assert record["lineage"] == {
        "goal_id": "fixture-goal",
        "agent_id": "codex-fixture",
        "todo_id": "todo_fixture0001",
    }
    assert record["based_on_revision"] == "sha256:fixture"
    assert [effect["kind"] for effect in record["proposed_effects"]] == [
        "goal_state_refresh",
        "quota_spend",
        "scheduler_reconcile",
    ]
    assert validate_turn_result_record(record)["ok"] is True
    assert receipt["schema_version"] == TURN_RECONCILIATION_RECEIPT_SCHEMA_VERSION
    assert receipt["status"] == "not_attempted"
    assert receipt["applied_effect_ids"] == []
    assert validate_turn_reconciliation_receipt(receipt)["ok"] is True
    tampered = dict(record)
    tampered["result_kind"] = "wait"
    assert validate_turn_result_record(tampered)["ok"] is False

    invalid_receipt = dict(receipt)
    invalid_receipt["status"] = "applied"
    invalid_receipt["receipt_id"] = _content_hash(
        {key: value for key, value in invalid_receipt.items() if key != "receipt_id"}
    )
    assert validate_turn_reconciliation_receipt(invalid_receipt)["ok"] is False

    extended_record = dict(record)
    extended_record["future_field"] = "not in v0"
    extended_record["record_id"] = _content_hash(
        {key: value for key, value in extended_record.items() if key != "record_id"}
    )
    assert validate_turn_result_record(extended_record)["ok"] is False

    extended_receipt = dict(receipt)
    extended_receipt["future_field"] = "not in v0"
    extended_receipt["receipt_id"] = _content_hash(
        {key: value for key, value in extended_receipt.items() if key != "receipt_id"}
    )
    assert validate_turn_reconciliation_receipt(extended_receipt)["ok"] is False

    effect_id = record["proposed_effects"][0]["effect_id"]
    with pytest.raises(ValueError, match="applied_effect_ids must be unique"):
        build_turn_reconciliation_receipt(
            record,
            status="applied",
            applied_effect_ids=[effect_id, effect_id],
        )


def test_semantic_review_request_is_bounded_to_changed_pre_effect_revision() -> None:
    plan = _plan()
    record = build_turn_result_record(plan, _host_result(plan))

    request = build_turn_semantic_review_request(
        record,
        ambiguity_kind="revision_changed_before_effects",
        observed_revision="sha256:advanced",
    )
    replay = build_turn_semantic_review_request(
        record,
        ambiguity_kind="revision_changed_before_effects",
        observed_revision="sha256:advanced",
    )

    assert request == replay
    assert request["schema_version"] == TURN_SEMANTIC_REVIEW_REQUEST_SCHEMA_VERSION
    assert request["result_record_id"] == record["record_id"]
    assert request["proposed_effect_ids"] == [
        effect["effect_id"] for effect in record["proposed_effects"]
    ]
    assert validate_turn_semantic_review_request(request)["ok"] is True
    with pytest.raises(ValueError, match="changed observed revision"):
        build_turn_semantic_review_request(
            record,
            ambiguity_kind="revision_changed_before_effects",
            observed_revision=record["based_on_revision"],
        )


def test_result_ledger_appends_immutable_rows_once(tmp_path: Path) -> None:
    plan = _plan()
    record = build_turn_result_record(plan, _host_result(plan))
    receipt = build_turn_reconciliation_receipt(
        record,
        status="not_attempted",
        reason="legacy_direct_writeback_not_reconciled",
    )
    path = tmp_path / "turn-result-ledger.jsonl"

    first = append_turn_result_ledger_records(path, [record, receipt])
    replay = append_turn_result_ledger_records(path, [record, receipt])

    assert first == {
        "schema_version": TURN_RESULT_LEDGER_APPEND_SCHEMA_VERSION,
        "status": "appended",
        "appended_count": 2,
        "reused_count": 0,
        "row_count": 2,
        "record_ids": [record["record_id"], receipt["receipt_id"]],
        "path_recorded": False,
    }
    assert replay["status"] == "reused"
    assert replay["appended_count"] == 0
    assert replay["reused_count"] == 2
    assert read_turn_result_ledger(path) == [record, receipt]


def test_result_ledger_rejects_dangling_or_corrupt_rows(tmp_path: Path) -> None:
    plan = _plan()
    record = build_turn_result_record(plan, _host_result(plan))
    receipt = build_turn_reconciliation_receipt(
        record,
        status="not_attempted",
        reason="legacy_direct_writeback_not_reconciled",
    )
    path = tmp_path / "turn-result-ledger.jsonl"

    with pytest.raises(ValueError, match="receipt has no result record"):
        append_turn_result_ledger_records(path, [receipt])
    assert not path.exists()

    path.write_text("{not-json}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="row 1 is not valid JSON"):
        append_turn_result_ledger_records(path, [record, receipt])
    assert path.read_text(encoding="utf-8") == "{not-json}\n"


def test_result_ledger_rejects_semantic_request_with_mismatched_lineage(
    tmp_path: Path,
) -> None:
    plan = _plan()
    record = build_turn_result_record(plan, _host_result(plan))
    request = build_turn_semantic_review_request(
        record,
        ambiguity_kind="revision_changed_before_effects",
        observed_revision="sha256:advanced",
    )
    request["turn_key"] = "sha256:other-turn"
    request["request_id"] = _content_hash(
        {key: value for key, value in request.items() if key != "request_id"}
    )
    path = tmp_path / "turn-result-ledger.jsonl"

    with pytest.raises(ValueError, match="does not match its result record"):
        append_turn_result_ledger_records(path, [record, request])

    assert not path.exists()


def test_result_ledger_recovers_one_torn_trailing_row(tmp_path: Path) -> None:
    plan = _plan()
    record = build_turn_result_record(plan, _host_result(plan))
    receipt = build_turn_reconciliation_receipt(
        record,
        status="not_attempted",
        reason="legacy_direct_writeback_not_reconciled",
    )
    path = tmp_path / "turn-result-ledger.jsonl"
    append_turn_result_ledger_records(path, [record])
    with path.open("ab") as handle:
        handle.write(b'{"schema_version":"turn_result_record_v0"')

    assert read_turn_result_ledger(path) == [record]

    recovered = append_turn_result_ledger_records(path, [receipt])

    assert recovered["status"] == "appended"
    assert read_turn_result_ledger(path) == [record, receipt]
    assert path.read_bytes().endswith(b"\n")


def test_shadow_reconciliation_compares_direct_effect_sequence() -> None:
    plan = _plan()
    record = build_turn_result_record(plan, _host_result(plan))
    proposed_kinds = [str(effect["kind"]) for effect in record["proposed_effects"]]

    matched = build_turn_shadow_reconciliation_receipt(
        record,
        observed_effect_kinds=proposed_kinds,
    )
    partial = build_turn_shadow_reconciliation_receipt(
        record,
        observed_effect_kinds=proposed_kinds[:1],
    )

    assert matched["status"] == "shadow_match"
    assert matched["applied_effect_ids"] == matched["proposed_effect_ids"]
    assert matched["reason"] == "direct_writeback_effect_sequence_matches"
    assert validate_turn_reconciliation_receipt(matched)["ok"] is True
    assert partial["status"] == "shadow_conflict"
    assert partial["applied_effect_ids"] == matched["proposed_effect_ids"][:1]
    assert partial["reason"] == "direct_writeback_effect_sequence_differs"
    assert validate_turn_reconciliation_receipt(partial)["ok"] is True


def test_run_once_preview_has_no_host_or_journal_effects(tmp_path: Path) -> None:
    plan = _plan()

    payload = run_loopx_turn_once(
        plan,
        host_argv=[sys.executable, "-c", "raise SystemExit(9)"],
        project=tmp_path,
        runtime_root=tmp_path / "runtime",
        goal_id="fixture-goal",
        timeout_seconds=5,
        execute=False,
        reconciliation_mode="enforce",
        observe_revision=lambda: "sha256:fixture",
        semantic_escalation=True,
    )

    assert payload["ok"] is True
    assert payload["status"] == "preview"
    assert payload["reconciliation_mode"] == "enforce"
    assert payload["effects"] == {
        "host_invoked": False,
        "state_written": False,
        "quota_spent": False,
        "scheduler_acknowledged": False,
    }
    assert not (tmp_path / "runtime").exists()


def test_run_once_rejects_oversized_built_in_host_result(tmp_path: Path) -> None:
    plan = _plan()
    calls = {"writeback": 0, "spend": 0, "scheduler": 0}
    writeback, spend, scheduler = _callbacks(calls)
    oversized = _host_result(plan)
    oversized["summary"] = "x" * 13_000

    payload = run_loopx_turn_once(
        plan,
        host_runner=lambda _request: oversized,
        project=tmp_path,
        runtime_root=tmp_path / "runtime",
        goal_id="fixture-goal",
        timeout_seconds=5,
        execute=True,
        writeback=writeback,
        spend=spend,
        scheduler=scheduler,
    )

    assert payload["ok"] is False
    assert payload["reason"] == "built-in host result exceeded the result budget"
    assert calls == {"writeback": 0, "spend": 0, "scheduler": 0}


def test_run_once_rejects_nonserializable_built_in_host_result(tmp_path: Path) -> None:
    plan = _plan()
    calls = {"writeback": 0, "spend": 0, "scheduler": 0}
    writeback, spend, scheduler = _callbacks(calls)
    nonserializable = _host_result(plan)
    nonserializable["unexpected"] = {Path("private")}
    observation = _run_host_runner(
        {},
        runner=lambda _request: nonserializable,
    )

    payload = run_loopx_turn_once(
        plan,
        host_runner=lambda _request: nonserializable,
        project=tmp_path,
        runtime_root=tmp_path / "runtime",
        goal_id="fixture-goal",
        timeout_seconds=5,
        execute=True,
        writeback=writeback,
        spend=spend,
        scheduler=scheduler,
    )

    assert observation == {
        "ok": False,
        "reason": "built-in host result is not JSON-serializable",
        "returncode": None,
    }
    assert payload["ok"] is False
    assert payload["reason"] == "built-in host result is not JSON-serializable"
    assert calls == {"writeback": 0, "spend": 0, "scheduler": 0}


def test_run_once_rejects_deeply_nested_built_in_host_result(tmp_path: Path) -> None:
    plan = _plan()
    calls = {"writeback": 0, "spend": 0, "scheduler": 0}
    writeback, spend, scheduler = _callbacks(calls)
    nested: dict[str, object] = {}
    cursor = nested
    for _ in range(sys.getrecursionlimit() + 10):
        child: dict[str, object] = {}
        cursor["nested"] = child
        cursor = child
    nonserializable = _host_result(plan)
    nonserializable["unexpected"] = nested

    payload = run_loopx_turn_once(
        plan,
        host_runner=lambda _request: nonserializable,
        project=tmp_path,
        runtime_root=tmp_path / "runtime",
        goal_id="fixture-goal",
        timeout_seconds=5,
        execute=True,
        writeback=writeback,
        spend=spend,
        scheduler=scheduler,
    )

    assert payload["ok"] is False
    assert payload["reason"] == "built-in host result is not JSON-serializable"
    assert calls == {"writeback": 0, "spend": 0, "scheduler": 0}


def test_enforce_mode_requires_revision_observer_before_host(
    tmp_path: Path,
) -> None:
    plan = _plan()
    calls = {"host": 0, "writeback": 0, "spend": 0, "scheduler": 0}
    writeback, spend, scheduler = _callbacks(calls)

    def host(_request: dict[str, object]) -> dict[str, object]:
        calls["host"] += 1
        return _host_result(plan)

    with pytest.raises(
        ValueError,
        match="enforce reconciliation mode requires observe_revision",
    ):
        run_loopx_turn_once(
            plan,
            host_runner=host,
            project=tmp_path,
            runtime_root=tmp_path / "runtime",
            goal_id="fixture-goal",
            timeout_seconds=5,
            execute=True,
            writeback=writeback,
            spend=spend,
            scheduler=scheduler,
            reconciliation_mode="enforce",
        )

    assert calls == {"host": 0, "writeback": 0, "spend": 0, "scheduler": 0}


def test_run_once_explicitly_retries_failed_host_without_duplicate_effects(
    tmp_path: Path,
) -> None:
    plan = _plan()
    calls = {"host": 0, "writeback": 0, "spend": 0, "scheduler": 0}
    writeback, spend, scheduler = _callbacks(calls)

    def host(_request: dict[str, object]) -> dict[str, object]:
        calls["host"] += 1
        if calls["host"] == 1:
            raise BuiltInHostError("codex_cli_model_requires_newer_codex")
        return _host_result(plan)

    kwargs = {
        "host_runner": host,
        "project": tmp_path,
        "runtime_root": tmp_path / "runtime",
        "goal_id": "fixture-goal",
        "timeout_seconds": 5,
        "execute": True,
        "task_validator": _passing_validator,
        "writeback": writeback,
        "spend": spend,
        "scheduler": scheduler,
    }
    failed = run_loopx_turn_once(plan, **kwargs)
    replayed = run_loopx_turn_once(plan, **kwargs)
    recovered = run_loopx_turn_once(plan, retry_failed=True, **kwargs)

    assert failed["reason"] == "codex_cli_model_requires_newer_codex"
    assert failed["result_kind"] == "host_failure"
    assert failed["receipt"]["result_kind"] == "host_failure"
    assert failed["receipt"]["failed_phase"] == "host_execute"
    assert replayed["replayed"] is True
    assert recovered["status"] == "committed"
    assert calls == {"host": 2, "writeback": 1, "spend": 1, "scheduler": 1}


def test_run_once_rebuilds_derived_records_when_failed_validation_reinvokes_host(
    tmp_path: Path,
) -> None:
    plan = _plan()
    calls = {"host": 0, "writeback": 0, "spend": 0, "scheduler": 0}
    writeback, spend, scheduler = _callbacks(calls)

    def host(_request: dict[str, object]) -> dict[str, object]:
        calls["host"] += 1
        result = _host_result(plan)
        if calls["host"] == 2:
            result["classification"] = "retry_progress"
        return result

    def reject(
        _plan: dict[str, object],
        _result: dict[str, object],
    ) -> dict[str, object]:
        return {
            "status": "failed",
            "validator_kind": "fixture",
            "summary": "retry the host result contract",
            "recovery_kind": "repair_required",
        }

    kwargs = {
        "host_runner": host,
        "project": tmp_path,
        "runtime_root": tmp_path / "runtime",
        "goal_id": "fixture-goal",
        "timeout_seconds": 5,
        "execute": True,
        "writeback": writeback,
        "spend": spend,
        "scheduler": scheduler,
    }
    failed = run_loopx_turn_once(plan, task_validator=reject, **kwargs)
    assert failed["status"] == "failed"

    journal_path = next(
        (tmp_path / "runtime" / "goals" / "fixture-goal" / "turns").glob("*.json")
    )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["validation_stage"] = "host_result_contract"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    recovered = run_loopx_turn_once(
        plan,
        task_validator=_passing_validator,
        retry_failed=True,
        **kwargs,
    )

    assert recovered["status"] == "committed"
    assert recovered["result_record"]["candidate_result"]["classification"] == (
        "retry_progress"
    )
    assert calls == {"host": 2, "writeback": 1, "spend": 1, "scheduler": 1}


def test_run_once_commits_once_and_replays_without_duplicate_effects(
    tmp_path: Path,
) -> None:
    plan = _plan()
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(_host_result(plan)), encoding="utf-8")
    count_path = tmp_path / "host-count"
    calls = {"writeback": 0, "spend": 0, "scheduler": 0}
    writeback, spend, scheduler = _callbacks(calls)
    kwargs = {
        "host_argv": _host_argv(result_path, count_path),
        "project": tmp_path,
        "runtime_root": tmp_path / "runtime",
        "goal_id": "fixture-goal",
        "timeout_seconds": 5,
        "execute": True,
        "task_validator": _passing_validator,
        "writeback": writeback,
        "spend": spend,
        "scheduler": scheduler,
    }

    first = run_loopx_turn_once(plan, **kwargs)
    replay = run_loopx_turn_once(plan, **kwargs)

    assert first["ok"] is True
    assert first["status"] == "committed"
    assert first["receipt"]["status"] == "committed"
    assert first["effects"]["host_invoked"] is True
    assert first["effects"]["state_written"] is True
    assert first["effects"]["quota_spent"] is True
    assert replay["replayed"] is True
    assert not any(replay["effects"].values())
    assert first["result_record"] == replay["result_record"]
    assert first["reconciliation_receipt"] == replay["reconciliation_receipt"]
    assert first["reconciliation_receipt"]["status"] == "not_attempted"
    assert first["shadow_reconciliation_receipt"]["status"] == "shadow_match"
    assert (
        replay["shadow_reconciliation_receipt"]
        == (first["shadow_reconciliation_receipt"])
    )
    assert first["result_ledger"]["status"] == "appended"
    assert first["result_ledger"]["appended_count"] == 1
    assert first["result_ledger"]["row_count"] == 3
    assert replay["result_ledger"]["status"] == "appended"
    ledger_path = (
        tmp_path / "runtime" / "goals" / "fixture-goal" / "turn-result-ledger.jsonl"
    )
    assert read_turn_result_ledger(ledger_path) == [
        first["result_record"],
        first["reconciliation_receipt"],
        first["shadow_reconciliation_receipt"],
    ]
    assert count_path.read_text(encoding="utf-8") == "1"
    assert calls == {"writeback": 1, "spend": 1, "scheduler": 1}


def test_run_once_enforces_matching_revision_and_records_applied_receipt(
    tmp_path: Path,
) -> None:
    plan = _plan()
    calls = {"writeback": 0, "spend": 0, "scheduler": 0}
    writeback, spend, scheduler = _callbacks(calls)

    payload = run_loopx_turn_once(
        plan,
        host_runner=lambda _request: _host_result(plan),
        project=tmp_path,
        runtime_root=tmp_path / "runtime",
        goal_id="fixture-goal",
        timeout_seconds=5,
        execute=True,
        task_validator=_passing_validator,
        writeback=writeback,
        spend=spend,
        scheduler=scheduler,
        reconciliation_mode="enforce",
        observe_revision=lambda: "sha256:fixture",
    )

    assert payload["status"] == "committed"
    assert payload["reconciliation_mode"] == "enforce"
    assert payload["shadow_reconciliation_receipt"] is None
    assert payload["semantic_review_request"] is None
    assert payload["enforced_reconciliation_receipt"]["status"] == "applied"
    assert payload["enforced_reconciliation_receipt"]["observed_revision"] == (
        "sha256:fixture"
    )
    assert payload["enforced_reconciliation_receipt"]["applied_effect_ids"] == [
        effect["effect_id"] for effect in payload["result_record"]["proposed_effects"]
    ]
    assert calls == {"writeback": 1, "spend": 1, "scheduler": 1}


def test_run_once_enforcement_blocks_stale_revision_and_shadow_rolls_back(
    tmp_path: Path,
) -> None:
    plan = _plan()
    calls = {"writeback": 0, "spend": 0, "scheduler": 0}
    writeback, spend, scheduler = _callbacks(calls)
    common = {
        "host_runner": lambda _request: _host_result(plan),
        "project": tmp_path,
        "runtime_root": tmp_path / "runtime",
        "goal_id": "fixture-goal",
        "timeout_seconds": 5,
        "execute": True,
        "task_validator": _passing_validator,
        "writeback": writeback,
        "spend": spend,
        "scheduler": scheduler,
    }

    blocked = run_loopx_turn_once(
        plan,
        reconciliation_mode="enforce",
        observe_revision=lambda: "sha256:stale",
        **common,
    )
    replayed = run_loopx_turn_once(
        plan,
        reconciliation_mode="enforce",
        observe_revision=lambda: "sha256:fixture",
        **common,
    )

    assert blocked["status"] == "reconciliation_blocked"
    assert blocked["enforced_reconciliation_receipt"]["status"] == ("revision_conflict")
    assert blocked["enforced_reconciliation_receipt"]["applied_effect_ids"] == []
    assert replayed["replayed"] is True
    assert calls == {"writeback": 0, "spend": 0, "scheduler": 0}

    rolled_back = run_loopx_turn_once(
        plan,
        reconciliation_mode="shadow",
        **common,
    )

    assert rolled_back["status"] == "committed"
    assert rolled_back["reconciliation_mode"] == "shadow"
    assert rolled_back["shadow_reconciliation_receipt"]["status"] == "shadow_match"
    assert rolled_back["enforced_reconciliation_receipt"]["status"] == (
        "revision_conflict"
    )
    assert calls == {"writeback": 1, "spend": 1, "scheduler": 1}


def test_run_once_escalates_only_pre_effect_revision_ambiguity(tmp_path: Path) -> None:
    plan = _plan()
    calls = {"writeback": 0, "spend": 0, "scheduler": 0}
    writeback, spend, scheduler = _callbacks(calls)

    blocked = run_loopx_turn_once(
        plan,
        host_runner=lambda _request: _host_result(plan),
        project=tmp_path,
        runtime_root=tmp_path / "runtime",
        goal_id="fixture-goal",
        timeout_seconds=5,
        execute=True,
        task_validator=_passing_validator,
        writeback=writeback,
        spend=spend,
        scheduler=scheduler,
        reconciliation_mode="enforce",
        observe_revision=lambda: "sha256:advanced",
        semantic_escalation=True,
    )

    assert blocked["status"] == "reconciliation_blocked"
    assert blocked["enforced_reconciliation_receipt"]["status"] == (
        "revision_conflict"
    )
    assert blocked["semantic_review_request"]["ambiguity_kind"] == (
        "revision_changed_before_effects"
    )
    assert blocked["semantic_review_request"]["observed_revision"] == (
        "sha256:advanced"
    )
    assert calls == {"writeback": 0, "spend": 0, "scheduler": 0}
    ledger = read_turn_result_ledger(
        tmp_path / "runtime" / "goals" / "fixture-goal" / "turn-result-ledger.jsonl"
    )
    assert [row["schema_version"] for row in ledger].count(
        TURN_SEMANTIC_REVIEW_REQUEST_SCHEMA_VERSION
    ) == 1

    rolled_back = run_loopx_turn_once(
        plan,
        host_runner=lambda _request: _host_result(plan),
        project=tmp_path,
        runtime_root=tmp_path / "runtime",
        goal_id="fixture-goal",
        timeout_seconds=5,
        execute=True,
        task_validator=_passing_validator,
        writeback=writeback,
        spend=spend,
        scheduler=scheduler,
        reconciliation_mode="shadow",
    )

    assert rolled_back["status"] == "committed"
    assert rolled_back["semantic_review_request"] is None
    assert calls == {"writeback": 1, "spend": 1, "scheduler": 1}


def test_semantic_escalation_requires_enforce_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires enforce"):
        run_loopx_turn_once(
            _plan(),
            project=tmp_path,
            runtime_root=tmp_path / "runtime",
            goal_id="fixture-goal",
            timeout_seconds=5,
            execute=False,
            semantic_escalation=True,
        )


def test_run_once_enforcement_resume_reuses_checked_revision(
    tmp_path: Path,
) -> None:
    plan = _plan()
    calls = {
        "revision": 0,
        "writeback": 0,
        "spend": 0,
        "scheduler": 0,
    }
    writeback, healthy_spend, scheduler = _callbacks(calls)

    def observe_revision() -> str:
        calls["revision"] += 1
        return "sha256:fixture"

    def interrupted_spend() -> dict[str, object]:
        raise SystemExit(8)

    common = {
        "host_runner": lambda _request: _host_result(plan),
        "project": tmp_path,
        "runtime_root": tmp_path / "runtime",
        "goal_id": "fixture-goal",
        "timeout_seconds": 5,
        "execute": True,
        "task_validator": _passing_validator,
        "writeback": writeback,
        "scheduler": scheduler,
        "reconciliation_mode": "enforce",
        "observe_revision": observe_revision,
    }
    with pytest.raises(SystemExit):
        run_loopx_turn_once(plan, spend=interrupted_spend, **common)

    recovered = run_loopx_turn_once(plan, spend=healthy_spend, **common)

    assert recovered["status"] == "committed"
    assert recovered["enforced_reconciliation_receipt"]["status"] == "applied"
    assert calls == {
        "revision": 1,
        "writeback": 1,
        "spend": 1,
        "scheduler": 1,
    }


def test_distinct_enforced_turns_serialize_revision_check_and_effects(
    tmp_path: Path,
) -> None:
    plans = [
        _plan(turn_instance_id="2026-07-31T09:30:00Z"),
        _plan(turn_instance_id="2026-07-31T09:30:01Z"),
    ]
    current_revision = {"value": "sha256:fixture"}
    first_writeback_started = Event()
    second_host_completed = Event()
    release_first_writeback = Event()
    calls = {"writeback": 0, "spend": 0, "scheduler": 0}
    calls_lock = Lock()

    def observe_revision() -> str:
        return current_revision["value"]

    def writeback(_result: dict[str, object]) -> dict[str, object]:
        with calls_lock:
            calls["writeback"] += 1
            call_number = calls["writeback"]
        assert call_number == 1
        first_writeback_started.set()
        assert release_first_writeback.wait(timeout=3)
        current_revision["value"] = "sha256:advanced"
        return {"ok": True, "appended": True, "classification": "fixture_progress"}

    def spend() -> dict[str, object]:
        with calls_lock:
            calls["spend"] += 1
        return {"ok": True, "appended": True, "slots": 1}

    def scheduler(_spend: dict[str, object]) -> dict[str, object]:
        with calls_lock:
            calls["scheduler"] += 1
        return {
            "completed": True,
            "acknowledged": False,
            "disposition": "not_required",
        }

    def run(index: int) -> dict[str, object]:
        plan = plans[index]

        def host(_request: dict[str, object]) -> dict[str, object]:
            if index == 1:
                second_host_completed.set()
            return _host_result(plan)

        return run_loopx_turn_once(
            plan,
            host_runner=host,
            project=tmp_path,
            runtime_root=tmp_path / "runtime",
            goal_id="fixture-goal",
            timeout_seconds=5,
            execute=True,
            task_validator=_passing_validator,
            writeback=writeback,
            spend=spend,
            scheduler=scheduler,
            reconciliation_mode="enforce",
            observe_revision=observe_revision,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(run, 0)
        assert first_writeback_started.wait(timeout=3)
        second_future = pool.submit(run, 1)
        assert second_host_completed.wait(timeout=3)
        release_first_writeback.set()
        first = first_future.result(timeout=5)
        second = second_future.result(timeout=5)

    assert first["status"] == "committed"
    assert second["status"] == "reconciliation_blocked"
    assert second["enforced_reconciliation_receipt"]["status"] == ("revision_conflict")
    assert calls == {"writeback": 1, "spend": 1, "scheduler": 1}


def test_distinct_shadow_turns_close_out_concurrently(tmp_path: Path) -> None:
    plans = [
        _plan(turn_instance_id="2026-07-31T09:31:00Z"),
        _plan(turn_instance_id="2026-07-31T09:31:01Z"),
    ]
    both_writebacks_started = Event()
    release_writebacks = Event()
    calls = {"writeback": 0, "spend": 0, "scheduler": 0}
    calls_lock = Lock()

    def writeback(_result: dict[str, object]) -> dict[str, object]:
        with calls_lock:
            calls["writeback"] += 1
            if calls["writeback"] == 2:
                both_writebacks_started.set()
        assert both_writebacks_started.wait(timeout=3)
        assert release_writebacks.wait(timeout=3)
        return {"ok": True, "appended": True, "classification": "fixture_progress"}

    def spend() -> dict[str, object]:
        with calls_lock:
            calls["spend"] += 1
        return {"ok": True, "appended": True, "slots": 1}

    def scheduler(_spend: dict[str, object]) -> dict[str, object]:
        with calls_lock:
            calls["scheduler"] += 1
        return {
            "completed": True,
            "acknowledged": False,
            "disposition": "not_required",
        }

    def run(plan: dict[str, object]) -> dict[str, object]:
        return run_loopx_turn_once(
            plan,
            host_runner=lambda _request: _host_result(plan),
            project=tmp_path,
            runtime_root=tmp_path / "runtime",
            goal_id="fixture-goal",
            timeout_seconds=5,
            execute=True,
            task_validator=_passing_validator,
            writeback=writeback,
            spend=spend,
            scheduler=scheduler,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, plan) for plan in plans]
        assert both_writebacks_started.wait(timeout=3)
        release_writebacks.set()
        results = [future.result(timeout=5) for future in futures]

    assert {result["status"] for result in results} == {"committed"}
    assert calls == {"writeback": 2, "spend": 2, "scheduler": 2}


def test_concurrent_same_turn_calls_share_one_result_and_effect_sequence(
    tmp_path: Path,
) -> None:
    plan = _plan()
    counters = {"host": 0, "writeback": 0, "spend": 0, "scheduler": 0}
    counter_lock = Lock()
    host_started = Event()
    second_started = Event()
    release_host = Event()

    def increment(key: str) -> None:
        with counter_lock:
            counters[key] += 1

    def host(_request: dict[str, object]) -> dict[str, object]:
        increment("host")
        host_started.set()
        assert release_host.wait(timeout=3)
        return _host_result(plan)

    def writeback(_result: dict[str, object]) -> dict[str, object]:
        increment("writeback")
        return {"ok": True, "appended": True, "classification": "fixture_progress"}

    def spend() -> dict[str, object]:
        increment("spend")
        return {"ok": True, "appended": True, "slots": 1}

    def scheduler(_spend: dict[str, object]) -> dict[str, object]:
        increment("scheduler")
        return {"completed": True, "acknowledged": False, "disposition": "not_required"}

    kwargs = {
        "host_runner": host,
        "project": tmp_path,
        "runtime_root": tmp_path / "runtime",
        "goal_id": "fixture-goal",
        "timeout_seconds": 5,
        "execute": True,
        "task_validator": _passing_validator,
        "writeback": writeback,
        "spend": spend,
        "scheduler": scheduler,
    }

    def run_second() -> dict[str, object]:
        second_started.set()
        return run_loopx_turn_once(plan, **kwargs)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(run_loopx_turn_once, plan, **kwargs)
        assert host_started.wait(timeout=3)
        second_future = pool.submit(run_second)
        assert second_started.wait(timeout=3)
        release_host.set()
        results = [first_future.result(timeout=5), second_future.result(timeout=5)]

    assert sorted(result["replayed"] for result in results) == [False, True]
    assert {result["status"] for result in results} == {"committed"}
    assert len({result["result_record"]["record_id"] for result in results}) == 1
    assert (
        len({result["reconciliation_receipt"]["receipt_id"] for result in results}) == 1
    )
    assert counters == {"host": 1, "writeback": 1, "spend": 1, "scheduler": 1}


def test_committed_replay_rejects_tampered_result_record(tmp_path: Path) -> None:
    plan = _plan()
    calls = {"writeback": 0, "spend": 0, "scheduler": 0}
    writeback, spend, scheduler = _callbacks(calls)
    kwargs = {
        "host_runner": lambda _request: _host_result(plan),
        "project": tmp_path,
        "runtime_root": tmp_path / "runtime",
        "goal_id": "fixture-goal",
        "timeout_seconds": 5,
        "execute": True,
        "task_validator": _passing_validator,
        "writeback": writeback,
        "spend": spend,
        "scheduler": scheduler,
    }
    committed = run_loopx_turn_once(plan, **kwargs)
    assert committed["status"] == "committed"

    journal_path = next(
        (tmp_path / "runtime" / "goals" / "fixture-goal" / "turns").glob("*.json")
    )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["result_record"]["result_kind"] = "wait"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(ValueError, match="result record failed identity validation"):
        run_loopx_turn_once(plan, **kwargs)

    assert calls == {"writeback": 1, "spend": 1, "scheduler": 1}


def test_committed_replay_rejects_rehashed_result_record(tmp_path: Path) -> None:
    plan = _plan()
    calls = {"writeback": 0, "spend": 0, "scheduler": 0}
    writeback, spend, scheduler = _callbacks(calls)
    kwargs = {
        "host_runner": lambda _request: _host_result(plan),
        "project": tmp_path,
        "runtime_root": tmp_path / "runtime",
        "goal_id": "fixture-goal",
        "timeout_seconds": 5,
        "execute": True,
        "task_validator": _passing_validator,
        "writeback": writeback,
        "spend": spend,
        "scheduler": scheduler,
    }
    committed = run_loopx_turn_once(plan, **kwargs)
    assert committed["status"] == "committed"

    journal_path = next(
        (tmp_path / "runtime" / "goals" / "fixture-goal" / "turns").glob("*.json")
    )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    altered_result = dict(journal["host_result"])
    altered_result["classification"] = "altered_progress"
    altered_record = build_turn_result_record(plan, altered_result)
    journal["result_record"] = altered_record
    journal["reconciliation_receipt"] = build_turn_reconciliation_receipt(
        altered_record,
        status="not_attempted",
        reason="legacy_direct_writeback_not_reconciled",
    )
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match host result"):
        run_loopx_turn_once(plan, **kwargs)

    assert calls == {"writeback": 1, "spend": 1, "scheduler": 1}


def test_run_once_recovers_after_process_exit_before_writeback(tmp_path: Path) -> None:
    plan = _plan()
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(_host_result(plan)), encoding="utf-8")
    count_path = tmp_path / "host-count"
    calls = {"writeback": 0, "spend": 0, "scheduler": 0}
    healthy_writeback, spend, scheduler = _callbacks(calls)

    def interrupted_writeback(_result: dict[str, object]) -> dict[str, object]:
        raise SystemExit(7)

    common = {
        "host_argv": _host_argv(result_path, count_path),
        "project": tmp_path,
        "runtime_root": tmp_path / "runtime",
        "goal_id": "fixture-goal",
        "timeout_seconds": 5,
        "execute": True,
        "task_validator": _passing_validator,
        "spend": spend,
        "scheduler": scheduler,
    }
    with pytest.raises(SystemExit):
        run_loopx_turn_once(plan, writeback=interrupted_writeback, **common)

    interrupted = _journal(tmp_path / "runtime")
    assert interrupted["completed_phases"] == [
        "host_execute",
        "typed_result",
        "validation",
    ]
    assert interrupted["task_validation"]["status"] == "passed"
    assert "writeback" not in interrupted
    assert interrupted["result_record"]["schema_version"] == (
        TURN_RESULT_RECORD_SCHEMA_VERSION
    )
    assert interrupted["reconciliation_receipt"]["status"] == "not_attempted"
    result_record_id = interrupted["result_record"]["record_id"]

    recovered = run_loopx_turn_once(plan, writeback=healthy_writeback, **common)

    assert recovered["status"] == "committed"
    assert recovered["result_record"]["record_id"] == result_record_id
    assert count_path.read_text(encoding="utf-8") == "1"
    assert calls == {"writeback": 1, "spend": 1, "scheduler": 1}


def test_run_once_resumes_after_writeback_without_duplicate_effects(
    tmp_path: Path,
) -> None:
    plan = _plan()
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(_host_result(plan)), encoding="utf-8")
    count_path = tmp_path / "host-count"
    calls = {"writeback": 0, "spend": 0, "scheduler": 0}
    writeback, healthy_spend, scheduler = _callbacks(calls)

    def interrupted_spend() -> dict[str, object]:
        raise SystemExit(8)

    common = {
        "host_argv": _host_argv(result_path, count_path),
        "project": tmp_path,
        "runtime_root": tmp_path / "runtime",
        "goal_id": "fixture-goal",
        "timeout_seconds": 5,
        "execute": True,
        "task_validator": _passing_validator,
        "writeback": writeback,
        "scheduler": scheduler,
    }
    with pytest.raises(SystemExit):
        run_loopx_turn_once(plan, spend=interrupted_spend, **common)

    interrupted = _journal(tmp_path / "runtime")
    assert interrupted["completed_phases"] == [
        "host_execute",
        "typed_result",
        "validation",
        "durable_writeback",
    ]
    assert interrupted["writeback"]["appended"] is True
    assert "quota_spend" not in interrupted

    transaction = plan["transaction"]
    assert isinstance(transaction, dict)
    resumed_plan = load_loopx_turn_plan_from_journal(
        tmp_path / "runtime",
        goal_id="fixture-goal",
        turn_key=str(transaction["turn_key"]),
    )
    recovered = run_loopx_turn_once(resumed_plan, spend=healthy_spend, **common)

    assert recovered["status"] == "committed"
    assert count_path.read_text(encoding="utf-8") == "1"
    assert calls == {"writeback": 1, "spend": 1, "scheduler": 1}


def test_run_once_fails_closed_without_independent_task_validator(
    tmp_path: Path,
) -> None:
    plan = _plan()
    calls = {"writeback": 0, "spend": 0, "scheduler": 0}
    writeback, spend, scheduler = _callbacks(calls)

    payload = run_loopx_turn_once(
        plan,
        host_runner=lambda _request: _host_result(plan),
        project=tmp_path,
        runtime_root=tmp_path / "runtime",
        goal_id="fixture-goal",
        timeout_seconds=5,
        execute=True,
        writeback=writeback,
        spend=spend,
        scheduler=scheduler,
    )

    assert payload["ok"] is False
    assert payload["status"] == "failed"
    assert payload["result_kind"] == "validation_failed"
    assert payload["validation"]["status"] == "unavailable"
    assert payload["validation"]["recovery_kind"] == "repair_required"
    assert payload["receipt"]["failed_phase"] == "validation"
    assert payload["receipt"]["completed_phases"] == ["host_execute", "typed_result"]
    assert calls == {"writeback": 0, "spend": 0, "scheduler": 0}


def test_run_once_retries_task_validation_without_reinvoking_host(
    tmp_path: Path,
) -> None:
    plan = _plan()
    calls = {"host": 0, "writeback": 0, "spend": 0, "scheduler": 0}
    writeback, spend, scheduler = _callbacks(calls)

    def host(_request: dict[str, object]) -> dict[str, object]:
        calls["host"] += 1
        return _host_result(plan)

    def reject(
        _plan: dict[str, object],
        _result: dict[str, object],
    ) -> dict[str, object]:
        return {
            "status": "failed",
            "validator_kind": "fixture",
            "summary": "independent fixture postcondition is absent",
            "recovery_kind": "replan_required",
        }

    common = {
        "host_runner": host,
        "project": tmp_path,
        "runtime_root": tmp_path / "runtime",
        "goal_id": "fixture-goal",
        "timeout_seconds": 5,
        "execute": True,
        "writeback": writeback,
        "spend": spend,
        "scheduler": scheduler,
    }
    failed = run_loopx_turn_once(plan, task_validator=reject, **common)
    recovered = run_loopx_turn_once(
        plan,
        task_validator=_passing_validator,
        retry_failed=True,
        **common,
    )

    assert failed["result_kind"] == "validation_failed"
    assert failed["validation"]["recovery_kind"] == "replan_required"
    assert recovered["status"] == "committed"
    assert recovered["effects"]["host_invoked"] is False
    assert calls == {"host": 1, "writeback": 1, "spend": 1, "scheduler": 1}


def test_material_result_cannot_use_not_required_validation_receipt(
    tmp_path: Path,
) -> None:
    plan = _plan()
    calls = {"writeback": 0, "spend": 0, "scheduler": 0}
    writeback, spend, scheduler = _callbacks(calls)

    payload = run_loopx_turn_once(
        plan,
        host_runner=lambda _request: _host_result(plan),
        project=tmp_path,
        runtime_root=tmp_path / "runtime",
        goal_id="fixture-goal",
        timeout_seconds=5,
        execute=True,
        task_validator=lambda _plan, _result: {
            "status": "not_required",
            "validator_kind": "fixture",
            "summary": "skip validation",
        },
        writeback=writeback,
        spend=spend,
        scheduler=scheduler,
    )

    assert payload["result_kind"] == "validation_failed"
    assert payload["validation"]["status"] == "inconclusive"
    assert "cannot skip" in payload["validation"]["summary"]
    assert calls == {"writeback": 0, "spend": 0, "scheduler": 0}


def test_run_once_stops_without_writeback_or_spend(tmp_path: Path) -> None:
    plan = _plan()
    result_path = tmp_path / "result.json"
    result_path.write_text(
        json.dumps(_host_result(plan, kind="wait")), encoding="utf-8"
    )
    calls = {"writeback": 0, "spend": 0, "scheduler": 0}
    writeback, spend, scheduler = _callbacks(calls)

    payload = run_loopx_turn_once(
        plan,
        host_argv=_host_argv(result_path, tmp_path / "host-count"),
        project=tmp_path,
        runtime_root=tmp_path / "runtime",
        goal_id="fixture-goal",
        timeout_seconds=5,
        execute=True,
        writeback=writeback,
        spend=spend,
        scheduler=scheduler,
    )

    assert payload["ok"] is True
    assert payload["status"] == "stopped"
    assert payload["receipt"]["status"] == "stopped"
    assert calls == {"writeback": 0, "spend": 0, "scheduler": 0}


def test_run_once_projects_scheduler_action_without_false_ack(tmp_path: Path) -> None:
    plan = _plan()
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(_host_result(plan)), encoding="utf-8")
    calls = {"writeback": 0, "spend": 0, "scheduler": 0}
    writeback, spend, _scheduler = _callbacks(calls)

    def scheduler(_spend: dict[str, object]) -> dict[str, object]:
        calls["scheduler"] += 1
        return {
            "completed": False,
            "apply_needed": True,
            "disposition": "host_action_required",
        }

    payload = run_loopx_turn_once(
        plan,
        host_argv=_host_argv(result_path, tmp_path / "host-count"),
        project=tmp_path,
        runtime_root=tmp_path / "runtime",
        goal_id="fixture-goal",
        timeout_seconds=5,
        execute=True,
        task_validator=_passing_validator,
        writeback=writeback,
        spend=spend,
        scheduler=scheduler,
    )

    assert payload["ok"] is True
    assert payload["status"] == "scheduler_action_required"
    assert payload["receipt"]["next_phase"] == "scheduler_apply"
    assert payload["effects"]["scheduler_acknowledged"] is False
    assert payload["shadow_reconciliation_receipt"]["status"] == "shadow_conflict"
    assert [
        effect["kind"]
        for effect in payload["result_record"]["proposed_effects"]
        if effect["effect_id"]
        in payload["shadow_reconciliation_receipt"]["applied_effect_ids"]
    ] == ["goal_state_refresh", "quota_spend"]


def test_run_once_resumes_scheduler_without_repeating_committed_effects(
    tmp_path: Path,
) -> None:
    plan = _plan()
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(_host_result(plan)), encoding="utf-8")
    count_path = tmp_path / "host-count"
    calls = {"writeback": 0, "spend": 0, "scheduler": 0}
    writeback, spend, _scheduler = _callbacks(calls)

    def scheduler(_spend: dict[str, object]) -> dict[str, object]:
        calls["scheduler"] += 1
        if calls["scheduler"] == 1:
            return {
                "completed": False,
                "apply_needed": True,
                "disposition": "host_action_required",
            }
        return {
            "completed": True,
            "acknowledged": True,
            "disposition": "applied_and_acknowledged",
        }

    kwargs = {
        "host_argv": _host_argv(result_path, count_path),
        "project": tmp_path,
        "runtime_root": tmp_path / "runtime",
        "goal_id": "fixture-goal",
        "timeout_seconds": 5,
        "execute": True,
        "task_validator": _passing_validator,
        "writeback": writeback,
        "spend": spend,
        "scheduler": scheduler,
    }
    first = run_loopx_turn_once(plan, **kwargs)
    resumed = run_loopx_turn_once(plan, **kwargs)

    assert first["status"] == "scheduler_action_required"
    assert first["shadow_reconciliation_receipt"]["status"] == "shadow_conflict"
    assert resumed["status"] == "committed"
    assert resumed["shadow_reconciliation_receipt"]["status"] == "shadow_match"
    ledger_path = (
        tmp_path / "runtime" / "goals" / "fixture-goal" / "turn-result-ledger.jsonl"
    )
    assert len(read_turn_result_ledger(ledger_path)) == 4
    assert resumed["effects"]["scheduler_acknowledged"] is True
    assert count_path.read_text(encoding="utf-8") == "1"
    assert calls == {"writeback": 1, "spend": 1, "scheduler": 2}
