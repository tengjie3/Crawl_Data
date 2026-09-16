#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
base=intelligent-data-discovery-html-only-2026-09-16.zip
expected=f6b1e227b2435dbc6fd2d86db6d4d032930ac2ceeb8d9b278ee32b5b1228788e
checksum() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        sha256sum "$1" | awk '{print $1}'
    fi
}
if [ -e "$base" ]; then
    [ "$(checksum "$base")" = "$expected" ] || { printf '%s\n' 'Existing ZIP checksum differs; move it aside first.'; exit 1; }
    printf '%s\n' 'The verified ZIP already exists. Extract it, then open index.html.'
    exit 0
fi
for part in 01 02 03; do
    [ -f "$base.part$part" ] || { printf 'Missing: %s.part%s\n' "$base" "$part"; exit 1; }
done
[ ! -e "$base.partial" ] || { printf '%s\n' 'A partial file exists. Inspect or move it aside before retrying.'; exit 1; }
printf '%s\n' 'Combining 3 parts (about 4 GB)...'
cat "$base.part01" "$base.part02" "$base.part03" > "$base.partial"
printf '%s\n' 'Verifying SHA256...'
[ "$(checksum "$base.partial")" = "$expected" ] || { printf '%s\n' 'Checksum failed. Download the parts again; no ZIP was published.'; exit 1; }
mv "$base.partial" "$base"
printf '%s\n' 'Verified. Extract the ZIP, then open index.html in Chrome or Edge.'
