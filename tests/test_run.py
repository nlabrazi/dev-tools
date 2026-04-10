import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.assertIn("run.py auto-loads a local .env file", help_text)
        self.assertIn("--dry-run", help_text)
        self.assertIn("--prod", help_text)


if __name__ == "__main__":
    unittest.main()
