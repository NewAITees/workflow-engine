"""Tests for shared console helpers."""

from __future__ import annotations

from collections.abc import Callable

from rich.console import Console

from shared import console as console_module


def _capture_output(action: Callable[[], None]) -> str:
    test_console = Console(record=True, width=80)
    original_console = console_module.console
    console_module.console = test_console
    try:
        action()
        return test_console.export_text()
    finally:
        console_module.console = original_console


def test_print_header_outputs_title_and_underline() -> None:
    title = "Build"
    output = _capture_output(lambda: console_module.print_header(title))

    lines = output.splitlines()
    assert title in output
    assert "=" * len(title) in lines


def test_print_success_includes_message() -> None:
    message = "All good"
    output = _capture_output(lambda: console_module.print_success(message))

    assert message in output


def test_print_error_includes_message() -> None:
    message = "Something broke"
    output = _capture_output(lambda: console_module.print_error(message))

    assert message in output


def test_print_warning_includes_message() -> None:
    message = "Heads up"
    output = _capture_output(lambda: console_module.print_warning(message))

    assert message in output


def test_print_info_includes_message() -> None:
    message = "FYI"
    output = _capture_output(lambda: console_module.print_info(message))

    assert message in output
