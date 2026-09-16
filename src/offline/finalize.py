"""Archive only the offline site after file-origin end-to-end acceptance."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import zipfile


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(2**20), b''):
            h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('package', type=Path)
    parser.add_argument('verification', type=Path)
    args = parser.parse_args()
    package, evidence = args.package.resolve(), args.verification.resolve()
    data = read(package / 'offline/reports/data-validation.json')
    browser = read(evidence / 'browser-verification.json')
    extended = read(evidence / 'extended-verification.json')
    relocation = read(evidence.parent / 'relocation-verification/browser-verification.json')
    assert data['recordsVerified'] == 855000 and data['datasetsVerified'] == 285
    assert data['failedRecords'] == data['failedPaths'] == 0
    assert browser['protocol'] == extended['protocol'] == 'file:'
    assert not browser['errors'] and not browser['network']
    assert not extended['errors'] and not extended['network']
    assert extended['result']['complete-6000']['processed'] == 6000
    assert extended['result']['complete-6000']['verifiedEverySha256']
    assert extended['result']['videos']['tested'] == 310
    assert relocation['sourceSandboxed'] and not relocation['outsidePackage'] and not relocation['network'] and not relocation['errors']
    verification = {'data': data, 'browser': browser, 'extended': extended, 'relocation': relocation,
                    'pythonNeededByRecipient': False, 'httpServerNeeded': False,
                    'browserSecurityOverrides': False, 'testedPlatform': 'macOS / Chrome',
                    'untestedPlatforms': ['Windows', 'Linux'],
                    'externalLinksNeedInternet': True}
    write(package / 'VERIFICATION.json', verification)
    readme = package / 'README.md'
    text = readme.read_text(encoding='utf-8')
    if '## 本次验收' not in text:
        readme.write_text(text + '\n## 本次验收\n已在正常安全设置、断网的 Chrome 中直接以 file:// 打开。报告、筛选、对比、CSV/Excel、实际合并、重开后的历史下载、取消任务通过测试。\n6,000 条完整合并与原后端一致：保留 5,437 条，完全重复 563 条，失败 0 条，逐文件 SHA256 正确。310 段视频均实际播放通过。\n另将完整包复制到独立目录，并在操作系统层禁止浏览器访问原项目，仍通过报告、媒体和真实合并下载测试。\nWindows/Linux 未在真实系统执行；无需安装运行环境，推荐已安装的 Chrome/Edge 浏览器。\n', encoding='utf-8')
    paths = sorted(p for p in package.rglob('*') if p.is_file() and p.name != 'package_manifest.json')
    forbidden = {'.env','.git','__pycache__','runtime','record_archives','.agents','.codex'}
    assert all(not (set(p.relative_to(package).parts) & forbidden) for p in paths)
    assert not any(p.suffix in ('.py','.exe','.bat','.command','.sh') for p in paths)
    def item(path):
        return {'path':path.relative_to(package).as_posix(),'bytes':path.stat().st_size,'sha256':sha(path)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        inventory = list(pool.map(item,paths))
    write(package / 'package_manifest.json', {'schemaVersion':1,'datasets':285,'records':855000,
          'requiresInstallation':False,'entry':'index.html','files':inventory,
          'unpackedBytes':sum(i['bytes'] for i in inventory)})
    archive = package.parent / (package.name + '.zip')
    partial = archive.with_suffix('.zip.partial')
    paths.append(package / 'package_manifest.json')
    with zipfile.ZipFile(partial,'w',allowZip64=True) as z:
        for n,path in enumerate(paths,1):
            compression = zipfile.ZIP_STORED if path.suffix.lower() in ('.jpg','.png','.mp4','.webm','.gz','.zip','.otf','.woff2') else zipfile.ZIP_DEFLATED
            z.write(path,package.name+'/'+path.relative_to(package).as_posix(),compress_type=compression,compresslevel=3)
            if n%5000==0: write(evidence/'zip-progress.json',{'files':n,'total':len(paths)})
    with zipfile.ZipFile(partial) as z:
        assert z.testzip() is None
        assert len(z.infolist()) == len(paths)
        sizes = {i.filename:i.file_size for i in z.infolist()}
        for i in inventory: assert sizes[package.name+'/'+i['path']]==i['bytes']
    partial.replace(archive)
    checksum=sha(archive)
    archive.with_suffix('.zip.sha256').write_text(checksum+'  '+archive.name+'\n',encoding='ascii')
    summary = {'zip':str(archive),'zipBytes':archive.stat().st_size,'sha256':checksum,
               'unpackedBytes':sum(p.stat().st_size for p in paths),'zipCrcVerified':True,
               'entry':str(package/'index.html'),'datasets':285,'records':855000,'files':len(paths),
               'requiresPython':False,'requiresServer':False,'actualVideoPlays':310}
    write(package.parent/'delivery_summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__': main()
