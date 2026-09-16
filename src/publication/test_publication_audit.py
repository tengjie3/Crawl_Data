import importlib.util
import inspect
from pathlib import Path
import unittest


def auditor():
    target = Path(__file__).with_name("audit_zip.py")
    assert target.exists(), "independent ZIP publication auditor is not implemented"
    spec = importlib.util.spec_from_file_location("publication_auditor", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AuditTests(unittest.TestCase):
    def test_fresh_audit_can_accept_a_new_publication_digest(self):
        a = auditor()
        self.assertIn("expected_sha", inspect.signature(a.audit).parameters)

    def test_recognizes_vendor_tokens_without_exposing_values(self):
        a = auditor()
        examples = [("openai_api_key", "sk-proj-" + "A3b4C5d6E7f8G9h0" * 4),
                    ("github_token", "ghp_" + "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0iL3oP6"),
                    ("github_fine_grained_token", "github_pat_" + "Ab3_" * 30),
                    ("aws_access_key_id", "AKIA" + "A2B3C4D5E6F7G8H9")]
        for category, token in examples:
            findings = a.scan_text("api_key=" + token, "fixture.txt")
            self.assertTrue(any(f["category"] == category for f in findings))
            self.assertNotIn(token, str(findings))

    def test_private_key_cloud_secret_and_machine_paths(self):
        a = auditor()
        value = "AWS_SECRET_ACCESS_KEY=" + "b3D4f6H8j0K2m4N6p8Q0r2S4t6U8v0W2x4Y6z8A0"
        findings = a.scan_text(value + "\n-----BEGIN RSA PRIVATE KEY-----\n" + "Ab3d" * 20 + "\n-----END RSA PRIVATE KEY-----\n/Users/tengjie/Desktop/project", "fixture.txt")
        self.assertEqual({f["category"] for f in findings}, {"aws_secret_access_key", "private_key_material", "author_machine_path"})
        self.assertTrue(all(set(f) == {"file", "line", "category"} for f in findings))

    def test_only_known_placeholders_are_whitelisted_with_reason(self):
        a = auditor()
        findings = a.scan_text("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\nAWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "fixture.txt")
        self.assertTrue(findings)
        self.assertTrue(all(f["category"].startswith("known_placeholder:") for f in findings))
        self.assertTrue(a.WHITELIST_RATIONALES)

    def test_mentions_of_api_keys_are_not_keys(self):
        a = auditor()
        self.assertEqual(a.scan_text("This article discusses OpenAI sk- keys, GitHub tokens and AWS credentials. api_key=YOUR_API_KEY", "fixture.txt"), [])

    def test_public_url_home_path_is_not_a_machine_path(self):
        a = auditor()
        self.assertEqual(a.scan_text("https://public.example/home/site/index.html", "fixture.txt"), [])
        self.assertTrue(a.scan_text("file:///Users/tengjie/file.txt", "fixture.txt"))

    def test_frontend_identifiers_are_not_openai_tokens(self):
        a = auditor()
        self.assertEqual(a.scan_text("class=sk-render-component-container-long-name", "fixture.txt"), [])

    def test_sensitive_paths_and_safe_cas(self):
        a = auditor()
        for path in ("root/.env", "root/.git/config", "root/.aws/credentials", "root/id_rsa", "root/attachments/passport.pdf", "../escape"):
            self.assertTrue(a.scan_path(path))
        self.assertEqual(a.scan_path("root/offline/assets/ab/" + "a" * 64 + ".js"), [])


if __name__ == "__main__":
    unittest.main()
