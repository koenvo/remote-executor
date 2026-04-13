"""SSH + Docker executor: runs commands inside a Docker container on a remote
host, with workspace synced via mutagen."""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

from rich.console import Console
from rich.table import Table

from remote_executor import docker, mutagen, ssh, state
from remote_executor.config import Profile, ProjectConfig
from remote_executor.executor import Executor
from remote_executor.paths import mux_socket

console = Console(stderr=True)


class SshDockerExecutor(Executor):
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
        self._host = profile.host_alias
        assert self._host is not None
        self._cname = cfg.container_name(project_dir, profile_name)
        self._state_key = f"ssh-docker:{self._host}:{profile_name}"

    @property
    def workdir(self) -> str:
        return self._cfg.project.workdir

    def is_up(self) -> bool:
        st = state.load(self._state_key)
        if not st.container_name:
            return False
        try:
            if not ssh.mux_alive(self._host):
                return False
            return docker.container_running(self._host, st.container_name)
        except (subprocess.CalledProcessError, OSError):
            return False

    def _resolve_remote_workdir(self) -> str:
        remote_root = self._profile.sync_remote_root
        if remote_root.startswith("~"):
            home = ssh.home_dir(self._host)
            remote_root = home + remote_root[1:]
        return f"{remote_root}/{self._cname}/workspace"

    def _default_env(self) -> dict[str, str]:
        return {"TERM": "dumb", "CI": "1", "NO_COLOR": "1"}

    # -- lifecycle -----------------------------------------------------------

    def up(self) -> None:
        host = self._host
        cname = self._cname
        cfg = self._cfg
        profile = self._profile

        console.print(f"[bold]Connecting to {host}…[/]")
        ssh.ensure_mux(host)

        remote_wd = self._resolve_remote_workdir()
        console.print(f"[dim]Remote workspace: {remote_wd}[/]")
        ssh.remote_mkdir(host, remote_wd)

        # --- mutagen sync ---
        console.print("[bold]Starting file sync (laptop → remote)…[/]")
        mutagen.ensure_installed()

        ignores = list(profile.sync_ignore)
        for vol in profile.volumes:
            ignores.append(vol.name + "/")

        session_id = mutagen.sync_create(
            name=cname,
            local_path=self._project_dir,
            remote_host=host,
            remote_path=remote_wd,
            ignores=ignores,
            mode=profile.sync_mode,
        )
        console.print(f"[dim]Mutagen session: {session_id}[/]")
        mutagen.sync_flush(session_id, timeout=120.0)
        console.print("[green]Initial sync complete.[/]")

        # --- docker build ---
        tag = cfg.image_tag(self._profile_name)
        dockerfile_remote = f"{remote_wd}/{cfg.project.dockerfile}"
        context_remote = f"{remote_wd}/{cfg.project.build_context}"

        if not docker.image_exists(host, tag):
            console.print(f"[bold]Building image {tag}…[/]")
            rc = docker.build_image(host, tag=tag, dockerfile=dockerfile_remote, build_context=context_remote)
            if rc != 0:
                raise RuntimeError(f"docker build failed with exit code {rc}")
            console.print("[green]Image built.[/]")
        else:
            console.print(f"[dim]Image {tag} already exists, skipping build.[/]")

        # --- docker run ---
        if docker.container_running(host, cname):
            console.print(f"[dim]Container {cname} already running.[/]")
        elif docker.container_exists(host, cname):
            console.print(f"[bold]Starting existing container {cname}…[/]")
            ssh.run_shell(host, f"docker start {cname}", check=True)
        else:
            console.print(f"[bold]Creating container {cname}…[/]")
            docker.run_container(
                host,
                name=cname,
                image=tag,
                workspace_mount_source=remote_wd,
                workspace_mount_target=cfg.project.workdir,
                volumes=profile.volumes,
                gpus=profile.gpus,
                memory=profile.memory,
                shm_size=profile.shm_size,
                workdir=cfg.project.workdir,
                env=self._default_env(),
            )
            console.print("[green]Container started.[/]")

        # --- persist state ---
        st = state.load(self._state_key)
        st.container_name = cname
        st.container_id = docker.container_id(host, cname)
        st.image_tag = tag
        st.mutagen_session_id = session_id
        st.remote_workdir = remote_wd
        st.project_cwd = str(self._project_dir)
        st.backend_type = "ssh-docker"
        st.touch()
        state.save(self._state_key, st)

        console.print(f"\n[bold green]Ready.[/] Container [bold]{cname}[/] running on [bold]{host}[/] (profile: {self._profile_name}).")

    def down(self) -> None:
        host = self._host
        cname = self._cname
        st = state.load(self._state_key)

        if st.mutagen_session_id:
            console.print("[bold]Terminating sync session…[/]")
            mutagen.sync_terminate(st.mutagen_session_id)

        if docker.container_exists(host, cname):
            console.print(f"[bold]Stopping container {cname}…[/]")
            docker.stop_container(host, cname)
            docker.rm_container(host, cname, force=True)

        ssh.exit_mux(host)
        state.clear(self._state_key)
        console.print("[green]Down.[/]")

    def rebuild(self) -> None:
        host = self._host
        cname = self._cname
        cfg = self._cfg
        profile = self._profile
        st = state.load(self._state_key)

        ssh.ensure_mux(host)

        if st.mutagen_session_id:
            console.print("[bold]Flushing sync…[/]")
            mutagen.sync_flush(st.mutagen_session_id, timeout=120.0)

        remote_wd = st.remote_workdir or self._resolve_remote_workdir()
        tag = cfg.image_tag(self._profile_name)
        dockerfile_remote = f"{remote_wd}/{cfg.project.dockerfile}"
        context_remote = f"{remote_wd}/{cfg.project.build_context}"

        console.print(f"[bold]Rebuilding image {tag}…[/]")
        rc = docker.build_image(host, tag=tag, dockerfile=dockerfile_remote, build_context=context_remote)
        if rc != 0:
            raise RuntimeError(f"docker build failed with exit code {rc}")
        console.print("[green]Image rebuilt.[/]")

        if docker.container_exists(host, cname):
            console.print(f"[bold]Replacing container {cname}…[/]")
            docker.stop_container(host, cname)
            docker.rm_container(host, cname, force=True)

        docker.run_container(
            host,
            name=cname,
            image=tag,
            workspace_mount_source=remote_wd,
            workspace_mount_target=cfg.project.workdir,
            volumes=profile.volumes,
            gpus=profile.gpus,
            memory=profile.memory,
            shm_size=profile.shm_size,
            workdir=cfg.project.workdir,
            env=self._default_env(),
        )
        console.print("[green]Container replaced.[/]")

        st.container_id = docker.container_id(host, cname)
        st.image_tag = tag
        st.touch()
        state.save(self._state_key, st)

    # -- exec ---------------------------------------------------------------

    async def exec_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 600,
        on_line: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> int:
        container = self._ensure_container()

        workdir = self.workdir
        if cwd:
            cwd = cwd.lstrip("/")
            workdir = f"{self.workdir}/{cwd}"

        exec_env = {"TERM": "dumb", "CI": "1", "NO_COLOR": "1"}
        if env:
            exec_env.update(env)

        # Build the docker exec argv, then wrap it as a single shell command
        # and pass it through `bash -lc` with quoting — otherwise shell
        # metacharacters in `command` (like `&&` or `|`) get interpreted by
        # the remote host's login shell instead of the container's bash.
        argv = docker.exec_argv(
            container,
            command=command,
            workdir=workdir,
            env=exec_env,
        )
        shell_cmd = " ".join(shlex.quote(a) for a in argv)
        wrapped = ["bash", "-lc", shlex.quote(shell_cmd)]

        return await ssh.run_stream(
            self._host,
            wrapped,
            on_line=on_line,
            timeout=timeout,
        )

    # -- pull ---------------------------------------------------------------

    def pull_file(self, src: str, dest: str) -> Path:
        container = self._ensure_container()
        host = self._host

        if not src.startswith("/"):
            src = f"{self.workdir}/{src}"

        remote_tmp = f"/tmp/rex-pull-{os.getpid()}"
        ssh.run_shell(host, f"mkdir -p {shlex.quote(remote_tmp)}", check=True)

        basename = Path(src).name
        remote_dest = f"{remote_tmp}/{basename}"
        docker.cp_from_container(host, container, src, remote_dest)

        local_dest = Path(dest).resolve()
        local_dest.parent.mkdir(parents=True, exist_ok=True)

        sock = mux_socket(host)
        subprocess.run(
            ["scp", "-o", f"ControlPath={sock}", f"{host}:{remote_dest}", str(local_dest)],
            check=True,
        )

        ssh.run_shell(host, f"rm -rf {shlex.quote(remote_tmp)}", check=False)
        return local_dest

    # -- doctor -------------------------------------------------------------

    def doctor(self) -> bool:
        host = self._host
        cfg = self._cfg
        all_ok = True

        table = Table(title="remote-executor doctor (ssh-docker)", show_header=True)
        table.add_column("Check", style="bold")
        table.add_column("Status")
        table.add_column("Details", style="dim")

        try:
            ssh.ensure_mux(host)
            table.add_row("SSH connection", "[green]OK[/]", f"host={host}")
        except (subprocess.CalledProcessError, OSError) as e:
            table.add_row("SSH connection", "[red]FAIL[/]", str(e))
            all_ok = False

        try:
            resolved = ssh.resolve_config(host)
            hostname_line = next((l for l in resolved.splitlines() if l.startswith("hostname ")), "?")
            user_line = next((l for l in resolved.splitlines() if l.startswith("user ")), "?")
            table.add_row("SSH config", "[green]OK[/]", f"{hostname_line}, {user_line}")
        except (subprocess.CalledProcessError, OSError) as e:
            table.add_row("SSH config", "[yellow]WARN[/]", str(e))

        try:
            result = ssh.run_shell(host, "docker version --format '{{.Server.Version}}'", check=True)
            table.add_row("Docker", "[green]OK[/]", f"v{result.stdout.strip()}")
        except (subprocess.CalledProcessError, OSError) as e:
            table.add_row("Docker", "[red]FAIL[/]", str(e))
            all_ok = False

        if self._profile.gpus:
            try:
                result = ssh.run_shell(
                    host,
                    "docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 "
                    "nvidia-smi --query-gpu=name,driver_version --format=csv,noheader",
                    check=True,
                )
                gpu_info = result.stdout.strip().split("\n")[0]
                table.add_row("GPU (nvidia-smi)", "[green]OK[/]", gpu_info)
            except (subprocess.CalledProcessError, OSError) as e:
                table.add_row("GPU (nvidia-smi)", "[red]FAIL[/]", str(e))
                all_ok = False
        else:
            table.add_row("GPU", "[dim]SKIP[/]", "gpus not configured")

        try:
            bin_path = mutagen.ensure_installed()
            result = subprocess.run([str(bin_path), "version"], capture_output=True, text=True, check=True)
            table.add_row("Mutagen", "[green]OK[/]", f"v{result.stdout.strip()}")
        except (mutagen.MutagenError, subprocess.CalledProcessError, OSError) as e:
            table.add_row("Mutagen", "[red]FAIL[/]", str(e))
            all_ok = False

        sock = str(mux_socket(host))
        if len(sock) > 100:
            table.add_row("Socket path", "[yellow]WARN[/]", f"{len(sock)} chars (>100, using fallback)")
        else:
            table.add_row("Socket path", "[green]OK[/]", f"{len(sock)} chars")

        console.print(table)
        if all_ok:
            console.print("\n[bold green]All checks passed.[/]")
        else:
            console.print("\n[bold red]Some checks failed.[/]")
        return all_ok

    # -- helpers ------------------------------------------------------------

    def _ensure_container(self) -> str:
        st = state.load(self._state_key)
        if not st.container_name:
            raise RuntimeError("No container found. Run `remote-executor up` first.")
        if not docker.container_running(self._host, st.container_name):
            raise RuntimeError(
                f"Container {st.container_name} is not running. Run `remote-executor up`."
            )
        return st.container_name
