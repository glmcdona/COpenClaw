# COpenClaw 🦀

**Vibe code from your phone. Manage git repos from the bus. Build entire applications while you sleep.**

COpenClaw puts the full power of [GitHub Copilot CLI](https://docs.github.com/en/copilot/github-copilot-in-the-cli) — an agentic AI that can write code, edit files, run shell commands, manage git, create pull requests, and build whole applications from natural language — at your fingertips through the chat apps you already use.

Send a message from Telegram, Teams, WhatsApp, Signal, or Slack. COpenClaw forwards it to Copilot CLI running on your machine and returns the result. The AI reads and writes files, executes commands, commits code, pushes branches, opens PRs, and manages multi-step projects — autonomously, in the background, while you go about your day.

Inspired by [OpenClaw](https://github.com/openclaw/openclaw) — but radically simpler, cheaper, vibe-coding focused, and self-improving design.

---

## What can you do from your phone?

Every capability of Copilot CLI is available through your chat app. Some examples:

| You type in Telegram | What happens on your machine |
|---|---|
| `Build me a Flask API with user auth and deploy it` | Copilot CLI creates the project, writes all files, installs dependencies, runs it |
| `Commit these changes and push a PR to main` | Git add, commit, push, and PR creation — all automated |
| `Fix the bug in issue #42` | Agent reads the issue, checks out a branch, makes the fix, pushes a PR |
| `Show me this week's commits and summarize them` | Git log analysis and natural language summary |
| `Refactor the database layer to use async` | Multi-file code refactoring across the project |
| `Run the tests and fix any failures` | Executes test suite, reads errors, edits code, re-runs until green |
| `Create a GitHub Actions workflow for CI` | Writes the workflow YAML, commits, pushes, creates PR |
| `What changed in PR #57? Any problems?` | Reads the PR diff and reports issues |

Copilot CLI handles **all AI reasoning, code generation, file editing, shell execution, and git operations**. COpenClaw handles everything else: routing messages, managing sessions, dispatching background tasks, scheduling jobs, and keeping an audit trail. The result is a remarkably small codebase (~3,000 lines of Python) that delivers a full autonomous agent.

---

## Why COpenClaw?

### Copilot CLI does the heavy lifting

Copilot CLI is not just a chatbot — it's a **full agentic AI** that can read and write files, execute shell commands, manage git repositories, interact with GitHub (issues, PRs, Actions), and build entire applications from scratch. It handles multi-turn conversations with automatic context compression for virtually infinite sessions. COpenClaw simply makes all of this remote-controllable.

### No web interface = smaller attack surface

COpenClaw has **no dashboard, no web UI, no exposed frontend**. The only interfaces are your existing chat apps and a localhost-only MCP endpoint. There's no public web server to misconfigure, no OAuth flows to get wrong, no session tokens to leak. Chat platforms handle their own authentication natively (Telegram bot tokens, Slack signing secrets, etc.), and COpenClaw just verifies the sender ID against an allowlist.

### Self-improving by design

COpenClaw installs in editable mode and symlinks its own source code into the AI's workspace. The agent can read, edit, and extend COpenClaw itself — then commit the changes and push a PR upstream. You're encouraged to let the agent improve the project and contribute features back for everyone.

### Radically simple

Because Copilot CLI abstracts away all AI complexity (model selection, context management, tool calling, code generation), COpenClaw is just ~3,000 lines of Python glue. Compare that to OpenClaw's TypeScript codebase that must reimplement prompt engineering, model routing, and tool execution from scratch.

### Updates are just `git pull`

Run `copenclaw update` or send `/update` in chat to check for upstream changes and apply them. The agent can even update itself.

---

## Installation

### One-liner install (recommended)

No need to clone the repo first — the installer does it for you:

**Windows** (open Command Prompt or PowerShell):
```cmd
curl -o install.bat https://raw.githubusercontent.com/glmcdona/copenclaw/main/install.bat && .\install.bat
```

**Linux / macOS** (open a terminal):
```bash
curl -fsSL https://raw.githubusercontent.com/glmcdona/copenclaw/main/install.sh -o install.sh && chmod +x install.sh && ./install.sh
```

The installer clones the repo to `~/.copenclaw-src` (or `%USERPROFILE%\.copenclaw-src` on Windows) and installs in **editable mode** — so the AI can modify the source directly and push improvements as PRs.

### Manual install

```bash
git clone https://github.com/glmcdona/copenclaw.git
cd copenclaw
# Windows:
install.bat
# Linux / macOS:
chmod +x install.sh && ./install.sh
```

### What the installer does

1. **Check prerequisites** — Python ≥ 3.10, pip, git
2. **Install GitHub Copilot CLI** — via `winget` (Windows) or `brew` (macOS/Linux), and walk you through auth (`/login`) and model selection (`/model`)
3. **Set up a virtual environment** and install all dependencies (editable mode)
4. **Configure your workspace** — create `~/.copenclaw/` and link in folders you want the bot to access
5. **Detect installed chat apps** — scan for Telegram, WhatsApp, Signal, Teams, Slack
6. **Walk you through channel setup** — prompt for API credentials for each platform
7. **Optionally set up autostart** — Windows Scheduled Task, systemd service (Linux), or LaunchAgent (macOS)
8. **Verify the installation** — quick health check

Re-running the installer will `git pull` to update, then offer to **repair** (rebuild venv) or **reconfigure** (re-run channel setup).

### Updating

From the CLI:
```bash
copenclaw update          # Check for updates + prompt to apply
copenclaw update --apply  # Apply without prompting
```

From chat:
```
/update          # Check for available updates
/update apply    # Pull + reinstall
/restart         # Restart to load new code
```

---

## COpenClaw vs OpenClaw

| | **COpenClaw** | **OpenClaw** |
|---|---|---|
| **AI engine** | GitHub Copilot CLI (full agent) | Anthropic / OpenAI API (raw model calls) |
| **Vibe coding** | ✅ Build entire apps from natural language — Copilot CLI writes all code, creates files, installs deps, runs the app | Partial — requires more manual prompting |
| **Git & GitHub** | ✅ Commit, push, branch, merge, create PRs, work on issues, create Actions workflows — all via natural language | ❌ No built-in git/GitHub integration |
| **Context management** | ✅ Automatic — Copilot CLI compresses context at 95% capacity for virtually infinite sessions | Manual — user must manage context window |
| **Monthly cost** | ~$10–19 (Copilot subscription) | ~$100–200 (Claude Pro/Max recommended) |
| **Codebase size** | ~3,000 lines Python (Copilot CLI handles the hard parts) | ~15,000+ lines TypeScript (reimplements AI tooling) |
| **Web interface** | ❌ None — chat-only, minimal attack surface | ✅ Web dashboard |
| **Self-modification** | ✅ AI can edit its own code and push PRs | ❌ |
| **Install method** | Source clone + pip (editable) | `npm install -g` (packaged binary) |
| **Channels** | Telegram, Teams, WhatsApp, Signal, Slack | WhatsApp, Telegram, Slack, Discord, Signal, iMessage, Teams, + more |
| **Companion apps** | None (chat-only, lightweight) | macOS, iOS, Android native apps |
| **Voice / Canvas** | ❌ | ✅ Voice Wake, Talk Mode, Live Canvas |

**Choose COpenClaw** if you want a lightweight, self-improving agent powered by a Copilot subscription you likely already have, with full git/GitHub integration and vibe coding out of the box, and you value simplicity and a small attack surface over a polished UI.

**Choose OpenClaw** if you want a polished consumer experience with native apps, voice control, a web dashboard, and a large community, and you're willing to pay for Anthropic/OpenAI subscriptions.

---

## GitHub Copilot CLI — The Brain

COpenClaw delegates **all AI reasoning** to [GitHub Copilot CLI](https://docs.github.com/en/copilot/github-copilot-in-the-cli). Every message you send is forwarded to a `copilot` subprocess, and the response comes back through your chat app. This gives you the full power of Copilot CLI remotely:

- **Vibe coding** — describe an application in natural language and Copilot CLI builds it: creates files, writes code, installs dependencies, runs the app, iterates on feedback
- **Git operations** — commit, push, branch, merge, revert, rebase, view history, all via natural language
- **GitHub integration** — create and manage pull requests, work on issues, review PR diffs, create Actions workflows, list and merge PRs
- **File editing** — read, write, and modify any file in the workspace
- **Shell execution** — run arbitrary commands, install packages, start servers, run tests
- **Multi-turn memory** — persistent sessions with automatic context compression (conversations can run indefinitely without hitting token limits)
- **Code analysis** — refactor, debug, explain, optimize code across any language
- **MCP tool use** — COpenClaw exposes 20+ tools (task dispatch, scheduling, messaging, exec, audit) that Copilot CLI calls back into

### Pricing

GitHub Copilot CLI is included with any GitHub Copilot subscription:

| Plan | Price | Notes |
|---|---|---|
| **Copilot Free** | $0/mo | Limited completions, no CLI |
| **Copilot Individual** | $10/mo | Full CLI access, unlimited |
| **Copilot Business** | $19/mo/user | Organization management |
| **Copilot Enterprise** | $39/mo/user | Enterprise features |

Copilot is also **free for verified students, teachers, and open-source maintainers** via [GitHub Education](https://education.github.com/).

**No separate model API keys needed.** You don't need to sign up for Anthropic, OpenAI, or any other model provider. Your GitHub account is the only credential.

---

## The Workspace

When COpenClaw boots, it creates a **workspace directory** (default: `~/.copenclaw/` or the current directory) where the AI lives and works. Think of it as the agent's home folder.

```
~/.copenclaw/                        # Workspace root
├── OwnCode/                         # Symlink → COpenClaw source code
│                                    #   AI can read + edit its own code
├── README.md                        # Persistent project log
│                                    #   Workers update this after tasks
│                                    #   Orchestrator reads it for context
├── .github/
│   └── copilot-instructions.md      # System prompt (auto-deployed)
│
├── .data/                           # Runtime data
│   ├── tasks.json                   # Active task state
│   ├── sessions.json                # Per-user conversation sessions
│   ├── jobs.json                    # Scheduled jobs
│   ├── pairing.json                 # Approved user identities
│   ├── audit.jsonl                  # Append-only audit log
│   └── orchestrator.log             # Orchestrator response log
│
├── .tasks/                          # Per-task worker directories
│   └── task-abc123/                 # Isolated workspace per task
│       ├── .github/
│       │   └── copilot-instructions.md  # Worker system prompt
│       └── (task files...)
│
└── .backups/                        # Automatic source snapshots
    └── 2026-02-12T...Z/             # Timestamped backup
```

**Key concepts:**

- **OwnCode link** — a symlink (or junction on Windows) pointing back to the COpenClaw repository root. The AI can browse and modify its own source code, improve itself, and push PRs upstream.
- **README.md as project log** — The workspace README is seeded on first boot and maintained by workers. After completing a task, workers update the README with what they did. The orchestrator reads it on boot for continuity across restarts.
- **Per-task isolation** — Each background worker gets its own subdirectory under `.tasks/`, with its own Copilot CLI session and system prompt. Workers can't accidentally interfere with each other.
- **Automatic backups** — Before each boot, COpenClaw snapshots the source code to `.backups/` so you can recover from AI self-modifications gone wrong.

---

## ⚠️ Security Warning

> **COpenClaw grants an AI agent FULL ACCESS to your computer.**
> By installing and running this software, you acknowledge and accept the following risks:

| Risk | Description |
|---|---|
| **Remote Control** | Anyone who can message your connected chat channels can execute arbitrary commands on your machine via the AI agent. |
| **Account Takeover = Device Takeover** | If an attacker compromises any of your linked chat accounts, they gain full remote control of this computer. |
| **AI Mistakes** | The AI agent can and will make errors — deleting files, corrupting configs, or running destructive commands — even without malicious intent. |
| **Prompt Injection** | When the agent processes external content (web pages, emails, files), specially crafted inputs can hijack the agent. |
| **Self-Modification Risk** | Because the AI can edit its own code, a bad prompt or injection could alter COpenClaw's behavior permanently. (Mitigated by automatic backups.) |
| **Financial Risk** | If banking apps, crypto wallets, or payment services are accessible from this machine, the agent (or an attacker via the agent) could make unauthorized transactions. |

**Recommendation:** Run COpenClaw inside a **Docker container** or **virtual machine** to limit the blast radius. Never run on a machine with access to sensitive financial accounts or irreplaceable data without isolation.

### Security Model

COpenClaw defaults to a minimal, allowlist-first posture for who can talk to it:

- **No web UI** — chat connectors only.
- **Allowlist only** — only IDs in `*_ALLOW_FROM` can interact; add users manually.
- **Localhost-only MCP** — binds to `127.0.0.1` by default.
- **Execution policy** — allow-all by default with a denylist of dangerous commands; set `COPILOT_CLAW_ALLOW_ALL_COMMANDS=false` for an explicit allowlist.
- **Audit log** — best-effort event log (messages, execs, jobs, tasks); not a complete security record.
- **Risk acceptance required** — you must type `I AGREE` (or use `--accept-risks`).

**YOU USE THIS SOFTWARE ENTIRELY AT YOUR OWN RISK.**

---

## Architecture

```
 Chat Channels              COpenClaw Gateway                    AI Engine
───────────────         ─────────────────────────          ──────────────────

 Telegram  ───┐         ┌────────────────────────┐         Copilot CLI
 Teams     ───┤         │                        │        ┌──────────────┐
 WhatsApp  ───┼────────▶│  gateway.py             │───────▶│ Orchestrator │
 Signal    ───┤  HTTP/   │    ├─ router.py         │◀───────│ (persistent  │
 Slack     ───┘  poll    │    ├─ scheduler.py      │  MCP   │  session)    │
                         │    ├─ task_manager       │        └──────┬───────┘
                         │    ├─ worker_pool        │               │
 ┌──────────┐            │    ├─ policy.py          │        tasks_create
 │ Copilot  │            │    ├─ audit.py           │               │
 │ CLI      │───────────▶│    └─ mcp/protocol.py    │        ┌──────▼───────┐
 │ (MCP     │◀───────────│       (20+ tools)        │        │   Worker     │
 │  client) │   JSON-RPC │                          │        │ (background  │
 └──────────┘            └────────────────────────┘        │  CLI session)│
                                    │                       └──────┬───────┘
                              ┌─────▼──────┐                       │
                              │  Workspace  │                ┌─────▼────────┐
                              │ ~/.copenclaw│                │  Supervisor   │
                              │             │                │  (periodic    │
                              │ OwnCode/ ──┼── source code  │   check-ins)  │
                              │ README.md   │                └──────────────┘
                              │ .tasks/     │
                              │ .data/      │
                              │ .backups/   │
                              └─────────────┘
```

### 3-Tier Task Dispatch

COpenClaw uses a **3-tier autonomous task architecture**:

| Tier | Role | Session | Key Tools |
|---|---|---|---|
| **Orchestrator** | User-facing brain. Routes messages, proposes tasks | Persistent, resumes across restarts | `tasks_create`, `tasks_list`, `send_message`, `jobs_schedule` |
| **Worker** | Executes a task autonomously in a background thread | Per-task, isolated workspace | `task_report`, `task_check_inbox`, `task_set_status`, `files_read` |
| **Supervisor** | Periodically checks on worker, intervenes if stuck | Per-task | `task_read_peer`, `task_send_input`, `task_report` |

**Bidirectional ITC (Inter-Tier Communication):**

```
                    ┌──────────────┐
     tasks_send ──▶ │    INBOX     │ ──▶ task_check_inbox
  (instruction,     │  (per task)  │     (worker reads)
   input, pause,    └──────────────┘
   resume, cancel,
   redirect)        ┌──────────────┐
                    │   OUTBOX     │ ◀── task_report
  tasks_status ◀── │  (per task)  │     (progress, completed,
  tasks_logs   ◀── │  + timeline  │      failed, needs_input,
                    └──────────────┘      artifact, escalation)
```

**Lifecycle:** `proposed` → `pending` → `running` → `completed` / `failed` / `cancelled`
With intermediate states: `paused`, `needs_input`, `recovery_pending`

---

## Features

| Feature | Description |
|---|---|
| **Vibe coding from chat** | Describe an app in natural language — Copilot CLI builds it, writes files, installs deps, runs it |
| **Git & GitHub from chat** | Commit, push, create PRs, work on issues, review diffs, create CI workflows — all via messages |
| **Autonomous task dispatch** | Spawn background worker sessions with automatic supervisor monitoring and bidirectional messaging |
| **Self-improving** | The AI can read, edit, and extend COpenClaw's own source code — then commit and push PRs upstream |
| **Shell execution** | `/exec <command>` runs shell commands (governed by an allowlist policy) |
| **Scheduled jobs** | One-shot or cron-recurring jobs delivered to your chat channel on schedule |
| **MCP server** | Exposes 20+ tools (tasks, jobs, exec, messaging, audit) that Copilot CLI calls back into |
| **No web UI** | No dashboard to expose — chat connectors only, minimal attack surface |
| **Allowlist auth** | Allowlist-only — only IDs in `*_ALLOW_FROM` can interact |
| **Audit log** | Best-effort event log in `audit.jsonl` (messages, execs, jobs, tasks) |
| **Task watchdog** | Automatic detection and recovery of stuck workers (warn → restart → escalate) |
| **Telegram images** | Receive and send images via Telegram |
| **Infinite sessions** | Copilot CLI auto-compresses context at 95% capacity — conversations run indefinitely |
| **Self-update** | Check for and apply updates via CLI (`copenclaw update`) or chat (`/update`) |
| **Automatic backups** | Source snapshots before each boot for rollback safety |

---

## Quick start

1. Run the installer from the **Installation** section above.
2. When prompted, pick a chat channel (Telegram, Slack, Teams, WhatsApp, Signal) and paste the token or credentials. The installer will walk you through pairing by asking you to send a message and will update `.env` for you.
3. Start COpenClaw:
   ```bash
   copenclaw serve
   ```
   If you enabled autostart, it's already running.
   On boot, COpenClaw sends a command console snapshot to your owner chat with system status, tasks, recent logs, and a command input hint.

### Manual channel setup (optional): Telegram

If you skipped the installer prompts or want to reconfigure later, use `python scripts/configure.py` or follow the steps below.

Telegram is the simplest channel — it works via **long-polling** so no public URL or webhook is needed. Your bot runs entirely behind your firewall.

**Step 1: Create the bot**

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`, choose a name and username
3. Copy the bot token (looks like `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`)
4. Add it to your `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
   ```

**Step 2: Pair your account (interactive — recommended)**

Run the interactive configurator:
```bash
python scripts/configure.py
```
Select **Telegram**, enter your bot token, then the script detects your account:
- It polls the Telegram API for incoming messages (up to 120 seconds)
- **Send any message to your bot** from the Telegram app (e.g. "hello")
- The script shows who sent the message and asks you to confirm
- On confirmation, it automatically sets `TELEGRAM_OWNER_CHAT_ID` and `TELEGRAM_ALLOW_FROM` in `.env`
- Your bot sends back "✅ You're now paired as the owner"

**Step 2 (alternative): Manual setup**

If you know your Telegram user ID, set it directly in `.env`:
```
TELEGRAM_OWNER_CHAT_ID=123456789
TELEGRAM_ALLOW_FROM=123456789
```
Only IDs in `TELEGRAM_ALLOW_FROM` are authorized — add yourself there.

**Step 3: Start copenclaw**

```bash
copenclaw serve
```
COpenClaw automatically deletes any existing webhook and starts long-polling. No public URL needed.

> **Webhook mode** (optional): If you prefer webhooks, set `TELEGRAM_WEBHOOK_SECRET` in `.env` and point Telegram to `https://<your-host>/telegram/webhook`. Add the secret as the `X-Telegram-Bot-Api-Secret-Token` header.

### Adding other users

When someone new messages the bot, COpenClaw rejects the request and replies with their user ID plus instructions to add it to `.env`.

To authorize a new user, add their ID to the appropriate `*_ALLOW_FROM` variable in `.env` and restart:
```
TELEGRAM_ALLOW_FROM=123456789,987654321
```
The user is then permanently authorized.

### Other channels

<details>
<summary><strong>Microsoft Teams</strong></summary>

Teams requires an Azure Bot registration (free tier available) and a public HTTPS endpoint. Local-only Teams app integrations (deep links/protocol handlers) do **not** support inbound bot messages.

**Optional: auto-provision everything (admin required)**

If you have tenant admin + Azure subscription privileges, COpenClaw can create the app registration, bot resource, Teams channel, and a Teams app package for you:

```bash
copenclaw teams-setup \
  --messaging-endpoint https://<your-public-host>/teams/api/messages \
  --write-env .env
```

Provide admin credentials via env vars (or flags): `MSTEAMS_ADMIN_TENANT_ID`, `MSTEAMS_ADMIN_CLIENT_ID`, `MSTEAMS_ADMIN_CLIENT_SECRET`, `MSTEAMS_AZURE_SUBSCRIPTION_ID`, `MSTEAMS_AZURE_RESOURCE_GROUP`. The command generates a Teams app package `.zip` and prints the bot credentials. Upload the package in the Teams admin center (or pass `--publish` to auto-publish if allowed).

If you set these env vars during install and keep `MSTEAMS_AUTO_SETUP` unset/true, the installer will auto-run `teams-setup` and write the bot credentials into `.env` for you.

1. Go to [Azure Portal](https://portal.azure.com/) → **Bot Services** → **Create**
2. Note your **App ID**, **App Password**, and **Tenant ID**
3. Set the messaging endpoint to: `https://<your-public-host>/teams/api/messages` (use ngrok or Tailscale Funnel for local dev)
4. Add to `.env`:
   ```
   MSTEAMS_APP_ID=<App ID>
   MSTEAMS_APP_PASSWORD=<App Password>
   MSTEAMS_TENANT_ID=<Tenant ID>
   MSTEAMS_ALLOW_FROM=<comma-separated user IDs, or leave blank>
   ```

> **Note:** Set `MSTEAMS_VALIDATE_TOKEN=false` for local testing if you cannot validate Azure JWT tokens.
</details>

<details>
<summary><strong>WhatsApp</strong></summary>

Uses the [WhatsApp Business Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api). Requires a Meta developer account (free tier available with test numbers).

1. Go to [Meta for Developers](https://developers.facebook.com/apps/) → Create App → add **WhatsApp** product
2. In the WhatsApp dashboard, note your **Phone Number ID** and generate a **permanent access token**
3. Add to `.env`:
   ```
   WHATSAPP_PHONE_NUMBER_ID=123456789012345
   WHATSAPP_ACCESS_TOKEN=EAABs...
   WHATSAPP_VERIFY_TOKEN=my-random-secret
   WHATSAPP_ALLOW_FROM=1234567890,0987654321
   ```
   `WHATSAPP_ALLOW_FROM` uses E.164 phone numbers **without the leading `+`**.
4. Configure webhook in Meta dashboard:
   - **Callback URL:** `https://<your-public-host>/whatsapp/webhook`
   - **Verify Token:** same value as `WHATSAPP_VERIFY_TOKEN`
   - Subscribe to the `messages` webhook field
5. WhatsApp **requires a publicly accessible HTTPS endpoint**
</details>

<details>
<summary><strong>Signal</strong></summary>

Signal connects via [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api), which wraps Signal's desktop protocol. Local-only; no public URL needed.

1. Run signal-cli-rest-api:
   ```bash
   docker run -d --name signal-api -p 8080:8080 \
     -v signal-cli:/home/.local/share/signal-cli \
     bbernhard/signal-cli-rest-api
   ```
2. Register or link a phone number with signal-cli (see [signal-cli docs](https://github.com/bbernhard/signal-cli-rest-api#readme))
3. Add to `.env`:
   ```
   SIGNAL_API_URL=http://localhost:8080
   SIGNAL_PHONE_NUMBER=+1234567890
   SIGNAL_ALLOW_FROM=+1234567890,+0987654321
   ```
   Phone numbers are in E.164 format (with `+`).
4. Optional sanity check: `curl http://localhost:8080/v1/about`

**Interactive setup:** `python scripts/configure.py` supports Signal pairing — send a message to your Signal number and the script will detect the sender and offer to authorize them.
</details>

<details>
<summary><strong>Slack</strong></summary>

Uses the [Slack Web API](https://api.slack.com/web) + [Events API](https://api.slack.com/events-api). Requires a Slack App with Socket Mode or event subscriptions.

1. Go to [Slack API](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Under **OAuth & Permissions**, add bot scopes: `chat:write`, `files:write`, `im:history`, `channels:history`
3. Install the app to your workspace and copy the **Bot User OAuth Token** (`xoxb-...`)
4. Under **Basic Information**, copy the **Signing Secret**
5. Set **Event Subscriptions** URL to: `https://<your-public-host>/slack/events`
   - Subscribe to bot events: `message.im`, `message.channels`
6. Add to `.env`:
   ```
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_SIGNING_SECRET=abc123...
   SLACK_ALLOW_FROM=U01ABC123,U02DEF456
   ```
   `SLACK_ALLOW_FROM` uses Slack user IDs (find via **Profile → More → Copy member ID**).
7. Slack **requires a publicly accessible HTTPS endpoint** for event subscriptions
</details>

### Connect Copilot CLI to COpenClaw via MCP

Inside a Copilot CLI session:
```
/mcp add
```
- **Name**: `copenclaw`, **Type**: `http`, **URL**: `http://127.0.0.1:18790/mcp`

Or add to `~/.copilot/mcp-config.json`:
```json
{
  "mcpServers": {
    "copenclaw": {
      "type": "http",
      "url": "http://127.0.0.1:18790/mcp"
    }
  }
}
```

---

## Chat commands

| Command | Description |
|---|---|
| `/status` | Server health + task/job summary |
| `/whoami` | Your channel:sender_id |
| `/help` | List all commands |
| `/tasks` | List active tasks |
| `/task <id>` | Detailed task status + timeline |
| `/proposed` | List proposals awaiting approval |
| `/logs <id>` | Recent worker output |
| `/cancel <id>` | Cancel a task or job |
| `/jobs` | List scheduled jobs |
| `/job <id>` | Job details |
| `/exec <cmd>` | Run a shell command (policy-gated) |
| `/update` | Check for code updates |
| `/update apply` | Apply available update |
| `/restart [reason]` | Restart COpenClaw |
| Free text | Forwarded to Copilot CLI |

---

## MCP tools

### Infrastructure tools

| Tool | Description |
|---|---|
| `jobs_schedule` | Schedule a one-shot or cron job |
| `jobs_list` | List all jobs |
| `jobs_runs` | Job execution history |
| `jobs_cancel` | Cancel a job |
| `send_message` | Send a message to any channel |
| `files_read` | Read a file under data_dir |
| `audit_read` | Read audit log entries |

### Task dispatch tools (orchestrator)

| Tool | Description |
|---|---|
| `tasks_create` | Create and dispatch a background task |
| `tasks_list` | List tasks with status |
| `tasks_status` | Detailed status with timeline |
| `tasks_logs` | Raw worker session logs |
| `tasks_send` | Send instruction/input/pause/resume/cancel to worker |
| `tasks_cancel` | Cancel a running task |

### Task ITC tools (worker/supervisor)

| Tool | Description |
|---|---|
| `task_report` | Report progress/completion/failure upward |
| `task_check_inbox` | Check for instructions from orchestrator/supervisor |
| `task_set_status` | Update task status |
| `task_get_context` | Read original task prompt + recent messages |
| `task_read_peer` | Read worker logs (supervisor only) |
| `task_send_input` | Send guidance to worker (supervisor only) |

---

## Configuration

All configuration is via environment variables (or `.env` file). See [`.env.example`](.env.example) for the full list.

| Variable | Default | Description |
|---|---|---|
| `COPILOT_CLAW_DATA_DIR` | `.data` | Directory for jobs, sessions, tasks, audit log |
| `COPILOT_CLAW_WORKSPACE_DIR` | `.` | Working directory for Copilot CLI |
| `COPILOT_CLAW_CLI_TIMEOUT` | `7200` | Copilot CLI subprocess timeout (seconds) |
| `COPILOT_CLAW_MCP_TOKEN` | *(empty)* | Bearer token to protect MCP endpoints |
| `COPILOT_CLAW_ALLOW_ALL_COMMANDS` | `true` | Allow all shell commands (set false to use `COPILOT_CLAW_ALLOWED_COMMANDS`) |
| `COPILOT_CLAW_ALLOWED_COMMANDS` | *(empty)* | Comma-separated allowlist (when above is false) |
| `COPILOT_CLAW_DENIED_COMMANDS` | `shutdown,reboot,format` | Always-blocked commands |
| `COPILOT_CLAW_EXEC_TIMEOUT` | `300` | Max seconds per `/exec` command |
| `COPILOT_CLAW_HOST` | `127.0.0.1` | Bind address |
| `COPILOT_CLAW_PORT` | `18790` | Bind port |
| `COPILOT_CLAW_TASK_WATCHDOG_INTERVAL` | `60` | Seconds between watchdog checks |
| `COPILOT_CLAW_TASK_WATCHDOG_GRACE_SECONDS` | `600` | Grace period before watchdog actions |
| `COPILOT_CLAW_TASK_WATCHDOG_IDLE_WARN_SECONDS` | `1800` | Idle seconds before warning |
| `COPILOT_CLAW_TASK_WATCHDOG_IDLE_RESTART_SECONDS` | `3600` | Idle seconds before auto-restart |
| `COPILOT_CLAW_TASK_WATCHDOG_MAX_RESTARTS` | `1` | Max restarts before escalation |

---

## Testing

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

265 tests covering: health, MCP protocol, scheduler, routing, policy, audit, task dispatch lifecycle, self-update, and end-to-end worker flows.

---

## Project structure

```
copenclaw/
├── src/copenclaw/
│   ├── cli.py                 # Typer CLI entry point
│   ├── core/
│   │   ├── audit.py           # Append-only JSONL audit log
│   │   ├── backup.py          # Automatic source snapshots
│   │   ├── config.py          # Settings from env / .env
│   │   ├── disclaimer.py      # Security disclaimer + risk gate
│   │   ├── gateway.py         # FastAPI app factory + webhooks
│   │   ├── logging_config.py  # Centralized logging setup
│   │   ├── names.py           # Random name generator for tasks
│   │   ├── pairing.py         # User allowlist store
│   │   ├── policy.py          # Execution policy (allowlist)
│   │   ├── rate_limit.py      # Sliding-window rate limiter
│   │   ├── router.py          # Unified chat command router
│   │   ├── scheduler.py       # Job scheduler with cron support
│   │   ├── session.py         # Per-user session store
│   │   ├── task_events.py     # Task event stream
│   │   ├── tasks.py           # Task + TaskMessage + TaskManager
│   │   ├── templates.py       # System prompt template loader
│   │   ├── updater.py         # Git-based self-update system
│   │   └── worker.py          # WorkerThread + SupervisorThread + Pool
│   ├── integrations/
│   │   ├── copilot_cli.py     # Copilot CLI subprocess adapter
│   │   ├── signal.py          # Signal adapter (signal-cli-rest-api)
│   │   ├── slack.py           # Slack Web API + Events API
│   │   ├── teams.py           # Teams Bot Framework adapter
│   │   ├── teams_auth.py      # Teams JWT validation
│   │   ├── telegram.py        # Telegram Bot API (polling + webhook)
│   │   └── whatsapp.py        # WhatsApp Business Cloud API
│   └── mcp/
│       ├── protocol.py        # MCP JSON-RPC handler (20+ tools)
│       └── server.py          # MCP REST sub-router
├── templates/
│   ├── orchestrator.md        # Orchestrator system prompt
│   ├── worker.md              # Worker system prompt
│   └── supervisor.md          # Supervisor system prompt
├── tests/                     # 265 tests
├── scripts/
│   ├── configure.py           # Interactive channel configurator
│   └── start-windows.ps1      # Windows startup helper
├── install.bat                # Windows installer
├── install.sh                 # Linux/macOS installer
├── .env.example
├── pyproject.toml
└── README.md
```

---

## License

MIT
