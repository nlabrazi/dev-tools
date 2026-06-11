import io
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from core.review import CodeReviewExplanation, ReviewCommentPlan, ReviewCommentSuggestion, ReviewContext
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
        self.assertIn("OLLAMA_MAX_REVIEW_FILE_CHARS", help_text)
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
        self.assertIn("Comment Code", output)
        self.assertIn("Preview", output)

    def test_ask_main_action_returns_prompt_choice(self) -> None:
        with patch("run.Prompt.ask", return_value="3") as prompt:
            choice = run.ask_main_action()

        self.assertEqual(choice, "3")
        prompt.assert_called_once()

    def test_ask_review_repository_returns_selected_repo(self) -> None:
        repositories = [("api", "/tmp/api"), ("web", "/tmp/web")]

        with patch("run.Prompt.ask", return_value="2"), patch("run.console.print"):
            selected = run.ask_review_repository(repositories)

        self.assertEqual(selected, ("web", "/tmp/web"))

    def test_ask_review_repository_auto_selects_single_repo(self) -> None:
        repositories = [("api", "/tmp/api")]

        with patch("run.Prompt.ask") as prompt, patch("run.console.print"):
            selected = run.ask_review_repository(repositories)

        self.assertEqual(selected, ("api", "/tmp/api"))
        prompt.assert_not_called()

    def test_ask_review_target_supports_branch_ref(self) -> None:
        with patch("run.render_review_target_menu"), patch(
            "run.Prompt.ask",
            side_effect=["3", "develop"],
        ):
            target = run.ask_review_target()

        self.assertEqual(target, ("branch", "develop"))

    def test_ask_review_target_supports_specific_file_path(self) -> None:
        with patch("run.render_review_target_menu"), patch(
            "run.Prompt.ask",
            side_effect=["5", "src/app.ts"],
        ):
            target = run.ask_review_target()

        self.assertEqual(target, ("file", "src/app.ts"))

    def test_render_code_review_explanation_outputs_french_model_content(self) -> None:
        buffer = io.StringIO()
        test_console = Console(file=buffer, force_terminal=False, color_system=None, width=120)
        context = ReviewContext(
            repo_path="/tmp/repo",
            target="worktree",
            label="worktree changes",
            files=["src/app.ts"],
            diff="diff --git",
        )
        explanation = CodeReviewExplanation(
            title="Comprendre le payload API",
            overview="Ce fichier prépare les données envoyées à l'API.",
            technical_context="Il centralise la normalisation avant l'appel réseau.",
            important_files=["src/app.ts contient la logique principale."],
            behavior=["La modification rend le payload plus stable."],
            points_to_check=["Vérifier les cas de retry."],
            risks=["Un ordre incorrect peut casser la signature."],
        )

        with patch("run.console", test_console):
            run.render_code_review_explanation("repo", context, explanation)

        output = buffer.getvalue()
        self.assertIn("Review Result", output)
        self.assertIn("Comprendre le payload API", output)
        self.assertIn("Vérifier les cas de retry", output)

    def test_render_comment_plan_outputs_suggestions(self) -> None:
        buffer = io.StringIO()
        test_console = Console(file=buffer, force_terminal=False, color_system=None, width=120)
        context = ReviewContext(
            repo_path="/tmp/repo",
            target="worktree",
            label="worktree changes",
            files=["src/app.ts"],
            diff="diff --git",
        )
        plan = ReviewCommentPlan(
            summary="Clarify payload normalization.",
            comments=[
                ReviewCommentSuggestion(
                    file="src/app.ts",
                    anchor="function buildPayload(input) {",
                    placement="before",
                    comment="// Normalize before signing so retries stay stable.",
                    reason="Order-sensitive behavior.",
                )
            ],
        )

        with patch("run.console", test_console):
            run.render_comment_plan("repo", context, plan)

        output = buffer.getvalue()
        self.assertIn("Comment Preview", output)
        self.assertIn("Suggested Comment 1", output)
        self.assertIn("Preview only", output)

    def test_run_review_workflow_collects_generates_and_renders_explanation(self) -> None:
        context = ReviewContext(
            repo_path="/tmp/repo",
            target="worktree",
            label="worktree changes",
            files=["src/app.ts"],
            diff="diff --git",
        )
        explanation = CodeReviewExplanation(
            title="Titre",
            overview="Synthèse",
            technical_context="Contexte",
            important_files=[],
            behavior=[],
            points_to_check=[],
            risks=[],
        )

        with patch(
            "core.repositories.iter_git_repositories",
            return_value=[("repo", "/tmp/repo")],
        ), patch("run.render_repository_picker"), patch(
            "run.ask_review_repository",
            return_value=("repo", "/tmp/repo"),
        ), patch(
            "run.ask_review_target",
            return_value=("worktree", None),
        ), patch(
            "core.review.collect_review_context",
            return_value=context,
        ) as collect_review_context, patch(
            "core.review.generate_code_review",
            return_value=explanation,
        ) as generate_code_review, patch(
            "run.render_review_setup"
        ) as render_review_setup, patch(
            "run.render_code_review_explanation"
        ) as render_code_review_explanation, patch(
            "run.console.status",
            return_value=nullcontext(),
        ), patch("run.section_title"):
            run.run_review_workflow(["/tmp/root"])

        collect_review_context.assert_called_once_with("/tmp/repo", "worktree", None)
        render_review_setup.assert_called_once_with("repo", context)
        generate_code_review.assert_called_once_with("repo", context)
        render_code_review_explanation.assert_called_once_with("repo", context, explanation)

    def test_run_comment_workflow_collects_generates_and_renders_comment_preview(self) -> None:
        context = ReviewContext(
            repo_path="/tmp/repo",
            target="worktree",
            label="worktree changes",
            files=["src/app.ts"],
            diff="diff --git",
        )
        plan = ReviewCommentPlan(summary="Summary", comments=[])

        with patch(
            "core.repositories.iter_git_repositories",
            return_value=[("repo", "/tmp/repo")],
        ), patch(
            "run.ask_review_repository",
            return_value=("repo", "/tmp/repo"),
        ), patch(
            "run.ask_review_target",
            return_value=("worktree", None),
        ), patch(
            "core.review.collect_review_context",
            return_value=context,
        ) as collect_review_context, patch(
            "core.review.generate_review_comments",
            return_value=plan,
        ) as generate_review_comments, patch(
            "run.render_comment_plan"
        ) as render_comment_plan, patch(
            "run.console.status",
            return_value=nullcontext(),
        ), patch("run.section_title"):
            run.run_comment_workflow(["/tmp/root"])

        collect_review_context.assert_called_once_with("/tmp/repo", "worktree", None)
        generate_review_comments.assert_called_once_with("repo", context)
        render_comment_plan.assert_called_once_with("repo", context, plan)

    def test_run_selected_action_dispatches_review_workflow(self) -> None:
        with patch("run.run_review_workflow") as run_review_workflow:
            should_continue = run.run_selected_action(
                "3",
                root_dirs=["/tmp/root"],
                head_branch="staging",
                base_branch_strategy="strategy",
            )

        self.assertTrue(should_continue)
        run_review_workflow.assert_called_once_with(["/tmp/root"])

    def test_run_selected_action_dispatches_comment_workflow(self) -> None:
        with patch("run.run_comment_workflow") as run_comment_workflow:
            should_continue = run.run_selected_action(
                "4",
                root_dirs=["/tmp/root"],
                head_branch="staging",
                base_branch_strategy="strategy",
            )

        self.assertTrue(should_continue)
        run_comment_workflow.assert_called_once_with(["/tmp/root"])

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
