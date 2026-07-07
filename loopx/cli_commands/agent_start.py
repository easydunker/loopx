from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..host_loop_activation import build_agent_type_catalog, build_host_loop_activation_packet


PrintPayload = Callable[[dict[str, Any], str, Callable[[dict[str, Any]], str]], None]
FormatSelector = Callable[..., str]

_CURSOR_TICK_WORKER = Path.home() / ".cursor" / "bin" / "loopx-cursor-cli-tick-worker"
_CURSOR_LOOP_SCRIPT = Path.home() / ".cursor" / "bin" / "loopx-cursor-cli-loop"
_DEFAULT_CURSOR_MODEL = "composer-2.5"


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
        help="Seconds between ticks for cursor-cli loop (default: 10).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the command that would be run without executing it.",
    )


def _resolve_agent_type(raw: str) -> str:
    catalog = build_agent_type_catalog()
    for item in catalog["canonical_agent_types"]:
        accepted = item.get("accepted_inputs", [item["agent_type"]])
        if raw in accepted or raw == item["agent_type"]:
            return item["agent_type"]
    return raw


def _render_agent_start_markdown(payload: dict[str, Any]) -> str:
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


def handle_agent_start_command(
    args: argparse.Namespace,
    *,
    output_format: FormatSelector,
    print_payload: PrintPayload,
) -> int | None:
    if args.command != "agent-start":
        return None

    canonical = _resolve_agent_type(args.agent_type)
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
    output_format: FormatSelector,
    print_payload: PrintPayload,
) -> int:
    model = (
        args.model
        or os.environ.get("LOOPX_CURSOR_MODEL", "").strip()
        or _DEFAULT_CURSOR_MODEL
    )

    if not _CURSOR_LOOP_SCRIPT.exists():
        payload: dict[str, Any] = {
            "ok": False,
            "agent_type": canonical,
            "goal_id": goal_id,
            "agent_id": agent_id,
            "project": project,
            "action": "surface_not_installed",
            "error": (
                f"Loop script not found at {_CURSOR_LOOP_SCRIPT}. "
                "Run: loopx slash-commands --install --surface cursor"
            ),
            "instructions": [
                "Install the cursor surface first:",
                "```bash",
                "loopx slash-commands --install --surface cursor",
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
    if args.max_ticks:
        env["LOOPX_CURSOR_MAX_TICKS"] = str(args.max_ticks)
    if args.tick_interval:
        env["LOOPX_CURSOR_TICK_INTERVAL"] = str(args.tick_interval)

    cmd_display = (
        f"LOOPX_GOAL_ID={goal_id} "
        f"LOOPX_AGENT_ID={agent_id} "
        f"LOOPX_PROJECT={project} "
        f"LOOPX_CURSOR_MODEL={model} "
        + (f"LOOPX_CURSOR_MAX_TICKS={args.max_ticks} " if args.max_ticks else "")
        + str(_CURSOR_LOOP_SCRIPT)
    )

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
    os.execve(str(_CURSOR_LOOP_SCRIPT), [str(_CURSOR_LOOP_SCRIPT)], env)
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
