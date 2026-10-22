# Arca 仓库初始化提交计划（2026-10-23）

> 执行时机：**第五轮验收通过后**（kimi 改 src 期间禁止 commit，避免中途快照）。
> 前置：gh 已设备码登录；`.gitignore` / `LICENSE` / `NOTICE` 已就位。
>
> ⚠️ 2026-10-23 05:45 更新：社区模板（.github/ISSUE_TEMPLATE + CONTRIBUTING.md）
> 已提前经 Contents API 上传到远端（代理挡 github.com，api.github.com 稳定），
> 远端 main 现有 4 个单文件 commit。本地 main 也有同内容 commit 41a4a38——
> 首次正式 push 前先 `git fetch origin && git rebase origin/main`
> （同补丁会被自动丢弃），再按下面顺序提交推送。git push 走 https 需带
> `git -c http.version=HTTP/1.1`，且代理对 github.com 的 CONNECT 隧道 502，
> 备选：`env -u http_proxy -u https_proxy git push` 直连多次重试，
> 或继续用 gh api 分文件上传（文件 ≤1MB 时最稳）。

## 不进仓库的（.gitignore 已覆盖）
- `/build/`（venv、运行时副本、日志、探针、测试包）
- `/release/`、`/dist/`（构建产物；正式分发走 GitHub Releases）
- `/.workbuddy/`（本地记忆）、`*.log`、`gh-device.json`、`gh-token.tmp`
- `.tmp/`、`__pycache__`、`.DS_Store`

## 提交拆分（按轮次语义，content 为当前最终态）

1. `chore: repository bootstrap` —— `.gitignore`、`LICENSE`、`NOTICE`、`README.md`（现有）
2. `feat: Arca 1.0.0 application sources` —— `src/`（第 1–5 轮累计终态）+ `vendor/`（二进制与 THIRD-PARTY.md，~10MB 可接受）+ `Info.plist` 等应用资源
3. `test: gated test suite` —— `scripts/`（bundle/package 除外的测试脚本）+ `tests/fixtures/`
4. `docs: design spec, roadmap and acceptance reports` —— `docs/` 全部 + `WORKLOG.md`
5. 打 `v1.0.0` tag 指向提交 2

## 仓库设置
- 名 `arca`，Public，Apache-2.0（LICENSE 已在文件里，gh 建库时选 None 避免重复文件）
- description: `Native macOS archive manager — 7z/zip/rar/tar.zst, GBK-safe, PAR2 repair`
- push 后开启：Issues； Releases 由 A4 的 publish 流程产出

## 待用户确认
- git user.name / user.email（建议 GitHub noreply：`<id>+<username>@users.noreply.github.com`，登录后我从 gh api user 取 id 和 username）
- 仓库名是否就叫 `arca`（如被占用改 `arca-app`）
