"""Interactive project setup: `remote-executor init`.

Writes .remote-executor.toml with a single-profile starter config,
plus .claude/settings.json and a CLAUDE.md snippet.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt

from remote_executor.config import (
    CONFIG_FILENAME,
    McpConfig,
    Profile,
    ProjectConfig,
    ProjectSection,
    write,
)

console = Console(stderr=True)

CLAUDE_MARKER_BEGIN = "<!-- remote-executor:begin -->"
CLAUDE_MARKER_END = "<!-- remote-executor:end -->"


def run_init(
    host: str | None = None,
    backend: str = "ssh-docker",
    gpu: str = "all",
    project_dir: Path | None = None,
) -> None:
    project_dir = (project_dir or Path.cwd()).resolve()

    if (project_dir / CONFIG_FILENAME).exists():
        if not Confirm.ask(f"{CONFIG_FILENAME} already exists. Overwrite?", default=False):
            raise typer.Abort()

    # --- dockerfile ---
    dockerfile = "Dockerfile"
    candidates = list(project_dir.glob("Dockerfile*"))
    if candidates:
        names = [c.name for c in candidates]
        if len(names) == 1:
            dockerfile = names[0]
        else:
            dockerfile = Prompt.ask("Which Dockerfile?", default=names[0], choices=names)
    else:
        dockerfile = Prompt.ask("Dockerfile path (relative)", default="Dockerfile")

    default_name = project_dir.name.lower().replace(" ", "-")
    name = Prompt.ask("Project name", default=default_name)

    # --- single starter profile ---
    if backend == "ssh-docker":
        if not host:
            available = _discover_ssh_hosts()
            if available:
                console.print("[dim]Available SSH hosts:[/]", ", ".join(available[:20]))
            host = Prompt.ask("SSH host alias", default=available[0] if available else None)
        assert host is not None
        profile_name = host
        profile = Profile(
            backend="ssh-docker",
            host_alias=host,
            gpus=gpu if gpu != "none" else None,
            sync_ignore=_seed_ignores(project_dir),
        )
    elif backend == "modal":
        gpu_type = gpu if gpu != "all" else Prompt.ask("Modal GPU type", default="T4")
        profile_name = f"modal-{gpu_type.lower()}"
        profile = Profile(backend="modal", gpu=gpu_type)
    else:
        raise typer.BadParameter(f"Unknown backend: {backend}")

    cfg = ProjectConfig(
        project=ProjectSection(
            name=name, dockerfile=dockerfile, default_profile=profile_name
        ),
        profiles={profile_name: profile},
        mcp=McpConfig(),
    )

    toml_path = write(project_dir, cfg)
    console.print(f"[green]Wrote {toml_path.relative_to(project_dir)}[/] (profile: {profile_name})")

    _write_claude_settings(project_dir)
    _write_claude_md(project_dir, cfg, profile_name, profile)

    console.print(
        f"\n[bold green]Init complete.[/] Next: `remote-executor doctor`, then `remote-executor up`."
    )


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


def _write_claude_settings(project_dir: Path) -> None:
    settings_dir = project_dir / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"

    stanza = {
        "permissions": {"deny": ["Bash"]},
        "mcpServers": {
            "remote-executor": {
                "command": "remote-executor",
                "args": ["mcp"],
            },
        },
    }

    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            existing = {}

        if "permissions" in existing or "mcpServers" in existing:
            console.print(
                f"[yellow]{settings_path.relative_to(project_dir)} already has permissions or mcpServers.[/]"
            )
            console.print("[yellow]Merge the following manually:[/]")
            console.print_json(json.dumps(stanza, indent=2))
            return

        existing.update(stanza)
        settings_path.write_text(json.dumps(existing, indent=2) + "\n")
    else:
        settings_path.write_text(json.dumps(stanza, indent=2) + "\n")


def _write_claude_md(
    project_dir: Path, cfg: ProjectConfig, profile_name: str, profile: Profile
) -> None:
    claude_md = project_dir / "CLAUDE.md"

    if profile.backend == "ssh-docker":
        backend_desc = f"SSH+Docker on `{profile.host_alias}`"
    else:
        backend_desc = f"Modal (gpu={profile.gpu})"

    snippet = f"""{CLAUDE_MARKER_BEGIN}
## Remote Execution

This project runs on {backend_desc} (profile: `{profile_name}`).

1. **No local shell.** The built-in `Bash` tool is denied. Use `mcp__remote-executor__remote_bash` for all commands.
2. **Workspace is mirrored.** The local project directory is synced to `{cfg.project.workdir}` inside the environment. File edits via `Read`/`Edit`/`Write` are live — no rebuild needed.
3. **Dockerfile changes** require an explicit `remote-executor rebuild`.
4. **Switch profiles** by running `remote-executor up --profile <name>` (see `.remote-executor.toml` for available profiles).
{CLAUDE_MARKER_END}"""

    if claude_md.exists():
        content = claude_md.read_text()
        if CLAUDE_MARKER_BEGIN in content:
            pattern = re.escape(CLAUDE_MARKER_BEGIN) + r".*?" + re.escape(CLAUDE_MARKER_END)
            content = re.sub(pattern, snippet, content, flags=re.DOTALL)
            claude_md.write_text(content)
        else:
            claude_md.write_text(content.rstrip() + "\n\n" + snippet + "\n")
    else:
        claude_md.write_text(snippet + "\n")
