# Arca 第二轮改进任务验收报告

验收通过（附两项环境性说明，见 §9）

- 验收时间：2026-10-22 21:20 前后（GMT+8）
- 验收依据：docs/improve-handoff-2026-10-22.md（任务书）§7、§8
- 被验收方：WORKLOG.md「第二轮最终验收清单（交付原文）」
- 验收方式：只读检查 + 复跑测试 + 行为探针，未修改任何项目代码

## 1. 语法与设计审计
- `node --check src/arca/web/app.js`：退出码 0。
- `design_audit.py`（指定 Python 3.13.12）：`0 finding(s)`。
- `git diff --check`：退出码 0。

## 2. UI 回归
- headless 实例 + `menu_test.py`：`82 passed, 0 failed`。实例已终止，无残留进程。

## 3. 引擎/集成与自测
- `integrity_test.py`：`通过 78 · 失败 0 · 跳过 0`。
- `selftest.py`：`通过 36 · 失败 0 · 跳过 1`（SKIPPED: RAR 解压，样本两次下载均
  报「Remote end closed connection without response」，网络抖动所致；分卷
  ZIP/7Z fixture 已生效并全部通过）。WORKLOG 记录 38/0/0，差异仅 RAR 一项。

## 4. 新增单元测试（C1/C2/C3 配套）
- `scripts/checksum_test.py`：`Ran 13 tests ... OK`。
- `scripts/compress_space_test.py`：`Ran 14 tests ... OK`。
- `scripts/inspect_cache_test.py`：`Ran 12 tests ... OK`。

## 5. update_test（C4 欠账）
- 以当前内置 26.04 为基线直接运行：`通过 22 · 失败 4`。4 项失败
  （发布日期与跨度 / 安全修复统计 / 逐版本清单 ≥13 行 / 长清单滚动）均为
  **版本跨度依赖断言**：26.04→26.05 只跨 1 个版本，测试按 13+ 行场景编写，
  属预期不适用，非产品缺陷。
- 对方声称的 `38/0` 使用 `build/improve_c4_update.py`（隔离 HOME + 真实官方
  23.01 二进制 + 在线升 26.05 + 校验 + 安装 + 恢复），其完整输出
  `build/improve-c4-update.log` 含 38 项逐条 ok 及引擎切换/恢复记录。
- 验收方两次复现该脚本均在 CDP Page.navigate 处超时（本环境当前对外部
  Chrome 启动受限），无法独立复现，**采信对方完整日志 + checksum_test 13/13
  对同一校验管线的独立覆盖**，判为有条件采信。

## 6. 行为探针（build/accept2_probe.py，经真实 /api，8 项）
`8 passed, 0 failed`：
- C2：50GB 稀疏文件压缩被开工前同步拒绝，未提交任务：
  `400 磁盘剩余空间不足：还需要约 50.0 GB，可用 10.8 GB`。
- C3：同路径替换归档（不同大小/内容）后 inspect 立即刷新为新条目，解压落地
  新内容（缓存失效生效）。
- 回归：GBK zip inspect 中文名与 nameEncoding=gbk 未回退。

## 7. 代码抽查
- **C1**：`updates.py` 抽出共享 `verify_download()`（:218，流式 SHA256 比对、
  fail closed）；`components.py` 下载后、解包/编译前调用
  （:366-369，含 release_api digest 回退）。zstd/par2 接入完整。
- **C2**：`_compress()` 开工前遍历输入估算（st_dev+st_ino 去重、拒绝符号
  链接、OSError 时放行），`free < need + 64MB` 即拒绝，文案与解压侧一致
  （server.py:526-559）。
- **C3**：`_archive_stat()` 返回 `(st_mtime_ns, st_size)`、stat 失败返回
  None（:470-475）；缓存写入前后双检防 TOCTOU（:479-482）；解压读取命中必须
  指纹一致且非加密包（:611-618）；64 条 LRU 保持不变。
- **回归守卫**：zipfix.py（08:21）、jobs.py（08:21）、styles.css（09:23）、
  app.js（11:48）mtime 均早于本轮 19:47 下发时间，上轮已验收代码未动。

## 8. 产物检查
- `release/Arca.app`（20:43:59）、`dist/Arca-1.0.0.dmg`（20:45:31）、
  `dist/Arca-1.0.0.pkg`（20:45:34），均为本轮新构建。
- SHA256 与 WORKLOG 记录逐字一致：
  ```text
  ff06e817ea4d6e9411979ab516257c2cfcaea1764926517a874858b93b1ad76d  dist/Arca-1.0.0.dmg
  dbae3d057405359f4870fc899c57ce1bf72c791a1f68da8d347ee885955b6e7c  dist/Arca-1.0.0.pkg
  ```
- 旧包同盘隐藏保留：`release/.Arca-before-improve-release-20261022-204358.app`。
- **DMG 挂载抽查说明**：验收方两次执行 `diskutil image attach` 均被环境
  终止（SIGKILL，当日 19:34 首轮验收时尚可正常挂载），无法独立挂载；改以
  证据链核验：对方 `build/improve-release-attach.log`（EXIT 0）、
  `improve-release-dmg-test.log`（挂载卷内 15 passed, 0 failed，覆盖 GBK
  中文名、空 ZIP、重复解压合并/新建副本）、`improve-release-detach.log`
  （`"disk4" ejected.`，EXIT 0）、`improve-release-hdiutil-info.log`
  （无残留镜像记录）、`improve-release-verification.log`（13 份日志与
  WORKLOG 逐字节一致；bundle.py/package.py 与基线 SHA256 相同未改动）。
  验收方确认 `hdiutil info` 无任何残留挂载记录。

## 9. 结论与说明
**验收通过。** C1-C4 全部实现且门禁通过，C5 评估不做（理由成立），C6 按约定留待人工。

两项环境性说明（不构成返工项）：
1. selftest 的 RAR 样本当前下载失败，复跑得 36/0/1（对方记录 38/0/0）；
   样本源网络问题，非代码缺陷，网络恢复后重跑 selftest 即可消除差异。
2. update_test 的 38/0 依赖 23.01→26.05 的跨度设置，本环境复现受 CDP
   超时阻塞，采信对方完整日志（38 项逐条 ok 含真实下载/校验/安装/恢复），
   且校验管线另有 checksum_test 13/13 独立覆盖。

遗留风险（对方已如实记录，与验收无冲突）：产物未签名/未公证（待证书）；
预检是拒绝不是预留；缓存指纹不能识别「mtime+size 同时不变」的替换；
组件安装为 mock+真实摘要计算，未对两组件都联网实测编译；PKG 未实际安装。
