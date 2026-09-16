# Offline Tooling Development

## Scope

`src/offline/` contains the reviewed browser merge core, browser loader and ZIP
writer, merge UI, data packaging tools, and verification scripts. It does not
contain the complete dataset or a fresh-crawl pipeline. Complete runnable
artifacts are distributed through this repository's GitHub Releases.

The delivered HTML package needs no Node.js, Python, server, or runtime download.
The tools below are for developers and release verification only.

## Local Setup

Use Node.js 20 or newer, npm, and Python 3.10 or newer. Browser tests use an
already installed Google Chrome with its normal security settings. Playwright
is a pinned development dependency, not a dependency of the delivered HTML.

Check the active runtime before testing. An older system default such as Node
15 is unsupported even when a newer bundled runtime is installed. Select the
supported runtime and its npm on `PATH`; the release was independently verified
with Node 24. For example, in a POSIX shell:

```sh
export PATH="/path/to/node-24/bin:$PATH"
node --version
npm --version
```

Replace the example with your installed runtime directory. On Windows, select
that installation through your version manager or update the session's `PATH`.

Run commands from the repository root:

```sh
npm ci --ignore-scripts
npm test
npm run test:tools
python3 -B -m unittest discover -s src/offline -p 'test_build.py' -v
```

On Windows, use `py -3` instead of `python3` when appropriate. Python tests use
only the standard library and create their own temporary miniature source
packages; no corpus download is required. `npm test` runs only the merge-core
and browser-runtime unit tests and also works before installing Playwright.
`test:tools` verifies the portable browser configuration and command arguments.

No `playwright install` is required when Google Chrome is already installed.
All browser scripts use `require('playwright')`, resolved from the development
environment, rather than any author's runtime installation path.

## Browser Tests

The loader test builds and removes an isolated temporary fixture, so it does not
need a release package or write into the repository:

```sh
npm run test:e2e:loader
```

The full browser tests require an explicit, completely extracted HTML package
directory containing `index.html`, and a separate evidence/output directory:

```sh
npm run test:e2e -- /path/to/extracted-package /path/to/evidence full
npm run test:e2e:extended -- /path/to/extracted-package /path/to/evidence
```

Use `smoke` instead of `full` for a runtime-loading check only; smoke is not full
acceptance. Evidence must be outside the package under test. Downloads,
screenshots, and verification JSON go into that evidence directory, while the
browser uses a disposable profile for local job storage.

Every browser script honors `CHROME_WRAPPER` as an optional executable path.
Unset it to use installed Chrome through Playwright's `chrome` channel. The
value is a path, not a command plus arguments; quote paths containing spaces:

```sh
CHROME_WRAPPER=/path/to/chrome-wrapper npm run test:e2e:loader
```

Do not add file-access/security bypass flags. The original relocation acceptance
used a separately supplied OS-sandbox wrapper to prevent access to the source
tree. Setting `CHROME_WRAPPER` alone does not establish that isolation; inspect
the wrapper policy before treating `sourceSandboxed` evidence as proof.

Full browser assertions intentionally target the reviewed release: 285 datasets,
855,000 records, the recorded logical storage total, 86 individual reports, and
310 video previews. The extended merge checks retain the original expected
6,000 processed / 5,437 kept / 563 duplicates counts. These are not generic tests
for arbitrary or tiny fixture catalogs. Do not relax these assertions to make
an incomplete package pass. Use the unit and loader fixtures for corpus-free
development; full acceptance needs the complete release artifact.

## Build Inputs

`build.py` converts a **prior validated portable source package** into
file-origin-loadable data. A source-only checkout, or an already generated
HTML-only package without the original record archives, is not sufficient to
reproduce the complete corpus. Required inputs include:

- `package_manifest.json`, with source file hashes and per-dataset archive data.
- `record_archives/<dataset>.zip`, containing complete record JSON and
  `_records_manifest.json` with the original integrity/provenance hash chain.
- `assets/`, containing the original content-addressed media bytes.
- `output/dataset_catalog_2026-09-02/`, containing the existing catalog and HTML.
- The static files, reports, label files, and dataset metadata referenced by
  the manifest, in the original portable relative layout.

`test_build.py` is an executable miniature example of that source contract.
The dated relative paths in the builder are package schema paths, not absolute
dependencies on a developer workstation. The retained historical home-directory
marker check rejects leaked original absolute paths in records; it never reads
that home directory. Source record hashes and this guard have not been weakened.

When the prerequisite source is available, use separate source, output, and
checkpoint directories. Never target the existing release artifact:

```sh
python3 -B src/offline/build.py --source /path/to/prior-portable-source \
  --output /path/to/new-html-package --state /path/to/build-state --workers 4
python3 -B src/offline/integrate.py /path/to/new-html-package
```

The builder preserves source bytes and catalog scores, validates referenced
media, creates bounded gzip/base64 record chunks and SHA-addressed media
scripts, and publishes the data index only after verification. Integration
copies the browser runtime into the generated package and replaces the old
server merge script. It requires the existing catalog HTML and is not a UI
builder from a blank page. It also emits release-specific documentation/counts,
so the miniature fixture output is not a substitute for a full release.

`finalize.py <package-directory> <evidence-directory>` is likewise a
release-specific finalizer, not a generic ZIP command. It requires successful
data, browser, extended, and relocation verification. Relocation evidence must
be at `relocation-verification/browser-verification.json` beside the main
evidence directory, with actual source isolation. Do not manufacture evidence
or remove assertions to bypass these prerequisites. It updates generated
verification/manifest files and writes the final ZIP and SHA-256 sidecar.

## Licenses

The bundled `fflate.umd.js` is accompanied by its original MIT license in
`src/offline/fflate-LICENSE.txt`; integration copies that license into the
generated HTML package. Keep the notice with redistributed copies.

The author's own source license is unspecified. The development package is
marked `UNLICENSED` and `private` to avoid implying an open-source grant or
publishing it to npm. A public GitHub repository does not by itself supply an
author-source license. The third-party fflate license does not license this
repository's original code or grant rights to dataset content. Preserve each
record's source, license, robots, and provenance evidence.
