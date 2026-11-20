"""Guard the public project entry page against broken navigation and formatting."""
from pathlib import Path
import re
from unittest import TestCase, main

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"


class ReadmeContractTests(TestCase):
    def test_readme_does_not_contain_literal_newline_escape_sequences(self):
        content = README.read_text(encoding="utf-8")
        self.assertNotIn("\\n", content)

    def test_version_and_workflow_choices_remain_explicit(self):
        content = README.read_text(encoding="utf-8")
        for phrase in (
            "Java Desktop v0.2.4",
            "Source-only v0.2.5",
            "Maximum-compression research",
            "Fast fused image workflow",
            "Current development source",
            "Workflow distinction",
            "Anchor",
            "Context",
            "HF-Detail",
            "docs/CODEC_NAMING.md",
            "stpyramid_native_codecs",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, content)

    def test_relative_markdown_links_resolve(self):
        content = README.read_text(encoding="utf-8")
        links = re.findall(r"\]\(([^)]+)\)", content)
        missing = []
        for target in links:
            if target.startswith(("https://", "http://", "#")):
                continue
            path = target.split("#", 1)[0]
            if path and not (ROOT / path).is_file() and not (ROOT / path).is_dir():
                missing.append(target)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    main()
