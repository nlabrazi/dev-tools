import io
import os
import unittest
from unittest.mock import patch

from rich.console import Console

from utils.console import clear_screen, is_clear_screen_disabled, ui_panel, ui_table


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


if __name__ == "__main__":
    unittest.main()
