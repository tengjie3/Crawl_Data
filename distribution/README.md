# 完整离线版下载说明

下载同一版本的三个 `.zip.part01`、`.zip.part02`、`.zip.part03` 文件；它们不是独立压缩包，不要直接解压分卷。

1. 把三个分卷和此目录内的合并脚本放到同一文件夹。
2. Windows：双击 `join-windows.cmd`。macOS/Linux：在此目录运行 `sh join-macos-linux.command`。
3. 等待 SHA256 校验通过，再解压生成的完整 `.zip` 文件。
4. 用 Chrome/Edge 打开解压目录里的 `index.html`。网页运行无需 Python、Node、服务器或联网。

脚本只拼接本地文件并核验已固定的 SHA256，不访问网络、不修改系统设置，不会覆盖已有完整 ZIP。下载与解压建议至少留出 15 GB 磁盘。

需要手动合并时，macOS/Linux 可执行：

```sh
cat intelligent-data-discovery-html-only-2026-09-15.zip.part01 intelligent-data-discovery-html-only-2026-09-15.zip.part02 intelligent-data-discovery-html-only-2026-09-15.zip.part03 > intelligent-data-discovery-html-only-2026-09-15.zip
shasum -a 256 intelligent-data-discovery-html-only-2026-09-15.zip
```

完整 ZIP 的 SHA256 必须是：

```text
59895244a6888785e56b60a3757f865cd63b47b37d83d128f00e12c4d40a2602
```

Windows 脚本使用系统自带的 `copy` 和 PowerShell `Get-FileHash`；目前未在 Windows 真机测试。若脚本不可运行，也可以使用支持分卷拼接的已有工具，拼接后务必比对上述 SHA256。
