"""Freeze the public names and runtime contract for STPyramid image codecs."""
from pathlib import Path
from unittest import TestCase, main

ROOT = Path(__file__).resolve().parents[1]


class CodecNamingContractTests(TestCase):
    def test_canonical_names_are_defined_and_described(self):
        naming = (ROOT / "docs/CODEC_NAMING.md").read_text(encoding="utf-8")
        for name in ("Anchor Codec", "Context Codec", "HF-Detail Codec"):
            with self.subTest(name=name):
                self.assertIn(name, naming)

    def test_public_entry_points_do_not_use_experiment_ids_as_codec_names(self):
        public_docs = (
            ROOT / "README.md",
            ROOT / "docs/ARCHITECTURE.md",
            ROOT / "docs/CODEMAP.md",
            ROOT / "docs/ADVANCED_WORKFLOWS.md",
            ROOT / "results/README.md",
        )
        forbidden = ("089 Codec", "091 Codec", "HF1 Codec", "slide_jxl089", "slide_jxl091", "slide_hf1")
        for path in public_docs:
            content = path.read_text(encoding="utf-8")
            for token in forbidden:
                with self.subTest(path=path.relative_to(ROOT), token=token):
                    self.assertNotIn(token, content)

    def test_cmake_exposes_only_descriptive_public_build_targets(self):
        cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
        for target in (
            "stpyramid_anchor",
            "stpyramid_context",
            "stpyramid_hf_detail",
            "stpyramid_native_codecs",
        ):
            with self.subTest(target=target):
                self.assertIn(f"add_custom_target({target}", cmake)

    def test_staged_runtime_uses_canonical_folders_and_binary_stems(self):
        staging = (ROOT / "stage_runtime.py").read_text(encoding="utf-8")
        for value in (
            '"anchor"',
            '"context"',
            '"hf-detail"',
            '"anchor_codec"',
            '"context_codec"',
            '"hf_detail_codec"',
        ):
            with self.subTest(value=value):
                self.assertIn(value, staging)

    def test_decoder_adapters_keep_legacy_fallbacks(self):
        checks = {
            "codec/stbaseline/wsi_squeeze089.py": ("anchor_codec.dll", "slide_jxl089.dll"),
            "codec/stbaseline/wsi_conditioned091.py": ("context_codec.dll", "slide_jxl091.dll"),
            "codec/hf_adapter.py": ("hf_detail_codec.dll", "slide_hf1.dll"),
        }
        for relative, tokens in checks.items():
            content = (ROOT / relative).read_text(encoding="utf-8")
            for token in tokens:
                with self.subTest(path=relative, token=token):
                    self.assertIn(token, content)


if __name__ == "__main__":
    main()
