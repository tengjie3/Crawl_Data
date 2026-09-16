# 公开发布检查与脱敏工具

这组工具记录本次发布的数据安全处理过程，不是通用隐私合规证明，也不是用户打开网页的运行依赖。

- `audit_zip.py`：扫描归档文件名、明文和解码后的记录分片，只报告位置与类型，不输出疑似密钥值。
- `sanitize_publication.py`：针对本次已核验源归档生成独立公开副本；片段替换疑似密钥，更新分片、索引、文件清单和哈希，保留原始来源证据。
- `test_*.py`：检测、误报排除、记录保留、分片哈希与脱敏说明回归测试。

运行单元测试：

```sh
python3 -B -m unittest discover -s src/publication -p 'test_*.py' -v
```

重新检查完整公开包，可显式传入该版本的 SHA256：

```sh
python3 src/publication/audit_zip.py --zip /path/to/intelligent-data-discovery-html-only-2026-09-16.zip \
  --expected-sha f6b1e227b2435dbc6fd2d86db6d4d032930ac2ceeb8d9b278ee32b5b1228788e \
  --output /path/to/audit.json --workers 4
```

脱敏脚本是本次发布的可审阅实现，绑定原源包 SHA、目录及数量，不适用于任意归档。执行前应把四个脚本复制到独立工作目录，因为输出副本写在脚本旁。原始未脱敏归档不随公开 Release 分发；不要使用公开副本冒充原始输入来绕过校验。

扫描未 OCR 检查图片、PDF 或视频画面，不覆盖所有可能的隐私信息与混淆凭证。保留的第三方研究路径不代表打包了开发者私有目录。数据许可与隐私使用仍需逐项核对。
