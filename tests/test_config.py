"""Tests for config loading, validation, profiles, and backend factory."""

from pathlib import Path

import pytest

from remote_executor.config import (
    CONFIG_FILENAME,
    MetaSection,
    Profile,
    ProjectConfig,
    ProjectSection,
    VolumeMount,
    load,
    write,
)


def _cfg(**overrides) -> ProjectConfig:
    defaults = dict(
        project=ProjectSection(name="test-project", default_profile="modal-t4"),
        profiles={
            "modal-t4": Profile(backend="modal", gpu="T4", timeout_minutes=30),
            "modal-h200": Profile(backend="modal", gpu="H200", timeout_minutes=60),
            "office-box": Profile(
                backend="ssh-docker",
                host_alias="office-box-host",
                gpus="all",
                memory="16g",
            ),
        },
    )
    defaults.update(overrides)
    return ProjectConfig(**defaults)


class TestContainerName:
    def test_stable_across_calls(self, tmp_path: Path) -> None:
        cfg = _cfg()
        assert cfg.container_name(tmp_path, "modal-t4") == cfg.container_name(tmp_path, "modal-t4")

    def test_different_profiles_different_names(self, tmp_path: Path) -> None:
        cfg = _cfg()
        assert cfg.container_name(tmp_path, "modal-t4") != cfg.container_name(tmp_path, "modal-h200")

    def test_includes_profile_name(self, tmp_path: Path) -> None:
        cfg = _cfg()
        assert "modal-t4" in cfg.container_name(tmp_path, "modal-t4")
        assert "office-box" in cfg.container_name(tmp_path, "office-box")

    def test_image_tag_per_profile(self) -> None:
        cfg = _cfg()
        assert cfg.image_tag("modal-t4") == "rex-test-project-modal-t4:latest"
        assert cfg.image_tag("office-box") == "rex-test-project-office-box:latest"


class TestGetProfile:
    def test_default_profile(self) -> None:
        cfg = _cfg()
        name, p = cfg.get_profile()
        assert name == "modal-t4"
        assert p.backend == "modal"
        assert p.gpu == "T4"

    def test_named_profile(self) -> None:
        cfg = _cfg()
        name, p = cfg.get_profile("office-box")
        assert name == "office-box"
        assert p.backend == "ssh-docker"
        assert p.host_alias == "office-box-host"

    def test_unknown_profile_raises(self) -> None:
        cfg = _cfg()
        with pytest.raises(ValueError, match="not found"):
            cfg.get_profile("nonexistent")

    def test_ssh_docker_missing_host_raises(self) -> None:
        cfg = _cfg(
            profiles={"bad": Profile(backend="ssh-docker")},
            project=ProjectSection(name="p", default_profile="bad"),
        )
        with pytest.raises(ValueError, match="host_alias"):
            cfg.get_profile("bad")

    def test_modal_missing_gpu_raises(self) -> None:
        cfg = _cfg(
            profiles={"bad": Profile(backend="modal")},
            project=ProjectSection(name="p", default_profile="bad"),
        )
        with pytest.raises(ValueError, match="gpu"):
            cfg.get_profile("bad")


class TestRoundTrip:
    def test_roundtrip_multi_profile(self, tmp_path: Path) -> None:
        original = _cfg()
        write(tmp_path, original)
        loaded = load(tmp_path)
        assert loaded.project.name == "test-project"
        assert loaded.project.default_profile == "modal-t4"
        assert set(loaded.profiles.keys()) == {"modal-t4", "modal-h200", "office-box"}
        assert loaded.profiles["office-box"].host_alias == "office-box-host"
        assert loaded.profiles["modal-h200"].gpu == "H200"

    def test_load_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="remote-executor init"):
            load(tmp_path)

    def test_tool_version_roundtrip(self, tmp_path: Path) -> None:
        original = _cfg(meta=MetaSection(tool_version="9.9.9"))
        write(tmp_path, original)
        loaded = load(tmp_path, check_version=False)
        assert loaded.meta is not None
        assert loaded.meta.tool_version == "9.9.9"

    def test_version_mismatch_warns(self, tmp_path: Path, capsys) -> None:
        original = _cfg(meta=MetaSection(tool_version="0.0.1-old"))
        write(tmp_path, original)
        load(tmp_path)  # check_version=True by default
        captured = capsys.readouterr()
        assert "warning" in captured.err.lower()
        assert "0.0.1-old" in captured.err

    def test_no_version_no_warning(self, tmp_path: Path, capsys) -> None:
        original = _cfg()  # no meta
        write(tmp_path, original)
        load(tmp_path)
        captured = capsys.readouterr()
        assert "warning" not in captured.err.lower()


class TestBackendFactory:
    def test_ssh_docker_creates(self, tmp_path: Path) -> None:
        from remote_executor.backends import create_executor
        from remote_executor.backends.ssh_docker import SshDockerExecutor

        cfg = _cfg()
        executor = create_executor(tmp_path, cfg, profile_name="office-box")
        assert isinstance(executor, SshDockerExecutor)

    def test_modal_creates_with_default(self, tmp_path: Path) -> None:
        from remote_executor.backends import create_executor
        from remote_executor.backends.modal_backend import ModalExecutor

        cfg = _cfg()
        executor = create_executor(tmp_path, cfg)  # no profile → default
        assert isinstance(executor, ModalExecutor)

    def test_unknown_profile_raises(self, tmp_path: Path) -> None:
        from remote_executor.backends import create_executor

        cfg = _cfg()
        with pytest.raises(ValueError, match="not found"):
            create_executor(tmp_path, cfg, profile_name="nope")
