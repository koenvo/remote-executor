"""Pure-string tests for SSH command construction (no network needed)."""

from unittest.mock import patch

from remote_executor.ssh import ssh_cmd, quote


class TestSshCmd:
    def test_basic_command(self) -> None:
        with patch("remote_executor.ssh.mux_socket") as mock_sock:
            mock_sock.return_value = type("P", (), {"__str__": lambda s: "/tmp/test.sock"})()
            cmd = ssh_cmd("gpu-box", ["docker", "ps"])

        assert cmd[0] == "ssh"
        assert "gpu-box" in cmd
        assert "docker" in cmd
        assert "ps" in cmd

    def test_controlpath_in_cmd(self) -> None:
        with patch("remote_executor.ssh.mux_socket") as mock_sock:
            mock_sock.return_value = type("P", (), {"__str__": lambda s: "/tmp/test.sock"})()
            cmd = ssh_cmd("gpu-box")

        joined = " ".join(cmd)
        assert "ControlPath=/tmp/test.sock" in joined
        assert "ControlMaster=auto" in joined
        assert "ControlPersist=" in joined

    def test_no_remote_argv(self) -> None:
        with patch("remote_executor.ssh.mux_socket") as mock_sock:
            mock_sock.return_value = type("P", (), {"__str__": lambda s: "/tmp/test.sock"})()
            cmd = ssh_cmd("gpu-box")

        assert cmd[-1] == "gpu-box"


class TestQuote:
    def test_simple(self) -> None:
        assert quote(["echo", "hello"]) == "echo hello"

    def test_spaces(self) -> None:
        result = quote(["echo", "hello world"])
        assert "hello world" in result
        assert result.startswith("echo ")

    def test_special_chars(self) -> None:
        result = quote(["echo", "$HOME"])
        assert "'" in result or "\\" in result
