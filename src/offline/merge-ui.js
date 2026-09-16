(function () {
  'use strict';
  const index = window.OFFLINE_DATA_INDEX;
  const core = window.OfflineMergeCore;
  const store = window.OfflineJobs;
  const catalog = JSON.parse(document.getElementById('catalog-data').textContent);
  const known = new Map(catalog.datasets.map(row => [row.datasetName, row]));
  const dialog = document.createElement('dialog');
  dialog.className = 'merge-dialog';
  dialog.setAttribute('aria-labelledby','mergeTitle');
  dialog.innerHTML = '<header><h2 id="mergeTitle">合并数据集</h2><button type="button" class="merge-close" aria-label="关闭">×</button></header><div class="merge-body" id="mergeBody"></div>';
  document.body.appendChild(dialog);
  const body = dialog.querySelector('#mergeBody');
  const title = dialog.querySelector('#mergeTitle');
  const escape = value => String(value ?? '').replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
  const count = value => Number(value||0).toLocaleString('zh-CN');
  const bytes = value => value >= 1024**3 ? (value/1024**3).toFixed(2)+' GB' : value >= 1024**2 ? (value/1024**2).toFixed(1)+' MB' : Math.round(value/1024)+' KB';
  const labels = {running:'正在合并',completed:'合并完成',completed_with_errors:'部分完成',failed:'合并失败',cancelled:'已取消',interrupted:'任务已中断'};
  const phases = {merging:'读取、校验与合并',finalizing:'生成报告与压缩包',completed:'文件已生成',failed:'执行失败',cancelled:'已取消'};
  const objectUrls = new Set();
  const memoryJobs = new Map();
  let active = null, visibleJob = null, revision = 0;
  const delay = () => new Promise(resolve=>setTimeout(resolve,0));
  function show(heading, html) {
    title.textContent=heading; body.innerHTML=html;
    if (!dialog.open) dialog.showModal();
  }
  function revokeUrls() { for(const url of objectUrls) URL.revokeObjectURL(url); objectUrls.clear(); }
  function close() { visibleJob=null; revision++; revokeUrls(); dialog.close(); }
  dialog.querySelector('.merge-close').onclick=close;
  dialog.addEventListener('cancel',()=>{visibleJob=null;revision++;revokeUrls();});
  async function persist(row) {
    memoryJobs.set(row.id,row);
    try {await store.save(row);} catch(error) {row.persistenceWarning=error.message;}
  }
  function downloadJson(value,name) {
    const link=document.createElement('a'); const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));
    link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),60000);
  }
  async function renderJob(row, download=false) {
    if (!dialog.open || visibleJob!==row.id) return;
    const total=row.total || row.processed || 0;
    body.innerHTML='<h3>'+escape(labels[row.status] || row.phase || '正在准备')+'</h3>'+
      '<p class="merge-note">'+escape(phases[row.phase] || row.phase || '')+' · '+new Date(row.createdAt*1000).toLocaleString('zh-CN')+'</p>'+
      '<progress value="'+(row.processed||0)+'" max="'+Math.max(total,1)+'"></progress>'+
      '<p class="merge-note">已处理 '+count(row.processed)+' / '+count(total)+'</p>'+
      '<div class="merge-stats"><div>实际保留<strong>'+count(row.kept)+'</strong></div><div>完全重复<strong>'+count(row.duplicates)+'</strong></div><div>失败记录<strong>'+count(row.failed)+'</strong></div></div>'+
      Object.entries(row.sources||{}).map(([name,s])=>'<div class="merge-source">'+escape(name)+'<div class="merge-note">保留 '+count(s.kept)+' · 重复 '+count(s.duplicates)+' · 失败 '+count(s.failed)+'</div></div>').join('')+
      (row.error?'<p class="merge-error">'+escape(row.error)+'</p>':'')+
      (row.persistenceWarning?'<p class="merge-note">'+escape(row.persistenceWarning)+'</p>':'')+
      (row.parts?.length>1&&row.status.startsWith('completed')?'<p class="merge-note">共 '+row.parts.length+' 个 ZIP，请全部下载并解压到同一目录。</p>':'')+
      (row.savedToFolder?'<p class="merge-note">保存文件夹：'+escape(row.folderName)+'</p>':'')+
      '<div class="merge-actions" id="offlineDownloadLinks"></div>'+
      '<div class="merge-actions">'+(row.status==='running'?'<button type="button" id="offlineCancel">取消任务</button>':'<button type="button" id="offlineRebuild">按原选择重新生成</button>')+
      '<button type="button" id="offlineTaskReport">下载任务报告</button>'+(row.failureList?.length?'<button type="button" id="offlineFailures">下载失败清单</button>':'')+'</div>';
    body.querySelector('#offlineCancel')?.addEventListener('click',()=>{
      if(active?.row.id===row.id) {active.controller.abort(); row.phase='正在取消'; void renderJob(row);}
    });
    body.querySelector('#offlineRebuild')?.addEventListener('click',()=>openMerge(row.request.datasets.map(name=>known.get(name)),row.request.limitPerDataset));
    body.querySelector('#offlineTaskReport').onclick=()=>downloadJson(row,'merge_task_'+row.id.slice(0,8)+'.json');
    body.querySelector('#offlineFailures')?.addEventListener('click',()=>downloadJson(row.failureList,'failed_records.json'));
    if (!download || !row.status.startsWith('completed')) return;
    revokeUrls();
    for (const part of row.parts||[]) {
      const holder=body.querySelector('#offlineDownloadLinks');
      if (visibleJob!==row.id || !holder) return;
      if(row.savedToFolder) {
        const span=document.createElement('span'); span.className='merge-note'; span.textContent=part.name+' · '+bytes(part.bytes)+' · 已保存';holder.appendChild(span); continue;
      }
      try {
        const blob=await store.part(row.id+':'+part.number);
        if (!blob) throw new Error('浏览器已清理下载缓存，请按原选择重新生成。');
        if(visibleJob!==row.id) return;
        const link=document.createElement('a');const url=URL.createObjectURL(blob);objectUrls.add(url);
        link.className='merge-link merge-primary';link.href=url;link.download=part.name;
        link.textContent='下载 ZIP'+(row.parts.length>1?' '+part.number:'')+' · '+bytes(blob.size);holder.appendChild(link);
      } catch(error) {holder.textContent=error.message;}
    }
  }
  async function run(request, saveFolder) {
    if(active) {visibleJob=active.row.id;show('合并数据集','');await renderJob(active.row);return;}
    const id=crypto.randomUUID();
    let folder=null;
    if(saveFolder) {
      try {
        const picked=await window.showDirectoryPicker({mode:'readwrite'});
        folder=await picked.getDirectoryHandle('merged_'+id,{create:true});
      } catch(error) {if(error.name==='AbortError') return;throw error;}
    }
    const total=request.datasets.reduce((sum,name)=>sum+Math.min(index.datasets[name].recordCount,request.limitPerDataset||Infinity),0);
    const row={id,status:'running',createdAt:Date.now()/1000,request,total,processed:0,kept:0,duplicates:0,failed:0,sources:{},parts:[],savedToFolder:!!folder,folderName:folder?.name};
    const controller=new AbortController();visibleJob=id;revokeUrls();show('合并数据集','');
    const writer=new OfflineBrowserRuntime.ZipParts({fflate,save:async(blob,info)=>{
      if(controller.signal.aborted) throw new DOMException('已取消','AbortError');
      if(folder) {
        const file=await folder.getFileHandle(info.name,{create:true});
        const stream=await file.createWritable();try {await stream.write(blob);await stream.close();} catch(error) {await stream.abort();throw error;}
      } else await store.part(id+':'+info.number,blob);
      row.parts.push(info);await persist(row);
    }});
    active={row,writer,controller};await persist(row);await renderJob(row);await delay();
    try {
      const result=await core.merge({request,index,id,signal:controller.signal,
        loadChunk:descriptor=>OfflineData.load(descriptor),loadAsset:descriptor=>OfflineData.load(descriptor),
        write:async(name,data)=>{
          if(name==='failed_records.jsonl') row.failureList=new TextDecoder().decode(data).split('\n').filter(Boolean).map(JSON.parse);
          await writer.write(name,data);
        },
        onProgress:state=>{Object.assign(row,state,{status:'running'});void renderJob(row);}
      });
      await writer.close();
      if(controller.signal.aborted) throw new DOMException('已取消','AbortError');
      Object.assign(row,result,{status:result.failed?'completed_with_errors':'completed',phase:'文件已生成',parts:writer.parts});
    } catch(error) {
      writer.abort();if(error.result) Object.assign(row,error.result);row.status=controller.signal.aborted?'cancelled':'failed';row.error=controller.signal.aborted?'用户取消了本轮合并，未完成的文件已清理。':error.message;
      if(folder) for(const part of row.parts) {try {await folder.removeEntry(part.name);} catch {}}
      try {await store.remove(row);} catch {}
      row.parts=[];
    } finally {active=null;await persist(row);await renderJob(row,true);}
  }
  async function openMerge(items,limit=100) {
    revision++;visibleJob=null;revokeUrls();
    if(active) {visibleJob=active.row.id;show('合并数据集','');await renderJob(active.row);return;}
    items=(items||[]).filter(Boolean);
    const names=items.map(row=>row.datasetName);
    try {core.validateRequest({datasets:names,limitPerDataset:limit},index);}
    catch(error) {show('合并数据集','<p class="merge-error">'+escape(error.message)+'</p>');return;}
    show('合并数据集','<h3>已选 '+items.length+' 个数据集</h3>'+items.map(row=>'<div class="merge-source">'+escape(row.datasetName)+'</div>').join('')+
      '<div class="merge-options"><label>合并范围<select id="mergeMode"><option value="limited">每个数据集指定条数</option><option value="all">全部样本</option></select></label><label id="mergeLimitLabel"><input id="mergeLimit" aria-label="每个数据集条数" type="number" min="1" max="100000" value="'+(limit||100)+'"> 条 / 数据集</label></div>'+
      (window.showDirectoryPicker?'<label class="merge-note"><input type="checkbox" id="offlineFolder"> 直接保存到文件夹</label>':'')+
      '<div id="mergeEstimate" aria-live="polite"></div><div class="merge-actions"><button type="button" id="mergeEstimateButton">检查合并范围</button><button type="button" class="merge-primary" id="mergeStart" disabled>开始合并</button></div>');
    const mode=body.querySelector('#mergeMode'),input=body.querySelector('#mergeLimit'),estimate=body.querySelector('#mergeEstimate'),start=body.querySelector('#mergeStart');
    if(limit===null) {mode.value='all';body.querySelector('#mergeLimitLabel').hidden=true;}
    let approved=null;
    const invalidate=()=>{approved=null;start.disabled=true;estimate.innerHTML='';body.querySelector('#mergeLimitLabel').hidden=mode.value==='all';};
    mode.onchange=invalidate;input.oninput=invalidate;
    body.querySelector('#mergeEstimateButton').onclick=()=>{
      try {
        const request={datasets:names,limitPerDataset:mode.value==='all'?null:Number(input.value)};
        core.validateRequest(request,index);
        const rows=names.map(name=>({name,selected:Math.min(index.datasets[name].recordCount,request.limitPerDataset||Infinity)}));
        estimate.innerHTML='<div class="merge-stats"><div>待处理样本<strong>'+count(rows.reduce((n,row)=>n+row.selected,0))+'</strong></div><div>数据集<strong>'+rows.length+'</strong></div><div>执行位置<strong>当前浏览器</strong></div></div>';
        approved=request;start.disabled=false;
      } catch(error) {estimate.innerHTML='<p class="merge-error">'+escape(error.message)+'</p>';}
    };
    start.onclick=()=>{
      if(!approved) return;
      const folder=!!body.querySelector('#offlineFolder')?.checked;
      void run(approved,folder).catch(error=>{estimate.innerHTML='<p class="merge-error">'+escape(error.message)+'</p>';});
    };
  }
  async function openHistory() {
    revision++;visibleJob=null;revokeUrls();show('下载任务','<p>正在读取任务…</p>');
    const token=revision;
    let rows=[];
    try {rows=await store.list();} catch(error) {rows=[...memoryJobs.values()];if(!rows.length){body.textContent=error.message;return;}}
    if(token!==revision) return;
    rows.sort((a,b)=>b.createdAt-a.createdAt);
    for(const row of rows) if(row.status==='running'&&active?.row.id!==row.id) {row.status='interrupted';row.phase='可按原选择重新生成';await persist(row);}
    body.innerHTML=rows.length?rows.map(row=>'<div class="merge-history-item"><strong>'+escape(labels[row.status])+'</strong><span class="merge-note">'+new Date(row.createdAt*1000).toLocaleString('zh-CN')+'</span><div class="merge-note">'+row.request.datasets.map(escape).join(' + ')+'</div><p>保留 '+count(row.kept)+' 条 · 失败 '+count(row.failed)+' 条</p><div class="merge-actions"><button type="button" data-job="'+row.id+'">查看任务</button><button type="button" data-remove-job="'+row.id+'"'+(active?.row.id===row.id?' disabled':'')+'>清理下载缓存</button></div></div>').join(''):'<p>暂无合并任务</p>';
    body.querySelectorAll('[data-job]').forEach(button=>button.onclick=()=>{const row=rows.find(r=>r.id===button.dataset.job);visibleJob=row.id;title.textContent='合并数据集';void renderJob(row,true);});
    body.querySelectorAll('[data-remove-job]').forEach(button=>button.onclick=async()=>{const row=rows.find(r=>r.id===button.dataset.removeJob);try{await store.remove(row);memoryJobs.delete(row.id);await openHistory();}catch(error){button.textContent=error.message;}});
  }
  window.openDatasetMerge=openMerge;
  window.openDatasetMergeHistory=openHistory;
  window.addEventListener('beforeunload',event=>{if(active){event.preventDefault();event.returnValue='';}});
})();
