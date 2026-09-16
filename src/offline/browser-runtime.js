(function (root) {
  'use strict';
  class ZipParts {
    constructor(options) {
      this.options = options;
      this.maxBytes = options.maxBytes || 192 * 1024 * 1024;
      this.parts = [];
      this.aborted = false;
      this.zip = null;
    }
    open() {
      this.buffers = []; this.bytes = 0; this.count = 0; this.failure = null;
      this.zip = new this.options.fflate.Zip((error, data) => {
        if (error) { this.failure = error; return; }
        this.buffers.push(data); this.bytes += data.length;
      });
    }
    async write(name, bytes) {
      if (this.aborted) throw new Error('cancelled');
      if (!name || name.startsWith('/') || name.split('/').includes('..') || name.includes('\\')) throw new Error('Invalid ZIP path');
      if (!this.zip) this.open();
      if (this.count && (this.bytes + bytes.length > this.maxBytes || this.count >= 50000)) {
        await this.flush(); this.open();
      }
      const compressed = /\.(json|jsonl|txt|md|html|csv)$/i.test(name);
      const entry = compressed ? new this.options.fflate.ZipDeflate(name, {level: 1}) : new this.options.fflate.ZipPassThrough(name);
      this.zip.add(entry); entry.push(bytes, true); this.count++;
      if (this.failure) throw this.failure;
    }
    async flush() {
      if (!this.zip || !this.count) return;
      this.zip.end();
      if (this.failure) throw this.failure;
      const blob = new Blob(this.buffers, {type: 'application/zip'});
      const info = {number: this.parts.length + 1, bytes: blob.size, files: this.count};
      info.name = 'dataset_part_' + String(info.number).padStart(3, '0') + '.zip';
      await this.options.save(blob, info);
      this.parts.push(info); this.buffers = []; this.zip = null;
    }
    async close() {
      if (this.aborted) throw new Error('cancelled');
      await this.flush(); return this.parts;
    }
    abort() { this.aborted = true; if (this.zip?.terminate) this.zip.terminate(); this.buffers = []; this.zip = null; }
  }
  const api = {ZipParts};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.OfflineBrowserRuntime = api;
  if (typeof document === 'undefined') return;

  const packageRoot = new URL('../', document.currentScript.src);
  const waiting = new Map();
  const receive = (id, encoding, payload) => {
    const slot = waiting.get(id);
    if (slot) { slot.received = true; slot.resolve({encoding, payload}); }
  };
  async function load(descriptor) {
    if (!descriptor?.path || !descriptor.id) throw new Error('本地数据分片索引缺失');
    if (descriptor.path.startsWith('/') || descriptor.path.includes('..') || descriptor.path.includes(':')) throw new Error('数据分片路径无效');
    if (waiting.has(descriptor.id)) throw new Error('数据分片正在读取，请稍候');
    let script, timer;
    try {
      const data = await new Promise((resolve, reject) => {
        const slot = {resolve, reject, received: false}; waiting.set(descriptor.id, slot);
        script = document.createElement('script'); script.src = new URL(descriptor.path, packageRoot).href;
        script.onerror = () => reject(new Error('本地数据分片无法打开，请完整解压 ZIP：' + descriptor.path));
        script.onload = () => { if (!slot.received) reject(new Error('数据分片内容不匹配：' + descriptor.path)); };
        timer = setTimeout(() => reject(new Error('读取数据超时：' + descriptor.path)), 120000);
        document.head.appendChild(script);
      });
      const binary = atob(data.payload);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      const digest = await root.OfflineMergeCore.sha256(bytes);
      if (digest !== descriptor.sha256 || bytes.length !== descriptor.bytes) throw new Error('数据分片校验失败：' + descriptor.path);
      if (data.encoding === 'gzip-base64') return JSON.parse(new TextDecoder().decode(root.fflate.gunzipSync(bytes)));
      if (data.encoding === 'base64') return bytes;
      throw new Error('不支持的数据分片格式');
    } finally { clearTimeout(timer); waiting.delete(descriptor.id); script?.remove(); }
  }
  root.OfflineData = {receive, load, root: packageRoot.href};

  let database;
  const db = () => database ||= new Promise((resolve, reject) => {
    const request = indexedDB.open('intelligent-discovery-html-v1', 1);
    request.onupgradeneeded = () => { request.result.createObjectStore('jobs', {keyPath:'id'}); request.result.createObjectStore('parts'); };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new Error('浏览器无法保存下载任务，请使用 Chrome/Edge 正常窗口或选择保存文件夹。'));
  });
  async function transact(store, mode, action) {
    const connection = await db();
    return new Promise((resolve, reject) => {
      const tx = connection.transaction(store, mode);
      const request = action(tx.objectStore(store)); let value;
      request.onsuccess = () => { value = request.result; };
      tx.oncomplete = () => resolve(value);
      tx.onabort = tx.onerror = () => reject(tx.error || new Error('浏览器存储不足，请清理下载任务或选择保存文件夹。'));
    });
  }
  root.OfflineJobs = {
    save: row => transact('jobs','readwrite',s=>s.put(row)),
    list: () => transact('jobs','readonly',s=>s.getAll()),
    part: (key, value) => value === undefined ? transact('parts','readonly',s=>s.get(key)) : transact('parts','readwrite',s=>s.put(value,key)),
    async remove(row) {
      for (const part of row.parts || []) await transact('parts','readwrite',s=>s.delete(row.id+':'+part.number));
      await transact('jobs','readwrite',s=>s.delete(row.id));
    }
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);
