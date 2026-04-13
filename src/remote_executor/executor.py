"""Executor protocol — the abstraction all backends implement."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path


class Executor(ABC):
    """A remote execution environment that can build images, run containers,
    execute commands, and transfer files."""

    @property
    @abstractmethod
    def workdir(self) -> str:
        """The working directory inside the execution environment (e.g. /workspace)."""

    @abstractmethod
    def up(self) -> None:
        """Provision the environment: sync files, build image, start container/sandbox."""

    @abstractmethod
    def down(self) -> None:
        """Tear down the environment."""

    @abstractmethod
    def rebuild(self) -> None:
        """Rebuild the image and recreate the environment."""

    @abstractmethod
    async def exec_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 600,
        on_line: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> int:
        """Execute a shell command. Streams output via `on_line(stream, line)`.
        Returns exit code (124 = timeout)."""

    @abstractmethod
    def pull_file(self, src: str, dest: str) -> Path:
        """Copy a file from the environment to the local machine."""

    @abstractmethod
    def doctor(self) -> bool:
        """Run diagnostic checks. Returns True if all passed."""
