from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
HANGUL_PATTERN = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]")


class EnglishOnlyRepositoryTests(unittest.TestCase):
    def test_tracked_text_files_contain_no_hangul(self) -> None:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
        )
        violations: list[str] = []

        for encoded_path in result.stdout.split(b"\0"):
            if not encoded_path:
                continue
            relative_path = encoded_path.decode("utf-8")
            data = (REPOSITORY_ROOT / relative_path).read_bytes()
            if b"\0" in data:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue

            for line_number, line in enumerate(text.splitlines(), start=1):
                if HANGUL_PATTERN.search(line):
                    violations.append(f"{relative_path}:{line_number}")

        self.assertEqual(
            violations,
            [],
            "Tracked text files must use English only; Hangul found at: "
            + ", ".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
