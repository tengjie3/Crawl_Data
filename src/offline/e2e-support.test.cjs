const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {pathToFileURL} = require('node:url');

async function support() {
  const target = path.join(__dirname, 'e2e-support.mjs');
  assert.ok(fs.existsSync(target), 'portable E2E argument and browser configuration is required');
  return import(pathToFileURL(target).href);
}

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'crawl data tooling '));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const packageRoot = path.join(root, 'extracted package');
  fs.mkdirSync(packageRoot);
  fs.writeFileSync(path.join(packageRoot, 'index.html'), '<!doctype html><title>Fixture</title>');
  return {root, packageRoot, output: path.join(root, 'evidence')};
}

test('browser defaults to installed Chrome without disabling security', async () => {
  const {browserOptions} = await support();
  assert.deepEqual(browserOptions({}), {headless: true, channel: 'chrome'});
  assert.deepEqual(browserOptions({CHROME_WRAPPER: ''}), {headless: true, channel: 'chrome'});
});

test('CHROME_WRAPPER overrides channel selection without shell parsing the path', async () => {
  const {browserOptions} = await support();
  const executable = path.join(os.tmpdir(), 'Browser Wrapper');
  assert.deepEqual(browserOptions({CHROME_WRAPPER: executable}), {headless: true, executablePath: executable});
});

test('full E2E requires explicit package and evidence directories before doing work', async () => {
  const {packageArguments} = await support();
  for (const args of [[], ['package']]) {
    assert.throws(() => packageArguments(args, 'browser-e2e.mjs', true), /Usage:.*package-directory.*evidence-directory/);
  }
});

test('package and evidence paths resolve independently of repository layout, including spaces', async t => {
  const {packageArguments} = await support();
  const f = fixture(t);
  const args = packageArguments([f.packageRoot, f.output], 'browser-e2e.mjs', true);
  assert.deepEqual(args, {packageRoot: f.packageRoot, html: path.join(f.packageRoot, 'index.html'), output: f.output, mode: 'full'});
  assert.equal(fs.existsSync(f.output), false, 'argument validation must not create output directories');
  assert.equal(packageArguments([f.packageRoot, f.output, 'smoke'], 'browser-e2e.mjs', true).mode, 'smoke');
});

test('invalid packages, modes, and extra arguments cannot silently skip assertions', async t => {
  const {packageArguments} = await support();
  const f = fixture(t);
  assert.throws(() => packageArguments([path.join(f.root, 'missing'), f.output], 'browser-e2e.mjs', true), /index.html/);
  assert.throws(() => packageArguments([f.packageRoot, f.output, 'skip'], 'browser-e2e.mjs', true), /mode/);
  assert.throws(() => packageArguments([f.packageRoot, f.output, 'smoke'], 'extended-e2e.mjs'), /Usage:/);
  assert.throws(() => packageArguments([f.packageRoot, f.output, 'full', 'extra'], 'browser-e2e.mjs', true), /Usage:/);
});

test('evidence must not overwrite the package under test', async t => {
  const {packageArguments} = await support();
  const f = fixture(t);
  for (const output of [f.packageRoot, path.join(f.packageRoot, 'evidence')]) {
    assert.throws(() => packageArguments([f.packageRoot, output], 'browser-e2e.mjs', true), /outside.*package/);
  }
});

test('symlink aliases cannot place evidence inside the package under test', async t => {
  const {packageArguments} = await support();
  const f = fixture(t);
  const alias = path.join(f.root, 'package alias');
  fs.symlinkSync(f.packageRoot, alias, 'junction');
  for (const [packageRoot, output] of [
    [f.packageRoot, alias],
    [f.packageRoot, path.join(alias, 'new', 'evidence')],
    [alias, path.join(f.packageRoot, 'evidence')]
  ]) {
    assert.throws(() => packageArguments([packageRoot, output], 'browser-e2e.mjs', true), /outside.*package/);
  }
});
