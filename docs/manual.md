# deepseek-bridge — technical manual

> Plain-language overview and the one-message install prompt: see the [README](../README.md). Paths below are relative to the repository root.

Let a supervising agent (Claude Code, Codex, Cursor, …) **delegate work to a local [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`) agent and coach it**: write the brief, dispatch it, wait, verify the real output, give precise feedback, repeat.

[中文说明](#中文)

| Part | What it is |
|---|---|
| **Skill** (`SKILL.md`, `references/`) | The coaching method: briefs with checkable acceptance criteria, one phase at a time, independent verification (git diff, tests, tool results), feedback that names location + expectation + check, when to restart or escalate, and how to diagnose a user's existing DeepSeek sessions. |
| **MCP server** (`mcp/server.py`) | 7 tools: `dsh_start`, `dsh_wait`, `dsh_cancel`, `dsh_jobs`, `dsh_list_sessions`, `dsh_models`, `dsh_history`. |
| **CLI** (`scripts/dsh_bridge.py`) | The same features from a shell. |
| **Tests** (`tests/`) | A fake dsh plus regression scenarios; no DeepSeek account needed. |

## How it connects

dsh ships an Agent Client Protocol app, `dsh --profile acp`, which speaks JSON-RPC over stdio. The bridge is a small client for it.

- **Shared sessions.** Sessions are stored in dsh's own home (`~/.dsh`), so every bridge turn also appears in the dsh Web UI, and Web UI sessions can be read and resumed.
- **dsh's own settings apply.** The bridge uses dsh's credentials, providers and permission preset. It never touches the Web UI's login token.
- **Background jobs.** Long turns run as jobs: `dsh_start` returns within about 15 s, and `dsh_wait` blocks as long as you allow and can be called again. Every job's transcript is saved to `~/.dsh-bridge/jobs/<job>/log.md`.

## Requirements

- **dsh**, signed in to a provider (run `dsh web` once and add your DeepSeek API key under Settings → Models). dsh needs Node ≥ 22.19. The bridge finds it in this order:
  1. `DSH_CMD`, e.g. `DSH_CMD="npx -y @deepseek-ai/dsh"`;
  2. `DSH_REPO=/path/to/deepseek-harness`, a source checkout after `pnpm install && pnpm run build`;
  3. `dsh` on `PATH` (`npm i -g @deepseek-ai/dsh`);
  4. a checkout at `~/deepseek-harness`.
- **Python 3.9+.** Use `python3` wherever `python` is not Python 3 (macOS, most Linux).
- **`pip install zstandard`**, needed for `history`, because dsh compresses saved sessions.

Check the connection:

```bash
python scripts/dsh_bridge.py models --cwd .
```

## Install

**As an MCP server:**

```bash
# Claude Code
claude mcp add deepseek-bridge -- python /abs/path/to/deepseek-bridge/mcp/server.py
```

```toml
# Codex (~/.codex/config.toml). Codex's default tool timeout is 60 s, too short for dsh_wait.
[mcp_servers.deepseek-bridge]
command = "python"
args = ["/abs/path/to/deepseek-bridge/mcp/server.py"]
startup_timeout_sec = 30
tool_timeout_sec = 900
```

```json
// Cursor / generic mcp.json
{ "mcpServers": { "deepseek-bridge": { "command": "python", "args": ["/abs/path/to/deepseek-bridge/mcp/server.py"] } } }
```

Pass environment variables (`DSH_CMD`, `DSH_REPO`, …) through your client's MCP `env` setting if dsh is not on `PATH`.

**As a skill:** clone this repository (or copy / symlink it) into your agent's skills directory, e.g. `~/.claude/skills/deepseek-bridge` (Claude Code) or `~/.agents/skills/deepseek-bridge`.

Install both: the MCP server gives the tools, and the skill teaches the agent to verify instead of relaying.

## End-to-end example (CLI)

```bash
B=scripts/dsh_bridge.py
cd ~/proj && git status                       # start from a clean tree
python $B start --cwd ~/proj --model deepseek-v4-pro --file brief.md
# -> {"job": "20260101-120000-ab12cd", "session": "…", "status": "running", …}
python $B wait 20260101-120000-ab12cd --timeout 900     # repeat until status is done
git diff --stat && pytest                     # verify yourself; don't trust the reply
python $B history <session> --tail 80         # what did it actually run? any failed tool results?
python $B start --cwd ~/proj --last --file feedback.md # precise feedback, same session
```

Other commands: `send` (foreground, answer on stdout), `status [JOB]`, `cancel JOB`, `list [--cwd DIR]`, `prune --days 14`. Pass `-` instead of text to read the message from stdin (UTF-8).

## Permissions: read this

dsh decides what DeepSeek may do through its permission preset (`/permission` in dsh):
- `workspace-write` lets it edit and run inside the working directory, and **asks** before anything else (writing elsewhere, network, unsandboxed commands);
- `danger-full-access` never asks.

The bridge answers dsh's questions:

- **`deny` (default):** every out-of-sandbox request is refused. DeepSeek usually finds another way or reports the refusal.
- **`allow`** (`--permission allow`, `permission: "allow"`, or `DSH_BRIDGE_PERMISSION=allow`): every request is approved **without anyone seeing it first**. Use it only when the user agrees.

Every decision is logged with the actual command or path: `[permission ALLOWED] …` lines in `log.md`, and the `permissions` field of the job result. `cwd` is not a security boundary. Messages you send go to DeepSeek's API, so keep secrets out of briefs. See dsh's own [SAFETY.md](https://github.com/deepseek-ai/deepseek-harness/blob/main/SAFETY.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `cannot start dsh` | Install dsh or set `DSH_CMD` / `DSH_REPO`; check Node ≥ 22.19. |
| First call is slow | dsh initialises its `acp` profile on first boot. `dsh_start` returns without a session id meanwhile; `dsh_wait` fills it in. |
| Nothing happens / frames fail to parse | Never start dsh via `pnpm dsh` (it echoes to stdout). Set `DSH_BRIDGE_DEBUG=1` to see dsh's stderr. |
| `session/resume` fails | The session is busy in the Web UI or another process. Wait, or start a new session. |
| Client times out on `dsh_wait` | Use a `timeout_s` below the client's tool timeout, or raise it (Codex: `tool_timeout_sec`). |
| `models` adds an empty session to the Web UI | It reuses the last bridge session for that directory when it can; otherwise it has to open one. |

More detail: [references/protocol.md](../references/protocol.md).

## State and cleanup

`~/.dsh-bridge/` holds jobs (full prompts, thoughts and logs in plain text), locks, and the last session per directory. Run `python scripts/dsh_bridge.py prune --days 14` to delete old finished jobs. The dsh sessions themselves live in `~/.dsh` and are managed by dsh.

## Tests

```bash
python tests/run_tests.py        # ~2 minutes, uses tests/fake_dsh.py
```

These cover permission handling, malformed frames, dsh crashes, cancel and kill-after-grace, busy sessions, job-state races (dozens of concurrent jobs), input encodings, model resolution, and MCP protocol errors.

## Status

- **Tested with:** Windows 11, Python 3.11, dsh 0.1.3-alpha.1 (source checkout), and the `deepseek-v4-flash` / `deepseek-v4-pro` models, with real turns for send, background jobs, resume, cancel, models and history.
- **Tested only against the fake dsh** (the real install used `danger-full-access`, so dsh never asked): the permission prompt path.
- **Not yet tested:** npm releases of dsh (0.2.x), and macOS / Linux.

dsh is in developer preview; its ACP surface may change. Issues and PRs are welcome.

---

## 中文

让一个"监工" agent（Claude Code、Codex、Cursor 等）把任务**派给本机的 DeepSeek Harness（dsh）去执行，并负责调教和验收**：写需求、派发、等待、独立检查真实产出、给精确反馈，循环直到达标。

- **Skill**（`SKILL.md` + `references/`）：调教方法。包括：
  - 可检查的验收标准，一次只派一个阶段；
  - 独立验收：看 git diff、跑测试、看工具结果；
  - "位置 + 期望 + 验收"式反馈；
  - 什么时候开新会话或换模型；
  - 怎么诊断用户已有的 DeepSeek 对话。
- **MCP 服务器**（`mcp/server.py`）：7 个工具。
- **命令行**（`scripts/dsh_bridge.py`）：功能和 MCP 相同。
- **测试**（`tests/`）：用假 dsh 跑，不需要 DeepSeek 账号。

**原理**：dsh 自带 ACP 应用（`dsh --profile acp`，stdio 上的 JSON-RPC），bridge 是它的客户端。会话存在 dsh 自己的 home（`~/.dsh`）里，和 Web UI 是同一份，所以 bridge 的每一轮在 Web UI 里都能看到。凭据、模型和权限预设都沿用 dsh 的设置，bridge 不碰 Web UI 的登录 token。长任务是**后台 job**：`dsh_start` 约 15 秒内返回，`dsh_wait` 可以反复调用。

**依赖**：
- dsh，并且已在 dsh 里配置好 DeepSeek 凭据（需要 Node ≥ 22.19）；
- Python 3.9+；
- 读历史时需要 `pip install zstandard`。

**安装**：作为 MCP 用上面的命令配置（Codex 记得把 `tool_timeout_sec` 调大）；作为 skill 就把本目录复制或软链到 skills 目录。两者都装效果最好：MCP 提供工具，skill 教 agent 去验收而不是转发。

**权限（重要）**：dsh 的权限预设决定它越界时问不问：
- `workspace-write` 会问；
- `danger-full-access` 不问。

bridge 默认对越界请求**一律拒绝**。用 `allow` 会不经任何人查看就全部批准，只在用户同意时使用。每个决定都记在 `log.md` 和结果的 `permissions` 字段里。需求里不要放密钥。

**清理**：`~/.dsh-bridge/` 里存着 job（完整的明文提示和过程）。用 `python scripts/dsh_bridge.py prune --days 14` 清理。

## License

MIT, see [LICENSE](../LICENSE).
