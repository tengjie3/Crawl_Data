import {createRequire} from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import zlib from 'node:zlib';
import assert from 'node:assert/strict';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {browserOptions} from './e2e-support.mjs';
const require=createRequire(import.meta.url);
const {chromium}=require('playwright');
const directory=fs.mkdtempSync(path.join(os.tmpdir(),'crawl-data-loader-'));
const source=path.dirname(fileURLToPath(import.meta.url));
fs.mkdirSync(path.join(directory,'offline'),{recursive:true});
for(const name of ['browser-runtime.js','fflate.umd.js']) fs.copyFileSync(path.join(source,name),path.join(directory,'offline',name));
const record=[{file:'record_1.json',record:{text:'这是真实内容。'},labels:{},assetHash:null}];
const data=zlib.gzipSync(Buffer.from(JSON.stringify(record)));
const sha256=crypto.createHash('sha256').update(data).digest('hex');
fs.writeFileSync(path.join(directory,'offline','chunk.js'),`OfflineData.receive('test','gzip-base64','${data.toString('base64')}');`);
fs.writeFileSync(path.join(directory,'index.html'),`<!doctype html><meta charset="utf-8"><title>Local loader test</title><script src="offline/fflate.umd.js"></script><script>window.OfflineMergeCore={sha256:async bytes=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),x=>x.toString(16).padStart(2,'0')).join('')};</script><script src="offline/browser-runtime.js"></script>`);
let browser;
try {
  browser=await chromium.launch(browserOptions());
  const context=await browser.newContext();await context.setOffline(true);
  const page=await context.newPage();await page.goto(pathToFileURL(path.join(directory,'index.html')).href);
  const descriptor={id:'test',path:'offline/chunk.js',bytes:data.length,sha256};
  assert.deepEqual(await page.evaluate(d=>OfflineData.load(d),descriptor),record);
  assert.deepEqual(await page.evaluate(d=>OfflineData.load(d),descriptor),record);
  assert.match(await page.evaluate(async d=>{try{await OfflineData.load(d);}catch(e){return e.message;}},{...descriptor,sha256:'0'.repeat(64)}),/校验失败/);
  assert.match(await page.evaluate(async d=>{try{await OfflineData.load(d);}catch(e){return e.message;}},{...descriptor,path:'../escape.js'}),/路径无效/);
  assert.match(await page.evaluate(async d=>{try{await OfflineData.load(d);}catch(e){return e.message;}},{...descriptor,path:'offline/missing.js'}),/无法打开/);
  const persisted=await page.evaluate(async()=>{
    await OfflineJobs.save({id:'loader-test',status:'completed'});
    await OfflineJobs.part('loader-test:1',new Blob(['actual bytes']));
    return (await OfflineJobs.part('loader-test:1')).text();
  }); assert.equal(persisted,'actual bytes');
  await page.reload();assert((await page.evaluate(()=>OfflineJobs.list())).some(x=>x.id==='loader-test'));
  console.log(JSON.stringify({protocol:'file:',realClassicScriptLoads:true,hashMismatchRejected:true,pathTraversalRejected:true,missingFileRejected:true,idbPersistenceAfterReload:true,networkOffline:true,securityFlagsDisabled:false}));
} finally {await browser?.close();fs.rmSync(directory,{recursive:true,force:true});}
