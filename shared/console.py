from rich.console import Console
from rich.theme import Theme

# Define a custom theme for consistent output styles
custom_theme = Theme(
    {
        "info": "dim cyan",
        "warning": "yellow",
        "error": "bold red",
        "success": "bold green",
        "header": "bold magenta",
    }
)

# Shared console instance
console = Console(theme=custom_theme)


def print_header(title: str) -> None:
    """Print a styled header."""
    console.print(f"\n[header]{title}[/header]")
    console.print("=" * len(title), style="dim")


def print_success(message: str) -> None:
    """Print a success message."""
    console.print(f"[success]✔ {message}[/success]")


def print_error(message: str) -> None:
    """Print an error message."""
    console.print(f"[error]✖ {message}[/error]")


def print_warning(message: str) -> None:
    """Print a warning message."""
    console.print(f"[warning]⚠ {message}[/warning]")


def print_info(message: str) -> None:
    """Print an informational message."""
    console.print(f"[info]ℹ {message}[/info]")
