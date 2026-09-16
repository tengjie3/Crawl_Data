"""Create a separately verified publication copy. Never modify canonical sources."""
import argparse
import base64
import collections
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time
import zipfile

import audit_zip as auditor

ROOT = Path(__file__).resolve().parent
NAME = "intelligent-data-discovery-html-only-2026-09-16"
INDEX_PREFIX = b"window.OFFLINE_DATA_INDEX="
TOKENS = set()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def hash_file(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(1024 * 1024):
            result.update(data)
    return result.hexdigest()


def atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".partial")
    temp.write_bytes(payload)
    temp.replace(path)


def write_json(path, value):
    atomic(path, encoded(value) + b"\n")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def safe_path(root, relative):
    p = PurePosixPath(relative)
    require(not p.is_absolute() and ".." not in p.parts and "\\" not in relative and ":" not in relative, "unsafe relative path")
    destination = root.joinpath(*p.parts)
    require(destination.resolve().is_relative_to(root.resolve()), "path escapes publication directory")
    return destination


def progress(stage, **values):
    message = {"stage": stage, "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "sourceZipSha256": auditor.EXPECTED_SHA, **values}
    write_json(ROOT / "sanitization_progress.json", message)
    print(json.dumps(message), flush=True)


def redact_text(text):
    counts = collections.Counter()
    for marker, category, pattern in auditor.RULES:
        present = marker.lower() in text.lower() if pattern.flags & re.I else marker in text
        if not present:
            continue

        def replace(match):
            value = match.group(1) if match.lastindex else match.group()
            if value in auditor.KNOWN_VALUES:
                return match.group()
            body = re.sub(r"^(?:sk-(?:proj-|svcacct-)?|gh[pousr]_|github_pat_)", "", value)
            if category != "google_api_key" and len(body) >= 20 and set(body) <= set("xX0"):
                return match.group()
            counts[category] += 1
            if category == "google_api_key":
                TOKENS.add(value)
            replacement = "[REDACTED_GOOGLE_API_KEY]" if category == "google_api_key" else "[REDACTED_CREDENTIAL]"
            if match.lastindex:
                start, end = match.span(1)
                return match.group()[:start - match.start()] + replacement + match.group()[end - match.start():]
            return replacement

        text = pattern.sub(replace, text)
    prefix = "/Users/tengjie/Desktop/VibeDataBot-dev/"
    if prefix in text:
        counts["author_machine_path"] += text.count(prefix)
        text = text.replace(prefix, "project-relative/")
    for prefix in ("/Users/tengjie/", "/Users/tengjie3/"):
        if prefix in text:
            counts["author_machine_path"] += text.count(prefix)
            text = text.replace(prefix, "[REDACTED_AUTHOR_HOME]/")
    text, count = re.subn(r"[A-Za-z]:\\+(?:Users)\\+(?:tengjie3?)\\+", "[REDACTED_AUTHOR_HOME]/", text, flags=re.I)
    counts["author_machine_path"] += count
    return text, {key: value for key, value in counts.items() if value}


def redact_value(value):
    counts = collections.Counter()
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        result = []
        for child in value:
            updated, found = redact_value(child)
            result.append(updated)
            counts.update(found)
        return result, dict(counts)
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            updated, found = redact_value(child)
            result[key] = updated
            counts.update(found)
        return result, dict(counts)
    return value, {}


def callback(identifier, compressed):
    return b"window.OfflineData.receive(" + encoded([identifier, "gzip-base64", base64.b64encode(compressed).decode("ascii")])[1:-1] + b");\n"


def decode_chunk(script, descriptor):
    prefix = b"window.OfflineData.receive("
    require(script.startswith(prefix) and script.endswith(b");\n"), "invalid callback")
    identifier, encoding, payload = json.loads(b"[" + script[len(prefix):-3] + b"]")
    require(identifier == descriptor["id"] and encoding == "gzip-base64", "callback identity mismatch")
    compressed = base64.b64decode(payload, validate=True)
    require(len(compressed) == descriptor["bytes"] and digest(compressed) == descriptor["sha256"], "compressed checksum mismatch")
    decoded = gzip.decompress(compressed)
    require(len(decoded) == descriptor["decodedBytes"], "decoded size mismatch")
    rows = json.loads(decoded)
    require(len(rows) == descriptor["records"], "record count mismatch")
    return rows


def make_chunk(rows, descriptor):
    decoded = encoded(rows)
    compressed = gzip.compress(decoded, compresslevel=6, mtime=0)
    script = callback(descriptor["id"], compressed)
    updated = {**descriptor, "records": len(rows), "bytes": len(compressed), "sha256": digest(compressed),
               "decodedBytes": len(decoded), "scriptBytes": len(script), "scriptSha256": digest(script)}
    require(decode_chunk(script, updated) == rows, "changed chunk round-trip mismatch")
    return script, updated


def selected_text(record):
    training = record.get("training_data") or {}
    values = [record.get("training_text"), training.get("content") if isinstance(training, dict) else None,
              record.get("text"), record.get("content"), record.get("body"), record.get("caption")]
    return next((value for value in values if isinstance(value, str) and value.strip()), "")


def sanitize_chunk(script, descriptor, source_record_archive_sha256=None):
    original = decode_chunk(script, descriptor)
    rows, changes = [], []
    counts = collections.Counter()
    record_delta = text_delta = labels_delta = 0
    for row in original:
        updated, found = redact_value(row)
        if found:
            updated["publicationRedactions"] = [{"category": category, "count": count} for category, count in sorted(found.items())]
            updated["record"]["publication_redactions"] = copy.deepcopy(updated["publicationRedactions"])
            updated["record"]["publication_provenance"] = {"source_zip_sha256": auditor.EXPECTED_SHA,
                                                         "scope": "Credential-substring redactions in a separate publication copy; original provenance retained."}
            if source_record_archive_sha256:
                updated["record"]["publication_provenance"]["source_record_archive_sha256"] = source_record_archive_sha256
            changes.append({"file": row["file"], "redactions": updated["publicationRedactions"]})
            record_delta += len(encoded(updated["record"])) - len(encoded(row["record"]))
            text_delta += len(selected_text(updated["record"]).encode()) - len(selected_text(row["record"]).encode())
            labels_delta += len(encoded(updated["labels"])) - len(encoded(row["labels"]))
            counts.update(found)
        require(updated.get("assetHash") == row.get("assetHash"), "media identity changed")
        rows.append(updated)
    changed, updated_descriptor = make_chunk(rows, descriptor)
    return changed, updated_descriptor, {"changedRecords": len(changes), "records": changes, "categoryCounts": dict(counts),
                                         "recordBytesDelta": record_delta, "textBytesDelta": text_delta, "labelsBytesDelta": labels_delta}


def capped_chunks(rows, original_descriptor):
    groups, group, size = [], [], 2
    for row in rows:
        length = len(encoded(row))
        require(length + 2 <= 2_000_000, "single publication record exceeds decoded cap")
        if group and (len(group) == 50 or size + 1 + length > 2_000_000):
            groups.append(group)
            group, size = [], 2
        size += length + bool(group)
        group.append(row)
    if group:
        groups.append(group)
    result = []
    for n, values in enumerate(groups):
        descriptor = dict(original_descriptor)
        if n:
            descriptor["id"] += f":publication-{n}"
            descriptor["path"] = descriptor["path"].removesuffix(".js") + f"-publication-{n}.js"
        result.append(make_chunk(values, descriptor))
    return result


def run(source_zip, source_package, audit_path):
    target, output_zip = ROOT / NAME, ROOT / (NAME + ".zip")
    require(not target.is_relative_to(source_package.resolve()), "publication must be outside original package")
    original_zip_stat = source_zip.stat()
    require(hash_file(source_zip) == auditor.EXPECTED_SHA, "canonical ZIP checksum mismatch")
    original_audit = json.loads(audit_path.read_text())
    require(original_audit["sha256Matches"] and original_audit["actualSha256"] == auditor.EXPECTED_SHA
            and original_audit["counts"]["records"] == 855000 and original_audit["counts"]["validationErrors"] == 0,
            "complete canonical audit required")
    if target.exists():
        state = ROOT / "sanitization_progress.json"
        require(state.exists() and json.loads(state.read_text()).get("sourceZipSha256") == auditor.EXPECTED_SHA, "refusing to overwrite unowned publication directory")
    else:
        progress("cloning-publication-copy")
        if sys.platform == "darwin":
            subprocess.run(["/bin/cp", "-cR", str(source_package), str(target)], check=True, capture_output=True)
        else:
            shutil.copytree(source_package, target, ignore=shutil.ignore_patterns(".DS_Store"))
    changed_chunks, changed_static, records_changed = [], [], 0
    redaction_counts = collections.Counter()
    with zipfile.ZipFile(source_zip) as archive:
        index_name = next(name for name in archive.namelist() if name.endswith("/offline/data-index.js"))
        source_root = index_name[:-len("offline/data-index.js")]
        manifest_payload = archive.read(source_root + "package_manifest.json")
        original_manifest = json.loads(manifest_payload)
        baseline = {row["path"]: row for row in original_manifest["files"]}
        original_names = {item.filename.removeprefix(source_root) for item in archive.infolist() if not item.is_dir()}
        baseline["package_manifest.json"] = {"path": "package_manifest.json", "bytes": len(manifest_payload), "sha256": digest(manifest_payload)}
        progress("verify-cloned-copy", total=len(original_names))
        for root, dirs, files in os.walk(target, topdown=False):
            for name in files:
                file = Path(root) / name
                relative = file.relative_to(target).as_posix()
                if name == ".DS_Store" or relative not in original_names:
                    file.unlink()
        restored = 0
        for n, relative in enumerate(sorted(original_names), 1):
            destination = safe_path(target, relative)
            expected = baseline.get(relative)
            if expected is None:
                payload = archive.read(source_root + relative)
                expected = baseline[relative] = {"path": relative, "bytes": len(payload), "sha256": digest(payload)}
            if not destination.is_file() or destination.stat().st_size != expected["bytes"] or hash_file(destination) != expected["sha256"]:
                payload = archive.read(source_root + relative)
                require(digest(payload) == expected["sha256"], "canonical manifest/file checksum mismatch")
                atomic(destination, payload)
                restored += 1
            if n % 20000 == 0:
                progress("verify-cloned-copy", files=n, total=len(original_names), restoredFromCanonicalZip=restored)
        index_payload = archive.read(index_name)
        index = json.loads(index_payload.removeprefix(INDEX_PREFIX).removesuffix(b";\n"))
        require(index["buildComplete"] and index["recordCount"] == 855000, "source index incomplete")
        targets = {row["file"].split("::", 1)[0] for row in original_audit["blockers"] if row["file"].startswith("offline/records/")}
        seen_targets = set()
        for dataset_name, dataset in index["datasets"].items():
            updated_chunks = []
            for descriptor in dataset["chunks"]:
                if descriptor["path"] not in targets:
                    updated_chunks.append(descriptor)
                    continue
                seen_targets.add(descriptor["path"])
                original_script = archive.read(source_root + descriptor["path"])
                script, updated, evidence = sanitize_chunk(original_script, descriptor, dataset.get("sourceArchiveSha256"))
                require(evidence["changedRecords"] > 0, "flagged chunk had no credential redaction")
                values = decode_chunk(script, updated)
                dataset["maxRecordBytes"] = max(dataset["maxRecordBytes"], max(len(encoded(row["record"])) for row in values))
                dataset["maxRecordBytesIsConservativeUpperBound"] = True
                dataset["recordByteEstimatesIncludePublicationMetadata"] = True
                produced = capped_chunks(values, descriptor)
                for new_script, new_descriptor in produced:
                    atomic(target / new_descriptor["path"], new_script)
                    updated_chunks.append(new_descriptor)
                samples = {row["file"]: row for row in dataset.get("sampleRecords", [])}
                changed_record_files = {row["file"] for row in evidence["records"]}
                for row in values:
                    if row["file"] in changed_record_files and row["file"] in samples:
                        sample = samples[row["file"]]
                        payload = encoded(row["record"])
                        atomic(target / sample["path"], payload)
                        sample.update(bytes=len(payload), sha256=digest(payload), publicationRedactions=row["publicationRedactions"])
                records_changed += evidence["changedRecords"]
                redaction_counts.update(evidence["categoryCounts"])
                for field, delta_field in (("recordBytes", "recordBytesDelta"), ("textBytes", "textBytesDelta"), ("labelsBytes", "labelsBytesDelta")):
                    dataset[field] += evidence[delta_field]
                changed_chunks.append({"path": descriptor["path"], "dataset": dataset_name,
                                       "sourceChunkSha256": descriptor["sha256"], "publicationChunks": [row[1] for row in produced], **evidence})
            dataset["chunks"] = updated_chunks
            dataset["recordChunkBytes"] = sum(row["bytes"] for row in updated_chunks)
            dataset["recordChunkDecodedBytes"] = sum(row["decodedBytes"] for row in updated_chunks)
            dataset["recordChunkScriptBytes"] = sum(row["scriptBytes"] for row in updated_chunks)
            dataset["maxChunkDecodedBytes"] = max(row["decodedBytes"] for row in updated_chunks)
            require(sum(row["records"] for row in updated_chunks) == dataset["recordCount"], "dataset count changed")
        require(seen_targets == targets, "not all flagged chunks were handled")
        progress("records-redacted", chunks=len(changed_chunks), records=records_changed,
                 distinctGoogleTokens=len(TOKENS), replacements=dict(redaction_counts))
        static_targets = {row["file"].split("::", 1)[0] for row in original_audit["blockers"] if not row["file"].startswith("offline/records/")}
        for relative in sorted(static_targets):
            original = archive.read(source_root + relative)
            if Path(relative).suffix == ".json":
                updated, counts = redact_value(json.loads(original))
                if relative == "VERIFICATION.json":
                    updated["publicationScope"] = "Historical verification of the canonical 2026-09-15 package, before publication redactions. Not a browser acceptance claim for this new copy."
                    updated["sourceZipSha256"] = auditor.EXPECTED_SHA
                payload = json.dumps(updated, ensure_ascii=False, indent=2).encode() + b"\n"
            else:
                updated, counts = redact_text(original.decode("utf-8"))
                payload = updated.encode("utf-8")
            atomic(target / relative, payload)
            changed_static.append({"path": relative, "redactions": counts})
            redaction_counts.update(counts)
        atomic(target / "SOURCE/package_manifest.json", manifest_payload)
        source_evidence = {"scope": "SOURCE evidence only, before publication security redactions", "canonicalZip": source_zip.name,
                           "canonicalZipSha256": auditor.EXPECTED_SHA, "canonicalZipBytes": original_zip_stat.st_size,
                           "sourcePackageManifestSha256": digest(manifest_payload), "sourceDataIndexSha256": digest(index_payload),
                           "sourcePortableManifestSha256": index["sourceManifestSha256"],
                           "recordHashChains": "../offline/reports/record-chains.json",
                           "hashChainSemantics": "Original archive/record hashes remain source evidence. New compressed and script hashes are in the publication data index. No per-token values or hashes are stored."}
        write_json(target / "SOURCE/evidence.json", source_evidence)
    index["publication"] = {"date": "2026-09-16", "sourceZipSha256": auditor.EXPECTED_SHA,
                            "redactedRecords": records_changed, "redactionCategories": dict(redaction_counts),
                            "originalRecordsAndMediaPreservedExceptCredentialSubstrings": True,
                            "sourceArchiveHashesAreHistoricalEvidence": True}
    atomic(target / "offline/data-index.js", INDEX_PREFIX + encoded(index) + b";\n")
    disclosure = {
        "publicationDate": "2026-09-16", "sourceZipSha256": auditor.EXPECTED_SHA,
        "datasets": len(index["datasets"]), "records": index["recordCount"], "mediaObjects": len(index["assets"]),
        "changedRecordChunks": len(changed_chunks), "changedRecords": records_changed,
        "distinctGoogleTokensRemoved": len(TOKENS), "replacementCounts": dict(redaction_counts),
        "changedChunks": changed_chunks, "changedStaticFiles": changed_static,
        "policy": "Only matched credential substrings and the author local-machine prefix are replaced. Complete samples, row order, labels, media identities, scores and research content otherwise remain. Public third-party research paths and URLs are not secrets and are retained.",
        "googleContext": "Matches occur inside decoded structured records, not encoded script payloads or filenames. No token values or per-token hashes are recorded. Tokens were conservatively removed without online validity checks.",
        "sourceEvidence": "SOURCE/evidence.json and SOURCE/package_manifest.json preserve canonical provenance and file hashes. Historical VERIFICATION.json and offline/reports concern the pre-publication source unless explicitly noted.",
        "legalLimitations": "Accessible public samples are not necessarily open-licensed for redistribution or training. Source-specific copyright, license, privacy and attribution review remains required; original unknown/NOASSERTION metadata is not converted into a rights guarantee.",
    }
    write_json(target / "PUBLICATION_REDACTIONS.json", disclosure)
    readme = target / "README.md"
    readme_text = readme.read_text(encoding="utf-8")
    readme_text += "\n\n## Publication Security Hygiene (2026-09-16)\n\nThis is a separate publication copy. All 855,000 complete records and original media remain; matched credential substrings were replaced by [REDACTED_GOOGLE_API_KEY] (or a category-specific credential marker). Changed rows carry publicationRedactions category/count metadata. Original local data and the 2026-09-15 canonical ZIP were not edited. See PUBLICATION_REDACTIONS.json and SOURCE/evidence.json.\n\nHistorical VERIFICATION.json and offline/reports describe the source package before these redactions, not new browser acceptance. Public research paths and URLs remain intact. A public source is not proof of an open redistribution or training license; source-specific rights, privacy and attribution review still applies.\n"
    atomic(readme, readme_text.encode("utf-8"))
    progress("verify-publication-delta")
    inventory, changed, unchanged, delta_findings = [], [], 0, []
    for root, dirs, files in os.walk(target):
        for name in sorted(files):
            file = Path(root) / name
            relative = file.relative_to(target).as_posix()
            if relative == "package_manifest.json":
                continue
            require(name != ".DS_Store" and not name.endswith(".partial"), "unwanted output file")
            info = {"path": relative, "bytes": file.stat().st_size, "sha256": hash_file(file)}
            inventory.append(info)
            if relative in baseline and info == baseline[relative]:
                unchanged += 1
                continue
            changed.append(relative)
            if relative.startswith("offline/records/") and relative.endswith(".js"):
                descriptor = next(row for dataset in index["datasets"].values() for row in dataset["chunks"] if row["path"] == relative)
                rows = decode_chunk(file.read_bytes(), descriptor)
                require(descriptor["decodedBytes"] <= 2_000_000 and len(rows) <= 50, "publication chunk cap exceeded")
                delta_findings.extend(auditor.scan_text(encoded(rows).decode(), relative + "::decoded-gzip-json", True))
            elif file.suffix.lower() in auditor.TEXT_SUFFIXES:
                delta_findings.extend(auditor.scan_text(file.read_text(encoding="utf-8"), relative, "/records/" in relative))
    blockers = [row for row in delta_findings if not row["category"].startswith(("known_placeholder:", "review:"))]
    require(not blockers, "publication delta contains unresolved credential or author-path blockers")
    require(all(row["file"].split("::", 1)[0] in changed for row in original_audit["blockers"]), "an original blocker was not changed")
    require(all(path in original_names or not path.startswith("assets/") for path in changed), "unexpected media changes")
    require(not any(path.startswith(("assets/", "offline/assets/")) for path in changed), "media content changed")
    manifest = {**{key: value for key, value in original_manifest.items() if key not in ("files", "unpackedBytes")},
                "publicationDate": "2026-09-16", "sourceZipSha256": auditor.EXPECTED_SHA,
                "files": sorted(inventory, key=lambda row: row["path"]), "unpackedBytes": sum(row["bytes"] for row in inventory)}
    write_json(target / "package_manifest.json", manifest)
    manifest_path = target / "package_manifest.json"
    manifest_info = {"path": "package_manifest.json", "bytes": manifest_path.stat().st_size, "sha256": hash_file(manifest_path)}
    inventory.append(manifest_info)
    delta_manifest_findings = auditor.scan_text(manifest_path.read_text(), "package_manifest.json")
    require(not [f for f in delta_manifest_findings if not f["category"].startswith(("known_placeholder:", "review:"))], "manifest scan blocker")
    delta = {"status": "passed-with-documented-research-and-license-limitations", "sourceZipSha256": auditor.EXPECTED_SHA,
             "basis": "Canonical full audit of 855000 records plus byte-for-byte identity of every unchanged file and re-decoding/rescanning of every changed/new text file using the same auditor.",
             "unchangedFilesExactSha256": unchanged, "changedOrNewFiles": changed + ["package_manifest.json"],
             "changedOrNewFileCount": len(changed) + 1, "all58OriginalBlockerLocationsAddressed": len(original_audit["blockers"]) == 58,
             "credentialOrAuthorPathBlockers": blockers, "changedRecords": records_changed, "distinctGoogleTokensRemoved": len(TOKENS),
             "replacementCounts": dict(redaction_counts), "datasets": 285, "records": 855000,
             "recordChunks": sum(len(row["chunks"]) for row in index["datasets"].values()),
             "allMediaFilesUnchanged": True, "bodyReplacementPolicy": "Exact credential-substring replacements, never whole-record or whole-field substitution",
             "researchPathTriage": {"originalCandidates": 2322, "excludedHttpUrlLocations": original_audit.get("candidateRefinement", {}).get("removedNonmatchingLocations", 0),
                                     "retainedThirdPartyResearchLocations": original_audit["manualReviewCount"],
                                     "reason": "Remaining non-author locations are in decoded research records, sample previews and generated label/source metadata; not bundled developer credential/config files."},
             "googleFalsePositiveDisposition": "All Google matches were found in decoded record string values, not filename/base64 artifacts. Validity and ownership were not assumed; all were redacted conservatively.",
             "sourceMutation": "none", "networkCalls": "none", "gitActions": "none"}
    write_json(ROOT / "publication_delta_audit.json", delta)
    progress("zip-publication", files=len(inventory), bytes=sum(row["bytes"] for row in inventory))
    partial_zip = output_zip.with_suffix(".zip.partial")
    with zipfile.ZipFile(partial_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for n, row in enumerate(sorted(inventory, key=lambda row: row["path"]), 1):
            archive.write(target / row["path"], NAME + "/" + row["path"])
            if n % 10000 == 0:
                progress("zip-publication", files=n, total=len(inventory))
    partial_zip.replace(output_zip)
    progress("verify-zip-extracted-identity", files=len(inventory))
    expected = {NAME + "/" + row["path"]: row for row in inventory}
    with zipfile.ZipFile(output_zip) as archive:
        require(set(archive.namelist()) == set(expected), "ZIP inventory mismatch")
        for n, info in enumerate(archive.infolist(), 1):
            row = expected[info.filename]
            result = hashlib.sha256()
            with archive.open(info) as stream:
                while payload := stream.read(1024 * 1024):
                    result.update(payload)
            require(info.file_size == row["bytes"] and result.hexdigest() == row["sha256"], "ZIP/extracted file mismatch")
            if n % 20000 == 0:
                progress("verify-zip-extracted-identity", files=n, total=len(inventory))
    require((source_zip.stat().st_size, source_zip.stat().st_mtime_ns) == (original_zip_stat.st_size, original_zip_stat.st_mtime_ns), "canonical ZIP changed during work")
    require(hash_file(source_zip) == auditor.EXPECTED_SHA, "canonical ZIP no longer matches original digest")
    summary = {"status": "ready-for-main-browser-tests", "packagePath": str(target), "zipPath": str(output_zip),
               "zipSha256": hash_file(output_zip), "zipBytes": output_zip.stat().st_size,
               "files": len(inventory), "extractedLogicalBytes": sum(row["bytes"] for row in inventory),
               "datasets": 285, "records": 855000, "recordChunks": delta["recordChunks"], "changedChunks": len(changed_chunks),
               "changedRecords": records_changed, "distinctGoogleTokensRemoved": len(TOKENS), "redactionCounts": dict(redaction_counts),
               "mediaUnchanged": True, "sourceZipSha256": auditor.EXPECTED_SHA, "sourceZipUnchanged": True,
               "allZipFilesMatchExtractedSha256": True, "deltaAuditBlockers": 0,
               "browserTests": "Main agent owns new publication browser acceptance", "uploads": "none"}
    write_json(ROOT / "sanitized_delivery_summary.json", summary)
    delta.update(publicationZipSha256=summary["zipSha256"], publicationZipBytes=summary["zipBytes"], allZipFilesMatchExtractedSha256=True)
    write_json(ROOT / "publication_delta_audit.json", delta)
    progress("complete", **summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-zip", type=Path, required=True)
    parser.add_argument("--source-package", type=Path, required=True)
    parser.add_argument("--audit", type=Path, default=ROOT / "publication_audit.json")
    args = parser.parse_args()
    run(args.source_zip.resolve(), args.source_package.resolve(), args.audit.resolve())


if __name__ == "__main__":
    main()
