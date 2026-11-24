import io
import os
import unittest
from unittest.mock import patch

from rich.console import Console

from utils.console import (
    ask_console_command,
    clear_screen,
    is_clear_screen_disabled,
    resolve_console_command,
    ui_banner,
    ui_command_bar,
    ui_panel,
    ui_step_line,
    ui_table,
)


class ConsoleUiTests(unittest.TestCase):
    def test_ui_panel_renders_shared_title_and_content(self) -> None:
        buffer = io.StringIO()
        console = Console(file=buffer, force_terminal=False, color_system=None, width=80)

        console.print(
            ui_panel(
                "No source file was modified.",
                title="Cancelled",
                tone="warning",
                compact=True,
            )
        )

        output = buffer.getvalue()
        self.assertIn("Cancelled", output)
        self.assertIn("No source file was modified.", output)

    def test_ui_table_renders_title_caption_and_rows(self) -> None:
        buffer = io.StringIO()
        console = Console(file=buffer, force_terminal=False, color_system=None, width=80)
        table = ui_table(
            title="Step 2/2 · Scope",
            caption="Choose one scope.",
        )
        table.add_column("Key")
        table.add_column("Scope")
        table.add_row("1", "Current changes")

        console.print(table)

        output = buffer.getvalue()
        self.assertIn("Step 2/2 · Scope", output)
        self.assertIn("Current changes", output)
        self.assertIn("Choose one scope.", output)

    def test_clear_screen_skips_non_terminal_consoles(self) -> None:
        buffer = io.StringIO()
        console = Console(file=buffer, force_terminal=False, color_system=None, width=80)

        with patch.dict(os.environ, {}, clear=False), patch.object(console, "clear") as clear_mock:
            os.environ.pop("DEVTOOLS_NO_CLEAR", None)
            cleared = clear_screen(console, animated=True, label="Review")

        self.assertFalse(cleared)
        clear_mock.assert_not_called()

    def test_clear_screen_respects_devtools_no_clear_env(self) -> None:
        buffer = io.StringIO()
        console = Console(file=buffer, force_terminal=True, color_system=None, width=80)

        with patch.dict(os.environ, {"DEVTOOLS_NO_CLEAR": "1"}), patch.object(console, "clear") as clear_mock:
            self.assertTrue(is_clear_screen_disabled())
            cleared = clear_screen(console, animated=True, label="Review")

        self.assertFalse(cleared)
        clear_mock.assert_not_called()

    def test_clear_screen_animates_and_clears_on_interactive_terminal(self) -> None:
        buffer = io.StringIO()
        console = Console(file=buffer, force_terminal=True, color_system=None, width=80)

        with patch.dict(os.environ, {}, clear=False), patch("utils.console.time.sleep") as sleep_mock, patch.object(
            console, "clear"
        ) as clear_mock:
            os.environ.pop("DEVTOOLS_NO_CLEAR", None)
            cleared = clear_screen(console, animated=True, label="Review · api", duration=0.12)

        self.assertTrue(cleared)
        self.assertEqual(sleep_mock.call_count, 6)
        clear_mock.assert_called_once()

    def test_retro_amstrad_banner_command_bar_step_line_and_prompt(self) -> None:
        buffer = io.StringIO()
        console = Console(file=buffer, force_terminal=False, color_system=None, width=100)

        console.print(ui_banner("argandici", mode="PROD", status_items=["staging (propre)", "Renovate OK"]))
        console.print(ui_command_bar([("1", "Renovate"), ("2", "Bump"), ("q", "Retour")]))
        console.print(ui_step_line("Biome Check", status="OK", detail="18 files"))
        console.print(ui_step_line("Vitest", status="FAIL", detail="1 failed"))

        output = buffer.getvalue()
        self.assertIn("DEV-TOOLS", output)
        self.assertIn("argandici", output)
        self.assertIn("[1] Renovate", output)
        self.assertIn("[2] Bump", output)
        self.assertIn("Biome Check", output)
        self.assertIn("FAIL", output)

        aliases = {"bump": "2", "quit": "q"}
        self.assertEqual(resolve_console_command("BUMP", valid_choices={"1", "2", "q"}, aliases=aliases), "2")
        self.assertEqual(resolve_console_command("", valid_choices={"1", "2", "q"}, default="q"), "q")
        self.assertIsNone(resolve_console_command("unknown", valid_choices={"1", "2", "q"}, aliases=aliases))

        with patch("builtins.input", side_effect=["nope", "bump"]):
            choice = ask_console_command(
                "argandici",
                valid_choices={"1", "2", "q"},
                aliases=aliases,
                target_console=console,
            )
        self.assertEqual(choice, "2")
        self.assertIn("Commande inconnue", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
