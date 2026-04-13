"""Backend registry: create the right Executor for the requested profile."""

from __future__ import annotations

from pathlib import Path

from remote_executor.config import ProjectConfig
from remote_executor.executor import Executor


def create_executor(
    project_dir: Path,
    cfg: ProjectConfig,
    profile_name: str | None = None,
) -> Executor:
    name, profile = cfg.get_profile(profile_name)

    if profile.backend == "ssh-docker":
        from remote_executor.backends.ssh_docker import SshDockerExecutor
        return SshDockerExecutor(project_dir, cfg, name, profile)

    if profile.backend == "modal":
        from remote_executor.backends.modal_backend import ModalExecutor
        return ModalExecutor(project_dir, cfg, name, profile)

    raise ValueError(f"Unknown backend type: {profile.backend!r}")
