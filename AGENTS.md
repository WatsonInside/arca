# AGENTS.md — 给接手本项目的 AI Agent

> 人类读者请看 [README.md](README.md)；实现细节看 [docs/ENGINEERING-NOTES.md](docs/ENGINEERING-NOTES.md)；
> 设计规则看 [docs/DESIGN.md](docs/DESIGN.md)。本文件只讲 Agent 需要知道的事实、规则与捷径。

## 这个项目是什么

Arca：macOS 原生压缩/解压应用。C 启动器（dlopen libpython）+ Python/pyobjc 后端 +
本地 HTTP + 无框架前端（WKWebView）。内置 7-Zip 26.05 / zstd 1.5.7 / par2 1.4.0（universal）。
单仓库、单人维护、无 CI——质量靠本地全量门禁。

## 硬性规则（先读这个，违反会造成实际伤害）

1. **未经用户事先确认，禁止 `git push`、创建 tag、发布 Release、或以任何方式同步到 GitHub。**
   包括"顺手提交推送"。用户已两次明确要求。本地 commit 允许。
2. 发布 Release 是已确认的例外流程，但每次发布前先向用户列出将要推送的内容。
3. 朋友圈图文等**个人分享素材不进仓库**，放 `build/wecom/`（build/ 已被 gitignore）。
4. 不要动 `.workbuddy/`（工作记忆）和根目录那些乱码名文件（历史遗留，忽略即可）。
5. 不要提交 `__pycache__`（.gitignore 已管）；不要原地改写 `.app` 内文件（arm64 签名缓存会 SIGKILL）。

## 环境

- Python：`build/venv/bin/python`（venv 已存在；重建：`make setup`）
- 所有脚本用 `-S -E -B` 标志跑（见 PY 变量，scripts/all_test.py）
- 测试用应用包：`build/Arca-test.app`（由 all_test 的 `sync_bundle` 同步源码生成）
- 发布产物：`release/Arca.app`、`dist/Arca-*.dmg|pkg|tar.xz`
- `dist/` 里可能有陈旧的 `Arca.app`——**别用它**，真实包在 `release/`

## 常用命令（等价 Makefile）

```bash
make test          # 全量门禁（30 套件，~3 分钟，发布前必须过）
make test-fast     # node --check + design_audit + selftest + progress（快速反馈）
make audit         # 设计规则确定性检查（0 = 干净）
make bundle        # 组装 release/Arca.app
make package       # dist/*.dmg + *.pkg
make publish       # 本地发布资产 + 同步 README 版本徽章（不上传）
make shots         # CDP 驱动真实 UI 截图到 assets/shots/
```

单跑某套件：`build/venv/bin/python -S -E scripts/<name>_test.py`。
需要 headless 实例的套件（menu/acceptance/update）先起实例：

```bash
build/Arca-test.app/Contents/MacOS/Arca --headless --port-file build/port-x.json
# 然后脚本传 --port-file build/port-x.json；HOME 必须指向临时目录（隔离状态）
```

## 已知坑（都真实踩过）

- **git push 经常失败**（代理 502 / HTTP2 framing）。兜底：用 `gh api` 走 Git Data API
  （blobs → tree → commit → PATCH refs/heads/main）。注意远端与本地可能是两套 SHA
  （API 提交 vs 本地提交），网络恢复后 `git fetch && git reset --hard origin/main` 对齐。
- **7-Zip 26.x 用退格符 `\b` 重绘进度行**（管道下无 `\r\n`）。解析必须走
  `engine.split_progress()`，有 progress_test 套件锁住。
- **设置页必须一屏可见**（`.sheet-body` 不滚动）且至少 2 个 `.sheet-fold`——menu_test 有断言。
- **沙箱限制**：从 /Applications 复制 .app 会大量 `Operation not permitted`；`ps` 部分参数被禁。
- 改 `src/arca` 后要同步到测试包：`cp -R src/arca build/Arca-test.app/Contents/Resources/app/arca`
  （先删旧的）。`scripts/interrupt_recovery_test.py` 会自己同步。
- 长时 soak 用 `--soak-seconds 1800` 以上（默认短冒烟仍在 `make test` 里，别把长跑接进去）：
  `build/venv/bin/python -S -E -B scripts/fuzz_soak_test.py --soak-seconds 2100 --heartbeat 30 --json build/soak-<ts>.json`。
- README 徽章/示例文件名的版本号由 `scripts/publish.py` 自动同步，不要手改。

## 测试矩阵速查

| 套件 | 测什么 |
| --- | --- |
| selftest 39 / integrity 97 | 引擎与组件、zstd/PAR2 全链路、Finder 集成（含 zstd 子进程资源收尾） |
| menu 103 | 右键菜单 + 设置面板 + A1 嵌套选择/重选真实 UI（CDP） |
| acceptance 22 | HTTP 行为探针 |
| progress 8 | 7-Zip 进度解析（退格符回归锁） |
| keyring 53 / appupdate 29 | 钥匙串、应用自更新（含重启脚本） |
| update 25 / update-long 27 | 引擎更新全流程（long 含真实下载） |
| compress-scan / compress-space / zip-* / inspect-* / extract-list / concurrency | 各子域 |
| jobwait / menu-cleanup | 任务等待时序、menu 收尾策略（mock） |
| partial-extract 35 / fuzz-soak 6 | A1 选择性解压与异常回滚、损坏/截断/穿越/低磁盘/有界 soak |
| interrupt-recovery 4 | 崩溃/中断后状态、任务日志、merge 回滚边界（真实 .app + 隔离 HOME） |
| compress-draft / menu 103 | 压缩草稿生命周期、取消状态、真实 UI→HTTP→磁盘回归 |

## 修改后的标准流程

1. `node --check src/arca/web/app.js`（改前端时）
2. `make test-fast`
3. 相关套件单跑
4. 交给用户确认后 → `make test` 全量 → bump 版本（src/arca/util.py APP_VERSION）
   → bundle + package + publish（README 徽章自动同步）→ **经用户确认后**再推 tag/Release。
5. 发布产物就绪后，**把本地链接直接贴给用户供本机测试**（Markdown 链接）：
   安装包 `dist/Arca-<版本>.dmg`、免装直开 `release/Arca.app`。
