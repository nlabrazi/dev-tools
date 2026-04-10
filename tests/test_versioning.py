import unittest
from unittest.mock import patch

from core.versioning import compute_next_version_from_messages


class VersioningTests(unittest.TestCase):
    def test_compute_next_version_from_messages_uses_detected_bump(self) -> None:
        with patch("core.versioning.get_last_semver_tag", return_value="v1.2.3"):
            version = compute_next_version_from_messages(
                "/tmp/repo",
                [
                    "feat(api): add release endpoint",
                    "fix(worker): guard empty jobs",
                ],
            )

        self.assertEqual(version, "v1.3.0")


if __name__ == "__main__":
    unittest.main()
