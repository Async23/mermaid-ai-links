"""Regression checks for files intended to be published."""

from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path

from mermaid_ai_links import __version__


ROOT = Path(__file__).resolve().parents[1]
SIGNED_LOCAL_LINK_RE = re.compile(r"http://(?:127\.0\.0\.1|localhost):\d+/v1/open/[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
PERSONAL_HOME_RE = re.compile(r"(?:/Users|/home)/[^/\s\"'`]+/")


class PublicRepositoryTests(unittest.TestCase):
    def _public_markdown_files(self) -> list[Path]:
        docs = [path for path in (ROOT / "docs").rglob("*.md") if not path.name.endswith(".local.md")]
        return [ROOT / "README.md", *docs]

    def test_public_markdown_has_no_signed_local_links(self) -> None:
        for path in self._public_markdown_files():
            with self.subTest(path=path.relative_to(ROOT)):
                content = path.read_text(encoding="utf-8")
                self.assertIsNone(SIGNED_LOCAL_LINK_RE.search(content))

    def test_public_markdown_has_no_personal_absolute_paths(self) -> None:
        for path in self._public_markdown_files():
            content = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertIsNone(PERSONAL_HOME_RE.search(content))

    def test_public_c4_keeps_the_three_mermaid_blocks(self) -> None:
        content = (ROOT / "docs" / "C4.md").read_text(encoding="utf-8")
        self.assertEqual(content.count("```mermaid\n"), 3)

    def test_python_and_npm_versions_match(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        npm_package = json.loads((ROOT / "npm" / "package.json").read_text(encoding="utf-8"))
        self.assertEqual("0.2.3", __version__)
        self.assertEqual(__version__, project["project"]["version"])
        self.assertEqual(__version__, npm_package["version"])


if __name__ == "__main__":
    unittest.main()
