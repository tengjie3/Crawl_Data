"""Build file://-loadable data from the validated portable package, read-only.

No server, original source dataset, or third-party dependency is needed here.
The integrator owns HTML/runtime edits; existing static copies are never replaced.
"""
import argparse
import base64
import concurrent.futures
import gzip
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import time
from urllib.parse import unquote, urlsplit
import zipfile

CATALOG = "output/dataset_catalog_2026-09-02"
DATA = "outputs/commoncrawl_249_datasets_min3000_delivery_2026-08-21/datasets"
LABEL_FILES = {
    "movie": "output/pdf/movie_dataset_profile_value_v2_2026-08-20/movie_actual_sample_value_labels.jsonl",
    "education": "output/pdf/education_dataset_profile_value_2026-08-21/education_core_sample_labels.jsonl",
    "literature": "output/pdf/literature_dataset_profile_value_2026-08-24/literature_core_sample_labels.jsonl",
}
MAX_RECORDS = 50
MAX_DECODED_BYTES = 2_000_000
FORMAT_VERSION = "offline-classic-chunks-v2"
WORK_CONTEXT = None


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def file_digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def safe_path(root, relative):
    require(isinstance(relative, str) and bool(relative), "invalid empty path")
    path = PurePosixPath(relative)
    require(not path.is_absolute() and ".." not in path.parts and ":" not in relative
            and "\\" not in relative and "\x00" not in relative, "unsafe path: " + relative)
    target = root.joinpath(*path.parts)
    require(target.resolve().is_relative_to(root.resolve()), "path escapes package: " + relative)
    return target


def atomic_bytes(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_bytes(payload)
    partial.replace(path)


def write_json(path, value):
    atomic_bytes(path, encoded(value) + b"\n")


def read_json(path):
    return json.loads(path.read_bytes())


def verify_file(path, description):
    require(path.is_file(), "missing file: " + str(path))
    require(path.stat().st_size == description["bytes"], "size mismatch: " + str(path))
    require(file_digest(path) == description["sha256"], "checksum mismatch: " + str(path))


def make_script(identifier, encoding, payload):
    arguments = encoded([identifier, encoding, base64.b64encode(payload).decode("ascii")])[1:-1]
    return b"window.OfflineData.receive(" + arguments + b");\n"


def decode_script(path, descriptor, encoding):
    script = path.read_bytes()
    prefix, suffix = b"window.OfflineData.receive(", b");\n"
    require(script.startswith(prefix) and script.endswith(suffix), "invalid classic script: " + str(path))
    identifier, actual_encoding, payload = json.loads(b"[" + script[len(prefix):-len(suffix)] + b"]")
    require(identifier == descriptor["id"] and actual_encoding == encoding, "callback identity mismatch")
    binary = base64.b64decode(payload, validate=True)
    require(len(binary) == descriptor["bytes"] and digest(binary) == descriptor["sha256"], "callback checksum mismatch")
    return binary


def split_records(records, max_records=MAX_RECORDS, max_bytes=MAX_DECODED_BYTES):
    require(0 < max_records <= MAX_RECORDS and 2 < max_bytes <= MAX_DECODED_BYTES, "invalid chunk caps")
    chunk, size = [], 2
    for record in records:
        length = len(encoded(record))
        require(length + 2 <= max_bytes, "oversize record: " + str(record.get("file", "unknown")))
        if chunk and (len(chunk) == max_records or size + 1 + length > max_bytes):
            yield chunk
            chunk, size = [], 2
        size += length + bool(chunk)
        chunk.append(record)
    if chunk:
        yield chunk


def write_record_chunk(root, name, number, values):
    require(re.fullmatch(r"[A-Za-z0-9_-]+", name) is not None, "unsafe dataset path")
    decoded = encoded(values)
    require(0 < len(values) <= MAX_RECORDS and len(decoded) <= MAX_DECODED_BYTES, "oversize chunk")
    compressed = gzip.compress(decoded, compresslevel=6, mtime=0)
    identifier = f"records:{name}:{number:05d}"
    relative = f"offline/records/{name}/{number:05d}.js"
    script = make_script(identifier, "gzip-base64", compressed)
    descriptor = {"id": identifier, "path": relative, "records": len(values), "bytes": len(compressed),
                  "sha256": digest(compressed), "decodedBytes": len(decoded),
                  "scriptBytes": len(script), "scriptSha256": digest(script)}
    path = safe_path(root, relative)
    if not path.exists() or path.read_bytes() != script:
        atomic_bytes(path, script)
    actual = gzip.decompress(decode_script(path, descriptor, "gzip-base64"))
    require(actual == decoded and json.loads(actual) == values, "record chunk round-trip mismatch")
    return descriptor


def load_labels(source, catalog):
    result = {name: {} for name in catalog}
    domains = {row.get("domain") for row in catalog.values()}
    for domain, relative in LABEL_FILES.items():
        path = source / relative
        if domain not in domains or not path.exists():
            continue
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                name = row.get("dataset_name")
                if name not in catalog:
                    continue
                if domain == "movie":
                    labels = dict(zip(["\u7535\u5f71\u7c7b\u522b", "\u5267\u60c5\u4e3b\u9898", "\u4e3b\u521b\u59d3\u540d"],
                        [row.get("movie_genre"), row.get("plot_theme"), (row.get("creator_names") or [None])[0]]))
                else:
                    keys = [("subject", "\u5b66\u79d1\u4e3b\u9898"), ("education_level", "\u6559\u80b2\u9636\u6bb5"),
                            ("learning_task", "\u5b66\u4e60\u6d3b\u52a8")] if domain == "education" else [
                            ("literary_genre", "\u6587\u5b66\u4f53\u88c1"), ("narrative_theme", "\u53d9\u4e8b\u4e3b\u9898"),
                            ("character_relationship", "\u4eba\u7269\u5173\u7cfb")]
                    labels = {title: (row.get("labels", {}).get(key) or {}).get("label_zh") or
                              (row.get("labels", {}).get(key) or {}).get("label") for key, title in keys}
                result[name][row.get("record_id")] = labels
    return result


def text_content(record):
    training = record.get("training_data") or {}
    candidates = [record.get("training_text"), training.get("content") if isinstance(training, dict) else None,
                  record.get("text"), record.get("content"), record.get("body"), record.get("caption")]
    return next((value for value in candidates if isinstance(value, str) and value.strip()), "")


def media_types(prefix):
    signatures = {
        "pdf": prefix.startswith(b"%PDF-"),
        "image": prefix.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"II*\x00", b"MM\x00*"))
                 or (prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"),
        "video": prefix[4:8] == b"ftyp" or prefix.startswith(b"\x1aE\xdf\xa3")
                 or (prefix.startswith(b"RIFF") and prefix[8:12] == b"AVI "),
    }
    return [kind for kind, valid in signatures.items() if valid]


def progress(state, stage, **values):
    message = {"stage": stage, "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **values}
    write_json(state / "progress.json", message)
    print(json.dumps(message), flush=True)


def copy_static(source, target, manifest, state):
    files = {item["path"]: item for item in manifest["files"]}
    selected = set(manifest["staticFiles"])
    selected.update(relative for relative in files if relative.startswith(DATA + "/") and
                    relative.rsplit("/", 1)[-1] in ("dataset_metadata.json", "DATASET_CARD.md"))
    copied, preserved, inventory = 0, [], []
    for index, relative in enumerate(sorted(selected), 1):
        require(relative in files, "static dependency missing from manifest: " + relative)
        require(not relative.startswith(("runtime/", "record_archives/", "outputs/tools/")), "forbidden static path")
        require(Path(relative).suffix.lower() not in (".py", ".sh", ".bat", ".command", ".exe", ".log"), "forbidden static file")
        original, destination = safe_path(source, relative), safe_path(target, relative)
        verify_file(original, files[relative])
        if not destination.exists():
            atomic_bytes(destination, original.read_bytes())
            verify_file(destination, files[relative])
            copied += 1
        elif file_digest(destination) != files[relative]["sha256"]:
            preserved.append(relative)
        inventory.append({**files[relative], "copyPathExists": destination.is_file()})
        if index % 200 == 0:
            progress(state, "static", checked=index, total=len(selected), copied=copied)
    return {"files": inventory, "copied": copied, "preservedIntegratorFiles": preserved}


def build_assets(source, target, manifest, state, fingerprint):
    checkpoint = state / "assets.json"
    previous = read_json(checkpoint) if checkpoint.exists() else {}
    entries = previous.get("assets", {}) if previous.get("fingerprint") == fingerprint else {}
    assets, by_path = {}, {}
    selected = [item for item in manifest["files"] if item["path"].startswith("assets/")]
    for number, item in enumerate(selected, 1):
        original = safe_path(source, item["path"])
        payload = original.read_bytes()
        require(len(payload) == item["bytes"], "asset size mismatch: " + item["path"])
        require(digest(payload) == item["sha256"], "asset checksum mismatch: " + item["path"])
        sha = item["sha256"]
        require(re.fullmatch(r"[0-9a-f]{64}", sha) is not None, "invalid asset SHA256")
        by_path[item["path"]] = sha
        binary_path = safe_path(target, item["path"])
        if not binary_path.exists():
            atomic_bytes(binary_path, payload)
        verify_file(binary_path, item)
        if sha in assets:
            continue
        relative = f"offline/assets/{sha[:2]}/{sha}.js"
        descriptor = {"id": "asset:" + sha, "path": relative, "bytes": len(payload), "sha256": sha,
                      "ext": original.suffix.lower(), "binaryPath": item["path"], "mediaTypes": media_types(payload[:1024])}
        script_path = safe_path(target, relative)
        previous_entry = entries.get(sha)
        reusable = False
        if previous_entry and script_path.is_file():
            reusable = (script_path.stat().st_size == previous_entry["scriptBytes"] and
                        file_digest(script_path) == previous_entry["scriptSha256"])
        if reusable:
            descriptor.update(scriptBytes=previous_entry["scriptBytes"], scriptSha256=previous_entry["scriptSha256"])
        else:
            script = make_script(descriptor["id"], "base64", payload)
            atomic_bytes(script_path, script)
            descriptor.update(scriptBytes=len(script), scriptSha256=digest(script))
        require(decode_script(script_path, descriptor, "base64") == payload, "asset round-trip mismatch")
        assets[sha] = descriptor
        entries[sha] = descriptor
        if number % 2000 == 0:
            write_json(checkpoint, {"fingerprint": fingerprint, "assets": entries})
            progress(state, "assets", filesVerified=number, total=len(selected), bytesVerified=sum(a["bytes"] for a in assets.values()))
    require(len(assets) == manifest["uniqueAssets"], "unique asset count mismatch")
    require(sum(item["bytes"] for item in assets.values()) == manifest["uniqueAssetBytes"], "unique asset bytes mismatch")
    write_json(checkpoint, {"fingerprint": fingerprint, "assets": entries})
    return assets, by_path


def init_worker(source, target, state, fingerprint, assets, by_path):
    global WORK_CONTEXT
    WORK_CONTEXT = source, target, state, fingerprint, assets, by_path


def index_payload(datasets, assets, source_manifest_hash, manifest, complete):
    index = {"schemaVersion": 1, "formatVersion": FORMAT_VERSION, "sourceManifestSha256": source_manifest_hash,
             "complete": complete, "buildComplete": complete, "buildStatus": "ready" if complete else "building",
             "expectedDatasetCount": manifest["datasetCount"], "expectedRecordCount": manifest["recordCount"],
             "datasetCount": len(datasets), "recordCount": sum(row["recordCount"] for row in datasets.values()),
             "assetCount": len(assets), "pathsRelativeTo": "package-root",
             "maxRecordsPerChunk": MAX_RECORDS, "maxDecodedChunkBytes": MAX_DECODED_BYTES,
             "datasets": dict(sorted(datasets.items())), "assets": dict(sorted(assets.items())),
             "notes": {"scores": "Existing archived scores preserved, not a legal or training suitability guarantee.",
                       "archiveScoreWeights": manifest.get("archiveScoreWeights", [25, 20, 25, 30]),
                       "existingUiWeightLabels": manifest.get("existingUiWeightLabels", [25, 25, 25, 25]),
                       "languageClasses": manifest.get("languageClasses", {"identified": 13, "unknown": 1})}}
    return b"window.OFFLINE_DATA_INDEX=" + encoded(index) + b";\n"


def dataset_worker(dataset, metadata, labels):
    source, target, state, fingerprint, assets, by_path = WORK_CONTEXT
    name = dataset["name"]
    require(re.fullmatch(r"[A-Za-z0-9_-]+", name) is not None, "unsafe dataset path")
    archive_path = safe_path(source, dataset["archive"])
    require(file_digest(archive_path) == dataset["archiveSha256"], "archive checksum mismatch: " + name)
    checkpoint_path = state / "datasets" / (name + ".json")
    if checkpoint_path.exists():
        previous = read_json(checkpoint_path)
        if previous.get("fingerprint") == fingerprint and previous.get("archiveSha256") == dataset["archiveSha256"]:
            try:
                for chunk in previous["dataset"]["chunks"]:
                    verify_file(safe_path(target, chunk["path"]), {"bytes": chunk["scriptBytes"], "sha256": chunk["scriptSha256"]})
                for sample in previous["dataset"]["sampleRecords"]:
                    verify_file(safe_path(target, sample["path"]), sample)
                return {**previous, "reused": True}
            except ValueError:
                pass
    counters = {"records": 0, "recordBytes": 0, "textBytes": 0, "labelsBytes": 0, "assetBytes": 0,
                "textRecordCount": 0, "mediaRecordCount": 0, "recordsWithLabels": 0,
                "maxRecordBytes": 0, "maxAssetBytes": 0, "rawAssetPathsVerified": 0}
    used_assets, chunks = set(), []
    sample_files = {preview["recordFile"].removeprefix("records/") for preview in metadata.get("samplePreviews", []) if preview.get("recordFile")}
    sample_records = []
    kind = name.rsplit("_", 1)[-1]
    source_hash_chain = hashlib.sha256()
    with zipfile.ZipFile(archive_path) as archive:
        manifest_bytes = archive.read("_records_manifest.json")
        checks = json.loads(manifest_bytes)
        require(len(checks) == dataset["records"] == metadata["recordCount"], "record count mismatch: " + name)
        expected_names = [item["path"] for item in checks]
        require(len(set(expected_names)) == len(expected_names), "duplicate record path: " + name)
        require(sorted(archive.namelist()) == sorted(expected_names + ["_records_manifest.json"]), "archive entries mismatch: " + name)

        def values():
            for item in checks:
                file = item["path"]
                require(re.fullmatch(r"record_[0-9]+\.json", file) is not None, "unsafe record path: " + file)
                payload = archive.read(file)
                require(digest(payload) == item["sha256"], "record checksum mismatch: " + name + "/" + file)
                require(b"/Users/tengjie/" not in payload, "nonportable record path: " + name + "/" + file)
                record = json.loads(payload)
                require(isinstance(record, dict), "invalid record object")
                preserved = {key: value for key, value in record.items() if key != "raw_asset"}
                require(digest(encoded(preserved)) == item["preservedFieldsSha256"], "preserved fields checksum mismatch")
                # Original text/provenance hashes remain in the already-validated archive chain.
                for key in ("sourceSha256", "sourceTextSha256"):
                    require(re.fullmatch(r"[0-9a-f]{64}", item[key]) is not None, "missing source hash chain")
                source_hash_chain.update(encoded(item))
                asset_hash = item.get("assetSha256")
                if asset_hash:
                    require(asset_hash in assets, "missing asset hash: " + asset_hash)
                    asset = assets[asset_hash]
                    raw = record.get("raw_asset")
                    require(isinstance(raw, dict), "missing raw asset metadata")
                    require(raw.get("sha256") == asset_hash and raw.get("bytes") == asset["bytes"], "raw asset checksum/size mismatch")
                    relative = raw.get("relative_path")
                    require(isinstance(relative, str) and not relative.startswith("/") and "\\" not in relative,
                            "nonportable raw asset path")
                    normalized = posixpath.normpath(f"{DATA}/{name}/records/" + relative)
                    require(by_path.get(normalized) == asset_hash, "raw asset path/hash mismatch: " + normalized)
                    counters["rawAssetPathsVerified"] += 1
                    require(asset["bytes"] > 0 and kind in asset["mediaTypes"], "empty or invalid media: " + name + "/" + file)
                    counters["assetBytes"] += asset["bytes"]
                    counters["maxAssetBytes"] = max(counters["maxAssetBytes"], asset["bytes"])
                    used_assets.add(asset_hash)
                content = text_content(record)
                if kind in ("image", "video", "pdf"):
                    require(bool(asset_hash), "missing media: " + name + "/" + file)
                    counters["mediaRecordCount"] += 1
                else:
                    require(bool(content.strip()), "missing text content: " + name + "/" + file)
                    counters["textRecordCount"] += 1
                label = labels.get(record.get("record_id"), {})
                if file in sample_files:
                    relative = f"{DATA}/{name}/records/{file}"
                    path = safe_path(target, relative)
                    if not path.exists():
                        atomic_bytes(path, payload)
                    sample = {"file": file, "path": relative, "bytes": len(payload), "sha256": item["sha256"],
                              "assetHash": asset_hash, "assetPath": assets[asset_hash]["binaryPath"] if asset_hash else None}
                    verify_file(path, sample)
                    sample_records.append(sample)
                counters["records"] += 1
                counters["recordBytes"] += len(payload)
                counters["maxRecordBytes"] = max(counters["maxRecordBytes"], len(payload))
                counters["textBytes"] += len(content.encode("utf-8"))
                counters["labelsBytes"] += len(encoded(label))
                counters["recordsWithLabels"] += bool(label)
                yield {"file": file, "record": record, "labels": label, "assetHash": asset_hash}

        for number, chunk_values in enumerate(split_records(values()), 1):
            chunks.append(write_record_chunk(target, name, number, chunk_values))
    require(counters["records"] == dataset["records"], "dataset output count mismatch")
    require(counters["recordBytes"] == dataset["recordBytes"], "dataset record bytes mismatch")
    require(counters["mediaRecordCount"] == dataset["mediaRecords"], "dataset media count mismatch")
    require(len(sample_records) == len(sample_files), "missing catalog sample record reference: " + name)
    descriptor = {**metadata, **{key: value for key, value in counters.items() if key != "records"},
                  "recordCount": counters["records"], "chunks": chunks,
                  "uniqueAssetCount": len(used_assets), "uniqueAssetBytes": sum(assets[sha]["bytes"] for sha in used_assets),
                  "recordChunkBytes": sum(item["bytes"] for item in chunks),
                  "recordChunkDecodedBytes": sum(item["decodedBytes"] for item in chunks),
                  "recordChunkScriptBytes": sum(item["scriptBytes"] for item in chunks),
                  "maxChunkDecodedBytes": max(item["decodedBytes"] for item in chunks),
                  "sampleRecords": sample_records,
                  "sourceArchiveSha256": dataset["archiveSha256"]}
    result = {"fingerprint": fingerprint, "archiveSha256": dataset["archiveSha256"], "dataset": descriptor,
              "usedAssets": sorted(used_assets), "recordsManifestSha256": digest(manifest_bytes),
              "recordHashChainSha256": source_hash_chain.hexdigest(), "reused": False}
    write_json(checkpoint_path, result)
    return result


def json_links(value):
    if isinstance(value, list):
        for child in value:
            yield from json_links(child)
    elif isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, str) and (key.lower().endswith("href") or key in ("asset", "labelsPath", "sourceEnrichmentPath")):
                yield child
            elif isinstance(child, (dict, list)):
                yield from json_links(child)


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.body, self.literal = [], [], []
        self.json_script = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.links.extend(attrs[key] for key in ("href", "src", "poster", "data") if attrs.get(key))
        if tag == "script":
            self.json_script = attrs.get("type") == "application/json"
            self.body = []

    def handle_data(self, value):
        (self.body if self.json_script else self.literal).append(value)

    def handle_endtag(self, tag):
        if tag == "script" and self.json_script:
            self.links.extend(json_links(json.loads("".join(self.body))))
            self.json_script = False


def file_links(path):
    if path.suffix == ".json":
        return list(json_links(read_json(path)))
    if path.suffix not in (".html", ".css", ".js"):
        return []
    text = path.read_text(encoding="utf-8")
    parser = Links()
    if path.suffix == ".html":
        parser.feed(text)
        text = "\n".join(parser.literal)
    css = re.findall(r"url\(\s*[\"']?([^\s)\"']+)", text)
    literals = re.findall(r"[\"']((?:\.\.?/|sample_assets/)[^\s\"'<>+]+\.(?:json|html|css|js|jpg|png|webm|mp4|pdf))[\"']", text)
    return parser.links + css + literals


def verify_static_links(source, target, paths):
    checked, failures = [], []
    for owner in paths:
        for link in file_links(source / owner):
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            relative = posixpath.normpath(posixpath.join(posixpath.dirname(owner), unquote(parsed.path)))
            try:
                path = safe_path(target, relative)
                require(path.is_file(), "missing dependency")
                checked.append({"owner": owner, "link": link, "path": relative, "status": "present"})
            except ValueError as error:
                failures.append({"owner": owner, "link": link, "path": relative, "error": str(error)})
    return {"checked": len(checked), "failed": len(failures), "failures": failures, "paths": checked,
            "scope": "Link closure of copied source static content; integrator HTML/runtime changes are separately tested."}


def build(source, target, workers=4, state=None):
    source, target = Path(source).resolve(), Path(target).resolve()
    require(source != target and not target.is_relative_to(source), "source and output must be separate")
    require(workers >= 1, "workers must be positive")
    state = Path(state).resolve() if state else target.parent / "data-build"
    target.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    manifest_payload = (source / "package_manifest.json").read_bytes()
    manifest = json.loads(manifest_payload)
    source_manifest_hash = digest(manifest_payload)
    fingerprint = digest(encoded({"manifest": source_manifest_hash, "format": FORMAT_VERSION,
                                  "maxRecords": MAX_RECORDS, "maxBytes": MAX_DECODED_BYTES}))
    index_path = target / "offline/data-index.js"
    # A failed/resumed build must never expose an old index over partial new data.
    index_path.unlink(missing_ok=True)
    progress(state, "starting", sourceManifestSha256=source_manifest_hash, workers=workers)
    try:
        static = copy_static(source, target, manifest, state)
        catalog = {row["datasetName"]: row for row in read_json(source / CATALOG / "dataset_catalog.json")["datasets"]}
        require(set(catalog) == {row["name"] for row in manifest["datasets"]}, "catalog/manifest datasets mismatch")
        labels = load_labels(source, catalog)
        progress(state, "labels", datasets=len(catalog), labeledRecords=sum(len(value) for value in labels.values()))
        assets, by_path = build_assets(source, target, manifest, state, fingerprint)
        progress(state, "records", datasets=0, records=0, totalDatasets=len(catalog))
        results = []
        context = (source, target, state, fingerprint, assets, by_path)
        jobs = [(row, catalog[row["name"]], labels[row["name"]]) for row in manifest["datasets"]]
        jobs.sort(key=lambda job: (not job[0]["name"].startswith("movie_English"), job[0]["name"]))

        def completed(result):
            results.append(result)
            progress(state, "records", datasets=len(results), totalDatasets=len(jobs),
                     records=sum(row["dataset"]["recordCount"] for row in results),
                     chunks=sum(len(row["dataset"]["chunks"]) for row in results),
                     datasetsReused=sum(row["reused"] for row in results))
            if len(results) <= 4 or len(results) % 25 == 0:
                available = {row["dataset"]["datasetName"]: row["dataset"] for row in results}
                atomic_bytes(index_path, index_payload(available, assets, source_manifest_hash, manifest, complete=False))

        if workers == 1:
            init_worker(*context)
            for job in jobs:
                completed(dataset_worker(*job))
        else:
            with concurrent.futures.ProcessPoolExecutor(max_workers=workers, initializer=init_worker, initargs=context) as pool:
                futures = [pool.submit(dataset_worker, *job) for job in jobs]
                for future in concurrent.futures.as_completed(futures):
                    completed(future.result())
        datasets = {row["dataset"]["datasetName"]: row["dataset"] for row in sorted(results, key=lambda row: row["dataset"]["datasetName"])}
        chunks = [chunk for row in datasets.values() for chunk in row["chunks"]]
        all_used = set().union(*(set(row["usedAssets"]) for row in results))
        require(len(datasets) == manifest["datasetCount"], "output dataset count mismatch")
        require(sum(row["recordCount"] for row in datasets.values()) == manifest["recordCount"], "output record count mismatch")
        progress(state, "path-verification", chunks=len(chunks), assets=len(assets))
        path_report = verify_static_links(source, target, manifest["staticFiles"])
        write_json(target / "offline/reports/static-paths.json", path_report)
        require(path_report["failed"] == 0, "static link closure failed: " + str(path_report["failures"][:3]))
        for descriptor in chunks + list(assets.values()):
            require(safe_path(target, descriptor["path"]).stat().st_size == descriptor["scriptBytes"], "generated script path/size mismatch")
        samples = [{"datasetName": name, **sample} for name, dataset in datasets.items() for sample in dataset["sampleRecords"]]
        sample_rows = []
        for sample in samples:
            raw = '<a href="../../' + html.escape(sample["assetPath"], quote=True) + '">Raw media</a>' if sample["assetPath"] else "Text record"
            sample_rows.append('<tr><td>' + html.escape(sample["datasetName"]) + '</td><td><a href="../../' +
                               html.escape(sample["path"], quote=True) + '">' + html.escape(sample["file"]) +
                               '</a></td><td>' + raw + '</td></tr>')
        sample_page = '<!doctype html><html lang="en"><meta charset="utf-8"><title>Original sample records</title><style>body{font:14px system-ui;margin:24px;color:#171717}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:7px;border-bottom:1px solid #ddd}a{color:#0059a6}</style><h1>Original sample records</h1><table><thead><tr><th>Dataset</th><th>Complete record JSON</th><th>Original media</th></tr></thead><tbody>' + "".join(sample_rows) + '</tbody></table></html>'
        atomic_bytes(target / "offline/reports/sample-records.html", sample_page.encode("utf-8"))
        write_json(target / "offline/reports/sample-records.json", {"records": samples, "status": "all-present",
                   "catalogPresentation": "recordFile is displayed as text, not a hyperlink. This added index links each referenced original sample JSON and its original CAS media. The catalog preview assetHref links reviewed derivatives, not raw CAS."})
        report = {
            "status": "passed", "formatVersion": FORMAT_VERSION, "sourcePackage": source.name,
            "sourceManifestSha256": source_manifest_hash, "buildFingerprint": fingerprint,
            "datasetsVerified": len(datasets), "recordsVerified": sum(row["recordCount"] for row in datasets.values()),
            "textRecordsVerified": sum(row["textRecordCount"] for row in datasets.values()),
            "mediaRecordsVerified": sum(row["mediaRecordCount"] for row in datasets.values()), "failedRecords": 0,
            "recordBytes": sum(row["recordBytes"] for row in datasets.values()),
            "decodedChunkBytes": sum(item["decodedBytes"] for item in chunks),
            "gzipChunkBytes": sum(item["bytes"] for item in chunks), "recordScriptBytes": sum(item["scriptBytes"] for item in chunks),
            "recordChunks": len(chunks), "maxChunkRecords": max(item["records"] for item in chunks),
            "maxChunkDecodedBytes": max(item["decodedBytes"] for item in chunks),
            "assetObjectsVerified": len(assets), "assetBytes": sum(item["bytes"] for item in assets.values()),
            "assetScriptBytes": sum(item["scriptBytes"] for item in assets.values()),
            "referencedAssetObjects": len(all_used), "unreferencedAssetObjects": len(assets) - len(all_used),
            "emptyReferencedAssets": sum(assets[sha]["bytes"] == 0 for sha in all_used),
            "unreferencedEmptyAssets": [sha for sha, item in assets.items() if not item["bytes"] and sha not in all_used],
            "rawAssetPathsVerified": sum(row["rawAssetPathsVerified"] for row in datasets.values()),
            "recordsWithLabels": sum(row["recordsWithLabels"] for row in datasets.values()),
            "staticFilesVerified": len(static["files"]), "staticBytes": sum(item["bytes"] for item in static["files"]),
            "localReferencesVerified": path_report["checked"], "failedPaths": path_report["failed"],
            "generatedPathsVerified": len(chunks) + len(assets), "binaryPathsVerified": len(by_path),
            "catalogSampleRecordFilesVerified": len(samples), "catalogSampleRecordBytes": sum(sample["bytes"] for sample in samples),
            "catalogSampleRawMediaLinksVerified": sum(bool(sample["assetHash"]) for sample in samples),
            "sampleRecordIndexPath": "offline/reports/sample-records.html",
            "preservedIntegratorFiles": static["preservedIntegratorFiles"], "datasetsReused": sum(row["reused"] for row in results),
            "scoresRecalculated": False, "originalLogicalStorageBytes": manifest.get("originalLogicalStorageBytes"),
            "sourceRecordBodyAndProvenance": "Every complete portable record SHA256 and preservedFieldsSha256 checked; generated gzip/base64 round-trip compared exactly. Original-source text/provenance verification inherited from the validated portable archive chain, not recomputed against original data.",
            "sizeSemantics": {"recordBytes": "Exact source portable JSON bytes", "assetBytes": "Dataset fields sum all media references before deduplication; this report sums unique CAS objects", "uniqueAssetBytes": "Exact per-dataset unique binary bytes", "chunks.bytes": "Compressed gzip bytes; sha256 hashes these bytes", "chunks.decodedBytes": "UTF-8 JSON array bytes including wrappers and labels", "textBytes": "UTF-8 selected text before strip, including media captions", "storageBytes": "Original catalog logical storage, unchanged"},
            "remainingIntegration": ["Main agent supplies index.html, browser loader and merge UI", "Main agent runs browser/file-origin tests and final ZIP verification"],
        }
        empty_originals = []
        source_map = source / "asset_source_map.jsonl.gz"
        if source_map.exists():
            with gzip.open(source_map, "rt", encoding="utf-8") as stream:
                for line in stream:
                    item = json.loads(line)
                    if item["bytes"] == 0:
                        empty_originals.append(item["source"])
        report["emptyOriginalFiles"] = empty_originals
        verification = source / "VERIFICATION.json"
        if verification.exists():
            atomic_bytes(target / "offline/reports/source-portable-verification.json", verification.read_bytes())
        write_json(target / "offline/reports/static-inventory.json", static)
        write_json(target / "offline/reports/record-chains.json", [{key: row[key] for key in ("archiveSha256", "recordsManifestSha256", "recordHashChainSha256")} | {"datasetName": row["dataset"]["datasetName"]} for row in results])
        write_json(target / "offline/reports/data-validation.json", report)
        payload = index_payload(datasets, assets, source_manifest_hash, manifest, complete=True)
        write_json(state / "index-publication.json", {"bytes": len(payload), "sha256": digest(payload), "sourceManifestSha256": source_manifest_hash})
        progress(state, "complete", datasets=len(datasets), records=report["recordsVerified"], failedRecords=0,
                 recordChunks=len(chunks), assets=len(assets), indexBytes=len(payload))
        atomic_bytes(index_path, payload)
        return report
    except Exception as error:
        index_path.unlink(missing_ok=True)
        progress(state, "failed", error=str(error), indexPublished=False)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    report = build(args.source, args.output, args.workers, args.state)
    print(json.dumps({key: report[key] for key in ("status", "datasetsVerified", "recordsVerified", "failedRecords", "recordChunks", "assetObjectsVerified", "failedPaths")}), flush=True)


if __name__ == "__main__":
    main()
