"""Project setup: `remote-executor init`.

Writes everything a project needs in one shot:
- `.remote-executor.toml` — a single starter profile
- `.mcp.json` at the project root — the canonical Claude Code project-scoped
  MCP server registration
- `.claude/skills/remote-execution/SKILL.md` — a self-contained skill that
  teaches Claude when to use `remote_bash`. Doesn't touch the project's
  existing CLAUDE.md.

One command, no manual stitching required.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt

from remote_executor import __version__
from remote_executor.config import (
    CONFIG_FILENAME,
    McpConfig,
    MetaSection,
    Profile,
    ProjectConfig,
    ProjectSection,
    write,
)

console = Console(stderr=True)

SKILL_NAME = "remote-execution"


def run_init(
    host: str | None = None,
    backend: str | None = None,
    gpu: str | None = None,
    project_dir: Path | None = None,
) -> None:
    project_dir = (project_dir or Path.cwd()).resolve()

    if (project_dir / CONFIG_FILENAME).exists():
        if not Confirm.ask(f"{CONFIG_FILENAME} already exists. Overwrite?", default=False):
            raise typer.Abort()

    # --- Dockerfile detection ---
    dockerfile = _detect_dockerfile(project_dir)

    # --- project name ---
    name = project_dir.name.lower().replace(" ", "-").replace("_", "-")

    # --- backend selection (prompt if not supplied) ---
    if backend is None:
        backend = Prompt.ask(
            "Which backend?",
            choices=["modal", "ssh-docker"],
            default="modal",
        )

    # --- build the starter profile ---
    if backend == "ssh-docker":
        if not host:
            host = _prompt_for_host()
        profile_name = host
        profile = Profile(
            backend="ssh-docker",
            host_alias=host,
            gpus=gpu or "all",
            sync_ignore=_seed_ignores(project_dir),
        )
    elif backend == "modal":
        if not gpu:
            gpu = Prompt.ask(
                "Modal GPU type",
                choices=["T4", "L4", "L40S", "A10G", "A100", "H100", "H200"],
                default="T4",
            )
        profile_name = f"modal-{gpu.lower()}"
        profile = Profile(backend="modal", gpu=gpu)
    else:
        raise typer.BadParameter(f"Unknown backend: {backend}. Use 'ssh-docker' or 'modal'.")

    cfg = ProjectConfig(
        meta=MetaSection(tool_version=__version__),
        project=ProjectSection(
            name=name, dockerfile=dockerfile, default_profile=profile_name
        ),
        profiles={profile_name: profile},
        mcp=McpConfig(),
    )

    toml_path = write(project_dir, cfg)
    console.print(f"[green]✓[/] {toml_path.relative_to(project_dir)} (profile: {profile_name})")

    _write_mcp_json(project_dir, profile_name)
    _write_skill(project_dir, cfg, profile_name, profile)

    console.print(
        f"\n[bold green]Init complete.[/] Next:\n"
        f"  [bold]remote-executor doctor[/]\n"
        f"  [bold]remote-executor up[/]\n"
        f"  [bold]claude[/]  (launch Claude Code in this directory)"
    )


def _detect_dockerfile(project_dir: Path) -> str:
    candidates = list(project_dir.glob("Dockerfile*"))
    if len(candidates) == 1:
        return candidates[0].name
    if len(candidates) > 1:
        names = [c.name for c in candidates]
        return Prompt.ask("Which Dockerfile?", default=names[0], choices=names)
    console.print("[yellow]No Dockerfile found in project root.[/]")
    return Prompt.ask("Dockerfile path (relative)", default="Dockerfile")


def _prompt_for_host() -> str:
    available = _discover_ssh_hosts()
    if available:
        console.print("[dim]Available SSH hosts:[/]", ", ".join(available[:20]))
    return Prompt.ask("SSH host alias", default=available[0] if available else None)


def _discover_ssh_hosts() -> list[str]:
    ssh_config = Path.home() / ".ssh" / "config"
    if not ssh_config.exists():
        return []
    hosts: list[str] = []
    for line in ssh_config.read_text().splitlines():
        line = line.strip()
        if line.lower().startswith("host ") and "*" not in line and "?" not in line:
            for h in line.split()[1:]:
                if h not in ("*",):
                    hosts.append(h)
    return hosts


def _seed_ignores(project_dir: Path) -> list[str]:
    ignores: list[str] = []
    gitignore = project_dir / ".gitignore"
    if gitignore.exists():
        for line in gitignore.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                ignores.append(line)

    defaults = [".git/", ".venv/", "__pycache__/", "*.pyc", ".DS_Store", ".idea/", ".vscode/"]
    for d in defaults:
        if d not in ignores:
            ignores.append(d)
    return ignores


def _write_mcp_json(project_dir: Path, profile_name: str) -> None:
    """Write `.mcp.json` at the project root — Claude Code's canonical location
    for project-scoped MCP server registration (committed with the project)."""
    mcp_path = project_dir / ".mcp.json"

    stanza = {
        "mcpServers": {
            "remote-executor": {
                "command": "remote-executor",
                "args": ["mcp"],
                "env": {
                    "REMOTE_EXECUTOR_PROFILE": profile_name,
                },
            },
        },
    }

    if mcp_path.exists():
        try:
            existing = json.loads(mcp_path.read_text())
        except json.JSONDecodeError:
            existing = {}

        existing_servers = existing.get("mcpServers", {})
        if "remote-executor" in existing_servers:
            console.print(f"[dim]✓[/] {mcp_path.name} already has remote-executor (updating)")
        existing_servers["remote-executor"] = stanza["mcpServers"]["remote-executor"]
        existing["mcpServers"] = existing_servers
        mcp_path.write_text(json.dumps(existing, indent=2) + "\n")
    else:
        mcp_path.write_text(json.dumps(stanza, indent=2) + "\n")

    console.print(f"[green]✓[/] {mcp_path.name} (MCP server registered)")


def _write_skill(
    project_dir: Path, cfg: ProjectConfig, profile_name: str, profile: Profile
) -> None:
    """Write a self-contained skill at .claude/skills/remote-execution/SKILL.md.
    Doesn't touch CLAUDE.md — safe to re-run on projects with existing docs."""
    skill_dir = project_dir / ".claude" / "skills" / SKILL_NAME
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"

    if profile.backend == "ssh-docker":
        backend_desc = f"SSH+Docker on `{profile.host_alias}`"
    else:
        backend_desc = f"Modal sandbox (gpu={profile.gpu})"

    content = f"""---
name: {SKILL_NAME}
description: Use when running commands that need the project's runtime environment (GPU, CUDA, ffmpeg/NVENC, uv/python entry points, tests). This project runs on a remote environment via the remote-executor MCP server — local Bash will fail for anything that touches the toolchain.
---

# Remote execution

This project runs on **{backend_desc}** (active profile: `{profile_name}`). The local machine does not have the architecture this project needs, so shell commands that touch the toolchain must run on the remote environment.

## Which tool to use

**Use `mcp__remote-executor__remote_bash`** for any command that needs the remote environment:
- Running `uv`, `python`, or the project's entry points
- `ffmpeg` / `ffprobe` / NVENC
- `nvidia-smi` and other CUDA tools
- Tests that exercise the project's runtime dependencies
- Anything that imports GPU-only packages (PyNvVideoCodec, torch with CUDA, etc.)

**Local `Bash` is still fine** for things that don't need the remote environment: `git`, reading small files, basic filesystem operations. When in doubt, prefer `remote_bash`.

## How the workspace sync works

- The local project directory is mirrored to `{cfg.project.workdir}` inside the remote environment.
- File edits via `Read` / `Edit` / `Write` are visible to the next `remote_bash` call without a rebuild.
- Changes to `Dockerfile` or system dependencies require `remote-executor rebuild` — ask the user to run it.
- To retrieve files from environment-local volumes (e.g. output directories that aren't part of the sync), use `mcp__remote-executor__pull`.

## Switching profiles

This project may define multiple profiles in `.remote-executor.toml` (e.g. `modal-t4`, `modal-l40s`, an ssh-docker host). To switch, ask the user to run:

```
remote-executor down && remote-executor up --profile <name>
```

and restart the Claude Code session so the MCP server picks up the new `REMOTE_EXECUTOR_PROFILE` env var.
"""

    skill_path.write_text(content)
    console.print(f"[green]✓[/] {skill_path.relative_to(project_dir)} (skill for assistant)")
