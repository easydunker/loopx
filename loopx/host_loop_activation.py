from __future__ import annotations

from typing import Any

from .project_prompt import (
    render_heartbeat_prompt_command,
    render_heartbeat_prompt_json_command,
)


SCHEMA_VERSION = "loopx_host_loop_activation_v1"
AGENT_TYPE_CATALOG_SCHEMA_VERSION = "loopx_agent_type_catalog_v0"

SUPPORTED_AGENT_TYPES = ["codex-app", "codex-cli", "claude-code", "cursor-cli", "manual", "other-agent"]

AGENT_TYPE_CATALOG: dict[str, dict[str, Any]] = {
    "codex-app": {
        "display_name": "Codex App",
        "host_loop": "Codex App heartbeat automation",
        "entry": "$loopx <task> or the explicit LoopX skill from /skills",
        "accepted_inputs": ["codex-app", "codex_app", "codex app", "codex-desktop", "codex desktop"],
    },
    "codex-cli": {
        "display_name": "Codex CLI TUI",
        "host_loop": "visible Codex CLI /goal",
        "entry": "$loopx <task> or the explicit LoopX skill from /skills",
        "accepted_inputs": [
            "codex-cli",
            "codex_cli",
            "codex cli",
            "codex-cli-tui",
            "codex_cli_tui",
            "codex tui",
        ],
    },
    "claude-code": {
        "display_name": "Claude Code",
        "host_loop": "native /loop gated by LoopX",
        "entry": "/loopx <task> then /loop",
        "accepted_inputs": ["claude-code", "claude_code", "claude code", "cc"],
    },
    "cursor-cli": {
        "display_name": "Cursor CLI",
        "host_loop": "external loop driver re-invoking cursor-agent -p, gated by LoopX CLI should_run",
        "entry": "loopx slash-commands --install --surface cursor, then the LoopX-owned tick driver",
        "accepted_inputs": [
            "cursor-cli",
            "cursor_cli",
            "cursor cli",
            "cursor-agent",
            "cursor agent",
            "cursor",
        ],
    },
    "manual": {
        "display_name": "Manual shell / external scheduler",
        "host_loop": "external scheduler or manual quota/status loop",
        "entry": "CLI packet plus an external loop driver",
        "accepted_inputs": ["manual", "shell", "manual-shell", "external-scheduler"],
    },
    "other-agent": {
        "display_name": "Other explicit agent host",
        "host_loop": "custom agent loop driver",
        "entry": "@loopx <task>, $loopx <task>, or another host facade",
        "accepted_inputs": ["other-agent", "other_agent", "custom-agent", "custom agent"],
    },
}

AMBIGUOUS_AGENT_TYPE_INPUTS: dict[str, list[str]] = {
    "codex": ["codex-app", "codex-cli"],
    "openai-codex": ["codex-app", "codex-cli"],
    "openai codex": ["codex-app", "codex-cli"],
    "cli": ["codex-cli", "manual", "other-agent"],
}


def _agent_type_key(value: str | None) -> str:
    return (value or "").strip().lower().replace("_", "-")


AGENT_TYPE_ALIASES = {
    _agent_type_key(alias): canonical
    for canonical, metadata in AGENT_TYPE_CATALOG.items()
    for alias in metadata["accepted_inputs"]
}


class AgentTypeError(ValueError):
    def __init__(self, *, value: str | None, reason: str, suggestions: list[str] | None = None) -> None:
        self.value = value
        self.reason = reason
        self.suggestions = suggestions or []
        super().__init__(reason)

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": "loopx_agent_type_error_v0",
            "error_kind": "ambiguous_or_unsupported_agent_type",
            "agent_type": self.value,
            "reason": self.reason,
            "suggestions": self.suggestions,
            "agent_type_catalog": build_agent_type_catalog(),
        }


HOST_SURFACE_TO_AGENT_TYPE = {
    "codex-app": "codex-app",
    "chat-box": "codex-app",
    "codex-cli-tui": "codex-cli",
    "claude-code": "claude-code",
    "cursor-cli": "cursor-cli",
    "shell": "manual",
    "http": "other-agent",
    "worker-bridge": "other-agent",
}


def build_agent_type_catalog() -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": AGENT_TYPE_CATALOG_SCHEMA_VERSION,
        "canonical_agent_types": [
            {
                "agent_type": agent_type,
                "display_name": metadata["display_name"],
                "host_loop": metadata["host_loop"],
                "entry": metadata["entry"],
                "accepted_inputs": metadata["accepted_inputs"],
            }
            for agent_type, metadata in AGENT_TYPE_CATALOG.items()
        ],
        "ambiguous_inputs": [
            {"input": value, "use_one_of": choices}
            for value, choices in AMBIGUOUS_AGENT_TYPE_INPUTS.items()
        ],
        "selection_rule": (
            "Agents should pass a canonical agent_type. Ambiguous values such as "
            "`codex` are rejected because Codex App and Codex CLI have different "
            "host-loop activation paths."
        ),
    }


def render_agent_type_catalog_markdown(payload: dict[str, Any]) -> str:
    if not payload.get("ok") and isinstance(payload.get("agent_type_catalog"), dict):
        catalog = payload["agent_type_catalog"]
        header = [
            "# LoopX Agent Type Error",
            "",
            f"- ok: `{payload.get('ok')}`",
            f"- error_kind: `{payload.get('error_kind')}`",
            f"- agent_type: `{payload.get('agent_type')}`",
            f"- reason: {payload.get('reason')}",
            f"- suggestions: `{', '.join(payload.get('suggestions') or [])}`",
            "",
        ]
        return "\n".join(header) + render_agent_type_catalog_markdown(catalog)
    lines = [
        "# LoopX Agent Types",
        "",
        str(payload.get("selection_rule") or ""),
        "",
        "| agent_type | Host loop | Entry | Accepted inputs |",
        "| --- | --- | --- | --- |",
    ]
    for item in payload.get("canonical_agent_types") or []:
        if not isinstance(item, dict):
            continue
        aliases = ", ".join(f"`{value}`" for value in item.get("accepted_inputs") or [])
        lines.append(
            "| "
            f"`{item.get('agent_type')}` | "
            f"{item.get('host_loop')} | "
            f"{item.get('entry')} | "
            f"{aliases} |"
        )
    lines.extend(["", "Ambiguous inputs:"])
    for item in payload.get("ambiguous_inputs") or []:
        if isinstance(item, dict):
            choices = ", ".join(f"`{value}`" for value in item.get("use_one_of") or [])
            lines.append(f"- `{item.get('input')}` -> use one of {choices}")
    return "\n".join(lines)


def normalize_agent_type(value: str | None) -> str:
    key = _agent_type_key(value)
    if not key:
        raise AgentTypeError(
            value=value,
            reason="agent_type is required",
            suggestions=SUPPORTED_AGENT_TYPES,
        )
    if key in AMBIGUOUS_AGENT_TYPE_INPUTS:
        suggestions = AMBIGUOUS_AGENT_TYPE_INPUTS[key]
        raise AgentTypeError(
            value=value,
            reason=(
                f"agent_type {value!r} is ambiguous; choose the exact host runtime "
                f"because each one has a different host_loop_activation"
            ),
            suggestions=suggestions,
        )
    try:
        return AGENT_TYPE_ALIASES[key]
    except KeyError as exc:
        raise AgentTypeError(
            value=value,
            reason=f"unsupported agent_type {value!r}",
            suggestions=SUPPORTED_AGENT_TYPES,
        ) from exc


def agent_type_for_host_surface(value: str | None) -> str:
    key = (value or "chat-box").strip().lower()
    if key in HOST_SURFACE_TO_AGENT_TYPE:
        return HOST_SURFACE_TO_AGENT_TYPE[key]
    return normalize_agent_type(key)


def _heartbeat_commands(
    *,
    goal_id: str,
    agent_type: str,
    cli_bin: str,
    agent_id: str | None,
) -> dict[str, str]:
    scope_by_type = {
        "codex-app": "Codex App heartbeat automation",
        "codex-cli": "Codex CLI /goal visible TUI loop",
        "claude-code": "Claude Code native /loop gated by LoopX",
        "cursor-cli": "Cursor CLI external loop driver re-invoking cursor-agent -p, gated by LoopX",
        "manual": "External scheduler or manual shell LoopX poll",
        "other-agent": "Custom agent host loop gated by LoopX",
    }
    agent_scope = scope_by_type.get(agent_type, scope_by_type["other-agent"])
    return {
        "heartbeat_prompt_json": render_heartbeat_prompt_json_command(
            goal_id,
            cli_bin=cli_bin,
            agent_id=agent_id,
            agent_scope=agent_scope,
        ),
        "heartbeat_prompt": render_heartbeat_prompt_command(
            goal_id,
            cli_bin=cli_bin,
            agent_id=agent_id,
            agent_scope=agent_scope,
        ),
    }


def _codex_app_activation(commands: dict[str, str]) -> dict[str, Any]:
    return {
        "host_surface": "codex_app_heartbeat_automation",
        "entry_command_hint": "$loopx <task> or the explicit LoopX skill from /skills",
        "activation_method": "create_or_update_codex_app_automation",
        "activation_input_command": commands["heartbeat_prompt_json"],
        "host_mutation": {
            "owner": "Codex App host",
            "preferred_tool": "automation_update",
            "cli_can_mutate_directly": False,
            "missing_host_tool_gate": (
                "Codex App automation_update is unavailable; surface a pasteable "
                "heartbeat task_body gate instead of claiming autonomous setup."
            ),
        },
        "activation_steps": [
            "Run the heartbeat-prompt JSON command after project state and todos are written.",
            "Read task_body from the JSON payload.",
            "Create or update a Codex App heartbeat automation starting at 3 minutes.",
            "On later ticks, follow quota should-run scheduler_hint for backoff, reset, and scheduler-ack.",
        ],
        "success_criteria": [
            "A Codex App heartbeat automation exists for this goal and uses the generated task_body.",
            "The next wakeup starts from LoopX quota/status/state, not stale chat memory.",
        ],
    }


def _codex_cli_activation(commands: dict[str, str]) -> dict[str, Any]:
    return {
        "host_surface": "codex_cli_visible_goal_mode",
        "entry_command_hint": "$loopx <task> or the explicit LoopX skill from /skills",
        "activation_method": "set_visible_tui_goal",
        "activation_input_command": commands["heartbeat_prompt_json"],
        "host_mutation": {
            "owner": "Codex CLI TUI",
            "host_command": "/goal <task_body>",
            "cli_can_mutate_directly": False,
            "missing_host_tool_gate": (
                "Current session cannot set Codex CLI /goal; show the exact "
                "`/goal <task_body>` text for the user to paste."
            ),
        },
        "activation_steps": [
            "Run the heartbeat-prompt JSON command after project state and todos are written.",
            "Read task_body from the JSON payload.",
            "Set the visible Codex CLI TUI goal to `/goal <task_body>`.",
            "Keep delivery in the visible TUI; do not switch to hidden headless execution.",
        ],
        "success_criteria": [
            "The visible Codex CLI TUI has `/goal <task_body>` active for this goal.",
            "Future TUI turns enter through LoopX quota/status/state before delivery work.",
        ],
    }


def _claude_code_activation(commands: dict[str, str], cli_bin: str) -> dict[str, Any]:
    return {
        "host_surface": "claude_code_native_loop",
        "entry_command_hint": "/loopx <task> then /loop",
        "activation_method": "arm_loopx_then_run_native_loop",
        "activation_input_command": commands["heartbeat_prompt_json"],
        "setup_command": f"{cli_bin} slash-commands --install --surface claude-code",
        "host_mutation": {
            "owner": "Claude Code",
            "host_command": "/loop",
            "cli_can_mutate_directly": False,
            "missing_host_tool_gate": (
                "Claude Code adapter or native /loop is unavailable; install the "
                "Claude Code LoopX surface or report the exact gate."
            ),
        },
        "activation_steps": [
            "Install or refresh the Claude Code LoopX surface when needed.",
            "Run `/loopx <task>` to arm LoopX state for the task.",
            "Run native `/loop`; the adapter gates each tick through LoopX should_run.",
        ],
        "success_criteria": [
            "Claude Code has the LoopX command surface installed.",
            "Native `/loop` is running with LoopX should_run gating, not an unrelated free-running loop.",
        ],
    }


def _cursor_cli_activation(commands: dict[str, str], cli_bin: str) -> dict[str, Any]:
    return {
        "host_surface": "cursor_cli_external_loop_driver",
        "entry_command_hint": "loopx slash-commands --install --surface cursor, then start the LoopX-owned tick driver",
        "activation_method": "install_surface_then_run_external_tick_driver",
        "activation_input_command": commands["heartbeat_prompt_json"],
        "setup_command": f"{cli_bin} slash-commands --install --surface cursor",
        "native_goal_api_present": False,
        "native_loop_runtime_present": False,
        "control_plane": "loopx_cli_subcommands",
        "tick_driver": {
            "seam": "loopx_cli_quota_should_run",
            "tick_script": "~/.cursor/bin/loopx-cursor-cli-tick-worker",
            "tick_installed_by": f"{cli_bin} slash-commands --install --surface cursor",
            "tick_invocation": "~/.cursor/bin/loopx-cursor-cli-tick-worker",
            "pane_a2a_compat": "LOOPX_PANE_WORKER_TURN=~/.cursor/bin/loopx-cursor-cli-tick-worker $LOOPX_PANE_A2A_TICK",
            "model_env_var": "LOOPX_CURSOR_MODEL",
            "model_default": "composer-2.5",
            "model_note": (
                "Set LOOPX_CURSOR_MODEL to control cost per tick. "
                "Defaults to composer-2.5 with a warning if unset. "
                "Thinking/high models cost significantly more — set explicitly. "
                "Run cursor-agent --list-models for available options."
            ),
            "note": (
                "Self-contained tick: gates via loopx quota should-run, then calls "
                "loopx heartbeat-prompt --thin to get the adaptive task_body (goal-start "
                "instructions when no todos exist, execution instructions otherwise), "
                "then invokes cursor-agent -p <task_body> --model $LOOPX_CURSOR_MODEL. "
                "Same unified planning+execution behaviour as the Codex TUI goal session."
            ),
        },
        "host_mutation": {
            "owner": "Cursor CLI",
            "host_command": "cursor-agent -p <task_body>",
            "cli_can_mutate_directly": False,
            "missing_host_tool_gate": (
                "No native Cursor CLI loop runtime present; surface a pasteable "
                "external-loop gate showing the cursor-agent invocation and the "
                "`loopx quota should-run` gate command."
            ),
        },
        "activation_steps": [
            "Install or refresh the Cursor CLI LoopX surface: `loopx slash-commands --install --surface cursor`.",
            "This writes ~/.cursor/rules/loopx.mdc (tick protocol) and ~/.cursor/bin/loopx-cursor-cli-tick-worker (self-contained tick).",
            "Set env: export LOOPX_GOAL_ID=<id> LOOPX_AGENT_ID=<id> LOOPX_PROJECT=$(pwd)",
            "Set model: export LOOPX_CURSOR_MODEL=composer-2.5  # or gpt-5.3-codex-low for cheaper",
            "Run one tick: ~/.cursor/bin/loopx-cursor-cli-tick-worker",
            "The tick worker gates via loopx quota should-run, then invokes cursor-agent -p per allowed tick.",
            "Auto-approval flags (--force / --yolo / --approve-mcps) are not passed by the LoopX-owned driver; use direct Cursor CLI outside loopx agent-start until an owner-authorized pass-through exists.",
        ],
        "success_criteria": [
            "The LoopX-owned tick worker (loopx-cursor-cli-tick-worker) is installed and executable.",
            "Each tick invocation starts from `loopx quota should-run` before invoking cursor-agent.",
            "Writeback uses `loopx todo complete` and `loopx quota spend-slot` after validated delivery.",
        ],
    }


def _manual_activation(commands: dict[str, str]) -> dict[str, Any]:
    return {
        "host_surface": "external_scheduler_or_manual_shell",
        "entry_command_hint": "run loopx agent-onboard, then wire a scheduler or invoke quota manually",
        "activation_method": "external_loop_driver",
        "activation_input_command": commands["heartbeat_prompt_json"],
        "host_mutation": {
            "owner": "external agent or operator",
            "cli_can_mutate_directly": False,
            "missing_host_tool_gate": (
                "No host loop is declared. Wire a cron/task/agent loop that starts "
                "from quota should-run, or run LoopX manually."
            ),
        },
        "activation_steps": [
            "Generate the heartbeat-prompt JSON task body or equivalent lifecycle prompt.",
            "Configure the external loop driver to call quota should-run before each delivery slice.",
            "Record evidence/writeback and spend quota only after validated delivery work.",
        ],
        "success_criteria": [
            "An external loop driver reliably starts from the LoopX quota/status contract.",
            "The driver has an explicit stop/backoff policy for no-progress or unchanged polls.",
        ],
    }


def build_host_loop_activation_packet(
    *,
    agent_type: str,
    goal_id: str,
    cli_bin: str = "loopx",
    agent_id: str | None = None,
) -> dict[str, Any]:
    canonical = normalize_agent_type(agent_type)
    commands = _heartbeat_commands(
        goal_id=goal_id,
        agent_type=canonical,
        cli_bin=cli_bin,
        agent_id=agent_id,
    )
    if canonical == "codex-app":
        surface = _codex_app_activation(commands)
    elif canonical == "codex-cli":
        surface = _codex_cli_activation(commands)
    elif canonical == "claude-code":
        surface = _claude_code_activation(commands, cli_bin)
    elif canonical == "cursor-cli":
        surface = _cursor_cli_activation(commands, cli_bin)
    else:
        surface = _manual_activation(commands)
        if canonical == "other-agent":
            surface["entry_command_hint"] = "@loopx <task>, $loopx <task>, or another explicit host command facade"
            surface["host_surface"] = "custom_agent_loop_driver"
    return {
        "schema_version": SCHEMA_VERSION,
        "agent_type": canonical,
        "goal_id": goal_id,
        "agent_id": agent_id,
        "activation_required_after_todo_write": True,
        "status_probe_policy": {
            "check_once_during_onboarding": True,
            "cheap_recheck_on_loopx": "only when activation is missing, unknown, stale, or the agent is newly installed",
            "do_not_recompute_every_loopx_turn": True,
        },
        "commands": commands,
        **surface,
    }
