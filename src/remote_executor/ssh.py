"""SSH command builder + ControlMaster lifecycle.

We deliberately shell out to the user's `ssh` binary instead of using
asyncssh/paramiko so we inherit ProxyJump, agent forwarding, hardware keys,
and `Include` directives from `~/.ssh/config` for free.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

from remote_executor.paths import mux_socket

CONTROL_PERSIST = "10m"


def ssh_cmd(host: str, remote_argv: list[str] | None = None) -> list[str]:
    """Build an `ssh` argv that uses our sidecar-owned ControlMaster socket.

    With `ControlMaster=auto`, the first invocation creates the mux socket
    transparently and subsequent invocations reuse it.
    """
    sock = mux_socket(host)
    base = [
        "ssh",
        "-o",
        f"ControlPath={sock}",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPersist={CONTROL_PERSIST}",
        host,
    ]
    if remote_argv:
        base.extend(remote_argv)
    return base


def ensure_mux(host: str) -> None:
    """Start the ControlMaster connection if it isn't running."""
    sock = mux_socket(host)
    check = subprocess.run(
        ["ssh", "-o", f"ControlPath={sock}", "-O", "check", host],
        capture_output=True,
        text=True,
    )
    if check.returncode == 0:
        return

    sock.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ssh",
            "-M",
            "-N",
            "-f",
            "-o",
            f"ControlPath={sock}",
            "-o",
            "ControlMaster=yes",
            "-o",
            f"ControlPersist={CONTROL_PERSIST}",
            host,
        ],
        check=True,
    )


def exit_mux(host: str) -> None:
    """Tear down the ControlMaster connection if it's running."""
    sock = mux_socket(host)
    if not sock.exists():
        return
    subprocess.run(
        ["ssh", "-o", f"ControlPath={sock}", "-O", "exit", host],
        capture_output=True,
    )


def mux_alive(host: str) -> bool:
    sock = mux_socket(host)
    if not sock.exists():
        return False
    result = subprocess.run(
        ["ssh", "-o", f"ControlPath={sock}", "-O", "check", host],
        capture_output=True,
    )
    return result.returncode == 0


def resolve_config(host: str) -> str:
    """Return `ssh -G host` output (resolved effective config)."""
    result = subprocess.run(
        ["ssh", "-G", host],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def run(
    host: str,
    remote_argv: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    text: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Synchronous run-on-remote, returning a CompletedProcess."""
    return subprocess.run(
        ssh_cmd(host, remote_argv),
        check=check,
        capture_output=capture,
        text=text,
        timeout=timeout,
    )


def run_shell(host: str, command: str, **kwargs) -> subprocess.CompletedProcess:
    """Run a shell command line on the remote (single string, not argv).

    SSH joins argv with spaces and sends to the remote login shell, which
    re-parses everything. Without an extra layer of quoting, `bash -lc <cmd>`
    would be re-split by the remote shell — e.g. `docker image inspect foo`
    would become separate arguments and `bash -c docker` would just run the
    `docker` help (exit 0). Wrap the command in shlex.quote so the remote
    shell passes it to bash as a single argument.
    """
    return run(host, ["bash", "-lc", shlex.quote(command)], **kwargs)


async def run_stream(
    host: str,
    remote_argv: list[str],
    *,
    on_line: Callable[[str, str], Awaitable[None]] | None = None,
    timeout: float | None = None,
) -> int:
    """Run a remote command, line-streaming stdout+stderr to `on_line`.

    `on_line(stream, line)` is awaited per line where stream ∈ {"stdout","stderr"}.
    Returns the remote process exit code (or 124 on timeout).
    """
    proc = await asyncio.create_subprocess_exec(
        *ssh_cmd(host, remote_argv),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def pump(stream: asyncio.StreamReader, name: str) -> None:
        while True:
            raw = await stream.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if on_line is not None:
                await on_line(name, line)

    assert proc.stdout is not None and proc.stderr is not None
    pumps = asyncio.gather(pump(proc.stdout, "stdout"), pump(proc.stderr, "stderr"))

    try:
        if timeout is not None:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        else:
            await proc.wait()
    except asyncio.TimeoutError:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        await pumps
        return 124

    await pumps
    return proc.returncode if proc.returncode is not None else -1


def quote(argv: list[str]) -> str:
    """Shell-quote an argv into a single string for `bash -lc`."""
    return " ".join(shlex.quote(a) for a in argv)


def remote_path_exists(host: str, path: str) -> bool:
    result = run(host, ["test", "-e", path], check=False)
    return result.returncode == 0


def remote_mkdir(host: str, path: str) -> None:
    run(host, ["mkdir", "-p", path], check=True)


def home_dir(host: str) -> str:
    """Resolve $HOME on the remote."""
    result = run(host, ["printf", "%s", "$HOME"], check=True)
    # Bash expands $HOME inside the printf since ssh runs through a login shell.
    home = result.stdout.strip()
    if not home:
        # Fallback for nonstandard remote shells.
        result = run(host, ["sh", "-c", "echo $HOME"], check=True)
        home = result.stdout.strip()
    if not home or not home.startswith("/"):
        raise RuntimeError(f"Could not resolve $HOME on {host}: got {home!r}")
    return home


__all__ = [
    "ssh_cmd",
    "ensure_mux",
    "exit_mux",
    "mux_alive",
    "resolve_config",
    "run",
    "run_shell",
    "run_stream",
    "quote",
    "remote_path_exists",
    "remote_mkdir",
    "home_dir",
]


# Convenience for callers that need a Path import without re-importing
_ = Path
