from __future__ import annotations

import argparse
import os
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..paths import global_registry_path, DEFAULT_RUNTIME_ROOT

from ..host_loop_activation import (
    AgentTypeError,
    build_agent_type_catalog,
    build_host_loop_activation_packet,
    normalize_agent_type,
    render_agent_type_catalog_markdown,
)
from ..slash_command_install import MANAGED_MARKER_PREFIX


PrintPayload = Callable[[dict[str, Any], str, Callable[[dict[str, Any]], str]], None]
FormatSelector = Callable[..., str]

_DEFAULT_CURSOR_MODEL = "composer-2.5"


def _cursor_home(value: str | None = None) -> Path:
    raw = value or os.environ.get("CURSOR_HOME") or str(Path.home() / ".cursor")
    return Path(raw).expanduser()


def _cursor_loop_script(cursor_home: str | None = None) -> Path:
    return _cursor_home(cursor_home) / "bin" / "loopx-cursor-cli-loop"


def register_agent_start_command(
    subparsers: argparse._SubParsersAction,
    add_subcommand_format: Callable[[argparse.ArgumentParser], None],
) -> None:
    catalog = build_agent_type_catalog()
    all_types = [item["agent_type"] for item in catalog["canonical_agent_types"]]
    accepted = [
        inp
        for item in catalog["canonical_agent_types"]
        for inp in item.get("accepted_inputs", [item["agent_type"]])
    ]

    parser = subparsers.add_parser(
        "agent-start",
        help=(
            "Start a LoopX agent loop for a goal. "
            "cursor-cli: starts the loop directly. "
            "Other agent types: print activation instructions."
        ),
    )
    add_subcommand_format(parser)
    parser.add_argument(
        "--agent-type",
        required=True,
        metavar="AGENT_TYPE",
        help=(
            f"Agent type to start. Canonical types: {', '.join(all_types)}. "
            f"Also accepts aliases: {', '.join(a for a in accepted if a not in all_types)}."
        ),
    )
    parser.add_argument("--goal-id", required=True, help="LoopX goal ID.")
    parser.add_argument(
        "--agent-id",
        help="Agent ID. Defaults to '<agent-type>-<goal-id[:8]>' for cursor-cli.",
    )
    parser.add_argument(
        "--project",
        default=".",
        help="Project root directory. Defaults to current directory.",
    )
    parser.add_argument(
        "--model",
        help=(
            f"Model for cursor-cli (default: {_DEFAULT_CURSOR_MODEL}). "
            "Overrides LOOPX_CURSOR_MODEL. Run cursor-agent --list-models for options."
        ),
    )
    parser.add_argument(
        "--max-ticks",
        type=int,
        metavar="N",
        help="Stop the cursor-cli loop after N ticks (default: unlimited).",
    )
    parser.add_argument(
        "--tick-interval",
        type=int,
        metavar="SECONDS",
        help="Seconds between ticks for cursor-cli loop (default: 60).",
    )
    parser.add_argument(
        "--cursor-home",
        metavar="DIR",
        help=(
            "Cursor home directory for cursor-cli. Defaults to CURSOR_HOME env var or ~/.cursor. "
            "Use --cursor-home $(pwd)/.cursor to match a project-level install "
            "(loopx slash-commands --install --surface cursor --cursor-home $(pwd)/.cursor)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the command that would be run without executing it.",
    )


def _render_agent_start_markdown(payload: dict[str, Any]) -> str:
    if not payload.get("ok") and isinstance(payload.get("agent_type_catalog"), dict):
        return render_agent_type_catalog_markdown(payload)
    lines = ["# loopx agent-start", ""]
    lines.append(f"- agent_type: `{payload.get('agent_type')}`")
    lines.append(f"- goal_id: `{payload.get('goal_id')}`")
    lines.append(f"- agent_id: `{payload.get('agent_id')}`")
    lines.append(f"- project: `{payload.get('project')}`")
    lines.append(f"- action: `{payload.get('action')}`")
    if payload.get("command"):
        lines += ["", "```bash", payload["command"], "```"]
    if payload.get("instructions"):
        lines += ["", *payload["instructions"]]
    if payload.get("error"):
        lines += ["", f"**Error:** {payload['error']}"]
    return "\n".join(lines) + "\n"


def _resolve_global_registry(registry_path: Path | None) -> str:
    """Return the path to use for LOOPX_GLOBAL_REGISTRY.

    Precedence: explicit --registry arg > LOOPX_GLOBAL_REGISTRY env > LOOPX_REGISTRY env >
    system global registry default.
    """
    if registry_path is not None:
        return str(registry_path)
    from_env = (
        os.environ.get("LOOPX_GLOBAL_REGISTRY", "").strip()
        or os.environ.get("LOOPX_REGISTRY", "").strip()
    )
    if from_env:
        return from_env
    default = global_registry_path(DEFAULT_RUNTIME_ROOT)
    if default.exists():
        return str(default)
    return ""


def handle_agent_start_command(
    args: argparse.Namespace,
    *,
    registry_path: Path | None = None,
    output_format: FormatSelector,
    print_payload: PrintPayload,
) -> int | None:
    if args.command != "agent-start":
        return None

    try:
        canonical = normalize_agent_type(args.agent_type)
    except AgentTypeError as exc:
        print_payload(exc.to_payload(), output_format(args), _render_agent_start_markdown)
        return 2

    goal_id = args.goal_id
    project = str(Path(args.project).resolve())
    agent_id = args.agent_id or f"{canonical}-{goal_id[:8]}"

    if canonical == "cursor-cli":
        return _start_cursor_cli(
            args,
            canonical=canonical,
            goal_id=goal_id,
            agent_id=agent_id,
            project=project,
            cursor_home=getattr(args, "cursor_home", None),
            global_registry=_resolve_global_registry(registry_path),
            output_format=output_format,
            print_payload=print_payload,
        )

    activation = build_host_loop_activation_packet(
        agent_type=canonical,
        goal_id=goal_id,
        agent_id=agent_id,
    )
    payload = _activation_instructions_payload(
        canonical=canonical,
        goal_id=goal_id,
        agent_id=agent_id,
        project=project,
        activation=activation,
    )
    print_payload(payload, output_format(args), _render_agent_start_markdown)
    return 0


def _start_cursor_cli(
    args: argparse.Namespace,
    *,
    canonical: str,
    goal_id: str,
    agent_id: str,
    project: str,
    cursor_home: str | None = None,
    global_registry: str = "",
    output_format: FormatSelector,
    print_payload: PrintPayload,
) -> int:
    model = (
        args.model
        or os.environ.get("LOOPX_CURSOR_MODEL", "").strip()
        or _DEFAULT_CURSOR_MODEL
    )

    cursor_loop_script = _cursor_loop_script(cursor_home)
    install_hint = "loopx slash-commands --install --surface cursor"
    if cursor_home:
        install_hint += f" --cursor-home {shlex.quote(cursor_home)}"

    if not cursor_loop_script.exists():
        payload: dict[str, Any] = {
            "ok": False,
            "agent_type": canonical,
            "goal_id": goal_id,
            "agent_id": agent_id,
            "project": project,
            "action": "surface_not_installed",
            "error": (
                f"Loop script not found at {cursor_loop_script}. "
                f"Run: {install_hint}"
            ),
            "instructions": [
                "Install the cursor surface first:",
                "```bash",
                install_hint,
                "```",
            ],
        }
        print_payload(payload, output_format(args), _render_agent_start_markdown)
        return 1

    try:
        script_content = cursor_loop_script.read_text(encoding="utf-8")
    except OSError:
        script_content = ""
    if MANAGED_MARKER_PREFIX not in script_content:
        payload = {
            "ok": False,
            "agent_type": canonical,
            "goal_id": goal_id,
            "agent_id": agent_id,
            "project": project,
            "action": "surface_not_managed",
            "error": (
                f"Loop script at {cursor_loop_script} exists but is not a LoopX-managed file. "
                f"To install the LoopX-managed script, run: {install_hint}"
            ),
            "instructions": [
                "The file at the cursor loop path is not LoopX-managed and will not be executed.",
                "Install the LoopX cursor surface to create the managed script:",
                "```bash",
                install_hint,
                "```",
            ],
        }
        print_payload(payload, output_format(args), _render_agent_start_markdown)
        return 1

    env = os.environ.copy()
    env["LOOPX_GOAL_ID"] = goal_id
    env["LOOPX_AGENT_ID"] = agent_id
    env["LOOPX_PROJECT"] = project
    env["LOOPX_CURSOR_MODEL"] = model
    if global_registry:
        env["LOOPX_GLOBAL_REGISTRY"] = global_registry
    if cursor_home:
        env["CURSOR_HOME"] = str(_cursor_home(cursor_home))
    if args.max_ticks:
        env["LOOPX_CURSOR_MAX_TICKS"] = str(args.max_ticks)
    if args.tick_interval:
        env["LOOPX_CURSOR_TICK_INTERVAL"] = str(args.tick_interval)

    def _env(k: str, v: str) -> str:
        return f"{k}={shlex.quote(v)}"

    cmd_parts = [
        _env("LOOPX_GOAL_ID", goal_id),
        _env("LOOPX_AGENT_ID", agent_id),
        _env("LOOPX_PROJECT", project),
        _env("LOOPX_CURSOR_MODEL", model),
    ]
    if global_registry:
        cmd_parts.append(_env("LOOPX_GLOBAL_REGISTRY", global_registry))
    if cursor_home:
        cmd_parts.append(_env("CURSOR_HOME", str(_cursor_home(cursor_home))))
    if args.max_ticks:
        cmd_parts.append(_env("LOOPX_CURSOR_MAX_TICKS", str(args.max_ticks)))
    if args.tick_interval:
        cmd_parts.append(_env("LOOPX_CURSOR_TICK_INTERVAL", str(args.tick_interval)))
    cmd_parts.append(shlex.quote(str(cursor_loop_script)))
    cmd_display = " ".join(cmd_parts)

    if args.dry_run:
        payload = {
            "ok": True,
            "agent_type": canonical,
            "goal_id": goal_id,
            "agent_id": agent_id,
            "project": project,
            "model": model,
            "action": "dry_run",
            "command": cmd_display,
        }
        print_payload(payload, output_format(args), _render_agent_start_markdown)
        return 0

    print(
        f"\n[loopx agent-start] cursor-cli goal={goal_id} agent={agent_id} model={model}\n",
        flush=True,
    )
    os.execve(str(cursor_loop_script), [str(cursor_loop_script)], env)
    return 0  # unreachable


def _activation_instructions_payload(
    *,
    canonical: str,
    goal_id: str,
    agent_id: str,
    project: str,
    activation: dict[str, Any],
) -> dict[str, Any]:
    host_surface = activation.get("host_surface", "")
    steps = activation.get("activation_steps") or []

    if canonical in ("codex-cli", "codex-app"):
        instructions = [
            "Codex uses the LoopX multi-agent launcher to start the loop.",
            "Run the launcher or use `/goal <task_body>` in the Codex TUI.",
            "",
            "Get the task_body:",
            f"```bash",
            f"loopx --format json heartbeat-prompt --thin --goal-id {goal_id} --agent-id {agent_id}",
            f"```",
            "",
            "Then start the Codex TUI with that task body.",
        ]
    elif canonical == "claude-code":
        instructions = [
            "Claude Code uses its native `/loop` command.",
            "Open Claude Code in your project directory, then run `/loop`.",
            "",
            "The `~/.claude/loop.md` rule (installed via `loopx slash-commands --install --surface claude-code`)",
            "provides the LoopX tick protocol to Claude automatically.",
            "",
            "Get the task_body for context:",
            "```bash",
            f"loopx --format json heartbeat-prompt --thin --goal-id {goal_id} --agent-id {agent_id}",
            "```",
        ]
    else:
        instructions = [f"Activation steps for {canonical}:", *[f"- {s}" for s in steps]]

    return {
        "ok": True,
        "agent_type": canonical,
        "goal_id": goal_id,
        "agent_id": agent_id,
        "project": project,
        "action": "print_instructions",
        "host_surface": host_surface,
        "instructions": instructions,
    }
