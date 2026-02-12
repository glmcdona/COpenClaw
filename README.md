# copenclaw 🦀

Remote-control your desktop through **GitHub Copilot CLI** via Telegram, Microsoft Teams, WhatsApp, Signal, Slack, or the built-in MCP server.
Inspired by [openclaw](https://github.com/nichochar/openclaw) — but powered by GitHub Copilot CLI instead.

## ⚠️ Security Warning

> **copenclaw grants an AI agent FULL ACCESS to your computer.**
> By installing and running this software, you acknowledge and accept the following risks:

| Risk | Description |
|---|---|
| **Remote Control** | Anyone who can message your connected chat channels (Telegram, WhatsApp, Signal, Teams, Slack) can execute arbitrary commands on your machine. |
| **Account Takeover = Device Takeover** | If an attacker compromises any of your linked chat accounts, they gain full remote control of this computer through copenclaw. |
| **AI Mistakes** | The AI agent can and will make errors. It may delete files, wipe data, corrupt configurations, or execute destructive commands — even without malicious intent. |
| **Prompt Injection** | When the agent browses the web, reads emails, or processes external content, specially crafted inputs can hijack the agent and take control of your system. |
| **Malicious Tools** | The agent may autonomously download and install MCP servers or other tools from untrusted sources, which could contain malware or exfiltrate your data. |
| **Financial Risk** | If you have banking apps, crypto wallets, payment services, or trading platforms accessible from this machine, the agent (or an attacker via the agent) could make unauthorized transactions, transfers, or purchases on your behalf. |

**Recommendation:** Run copenclaw inside a **Docker container** or **virtual machine** to limit the blast radius of any incident. Never run on a machine with access to sensitive financial accounts or irreplaceable data without appropriate isolation.

**YOU USE THIS SOFTWARE ENTIRELY AT YOUR OWN RISK.**

Both the installer and the application itself require you to explicitly type `I AGREE` before proceeding. For headless/container deployments, use `copenclaw serve --accept-risks`.

---

## Features

| Feature | Description |
|---|---|
| **Chat → Copilot CLI** | Send a message from Telegram, Teams, WhatsApp, Signal, or Slack and copenclaw forwards it to `copilot` CLI, returning the answer |
| **Telegram image support** | Receive images from Telegram (saved under `data_dir/telegram_uploads`) and send images back via MCP |
| **Autonomous task dispatch** | Spawn background worker Copilot CLI sessions with automatic supervisor monitoring and bidirectional ITC |
| **Shell execution** | `/exec <command>` runs shell commands (governed by an allowlist policy) |
| **Scheduled jobs** | One-shot or cron-recurring jobs delivered to your chat channel on schedule |
| **MCP server** | Exposes 22 tools (tasks, jobs, exec, messaging, audit, pairing) that Copilot CLI can call directly |
| **3-tier architecture** | Orchestrator → Worker → Supervisor with message passing, progress tracking, and auto-escalation |
| **Pairing / allowlist** | Three auth modes — `open`, `allowlist`, `pairing` (approve-by-code) |
| **Audit log** | Every action (messages, execs, jobs, tasks, pairing) is appended to `audit.jsonl` with request IDs |
| **Rate limiting** | Per-channel webhook rate limiting (configurable) |
| **Session memory** | Tracks per-user conversation context across messages |

## Installation

### Windows

```powershell
git clone https://github.com/your-org/copenclaw.git
cd copenclaw
.\install.ps1
```

### Linux / macOS

```bash
git clone https://github.com/your-org/copenclaw.git
cd copenclaw
chmod +x install.sh
./install.sh
```

The installer will:

1. **Check prerequisites** — Python ≥ 3.10, pip, git
2. **Install GitHub Copilot CLI** — via `winget` (Windows) or `brew` (macOS/Linux), and walk you through authentication (`/login`) and model selection (`/model`)
3. **Set up a virtual environment** and install all dependencies
4. **Configure your workspace** — create `~/.copenclaw/` and link in any folders (repos, documents) you want the bot to access
5. **Detect installed chat apps** — scans for Telegram, WhatsApp, Signal, Teams, and Slack on your system
6. **Walk you through channel setup** — prompts for API credentials for each chat platform you want to enable
7. **Optionally set up autostart on boot** — Windows Scheduled Task, systemd service (Linux), or LaunchAgent (macOS)
8. **Verify the installation** — quick health check to confirm everything works

If copenclaw is already installed, the script detects it and offers to **repair** (rebuild venv) or **reconfigure** (re-run channel setup).

To reconfigure channels later without reinstalling:

```bash
python scripts/configure.py             # full reconfiguration
python scripts/configure.py --reconfigure   # channels only
```

## Quick start

```bash
cd copenclaw
cp .env.example .env          # edit with your tokens
pip install -e ".[dev]"
copenclaw serve             # starts on 127.0.0.1:18790
```

### Expose to Telegram

1. Create a bot via [@BotFather](https://t.me/BotFather), copy the token to `TELEGRAM_BOT_TOKEN`
2. Set your webhook: `https://<host>/telegram/webhook`
3. Optionally set `TELEGRAM_WEBHOOK_SECRET` and `TELEGRAM_ALLOW_FROM`
4. Image uploads are stored in `copenclaw_DATA_DIR/telegram_uploads` (sent images can use MCP `send_message` with `image_path`)

### Expose to Microsoft Teams

1. Register a Bot in Azure, copy App ID / Password / Tenant ID
2. Set the messaging endpoint to `https://<host>/teams/api/messages`

### Expose to WhatsApp

copenclaw uses the [WhatsApp Business Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api) (via Meta's `graph.facebook.com`).

1. Create a Meta App at [developers.facebook.com](https://developers.facebook.com/apps/) and add the **WhatsApp** product
2. In the WhatsApp settings, note your **Phone Number ID** and generate a **permanent access token**
3. Set in `.env`:
   ```
   WHATSAPP_PHONE_NUMBER_ID=<your phone number ID>
   WHATSAPP_ACCESS_TOKEN=<your access token>
   WHATSAPP_VERIFY_TOKEN=<any random string you choose>
   WHATSAPP_ALLOW_FROM=<comma-separated phone numbers, E.164 without +>
   ```
4. Configure the webhook in Meta's dashboard:
   - **Callback URL**: `https://<host>/whatsapp/webhook`
   - **Verify token**: the same string you set in `WHATSAPP_VERIFY_TOKEN`
   - Subscribe to the `messages` field
5. Messages from allowed numbers are forwarded to Copilot CLI; replies are sent back via the Cloud API

### Expose to Signal

copenclaw connects to Signal via [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api), a self-hosted REST bridge.

1. Run signal-cli-rest-api (e.g. via Docker):
   ```bash
   docker run -d --name signal-api -p 8080:8080 \
     -v signal-cli-config:/home/.local/share/signal-cli \
     bbernhard/signal-cli-rest-api
   ```
2. Register or link a phone number with signal-cli (see the [signal-cli docs](https://github.com/AsamK/signal-cli/wiki))
3. Set in `.env`:
   ```
   SIGNAL_API_URL=http://localhost:8080
   SIGNAL_PHONE_NUMBER=+1234567890
   SIGNAL_ALLOW_FROM=+1234567890,+0987654321
   ```
4. copenclaw polls `GET /v1/receive/<number>` for incoming messages (no public webhook needed)
5. Replies are sent via `POST /v2/send`

### Expose to Slack

copenclaw uses the [Slack Web API](https://api.slack.com/web) and [Events API](https://api.slack.com/events-api).

1. Create a Slack App at [api.slack.com/apps](https://api.slack.com/apps)
2. Under **OAuth & Permissions**, add these bot token scopes:
   - `chat:write` — send messages
   - `files:write` — upload images
   - `channels:history` / `groups:history` / `im:history` — read messages
3. Install the app to your workspace and copy the **Bot User OAuth Token** (`xoxb-...`)
4. Under **Event Subscriptions**:
   - Enable events
   - Set the **Request URL** to `https://<host>/slack/events`
   - Subscribe to the `message.im` (and optionally `message.channels`) bot event
5. Copy the **Signing Secret** from **Basic Information**
6. Set in `.env`:
   ```
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_SIGNING_SECRET=<your signing secret>
   SLACK_ALLOW_FROM=<comma-separated Slack user IDs>
   ```
7. DM or mention the bot in a channel — messages are forwarded to Copilot CLI and replies posted back

### Connect to Copilot CLI via MCP

**Option 1: Interactive** — Inside a Copilot CLI session, use the interactive slash command:

```
/mcp add
```

Then fill in:
- **Name**: `copenclaw`
- **Type**: `http`
- **URL**: `http://127.0.0.1:18790/mcp`

Press <kbd>Ctrl</kbd>+<kbd>S</kbd> to save.

**Option 2: CLI flag** — Pass the config as a JSON string when starting Copilot:

```bash
copilot --additional-mcp-config '{"mcpServers":{"copenclaw":{"type":"http","url":"http://127.0.0.1:18790/mcp"}}}'
```

**Option 3: Config file** — Add to `~/.copilot/mcp-config.json`:

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

Fetch the ready-made config block from the running server:

```bash
curl http://127.0.0.1:18790/mcp/config
```

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  Telegram   │────▶│                  │────▶│  Copilot CLI     │
│  / Teams    │◀────│                  │◀────│  (orchestrator)  │
│  / WhatsApp │     │   gateway.py     │     └──────────────────┘
│  / Signal   │     │                  │
│  / Slack    │     │                  │
└─────────────┘     │                  │
                    │   router.py      │             │
                    │   scheduler      │      tasks_create
                    │   task_manager   │             │
┌─────────────┐     │   worker_pool    │     ┌──────▼──────────┐
│  MCP client │────▶│   audit.py       │     │  Worker CLI     │──── task_report ────▶
│  (Copilot)  │◀────│   policy.py      │     │  (background)   │◀─── task_check_inbox ─
└─────────────┘     │   protocol.py    │     └──────┬──────────┘
                    └──────────────────┘            │
                                              ┌─────▼──────────┐
                                              │  Supervisor CLI │
                                              │  (periodic)     │
                                              └────────────────┘
```

### 3-Tier Task Dispatch

copenclaw uses a **3-tier autonomous task architecture**:

| Tier | Role | Session | Tools |
|---|---|---|---|
| **Orchestrator** | User-facing brain. Routes messages, dispatches tasks | Persistent | `tasks_create`, `tasks_list`, `tasks_status`, `tasks_send`, `tasks_cancel`, `jobs_*`, `exec_run`, `send_message` |
| **Worker** | Executes a task autonomously in a background thread | Per-task | `task_report`, `task_check_inbox`, `task_set_status`, `task_get_context`, `exec_run`, `files_read` |
| **Supervisor** | Periodically checks on worker, intervenes if stuck | Per-task | `task_read_peer`, `task_send_input`, `task_report`, `task_check_inbox` |

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

**Lifecycle:** `pending` → `running` → `completed` / `failed` / `cancelled`
With intermediate states: `paused`, `needs_input`

## Chat commands

| Command | Description |
|---|---|
| `/status` | Server health + config summary |
| `/whoami` | Your channel:sender_id |
| `/exec <cmd>` | Run a shell command (policy-gated) |
| `/pair approve <code>` | Approve a pending pairing request |
| `/pair list` | List pending pairing codes |
| Free text | Forwarded to Copilot CLI and response returned |

## MCP tools

### Infrastructure tools

| Tool | Description |
|---|---|
| `jobs_schedule` | Schedule a one-shot or cron job |
| `jobs_list` | List all jobs |
| `jobs_runs` | List job execution history |
| `jobs_cancel` | Cancel a job |
| `exec_run` | Execute a command under policy |
| `send_message` | Send a message to Telegram/Teams/WhatsApp/Signal/Slack |
| `files_read` | Read a file under data_dir |
| `audit_read` | Read audit log entries |
| `pairing_pending` | List pending pairing requests |
| `pairing_approve` | Approve a pairing code |

### Task dispatch tools (orchestrator level)

| Tool | Description |
|---|---|
| `tasks_create` | Create and dispatch an autonomous background task |
| `tasks_list` | List all tasks with current status |
| `tasks_status` | Detailed task status with concise timeline |
| `tasks_logs` | Raw worker session logs |
| `tasks_send` | Send instruction/input/redirect/pause/resume/cancel to a worker |
| `tasks_cancel` | Cancel a running task |

### Task ITC tools (worker/supervisor level)

| Tool | Description |
|---|---|
| `task_report` | Report progress/completion/failure/needs_input upward |
| `task_check_inbox` | Check for new instructions from orchestrator/supervisor |
| `task_set_status` | Update task status |
| `task_get_context` | Read original task prompt and recent messages |
| `task_read_peer` | Read worker logs (for supervisors) |
| `task_send_input` | Send guidance from supervisor to worker |

## Configuration

All configuration is via environment variables (or `.env` file). See [`.env.example`](.env.example) for the full list.

Key settings:

| Variable | Default | Description |
|---|---|---|
| `copenclaw_DATA_DIR` | `.data` | Directory for jobs, sessions, tasks, audit log |
| `copenclaw_WORKSPACE_DIR` | `.` | Working directory for Copilot CLI |
| `copenclaw_COPILOT_CLI_TIMEOUT` | `120` | Copilot CLI subprocess timeout (seconds) |
| `copenclaw_PAIRING_MODE` | `pairing` | Auth mode: `open`, `allowlist`, or `pairing` |
| `copenclaw_MCP_TOKEN` | *(empty)* | Bearer token to protect MCP endpoints |
| `copenclaw_ALLOW_ALL_COMMANDS` | `false` | Allow all shell commands (dangerous!) |
| `copenclaw_ALLOWED_COMMANDS` | *(empty)* | Comma-separated allowlist of shell commands |

## Testing

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

100 tests covering: health, MCP protocol, scheduler, routing, policy, audit, and task dispatch lifecycle.

## Project structure

```
copenclaw/
├── src/copenclaw/
│   ├── cli.py                 # Typer CLI entry point
│   ├── core/
│   │   ├── audit.py           # Append-only JSONL audit log
│   │   ├── config.py          # Settings from env / .env
│   │   ├── disclaimer.py      # Security disclaimer + risk-acceptance gate
│   │   ├── gateway.py         # FastAPI app factory + webhook routes
│   │   ├── pairing.py         # Pairing code store
│   │   ├── policy.py          # Execution policy (allowlist / allow-all)
│   │   ├── rate_limit.py      # Sliding-window rate limiter
│   │   ├── router.py          # Unified chat command router
│   │   ├── scheduler.py       # Job scheduler with cron support
│   │   ├── session.py         # Per-user session store
│   │   ├── tasks.py           # Task + TaskMessage + TaskManager (ITC protocol)
│   │   └── worker.py          # WorkerThread + SupervisorThread + WorkerPool
│   ├── integrations/
│   │   ├── copilot_cli.py     # Copilot CLI subprocess adapter
│   │   ├── signal.py          # Signal adapter (via signal-cli-rest-api)
│   │   ├── slack.py           # Slack Web API + Events API adapter
│   │   ├── teams.py           # Teams Bot Framework adapter
│   │   ├── teams_auth.py      # Teams JWT validation
│   │   ├── telegram.py        # Telegram Bot API adapter (polling + webhook)
│   │   └── whatsapp.py        # WhatsApp Business Cloud API adapter
│   └── mcp/
│       ├── __init__.py
│       ├── protocol.py        # MCP JSON-RPC handler (22 tools)
│       └── server.py          # MCP REST sub-router (legacy)
├── tests/
│   ├── test_audit.py
│   ├── test_health.py
│   ├── test_mcp_jobs.py
│   ├── test_policy.py
│   ├── test_router.py
│   ├── test_scheduler.py
│   └── test_tasks.py          # 56 tests for task lifecycle + ITC
├── scripts/
│   ├── configure.py       # Interactive channel & workspace configurator
│   └── start-windows.ps1
├── install.ps1                # Windows installer
├── install.sh                 # Linux/macOS installer
├── .env.example
├── pyproject.toml
├── README.md
└── RUNBOOK.md
```

## License

MIT
