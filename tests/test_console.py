import io
import unittest

from rich.console import Console

from utils.console import ui_panel, ui_table


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


if __name__ == "__main__":
    unittest.main()
