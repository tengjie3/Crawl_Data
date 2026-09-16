import {createRequire} from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {browserOptions,packageArguments} from './e2e-support.mjs';
const require=createRequire(import.meta.url);
const {html,output:out}=packageArguments(process.argv.slice(2),'extended-e2e.mjs');
const {chromium}=require('playwright');
const fflate=require('./fflate.umd.js');
fs.mkdirSync(out,{recursive:true});
const browser=await chromium.launch(browserOptions());
const context=await browser.newContext({acceptDownloads:true,viewport:{width:1440,height:960}});
await context.setOffline(true);
const page=await context.newPage();
const errors=[],network=[];page.on('pageerror',e=>errors.push(e.message));
await context.route(/^https?:/,r=>{network.push(r.request().url());return r.abort();});
const result={};
async function merge(names,limit,label) {
  await page.evaluate(({names,limit})=>{const data=JSON.parse(document.getElementById('catalog-data').textContent);void openDatasetMerge(names.map(n=>data.datasets.find(d=>d.datasetName===n)),limit);},{names,limit});
  await page.locator('#mergeEstimateButton').click();await page.locator('#mergeStart').click();
  await page.locator('#mergeBody a[download]').first().waitFor({timeout:300000});
  const all={};let zipBytes=0;
  for(const link of await page.locator('#mergeBody a[download]').all()) {
    const promise=page.waitForEvent('download');await link.click();const downloaded=await promise;
    const target=path.join(out,label+'-'+downloaded.suggestedFilename());await downloaded.saveAs(target);
    const archive=fs.readFileSync(target);zipBytes+=archive.length;
    Object.assign(all,fflate.unzipSync(archive));
  }
  const report=JSON.parse(new TextDecoder().decode(all['merge_report.json']));
  const manifest=JSON.parse(new TextDecoder().decode(all['manifest.json']));
  for(const f of manifest.files) {assert.equal(all[f.path]?.length,f.bytes,f.path);assert.equal(crypto.createHash('sha256').update(all[f.path]).digest('hex'),f.sha256,f.path);}
  const records=new TextDecoder().decode(all['records.jsonl']).trim().split('\n').map(JSON.parse);
  assert.equal(records.length,report.kept);assert.equal(report.failed,0);
  for(const row of records) {
    const original=JSON.parse(new TextDecoder().decode(all[row.recordPath]));
    if(row.assetPath) {assert(all[row.assetPath].length>0);assert.equal(path.posix.normalize(path.posix.join(path.posix.dirname(row.recordPath),original.raw_asset.relative_path)),row.assetPath);}
    if(row.textPath) assert(all[row.textPath].length>0);
  }
  result[label]={processed:report.processed,kept:report.kept,duplicates:report.duplicates,failed:report.failed,files:manifest.files.length,zipBytes,verifiedEverySha256:true};
  await page.locator('.merge-close').click();return report;
}
try {
  await page.goto(pathToFileURL(path.resolve(html)).href);
  await page.locator('#catalog-data').waitFor({state:'attached'});
  await page.waitForFunction(()=>typeof OfflineData==='object' && typeof openDatasetMerge==='function');
  const report=await merge(['movie_English_html_text','movie_English_video'],null,'complete-6000');
  assert.equal(report.processed,6000);assert.equal(report.kept,5437);assert.equal(report.duplicates,563);
  const mixed=await merge(['education_English_html_text','education_German_image','literature_English_pdf'],5,'mixed');
  assert.equal(mixed.processed,15);
  await page.evaluate(()=>{const d=JSON.parse(document.getElementById('catalog-data').textContent).datasets;void openDatasetMerge([d.find(x=>x.datasetName==='movie_English_html_text'),d.find(x=>x.datasetName==='movie_English_video')],null);});
  await page.locator('#mergeEstimateButton').click();await page.locator('#mergeStart').click();
  await page.locator('#offlineCancel').click();
  await page.waitForFunction(()=>document.getElementById('mergeBody').textContent.includes('用户取消'));
  assert.equal(await page.locator('#mergeBody a[download]').count(),0);result.cancellation=true;
  await page.locator('.merge-close').click();
  const catalog=await page.locator('#catalog-data').textContent().then(JSON.parse);
  const reports=catalog.datasets.filter(d=>['movie','education','literature'].includes(d.domain));
  assert.equal(reports.length,86);
  for(const row of reports) {
    const url=new URL(row.datasetProfileHref,page.url());
    const text=fs.readFileSync(fileURLToPath(url),'utf8');assert(text.includes(row.datasetName));
  }
  result.individualReportCount=86;
  const clips=catalog.datasets.filter(d=>d.dataType==='video').flatMap(d=>d.samplePreviews.map(s=>({dataset:d.datasetName,url:new URL(s.webmHref||s.assetHref,page.url()).href})));
  const videoResults=[];
  for(let i=0;i<clips.length;i++) {
    const clip=clips[i];
    const playback=await page.evaluate(async url=>{
      const v=document.createElement('video');v.muted=true;v.src=url;v.preload='auto';document.body.append(v);
      try {
        await Promise.race([v.play(),new Promise((_,reject)=>setTimeout(()=>reject(new Error('play timeout')),8000))]);
        await new Promise((resolve,reject)=>{const start=Date.now();const timer=setInterval(()=>{if(v.currentTime>0.06&&v.videoWidth>0){clearInterval(timer);resolve();}else if(Date.now()-start>8000){clearInterval(timer);reject(new Error('frame timeout'));}},30);});
        return {width:v.videoWidth,height:v.videoHeight,time:v.currentTime};
      } catch(e) {return {error:e.message};}finally{v.pause();v.removeAttribute('src');v.load();v.remove();}
    },clip.url);
    assert(!playback.error,JSON.stringify({clip,playback}));videoResults.push({dataset:clip.dataset,...playback});
    if((i+1)%20===0) fs.writeFileSync(path.join(out,'video-progress.json'),JSON.stringify({tested:i+1,total:clips.length}));
  }
  assert.equal(videoResults.length,310);result.videos={tested:310,failed:0,actualPlayback:true};
  assert.deepEqual(errors,[]);assert.deepEqual(network,[]);
  fs.writeFileSync(path.join(out,'extended-verification.json'),JSON.stringify({result,errors,network,protocol:'file:',serverStarted:false},null,2));
  console.log(JSON.stringify(result,null,2));
} finally {await browser.close();}
