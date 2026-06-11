import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

import run


class RunConfigTests(unittest.TestCase):
    def test_load_env_file_sets_missing_values_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_file = Path(tmp_dir) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "DEVTOOLS_REMOTE=fork",
                        "export DEVTOOLS_HEAD_BRANCH=release",
                        'OLLAMA_HOST="http://localhost:11434"',
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"DEVTOOLS_REMOTE": "origin"}, clear=True):
                run.load_env_file(env_file)

                self.assertEqual(os.environ["DEVTOOLS_REMOTE"], "origin")
                self.assertEqual(os.environ["DEVTOOLS_HEAD_BRANCH"], "release")
                self.assertEqual(os.environ["OLLAMA_HOST"], "http://localhost:11434")

    def test_build_parser_help_mentions_env_and_dotenv_support(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            parser = run.build_parser()

        help_text = parser.format_help()
        self.assertIn("DEVTOOLS_ROOT_DIRS", help_text)
        self.assertIn("OLLAMA_ALLOW_REMOTE_CONTEXT", help_text)
        self.assertIn("OLLAMA_MAX_REVIEW_DIFF_CHARS", help_text)
        self.assertIn("run.py auto-loads a local .env file", help_text)
        self.assertIn("--dry-run", help_text)
        self.assertIn("--prod", help_text)

    def test_render_main_menu_includes_review_placeholder_entry(self) -> None:
        buffer = io.StringIO()
        test_console = Console(file=buffer, force_terminal=False, color_system=None, width=120)

        with patch("run.console", test_console):
            run.render_main_menu(
                mode_label="DRY RUN",
                root_dirs=["/tmp/root"],
                head_branch="staging",
                base_branch_strategy="origin/HEAD default branch",
            )

        output = buffer.getvalue()
        self.assertIn("Dev Tools Control Deck", output)
        self.assertIn("Review Code", output)
        self.assertIn("Soon", output)

    def test_ask_main_action_returns_prompt_choice(self) -> None:
        with patch("run.Prompt.ask", return_value="3") as prompt:
            choice = run.ask_main_action()

        self.assertEqual(choice, "3")
        prompt.assert_called_once()

    def test_run_selected_action_dispatches_review_placeholder(self) -> None:
        with patch("run.show_review_placeholder") as show_review_placeholder:
            should_continue = run.run_selected_action(
                "3",
                root_dirs=["/tmp/root"],
                head_branch="staging",
                base_branch_strategy="strategy",
            )

        self.assertTrue(should_continue)
        show_review_placeholder.assert_called_once_with()

    def test_run_selected_action_quit_stops_menu_loop(self) -> None:
        should_continue = run.run_selected_action(
            "q",
            root_dirs=["/tmp/root"],
            head_branch="staging",
            base_branch_strategy="strategy",
        )

        self.assertFalse(should_continue)

    def test_main_quits_from_menu_without_running_workflows(self) -> None:
        with patch("run.load_env_file"), patch("run.console.print"), patch("run.print"), patch(
            "run.figlet_format",
            side_effect=lambda text, font: text,
        ), patch("run.render_main_menu") as render_main_menu, patch(
            "run.ask_main_action",
            return_value="q",
        ), patch(
            "run.run_selected_action",
            return_value=False,
        ) as run_selected_action:
            run.main(["--dry-run"])

        render_main_menu.assert_called_once()
        run_selected_action.assert_called_once()


if __name__ == "__main__":
    unittest.main()
