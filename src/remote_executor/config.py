"""Project-level config: .remote-executor.toml schema, load + write.

A project has multiple named profiles, each pointing at a backend (ssh-docker
or modal) with backend-specific settings. One profile is the default; the CLI's
`--profile` flag overrides it.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path

import tomli_w
from pydantic import BaseModel, Field

CONFIG_FILENAME = ".remote-executor.toml"


class ProjectSection(BaseModel):
    name: str
    dockerfile: str = "Dockerfile"
    build_context: str = "."
    workdir: str = "/workspace"
    default_profile: str


class VolumeMount(BaseModel):
    name: str
    path: str


class Profile(BaseModel):
    """A named backend configuration. The `backend` field selects which fields apply."""

    backend: str  # "ssh-docker" or "modal"

    # --- ssh-docker fields ---
    host_alias: str | None = None
    gpus: str | None = None
    memory: str | None = None
    shm_size: str | None = None
    idle_ttl_minutes: int = 30
    sync_remote_root: str = "~/.remote-executor/projects"
    sync_ignore: list[str] = Field(default_factory=list)
    volumes: list[VolumeMount] = Field(default_factory=list)

    # --- modal fields ---
    gpu: str | None = None
    timeout_minutes: int = 60
    python_version: str = "3.12"
    region: str | None = None
    cloud: str | None = None

    def validate_for_backend(self, profile_name: str) -> None:
        if self.backend == "ssh-docker":
            if not self.host_alias:
                raise ValueError(f"profile {profile_name!r}: ssh-docker requires host_alias")
        elif self.backend == "modal":
            if not self.gpu:
                raise ValueError(f"profile {profile_name!r}: modal requires gpu")
        else:
            raise ValueError(f"profile {profile_name!r}: unknown backend {self.backend!r}")


class McpConfig(BaseModel):
    default_timeout_sec: int = 600
    max_timeout_sec: int = 3600


class ProjectConfig(BaseModel):
    project: ProjectSection
    profiles: dict[str, Profile]
    mcp: McpConfig = Field(default_factory=McpConfig)

    def get_profile(self, name: str | None = None) -> tuple[str, Profile]:
        """Return (profile_name, profile). If name is None, returns the default."""
        if name is None:
            name = self.project.default_profile
        if name not in self.profiles:
            available = ", ".join(sorted(self.profiles.keys()))
            raise ValueError(f"Profile {name!r} not found. Available: {available}")
        profile = self.profiles[name]
        profile.validate_for_backend(name)
        return name, profile

    def container_name(self, project_dir: Path, profile_name: str) -> str:
        """Stable per-(project, profile) name. Two profiles for the same project
        get distinct containers/sandboxes."""
        digest = hashlib.sha1(str(project_dir.resolve()).encode()).hexdigest()[:8]
        return f"rex-{_slug(self.project.name)}-{_slug(profile_name)}-{digest}"

    def image_tag(self, profile_name: str) -> str:
        return f"rex-{_slug(self.project.name)}-{_slug(profile_name)}:latest"


def load(project_dir: Path) -> ProjectConfig:
    path = project_dir / CONFIG_FILENAME
    if not path.exists():
        raise FileNotFoundError(
            f"No {CONFIG_FILENAME} in {project_dir}. Run `remote-executor init` first."
        )
    data = tomllib.loads(path.read_text())
    return ProjectConfig.model_validate(data)


def write(project_dir: Path, config: ProjectConfig) -> Path:
    path = project_dir / CONFIG_FILENAME
    path.write_text(tomli_w.dumps(config.model_dump(mode="python", exclude_none=True)))
    return path


_SLUG_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _slug(value: str) -> str:
    return _SLUG_RE.sub("-", value).strip("-").lower() or "default"
