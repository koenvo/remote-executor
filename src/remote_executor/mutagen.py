"""Mutagen binary fetcher (Playwright pattern) + sync session lifecycle.

We download a pinned mutagen release into our user-data dir on first use, then
shell out to it for `sync create` / `sync terminate` etc. We don't try to
manage the mutagen daemon explicitly — mutagen autostarts it on first invocation.
"""

from __future__ import annotations

import hashlib
import platform
import re
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from remote_executor.paths import bin_dir, mutagen_current_symlink

MUTAGEN_VERSION = "0.18.1"
RELEASE_URL = "https://github.com/mutagen-io/mutagen/releases/download/v{version}"


class MutagenError(RuntimeError):
    pass


@dataclass(frozen=True)
class Platform:
    os: str
    arch: str

    @property
    def asset_name(self) -> str:
        return f"mutagen_{self.os}_{self.arch}_v{MUTAGEN_VERSION}.tar.gz"


def detect_platform() -> Platform:
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "darwin":
        os_name = "darwin"
    elif system == "linux":
        os_name = "linux"
    else:
        raise MutagenError(f"Unsupported OS: {system}")

    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        raise MutagenError(f"Unsupported architecture: {machine}")

    return Platform(os=os_name, arch=arch)


def install_dir() -> Path:
    return bin_dir() / f"mutagen-{MUTAGEN_VERSION}"


def binary_path() -> Path:
    return install_dir() / "mutagen"


def is_installed() -> bool:
    bin_p = binary_path()
    if not bin_p.exists():
        return False
    try:
        subprocess.run([str(bin_p), "version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, OSError):
        return False
    return True


def ensure_installed() -> Path:
    """Download + extract mutagen if not already present. Returns binary path."""
    if is_installed():
        return binary_path()

    plat = detect_platform()
    install_dir().mkdir(parents=True, exist_ok=True)

    base = RELEASE_URL.format(version=MUTAGEN_VERSION)
    asset_url = f"{base}/{plat.asset_name}"
    sums_url = f"{base}/SHA256SUMS"

    with httpx.Client(follow_redirects=True, timeout=120.0) as client:
        sums_resp = client.get(sums_url)
        sums_resp.raise_for_status()
        expected_sha = _parse_sha256(sums_resp.text, plat.asset_name)
        if expected_sha is None:
            raise MutagenError(f"No SHA256 entry for {plat.asset_name} in SHA256SUMS")

        tarball_resp = client.get(asset_url)
        tarball_resp.raise_for_status()
        tarball_bytes = tarball_resp.content

    actual_sha = hashlib.sha256(tarball_bytes).hexdigest()
    if actual_sha != expected_sha:
        raise MutagenError(
            f"SHA256 mismatch for {plat.asset_name}: expected {expected_sha}, got {actual_sha}"
        )

    tarball_path = install_dir() / plat.asset_name
    tarball_path.write_bytes(tarball_bytes)
    with tarfile.open(tarball_path, "r:gz") as tf:
        # The mutagen tarball contains the `mutagen` binary at the root.
        tf.extractall(install_dir(), filter="data")
    tarball_path.unlink()

    bin_p = binary_path()
    if not bin_p.exists():
        raise MutagenError(f"mutagen binary not found in extracted tarball at {bin_p}")
    bin_p.chmod(0o755)

    symlink = mutagen_current_symlink()
    if symlink.exists() or symlink.is_symlink():
        symlink.unlink()
    symlink.symlink_to(bin_p)

    return bin_p


def _parse_sha256(sums_text: str, filename: str) -> str | None:
    for line in sums_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1].lstrip("*") == filename:
            return parts[0].lower()
    return None


# ---------------------------------------------------------------------------
# Sync session lifecycle
# ---------------------------------------------------------------------------


_SESSION_ID_RE = re.compile(r"\b(sync_[A-Za-z0-9]+)\b")


def sync_create(
    *,
    name: str,
    local_path: Path,
    remote_host: str,
    remote_path: str,
    ignores: list[str],
    mode: str = "two-way-safe",
) -> str:
    """Create a sync between local_path and remote_host:remote_path.

    Default mode is `two-way-safe`: both sides can write, conflicts abort
    rather than clobber. This gives the user the illusion of a single
    `/workspace` that happens to have remote compute attached to it.

    Returns the mutagen session ID.
    """
    bin_p = ensure_installed()

    args: list[str] = [
        str(bin_p),
        "sync",
        "create",
        "--name",
        name,
        "--mode",
        mode,
    ]
    for pattern in ignores:
        args += ["--ignore", pattern]
    args += [str(local_path), f"{remote_host}:{remote_path}"]

    result = subprocess.run(args, capture_output=True, text=True, check=True)
    combined = result.stdout + "\n" + result.stderr
    match = _SESSION_ID_RE.search(combined)
    if match is None:
        raise MutagenError(f"Could not parse session ID from mutagen output:\n{combined}")
    return match.group(1)


def sync_terminate(session_id: str) -> None:
    bin_p = ensure_installed()
    subprocess.run(
        [str(bin_p), "sync", "terminate", session_id],
        capture_output=True,
        check=False,
    )


def sync_flush(session_id: str, timeout: float = 60.0) -> None:
    bin_p = ensure_installed()
    subprocess.run(
        [str(bin_p), "sync", "flush", session_id],
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def sync_list() -> str:
    bin_p = ensure_installed()
    result = subprocess.run(
        [str(bin_p), "sync", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout
