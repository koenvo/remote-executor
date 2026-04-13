"""Per-host state.json: which container/image/sync-session belongs to which project."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from remote_executor.paths import host_state_file


@dataclass
class HostState:
    container_id: str | None = None
    container_name: str | None = None
    image_tag: str | None = None
    mutagen_session_id: str | None = None
    remote_workdir: str | None = None
    last_up_at: str | None = None
    project_cwd: str | None = None
    sandbox_id: str | None = None
    backend_type: str | None = None

    def touch(self) -> None:
        self.last_up_at = datetime.now(timezone.utc).isoformat()


def load(host: str) -> HostState:
    path = host_state_file(host)
    if not path.exists():
        return HostState()
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return HostState()
    return HostState(**{k: v for k, v in data.items() if k in HostState.__dataclass_fields__})


def save(host: str, state: HostState) -> None:
    path = host_state_file(host)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(state), f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def clear(host: str) -> None:
    path = host_state_file(host)
    if path.exists():
        path.unlink()
