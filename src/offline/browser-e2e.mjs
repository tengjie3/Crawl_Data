import {createRequire} from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {browserOptions,packageArguments} from './e2e-support.mjs';
const require=createRequire(import.meta.url);
const {html,output,mode,packageRoot}=packageArguments(process.argv.slice(2),'browser-e2e.mjs',true);
const {chromium}=require('playwright');
fs.mkdirSync(output,{recursive:true});
const browser=await chromium.launch(browserOptions());
const context=await browser.newContext({viewport:{width:1440,height:960},acceptDownloads:true});
const page=await context.newPage();
const errors=[], network=[], checks={};
const packagePrefix=pathToFileURL(packageRoot+path.sep).href;
const outsidePackage=[];
context.on('request',r=>{if(r.url().startsWith('file:')&&!r.url().startsWith(packagePrefix)) outsidePackage.push(r.url());});
context.on('page',p=>p.on('pageerror',error=>errors.push(String(error))));
page.on('pageerror',error=>errors.push(String(error)));
await context.route(/^https?:/,route=>{network.push(route.request().url());return route.abort();});
await context.setOffline(true);
const shot=name=>page.screenshot({path:path.join(output,name+'.png')});
async function filters(domain,language,type='') {
  await page.locator('#clearFilters').click();
  await page.locator('#domainFilter').selectOption(domain);
  await page.locator('#languageFilter').selectOption(language);
  await page.locator('#typeFilter').selectOption(type);
}
async function preview(name) {
  await page.locator(`#catalogWorkspace [data-preview-dataset="${name}"]`).click();
  await page.locator('#samplePreviewStage').waitFor({state:'visible'});
}
try {
  await page.goto(pathToFileURL(path.resolve(html)).href);
  await page.locator('#catalog-data').waitFor({state:'attached'});
  if(mode!=='smoke') await page.waitForFunction(()=>typeof OfflineData==='object' && typeof openDatasetMerge==='function',null,{timeout:30000});
  assert.equal(await page.evaluate(()=>typeof OfflineData), 'object','offline runtime missing: merge still requires Python');
  assert(page.url().startsWith('file:///'));
  if(mode==='smoke') {console.log('HTML-only runtime loaded');process.exitCode=0;}
  else {
    const catalog=await page.locator('#catalog-data').textContent().then(JSON.parse);
    assert.equal(catalog.datasets.length,285);
    assert.equal(catalog.summary.storageBytes,219284168029);
    await shot('home');
    await page.locator('#searchInput').fill('英语电影视频');
    await page.waitForFunction(()=>document.querySelector('#typeFilter').value==='video');
    assert.equal(await page.locator('.dataset-select').count(),1);
    checks.search=true;
    await preview('movie_English_video');
    await page.locator('.sample-video-play').first().click();
    await page.waitForFunction(()=>{const v=document.querySelector('video.sample-slide-video');return v?.currentTime>0.1&&v.videoWidth>0;},null,{timeout:30000});
    checks.video=await page.locator('video').first().evaluate(v=>({src:v.currentSrc,time:v.currentTime,width:v.videoWidth}));
    await shot('video'); await page.locator('#samplePreviewClose').click();
    await filters('movie','English','image'); await preview('movie_English_image');
    await page.waitForFunction(()=>document.querySelector('img.sample-slide-image')?.naturalWidth>0);
    checks.image=true; await page.locator('#samplePreviewClose').click();
    await filters('literature','English','pdf'); await preview('literature_English_pdf');
    const href=await page.locator('object.sample-slide-pdf').first().getAttribute('data');
    const pdfPath=fileURLToPath(new URL(href,page.url()));
    assert(fs.readFileSync(pdfPath).subarray(0,5).toString()==='%PDF-');
    checks.pdf=true; await shot('pdf'); await page.locator('#samplePreviewClose').click();
    await filters('movie','English','html_text'); await preview('movie_English_html_text');
    assert((await page.locator('.sample-slide-text').first().textContent()).length>80);
    checks.text=true; await page.locator('#samplePreviewClose').click();
    await filters('movie','English');
    for(const name of ['movie_English_html_text','movie_English_video']) await page.locator(`.dataset-select[value="${name}"]`).check();
    await page.locator('#compareSelected').click();
    assert.equal(await page.locator('#comparePanel .compare-card').count(),2); checks.compare=true;
    for(const id of ['exportCsv','exportExcel']) {
      const promise=page.waitForEvent('download'); await page.locator('#'+id).click();
      const download=await promise; const target=path.join(output,download.suggestedFilename());
      await download.saveAs(target); assert(fs.readFileSync(target,'utf8').includes('movie_English_video')); checks[id]=true;
    }
    await page.locator('#mergeSelected').click();
    assert(!(await page.locator('#mergeBody').textContent()).includes('Python'));
    await page.locator('#mergeLimit').fill('3');
    await page.locator('#mergeEstimateButton').click();
    await page.waitForFunction(()=>!document.querySelector('#mergeStart').disabled);
    await page.locator('#mergeStart').click();
    const link=page.locator('#mergeBody a[download]').first();
    await link.waitFor({state:'visible',timeout:120000});
    await shot('merge-complete');
    const downloadPromise=page.waitForEvent('download'); await link.click();
    const downloaded=await downloadPromise; const merged=path.join(output,'browser-merged.zip');
    await downloaded.saveAs(merged);
    const fflate=require('./fflate.umd.js');
    const files=fflate.unzipSync(fs.readFileSync(merged));
    const report=JSON.parse(new TextDecoder().decode(files['merge_report.json']));
    assert.equal(report.processed,6); assert.equal(report.failed,0);
    assert(Object.keys(files).some(p=>p.startsWith('data/assets/')));
    assert(Object.keys(files).some(p=>p.startsWith('data/text/')));
    checks.merge=report;
    await page.locator('.merge-close').click(); await page.reload();
    await page.locator('#mergeHistory').click();
    await page.locator('.merge-history-item').first().waitFor();
    assert((await page.locator('.merge-history-item').count())>0); checks.historyAfterReload=true;
    await page.locator('.merge-close').click();
    await filters('movie','English');
    const popup=context.waitForEvent('page');
    await page.locator('.dataset-profile-link[href*="movie_English_video"]').click();
    const profile=await popup; await profile.waitForLoadState('domcontentloaded');
    assert(profile.url().startsWith('file:')); assert((await profile.locator('body').textContent()).length>300);
    await profile.screenshot({path:path.join(output,'dataset-report.png')}); await profile.close(); checks.datasetReport=true;
    await page.locator('#clearFilters').click();
    for(const domain of ['movie','education','literature']) {
      const popup=context.waitForEvent('page');
      await page.locator(`.domain-report-link[href*="${domain}_dataset_profile"]`).first().click();
      const report=await popup; await report.waitForLoadState('domcontentloaded');
      assert(report.url().startsWith('file:'));
      for(const local of await report.locator('a[href]').evaluateAll(a=>a.map(x=>x.href).filter(x=>x.startsWith('file:')))) assert(fs.existsSync(fileURLToPath(new URL(local))),local);
      await report.screenshot({path:path.join(output,domain+'-report.png')}); await report.close(); checks[domain+'Report']=true;
    }
    await page.setViewportSize({width:390,height:844});
    await filters('movie','English','video'); await preview('movie_English_video');
    await page.locator('.sample-video-play').first().click();
    await page.waitForFunction(()=>document.querySelector('video.sample-slide-video')?.currentTime>0.1);
    checks.mobileVideo=true; await shot('mobile-video');
    assert.deepEqual(errors,[]); assert.deepEqual(network,[]);assert.deepEqual(outsidePackage,[]);
    fs.writeFileSync(path.join(output,'browser-verification.json'),JSON.stringify({checks,errors,network,outsidePackage,protocol:'file:',serverStarted:false,browserSecurityDisabled:false,sourceSandboxed:!!process.env.CHROME_WRAPPER},null,2));
    console.log(JSON.stringify(checks,null,2));
  }
} finally {await browser.close();}
