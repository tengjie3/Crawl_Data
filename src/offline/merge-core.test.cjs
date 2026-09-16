const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createHash, webcrypto} = require('node:crypto');

if (!globalThis.crypto) globalThis.crypto = webcrypto;
const target = path.join(__dirname, 'merge-core.js');
const bytes = value => new TextEncoder().encode(value);
const hash = value => createHash('sha256').update(value).digest('hex');
const identity = (kind, text, asset = '') => hash(bytes(JSON.stringify([kind, text, asset])));
function core() {
  assert.ok(fs.existsSync(target), 'merge-core.js must implement the browser-only merge contract');
  return require(target);
}
function row(text, extra = {}, labels = {}) {
  return {file: 'records/record_00000001.json', record: {record_id: 'r1', text, ...extra}, labels, assetHash: null};
}
function fixture(groups = {Alpha: [row('first')], Beta: [row('second')]}, catalogs = {}) {
  const chunks = new Map();
  const index = {datasets: {}, assets: {}};
  for (const [name, rows] of Object.entries(groups)) {
    const descriptors = [];
    for (let start = 0; start < rows.length; start += 2) {
      const id = name + '-' + start;
      const records = rows.slice(start, start + 2);
      chunks.set(id, records);
      descriptors.push({id, path: 'offline/chunks/' + id + '.js', records: records.length,
        bytes: 100, sha256: '1'.repeat(64)});
    }
    index.datasets[name] = {datasetName: name, recordCount: rows.length, chunks: descriptors,
      dataType: 'html_text', domain: 'education', language: 'English', taskTypes: ['retrieval'],
      totalScore: 99, ...catalogs[name]};
  }
  const files = new Map();
  const progress = [];
  const loaded = [];
  const assets = new Map();
  const options = {
    request: {datasets: Object.keys(groups)}, index, id: 'test-job-12345678',
    loadChunk: async descriptor => {
      loaded.push(descriptor.id);
      return structuredClone(chunks.get(descriptor.id));
    },
    loadAsset: async descriptor => {
      assert.equal(descriptor, index.assets[descriptor.sha256]);
      return assets.get(descriptor.sha256).slice();
    },
    write: async (name, content, info) => {
      assert.ok(content instanceof Uint8Array);
      assert.equal(files.has(name), false, 'each archive path is written exactly once: ' + name);
      files.set(name, {bytes: Buffer.from(content), info});
    },
    onProgress: state => progress.push(state)
  };
  return {index, chunks, files, progress, loaded, assets, options};
}
function addAsset(f, payload, ext = '.png') {
  const sha = hash(payload);
  f.index.assets[sha] = {id: 'asset-' + sha, path: 'offline/assets/' + sha + '.js',
    bytes: payload.length, sha256: sha, ext};
  f.assets.set(sha, payload);
  return sha;
}
function json(f, name) { return JSON.parse(f.files.get(name).bytes.toString('utf8')); }
function lines(f, name) {
  const content = f.files.get(name).bytes.toString('utf8');
  return content ? content.trimEnd().split('\n').map(line => JSON.parse(line)) : [];
}
const png = Uint8Array.from([137, 80, 78, 71, 13, 10, 26, 10, 0, 255, 128, 17, 33]);

test('works as a classic browser script without require, Buffer, or other dependencies', async () => {
  core();
  const context = vm.createContext({crypto: webcrypto, TextEncoder, Uint8Array, AbortController, setTimeout});
  vm.runInContext(fs.readFileSync(target, 'utf8'), context);
  const api = context.OfflineMergeCore;
  assert.deepEqual(Object.keys(api).sort(), ['merge', 'sha256', 'textContent', 'validateRequest']);
  assert.equal(await api.sha256(bytes('abc')), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  const f = fixture();
  assert.equal((await api.merge(f.options)).kept, 2);
});

test('text selection matches Python precedence and preserves exact text', () => {
  const {textContent} = core();
  const selected = '  exact\n\u4e2d\u6587  ';
  assert.equal(textContent({training_text: selected, training_data: {content: 'second'}, text: 'third'}), selected);
  assert.equal(textContent({training_text: ' \n', training_data: {content: selected}, text: 'third'}), selected);
  assert.equal(textContent({training_data: [], text: false, content: 4, body: 'body', caption: 'caption'}), 'body');
  assert.equal(textContent({body: '\t', caption: 'caption'}), 'caption');
  assert.equal(textContent({text: '\u0085\u001c', caption: 'fallback'}), 'fallback');
  assert.equal(textContent({text: '\ufeff', caption: 'fallback'}), '\ufeff');
  assert.equal(textContent({training_data: {content: 5}, text: '\n'}), '');
  for (const value of [null, [], 'bad', 42]) assert.throws(() => textContent(value), /invalid_record/);
});

test('sha256 hashes only the supplied byte view including binary and empty input', async () => {
  const {sha256} = core();
  const data = Uint8Array.from([8, 0, 255, 128, 7]);
  assert.equal(await sha256(data.subarray(1, 4)), hash(Buffer.from([0, 255, 128])));
  assert.equal(await sha256(new Uint8Array()), 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
});

test('request validation rejects unsafe, duplicate, unknown, and excess dataset names', () => {
  const {validateRequest} = core();
  const {index} = fixture();
  for (const datasets of [undefined, 'Alpha', [], ['Alpha'], ['Alpha', 'Alpha'], ['Alpha', 'Missing'],
    ['Alpha', '../Beta'], ['Alpha', '__proto__'], ['Alpha', 'Beta\n'], ['Alpha', 1],
    ['Alpha', 'Beta', 'C', 'D', 'E']]) {
    assert.throws(() => validateRequest({datasets}, index));
  }
  for (const invalid of [null, [], 'request']) assert.throws(() => validateRequest(invalid, index));
  const request = {datasets: ['Alpha', 'Beta']};
  const validated = validateRequest(request, index);
  assert.deepEqual(validated, {datasets: ['Alpha', 'Beta'], limitPerDataset: null});
  validated.datasets.push('not-in-original');
  assert.equal(request.datasets.length, 2);
});

test('limits are strict integers with a 12000 selected-record ceiling', () => {
  const {validateRequest} = core();
  const {index} = fixture();
  for (const limit of [0, -1, 1.5, '2', true, NaN, Infinity, 100001]) {
    assert.throws(() => validateRequest({datasets: ['Alpha', 'Beta'], limitPerDataset: limit}, index));
  }
  assert.equal(validateRequest({datasets: ['Alpha', 'Beta'], limitPerDataset: 100000}, index).limitPerDataset, 100000);
  for (const source of Object.values(index.datasets)) {
    source.recordCount = 6001;
    source.chunks[0].records = 6001;
  }
  assert.throws(() => validateRequest({datasets: ['Alpha', 'Beta']}, index), /12000/);
  assert.equal(validateRequest({datasets: ['Alpha', 'Beta'], limitPerDataset: 6000}, index).limitPerDataset, 6000);
});

test('invalid index counts are rejected before any loader or writer runs', async () => {
  const f = fixture();
  f.index.datasets.Alpha.recordCount = 2;
  await assert.rejects(core().merge(f.options), /count|index/i);
  assert.equal(f.loaded.length, 0);
  assert.equal(f.files.size, 0);
});

test('exact text dedup keeps whitespace variants and all duplicate source evidence', async () => {
  const source = {record_id: 'original', source_url_safe: 'https://safe.test/source',
    source_url: 'https://old.test/source', license: {name: 'CC0'}, provenance: {warc: 'capture'},
    robots_protocol: {allowed: true}, custom: {retained: [1, 2]}, collected_at: '2026-01-01'};
  const duplicate = {record_id: 'duplicate', source_url: 'https://second.test/source',
    license: 'CC-BY', provenance: {warc: 'second'}, Robts_protocol: {allowed: false}};
  const f = fixture({Alpha: [row('same', source)], Beta: [row('same', duplicate), row('same ')]});
  const result = await core().merge(f.options);
  const id = identity('html_text', 'same');
  assert.deepEqual([result.processed, result.kept, result.duplicates, result.failed], [3, 2, 1, 0]);
  const exported = json(f, 'data/records/' + id + '.json');
  for (const [key, value] of Object.entries({...source, text: 'same'})) assert.deepEqual(exported[key], value);
  assert.deepEqual(exported.merge_metadata, {id, source_dataset: 'Alpha', sample_labels: {}});
  assert.deepEqual(lines(f, 'duplicate_origins.jsonl'), [{mergedId: id, origin: {
    dataset: 'Beta', recordFile: 'record_00000001.json', recordId: 'duplicate',
    url: 'https://second.test/source', license: 'CC-BY', provenance: {warc: 'second'}, robots: {allowed: false}
  }}]);
  const first = lines(f, 'records.jsonl')[0];
  assert.equal(first.sourceUrl, 'https://safe.test/source');
  assert.equal(first.collectedAt, '2026-01-01');
  assert.deepEqual(first.provenance, source.provenance);
  assert.equal(f.files.get('data/text/' + id + '.txt').bytes.toString(), 'same');
});

test('media exports actual binary once, preserves captions, and rewrites every raw path', async () => {
  const a = row('caption A', {raw_asset: {path: '/old/a.png', local_path: '/old/a.png',
    relative_path: 'raw/a.png', sha256: 'stale', mime: 'image/png'}, extra: 'kept'});
  const b = row('caption B', {raw_asset: {path: 'old/b.png'}});
  const c = row('caption A', {raw_asset: {path: 'old/c.png'}});
  const f = fixture({Alpha: [a, b], Beta: [c]}, {Alpha: {dataType: 'image'}, Beta: {dataType: 'image'}});
  const sha = addAsset(f, png, 'PNG');
  for (const rows of f.chunks.values()) for (const item of rows) item.assetHash = sha;
  const original = structuredClone(a);
  const result = await core().merge(f.options);
  assert.deepEqual([result.kept, result.duplicates], [2, 1]);
  const assetPath = 'data/assets/' + sha + '.png';
  assert.deepEqual(f.files.get(assetPath).bytes, Buffer.from(png));
  assert.equal([...f.files.keys()].filter(name => name.startsWith('data/assets/')).length, 1);
  for (const item of lines(f, 'records.jsonl')) {
    assert.equal(item.assetPath, assetPath);
    const record = json(f, item.recordPath);
    assert.equal(record.raw_asset.sha256, sha);
    for (const key of ['path', 'local_path', 'relative_path']) assert.equal(record.raw_asset[key], '../assets/' + sha + '.png');
    assert.equal(item.id, identity('image', record.text, sha));
  }
  assert.equal(json(f, 'data/records/' + identity('image', 'caption A', sha) + '.json').raw_asset.mime, 'image/png');
  assert.deepEqual(a, original, 'input records must not be mutated');
});

test('all Python-supported image, PDF, and video header signatures are accepted', async () => {
  const samples = [
    ['image', '.jpg', Buffer.from([255, 216, 255, 0])], ['image', '.png', png],
    ['image', '.gif', bytes('GIF87a123')], ['image', '.gif', bytes('GIF89a123')],
    ['image', '.tif', bytes('II*\0abc')], ['image', '.tif', bytes('MM\0*abc')],
    ['image', '.webp', bytes('RIFF1234WEBPpayload')], ['pdf', '.pdf', bytes('%PDF-1.7\noriginal\n')],
    ['video', '.mp4', bytes('1234ftypisomoriginal')], ['video', '.webm', Buffer.from([26, 69, 223, 163, 0, 255])],
    ['video', '.avi', bytes('RIFF1234AVI original')]
  ];
  for (const [kind, ext, payload] of samples) {
    const f = fixture({Alpha: [row('', {raw_asset: {}})], Beta: [row('valid text')]}, {Alpha: {dataType: kind}});
    f.chunks.get('Alpha-0')[0].assetHash = addAsset(f, payload, ext);
    const result = await core().merge(f.options);
    assert.equal(result.kept, 2, kind + ext);
    assert.equal(lines(f, 'records.jsonl')[0].textPath, null);
  }
});

test('same bytes and text with different catalog media kinds are distinct identities', async () => {
  const payload = bytes('II*\0ftypisom');
  const f = fixture({Alpha: [row('same')], Beta: [row('same')]},
    {Alpha: {dataType: 'image'}, Beta: {dataType: 'video'}});
  const sha = addAsset(f, payload, '.bin');
  for (const chunk of f.chunks.values()) chunk[0].assetHash = sha;
  const result = await core().merge(f.options);
  assert.equal(result.kept, 2);
  assert.equal(result.duplicates, 0);
});

test('hash mismatch and malformed rows are isolated and never become successful duplicates', async () => {
  const f = fixture({Alpha: [row('one'), row('bad hash'), {file: 'bad.json', record: []}, row('three')],
    Beta: [row('kept')]}, {Alpha: {dataType: 'image'}});
  const sha = addAsset(f, png);
  const wrong = '0'.repeat(64);
  f.index.assets[wrong] = {...f.index.assets[sha], sha256: wrong};
  f.assets.set(wrong, png);
  f.chunks.get('Alpha-0')[0].assetHash = sha;
  f.chunks.get('Alpha-0')[1].assetHash = wrong;
  f.chunks.get('Alpha-2')[1].assetHash = sha;
  const result = await core().merge(f.options);
  assert.deepEqual([result.processed, result.kept, result.duplicates, result.failed], [5, 3, 0, 2]);
  assert.deepEqual(result.sources.Alpha, {processed: 4, kept: 2, duplicates: 0, failed: 2});
  assert.match(lines(f, 'failed_records.jsonl')[0].reason, /hash|sha256/i);
  assert.match(lines(f, 'failed_records.jsonl')[1].reason, /invalid_record/);
  assert.equal([...f.files.keys()].some(name => name.includes(wrong)), false);
});

test('wrong signatures, empty bytes, byte-count mismatch, missing media and unsafe extensions fail individually', async () => {
  const cases = [
    {payload: bytes('<html>not a PNG</html>'), reason: /invalid_media/},
    {payload: new Uint8Array(), reason: /empty_asset/},
    {payload: png, change: descriptor => descriptor.bytes++, reason: /size|bytes/i},
    {payload: png, change: descriptor => descriptor.sha256 = 'a'.repeat(64), reason: /hash|sha256/i},
    {payload: png, change: descriptor => descriptor.ext = '../../escape', reason: /ext|path/i},
    {payload: png, change: (_, f, sha) => delete f.index.assets[sha], reason: /missing_asset/},
    {payload: png, change: (_, f) => f.chunks.get('Alpha-0')[0].record.raw_asset = 'bad', reason: /invalid_record/}
  ];
  for (const item of cases) {
    const f = fixture({Alpha: [row('caption')], Beta: [row('good')]}, {Alpha: {dataType: 'image'}});
    const sha = addAsset(f, item.payload);
    f.chunks.get('Alpha-0')[0].assetHash = sha;
    if (item.change) item.change(f.index.assets[sha], f, sha);
    const result = await core().merge(f.options);
    assert.equal(result.failed, 1);
    assert.equal(result.kept, 1);
    assert.match(lines(f, 'failed_records.jsonl')[0].reason, item.reason);
    assert.equal([...f.files.keys()].some(name => name.startsWith('data/assets/')), false);
  }
});

test('loader errors and chunk count mismatches account for every selected record', async () => {
  for (const behavior of ['throw', 'short', 'not-array', 'long']) {
    const f = fixture({Alpha: [row('a'), row('b'), row('c')], Beta: [row('z')]});
    const load = f.options.loadChunk;
    f.options.loadChunk = async chunk => {
      if (chunk.id !== 'Alpha-0') return load(chunk);
      if (behavior === 'throw') throw new Error('chunk integrity failed');
      if (behavior === 'short') return [row('a')];
      if (behavior === 'long') return [row('a'), row('b'), row('extra')];
      return {};
    };
    const result = await core().merge(f.options);
    assert.deepEqual([result.processed, result.kept, result.failed], [4, 2, 2]);
    const failed = lines(f, 'failed_records.jsonl');
    assert.equal(failed.length, 2);
    assert.notEqual(failed[0].recordFile, failed[1].recordFile);
  }
});

test('limits use ordered chunk prefixes, await writes, and never load later chunks', async () => {
  const f = fixture({Alpha: [row('a'), row('b'), row('c')], Beta: [row('d'), row('e'), row('f')]});
  f.options.request.limitPerDataset = 1;
  let writesInFlight = 0;
  const write = f.options.write;
  f.options.write = async (...args) => {
    writesInFlight++;
    assert.equal(writesInFlight, 1);
    await new Promise(resolve => setImmediate(resolve));
    await write(...args);
    writesInFlight--;
  };
  const load = f.options.loadChunk;
  f.options.loadChunk = async chunk => {
    assert.equal(writesInFlight, 0);
    if (chunk.id === 'Beta-0') assert.ok([...f.files.keys()].some(name => name.startsWith('data/records/')));
    return load(chunk);
  };
  const result = await core().merge(f.options);
  assert.equal(result.processed, 2);
  assert.deepEqual(f.loaded, ['Alpha-0', 'Beta-0']);
  assert.deepEqual(lines(f, 'records.jsonl').map(item => item.id), [identity('html_text', 'a'), identity('html_text', 'd')]);
});

test('progress snapshots have stable per-source counters and total equals selected counts', async () => {
  const f = fixture({Alpha: [row('a'), row('a'), row('')], Beta: [row('b'), row('c')]});
  const result = await core().merge(f.options);
  assert.ok(f.progress.length >= 3);
  assert.equal(f.progress[0].processed, 0, 'earlier progress objects must not mutate');
  assert.deepEqual(f.progress[0].sources.Alpha, {processed: 0, kept: 0, duplicates: 0, failed: 0});
  for (const state of f.progress) {
    assert.equal(typeof state.phase, 'string');
    assert.equal(state.total, 5);
    assert.equal(state.processed, state.kept + state.duplicates + state.failed);
    assert.equal(state.processed, Object.values(state.sources).reduce((sum, source) => sum + source.processed, 0));
    for (const source of Object.values(state.sources)) assert.equal(source.processed, source.kept + source.duplicates + source.failed);
  }
  const last = f.progress.at(-1);
  assert.equal(last.processed, 5);
  assert.equal(last.status, 'completed_with_errors');
  assert.deepEqual(last.sources, result.sources);
});

test('metadata uses retained catalog distributions and actual labels, never inherited quality scores', async () => {
  const f = fixture({Alpha: [row('same', {}, {subject: 'Math'}), row('same', {}, {subject: 'Discarded'})],
    Beta: [row('different', {provenance: {source_url: 'https://example.test'}, license: {}}, {subject: null, level: ''})]},
  {Alpha: {domain: 'education', language: 'English', taskTypes: ['retrieval', 'classification']},
    Beta: {domain: 'literature', language: 'Chinese', taskTypes: ['retrieval', 'generation']}});
  const {metadata, report} = await core().merge(f.options);
  assert.deepEqual(metadata.distributions, {domain: {education: 1, literature: 1},
    language: {English: 1, Chinese: 1}, dataType: {html_text: 2}});
  assert.deepEqual(metadata.sampleLabelDistributions, {'education/subject': {Math: 1}});
  assert.equal(metadata.labeledRecords, 1);
  assert.equal(metadata.recordCount, 2);
  assert.deepEqual(metadata.taskTypes, ['classification', 'generation', 'retrieval']);
  assert.equal(metadata.totalScore, undefined);
  assert.equal(metadata.dataQuality, undefined);
  assert.equal(report.quality.scopeScores, null);
  assert.equal(report.quality.scoreStatus, 'not_evaluated');
  assert.equal(report.quality.denominator, 2);
  assert.equal(report.quality.sourceUrlPresent, 1);
  assert.equal(report.quality.licenseFieldPresent, 0);
  assert.deepEqual(lines(f, 'records.jsonl')[1].labels, {subject: null, level: ''});
});

test('untrusted label keys cannot pollute prototypes or disappear from distributions', async () => {
  const f = fixture({Alpha: [row('a', {}, JSON.parse('{"__proto__":"constructor"}'))], Beta: [row('b')]},
    {Alpha: {domain: '__proto__', language: 'constructor'}});
  const {metadata} = await core().merge(f.options);
  assert.equal(metadata.distributions.domain.__proto__, 1);
  assert.equal(metadata.distributions.language.constructor, 1);
  assert.equal(metadata.sampleLabelDistributions['__proto__/__proto__'].constructor, 1);
  assert.equal({}.polluted, undefined);
});

test('manifest and writer digests verify all original and generated bytes, excluding manifest self-reference', async () => {
  const f = fixture({Alpha: [row('a'), row('')], Beta: [row('a'), row('b')]});
  const result = await core().merge(f.options);
  const manifest = json(f, 'manifest.json');
  assert.equal(manifest.files.length, f.files.size - 1);
  assert.equal(new Set(manifest.files.map(file => file.path)).size, manifest.files.length);
  assert.equal(manifest.files.some(file => file.path === 'manifest.json'), false);
  for (const entry of manifest.files) {
    const file = f.files.get(entry.path);
    assert.ok(file, entry.path);
    assert.equal(entry.bytes, file.bytes.length);
    assert.equal(entry.sha256, hash(file.bytes));
  }
  for (const file of f.files.values()) assert.equal(file.info.sha256, hash(file.bytes));
  for (const name of ['records.jsonl', 'failed_records.jsonl', 'duplicate_origins.jsonl',
    'dataset_metadata.json', 'merge_report.json', 'README.md', 'manifest.json']) assert.ok(f.files.has(name), name);
  assert.deepEqual(json(f, 'dataset_metadata.json'), result.metadata);
  assert.deepEqual(json(f, 'merge_report.json'), result.report);
  const payloadBytes = manifest.files.filter(file => !['dataset_metadata.json', 'merge_report.json', 'README.md'].includes(file.path))
    .reduce((sum, file) => sum + file.bytes, 0);
  assert.equal(result.metadata.payloadBytes, payloadBytes);
});

test('all-failed runs save failure details and report then reject with inspectable result', async () => {
  const f = fixture({Alpha: [row('')], Beta: [{file: 'broken.json', record: null}]});
  await assert.rejects(core().merge(f.options), error => {
    assert.equal(error.code, 'ALL_RECORDS_FAILED');
    assert.deepEqual([error.result.processed, error.result.failed, error.result.kept], [2, 2, 0]);
    assert.ok(Array.isArray(error.failureReport), 'all-failed errors retain an inspectable row-level report');
    assert.equal(error.failureReport.length, 2, 'UI can persist failures before discarding partial ZIP parts');
    assert.match(error.failureReport[0].reason, /missing_content/);
    assert.equal(error.failureReport[1].recordFile, 'broken.json');
    return true;
  });
  assert.equal(lines(f, 'failed_records.jsonl').length, 2);
  assert.equal(json(f, 'merge_report.json').failed, 2);
  assert.equal(f.progress.at(-1).status, 'failed');
});

test('pre-aborted signals load and write nothing', async () => {
  const f = fixture();
  const controller = new AbortController();
  controller.abort();
  f.options.signal = controller.signal;
  await assert.rejects(core().merge(f.options), {name: 'AbortError'});
  assert.equal(f.loaded.length, 0);
  assert.equal(f.files.size, 0);
});

test('cancellation while loading chunks, assets, or writing is never swallowed as row failure', async () => {
  for (const boundary of ['loadChunk', 'loadAsset', 'write']) {
    const f = fixture({Alpha: [row('caption')], Beta: [row('text')]}, {Alpha: {dataType: 'image'}});
    f.chunks.get('Alpha-0')[0].assetHash = addAsset(f, png);
    const controller = new AbortController();
    f.options.signal = controller.signal;
    const original = f.options[boundary];
    f.options[boundary] = async (...args) => {
      const value = await original(...args);
      controller.abort(new Error('stop requested'));
      throw new Error('loader or writer interrupted');
    };
    await assert.rejects(core().merge(f.options), {name: 'AbortError'});
    assert.equal(f.files.has('merge_report.json'), false);
    assert.equal(f.progress.at(-1).failed, 0);
    assert.equal(f.progress.at(-1).status, 'cancelled');
  }
});

test('loader AbortError stops a run even without a signal', async () => {
  const f = fixture();
  f.options.loadChunk = async () => {
    const error = new Error('cancelled by loader');
    error.name = 'AbortError';
    throw error;
  };
  await assert.rejects(core().merge(f.options), {name: 'AbortError'});
  assert.equal(f.files.size, 0);
});

test('successful async loads still check cancellation before writing anything', async () => {
  const f = fixture();
  const controller = new AbortController();
  f.options.signal = controller.signal;
  const load = f.options.loadChunk;
  f.options.loadChunk = async chunk => {
    const result = await load(chunk);
    controller.abort();
    return result;
  };
  await assert.rejects(core().merge(f.options), {name: 'AbortError'});
  assert.equal(f.files.size, 0);
});

test('writer errors are fatal, not misreported as corrupt input records', async () => {
  const f = fixture();
  f.options.write = async () => { throw new Error('disk quota exceeded'); };
  await assert.rejects(core().merge(f.options), /disk quota exceeded/);
  assert.equal(f.progress.at(-1).failed, 0);
  assert.equal(f.progress.at(-1).status, 'failed');
});

test('progress every ten records yields to event-loop cancellation before record eleven', async () => {
  const f = fixture({Alpha: Array.from({length: 11}, (_, n) => row('row-' + n)), Beta: [row('last')]});
  const controller = new AbortController();
  f.options.signal = controller.signal;
  f.options.onProgress = state => {
    f.progress.push(state);
    if (state.processed === 10) setTimeout(() => controller.abort(), 0);
  };
  await assert.rejects(core().merge(f.options), {name: 'AbortError'});
  assert.equal(f.progress.at(-1).processed, 10);
  assert.equal(f.progress.some(state => state.processed > 0 && state.processed < 10), false);
});

test('complete four-dataset merge processes all 12000 rows without silently sampling', async () => {
  const groups = Object.fromEntries(['Alpha', 'Beta', 'Gamma', 'Delta'].map(name =>
    [name, Array.from({length: 3000}, (_, n) => row(name + '-' + n))]));
  const f = fixture(groups);
  let recordFiles = 0;
  let manifestFiles = 0;
  f.options.write = async (name, content) => {
    if (name.startsWith('data/records/')) recordFiles++;
    if (name === 'manifest.json') manifestFiles = JSON.parse(new TextDecoder().decode(content)).files.length;
  };
  const result = await core().merge(f.options);
  assert.equal(result.processed, 12000);
  assert.equal(result.kept, 12000);
  assert.equal(recordFiles, 12000);
  assert.equal(manifestFiles, 24006);
  assert.equal(f.loaded.length, 6000);
  assert.equal(f.progress.at(-1).total, 12000);
  for (const source of Object.values(result.sources)) assert.equal(source.kept, 3000);
});

test('successfully written media is loaded once per SHA while every caption retains rewritten asset paths', async () => {
  const f = fixture({Alpha: [row('first'), row('second')], Beta: [row('first'), row('third')]},
    {Alpha: {dataType: 'image'}, Beta: {dataType: 'image'}});
  const sha = addAsset(f, png);
  for (const chunk of f.chunks.values()) for (const item of chunk) {
    item.assetHash = sha;
    item.record.raw_asset = {path: '/original/image.png', mime: 'image/png'};
  }
  let loads = 0;
  const load = f.options.loadAsset;
  f.options.loadAsset = async descriptor => { loads++; return load(descriptor); };
  const result = await core().merge(f.options);
  assert.equal(loads, 1, 'original media must not be repeatedly loaded for caption variants');
  assert.deepEqual([result.kept, result.duplicates, result.failed], [3, 1, 0]);
  for (const item of lines(f, 'records.jsonl')) {
    assert.equal(item.assetPath, 'data/assets/' + sha + '.png');
    const raw = json(f, item.recordPath).raw_asset;
    assert.deepEqual(raw, {path: '../assets/' + sha + '.png', local_path: '../assets/' + sha + '.png',
      relative_path: '../assets/' + sha + '.png', sha256: sha, mime: 'image/png'});
  }
});

test('cached media validation still rejects a different incompatible catalog kind', async () => {
  const f = fixture({Alpha: [row('caption')], Beta: [row('caption')]},
    {Alpha: {dataType: 'image'}, Beta: {dataType: 'pdf'}});
  const sha = addAsset(f, png);
  for (const chunk of f.chunks.values()) chunk[0].assetHash = sha;
  let loads = 0;
  const load = f.options.loadAsset;
  f.options.loadAsset = async descriptor => { loads++; return load(descriptor); };
  const result = await core().merge(f.options);
  assert.equal(loads, 1);
  assert.deepEqual([result.kept, result.failed], [1, 1]);
  assert.match(lines(f, 'failed_records.jsonl')[0].reason, /invalid_media/);
});

test('failed asset writes stop immediately and cannot populate a reusable success cache', async () => {
  const f = fixture({Alpha: [row('first'), row('second')], Beta: [row('third')]},
    {Alpha: {dataType: 'image'}, Beta: {dataType: 'image'}});
  const sha = addAsset(f, png);
  for (const chunk of f.chunks.values()) for (const item of chunk) item.assetHash = sha;
  let loads = 0;
  const load = f.options.loadAsset;
  f.options.loadAsset = async descriptor => { loads++; return load(descriptor); };
  const write = f.options.write;
  f.options.write = async () => { throw new Error('asset storage failed'); };
  await assert.rejects(core().merge(f.options), /asset storage failed/);
  assert.equal(loads, 1);
  assert.equal(f.progress.at(-1).processed, 0);
  assert.equal(f.progress.at(-1).failed, 0);
  f.options.write = write;
  const result = await core().merge(f.options);
  assert.equal(result.kept, 3);
  assert.equal(loads, 2, 'retry revalidates bytes once; the failed attempt cannot supply cached assets');
});
