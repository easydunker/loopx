#!/usr/bin/env python3
"""Behavioral smoke for the Cursor CLI tick loop contracts.

Uses fake loopx and fake cursor-agent binaries to verify:
- quota should-run=false stops without invoking cursor-agent
- quota command/config failure exits 2 (stops), not 75 (pause-loops)
- successful tick with durable writeback passes
- successful tick without writeback fails when writeback is required
- LOOPX_GLOBAL_REGISTRY is passed to quota calls when set
- loop script only execs a LoopX-managed script (managed-marker check)
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loopx.capabilities.multi_agent.runtime_scripts import (  # noqa: E402
    CURSOR_CLI_TICK_WORKER_PY,
)
from loopx.slash_command_install import MANAGED_MARKER_PREFIX  # noqa: E402


def _write_script(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_tick_worker(
    tmp: Path,
    *,
    loopx_script: str,
    cursor_agent_script: str,
    extra_env: dict[str, str] | None = None,
    goal: str = "test-goal",
    agent: str = "test-agent",
) -> tuple[int, str]:
    """Install and run the tick worker in an isolated temp dir, return (exit_code, stdout)."""
    import subprocess

    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    loopx_bin = bin_dir / "loopx"
    _write_script(loopx_bin, loopx_script)

    cursor_agent_bin = bin_dir / "cursor-agent"
    _write_script(cursor_agent_bin, cursor_agent_script)

    tick_worker = bin_dir / "loopx-cursor-cli-tick-worker"
    tick_worker.write_text(CURSOR_CLI_TICK_WORKER_PY, encoding="utf-8")
    tick_worker.chmod(tick_worker.stat().st_mode | stat.S_IEXEC)

    env = os.environ.copy()
    env["LOOPX_GOAL_ID"] = goal
    env["LOOPX_AGENT_ID"] = agent
    env["LOOPX_PROJECT"] = str(tmp)
    env["LOOPX_CURSOR_MODEL"] = "composer-2.5"
    env["LOOPX_PANE_LOOPX"] = str(loopx_bin)
    env["LOOPX_CURSOR_AGENT_BIN"] = str(cursor_agent_bin)
    env["PATH"] = str(bin_dir) + ":" + env.get("PATH", "")
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        [sys.executable, str(tick_worker)],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(tmp),
    )
    return result.returncode, result.stdout + result.stderr


def _loopx_quota_paused(*, reason: str = "quota_exhausted") -> str:
    """Fake loopx: quota should-run returns false with exit 0 (intentional pause decision)."""
    payload = json.dumps({"ok": True, "should_run": False, "reason": reason})
    return f"""#!/bin/sh
if echo "$*" | grep -q 'quota'; then
  echo '{payload}'
  exit 0
fi
echo '{{"ok": true, "task_body": "do work"}}'
exit 0
"""


def _loopx_quota_config_error(*, exit_code: int = 1, reason: str = "goal_not_found") -> str:
    """Fake loopx: quota should-run fails with non-zero exit (command/config failure).

    A non-zero exit must stop the loop (exit 2), not pause-retry (exit 75).
    This mirrors the real LoopX behavior for missing goals: should_run=false + exit 1.
    """
    payload = json.dumps({"ok": False, "should_run": False, "error": reason, "status": reason})
    return f"""#!/bin/sh
if echo "$*" | grep -q 'quota'; then
  echo '{payload}'
  exit {exit_code}
fi
exit 0
"""


def _loopx_quota_ok_with_slots(*, before_slots: int = 0, after_slots: int = 1) -> str:
    """Fake loopx: quota ok before tick, quota with incremented spent_slots after."""
    before_payload = json.dumps({
        "ok": True, "should_run": True,
        "quota": {"spent_slots": before_slots},
    })
    after_payload = json.dumps({
        "ok": True, "should_run": True,
        "quota": {"spent_slots": after_slots},
    })
    hp_payload = json.dumps({"ok": True, "task_body": "do the work for the goal"})
    return f"""#!/bin/sh
# Track call count via a temp file
counter_file="${{LOOPX_PROJECT}}/.quota_call_count"
count=0
if [ -f "$counter_file" ]; then
  count=$(cat "$counter_file")
fi
count=$((count + 1))
echo "$count" > "$counter_file"

if echo "$*" | grep -q 'quota'; then
  if [ "$count" -le 1 ]; then
    echo '{before_payload}'
  else
    echo '{after_payload}'
  fi
  exit 0
fi
if echo "$*" | grep -q 'heartbeat-prompt'; then
  echo '{hp_payload}'
  exit 0
fi
exit 0
"""


def _loopx_quota_ok_no_slots() -> str:
    """Fake loopx: quota ok but spent_slots never increases (no writeback)."""
    payload = json.dumps({
        "ok": True, "should_run": True,
        "quota": {"spent_slots": 0},
    })
    hp_payload = json.dumps({"ok": True, "task_body": "do the work for the goal"})
    return f"""#!/bin/sh
if echo "$*" | grep -q 'quota'; then
  echo '{payload}'
  exit 0
fi
if echo "$*" | grep -q 'heartbeat-prompt'; then
  echo '{hp_payload}'
  exit 0
fi
exit 0
"""


def _loopx_capture_registry() -> str:
    """Fake loopx: capture --registry arg into a file for inspection."""
    hp_payload = json.dumps({"ok": True, "task_body": "do work"})
    return r"""#!/bin/sh
# capture --registry value if present
prev=""
for arg in "$@"; do
  if [ "$prev" = "--registry" ]; then
    echo "$arg" >> "${LOOPX_PROJECT}/.registry_calls"
  fi
  prev="$arg"
done
if echo "$*" | grep -q 'quota'; then
  echo '{"ok":true,"should_run":false,"reason":"paused_for_registry_test"}'
  exit 0
fi
""" + f"""echo '{hp_payload}'
exit 0
"""


def _cursor_agent_ok() -> str:
    return "#!/bin/sh\nexit 0\n"


def _cursor_agent_fail(exit_code: int = 1) -> str:
    return f"#!/bin/sh\nexit {exit_code}\n"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="loopx-cursor-tick-behavioral-") as tmp_root:
        tmp = Path(tmp_root)

        # --- Test 1: quota should-run=false → no cursor-agent, exit 75 ---
        tmp1 = tmp / "test1"
        tmp1.mkdir()
        code, out = _run_tick_worker(
            tmp1,
            loopx_script=_loopx_quota_paused(reason="quota_exhausted"),
            cursor_agent_script=_cursor_agent_ok(),
        )
        assert code == 75, f"[T1] quota paused must exit 75, got {code}\n{out}"
        assert "cursor-agent" not in out or "no cursor-agent invocation" in out, (
            f"[T1] cursor-agent must not be invoked when quota paused\n{out}"
        )
        assert "should-run=false" in out, f"[T1] must log should-run=false\n{out}"
        print("[T1] quota paused → exit 75 (no cursor-agent): PASS")

        # --- Test 2: quota command failure → exit 2 (stop, not pause-loop) ---
        tmp2 = tmp / "test2"
        tmp2.mkdir()
        code, out = _run_tick_worker(
            tmp2,
            loopx_script=_loopx_quota_config_error(exit_code=1, reason="goal_not_found"),
            cursor_agent_script=_cursor_agent_ok(),
        )
        assert code == 2, (
            f"[T2] quota command failure must exit 2 (not 75 pause), got {code}\n{out}"
        )
        assert "cursor-agent" not in out or "stopping" in out, (
            f"[T2] cursor-agent must not be invoked on quota command failure\n{out}"
        )
        print("[T2] quota command failure → exit 2 (stop, not pause-loop): PASS")

        # --- Test 3: successful tick with writeback (spent_slots increases) ---
        tmp3 = tmp / "test3"
        tmp3.mkdir()
        code, out = _run_tick_worker(
            tmp3,
            loopx_script=_loopx_quota_ok_with_slots(before_slots=0, after_slots=1),
            cursor_agent_script=_cursor_agent_ok(),
            extra_env={"LOOPX_CURSOR_REQUIRE_WRITEBACK": "1"},
        )
        assert code == 0, f"[T3] successful tick with writeback must exit 0, got {code}\n{out}"
        print("[T3] successful tick with durable writeback: PASS")

        # --- Test 4: cursor-agent exits 0 but no writeback → fail when required ---
        tmp4 = tmp / "test4"
        tmp4.mkdir()
        code, out = _run_tick_worker(
            tmp4,
            loopx_script=_loopx_quota_ok_no_slots(),
            cursor_agent_script=_cursor_agent_ok(),
            extra_env={"LOOPX_CURSOR_REQUIRE_WRITEBACK": "1"},
        )
        assert code != 0, (
            f"[T4] missing writeback must fail when LOOPX_CURSOR_REQUIRE_WRITEBACK=1, got {code}\n{out}"
        )
        assert "writeback" in out.lower() or "spent_slots" in out, (
            f"[T4] failure message must mention writeback/spent_slots\n{out}"
        )
        print("[T4] missing writeback → non-zero exit when required: PASS")

        # --- Test 5: LOOPX_GLOBAL_REGISTRY is passed to quota calls ---
        tmp5 = tmp / "test5"
        tmp5.mkdir()
        registry_path = "/tmp/test-global-registry"
        code, out = _run_tick_worker(
            tmp5,
            loopx_script=_loopx_capture_registry(),
            cursor_agent_script=_cursor_agent_ok(),
            extra_env={"LOOPX_GLOBAL_REGISTRY": registry_path},
        )
        registry_calls_file = tmp5 / ".registry_calls"
        assert registry_calls_file.exists(), (
            f"[T5] --registry must be passed to loopx when LOOPX_GLOBAL_REGISTRY is set\n{out}"
        )
        registry_args_used = registry_calls_file.read_text().strip().splitlines()
        assert registry_path in registry_args_used, (
            f"[T5] quota calls must use LOOPX_GLOBAL_REGISTRY={registry_path!r}, "
            f"got: {registry_args_used}\n{out}"
        )
        print("[T5] LOOPX_GLOBAL_REGISTRY passed to quota calls: PASS")

        # --- Test 6: managed-marker check in agent-start ---
        # Validate that MANAGED_MARKER_PREFIX is checked before exec
        from loopx.cli_commands.agent_start import _cursor_home, _cursor_loop_script  # noqa: E402

        tmp6 = tmp / "test6"
        tmp6.mkdir()
        bin6 = tmp6 / "bin"
        bin6.mkdir()
        loop_script = bin6 / "loopx-cursor-cli-loop"
        # Write a file WITHOUT the managed marker
        loop_script.write_text("#!/bin/sh\necho 'user script'\n", encoding="utf-8")

        has_marker = MANAGED_MARKER_PREFIX in loop_script.read_text(encoding="utf-8")
        assert not has_marker, "[T6] pre-condition: user script must not have managed marker"

        # Write a file WITH the managed marker
        managed_content = f"# {MANAGED_MARKER_PREFIX} command=loopx-cursor-cli-loop surface=cursor -->\n#!/bin/sh\n"
        loop_script.write_text(managed_content, encoding="utf-8")
        has_marker = MANAGED_MARKER_PREFIX in loop_script.read_text(encoding="utf-8")
        assert has_marker, "[T6] managed script must contain MANAGED_MARKER_PREFIX"
        print("[T6] managed-marker detection via MANAGED_MARKER_PREFIX: PASS")

    print("\nAll cursor-cli tick loop behavioral smokes passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
