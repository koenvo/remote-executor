"""CLI entry point: `remote-executor <subcommand>`."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(
    name="remote-executor",
    help="Run Claude Code commands inside a remote execution environment (SSH+Docker or Modal).",
    no_args_is_help=True,
)


def _get_executor(project_dir: Path | None = None, profile: str | None = None):
    from remote_executor.backends import create_executor
    from remote_executor.config import load

    project_dir = (project_dir or Path.cwd()).resolve()
    cfg = load(project_dir)
    return create_executor(project_dir, cfg, profile_name=profile)


ProfileOption = typer.Option(
    None,
    "--profile",
    "-p",
    help="Profile name (defaults to [project].default_profile in .remote-executor.toml)",
)
DirOption = typer.Option(None, "--dir", "-d", help="Project directory (default: cwd)")


@app.command()
def init(
    host: Optional[str] = typer.Option(None, "--host", "-h", help="SSH host alias (ssh-docker backend)"),
    backend: Optional[str] = typer.Option(None, "--backend", "-b", help="Backend: modal or ssh-docker (prompted if omitted)"),
    gpu: Optional[str] = typer.Option(None, "--gpu", "-g", help="GPU type (Modal) or --gpus value (ssh-docker)"),
    project_dir: Optional[Path] = DirOption,
) -> None:
    """Interactive project setup. Writes .remote-executor.toml, .mcp.json, and a Claude Code skill."""
    from remote_executor.init_cmd import run_init

    run_init(host=host, backend=backend, gpu=gpu, project_dir=project_dir)


@app.command()
def up(
    profile: Optional[str] = ProfileOption,
    project_dir: Optional[Path] = DirOption,
) -> None:
    """Start the remote environment and file sync."""
    _get_executor(project_dir, profile).up()


@app.command()
def down(
    profile: Optional[str] = ProfileOption,
    project_dir: Optional[Path] = DirOption,
) -> None:
    """Stop the environment and terminate sync."""
    _get_executor(project_dir, profile).down()


@app.command()
def rebuild(
    profile: Optional[str] = ProfileOption,
    project_dir: Optional[Path] = DirOption,
) -> None:
    """Rebuild the image and recreate the environment."""
    _get_executor(project_dir, profile).rebuild()


@app.command()
def doctor(
    profile: Optional[str] = ProfileOption,
    project_dir: Optional[Path] = DirOption,
) -> None:
    """Run diagnostic checks for the configured backend."""
    ok = _get_executor(project_dir, profile).doctor()
    if not ok:
        raise typer.Exit(1)


@app.command()
def pull(
    src: str = typer.Argument(help="Path inside the environment"),
    dest: str = typer.Argument(help="Local destination path"),
    profile: Optional[str] = ProfileOption,
    project_dir: Optional[Path] = DirOption,
) -> None:
    """Pull a file from the environment to the local machine."""
    local = _get_executor(project_dir, profile).pull_file(src, dest)
    typer.echo(f"Pulled to {local}")


@app.command()
def profiles(
    project_dir: Optional[Path] = DirOption,
) -> None:
    """List all profiles defined in .remote-executor.toml."""
    from remote_executor.config import load
    from rich.console import Console
    from rich.table import Table

    cfg = load((project_dir or Path.cwd()).resolve())
    table = Table(title=f"Profiles in {cfg.project.name}")
    table.add_column("Name", style="bold")
    table.add_column("Backend")
    table.add_column("Details", style="dim")
    table.add_column("Default")

    for name, p in cfg.profiles.items():
        if p.backend == "ssh-docker":
            details = f"host={p.host_alias} gpus={p.gpus or '-'}"
        elif p.backend == "modal":
            details = f"gpu={p.gpu} timeout={p.timeout_minutes}min"
        else:
            details = ""
        is_default = "*" if name == cfg.project.default_profile else ""
        table.add_row(name, p.backend, details, is_default)

    Console().print(table)


@app.command()
def version(
    project_dir: Optional[Path] = DirOption,
) -> None:
    """Show the installed tool version and, if a project is configured, the
    version that wrote its .remote-executor.toml."""
    from remote_executor import __version__
    from remote_executor.config import load

    typer.echo(f"remote-executor {__version__}")

    target = (project_dir or Path.cwd()).resolve()
    try:
        cfg = load(target, check_version=False)
    except FileNotFoundError:
        return

    stored = cfg.meta.tool_version if cfg.meta else None
    if stored is None:
        typer.echo(f"project at {target}: (no version recorded — pre-0.1.0 config)")
    elif stored == __version__:
        typer.echo(f"project at {target}: {stored} [match]")
    else:
        typer.echo(f"project at {target}: {stored} [outdated — re-run `remote-executor init`]")


@app.command()
def mcp() -> None:
    """Start the MCP stdio server (used by Claude Code)."""
    from remote_executor.mcp_server.server import serve

    serve()
