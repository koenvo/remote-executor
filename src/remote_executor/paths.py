"""XDG-style paths for remote-executor state, data, and cache."""

from __future__ import annotations

import os
import re
from pathlib import Path

from platformdirs import user_cache_dir, user_data_dir, user_state_dir

APP_NAME = "remote-executor"

# macOS / Linux limit Unix domain socket paths to ~104 chars.
# Leave some headroom for the filename itself.
_SOCKET_PATH_LIMIT = 100


def data_dir() -> Path:
    """Where downloaded binaries (mutagen) live."""
    p = Path(user_data_dir(APP_NAME))
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_dir() -> Path:
    """Per-host runtime state (mux sockets, container IDs, sync session IDs)."""
    p = Path(user_state_dir(APP_NAME))
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    p = Path(user_cache_dir(APP_NAME))
    p.mkdir(parents=True, exist_ok=True)
    return p


def bin_dir() -> Path:
    p = data_dir() / "bin"
    p.mkdir(parents=True, exist_ok=True)
    return p


def mutagen_current_symlink() -> Path:
    return bin_dir() / "mutagen-current"


def host_state_dir(key: str) -> Path:
    """Per-(backend, profile) state dir. `key` is a free-form identifier
    like 'ssh-docker:teamtv-gpu:default' or 'modal:modal-t4'."""
    p = state_dir() / "instances" / _slug(key)
    p.mkdir(parents=True, exist_ok=True)
    return p


def host_state_file(key: str) -> Path:
    return host_state_dir(key) / "state.json"


def mux_socket(host: str) -> Path:
    """SSH ControlMaster socket path.

    SSH's `-o ControlPath=<path>` can't handle spaces in the path, and on macOS
    the default user_state_dir is `~/Library/Application Support/...` which has
    a space. Falls back to $TMPDIR if the XDG path contains a space or exceeds
    the Unix-socket length limit (~104 chars)."""
    primary = state_dir() / "mux" / f"{_slug(host)}.sock"
    primary_str = str(primary)

    if " " not in primary_str and len(primary_str) <= _SOCKET_PATH_LIMIT:
        primary.parent.mkdir(parents=True, exist_ok=True)
        return primary

    tmp_root = Path(os.environ.get("TMPDIR", "/tmp")) / "rex-mux"
    tmp_root.mkdir(parents=True, exist_ok=True)
    return tmp_root / f"{_slug(host)}.sock"


_SLUG_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _slug(value: str) -> str:
    return _SLUG_RE.sub("-", value).strip("-") or "default"
