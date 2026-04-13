"""Thin wrappers around `docker` invoked on the remote host via ssh."""

from __future__ import annotations

import shlex
import subprocess

from remote_executor import ssh
from remote_executor.config import VolumeMount


def _docker(host: str, argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a `docker ...` command on the remote, with shell-safe quoting."""
    quoted = " ".join(shlex.quote(a) for a in ["docker", *argv])
    return ssh.run_shell(host, quoted, check=check)


def image_exists(host: str, tag: str) -> bool:
    result = _docker(host, ["image", "inspect", tag], check=False)
    return result.returncode == 0


def container_exists(host: str, name: str) -> bool:
    result = _docker(
        host,
        ["ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.ID}}"],
        check=False,
    )
    return bool(result.stdout.strip())


def container_running(host: str, name: str) -> bool:
    result = _docker(
        host,
        ["ps", "--filter", f"name=^{name}$", "--format", "{{.ID}}"],
        check=False,
    )
    return bool(result.stdout.strip())


def container_id(host: str, name: str) -> str | None:
    result = _docker(
        host,
        ["ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.ID}}"],
        check=False,
    )
    cid = result.stdout.strip()
    return cid or None


def build_image(
    host: str,
    *,
    tag: str,
    dockerfile: str,
    build_context: str,
    on_line=None,
) -> int:
    """Build an image on the remote. `dockerfile` and `build_context` are
    paths inside the synced workspace on the remote (e.g. `/workspace/Dockerfile`).

    Streams build output via `on_line` if provided.
    """
    quoted = " ".join(
        shlex.quote(a)
        for a in [
            "docker",
            "build",
            "-t",
            tag,
            "-f",
            dockerfile,
            build_context,
        ]
    )
    if on_line is None:
        result = ssh.run_shell(host, quoted, check=False)
        return result.returncode
    import asyncio

    return asyncio.run(ssh.run_stream(host, ["bash", "-lc", quoted], on_line=on_line))


def run_container(
    host: str,
    *,
    name: str,
    image: str,
    workspace_mount_source: str,
    workspace_mount_target: str,
    volumes: list[VolumeMount],
    gpus: str | None,
    memory: str | None,
    shm_size: str | None,
    workdir: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Start a detached container. Returns the container ID."""
    argv: list[str] = [
        "run",
        "-d",
        "--name",
        name,
        "--restart",
        "unless-stopped",
        "-v",
        f"{workspace_mount_source}:{workspace_mount_target}",
    ]
    for vol in volumes:
        argv += ["-v", f"{name}-{vol.name}:{vol.path}"]
    if gpus:
        argv += ["--gpus", gpus]
    if memory:
        argv += ["--memory", memory]
    if shm_size:
        argv += ["--shm-size", shm_size]
    if workdir:
        argv += ["-w", workdir]
    for k, v in (env or {}).items():
        argv += ["-e", f"{k}={v}"]
    argv += [image, "sleep", "infinity"]

    result = _docker(host, argv, check=True)
    return result.stdout.strip()


def stop_container(host: str, name: str) -> None:
    _docker(host, ["stop", name], check=False)


def rm_container(host: str, name: str, *, force: bool = True) -> None:
    argv = ["rm"]
    if force:
        argv.append("-f")
    argv.append(name)
    _docker(host, argv, check=False)


def exec_in_container(
    host: str,
    name: str,
    *,
    command: str,
    workdir: str | None = None,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """One-shot exec, capturing output. Use `exec_argv()` for streaming."""
    argv = exec_argv(name, command=command, workdir=workdir, env=env)
    return ssh.run_shell(host, " ".join(shlex.quote(a) for a in argv), check=check)


def exec_argv(
    name: str,
    *,
    command: str,
    workdir: str | None = None,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Build the `docker exec ...` argv (without the leading `ssh host`).

    Used by the MCP server's streaming `remote_bash` tool.
    """
    argv: list[str] = ["docker", "exec", "-i"]
    if workdir:
        argv += ["-w", workdir]
    for k, v in (env or {}).items():
        argv += ["-e", f"{k}={v}"]
    argv += [name, "bash", "-lc", command]
    return argv


def cp_from_container(host: str, name: str, src: str, dest_on_remote: str) -> None:
    """`docker cp <name>:<src> <dest_on_remote>`. Used by `pull`."""
    _docker(host, ["cp", f"{name}:{src}", dest_on_remote], check=True)
