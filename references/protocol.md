# Protocol, environment, job files, troubleshooting

## How the bridge connects

The dsh Web UI is protected by a one-time launch token. The bridge never touches it. Instead it starts dsh's own Agent Client Protocol app, `dsh --profile acp`, and exchanges newline-delimited JSON-RPC (ACP v1) over stdin/stdout.

| Method | Used for |
|---|---|
| `initialize` | Handshake: protocolVersion 1. The bridge offers no client filesystem or terminal; dsh uses its own tools. |
| `session/new` / `session/resume` | Open a new session, or resume a persisted session that is not active. Both return `configOptions`. |
| `session/set_config_option` | Set `model` (the value is a JSON string `["provider","model"]`) and `reasoning_effort`. |
| `session/prompt` | Run one turn. `session/update` notifications stream in during it: message and thought chunks, `tool_call`, and `tool_call_update`. |
| `session/cancel` | Cancel the running turn. |
| `session/list` | List resumable sessions. |
| `session/close` | Settle, flush and release the session. |
| `session/request_permission` (dsh → bridge) | dsh asks before leaving its sandbox. The bridge answers according to the job's permission mode. |

dsh's permission request carries only a `toolCallId`. The bridge remembers each tool call's `title`, `kind` and `rawInput` from the earlier `tool_call` updates, so the log shows what was actually allowed or denied.

Sessions are persisted in dsh's home (`~/.dsh/sessions/<encoded cwd>/<session-id>/session.v2.jsonl[.zstd]`), the same store the Web UI uses. Every bridge turn therefore shows up in the Web UI. `history` reads these files directly.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `DSH_CMD` | — | Full command that starts dsh, e.g. `dsh` or `npx -y @deepseek-ai/dsh`. On Windows, `.cmd` shims are resolved through `PATH`. |
| `DSH_REPO` | — | A dsh source checkout (after `pnpm install && pnpm run build`). Started with `node --import tsx/esm apps/cli/src/bin.ts`. |
| (neither set) | | Uses `dsh` on `PATH`, then a checkout at `~/deepseek-harness` (the default `git clone` location). |
| `DSH_HOME` | `~/.dsh` | dsh's home, used by `history`. |
| `DSH_BRIDGE_HOME` | `~/.dsh-bridge` | Bridge state: `jobs/`, `locks/`, `last/`. |
| `DSH_BRIDGE_PERMISSION` | `deny` | Default answer to permission prompts: `deny` or `allow`. Any other value is an error. |
| `DSH_BRIDGE_CANCEL_GRACE` | `30` | Seconds dsh gets to honour `session/cancel` before its process tree is killed. |
| `DSH_BRIDGE_DEBUG` | — | When set, print dsh's stderr. |

## Job files

Each job lives in `~/.dsh-bridge/jobs/<job>/`:

| File | Contents |
|---|---|
| `meta.json` | State: `status` is `starting`, `running`, `done`, `cancelled` or `error`. Also `stop`, `session`, `permissions` and timestamps. Only the job process writes it. |
| `prompt.md` | The message that was sent. |
| `log.md` | The full transcript: thoughts, tool calls, permission decisions. |
| `reply.md` | DeepSeek's final message: the text after its last tool call. Earlier narration is only in `log.md`. |
| `bridge.err` | Errors from the bridge process itself. |
| `lock` | Held by the live job process. "Running" means this lock is held, so no PID check is involved. |
| `cancel` | Its presence asks the job to cancel. |

Other state:
- `locks/<hash>.lock` is held for the duration of a turn. That is how the bridge refuses a session one of its own turns is running.
- `last/<hash>.json` remembers the last session used per working directory, for `--last`.

Jobs keep full prompts and thoughts in plain text. Remove old ones with `dsh_bridge.py prune --days 14`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `dsh not found` / `cannot start dsh` | Install dsh (`npm i -g @deepseek-ai/dsh`; needs Node ≥ 22.19), or set `DSH_CMD` / `DSH_REPO`. |
| First call is slow | dsh initialises the `acp` profile on first boot; give it a minute or two. `dsh_start` returns without a session id meanwhile; `dsh_wait` fills it in. |
| Frames cannot be parsed / nothing happens | Don't start dsh through `pnpm dsh`: it echoes to stdout. Set `DSH_BRIDGE_DEBUG=1` to see dsh's stderr. |
| `session/resume` fails | The session is running in the Web UI or another process. Wait, or start a new session. |
| `model ... not available` | Run `models` for the exact values; they come from the providers configured in dsh. |
| Job shows `error: job process ended without finishing` | The job process was killed (machine sleep or reboot, task manager, or an MCP host that kills its children). See `bridge.err` and `log.md`. |
| Every `dsh_wait` times out in the client | Lower `timeout_s` below the client's tool timeout, or raise the client's timeout (Codex: `tool_timeout_sec`). |
| DeepSeek reports it was denied an action | That's the default `deny` mode at work. Ask the user whether to allow it (`permission: "allow"`), or change the task. |
| Garbled Chinese in logs | Logs are UTF-8. In PowerShell 5, use `Get-Content -Encoding utf8`. |
| `history` says `pip install zstandard` | Compressed sessions need it: `pip install zstandard`. |
