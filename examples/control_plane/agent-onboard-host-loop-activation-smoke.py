#!/usr/bin/env python3
"""Smoke-test agent onboarding and host-loop activation routing."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loopx.bootstrap_command_pack import build_loopx_bootstrap_command_pack  # noqa: E402
from loopx.capabilities.multi_agent.runtime_scripts import (  # noqa: E402
    CURSOR_CLI_LOOP_PY,
    CURSOR_CLI_TICK_WORKER_PY,
)
from loopx.host_loop_activation import (  # noqa: E402
    agent_type_for_host_surface,
    build_agent_type_catalog,
    build_host_loop_activation_packet,
)
from loopx.slash_command_install import install_slash_commands  # noqa: E402


def run_cli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "loopx.cli", "--format", "json", *args],
        cwd=REPO_ROOT,
        check=check,
        text=True,
        capture_output=True,
    )


def main() -> int:
    catalog = build_agent_type_catalog()
    agent_types = {item["agent_type"] for item in catalog["canonical_agent_types"]}
    assert {"codex-app", "codex-cli", "claude-code", "cursor-cli", "manual", "other-agent"} <= agent_types
    ambiguous = {item["input"]: item["use_one_of"] for item in catalog["ambiguous_inputs"]}
    assert ambiguous["codex"] == ["codex-app", "codex-cli"], ambiguous

    assert agent_type_for_host_surface("chat-box") == "codex-app"
    assert agent_type_for_host_surface("codex-cli-tui") == "codex-cli"
    assert agent_type_for_host_surface("cursor-cli") == "cursor-cli"

    codex_app = build_host_loop_activation_packet(agent_type="codex-app", goal_id="demo")
    codex_cli = build_host_loop_activation_packet(agent_type="codex-cli", goal_id="demo")
    claude_code = build_host_loop_activation_packet(agent_type="claude-code", goal_id="demo")
    assert codex_app["activation_method"] == "create_or_update_codex_app_automation", codex_app
    assert codex_cli["host_mutation"]["host_command"] == "/goal <task_body>", codex_cli
    assert claude_code["host_mutation"]["host_command"] == "/loop", claude_code

    cursor_cli = build_host_loop_activation_packet(agent_type="cursor-cli", goal_id="demo")
    assert cursor_cli["host_surface"] == "cursor_cli_external_loop_driver", cursor_cli
    assert cursor_cli["native_loop_runtime_present"] is False, cursor_cli
    assert cursor_cli["native_goal_api_present"] is False, cursor_cli
    assert cursor_cli["control_plane"] == "loopx_cli_subcommands", cursor_cli

    # Phase 3: tick driver contract is exposed in the activation packet
    tick_driver = cursor_cli.get("tick_driver", {})
    assert tick_driver.get("seam") == "loopx_cli_quota_should_run", tick_driver
    assert "loopx-cursor-cli-tick-worker" in tick_driver.get("tick_script", ""), tick_driver
    assert "loopx-cursor-cli-tick-worker" in tick_driver.get("tick_invocation", ""), tick_driver

    # Phase 3: CURSOR_CLI_TICK_WORKER_PY contract assertions (no real cursor-agent invocation)
    assert "cursor-agent" in CURSOR_CLI_TICK_WORKER_PY, "tick worker must reference cursor-agent"
    assert "'-p'" in CURSOR_CLI_TICK_WORKER_PY or '"-p"' in CURSOR_CLI_TICK_WORKER_PY, (
        "tick worker must pass -p flag to cursor-agent"
    )
    assert "'--output-format'" in CURSOR_CLI_TICK_WORKER_PY or '"--output-format"' in CURSOR_CLI_TICK_WORKER_PY, (
        "tick worker must pass --output-format to cursor-agent"
    )
    assert "LOOPX_GOAL_ID" in CURSOR_CLI_TICK_WORKER_PY, "tick worker must read LOOPX_GOAL_ID"
    assert "LOOPX_AGENT_ID" in CURSOR_CLI_TICK_WORKER_PY, "tick worker must read LOOPX_AGENT_ID"
    assert "quota" in CURSOR_CLI_TICK_WORKER_PY and "should-run" in CURSOR_CLI_TICK_WORKER_PY, (
        "tick worker must embed the quota should-run gate"
    )
    assert "_PAUSED_EXIT = 75" in CURSOR_CLI_TICK_WORKER_PY, (
        "tick worker must distinguish paused quota from completed ticks"
    )
    assert "raise SystemExit(_PAUSED_EXIT)" in CURSOR_CLI_TICK_WORKER_PY, (
        "paused quota must not return success to the outer loop"
    )
    assert "result.returncode == 75" in CURSOR_CLI_LOOP_PY, (
        "outer loop must treat paused quota separately from completed ticks"
    )
    assert "quota paused" in CURSOR_CLI_LOOP_PY and "pause_interval" in CURSOR_CLI_LOOP_PY, (
        "paused quota must use the pause backoff path"
    )
    assert "heartbeat-prompt" in CURSOR_CLI_TICK_WORKER_PY, (
        "tick worker must use heartbeat-prompt as the adaptive prompt source"
    )
    assert "task_body" in CURSOR_CLI_TICK_WORKER_PY, (
        "tick worker must extract task_body from heartbeat-prompt response"
    )
    assert "LOOPX_CURSOR_MODEL" in CURSOR_CLI_TICK_WORKER_PY, "tick worker must read LOOPX_CURSOR_MODEL"
    assert "'--model'" in CURSOR_CLI_TICK_WORKER_PY or '"--model"' in CURSOR_CLI_TICK_WORKER_PY, (
        "tick worker must pass --model to cursor-agent"
    )
    assert "composer-2.5" in CURSOR_CLI_TICK_WORKER_PY, "tick worker must have a safe model default"
    assert tick_driver.get("model_env_var") == "LOOPX_CURSOR_MODEL", tick_driver
    assert tick_driver.get("model_default") == "composer-2.5", tick_driver

    # Phase 3: slash-commands --install --surface cursor exposes cursor_tick_worker path
    install_dry = install_slash_commands(execute=False, surfaces=["cursor"])
    assert install_dry["ok"] is True, install_dry
    tick_worker_summary = install_dry["summary"].get("cursor_tick_worker")
    assert tick_worker_summary is not None, "cursor_tick_worker must be in install summary"
    assert "loopx-cursor-cli-tick-worker" in tick_worker_summary, tick_worker_summary
    tick_worker_items = [
        item for item in install_dry.get("installed", [])
        if item.get("mechanism") == "cursor_cli_tick_worker_script"
    ]
    assert len(tick_worker_items) == 1, f"expected 1 tick worker install item, got {tick_worker_items}"
    assert tick_worker_items[0].get("status") in ("would_create", "created", "updated", "unchanged"), tick_worker_items[0]

    # "cursor" is an accepted alias, not an ambiguous input — resolves unambiguously to cursor-cli
    cursor_alias = build_host_loop_activation_packet(agent_type="cursor", goal_id="demo")
    assert cursor_alias["agent_type"] == "cursor-cli", cursor_alias
    assert "cursor" not in {item["input"] for item in catalog["ambiguous_inputs"]}, catalog

    list_result = run_cli("agent-onboard", "--list-agent-types")
    list_payload = json.loads(list_result.stdout)
    assert list_payload["schema_version"] == "loopx_agent_type_catalog_v0", list_payload

    ambiguous_result = run_cli(
        "agent-onboard",
        "--agent-type",
        "codex",
        "--project",
        ".",
        check=False,
    )
    assert ambiguous_result.returncode == 2, ambiguous_result.stdout
    ambiguous_payload = json.loads(ambiguous_result.stdout)
    assert ambiguous_payload["ok"] is False, ambiguous_payload
    assert ambiguous_payload["suggestions"] == ["codex-app", "codex-cli"], ambiguous_payload

    with tempfile.TemporaryDirectory(prefix="loopx-agent-onboard-smoke-") as tmp:
        project = Path(tmp) / "project"
        project.mkdir()
        payload = build_loopx_bootstrap_command_pack(
            project=project,
            goal_id="demo-goal",
            agent_id="codex-value-explorer",
            cli_bin="loopx",
            host_surface="codex-cli-tui",
            goal_text="build a deterministic onboarding path",
        )
    assert payload["agent_type"] == "codex-cli", payload
    activation = payload["host_loop_activation"]
    assert activation["host_surface"] == "codex_cli_visible_goal_mode", activation
    contract = payload["goal_start_contract"]
    assert contract["activation"]["host_loop_required_after_todo_writeback"] is True, contract
    assert payload["safety_contract"]["explicit_goal_start_must_activate_host_loop"] is True, payload
    message = payload["message"]
    assert "/goal <task_body>" in message, message
    assert "agent-onboard" in message, message

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
