from __future__ import annotations

import logging
import os
from pathlib import Path
import sys
from typing import Optional

import typer
import uvicorn
from dotenv import load_dotenv

from copenclaw.core.gateway import create_app

app = typer.Typer(add_completion=False)

def _load_env() -> None:
    load_dotenv()

def _setup_logging() -> None:
    """Configure centralized logging to both stdout and log files."""
    from copenclaw.core.config import Settings
    from copenclaw.core.logging_config import setup_logging

    settings = Settings.from_env()
    setup_logging(log_dir=settings.log_dir, log_level=settings.log_level, clear_on_launch=settings.clear_logs_on_launch)

@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind host"),
    port: int = typer.Option(18790, help="Bind port"),
    reload: bool = typer.Option(False, help="Enable auto-reload"),
    accept_risks: bool = typer.Option(False, "--accept-risks", help="Accept security risks without interactive prompt"),
) -> None:
    _load_env()

    # Security disclaimer gate — must be accepted before the server starts
    from copenclaw.core.disclaimer import check_or_prompt
    check_or_prompt(allow_flag=accept_risks)

    _setup_logging()
    uvicorn.run("copenclaw.core.gateway:create_app", host=host, port=port, reload=reload, factory=True)

@app.command()
def version() -> None:
    from copenclaw import __version__

    typer.echo(__version__)

@app.command()
def update(
    check_only: bool = typer.Option(False, "--check", help="Only check for updates, don't apply"),
    apply_now: bool = typer.Option(False, "--apply", help="Apply update without prompting"),
) -> None:
    """Check for and apply COpenClaw updates from git."""
    _load_env()

    from copenclaw.core.updater import (
        check_for_updates,
        apply_update,
        format_update_check,
        format_update_result,
    )

    typer.echo("Checking for updates...")
    info = check_for_updates()

    if info is None:
        typer.echo("✅ COpenClaw is up to date.")
        raise typer.Exit()

    # Show update info
    typer.echo(format_update_check(info))

    if check_only:
        raise typer.Exit()

    # Warn about conflicts
    if info.has_conflicts:
        typer.echo("")
        typer.secho(
            "⚠️  WARNING: Some local files conflict with the update.",
            fg=typer.colors.YELLOW,
            bold=True,
        )
        if not apply_now:
            proceed = typer.confirm("Do you want to proceed anyway?", default=False)
            if not proceed:
                typer.echo("Update cancelled.")
                raise typer.Exit()

    # Confirm if not --apply
    if not apply_now:
        proceed = typer.confirm("Apply this update?", default=True)
        if not proceed:
            typer.echo("Update cancelled.")
            raise typer.Exit()

    typer.echo("\nApplying update...")
    result = apply_update()
    typer.echo(format_update_result(result))

    if result.success:
        typer.echo("\nRestart COpenClaw to load the new code.")


@app.command("teams-setup")
def teams_setup(
    tenant_id: str = typer.Option(..., envvar="MSTEAMS_ADMIN_TENANT_ID", help="Azure AD tenant ID"),
    admin_client_id: str = typer.Option(..., envvar="MSTEAMS_ADMIN_CLIENT_ID", help="Admin app client ID"),
    admin_client_secret: str = typer.Option(..., envvar="MSTEAMS_ADMIN_CLIENT_SECRET", help="Admin app client secret"),
    subscription_id: str = typer.Option(..., envvar="MSTEAMS_AZURE_SUBSCRIPTION_ID", help="Azure subscription ID"),
    resource_group: str = typer.Option(..., envvar="MSTEAMS_AZURE_RESOURCE_GROUP", help="Azure resource group name"),
    resource_group_location: str = typer.Option(
        "eastus",
        envvar="MSTEAMS_AZURE_LOCATION",
        help="Azure resource group location",
    ),
    bot_name: str = typer.Option(
        "copenclaw-teams-bot",
        envvar="MSTEAMS_BOT_NAME",
        help="Bot display name",
    ),
    messaging_endpoint: str = typer.Option(
        ...,
        envvar="MSTEAMS_BOT_ENDPOINT",
        help="Public HTTPS endpoint (https://<host>/teams/api/messages)",
    ),
    package_dir: str = typer.Option(
        ".",
        envvar="MSTEAMS_APP_PACKAGE_DIR",
        help="Directory for generated Teams app package",
    ),
    publish: bool = typer.Option(
        False,
        "--publish/--no-publish",
        envvar="MSTEAMS_AUTO_PUBLISH",
        help="Publish the Teams app package to the tenant app catalog",
    ),
    create_resource_group: bool = typer.Option(
        True,
        "--create-resource-group/--no-create-resource-group",
        envvar="MSTEAMS_AUTO_CREATE_RG",
        help="Create the resource group if missing",
    ),
    write_env: Optional[str] = typer.Option(
        None,
        "--write-env",
        envvar="MSTEAMS_WRITE_ENV",
        help="Write MSTEAMS_* credentials to this .env file",
    ),
) -> None:
    """Provision a Teams bot + app registration and generate a Teams app package."""
    _load_env()
    _setup_logging()

    from copenclaw.integrations.teams_provision import (
        TeamsProvisioningConfig,
        provision_teams_bot,
        update_env_file,
    )

    config = TeamsProvisioningConfig(
        tenant_id=tenant_id,
        admin_client_id=admin_client_id,
        admin_client_secret=admin_client_secret,
        subscription_id=subscription_id,
        resource_group=resource_group,
        resource_group_location=resource_group_location,
        bot_name=bot_name,
        messaging_endpoint=messaging_endpoint,
        package_dir=Path(package_dir).expanduser().resolve(),
        create_resource_group=create_resource_group,
        publish=publish,
    )

    result = provision_teams_bot(config)

    typer.echo("✅ Teams bot provisioned.")
    typer.echo(f"   MSTEAMS_APP_ID={result.app_id}")
    typer.echo(f"   MSTEAMS_APP_PASSWORD={result.app_password}")
    typer.echo(f"   MSTEAMS_TENANT_ID={result.tenant_id}")
    typer.echo(f"   Teams app package: {result.app_package_path}")
    if result.teams_channel_enabled:
        typer.echo("   Teams channel: enabled")
    else:
        typer.echo(f"   Teams channel: failed ({result.teams_channel_error})")
    if result.published:
        typer.echo("   Teams app: published to tenant catalog")

    if write_env:
        update_env_file(Path(write_env).expanduser(), {
            "MSTEAMS_APP_ID": result.app_id,
            "MSTEAMS_APP_PASSWORD": result.app_password,
            "MSTEAMS_TENANT_ID": result.tenant_id,
        })
        typer.echo(f"   Updated {write_env}")

if __name__ == "__main__":
    app()
