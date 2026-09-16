import base64
import gzip
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


def sanitizer():
    path = Path(__file__).with_name("sanitize_publication.py")
    assert path.exists(), "publication sanitizer is not implemented"
    spec = importlib.util.spec_from_file_location("publication_sanitizer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SanitizerTests(unittest.TestCase):
    def test_replaces_only_credential_and_preserves_research_text(self):
        s = sanitizer()
        token = "AIza" + "A3b5C7d9E1f2G4h6J8k0L2m4N6p8Q0r2S4t"
        text = "before " + token + " after /home/researcher/project https://example.org/home/person"
        cleaned, counts = s.redact_text(text)
        self.assertEqual(cleaned, "before [REDACTED_GOOGLE_API_KEY] after /home/researcher/project https://example.org/home/person")
        self.assertEqual(counts, {"google_api_key": 1})
        self.assertNotIn(token, str(counts))

    def test_record_annotation_and_chunk_contract(self):
        s = sanitizer()
        token = "AIza" + "A3b5C7d9E1f2G4h6J8k0L2m4N6p8Q0r2S4t"
        rows = [{"file": "record_1.json", "record": {"text": "full " + token + " body", "provenance": {"license": "unknown"}}, "labels": {"topic": "unchanged"}, "assetHash": "a" * 64},
                {"file": "record_2.json", "record": {"text": "intact"}, "labels": {}, "assetHash": None}]
        original = s.encoded(rows)
        compressed = gzip.compress(original, mtime=0)
        descriptor = {"id": "records:example:1", "path": "offline/records/example/00001.js", "records": 2,
                      "bytes": len(compressed), "sha256": s.digest(compressed), "decodedBytes": len(original)}
        script = s.callback(descriptor["id"], compressed)
        changed, new_descriptor, evidence = s.sanitize_chunk(script, descriptor)
        actual = s.decode_chunk(changed, new_descriptor)
        self.assertEqual(len(actual), 2)
        self.assertEqual(actual[1], rows[1])
        self.assertEqual(actual[0]["assetHash"], rows[0]["assetHash"])
        self.assertEqual(actual[0]["record"]["provenance"], rows[0]["record"]["provenance"])
        self.assertEqual(actual[0]["publicationRedactions"], [{"category": "google_api_key", "count": 1}])
        self.assertEqual(actual[0]["record"].get("publication_redactions"), [{"category": "google_api_key", "count": 1}])
        self.assertEqual(actual[0]["record"].get("publication_provenance", {}).get("source_zip_sha256"), s.auditor.EXPECTED_SHA)
        self.assertEqual(evidence["changedRecords"], 1)
        self.assertNotIn(token, changed.decode())
        self.assertNotIn(token, str(evidence))
        self.assertEqual(s.encoded(rows), original)

    def test_author_prefix_only_and_known_aws_placeholder(self):
        s = sanitizer()
        cleaned, counts = s.redact_text("/Users/tengjie/Desktop/VibeDataBot-dev/output/file.json")
        self.assertEqual(cleaned, "project-relative/output/file.json")
        self.assertEqual(counts, {"author_machine_path": 1})
        example = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        self.assertEqual(s.redact_text(example), (example, {}))

    def test_damaged_original_chunk_is_rejected(self):
        s = sanitizer()
        with self.assertRaises(ValueError):
            s.sanitize_chunk(b"window.OfflineData.receive(\"x\",\"gzip-base64\",\"bad\");\n", {"id": "x", "records": 1, "bytes": 3, "sha256": "0" * 64, "decodedBytes": 2})


if __name__ == "__main__":
    unittest.main()
