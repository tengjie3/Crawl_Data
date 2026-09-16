"""Patch only the generated copy; original site and datasets remain untouched."""
import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil


class Scripts(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.offsets = [0]
        for line in source.splitlines(keepends=True):
            self.offsets.append(self.offsets[-1] + len(line))
        self.source = source
        self.start = None
        self.parts = []
        self.targets = []
        self.feed(source)

    def position(self):
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.start = self.position()
            self.parts = []

    def handle_data(self, data):
        if self.start is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.start is not None:
            content = ''.join(self.parts)
            if '/api/merges' in content and 'window.openDatasetMerge' in content:
                self.targets.append((self.start, self.position() + len('</script>')))
            self.start = None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('package', type=Path)
    args = parser.parse_args()
    package = args.package.resolve()
    tools = Path(__file__).parent
    offline = package / 'offline'
    offline.mkdir(exist_ok=True)
    for name in ('fflate.umd.js', 'fflate-LICENSE.txt', 'merge-core.js', 'browser-runtime.js', 'merge-ui.js'):
        shutil.copy2(tools / name, offline / name)
    html = package / 'output/dataset_catalog_2026-09-02/dataset_catalog_zh.html'
    source = html.read_text(encoding='utf-8')
    targets = Scripts(source).targets
    if targets:
        assert len(targets) == 1, targets
        scripts = '\n'.join('<script src="../../offline/' + name + '"></script>' for name in
                            ('fflate.umd.js', 'data-index.js', 'merge-core.js', 'browser-runtime.js', 'merge-ui.js'))
        start, end = targets[0]
        html.write_text(source[:start] + scripts + source[end:], encoding='utf-8')
    else:
        assert '../../offline/merge-ui.js' in source
    entry = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1">'
             '<title>智能找数</title><meta http-equiv="refresh" content="0;url=output/dataset_catalog_2026-09-02/dataset_catalog_zh.html">'
             '</head><body><a href="output/dataset_catalog_2026-09-02/dataset_catalog_zh.html">打开智能找数</a></body></html>')
    (package / 'index.html').write_text(entry, encoding='utf-8')
    (package / '打开智能找数.html').write_text(entry, encoding='utf-8')
    readme = '''# 智能找数：双击 HTML 离线版

## 打开
1. 将 ZIP 完整解压到一个文件夹，保留所有子文件夹；不要在压缩软件内部打开网页。
2. 用 Chrome 或 Edge 打开根目录的 `index.html` 或 `打开智能找数.html`。
3. 不需要 Python、Node、启动脚本、后台服务或安装软件依赖。无需联网。

不要只复制一个 HTML 文件。网页、报告、样本和实际数据分片都是本包的一部分。Windows 建议解压到较短路径，例如 C:\\DataFind。

## 内容与功能
包含 285 个数据集、855,000 条完整记录及其原始媒体、86 份独立画像报告、电影/教育/文学 3 份领域画像报告。
可用：一句话定位、领域/语言/类型筛选、排序、查看报告、10 条样本滚动预览、清晰视频预览、2-4 数据集对比、CSV/Excel 导出和真实合并下载。
网页初次打开只加载目录与索引。合并时才按需读取实际记录和原始文件，绝不是仅导出元数据或预览图。

## 合并下载
在数据集清单选择 2-4 套数据，点击“合并下载”，选择条数或全部样本，检查范围后开始合并。
按数据类型、完整正文与原始媒体 SHA256 去除完全重复样本；同一媒体配不同正文仍然保留。许可、来源、标签和失败原因保留。
结果包含 records.jsonl、data/records、data/text、data/assets、dataset_metadata.json、merge_report.json、manifest.json 和 README。
大结果会生成多个独立 ZIP。全部下载后解压到同一文件夹即为一个合并数据集。请不要只下载第一部分。
支持的浏览器可勾选“直接保存到文件夹”，通过浏览器选择保存位置；不需要运行本地服务。
普通下载存放在浏览器本地缓存中。下载任务页可查看历史、重复下载或清理缓存；清理浏览器数据或移动本包可能使历史不可见，可按相同选择重建。
关闭网页会中断尚未完成的合并。重新打开后可在下载任务中重建。浏览器隐私窗口、禁止本地存储或存储额度不足时，可能无法缓存大结果；可选择保存文件夹或减少本轮条数。

## 数据口径与边界
数据、标签和现有评分均原样保留，不因打包而重新评分。目录的 219,284,168,029 Byte 是原数据集目录的逻辑存储量，不是 ZIP 大小；重复媒体按 SHA256 共享。
14 个语言分类中有 13 种已识别自然语言和 UnknownLanguage。285 数据集并不表示 855,000 条互不重复的样本。
归档总分权重为25%/20%/25%/30%，旧界面写为各25%，这一既有差异未在本次打包中改动。评分不代表许可获批或实际模型训练效果。
对原始来源的外部网址访问仍需要联网；本地报告、样本、筛选、对比和合并不需要联网。媒体解码支持取决于浏览器，本包保留 MP4/WebM 预览格式。
这是无需安装的静态 HTML 应用，不包含后台7×24采集服务，不会自动重新采集或重新计算四项评分。

## 校验与开源组件
完整性与浏览器验证记录见 VERIFICATION.json。SHA256 文件用于核对外层 ZIP。
fflate 0.8.3，MIT，已随包内置，不使用CDN运行时加载： https://github.com/101arrowz/fflate
许可文件：offline/fflate-LICENSE.txt。
'''
    (package / 'README.md').write_text(readme, encoding='utf-8')
    print(json.dumps({'entry': str(package / 'index.html'), 'patchedServerScripts': len(targets)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
