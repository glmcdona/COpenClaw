from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

@dataclass
class Settings:
    log_level: str
    log_dir: str
    data_dir: str
    workspace_dir: str | None
    copilot_cli_timeout: int
    telegram_bot_token: str | None
    telegram_webhook_secret: str | None
    telegram_allow_from: list[str]
    msteams_app_id: str | None
    msteams_app_password: str | None
    msteams_tenant_id: str | None
    msteams_allow_from: list[str]
    whatsapp_phone_number_id: str | None
    whatsapp_access_token: str | None
    whatsapp_verify_token: str | None
    whatsapp_allow_from: list[str]
    signal_api_url: str | None
    signal_phone_number: str | None
    signal_allow_from: list[str]
    slack_bot_token: str | None
    slack_signing_secret: str | None
    slack_allow_from: list[str]
    pairing_mode: str
    pairing_code_length: int
    msteams_validate_token: bool
    webhook_rate_limit_calls: int
    webhook_rate_limit_seconds: int
    telegram_owner_chat_id: str | None
    mcp_token: str | None
    host: str
    port: int
    backup_max_snapshots: int
    clear_logs_on_launch: bool

    @staticmethod
    def from_env() -> "Settings":
        default_workspace = str(Path(os.path.expanduser("~")) / ".githubclaw")
        default_log_dir = str(Path(default_workspace) / ".logs")
        default_data_dir = str(Path(default_workspace) / ".data")
        telegram_allow = os.getenv("TELEGRAM_ALLOW_FROM", "")
        msteams_allow = os.getenv("MSTEAMS_ALLOW_FROM", "")
        whatsapp_allow = os.getenv("WHATSAPP_ALLOW_FROM", "")
        signal_allow = os.getenv("SIGNAL_ALLOW_FROM", "")
        slack_allow = os.getenv("SLACK_ALLOW_FROM", "")
        return Settings(
            log_level=os.getenv("copenclaw_LOG_LEVEL", "info"),
            log_dir=os.getenv("copenclaw_LOG_DIR") or default_log_dir,
            data_dir=os.getenv("copenclaw_DATA_DIR") or default_data_dir,
            workspace_dir=os.getenv("copenclaw_WORKSPACE_DIR") or default_workspace,
            copilot_cli_timeout=int(os.getenv("copenclaw_CLI_TIMEOUT", "7200")),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET"),
            telegram_allow_from=[v.strip() for v in telegram_allow.split(",") if v.strip()],
            msteams_app_id=os.getenv("MSTEAMS_APP_ID"),
            msteams_app_password=os.getenv("MSTEAMS_APP_PASSWORD"),
            msteams_tenant_id=os.getenv("MSTEAMS_TENANT_ID"),
            msteams_allow_from=[v.strip() for v in msteams_allow.split(",") if v.strip()],
            whatsapp_phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID"),
            whatsapp_access_token=os.getenv("WHATSAPP_ACCESS_TOKEN"),
            whatsapp_verify_token=os.getenv("WHATSAPP_VERIFY_TOKEN", ""),
            whatsapp_allow_from=[v.strip() for v in whatsapp_allow.split(",") if v.strip()],
            signal_api_url=os.getenv("SIGNAL_API_URL"),
            signal_phone_number=os.getenv("SIGNAL_PHONE_NUMBER"),
            signal_allow_from=[v.strip() for v in signal_allow.split(",") if v.strip()],
            slack_bot_token=os.getenv("SLACK_BOT_TOKEN"),
            slack_signing_secret=os.getenv("SLACK_SIGNING_SECRET"),
            slack_allow_from=[v.strip() for v in slack_allow.split(",") if v.strip()],
            pairing_mode=os.getenv("copenclaw_PAIRING_MODE", "pairing"),
            pairing_code_length=int(os.getenv("copenclaw_PAIRING_CODE_LENGTH", "6")),
            msteams_validate_token=os.getenv("MSTEAMS_VALIDATE_TOKEN", "true").lower() in {"1", "true", "yes"},
            webhook_rate_limit_calls=int(os.getenv("copenclaw_WEBHOOK_RATE_LIMIT_CALLS", "30")),
            webhook_rate_limit_seconds=int(os.getenv("copenclaw_WEBHOOK_RATE_LIMIT_SECONDS", "60")),
            telegram_owner_chat_id=os.getenv("TELEGRAM_OWNER_CHAT_ID"),
            mcp_token=os.getenv("copenclaw_MCP_TOKEN"),
            host=os.getenv("copenclaw_HOST", "127.0.0.1"),
            port=int(os.getenv("copenclaw_PORT", "18790")),
            backup_max_snapshots=int(os.getenv("copenclaw_BACKUP_MAX_SNAPSHOTS", "30")),
            clear_logs_on_launch=os.getenv("copenclaw_CLEAR_LOGS_ON_LAUNCH", "false").lower() in {"1", "true", "yes"},
        )
