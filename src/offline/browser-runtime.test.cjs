const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const fflate = require('./fflate.umd.js');
const target = path.join(__dirname, 'browser-runtime.js');
function runtime() { assert(fs.existsSync(target), 'browser-only runtime is not implemented'); return require(target); }
test('ZIP writer exports exact files with no HTTP or Python service', async () => {
  const {ZipParts} = runtime();
  const saved = [];
  const zip = new ZipParts({fflate, save:async (blob,info)=>saved.push({blob,info}), maxBytes:1000000});
  await zip.write('data/text/one.txt', new TextEncoder().encode('actual sample'));
  await zip.write('manifest.json', new TextEncoder().encode('{"ok":true}'));
  await zip.close();
  assert.equal(saved.length,1);
  const files = fflate.unzipSync(new Uint8Array(await saved[0].blob.arrayBuffer()));
  assert.equal(new TextDecoder().decode(files['data/text/one.txt']),'actual sample');
});
test('large exports split at file boundaries and preserve every actual byte', async () => {
  const {ZipParts} = runtime(); const parts=[];
  const zip = new ZipParts({fflate,maxBytes:200,save:async b=>parts.push(b)});
  const originals=Array.from({length:4},()=>crypto.randomBytes(300));
  for(let n=0;n<4;n++) await zip.write('data/assets/'+n+'.mp4', originals[n]);
  await zip.close(); assert.equal(parts.length,4);
  for(let n=0;n<4;n++) {
    const files=fflate.unzipSync(new Uint8Array(await parts[n].arrayBuffer()));
    assert.deepEqual(Buffer.from(files['data/assets/'+n+'.mp4']),originals[n]);
  }
});
test('ZIP writer rejects traversal and cancellation before retaining an output', async () => {
  const {ZipParts} = runtime(); let saves=0;
  const zip=new ZipParts({fflate,save:async()=>saves++});
  await assert.rejects(()=>zip.write('../secret',new Uint8Array([1])), /path/i);
  zip.abort(); await assert.rejects(()=>zip.write('x.txt',new Uint8Array([1])),/cancel/i);
  assert.equal(saves,0);
});
test('ZIP persistence failures surface instead of claiming a successful download', async () => {
  const {ZipParts}=runtime();
  const zip=new ZipParts({fflate,save:async()=>{throw new Error('quota exhausted');}});
  await zip.write('x.txt',new Uint8Array([1]));
  await assert.rejects(()=>zip.close(),/quota exhausted/);
});
