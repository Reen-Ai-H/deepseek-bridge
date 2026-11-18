# deepseek-bridge

**让你的 AI 编程助手当"监工"，把活交给更便宜的 DeepSeek 去干，并替你检查有没有干好。**

[English](#english)

---

## 这是什么

你可能已经在用 Claude Code、Codex、Cursor 这类 AI 编程助手。它们很聪明，但用得多了不便宜。

DeepSeek 很便宜，也能写代码、跑命令、改文件（通过 DeepSeek 官方的 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)，简称 dsh）。但它单独干活时质量不太稳定：常常还没做完就说"做完了"，或者把问题写进说明里就交差。

**deepseek-bridge 把两者连起来：**

- 🧑‍💼 **你的 AI 助手当监工**：把你的需求写成清楚的任务单，分阶段派给 DeepSeek，再亲自检查结果。
- 👷 **DeepSeek 当工人**：在你的电脑上真正动手干活。
- 🔁 **没做好就返工**：监工会指出具体哪里不对、要改成什么样、怎么算合格，直到达标为止。

你只需要像平时一样说一句话，比如：

> "让 DeepSeek 给这个项目加一个导出 Excel 的功能，做完你帮我检查。"

## 能带来什么

| | 效果 |
|---|---|
| 💰 **更省钱** | 大量的动手工作交给 DeepSeek，贵的 AI 助手只负责安排和检查。 |
| ✅ **质量更稳** | 每一轮都有独立检查：跑测试、看改了哪些文件、看 DeepSeek 实际执行了什么，而不是只听它说"完成了"。 |
| 🕐 **可以干长活** | 任务在后台运行，几十分钟甚至几小时都行。你的助手可以先忙别的，回头再来验收。 |
| 👀 **全程看得见** | 每一步都有记录；DeepSeek 自己的网页界面里也能看到同一段对话。 |
| 🩺 **帮你诊断** | 以前和 DeepSeek 的对话效果不好？让助手读一遍记录，告诉你问题出在哪，并帮你重写需求。 |

## 一句话安装：把下面这段话发给你的 AI 助手

复制下面整段，粘贴给你的 AI 编程助手（Claude Code、Codex、Cursor 等），它会帮你装好、配好、测好。

```
请帮我安装并配置 deepseek-bridge：https://github.com/Reen-Ai-H/deepseek-bridge

目标：让你（我的 AI 编程助手）能把任务派给我电脑上的 DeepSeek Harness（dsh）去执行，并由你来检查结果。

请按顺序做：
1. 先读仓库里的 README.md 和 docs/manual.md。
2. 检查环境：Node.js ≥ 22.19、Python ≥ 3.9、git。缺什么，先告诉我要装什么、怎么装，我同意后再装。
3. 如果我还没装 DeepSeek Harness，就帮我安装（npm i -g @deepseek-ai/dsh），然后让我运行 `dsh web`，在网页的设置里自己填 DeepSeek API Key。不要让我把 Key 发给你。
4. 把仓库 clone 到合适的位置，把 mcp/server.py 注册成你的 MCP 服务器，再把整个仓库文件夹装成你的 skill（链接或复制到你的 skills 目录）。
5. 运行 python tests/run_tests.py，确认测试全部通过。
6. 运行 python scripts/dsh_bridge.py models --cwd <一个临时目录>，确认能连上 dsh；再派一个简单的测试任务（比如让 DeepSeek 在临时目录里创建一个文件），确认整条链路能用。
7. 告诉我结果：装在了哪里、怎么用、我接下来可以对你说什么。

如果中间出现问题，请先尝试自己修复。如果问题出在 deepseek-bridge 本身（不是我电脑环境的问题），修好之后请尽可能帮我把修改整理成一个 Pull Request，提交到 https://github.com/Reen-Ai-H/deepseek-bridge（先 fork，再提 PR，写清楚遇到了什么问题、怎么复现、改了什么），方便作者改进这个项目。提交前先告诉我，征得我同意。
```

## 装好之后，你可以这样说

- "让 DeepSeek 把这个 bug 修了，修完你跑一遍测试确认。"
- "把这个需求交给 DeepSeek，分两步做，第一步做完先给我看。"
- "看看 DeepSeek 刚才做到哪了。"
- "我之前在 DeepSeek 里做的那个项目效果很差，帮我看看对话记录，问题出在哪？"
- "这个任务用 DeepSeek 最强的模型做。"

## 你需要准备什么

- 一个 **DeepSeek API Key**（在 [DeepSeek 开放平台](https://platform.deepseek.com) 申请）。
- 一个支持 MCP 的 AI 编程助手（Claude Code、Codex、Cursor 等）。
- 电脑上有 Node.js 22.19 以上和 Python 3.9 以上。没有也没关系，安装提示词会让助手先问你再装。

## 安全吗？

- DeepSeek 会在你的电脑上运行命令、修改文件，能做多少由你在 dsh 里选的权限决定。
- 默认情况下，DeepSeek 想做超出工作目录范围的事（比如改别的文件夹、联网），都会被**自动拒绝**。只有你明确同意，才会放行，而且每一次放行都有记录。
- API Key 只填在 dsh 自己的设置里，不经过这个项目，也不需要发给 AI 助手。
- 建议在 git 管理的项目里使用。改了什么一目了然，不满意可以随时撤回。

## 常见问题

**会花多少钱？** DeepSeek 按它的官方价格计费，通常比主流大模型便宜很多。你的 AI 助手只负责写任务和检查，用量比它自己从头做要少。

**支持哪些 AI 助手？** 任何支持 MCP 的都可以；Claude Code 还能额外装上 skill，学会"怎么当好监工"。

**能看到 DeepSeek 在干什么吗？** 能。打开 dsh 的网页（`dsh web`），会看到同一段对话；每个任务的完整过程也保存在本地。

**跑到一半想停？** 跟你的助手说"停下来"就行，它会让 DeepSeek 取消当前任务。

## 参与改进

遇到问题、有想法，欢迎提 [Issue](https://github.com/Reen-Ai-H/deepseek-bridge/issues) 或 Pull Request。上面那段安装提示词也会让你的 AI 助手在修好问题后，尽量帮你提一个 PR 回来。

想了解原理、命令行用法、配置项和测试，请看 [docs/manual.md](docs/manual.md)。

---

## English

**Let your AI coding assistant act as the supervisor: hand the work to cheaper DeepSeek, and have the assistant check that it was done right.**

AI coding assistants (Claude Code, Codex, Cursor, …) are smart but costly at scale. DeepSeek is cheap and can write code, run commands and edit files through its official [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`). Left alone, though, it often says "done" too early. **deepseek-bridge connects the two:**

- your assistant writes a clear brief and dispatches it to DeepSeek, one phase at a time;
- DeepSeek does the work on your machine;
- your assistant verifies the result itself (tests, changed files, the commands DeepSeek actually ran) and sends precise feedback until it passes.

**What you get:**
- **Lower cost:** the expensive model only plans and reviews.
- **Steadier quality:** every round is independently checked.
- **Long tasks:** they run in the background.
- **Visibility:** the full record is saved, and the same conversation appears in dsh's web UI.
- **Diagnosis:** your assistant can read past DeepSeek conversations to find what went wrong.

### One-message install: paste this to your AI assistant

```
Please install and set up deepseek-bridge: https://github.com/Reen-Ai-H/deepseek-bridge

Goal: let you (my AI coding assistant) delegate tasks to the DeepSeek Harness (dsh) on my computer and verify the results yourself.

Steps:
1. Read README.md and docs/manual.md in the repo.
2. Check for Node.js >= 22.19, Python >= 3.9 and git. If anything is missing, tell me what you want to install and how, and wait for my OK.
3. If DeepSeek Harness is not installed, install it (npm i -g @deepseek-ai/dsh), then ask me to run `dsh web` and enter my DeepSeek API key in its settings myself. Never ask me to send you the key.
4. Clone the repo, register mcp/server.py as your MCP server, and install the repo folder as your skill (link or copy it into your skills directory).
5. Run python tests/run_tests.py and make sure everything passes.
6. Run python scripts/dsh_bridge.py models --cwd <a temp dir> to confirm dsh is reachable, then dispatch a tiny test task (e.g. create a file in a temp dir) to confirm the whole loop works.
7. Tell me where it is installed, how to use it, and what I can say to you next.

If something goes wrong, try to fix it yourself first. If the problem is in deepseek-bridge itself (not my machine's setup), please package your fix as a Pull Request to https://github.com/Reen-Ai-H/deepseek-bridge whenever possible (fork, then open a PR describing the problem, how to reproduce it, and what you changed) so the author can improve the project. Ask me before submitting it.
```

**Then just say things like:** "Have DeepSeek fix this bug and run the tests to confirm," or "Hand this to DeepSeek in two phases; show me phase one first."

**Safety:** by default, anything DeepSeek tries outside its sandbox (other folders, network) is automatically denied, and allowing it is your explicit choice, with every decision logged. Your API key stays in dsh's own settings. Use it in a git repo so every change is visible and reversible.

Technical manual (how it works, CLI, MCP tools, configuration, tests): [docs/manual.md](docs/manual.md). Issues and PRs are welcome.

## License

MIT
