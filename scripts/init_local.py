# /// script
# requires-python = ">=3.12"
# dependencies = ["rich>=13,<16"]
# ///
"""Create a local development configuration without overwriting existing secrets."""

import re
import secrets
from pathlib import Path

from rich.console import Console
from rich.table import Table


def build_development_env(template: str) -> str:
    """Fill the repository template with local-only development settings."""
    values = {
        "APP_ENV": "development",
        "PROJECT_NAME": '"Anonymous AI Consultation - Local"',
        "DEBUG": "false",
        "LANGFUSE_TRACING_ENABLED": "false",
        "LANGFUSE_PUBLIC_KEY": '""',
        "LANGFUSE_SECRET_KEY": '""',
        "OPENAI_API_KEY": '""',
        "SESSION_NAMING_ENABLED": "false",
        "JWT_SECRET_KEY": f'"{secrets.token_urlsafe(48)}"',
        "POSTGRES_HOST": "localhost",
        "POSTGRES_DB": "anonymous_consultation_dev",
        "POSTGRES_USER": "consultation_dev",
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "PROFILING_DIR": ".profiles",
        "LOG_LEVEL": "INFO",
    }
    for name, value in values.items():
        template, count = re.subn(
            rf"^{re.escape(name)}=.*$",
            lambda match, name=name, value=value: f"{name}={value}",
            template,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise ValueError(f"Expected exactly one {name} entry in .env.example")
    return (
        "# Local development only. Add your API key before starting the application.\n"
        "# Privacy features described in docs/zh-CN are design proposals, not implemented controls.\n" + template
    )


def initialize(project_root: Path, console: Console) -> int:
    """Create .env.development once and report only non-secret status."""
    destination = project_root / ".env.development"
    if destination.exists() or destination.is_symlink():
        console.print("[yellow]Existing .env.development preserved; no values were changed.[/yellow]")
        return 0

    try:
        template = (project_root / ".env.example").read_text(encoding="utf-8")
        contents = build_development_env(template)
        with destination.open("x", encoding="utf-8", newline="\n") as env_file:
            env_file.write(contents)
    except FileExistsError:
        console.print("[yellow].env.development was created by another process; preserved.[/yellow]")
        return 0
    except (OSError, ValueError) as error:
        console.print(f"[red]Local configuration initialization failed ({type(error).__name__}).[/red]")
        return 1

    table = Table(title="Local configuration created")
    table.add_column("Item")
    table.add_column("Status")
    table.add_row(".env.development", "Created; existing files are never overwritten")
    table.add_row("JWT / database secrets", "Generated locally; not printed")
    table.add_row("Database host", "localhost (Compose overrides it to db inside the app container)")
    table.add_row("LLM API key", "Empty; supply a development credential locally")
    table.add_row("Tracing / session titles / debug", "Disabled for the initial configuration")
    console.print(table)
    console.print("Next: docs/zh-CN/02-local-development.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(initialize(Path(__file__).resolve().parent.parent, Console()))
