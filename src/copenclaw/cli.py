from __future__ import annotations

import logging
import os
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

if __name__ == "__main__":
    app()