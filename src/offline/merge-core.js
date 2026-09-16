(function (root) {
  'use strict';

  const encoder = new TextEncoder();
  const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const mediaKinds = new Set(['image', 'pdf', 'video']);
  // Python str.strip() treats NEL/control separators as whitespace, but not BOM.
  const hasText = value => typeof value === 'string' &&
    /[^\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/u.test(value);
  const encode = value => encoder.encode(JSON.stringify(value));
  const counters = () => ({processed: 0, kept: 0, duplicates: 0, failed: 0});

  function failure(code, message) {
    const error = new Error(code + ': ' + message);
    error.code = code;
    return error;
  }

  function truthy(value) {
    if (!value) return false;
    if (Array.isArray(value)) return value.length > 0;
    return !object(value) || Object.keys(value).length > 0;
  }

  function firstTruthy(...values) {
    return values.find(truthy) ?? null;
  }

  function textContent(record) {
    if (!object(record)) throw failure('invalid_record', 'record must be a JSON object');
    const training = record.training_data;
    return [record.training_text, object(training) ? training.content : null,
      record.text, record.content, record.body, record.caption].find(hasText) || '';
  }

  async function sha256(bytes) {
    if (Object.prototype.toString.call(bytes) !== '[object Uint8Array]') {
      throw new TypeError('sha256 requires Uint8Array bytes');
    }
    if (!root.crypto || !root.crypto.subtle) throw failure('crypto_unavailable', 'Web Crypto SHA-256 is required');
    const digest = new Uint8Array(await root.crypto.subtle.digest('SHA-256', bytes));
    return Array.from(digest, byte => byte.toString(16).padStart(2, '0')).join('');
  }

  function validateRequest(request, index) {
    if (!object(request)) throw failure('invalid_request', 'request must be an object');
    const names = request.datasets;
    if (!Array.isArray(names) || names.length < 2 || names.length > 4) {
      throw failure('invalid_request', 'select 2-4 datasets');
    }
    if (!object(index) || !object(index.datasets)) throw failure('invalid_index', 'datasets index is missing');
    if (new Set(names).size !== names.length || names.some(name => typeof name !== 'string' ||
      !name.length || /[^A-Za-z0-9_-]/u.test(name) || !own(index.datasets, name))) {
      throw failure('invalid_request', 'dataset names must be known, unique, and path-safe');
    }
    const limit = request.limitPerDataset ?? null;
    if (limit !== null && (!Number.isInteger(limit) || limit < 1 || limit > 100000)) {
      throw failure('invalid_request', 'limitPerDataset must be an integer from 1 to 100000 or null');
    }
    let total = 0;
    for (const name of names) {
      const source = index.datasets[name];
      if (!object(source) || !Number.isSafeInteger(source.recordCount) || source.recordCount < 1 ||
        !Array.isArray(source.chunks) || typeof source.dataType !== 'string' || !source.dataType.length) {
        throw failure('invalid_index', 'missing catalog type, chunks, or record count for ' + name);
      }
      let count = 0;
      for (const chunk of source.chunks) {
        if (!object(chunk) || !Number.isSafeInteger(chunk.records) || chunk.records < 1) {
          throw failure('invalid_index', 'invalid chunk record count for ' + name);
        }
        count += chunk.records;
      }
      if (count !== source.recordCount) throw failure('invalid_index', 'chunk counts do not match recordCount for ' + name);
      total += Math.min(source.recordCount, limit ?? source.recordCount);
    }
    if (total > 12000) throw failure('invalid_request', 'browser merge supports at most 12000 selected records');
    return {datasets: names.slice(), limitPerDataset: limit};
  }

  function checkCancelled(signal, error) {
    if (error && error.name === 'AbortError') throw error;
    if (signal && signal.aborted) {
      if (signal.reason && signal.reason.name === 'AbortError') throw signal.reason;
      const aborted = new Error('Merge cancelled');
      aborted.name = 'AbortError';
      aborted.cause = signal.reason;
      throw aborted;
    }
  }

  function validateMedia(bytes, kind) {
    const at = (offset, prefix) => bytes.length >= offset + prefix.length &&
      Array.from(prefix, (value, i) => bytes[offset + i] === (typeof value === 'number' ? value : value.charCodeAt(0))).every(Boolean);
    const riff = at(0, 'RIFF');
    const signatures = {
      pdf: at(0, '%PDF-'),
      image: at(0, [255, 216, 255]) || at(0, [137, 80, 78, 71, 13, 10, 26, 10]) ||
        at(0, 'GIF87a') || at(0, 'GIF89a') || at(0, 'II*\0') || at(0, 'MM\0*') || (riff && at(8, 'WEBP')),
      video: at(4, 'ftyp') || at(0, [26, 69, 223, 163]) || (riff && at(8, 'AVI '))
    };
    if (!signatures[kind]) throw failure('invalid_media', 'file header does not match media type or supported signatures');
    return signatures;
  }

  function increment(distribution, key) {
    const value = own(distribution, key) ? distribution[key] + 1 : 1;
    Object.defineProperty(distribution, key, {value, writable: true, enumerable: true, configurable: true});
  }

  function recordFile(row, chunk, offset) {
    if (object(row) && typeof row.file === 'string' && row.file.length) return row.file.split(/[\\/]/u).pop();
    return String(chunk.id || chunk.path || 'chunk') + '#row-' + (offset + 1);
  }

  async function merge(options) {
    if (!object(options)) throw failure('invalid_options', 'merge options must be an object');
    const {index, loadChunk, loadAsset, write, onProgress, signal} = options;
    checkCancelled(signal);
    const request = validateRequest(options.request, index);
    if (typeof loadChunk !== 'function' || typeof write !== 'function') {
      throw failure('invalid_options', 'loadChunk and write must be functions');
    }
    if (request.datasets.some(name => mediaKinds.has(index.datasets[name].dataType)) && typeof loadAsset !== 'function') {
      throw failure('invalid_options', 'loadAsset is required for media datasets');
    }
    const id = typeof options.id === 'string' && options.id ? options.id : 'offline';
    const selected = Object.fromEntries(request.datasets.map(name => [name,
      Math.min(index.datasets[name].recordCount, request.limitPerDataset ?? index.datasets[name].recordCount)]));
    const total = Object.values(selected).reduce((sum, count) => sum + count, 0);
    const counts = counters();
    const sources = Object.fromEntries(request.datasets.map(name => [name, counters()]));
    const distributions = {domain: {}, language: {}, dataType: {}};
    const labelCounts = {};
    const seen = new Set();
    const writtenAssets = new Map();
    const files = [];
    // Retain only index/diagnostic JSONL, digests and counters, not records or asset bytes.
    let records = [];
    let failures = [];
    let duplicates = [];
    let hasSource = 0;
    let hasLicense = 0;
    let labeled = 0;
    const sourceSnapshot = () => Object.fromEntries(Object.entries(sources).map(([name, value]) => [name, {...value}]));

    function progress(phase, status = 'running') {
      if (typeof onProgress === 'function') onProgress({id, phase, status, total, ...counts, sources: sourceSnapshot()});
    }

    async function checked(action) {
      checkCancelled(signal);
      try {
        const value = await action();
        checkCancelled(signal);
        return value;
      } catch (error) {
        checkCancelled(signal, error);
        throw error;
      }
    }

    async function writeBytes(path, content, digest, include = true) {
      const actual = digest || await checked(() => sha256(content));
      await checked(() => write(path, content, {sha256: actual}));
      if (include) files.push({path, bytes: content.byteLength, sha256: actual});
    }

    function count(name, outcome) {
      counts[outcome]++;
      sources[name][outcome]++;
      counts.processed++;
      sources[name].processed++;
    }

    async function checkpoint() {
      if (counts.processed % 10 === 0 || counts.processed === total) {
        progress('merging');
        await new Promise(resolve => root.setTimeout(resolve, 0));
        checkCancelled(signal);
      }
    }

    function failRow(name, file, error) {
      checkCancelled(signal, error);
      failures.push(JSON.stringify({dataset: name, recordFile: file, reason: String(error && error.message || error)}) + '\n');
      count(name, 'failed');
    }

    async function prepare(row, name, file) {
      if (!object(row)) throw failure('invalid_record', 'chunk row must be an object');
      const record = row.record;
      const text = textContent(record);
      const catalog = index.datasets[name];
      const kind = catalog.dataType;
      let assetBytes = null;
      let assetHash = '';
      let binaryPath = null;
      let mediaSignatures = null;
      let raw = null;
      if (mediaKinds.has(kind)) {
        raw = truthy(record.raw_asset) ? record.raw_asset : {};
        if (!object(raw)) throw failure('invalid_record', 'raw_asset must be an object');
        assetHash = row.assetHash;
        if (typeof assetHash !== 'string' || assetHash.length !== 64 || /[^a-f0-9]/u.test(assetHash) ||
          !object(index.assets) || !own(index.assets, assetHash)) {
          throw failure('missing_asset', 'original asset descriptor not found');
        }
        const descriptor = index.assets[assetHash];
        if (!object(descriptor) || descriptor.sha256 !== assetHash) throw failure('asset_hash_mismatch', 'descriptor SHA256 differs from assetHash');
        if (!Number.isSafeInteger(descriptor.bytes) || descriptor.bytes < 0) throw failure('invalid_asset', 'invalid descriptor bytes');
        if (typeof descriptor.ext !== 'string') throw failure('invalid_asset', 'missing asset extension');
        const ext = descriptor.ext.replace(/^\./u, '').toLowerCase();
        if (ext && (ext.length > 16 || /[^a-z0-9]/u.test(ext))) throw failure('invalid_asset', 'unsafe asset extension');
        const written = writtenAssets.get(assetHash);
        if (written) {
          if (written.bytes !== descriptor.bytes) throw failure('asset_size_mismatch', 'descriptor bytes differ from the written asset');
          if (!written.signatures[kind]) throw failure('invalid_media', 'written asset header does not match this media type');
          binaryPath = written.path;
        } else {
          assetBytes = await checked(() => loadAsset(descriptor));
          if (Object.prototype.toString.call(assetBytes) !== '[object Uint8Array]') throw failure('invalid_asset', 'loader must return Uint8Array');
          if (!assetBytes.byteLength) throw failure('empty_asset', 'original media is empty');
          if (assetBytes.byteLength !== descriptor.bytes) throw failure('asset_size_mismatch', 'actual bytes differ from descriptor bytes');
          mediaSignatures = validateMedia(assetBytes, kind);
          if (await checked(() => sha256(assetBytes)) !== assetHash) throw failure('asset_hash_mismatch', 'actual bytes do not match expected SHA256');
          binaryPath = 'data/assets/' + assetHash + (ext ? '.' + ext : '');
        }
      } else if (!hasText(text)) {
        throw failure('missing_content', 'record has no body text');
      }
      const digest = await checked(() => sha256(encode([kind, text, assetHash])));
      const provenance = record.provenance ?? null;
      const sourceUrl = firstTruthy(record.source_url_safe, record.source_url, record.collection_address,
        object(provenance) ? provenance.source_url : null);
      const origin = {dataset: name, recordFile: file, recordId: record.record_id ?? null,
        url: sourceUrl, license: record.license ?? null, provenance,
        robots: firstTruthy(record.robots_protocol, record.Robts_protocol)};
      if (seen.has(digest)) return {duplicate: JSON.stringify({mergedId: digest, origin}) + '\n'};
      const labels = row.labels ?? {};
      if (!object(labels) || Object.values(labels).some(value => value !== null &&
        !['string', 'number', 'boolean'].includes(typeof value))) throw failure('invalid_record', 'labels must map names to scalar values or null');
      const recordPath = 'data/records/' + digest + '.json';
      const textPath = text ? 'data/text/' + digest + '.txt' : null;
      const exported = {...record};
      if (binaryPath) {
        const relative = '../assets/' + binaryPath.split('/').pop();
        exported.raw_asset = {...raw, local_path: relative, relative_path: relative, sha256: assetHash};
        if (own(raw, 'path')) exported.raw_asset.path = relative;
      }
      exported.merge_metadata = {id: digest, source_dataset: name, sample_labels: labels};
      const item = {id: digest, recordPath, textPath, assetPath: binaryPath,
        sourceDataset: name, sourceRecordId: record.record_id ?? null, sourceUrl,
        license: record.license ?? null, provenance,
        collectedAt: firstTruthy(record.collected_at, record.collection_time),
        domain: catalog.domain ?? null, language: catalog.language ?? null, dataType: kind, labels};
      return {digest, assetBytes, assetHash, binaryPath, mediaSignatures, textPath, textBytes: textPath ? encoder.encode(text) : null,
        recordPath, recordBytes: encode(exported), item, itemLine: JSON.stringify(item) + '\n'};
    }

    try {
      progress('merging');
      for (const name of request.datasets) {
        let remaining = selected[name];
        for (const chunk of index.datasets[name].chunks) {
          if (!remaining) break;
          checkCancelled(signal);
          const take = Math.min(remaining, chunk.records);
          let rows = null;
          try {
            rows = await checked(() => loadChunk(chunk));
            if (!Array.isArray(rows) || rows.length !== chunk.records) {
              throw failure('invalid_chunk', 'decoded row count does not match chunk.records');
            }
          } catch (error) {
            checkCancelled(signal, error);
            rows = null;
            for (let offset = 0; offset < take; offset++) {
              failRow(name, recordFile(null, chunk, offset), error);
              await checkpoint();
            }
            remaining -= take;
            continue;
          }
          try {
            for (let offset = 0; offset < take; offset++) {
              checkCancelled(signal);
              let prepared;
              try {
                prepared = await prepare(rows[offset], name, recordFile(rows[offset], chunk, offset));
              } catch (error) {
                failRow(name, recordFile(rows[offset], chunk, offset), error);
                await checkpoint();
                continue;
              }
              // Output errors must escape the row-failure handler: the archive is no longer reliable.
              if (prepared.duplicate) {
                duplicates.push(prepared.duplicate);
                count(name, 'duplicates');
              } else {
                if (prepared.assetBytes && !writtenAssets.has(prepared.assetHash)) {
                  await writeBytes(prepared.binaryPath, prepared.assetBytes, prepared.assetHash);
                  writtenAssets.set(prepared.assetHash, {path: prepared.binaryPath,
                    bytes: prepared.assetBytes.byteLength, signatures: prepared.mediaSignatures});
                }
                if (prepared.textPath) await writeBytes(prepared.textPath, prepared.textBytes);
                await writeBytes(prepared.recordPath, prepared.recordBytes);
                records.push(prepared.itemLine);
                seen.add(prepared.digest);
                for (const key of Object.keys(distributions)) increment(distributions[key], String(prepared.item[key]));
                let hasLabels = false;
                for (const [key, value] of Object.entries(prepared.item.labels)) {
                  if (!truthy(value) || (typeof value === 'string' && !hasText(value))) continue;
                  const labelKey = prepared.item.domain + '/' + key;
                  if (!own(labelCounts, labelKey)) Object.defineProperty(labelCounts, labelKey,
                    {value: {}, writable: true, enumerable: true, configurable: true});
                  increment(labelCounts[labelKey], String(value));
                  hasLabels = true;
                }
                hasSource += Number(truthy(prepared.item.sourceUrl));
                hasLicense += Number(truthy(prepared.item.license));
                labeled += Number(hasLabels);
                count(name, 'kept');
              }
              prepared = null;
              await checkpoint();
            }
          } finally {
            rows = null;
          }
          remaining -= take;
        }
      }
      checkCancelled(signal);
      progress('finalizing');
      await writeBytes('records.jsonl', encoder.encode(records.join('')));
      records = null;
      await writeBytes('failed_records.jsonl', encoder.encode(failures.join('')));
      const failureReport = counts.kept ? null : failures.map(line => JSON.parse(line));
      failures = null;
      await writeBytes('duplicate_origins.jsonl', encoder.encode(duplicates.join('')));
      duplicates = null;
      const metadata = {name: 'merged_' + id.slice(0, 8), schemaVersion: 1, request,
        recordCount: counts.kept, generatedAt: Date.now() / 1000, distributions,
        sampleLabelDistributions: labelCounts, labeledRecords: labeled,
        payloadBytes: files.reduce((sum, file) => sum + file.bytes, 0), sources: sourceSnapshot(),
        taskTypes: Array.from(new Set(request.datasets.flatMap(name => {
          const tasks = index.datasets[name].taskTypes;
          return Array.isArray(tasks) ? tasks.filter(task => typeof task === 'string') : [];
        }))).sort()};
      const report = {...counts, sources: sourceSnapshot(),
        deduplication: 'SHA-256 of [type, exact text, original asset SHA-256]',
        binaryDeduplication: 'SHA-256 shared assets; distinct captions retained',
        sampling: 'sorted record file prefix per dataset',
        quality: {sourceUrlPresent: hasSource, licenseFieldPresent: hasLicense, denominator: counts.kept,
          scopeScores: null, scoreStatus: 'not_evaluated',
          note: 'Evaluation engines were not run; source scores are not inherited or averaged. A license field is not proof of authorization.'}};
      await writeBytes('dataset_metadata.json', encode(metadata));
      await writeBytes('merge_report.json', encode(report));
      await writeBytes('README.md', encoder.encode(
        '# Merged dataset\n\n' +
        'records.jsonl indexes retained samples; all paths are relative to this directory. ' +
        'data/records preserves original fields plus merge_metadata; media paths point to original bytes in data/assets. ' +
        'data/text contains exact selected text without normalization.\n\n' +
        'duplicate_origins.jsonl preserves removed origins, source URLs, licenses, robots and provenance. ' +
        'failed_records.jsonl explains every unexported invalid record.\n\n' +
        'Labels and catalog distributions are recomputed from retained records only; absent/unknown labels are not invented. ' +
        'Identical media with different captions remains distinct. Consult original licensing and source evidence before use.\n\n' +
        'SHA-256/header validation checks integrity and supported signatures, not full media decoding or legal authorization. ' +
        'No quality engines were run and no aggregate quality scores are asserted.\n\n' +
        'manifest.json lists byte counts and SHA-256 of every file except itself. ' +
        'When the download is split into ZIP parts, extract all parts into the same directory.\n'));
      await writeBytes('manifest.json', encode({files}), null, false);
      const result = {...counts, sources: sourceSnapshot(), metadata, report};
      if (!counts.kept) {
        const error = failure('ALL_RECORDS_FAILED', 'No valid samples; failed_records.jsonl and merge_report.json have been written');
        error.result = result;
        error.failureReport = failureReport;
        error.failureReportPath = 'failed_records.jsonl';
        throw error;
      }
      progress('completed', counts.failed ? 'completed_with_errors' : 'completed');
      checkCancelled(signal);
      return result;
    } catch (error) {
      try {
        checkCancelled(signal, error);
      } catch (aborted) {
        progress('cancelled', 'cancelled');
        throw aborted;
      }
      progress('failed', 'failed');
      throw error;
    }
  }

  const api = {validateRequest, textContent, sha256, merge};
  root.OfflineMergeCore = api;
  if (typeof module === 'object' && module.exports) module.exports = api;
})(globalThis);
