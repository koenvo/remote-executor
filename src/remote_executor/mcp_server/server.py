"""MCP stdio server exposing `remote_bash` and `pull` tools.

Backend-agnostic: talks to whichever Executor the config selects.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from remote_executor.backends import create_executor
from remote_executor.config import load
from remote_executor.executor import Executor

mcp = FastMCP(
    "remote-executor",
    instructions=(
        "This MCP server runs commands inside a remote execution environment "
        "(Docker container via SSH, or a Modal sandbox). "
        "Source edits via Read/Edit/Write are live (synced automatically). "
        "Dockerfile or system-dependency changes require the user to run `remote-executor rebuild`."
    ),
)

_executor: Executor | None = None


def _get_executor() -> Executor:
    global _executor
    if _executor is None:
        project_dir = Path.cwd().resolve()
        cfg = load(project_dir)
        profile = os.environ.get("REMOTE_EXECUTOR_PROFILE")
        _executor = create_executor(project_dir, cfg, profile_name=profile)
    return _executor


@mcp.tool()
async def ensure_up() -> str:
    """Ensure the remote execution environment is running.

    Idempotent — free to call before the first `remote_bash` of a session.
    If the sandbox/container is already running, returns immediately.
    Otherwise provisions it (image build, sandbox start, initial workspace
    push). A cold start can take 20–120 seconds depending on backend and
    whether the image is cached.

    Call this proactively at the start of a session if you know you'll need
    the remote environment. Do NOT call `down` — that's user-controlled.
    """
    executor = _get_executor()
    if executor.is_up():
        return "already running"
    try:
        executor.up()
        return "started"
    except Exception as e:
        return f"failed to start: {e}"


@mcp.tool()
async def remote_bash(
    command: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout_sec: int = 600,
    ctx: Context[ServerSession, None] = None,  # type: ignore[assignment]
) -> str:
    """Execute a shell command inside the remote execution environment.

    The workspace at /workspace mirrors the local project directory.
    Source file edits via Read/Edit/Write are visible immediately (no rebuild).

    Args:
        command: Shell command to run (passed to `bash -lc`).
        cwd: Working directory relative to /workspace (e.g. "src" → /workspace/src).
             Defaults to /workspace.
        env: Extra environment variables to set for this command.
        timeout_sec: Max seconds before the command is killed (default 600, max 3600).
    """
    executor = _get_executor()
    project_dir = Path.cwd().resolve()
    cfg = load(project_dir)

    timeout_sec = min(max(timeout_sec, 1), cfg.mcp.max_timeout_sec)

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    start = time.monotonic()

    async def on_line(stream: str, line: str) -> None:
        if stream == "stdout":
            stdout_lines.append(line)
        else:
            stderr_lines.append(line)
        if ctx is not None:
            total_lines = len(stdout_lines) + len(stderr_lines)
            try:
                await ctx.report_progress(
                    progress=total_lines,
                    total=0,
                    message=line[:1000],
                )
            except Exception:
                pass

    exit_code = await executor.exec_command(
        command,
        cwd=cwd,
        env=env,
        timeout=float(timeout_sec),
        on_line=on_line,
    )

    elapsed = time.monotonic() - start
    timed_out = exit_code == 124

    max_tail = 200
    stdout_tail = stdout_lines[-max_tail:] if len(stdout_lines) > max_tail else stdout_lines
    stderr_tail = stderr_lines[-max_tail:] if len(stderr_lines) > max_tail else stderr_lines

    parts: list[str] = []
    if stdout_tail:
        parts.append("STDOUT:\n" + "\n".join(stdout_tail))
    if stderr_tail:
        parts.append("STDERR:\n" + "\n".join(stderr_tail))

    truncated = len(stdout_lines) > max_tail or len(stderr_lines) > max_tail
    summary = f"exit_code={exit_code} duration={elapsed:.1f}s"
    if truncated:
        summary += f" truncated=true (showed last {max_tail} lines of {len(stdout_lines)+len(stderr_lines)} total)"
    if timed_out:
        summary += " TIMED_OUT"
    parts.append(summary)

    return "\n\n".join(parts)


@mcp.tool()
async def sync_down(path: str) -> str:
    """Pull a file or directory from the remote workspace back to the local
    project directory at the matching path.

    Use this after running a command that produces output files you need to
    inspect locally (segments, logs, generated configs, exported models, a
    modified `pyproject.toml` after `uv add`, etc.). Local edits flow to the
    remote automatically — only pull-back requires this explicit call.

    `path` is relative to the workspace root (e.g. "out/seg_0002.ts" or
    "out/" to grab the whole directory). Do NOT call this for files that
    weren't created or modified by your command — it's a bandwidth cost.

    Args:
        path: Path inside /workspace (file or directory). Relative to workdir.
    """
    executor = _get_executor()
    try:
        count, size = executor.sync_down(path)
        if count == 0:
            return f"{path}: nothing pulled (path missing or all files ignored)"
        return f"pulled {count} file(s), {_format_bytes(size)} from {path}"
    except Exception as e:
        return f"sync_down failed: {e}"


def _format_bytes(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


@mcp.tool()
async def pull(
    src: str,
    dest: str,
) -> str:
    """Pull a file from the remote environment to the local machine.

    Use this when you need a file that lives on a container-local volume
    (not in the synced /workspace). For files inside /workspace, just use
    the local Read tool instead.

    Args:
        src: Path inside the environment (absolute, or relative to /workspace).
        dest: Local destination path (relative to project dir, or absolute).
    """
    executor = _get_executor()
    try:
        local_path = executor.pull_file(src, dest)
        return f"Pulled to {local_path}"
    except Exception as e:
        return f"Pull failed: {e}"


def serve() -> None:
    """Entry point called by `remote-executor mcp`."""
    mcp.run(transport="stdio")
