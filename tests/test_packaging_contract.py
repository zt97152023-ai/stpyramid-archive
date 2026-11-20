"""Ensure desktop package builders ship every root module imported by the worker."""
from pathlib import Path
from unittest import TestCase, main

ROOT = Path(__file__).resolve().parents[1]


class PackagingContractTests(TestCase):
    def test_python_portable_script_list_contains_worker_dependencies(self):
        builder = (ROOT / "desktop/build_portable.py").read_text(encoding="utf-8")
        for name in ("desktop_worker.py", "runtime_config.py", "task_contract.py",
                     "desktop_i18n.py", "fetch_count_runtime.py"):
            with self.subTest(name=name):
                self.assertIn(f'"{name}"', builder)
        self.assertIn("context_codec.exe", builder)

    def test_java_portable_overlay_contains_worker_dependencies(self):
        builder = (ROOT / "java-desktop/package.ps1").read_text(encoding="utf-8")
        for name in ("desktop_worker.py", "runtime_config.py", "task_contract.py"):
            with self.subTest(name=name):
                self.assertIn(f"'{name}'", builder)
        self.assertIn("context_codec.exe", builder)

    def test_descriptive_native_codec_build_targets_are_available(self):
        cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/native-codec-regression.yml").read_text(encoding="utf-8")
        for target in ("stpyramid_anchor", "stpyramid_context", "stpyramid_hf_detail", "stpyramid_native_codecs"):
            with self.subTest(target=target):
                self.assertIn(f"add_custom_target({target}", cmake)
        self.assertIn("--target stpyramid_native_codecs", workflow)
        staging = (ROOT / "stage_runtime.py").read_text(encoding="utf-8")
        for public_name in ("anchor_codec", "context_codec", "hf_detail_codec"):
            with self.subTest(public_name=public_name):
                self.assertIn(public_name, staging)

    def test_worker_root_imports_are_packaged(self):
        worker = (ROOT / "desktop_worker.py").read_text(encoding="utf-8")
        builder = (ROOT / "desktop/build_portable.py").read_text(encoding="utf-8")
        for module in ("runtime_config", "task_contract"):
            with self.subTest(module=module):
                self.assertIn(f"from {module} import", worker)
                self.assertIn(f'"{module}.py"', builder)


if __name__ == "__main__":
    main()
