"""Build reviewable source snapshots and exact, bounded-size release volumes."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile


PART_BYTES = 1_500_000_000


def sha(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--assets", type=Path, required=True)
    args = parser.parse_args()
    repo, assets = args.repo.resolve(), args.assets.resolve()
    assets.mkdir(parents=True, exist_ok=True)
    checksum = sha(args.archive)
    if checksum != args.expected_sha256:
        raise ValueError("Canonical archive SHA256 mismatch")
    print("Canonical archive SHA256 verified", flush=True)
    copied = []
    with zipfile.ZipFile(args.archive) as archive:
        for entry in archive.infolist():
            relative = Path(*Path(entry.filename).parts[1:])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe ZIP path")
            selected = relative.as_posix() in ("index.html", "打开智能找数.html")
            selected |= (relative.parts[0] == "output" and
                         "sample_assets" not in relative.parts and
                         relative.suffix in (".html", ".json", ".css", ".js", ".svg"))
            if not selected or entry.is_dir():
                continue
            if entry.file_size > 45 * 1024 * 1024:
                raise ValueError("Source snapshot unexpectedly large: " + str(relative))
            destination = repo / "site" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
            copied.append({"path": relative.as_posix(), "bytes": entry.file_size, "sha256": sha(destination)})
    write_json(repo / "docs/source-snapshot.json", {"archiveSha256": checksum, "files": copied,
               "note": "HTML/JSON source snapshot only; complete resources are release assets."})
    print(f"Copied {len(copied)} source snapshot files", flush=True)

    published = []
    with args.archive.open("rb") as source:
        remaining = args.archive.stat().st_size
        number = 0
        while remaining:
            number += 1
            name = args.archive.name + f".part{number:02d}"
            destination = assets / name
            needed = min(PART_BYTES, remaining)
            digest = hashlib.sha256()
            with destination.open("wb") as target:
                unfilled = needed
                while unfilled:
                    block = source.read(min(4 * 1024 * 1024, unfilled))
                    if not block:
                        raise EOFError("Source archive truncated")
                    target.write(block)
                    digest.update(block)
                    unfilled -= len(block)
            published.append({"name": name, "bytes": needed, "sha256": digest.hexdigest()})
            remaining -= needed
            print(f"Wrote {name}: {needed} bytes", flush=True)
    # Re-read all volumes in order to catch write/order errors, not just source errors.
    joined = hashlib.sha256()
    for item in published:
        with (assets / item["name"]).open("rb") as source:
            for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
                joined.update(block)
    if joined.hexdigest() != args.expected_sha256:
        raise ValueError("Volume concatenation SHA256 mismatch")
    helper = assets / "download-helpers.zip"
    with zipfile.ZipFile(helper, "w", zipfile.ZIP_DEFLATED) as target:
        for name in ("join-windows.cmd", "join-macos-linux.command", "README.md"):
            target.write(repo / "distribution" / name, arcname=name)
    published.append({"name": helper.name, "bytes": helper.stat().st_size, "sha256": sha(helper)})
    manifest = {"version": "2026.09.16", "archive": args.archive.name,
                "archiveBytes": args.archive.stat().st_size, "archiveSha256": args.expected_sha256,
                "volumeFormat": "binary-concatenation", "volumeCount": number,
                "joinedSha256Verified": True, "assets": published}
    write_json(repo / "distribution/release-manifest.json", manifest)
    write_json(assets / "release-manifest.json", manifest)
    checksums = "".join(item["sha256"] + "  " + item["name"] + "\n" for item in published)
    (assets / "SHA256SUMS.txt").write_text(checksums, encoding="ascii")
    (repo / "distribution/SHA256SUMS.txt").write_text(checksums, encoding="ascii")
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == "__main__":
    main()
