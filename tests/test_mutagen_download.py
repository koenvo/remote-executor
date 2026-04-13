"""Tests for mutagen platform detection and SHA parsing (no network needed)."""

from remote_executor.mutagen import Platform, _parse_sha256, detect_platform


class TestPlatformDetect:
    def test_returns_platform(self) -> None:
        plat = detect_platform()
        assert plat.os in ("darwin", "linux")
        assert plat.arch in ("amd64", "arm64")

    def test_asset_name_format(self) -> None:
        plat = Platform(os="darwin", arch="arm64")
        assert plat.asset_name.startswith("mutagen_darwin_arm64_v")
        assert plat.asset_name.endswith(".tar.gz")


class TestParseSha256:
    SAMPLE_SUMS = """\
abc123def456  mutagen_darwin_arm64_v0.18.1.tar.gz
789abc012def  mutagen_linux_amd64_v0.18.1.tar.gz
"""

    def test_finds_matching_entry(self) -> None:
        result = _parse_sha256(self.SAMPLE_SUMS, "mutagen_darwin_arm64_v0.18.1.tar.gz")
        assert result == "abc123def456"

    def test_finds_linux_entry(self) -> None:
        result = _parse_sha256(self.SAMPLE_SUMS, "mutagen_linux_amd64_v0.18.1.tar.gz")
        assert result == "789abc012def"

    def test_returns_none_for_missing(self) -> None:
        result = _parse_sha256(self.SAMPLE_SUMS, "mutagen_windows_amd64_v0.18.1.tar.gz")
        assert result is None

    def test_handles_bsd_star_prefix(self) -> None:
        sums = "abc123  *mutagen_darwin_arm64_v0.18.1.tar.gz\n"
        result = _parse_sha256(sums, "mutagen_darwin_arm64_v0.18.1.tar.gz")
        assert result == "abc123"

    def test_handles_empty(self) -> None:
        assert _parse_sha256("", "anything.tar.gz") is None

    def test_handles_comments(self) -> None:
        sums = "# SHA256 checksums\nabc123  mutagen_linux_amd64_v0.18.1.tar.gz\n"
        result = _parse_sha256(sums, "mutagen_linux_amd64_v0.18.1.tar.gz")
        assert result == "abc123"
