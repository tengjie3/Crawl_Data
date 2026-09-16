"""Builder contract tests; fixtures contain real record bytes and media headers."""
import base64
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

HERE = Path(__file__).parent


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def builder():
    target = HERE / "build.py"
    assert target.is_file(), "offline HTML data builder is not implemented"
    spec = importlib.util.spec_from_file_location("offline_builder", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def script_args(path):
    text = path.read_text()
    prefix = "window.OfflineData.receive("
    assert text.startswith(prefix) and text.endswith(");\n")
    return json.loads("[" + text[len(prefix):-3] + "]")


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, count=51, media=False):
        b = builder()
        source = self.root / "source"
        source.mkdir()
        files = []

        def write(relative, payload):
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            files.append({"path": relative, "bytes": len(payload), "sha256": digest(payload)})

        name = "movie_English_video" if media else "movie_English_html_text"
        asset = b"\x00\x00\x00\x20ftypisom" + bytes(range(256))
        asset_hash = digest(asset) if media else None
        asset_path = f"assets/{asset_hash[:2]}/{asset_hash}.mp4" if media else None
        if media:
            write(asset_path, asset)
        records, checks, byte_count = [], [], 0
        for index in range(count):
            record = {"dataset_name": name, "record_id": f"{name}::r{index+1}",
                      "training_text": "Actual full text \\n " + str(index),
                      "training_data": {"content": "complete content", "nested": [1, None]},
                      "provenance": {"source_url": "https://example.org/original", "license": "unknown"}}
            if media:
                record["raw_asset"] = {"relative_path": "../../../../../" + asset_path,
                                       "sha256": asset_hash, "bytes": len(asset)}
            payload = encoded(record)
            file = f"record_{index+1:08}.json"
            records.append((file, payload))
            checks.append({"path": file, "sha256": digest(payload), "sourceSha256": digest(payload),
                           "preservedFieldsSha256": digest(encoded({k: v for k, v in record.items() if k != "raw_asset"})),
                           "sourceTextSha256": digest(encoded({k: record.get(k) for k in ("text", "training_text", "training_data", "provenance")})),
                           "assetSha256": asset_hash})
            byte_count += len(payload)
        archive_path = source / f"record_archives/{name}.zip"
        archive_path.parent.mkdir()
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for file, payload in records:
                archive.writestr(file, payload)
            archive.writestr("_records_manifest.json", encoded(checks))
        archive_info = {"path": str(archive_path.relative_to(source)), "bytes": archive_path.stat().st_size,
                        "sha256": digest(archive_path.read_bytes())}
        files.append(archive_info)
        metadata = {"datasetName": name, "domain": "movie", "dataType": "video" if media else "text",
                    "recordCount": count, "storageBytes": 219284168029, "qualityScore": 87.2,
                    "samplePreviews": [{"recordFile": "records/record_00000001.json"}]}
        write(b.CATALOG + "/dataset_catalog.json", encoded({"datasets": [metadata]}))
        write(b.CATALOG + "/dataset_catalog_zh.html", b'<html><a href="detail.html">Report</a></html>')
        write(b.CATALOG + "/detail.html", b"<html>Real report</html>")
        label_path = b.LABEL_FILES["movie"]
        write(label_path, encoded({"dataset_name": name, "record_id": f"{name}::r1", "movie_genre": "drama", "creator_names": ["first", "second"]}) + b"\n")
        write("launch.py", b"MUST NOT COPY")
        write("runtime/private.log", b"MUST NOT COPY")
        static = [x["path"] for x in files if x["path"].startswith("output/")]
        manifest = {"datasetCount": 1, "recordCount": count, "staticFiles": static, "files": files,
                    "datasets": [{"name": name, "records": count, "mediaRecords": count if media else 0,
                                  "recordBytes": byte_count, "sourceRecordBytes": byte_count,
                                  "archive": archive_info["path"], "archiveSha256": archive_info["sha256"],
                                  "originalLogicalStorageBytes": metadata["storageBytes"]}],
                    "uniqueAssets": int(media), "uniqueAssetBytes": len(asset) if media else 0,
                    "originalLogicalStorageBytes": metadata["storageBytes"]}
        (source / "package_manifest.json").write_bytes(encoded(manifest))
        return b, source, self.root / "delivery", name, records, asset, manifest

    def test_builder_exists(self):
        self.assertTrue(callable(builder().build))

    def test_classic_chunk_and_asset_hash_contract(self):
        b = builder()
        values = [{"file": "record_1.json", "record": {"text": "full text"}, "labels": {}, "assetHash": None}]
        desc = b.write_record_chunk(self.root, "example", 1, values)
        identifier, encoding, payload = script_args(self.root / desc["path"])
        compressed = base64.b64decode(payload, validate=True)
        self.assertEqual((identifier, encoding), (desc["id"], "gzip-base64"))
        self.assertEqual(digest(compressed), desc["sha256"])
        self.assertEqual(len(compressed), desc["bytes"])
        self.assertEqual(json.loads(gzip.decompress(compressed)), values)
        self.assertEqual(len(gzip.decompress(compressed)), desc["decodedBytes"])

    def test_chunk_caps_and_oversize_are_not_silently_truncated(self):
        b = builder()
        records = [{"record": {"text": "x" * 30}, "file": str(i)} for i in range(101)]
        self.assertEqual([len(x) for x in b.split_records(records)], [50, 50, 1])
        chunks = list(b.split_records(records, max_bytes=250))
        self.assertTrue(all(len(encoded(x)) <= 250 for x in chunks))
        self.assertEqual([x for c in chunks for x in c], records)
        with self.assertRaisesRegex(ValueError, "oversize"):
            list(b.split_records([{"record": {"text": "x" * 300}}], max_bytes=250))

    def test_labels_match_backend_including_null_and_duplicate_semantics(self):
        b, source, _, name, _, _, _ = self.fixture(count=1)
        path = source / b.LABEL_FILES["movie"]
        with path.open("ab") as stream:
            stream.write(encoded({"dataset_name": name, "record_id": name + "::r1", "creator_names": []}) + b"\n")
        edu = source / b.LABEL_FILES["education"]
        edu.parent.mkdir(parents=True)
        edu.write_bytes(encoded({"dataset_name": "edu", "record_id": "r", "labels": {"subject": {"label_zh": "", "label": "math"}}}) + b"\n")
        labels = b.load_labels(source, {name: {"domain": "movie"}, "edu": {"domain": "education"}})
        self.assertEqual(list(labels[name][name + "::r1"].values()), [None, None, None])
        self.assertEqual(list(labels["edu"]["r"].values()), ["math", None, None])
        lit = source / b.LABEL_FILES["literature"]
        lit.parent.mkdir(parents=True)
        lit.write_bytes(encoded({"dataset_name": "lit", "record_id": "r", "labels": {"literary_genre": {"label_zh": "novel", "label": "fallback"}, "character_relationship": None}}) + b"\n")
        self.assertEqual(list(b.load_labels(source, {"lit": {"domain": "literature"}})["lit"]["r"].values()), ["novel", None, None])

    def test_full_build_preserves_records_labels_paths_and_catalog_on_resume(self):
        b, source, target, name, records, asset, _ = self.fixture(media=True)
        result = b.build(source, target, workers=1)
        self.assertEqual(result["recordsVerified"], 51)
        self.assertEqual(result["mediaRecordsVerified"], 51)
        self.assertEqual(result["failedRecords"], 0)
        index_text = (target / "offline/data-index.js").read_text()
        index = json.loads(index_text.removeprefix("window.OFFLINE_DATA_INDEX=").removesuffix(";\n"))
        dataset = index["datasets"][name]
        self.assertTrue(index["complete"])
        self.assertIs(index.get("buildComplete"), True)
        self.assertEqual(dataset["storageBytes"], 219284168029)
        self.assertEqual(dataset["qualityScore"], 87.2)
        self.assertEqual(dataset["uniqueAssetBytes"], len(asset))
        self.assertEqual(dataset["assetBytes"], len(asset) * 51)
        values = []
        for chunk in dataset["chunks"]:
            args = script_args(target / chunk["path"])
            values.extend(json.loads(gzip.decompress(base64.b64decode(args[2]))))
        for value, (file, original) in zip(values, records):
            self.assertEqual(value["file"], file)
            self.assertEqual(encoded(value["record"]), original)
            self.assertEqual(value["assetHash"], digest(asset))
        self.assertEqual(list(values[0]["labels"].values()), ["drama", None, "first"])
        descriptor = index["assets"][digest(asset)]
        args = script_args(target / descriptor["path"])
        self.assertEqual(args[1], "base64")
        self.assertEqual(base64.b64decode(args[2]), asset)
        self.assertEqual((target / descriptor["binaryPath"]).read_bytes(), asset)
        sample = dataset["sampleRecords"][0]
        self.assertEqual((target / sample["path"]).read_bytes(), records[0][1])
        self.assertEqual((target / sample["path"]).parent.joinpath(values[0]["record"]["raw_asset"]["relative_path"]).read_bytes(), asset)
        self.assertTrue((target / "offline/reports/sample-records.html").is_file())
        self.assertFalse((target / "record_archives").exists())
        self.assertFalse((target / "launch.py").exists())
        self.assertFalse((target / "runtime").exists())
        html = target / b.CATALOG / "dataset_catalog_zh.html"
        html.write_text("main agent integrated UI")
        mtime = (target / dataset["chunks"][0]["path"]).stat().st_mtime_ns
        again = b.build(source, target, workers=1)
        self.assertEqual(html.read_text(), "main agent integrated UI")
        self.assertEqual((target / dataset["chunks"][0]["path"]).stat().st_mtime_ns, mtime)
        self.assertEqual(again["datasetsReused"], 1)

    def test_tampered_or_missing_media_blocks_index_publication(self):
        b, source, target, _, _, _, manifest = self.fixture(media=True)
        asset = source / next(x["path"] for x in manifest["files"] if x["path"].startswith("assets/"))
        asset.write_bytes(b"x" * asset.stat().st_size)
        with self.assertRaisesRegex(ValueError, "checksum"):
            b.build(source, target, workers=1)
        self.assertFalse((target / "offline/data-index.js").exists())
        asset.unlink()
        with self.assertRaises((FileNotFoundError, ValueError)):
            b.build(source, target, workers=1)

    def test_unsafe_manifest_paths_are_rejected(self):
        b = builder()
        for path in ("../outside", "/absolute", "C:/Windows/config", "a\\b", "x/../../bad"):
            with self.assertRaisesRegex(ValueError, "path"):
                b.safe_path(self.root, path)

    def test_manifest_change_invalidates_checkpoint(self):
        b, source, target, name, _, _, _ = self.fixture(count=1)
        b.build(source, target, workers=1)
        path = source / "package_manifest.json"
        manifest = json.loads(path.read_text())
        manifest["newSourceRevision"] = 2
        path.write_bytes(encoded(manifest))
        result = b.build(source, target, workers=1)
        self.assertEqual(result["datasetsReused"], 0)

    def test_generated_chunk_corruption_is_not_trusted_by_resume(self):
        b, source, target, name, _, _, _ = self.fixture(count=2)
        b.build(source, target, workers=1)
        path = target / "offline/records" / name / "00001.js"
        expected = path.read_bytes()
        path.write_bytes(b"x" * len(expected))
        result = b.build(source, target, workers=1)
        self.assertEqual(result["datasetsReused"], 0)
        self.assertEqual(path.read_bytes(), expected)

    def test_media_signature_validation_matches_original_kinds(self):
        b = builder()
        self.assertEqual(b.media_types(b"%PDF-1.7"), ["pdf"])
        self.assertEqual(b.media_types(b"\x89PNG\r\n\x1a\n"), ["image"])
        self.assertEqual(b.media_types(b"\x00\x00\x00\x20ftypisom"), ["video"])
        self.assertEqual(b.media_types(b""), [])
        self.assertEqual(b.media_types(b"<html>not media</html>"), [])

    def test_preview_index_is_explicitly_incomplete_and_keeps_required_global(self):
        b = builder()
        payload = b.index_payload({"a": {"recordCount": 10}}, {}, "a" * 64,
                                  {"datasetCount": 2, "recordCount": 20}, complete=False)
        index = json.loads(payload.removeprefix(b"window.OFFLINE_DATA_INDEX=").removesuffix(b";\n"))
        self.assertFalse(index["complete"])
        self.assertIs(index.get("buildComplete"), False)
        self.assertEqual(index["buildStatus"], "building")
        self.assertEqual(index["expectedRecordCount"], 20)
        self.assertEqual(index["recordCount"], 10)


if __name__ == "__main__":
    unittest.main()
