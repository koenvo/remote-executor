# remote-executor

Run Claude Code commands inside a Docker container on a remote SSH host, with the local workspace one-way synced into the container. Lets you keep editing files locally on a machine that doesn't have the architecture your code needs (e.g. an NVIDIA GPU), while every shell command Claude runs executes on a remote box that does.

## How it works

- A small CLI manages a long-lived Docker container on a remote host you reach over SSH
- Your project directory is mirrored into the container at `/workspace` via [mutagen](https://mutagen.io) (one-way: laptop → remote)
- An MCP server exposes a `remote_bash` tool to Claude Code; Claude's built-in `Bash` is denied via project settings, so the only shell Claude has runs inside the container
- Build artifacts and caches live on container-local volumes; the `pull` tool retrieves them on demand

## Install

```bash
uv tool install remote-executor
```

First run will fetch the [mutagen](https://github.com/mutagen-io/mutagen) binary from GitHub releases into `~/.local/share/remote-executor/bin/` (Playwright pattern).

## Quickstart

```bash
cd ~/your/gpu-project
remote-executor init --host gpu-box   # writes .remote-executor.toml + .claude/settings.json + CLAUDE.md snippet
remote-executor doctor                 # verify SSH, Docker, GPU, mutagen
remote-executor up                     # start container, begin sync
claude                                 # Claude now talks to the container via MCP
```

## Status

v0 — POSIX only (macOS / Linux). Single host per project. See `/Users/koen/.claude/plans/harmonic-popping-hare.md` for the design plan.
