"""Read remote asset size/digests via gh; never prints credentials or asset content."""
import argparse
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="tengjie3/Crawl_Data")
    parser.add_argument("--tag", default="intelligent-discovery-html-v2026.09.16")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    expected = json.loads(Path(__file__).with_name("release-manifest.json").read_text())
    # Draft releases have no published tag yet; resolve their stable API URL first.
    resolved = subprocess.run(["gh", "release", "view", args.tag, "--repo", args.repo,
                               "--json", "apiUrl"],
                              check=True, capture_output=True, text=True)
    api_url = json.loads(resolved.stdout)["apiUrl"]
    response = subprocess.run(["gh", "api", api_url],
                              check=True, capture_output=True, text=True)
    release = json.loads(response.stdout)
    remote = {item["name"]: item for item in release["assets"]}
    result = []
    for item in expected["assets"]:
        asset = remote.get(item["name"])
        if not asset or asset["state"] != "uploaded":
            raise ValueError("Missing/unuploaded asset: " + item["name"])
        if asset["size"] != item["bytes"]:
            raise ValueError("Size mismatch: " + item["name"])
        digest = asset.get("digest")
        if digest != "sha256:" + item["sha256"]:
            raise ValueError("SHA256 mismatch or unavailable; download and verify manually: " + item["name"])
        result.append({"name": item["name"], "bytes": asset["size"], "sha256": item["sha256"],
                       "remoteDigestVerified": True, "url": asset["browser_download_url"]})
    evidence = {"repository": args.repo, "releaseUrl": release["html_url"],
                "draft": release["draft"], "tag": release["tag_name"],
                "allManifestAssetsVerified": True, "assets": result}
    content = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    print(content)


if __name__ == "__main__":
    main()
