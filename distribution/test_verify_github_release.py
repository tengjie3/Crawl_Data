"""Remote verification must also work before a draft release creates its tag."""
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import verify_github_release as verifier


class ReleaseVerificationTests(unittest.TestCase):
    def setUp(self):
        expected = json.loads(Path(__file__).with_name("release-manifest.json").read_text())
        self.api_url = "https://api.github.com/repos/tengjie3/Crawl_Data/releases/123"
        self.release = {
            "draft": True,
            "html_url": "https://github.com/tengjie3/Crawl_Data/releases/tag/untagged-test",
            "tag_name": "intelligent-discovery-html-v2026.09.16",
            "assets": [{
                "name": item["name"], "size": item["bytes"], "state": "uploaded",
                "digest": "sha256:" + item["sha256"],
                "browser_download_url": "https://example.invalid/" + item["name"],
            } for item in expected["assets"]],
        }

    def run_verification(self, release):
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            if command[:3] == ["gh", "release", "view"]:
                self.assertEqual(command[-2:], ["--json", "apiUrl"])
                response = {"apiUrl": self.api_url}
            elif command == ["gh", "api", self.api_url]:
                response = release
            else:
                self.fail("Must resolve the release API URL before inspecting draft assets")
            self.assertTrue(kwargs["check"])
            return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

        output = io.StringIO()
        with patch.object(verifier.subprocess, "run", side_effect=run), \
                patch("sys.argv", ["verify_github_release.py"]), \
                contextlib.redirect_stdout(output):
            verifier.main()
        self.assertEqual(len(commands), 2)
        return json.loads(output.getvalue())

    def test_draft_without_published_tag_can_be_verified(self):
        result = self.run_verification(self.release)
        self.assertTrue(result["draft"])
        self.assertTrue(result["allManifestAssetsVerified"])
        self.assertEqual(len(result["assets"]), 4)

    def test_published_release_can_be_verified(self):
        self.release["draft"] = False
        self.assertFalse(self.run_verification(self.release)["draft"])

    def test_missing_and_unuploaded_assets_are_rejected(self):
        for state in ("missing", "new"):
            with self.subTest(state=state):
                release = copy.deepcopy(self.release)
                if state == "missing":
                    release["assets"].pop(0)
                else:
                    release["assets"][0]["state"] = state
                with self.assertRaisesRegex(ValueError, "Missing/unuploaded asset"):
                    self.run_verification(release)

    def test_wrong_size_is_rejected(self):
        self.release["assets"][0]["size"] -= 1
        with self.assertRaisesRegex(ValueError, "Size mismatch"):
            self.run_verification(self.release)

    def test_wrong_and_unavailable_digests_are_rejected(self):
        for digest in (None, "sha256:incorrect"):
            with self.subTest(digest=digest):
                self.release["assets"][0]["digest"] = digest
                with self.assertRaisesRegex(ValueError, "SHA256 mismatch or unavailable"):
                    self.run_verification(self.release)


if __name__ == "__main__":
    unittest.main()
