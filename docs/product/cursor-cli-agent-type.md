# Cursor CLI Agent Type

`cursor-cli` is the LoopX agent type for Cursor CLI (`cursor-agent`).

The contract is intentionally narrow:

- Cursor CLI does not provide a native LoopX loop runtime.
- LoopX installs an external tick driver with `loopx slash-commands --install --surface cursor`.
- The driver starts each tick with `loopx quota should-run`.
- When quota allows work, the driver builds a thin `heartbeat-prompt` task body and invokes `cursor-agent -p <task_body>`.
- Writeback remains a LoopX control-plane responsibility through todo, evidence, and quota commands.

The default Cursor invocation is conservative. The LoopX-owned driver does not
pass auto-approval flags such as `--force`, `--yolo`, or `--approve-mcps`. Use
direct Cursor CLI outside `loopx agent-start` until LoopX exposes an explicit
owner-authorized pass-through contract for the project boundary.

Use `CURSOR_HOME` when Cursor user files should be installed outside
`~/.cursor`; `loopx agent-start --agent-type cursor-cli` resolves the same home
when locating the LoopX-owned loop script.

For `cursor-agent -p` headless use, install rules at the project level so
`cursor-agent` loads them from `.cursor/rules/`:

```bash
loopx slash-commands --install --surface cursor --cursor-home $(pwd)/.cursor
```
