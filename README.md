# remote-executor

**Run Claude Code on a machine that doesn't have the architecture your POC needs.**

Your laptop doesn't have an NVIDIA GPU. The POC you're working on needs CUDA, NVENC, or a specific Linux kernel. You could SSH into a remote machine manually (and lose Claude Code's local editing experience), or you could run Claude Code on the remote box (and lose your local files, your editor setup, your git workflow). Neither is great.

`remote-executor` is a tiny MCP server + CLI that bridges the gap: **Claude Code edits files locally, but every shell command runs inside a remote execution environment**. Source edits flow to the remote automatically. Commands stream output back. You scp nothing. You keep your laptop.

Two backends, same interface:

- **SSH + Docker** — long-lived container on a remote Linux host you already own
- **Modal** — serverless GPU sandbox (T4, L4, L40S, A10G, A100, H100, H200) billed by the second

Same Dockerfile, same CLI, same MCP tools. Switch with a `--profile` flag.

## The insight

Claude Code is a great assistant when it can run code and see the results. For GPU/CUDA/NVENC POCs, that loop breaks on a Mac. `remote-executor` replaces Claude's built-in `Bash` for anything that touches the toolchain with `mcp__remote-executor__remote_bash` — a tool that transparently executes inside a container on a remote host. From Claude's perspective, it's just a shell. From your perspective, the laptop suddenly has an H100 attached to it.

## Install

```bash
# SSH+Docker backend only (lighter install)
uv tool install "remote-executor @ git+https://github.com/koenvo/remote-executor.git"

# With Modal support
uv tool install "remote-executor[modal] @ git+https://github.com/koenvo/remote-executor.git"
```

Requirements:
- macOS or Linux (POSIX). Windows untested.
- `uv` (for `uv tool install`). Install from [astral.sh/uv](https://github.com/astral-sh/uv).
- For SSH+Docker: `ssh` in `PATH`, a remote host with Docker + NVIDIA Container Toolkit, and an entry in `~/.ssh/config`.
- For Modal: a Modal account (`modal token set`).
- First run fetches the [mutagen](https://github.com/mutagen-io/mutagen) binary into `~/.local/share/remote-executor/bin/` (used by the SSH backend for file sync).

## Quickstart: Modal

```bash
cd your-gpu-project
remote-executor init                        # interactive: pick "modal" + "T4"
remote-executor doctor                      # verify auth, Dockerfile, etc.
remote-executor up                          # build image, start sandbox
claude                                      # launch Claude Code in the project
```

In the Claude session, ask:

> Run `nvidia-smi` and tell me what GPU we're on.

Claude calls `mcp__remote-executor__remote_bash`, which executes `nvidia-smi` inside the Modal sandbox and streams the output back. When you're done:

```bash
remote-executor down                         # terminate the sandbox
```

## Quickstart: SSH + Docker

```bash
# Add your remote host to ~/.ssh/config first:
#   Host my-gpu-box
#       HostName 10.0.0.42
#       User you
#       IdentityFile ~/.ssh/id_ed25519

cd your-gpu-project
remote-executor init                         # interactive: pick "ssh-docker" + "my-gpu-box"
remote-executor doctor                       # verify SSH, Docker, GPU, mutagen
remote-executor up                           # start mutagen sync + container
claude
```

Same Claude experience — `remote_bash` tunnels into the container on `my-gpu-box`.

## How it works

```
┌──────────────┐            ┌──────────────────────────────┐
│              │            │                              │
│   Claude     │  MCP       │   remote-executor (local)    │
│   Code       │  stdio     │                              │
│              │───────────▶│   SSH ControlMaster  ────┐   │
│   (on        │            │   + mutagen sync engine  │   │
│   your Mac)  │            │   OR Modal SDK           │   │
│              │            │                          ▼   │
└──────────────┘            └──────────────────────────┼───┘
       ▲                                               │
       │                                               │
       │ reads/edits files                             │
       │                                               ▼
┌──────┴────────┐                         ┌─────────────────┐
│               │                         │                 │
│   /Users/you/ │                         │   /workspace    │
│   your-repo   │   ◀── one-way sync ──▶  │   (container or │
│               │                         │    sandbox)     │
└───────────────┘                         └─────────────────┘
```

- **Workspace sync is one-way** (laptop → remote). Local edits via Read/Edit/Write are visible to the next `remote_bash` call without a rebuild.
- **Pull is explicit.** After a command produces output files you want locally, Claude calls `mcp__remote-executor__sync_down <path>`. This avoids accidentally dragging `node_modules` / model checkpoints / intermediate artifacts back.
- **The environment lifecycle is user-controlled.** `remote-executor up` starts it. `remote-executor down` terminates it. Claude can call `ensure_up` to start it defensively but cannot tear it down.
- **Image building uses your Dockerfile.** Modal uses `Image.from_dockerfile()`; SSH+Docker runs `docker build` on the remote. Same Dockerfile, both backends.

## Profiles

A single project can target multiple environments. Example `.remote-executor.toml`:

```toml
[project]
name = "gpu-encode"
dockerfile = "Dockerfile"
workdir = "/workspace"
default_profile = "modal-t4"

[profiles.modal-t4]
backend = "modal"
gpu = "T4"

[profiles.modal-l40s]
backend = "modal"
gpu = "L40S"

[profiles.home-gpu]
backend = "ssh-docker"
host_alias = "my-gpu-box"
gpus = "all"
```

Switch with `remote-executor up --profile modal-l40s` (and restart Claude so the MCP server picks up the new profile env var).

## MCP tools Claude gets

| Tool | What it does |
|---|---|
| `ensure_up` | Start the environment if not already running. Idempotent. |
| `remote_bash` | Execute a shell command inside the environment. Streams stdout/stderr. |
| `sync_down` | Pull a file or directory from the remote workspace back to the matching local path. |
| `pull` | Lower-level: pull a specific file from an env-local volume to a specific local path. |

## CLI reference

| Command | Purpose |
|---|---|
| `remote-executor init` | Interactive setup. Writes `.remote-executor.toml`, `.mcp.json`, and a Claude Code skill. |
| `remote-executor doctor` | Verify connectivity, auth, Docker/Modal, GPU, mutagen. |
| `remote-executor up [--profile X]` | Provision the environment. |
| `remote-executor down [--profile X]` | Tear down. |
| `remote-executor rebuild [--profile X]` | Rebuild image + recreate environment. |
| `remote-executor pull <src> <dest>` | One-shot copy from environment to laptop. |
| `remote-executor profiles` | List defined profiles. |
| `remote-executor version` | Show tool version and project version if inside a project. |
| `remote-executor mcp` | Run the MCP stdio server (called by Claude Code, not by you). |

## Design decisions (and trade-offs)

These are the calls worth knowing about if you want to hack on the tool or report an issue:

- **One-way sync, explicit pull-back.** Auto-bidirectional sync either drags large unwanted files back (`node_modules`, intermediate artifacts) or fights the user on deletions. We went with: push is automatic and cheap, pull is a deliberate MCP tool call. Claude is instructed to call `sync_down` after producing output files.
- **Shell out to `ssh`, don't use `paramiko`.** We inherit `~/.ssh/config`, `ProxyJump`, `ControlMaster`, hardware keys, and 1Password / YubiKey signing for free. No credential handling code in the tool.
- **Sidecar-owned ControlMaster socket.** Stored at `$XDG_STATE_HOME/remote-executor/mux/<host>.sock` (or `$TMPDIR` if the XDG path has spaces or exceeds Unix-socket length limits on macOS).
- **Modal backend pushes files via `sb.filesystem.copy_from_local`** before each exec — not via `Volume` or `Mount`. Simpler and matches the "push each source file before exec" mental model.
- **Data-center H100/H200 have NVENC stripped by NVIDIA.** If your POC uses NVENC, use L40S/L4/T4 instead. The tool works fine on H100/H200 for anything else.
- **No `down` exposed via MCP.** Teardown is a "I'm done burning GPU minutes" moment that belongs to the user, not the assistant.
- **Tool version is stamped into `.remote-executor.toml`.** On load, the CLI warns if the project was configured by a different version than the one running.

## Security / threat model

- The tool runs under your user. It invokes your local `ssh` binary and (if enabled) the `modal` Python SDK with your credentials.
- The MCP server is a stdio subprocess launched by Claude Code. No network ports are opened locally.
- Claude gets shell access to the remote environment via `remote_bash` — treat it as you would any shell you grant the assistant. Default MCP permissions in Claude Code require confirmation for each call until you approve it.
- Your project directory is synced to `{sync_remote_root}/{container_name}/workspace` on the remote host. `sync_remote_root` defaults to `~/.remote-executor/projects`.
- `remote-executor` never stores credentials, does not open network sockets, and has no telemetry.

## Development

```bash
git clone https://github.com/koenvo/remote-executor.git
cd remote-executor
uv sync --all-extras
uv run pytest
```

## Status

v0. POSIX only. Two backends. API and config schema may still shift based on real-world POC usage. See `CHANGELOG.md` for changes.

## License

MIT. See [LICENSE](LICENSE).
