"""Failure-path tests; the full 4 GB reconstruction is separately verified."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class JoinScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "intelligent-data-discovery-html-only-2026-09-16.zip"
        self.script = self.root / "join-macos-linux.command"
        shutil.copy2(Path(__file__).with_name(self.script.name), self.script)

    def run_join(self):
        return subprocess.run(["sh", str(self.script)], cwd=self.root,
                              capture_output=True, text=True)

    def test_missing_part_does_not_create_zip(self):
        result = self.run_join()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing:", result.stdout)
        self.assertFalse(self.base.exists())

    def test_invalid_parts_do_not_publish_zip(self):
        for index in (1, 2, 3):
            Path(str(self.base) + f".part{index:02d}").write_bytes(b"invalid")
        result = self.run_join()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Checksum failed", result.stdout)
        self.assertFalse(self.base.exists())
        self.assertEqual(Path(str(self.base) + ".partial").read_bytes(), b"invalid" * 3)

    def test_existing_zip_is_not_overwritten(self):
        self.base.write_bytes(b"existing user file")
        result = self.run_join()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.base.read_bytes(), b"existing user file")

    def test_existing_partial_is_not_overwritten(self):
        for index in (1, 2, 3):
            Path(str(self.base) + f".part{index:02d}").write_bytes(b"invalid")
        partial = Path(str(self.base) + ".partial")
        partial.write_bytes(b"previous incomplete run")
        result = self.run_join()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(partial.read_bytes(), b"previous incomplete run")


if __name__ == "__main__":
    unittest.main()
