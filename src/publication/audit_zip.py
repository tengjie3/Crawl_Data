"""Independent read-only publication scan. Findings never include matched values."""
import argparse
import base64
import collections
import concurrent.futures
import gzip
import hashlib
import html
import json
from pathlib import Path, PurePosixPath
import re
import stat
import time
import zipfile

EXPECTED_SHA = "59895244a6888785e56b60a3757f865cd63b47b37d83d128f00e12c4d40a2602"
WHITELIST_RATIONALES = {
    "aws_documentation_access_id": "Exact AWS documentation example access-key ID; not a usable credential.",
    "aws_documentation_secret": "Exact published AWS documentation example secret ending in EXAMPLEKEY; not a usable credential.",
    "repeated_dummy_token": "Token value consists entirely of repeated x/X/0 placeholder characters, not a random credential.",
    "generic_documentation_path": "The user component is an explicit placeholder such as username, yourname or <user>; not an identifiable author's machine path.",
}
KNOWN_VALUES = {
    "AKIAIOSFODNN7EXAMPLE": "aws_documentation_access_id",
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY": "aws_documentation_secret",
}
RULES = [
    ("sk-", "openai_api_key", re.compile(r"(?<![\w-])sk-(?:(?:proj-|svcacct-)[A-Za-z0-9_-]{40,512}|[A-Za-z0-9]{32,64})(?![\w-])")),
    ("sk-ant-", "anthropic_api_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,512}")),
    ("gh", "github_token", re.compile(r"(?<!\w)gh[pousr]_[A-Za-z0-9]{36,255}(?![A-Za-z0-9])")),
    ("github_pat_", "github_fine_grained_token", re.compile(r"github_pat_[A-Za-z0-9_]{30,255}(?![A-Za-z0-9_])")),
    ("AKIA", "aws_access_key_id", re.compile(r"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])")),
    ("ASIA", "aws_access_key_id", re.compile(r"(?<![A-Z0-9])ASIA[A-Z0-9]{16}(?![A-Z0-9])")),
    ("secret", "aws_secret_access_key", re.compile(r"(?:aws[_ -]?)?secret[_ -]?(?:access[_ -]?)?key[\s\"'\\:=]{1,30}([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])", re.I)),
    ("BEGIN", "private_key_material", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----(?:\s|\\n|\\r)+[A-Za-z0-9+/=]{40,}")),
    ("AIza", "google_api_key", re.compile(r"AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])")),
    ("AccountKey", "azure_storage_account_key", re.compile(r"AccountKey[\s\"'\\:=]{1,12}([A-Za-z0-9+/]{86}==)(?![A-Za-z0-9/+=])", re.I)),
    ("SharedAccessKey", "azure_servicebus_shared_key", re.compile(r"SharedAccessKey[\s\"'\\:=]{1,12}([A-Za-z0-9+/]{43}=)(?![A-Za-z0-9/+=])", re.I)),
    ("refresh_token", "google_oauth_refresh_token", re.compile(r"refresh_token[\s\"'\\:=]{1,20}(1//[A-Za-z0-9_-]{30,})", re.I)),
    ("glpat-", "gitlab_token", re.compile(r"glpat-[A-Za-z0-9_-]{20,255}")),
    ("xox", "slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,255}")),
    ("sk_live_", "stripe_live_secret", re.compile(r"sk_live_[A-Za-z0-9]{20,255}")),
    ("hf_", "huggingface_access_token", re.compile(r"(?<!\w)hf_[A-Za-z0-9]{30,255}(?!\w)")),
]
MACHINE = re.compile(r"(?:/(?:Users|home)/([^/\s\"'<>]+)(?:/|$)|[A-Za-z]:\\+(?:Users|Documents and Settings)\\+([^\\\s\"'<>]+))")
TEXT_SUFFIXES = {".html", ".htm", ".css", ".js", ".json", ".jsonl", ".md", ".txt", ".csv", ".tsv", ".xml", ".svg", ".yaml", ".yml", ".toml", ".ini", ".conf", ".config", ".sh", ".bat", ".py", ".log"}
WORK_ZIP = None


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".partial")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def scan_path(name):
    parts = PurePosixPath(name).parts
    lower = name.lower()
    basename = PurePosixPath(lower).name
    categories = []
    if name.startswith(("/", "\\")) or ".." in parts or re.match(r"^[A-Za-z]:", name):
        categories.append("unsafe_zip_path")
    if any(part.lower() in (".git", ".svn", ".hg") for part in parts):
        categories.append("version_control_private_metadata")
    if basename == ".env" or basename.startswith(".env."):
        categories.append("dotenv_config")
    if any(part.lower() in (".aws", ".azure", ".ssh", ".kube", ".gcloud") for part in parts) or ".config/gcloud/" in lower:
        categories.append("cloud_or_ssh_private_config")
    if basename in ("credentials", "credentials.json", "application_default_credentials.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".npmrc", ".pypirc") or "service-account" in basename or "service_account" in basename:
        categories.append("credential_config_filename")
    if PurePosixPath(lower).suffix in (".pem", ".key", ".p12", ".pfx", ".keystore", ".keychain", ".pst", ".ost", ".eml", ".msg"):
        categories.append("private_key_or_private_attachment")
    if re.search(r"(?:^|[/_. -])(?:passport|payroll|salary|bank.statement|private.attachment|identity.card)(?:[/_. -]|$)", lower):
        categories.append("possible_private_attachment_filename")
    if basename == ".ds_store" or "__macosx" in lower:
        categories.append("local_machine_metadata")
    return [{"file": name, "line": 0, "category": category} for category in categories]


def scan_text(text, name, record_context=False):
    findings = []
    lowered = None
    for marker, category, pattern in RULES:
        if pattern.flags & re.I:
            if lowered is None:
                lowered = text.lower()
            present = marker.lower() in lowered
        else:
            present = marker in text
        if not present:
            continue
        for match in pattern.finditer(text):
            value = match.group(1) if match.lastindex else match.group()
            reason = KNOWN_VALUES.get(value)
            body = re.sub(r"^(?:sk-(?:proj-|svcacct-)?|gh[pousr]_|github_pat_)", "", value)
            if not reason and len(body) >= 20 and set(body) <= set("xX0"):
                reason = "repeated_dummy_token"
            found_category = "known_placeholder:" + reason if reason else category
            findings.append({"file": name, "line": text.count("\n", 0, match.start()) + 1, "category": found_category})
    if "/Users/" in text or "/home/" in text or "Users\\" in text or "Settings\\" in text:
        for match in MACHINE.finditer(text):
            prefix = text[max(0, match.start() - 2048):match.start()]
            if re.search(r"https?://[^\s\\\"'<>]*$", prefix, re.I):
                continue
            username = match.group(1) or match.group(2)
            if username.lower() in ("username", "yourname", "your_name", "your-user", "your_username", "user", "example"):
                category = "known_placeholder:generic_documentation_path"
            elif username.lower() in ("tengjie", "tengjie3"):
                category = "author_machine_path"
            else:
                category = "review:third_party_machine_path_in_research" if record_context else "review:machine_path_in_static_text"
            findings.append({"file": name, "line": text.count("\n", 0, match.start()) + 1, "category": category})
    for match in re.finditer(r"/private/var/folders/[^\s\"'<>]+|/(?:private/)?tmp/(?:discovery|codex)[^\s\"'<>]+", text):
        findings.append({"file": name, "line": text.count("\n", 0, match.start()) + 1, "category": "author_temporary_machine_path"})
    return list({(item["file"], item["line"], item["category"]): item for item in findings}.values())


def init_worker(path):
    global WORK_ZIP
    WORK_ZIP = zipfile.ZipFile(path)


def scan_dataset(job):
    name, root, dataset = job
    findings, scanned, decoded_bytes, records, errors = [], 0, 0, 0, []
    license_indicators = collections.Counter()
    for chunk in dataset["chunks"]:
        relative = chunk["path"]
        virtual = relative + "::decoded-gzip-json"
        try:
            script = WORK_ZIP.read(root + relative)
            prefix = b"window.OfflineData.receive("
            if not script.startswith(prefix) or not script.endswith(b");\n"):
                raise ValueError("invalid_callback")
            identifier, encoding, payload = json.loads(b"[" + script[len(prefix):-3] + b"]")
            if identifier != chunk["id"] or encoding != "gzip-base64":
                raise ValueError("callback_identity")
            compressed = base64.b64decode(payload, validate=True)
            if len(compressed) != chunk["bytes"] or sha256(compressed) != chunk["sha256"]:
                raise ValueError("compressed_checksum")
            decoded = gzip.decompress(compressed)
            if len(decoded) != chunk["decodedBytes"] or len(decoded) > 2_000_000:
                raise ValueError("decoded_size")
            content = decoded.decode("utf-8")
            values = json.loads(content)
            if len(values) != chunk["records"] or len(values) > 50:
                raise ValueError("record_count")
            findings.extend(scan_text(content, virtual, record_context=True))
            # Inspect parsed strings as well when JSON escaping could conceal a marker.
            if any(marker in content for marker in ("\\u0073", "\\u0067", "\\u0041", "\\u002f", "\\u002F")):
                findings.extend(scan_text(json.dumps(values, ensure_ascii=False), virtual + "::unicode-normalized", record_context=True))
            for value in values:
                record = value["record"]
                if not isinstance(record, dict) or not isinstance(value["labels"], dict):
                    raise ValueError("invalid_record_wrapper")
                evidence = json.dumps({key: record.get(key) for key in ("license", "licenses", "rights", "provenance")}, ensure_ascii=False)
                for indicator in ("NOASSERTION", "MULTI_SOURCE_LICENSE_REVIEW_REQUIRED", "unknown"):
                    if indicator.lower() in evidence.lower():
                        license_indicators[indicator] += 1
            scanned += 1
            decoded_bytes += len(decoded)
            records += len(values)
        except Exception as error:
            # Never include error messages that might embed untrusted content.
            errors.append({"file": relative, "line": 0, "category": "chunk_validation_error:" + type(error).__name__})
    if records != dataset["recordCount"]:
        errors.append({"file": name, "line": 0, "category": "dataset_record_count_mismatch"})
    return {"dataset": name, "chunks": scanned, "decodedBytes": decoded_bytes, "records": records,
            "findings": findings, "errors": errors, "licenseIndicators": dict(license_indicators)}


def progress(output, **values):
    state = {"updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **values}
    write_json(output.with_name("publication_audit_progress.json"), state)
    print(json.dumps(state), flush=True)


def refine_file(job):
    root, virtual = job
    actual = virtual.split("::", 1)[0]
    payload = WORK_ZIP.read(root + actual)
    if "::decoded-gzip-json" in virtual:
        arguments = json.loads(b"[" + payload[len(b"window.OfflineData.receive("):-3] + b"]")
        content = gzip.decompress(base64.b64decode(arguments[2], validate=True)).decode("utf-8")
        if virtual.endswith("::unicode-normalized"):
            content = json.dumps(json.loads(content), ensure_ascii=False)
        context = True
    else:
        content = payload.decode("utf-8")
        if virtual.endswith("::html-entity-decoded"):
            content = html.unescape(content)
        context = "/datasets/" in actual and "/records/" in actual
    return scan_text(content, virtual, context)


def refine_existing(path, output, workers):
    report = json.loads(output.read_text())
    check = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            check.update(block)
    if check.hexdigest() != report["actualSha256"]:
        raise ValueError("Canonical archive changed; do not reuse scan findings")
    all_findings = report["blockers"] + report["manualReview"] + report["whitelistedLocations"]
    affected = sorted({f["file"] for f in all_findings if f["line"] > 0 and
                       ("machine_path" in f["category"] or f["category"] == "openai_api_key" or f["category"] == "known_placeholder:generic_documentation_path")})
    kept = [f for f in all_findings if f["file"] not in affected]
    with zipfile.ZipFile(path) as archive:
        root = next(n[:-len("offline/data-index.js")] for n in archive.namelist() if n.endswith("/offline/data-index.js"))
    scanned = 0
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers, initializer=init_worker, initargs=(path,)) as pool:
        for result in pool.map(refine_file, [(root, name) for name in affected], chunksize=10):
            kept.extend(result)
            scanned += 1
            if scanned % 250 == 0 or scanned == len(affected):
                progress(output, stage="candidate-context-review", files=scanned, total=len(affected))
    findings = [{"file": file, "line": line, "category": category} for file, line, category in
                sorted({(f["file"], f["line"], f["category"]) for f in kept})]
    report["whitelistedLocations"] = [f for f in findings if f["category"].startswith("known_placeholder:")]
    report["manualReview"] = [f for f in findings if f["category"].startswith("review:")]
    report["blockers"] = [f for f in findings if not f["category"].startswith(("known_placeholder:", "review:"))]
    report["blockerCount"] = len(report["blockers"])
    report["manualReviewCount"] = len(report["manualReview"])
    report["whitelistedLocationCount"] = len(report["whitelistedLocations"])
    report["findingCategories"] = dict(collections.Counter(f["category"] for f in findings))
    report["status"] = "blocked" if report["blockers"] else "manual-review" if report["manualReview"] else "passed-with-scope-limitations"
    report["candidateRefinement"] = {"filesRescanned": scanned, "canonicalSha256Rechecked": True,
                                    "removedNonmatchingLocations": len(all_findings) - len(findings),
                                    "rules": ["HTTP(S) URL /home/... or /Users/... segments are not local-machine paths", "Non-key sk- frontend identifiers do not match OpenAI credential grammar"],
                                    "researchTextModified": False}
    write_json(output, report)
    progress(output, stage="complete", status=report["status"], blockers=report["blockerCount"],
             manualReview=report["manualReviewCount"], records=report["counts"]["records"], chunks=report["counts"]["decodedRecordChunks"])
    print(json.dumps({key: report[key] for key in ("status", "blockerCount", "manualReviewCount", "whitelistedLocationCount", "findingCategories", "candidateRefinement")}), flush=True)


def audit(path, output, workers=4, expected_sha=EXPECTED_SHA):
    before = path.stat()
    result_hash = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            result_hash.update(block)
    actual_sha = result_hash.hexdigest()
    progress(output, stage="zip-sha256", matches=actual_sha == expected_sha, bytes=before.st_size, sha256=actual_sha)
    findings, errors = [], []
    static_files = static_bytes = asset_scripts_skipped = binary_files_skipped = 0
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [item.filename for item in entries]
        duplicate_names = [name for name, count in collections.Counter(names).items() if count > 1]
        for name in duplicate_names:
            findings.append({"file": name, "line": 0, "category": "duplicate_zip_entry"})
        indexes = [name for name in names if name.endswith("/offline/data-index.js")]
        if len(indexes) != 1:
            raise ValueError("Expected exactly one offline data index")
        root = indexes[0][:-len("offline/data-index.js")]
        raw_index = archive.read(indexes[0])
        prefix = b"window.OFFLINE_DATA_INDEX="
        if not raw_index.startswith(prefix) or not raw_index.endswith(b";\n"):
            raise ValueError("Invalid data index wrapper")
        index = json.loads(raw_index[len(prefix):-2])
        expected_chunks = {root + chunk["path"] for row in index["datasets"].values() for chunk in row["chunks"]}
        actual_chunks = {name for name in names if name.startswith(root + "offline/records/") and name.endswith(".js")}
        if expected_chunks != actual_chunks:
            errors.append({"file": "offline/records/", "line": 0, "category": "record_chunk_inventory_mismatch"})
        for number, entry in enumerate(entries, 1):
            relative = entry.filename.removeprefix(root)
            findings.extend(scan_path(relative))
            if stat.S_ISLNK(entry.external_attr >> 16):
                findings.append({"file": relative, "line": 0, "category": "zip_symlink"})
            if entry.flag_bits & 1:
                findings.append({"file": relative, "line": 0, "category": "encrypted_zip_entry"})
            if entry.is_dir() or entry.filename in expected_chunks:
                continue
            if relative.startswith("offline/assets/") and relative.endswith(".js"):
                asset_scripts_skipped += 1
                continue
            suffix = Path(relative).suffix.lower()
            if suffix not in TEXT_SUFFIXES and not scan_path(relative):
                binary_files_skipped += 1
                continue
            try:
                payload = archive.read(entry)
                text = payload.decode("utf-8")
                record_context = "/datasets/" in relative and "/records/" in relative
                findings.extend(scan_text(text, relative, record_context))
                if suffix in (".html", ".htm", ".xml", ".svg") and "&#" in text:
                    findings.extend(scan_text(html.unescape(text), relative + "::html-entity-decoded", record_context))
                static_files += 1
                static_bytes += len(payload)
            except UnicodeDecodeError:
                errors.append({"file": relative, "line": 0, "category": "text_decode_error"})
            except (OSError, zipfile.BadZipFile):
                errors.append({"file": relative, "line": 0, "category": "zip_read_or_crc_error"})
            if static_files and static_files % 200 == 0:
                progress(output, stage="static-text", files=static_files, bytes=static_bytes)
    progress(output, stage="record-chunks", datasets=0, records=0, staticFiles=static_files, staticBytes=static_bytes)
    results, licenses = [], collections.Counter()
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers, initializer=init_worker, initargs=(path,)) as pool:
        pending = [pool.submit(scan_dataset, (name, root, row)) for name, row in index["datasets"].items()]
        for future in concurrent.futures.as_completed(pending):
            item = future.result()
            findings.extend(item.pop("findings"))
            errors.extend(item.pop("errors"))
            licenses.update(item.pop("licenseIndicators"))
            results.append(item)
            if len(results) % 10 == 0 or len(results) == len(pending):
                progress(output, stage="record-chunks", datasets=len(results), totalDatasets=len(pending),
                         chunks=sum(r["chunks"] for r in results), records=sum(r["records"] for r in results),
                         findingLocations=len(findings), validationErrors=len(errors))
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        errors.append({"file": path.name, "line": 0, "category": "zip_changed_during_audit"})
    if actual_sha != expected_sha:
        errors.append({"file": path.name, "line": 0, "category": "canonical_zip_sha256_mismatch"})
    if not index.get("buildComplete") or index["recordCount"] != 855000 or len(index["datasets"]) != 285:
        errors.append({"file": "offline/data-index.js", "line": 1, "category": "incomplete_data_index"})
    unique = sorted({(f["file"], f["line"], f["category"]) for f in findings})
    findings = [{"file": f, "line": line, "category": category} for f, line, category in unique]
    whitelisted = [f for f in findings if f["category"].startswith("known_placeholder:")]
    review = [f for f in findings if f["category"].startswith("review:")]
    blockers = [f for f in findings if not f["category"].startswith(("known_placeholder:", "review:"))] + errors
    report = {
        "auditType": "Independent read-only canonical ZIP public-publication scan",
        "targetRepository": "tengjie3/Crawl_Data", "canonicalZip": path.name,
        "expectedSha256": expected_sha, "actualSha256": actual_sha, "sha256Matches": actual_sha == expected_sha,
        "zipBytes": before.st_size, "zipEntries": len(entries), "archiveUnchangedDuringAudit": not any(e["category"] == "zip_changed_during_audit" for e in errors),
        "status": "blocked" if blockers else "manual-review" if review else "passed-with-scope-limitations",
        "blockerCount": len(blockers), "blockers": blockers,
        "manualReviewCount": len(review), "manualReview": review,
        "whitelistedLocationCount": len(whitelisted), "whitelistedLocations": whitelisted,
        "whitelistRationales": WHITELIST_RATIONALES,
        "counts": {"datasets": len(results), "records": sum(r["records"] for r in results),
                   "decodedRecordChunks": sum(r["chunks"] for r in results), "expectedRecordChunks": len(expected_chunks),
                   "decodedRecordBytes": sum(r["decodedBytes"] for r in results), "staticTextFiles": static_files,
                   "staticTextBytes": static_bytes, "binaryAssetScriptsExcludedFromTextScan": asset_scripts_skipped,
                   "binaryFilesExcludedFromTextScan": binary_files_skipped, "validationErrors": len(errors)},
        "findingCategories": dict(collections.Counter(f["category"] for f in findings)),
        "licenseIndicatorsInRecords": dict(licenses),
        "lineSemantics": "One-based physical line in scanned text; compact decoded gzip JSON is line 1. ::decoded-gzip-json indicates the JS payload after decoding; line 0 denotes a ZIP path/container finding.",
        "methods": ["Whole ZIP SHA256 compared with the independently supplied canonical digest", "All ZIP entry names checked for traversal, sensitive configuration, VCS metadata, key stores and recognizable private-attachment names", "UTF-8 frontend, reports, metadata and sampled record JSON scanned without printing matched values", "Every indexed gzip/base64 classic-script record chunk decoded; callback ID, gzip SHA256, decoded size and record counts checked", "Recognizable provider-specific tokens, context-bound cloud secrets, private-key material and machine paths scanned", "Only exact known documentation examples or explicit placeholder-only values whitelisted with rationale; public sample context alone does not exempt a recognizable credential"],
        "limitations": ["Pattern scanning is not proof that no secret exists; obfuscated, encrypted, unusual or unrecognized formats may escape detection", "Binary images, audio/video, fonts and PDF internals are not OCR/content-scanned; hashed filenames cannot establish whether media contains personal information", "No credential validity checks, network calls, uploads or Git operations were performed", "Research text and original package bytes were preserved; potentially real tokens remain blockers pending separate human review", "A public source does not establish an open redistribution license. Dataset and underlying media terms require source-specific review; NOASSERTION, unknown and mixed-source limitations are not legal clearance", "This audit does not establish model-training suitability, privacy compliance, copyright ownership or authorization to redistribute every source asset"],
        "sourceMutation": "none", "networkUploads": "none", "gitActions": "none",
    }
    write_json(output, report)
    progress(output, stage="complete", status=report["status"], blockers=len(blockers), manualReview=len(review),
             records=report["counts"]["records"], chunks=report["counts"]["decodedRecordChunks"])
    print(json.dumps({key: report[key] for key in ("status", "sha256Matches", "blockerCount", "manualReviewCount", "whitelistedLocationCount", "counts", "findingCategories")}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--refine-existing", action="store_true")
    parser.add_argument("--expected-sha", default=EXPECTED_SHA)
    args = parser.parse_args()
    if args.refine_existing:
        refine_existing(args.zip, args.output, args.workers)
    else:
        audit(args.zip, args.output, args.workers, args.expected_sha)


if __name__ == "__main__":
    main()
