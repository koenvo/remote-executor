"""Modal executor: runs commands in a Modal Sandbox with GPU access.

Uses `modal.Image.from_dockerfile()` so the same Dockerfile works across
both SSH+Docker and Modal backends. Workspace files are pushed into the
sandbox via `sandbox.copy_from_local()` before each exec so local edits
are visible without rebuilding.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from rich.console import Console
from rich.table import Table

from remote_executor import state
from remote_executor.config import Profile, ProjectConfig
from remote_executor.executor import Executor

console = Console(stderr=True)

DEFAULT_IGNORE = {
    # VCS
    ".git", ".hg", ".svn",
    # Python
    "__pycache__", ".venv", "venv", "env",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    # Node
    "node_modules",
    # Rust
    "target",
    # Build artifacts
    "dist", "build", ".next", ".nuxt",
    # IDE / OS
    ".idea", ".vscode", ".DS_Store", "Thumbs.db",
    # Caches
    ".cache", ".uv", ".pip",
}


def _import_modal():
    try:
        import modal
        return modal
    except ImportError:
        raise RuntimeError(
            "Modal backend requires the `modal` package. "
            "Install with: uv tool install remote-executor[modal]"
        )


def _should_ignore(path: Path, ignores: set[str]) -> bool:
    for part in path.parts:
        if part in ignores:
            return True
    return False


def _extra_ignores_from_profile(sync_ignore: list[str]) -> set[str]:
    """Extract plain directory/file names from mutagen glob patterns so the
    Modal backend (which uses simple path-component matching) can honor the
    same exclusions the user configured for the mutagen sync."""
    extras: set[str] = set()
    for pattern in sync_ignore:
        # Strip trailing slashes, leading **/  /  , glob markers
        stripped = pattern.strip().rstrip("/")
        while stripped.startswith(("**/", "*/")):
            stripped = stripped.split("/", 1)[1] if "/" in stripped else ""
        if stripped and "*" not in stripped and "/" not in stripped:
            extras.add(stripped)
    return extras


class ModalExecutor(Executor):
    def __init__(
        self,
        project_dir: Path,
        cfg: ProjectConfig,
        profile_name: str,
        profile: Profile,
    ) -> None:
        self._project_dir = project_dir.resolve()
        self._cfg = cfg
        self._profile_name = profile_name
        self._profile = profile
        self._app_name = cfg.container_name(project_dir, profile_name)
        self._state_key = f"modal:{profile_name}"
        self._sandbox = None

    @property
    def workdir(self) -> str:
        return self._cfg.project.workdir

    def _reconnect_sandbox(self):
        if self._sandbox is not None:
            return self._sandbox

        st = state.load(self._state_key)
        if not st.sandbox_id:
            return None

        modal = _import_modal()
        try:
            self._sandbox = modal.Sandbox.from_id(st.sandbox_id)
            return self._sandbox
        except Exception:
            state.clear(self._state_key)
            return None

    async def _reconnect_sandbox_async(self):
        if self._sandbox is not None:
            return self._sandbox

        st = state.load(self._state_key)
        if not st.sandbox_id:
            return None

        modal = _import_modal()
        try:
            self._sandbox = await modal.Sandbox.from_id.aio(st.sandbox_id)
            return self._sandbox
        except Exception:
            state.clear(self._state_key)
            return None

    def _ensure_sandbox(self):
        sb = self._reconnect_sandbox()
        if sb is None:
            raise RuntimeError(
                "No sandbox running. Run `remote-executor up` first."
            )
        return sb

    async def _ensure_sandbox_async(self):
        sb = await self._reconnect_sandbox_async()
        if sb is None:
            raise RuntimeError(
                "No sandbox running. Run `remote-executor up` first."
            )
        return sb

    def _effective_ignores(self) -> set[str]:
        """DEFAULT_IGNORE + any plain dir names extracted from profile.sync_ignore."""
        return DEFAULT_IGNORE | _extra_ignores_from_profile(self._profile.sync_ignore)

    def _push_workspace(self, sb) -> int:
        """Push all project files into the sandbox at /workspace (sync version for up())."""
        ignores = self._effective_ignores()
        count = 0
        for local_file, remote_path in self._walk_files(ignores):
            try:
                sb.filesystem.copy_from_local(str(local_file), remote_path)
                count += 1
            except Exception:
                pass
        return count

    async def _push_workspace_async(self, sb) -> int:
        """Push all project files into the sandbox (async version for exec_command())."""
        ignores = self._effective_ignores()
        count = 0
        for local_file, remote_path in self._walk_files(ignores):
            try:
                await sb.filesystem.copy_from_local.aio(str(local_file), remote_path)
                count += 1
            except Exception:
                pass
        return count

    def _walk_files(self, ignores: set[str]) -> list[tuple[Path, str]]:
        result = []
        for root, dirs, files in os.walk(self._project_dir):
            root_path = Path(root)
            rel_root = root_path.relative_to(self._project_dir)
            dirs[:] = [d for d in dirs if d not in ignores]
            for f in files:
                local_file = root_path / f
                rel_file = rel_root / f
                if _should_ignore(rel_file, ignores):
                    continue
                remote_path = f"{self.workdir}/{rel_file}"
                result.append((local_file, remote_path))
        return result

    def is_up(self) -> bool:
        """True if a sandbox is running and reachable."""
        sb = self._reconnect_sandbox()
        if sb is None:
            return False
        try:
            # poll() returns None if still running, exit code if finished
            return sb.poll() is None
        except Exception:
            return False

    def up(self) -> None:
        if self.is_up():
            console.print(f"[dim]Sandbox already running (id={self._sandbox.object_id})[/]")
            return

        modal = _import_modal()
        modal.enable_output()

        app = modal.App.lookup(self._app_name, create_if_missing=True)

        dockerfile_path = str(self._project_dir / self._cfg.project.dockerfile)
        context_dir = str(self._project_dir / self._cfg.project.build_context)

        console.print(f"[bold]Building Modal image from {self._cfg.project.dockerfile}…[/]")
        image = modal.Image.from_dockerfile(
            dockerfile_path,
            context_dir=context_dir,
            gpu=self._profile.gpu,
            add_python=self._profile.python_version,
        )

        gpu_spec = self._profile.gpu
        console.print(f"[bold]Starting Modal sandbox (gpu={gpu_spec})…[/]")

        self._sandbox = modal.Sandbox.create(
            image=image,
            app=app,
            gpu=gpu_spec,
            timeout=self._profile.timeout_minutes * 60,
            workdir=self.workdir,
            cloud=self._profile.cloud,
            region=self._profile.region,
            env={"TERM": "dumb", "CI": "1", "NO_COLOR": "1"},
        )

        # Push workspace files so /workspace has the latest source
        console.print("[bold]Pushing workspace files…[/]")
        count = self._push_workspace(self._sandbox)
        console.print(f"[dim]Pushed {count} files to {self.workdir}[/]")

        # Persist sandbox ID for MCP server reconnection
        st = state.HostState()
        st.sandbox_id = self._sandbox.object_id
        st.container_name = self._app_name
        st.backend_type = "modal"
        st.project_cwd = str(self._project_dir)
        st.touch()
        state.save(self._state_key, st)

        console.print(f"[bold green]Ready.[/] Modal sandbox running with {gpu_spec}.")
        console.print(f"[dim]Sandbox ID: {self._sandbox.object_id}[/]")

    def down(self) -> None:
        sb = self._reconnect_sandbox()
        if sb is not None:
            console.print("[bold]Terminating Modal sandbox…[/]")
            sb.terminate()
            self._sandbox = None

        state.clear(self._state_key)
        console.print(f"[green]Down.[/] App [dim]{self._app_name}[/] persists in Modal (reused on next `up`).")

    def rebuild(self) -> None:
        console.print("[bold]Rebuilding: tearing down and recreating…[/]")
        self.down()
        self.up()

    async def exec_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 600,
        on_line: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> int:
        sb = await self._ensure_sandbox_async()

        # Push latest workspace files before executing
        await self._push_workspace_async(sb)

        # Drop a marker so we can find files modified during the exec and
        # sync them back to the laptop afterwards. Gives the user a
        # "same /workspace, different compute" experience.
        marker = "/tmp/rex-exec-marker"
        await sb.filesystem.write_text.aio(marker, "")

        full_cmd = command
        if cwd:
            cwd_clean = cwd.lstrip("/")
            full_cmd = f"cd {self.workdir}/{cwd_clean} && {command}"

        process = await sb.exec.aio(
            "bash", "-lc", full_cmd,
        )

        async for line in process.stdout:
            text = line.rstrip("\n") if isinstance(line, str) else line.decode().rstrip("\n")
            if on_line is not None:
                await on_line("stdout", text)
        async for line in process.stderr:
            text = line.rstrip("\n") if isinstance(line, str) else line.decode().rstrip("\n")
            if on_line is not None:
                await on_line("stderr", text)

        await process.wait.aio()
        exit_code = process.returncode

        # Pull files that were created or modified during the exec
        try:
            await self._pull_changed_files(sb, marker)
        except Exception as e:
            if on_line is not None:
                await on_line("stderr", f"[rex] post-exec sync failed: {e}")

        return exit_code

    async def _pull_changed_files(self, sb, marker: str) -> int:
        """Find files under workdir that were modified after `marker` and
        copy them back to the local project dir. Returns the count pulled."""
        find_cmd = [
            "find",
            self.workdir,
            "-type", "f",
            "-newer", marker,
            "-print0",
        ]
        proc = await sb.exec.aio(*find_cmd)
        data = b""
        async for chunk in proc.stdout:
            data += chunk.encode() if isinstance(chunk, str) else chunk
        await proc.wait.aio()

        if not data:
            return 0

        remote_paths = [p for p in data.split(b"\x00") if p]
        ignores = self._effective_ignores()
        count = 0
        for raw in remote_paths:
            remote_path = raw.decode("utf-8", errors="replace")
            if not remote_path.startswith(self.workdir + "/"):
                continue
            rel = Path(remote_path[len(self.workdir) + 1:])
            if _should_ignore(rel, ignores):
                continue
            local_path = self._project_dir / rel
            local_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                await sb.filesystem.copy_to_local.aio(remote_path, str(local_path))
                count += 1
            except Exception:
                pass
        return count

    def pull_file(self, src: str, dest: str) -> Path:
        sb = self._ensure_sandbox()

        if not src.startswith("/"):
            src = f"{self.workdir}/{src}"

        local_dest = Path(dest).resolve()
        local_dest.parent.mkdir(parents=True, exist_ok=True)

        sb.filesystem.copy_to_local(src, str(local_dest))
        return local_dest

    def doctor(self) -> bool:
        all_ok = True

        table = Table(title="remote-executor doctor (modal)", show_header=True)
        table.add_column("Check", style="bold")
        table.add_column("Status")
        table.add_column("Details", style="dim")

        try:
            modal = _import_modal()
            table.add_row("Modal SDK", "[green]OK[/]", f"v{modal.__version__}")
        except RuntimeError as e:
            table.add_row("Modal SDK", "[red]FAIL[/]", str(e))
            all_ok = False
            console.print(table)
            return all_ok

        try:
            modal.App.lookup("rex-doctor-probe", create_if_missing=True)
            table.add_row("Modal auth", "[green]OK[/]", "authenticated")
        except Exception as e:
            table.add_row("Modal auth", "[red]FAIL[/]", f"Run `modal token set`. {e}")
            all_ok = False

        df_path = self._project_dir / self._cfg.project.dockerfile
        if df_path.exists():
            table.add_row("Dockerfile", "[green]OK[/]", str(df_path.name))
        else:
            table.add_row("Dockerfile", "[red]FAIL[/]", f"{df_path.name} not found")
            all_ok = False

        st = state.load(self._state_key)
        if st.sandbox_id:
            table.add_row("Sandbox", "[green]RUNNING[/]", st.sandbox_id)
        else:
            table.add_row("Sandbox", "[dim]NOT RUNNING[/]", "run `remote-executor up`")

        table.add_row("GPU requested", "[dim]INFO[/]", self._profile.gpu)
        table.add_row("Timeout", "[dim]INFO[/]", f"{self._profile.timeout_minutes} min")

        console.print(table)
        if all_ok:
            console.print("\n[bold green]All checks passed.[/]")
        else:
            console.print("\n[bold red]Some checks failed.[/]")
        return all_ok
