# Arca 交接任务验收报告

验收通过

- 验收时间：2026-10-22 19:40 前后（GMT+8）
- 验收人：Arca 交接验收监视自动化（只读检查 + 跑测试，未修改任何项目代码）
- 被验收方：WORKLOG.md「新验收清单（2026-10-22）」所述交付

---

## 完成判定（第一步）

| 条件 | 结果 |
|---|---|
| WORKLOG.md 存在且含「验收清单」章节 | 通过（第 73 行「## 验收清单」、第 235 行「## 新验收清单（2026-10-22）」） |
| git 有新提交，或 WORKLOG 显示 §2 待办全部完成 | 通过（git 仍只有基线 cc3c6a8，但 WORKLOG 执行记录显示历史读取、Toast 上限、死配置 saveTo/openWhenDone、引擎 sha256 校验、inspect_cache LRU 全部完成，附十项门禁日志索引） |
| dist/Arca-1.0.0.dmg 与 .pkg 晚于 2026-10-22 08:30 | 通过（两者 mtime 均为 10-22 19:21） |

## 逐项验收（第二步）

### 1. node --check src/arca/web/app.js
通过。退出码 0，无输出。

### 2. design_audit.py（指定 Python 3.13.12）
通过。输出原文：
```text
design audit: clean

0 finding(s)
```

### 3. UI 回归 menu_test.py（headless 实例 + CDP）
通过。`rm -f build/port-menu.json` 后后台启动 `build/Arca-test.app/Contents/MacOS/Arca --headless --port-file build/port-menu.json`（端口 50340），6 秒后运行 `build/venv/bin/python -S -E scripts/menu_test.py`，输出原文：
```text
82 passed, 0 failed
```
结束后实例已终止（pgrep 确认无残留进程）。

### 4. integrity_test.py
通过。输出末行原文：
```text
通过 78 · 失败 0 · 跳过 0
```

### 5. 边界探针（build/accept_probe.py，Python + http 调 /api，12 项）
通过。`12 passed, 0 failed`。关键输出原文：
```text
PASS GBK inspect HTTP 200  [200]
PASS GBK inspect 显示正确中文名  [['中文文件.txt', '子目录/数据.csv']]
PASS GBK inspect 标记 nameEncoding=gbk  [gbk]
PASS GBK 解压任务完成  [done]
PASS GBK 解压落地中文名正确  [['中文文件.txt', '子目录']]
PASS GBK 解压内容一致
PASS 空 zip inspect 正常  [200 counts={'files': 0, 'dirs': 0}]
PASS 空 zip 解压任务完成  [done]
PASS 空 zip 解压建空目录
PASS 重复解压返回 409 code=exists  [409 exists]
PASS 只读目录解压任务失败  [error]
PASS 只读目录解压返回中文权限提示  [没有写入权限：换个保存位置，或在「显示简介」里放开这个文件夹的权限]
```
探针脚本仅写入 build/（build/accept_probe.py），未触碰 src/。

### 6. 代码抽查（读文件）
全部通过：
- a) `app.js` renderQueue 增量更新未回归：`QNODES`（app.js:685）、`makeJobEl`、`patchJobEl`（app.js:730）、`queueNodeSync`（app.js:818）均在；renderQueue 只做统计与同步，列表 `innerHTML` 仅在退出历史视图时清空一次（app.js:811），无全量重建回归。
- b) `zipfix.py` 接管守卫条件正确：plan() 中 `if any(zi.flag_bits & 0x1 ...)`（加密即放弃，zipfix.py:64）且 `if not fixed: return None`（zipfix.py:88），即「zip 且未加密且有名字被修正」才接管。
- c) `modalize(back)` 为单参数函数（app.js:1646），无 `(back, {closer})` 解构回归。
- d) 待办各项确实实现：
  - 历史读取：`app.js:874` `await api('/history')` → `state.history`，任务页无活动任务时渲染历史分组（renderQueue/showHistory）。
  - Toast 上限/去重：容器最多 3 条（app.js:119 `while (box.children.length >= 3)`），同 message+kind 合并计数显示 `×N`（app.js:111-117），错误 Toast 可点击关闭（app.js:128）。
  - 死配置：`saveTo`/`openWhenDone` 在 settings.py、server.py、app.js 中均已无匹配。
  - 引擎更新 sha256：updates.py 流式计算（:435-439）比对 hexdigest，优先官方 `.sha256` sidecar（:172）、回退 GitHub release asset `sha256:<64hex>` 摘要（:206-207），失败关闭。
  - inspect_cache LRU：64 条上限（server.py:468-470），写入 pop+重插（_inspect），读取命中同样 pop+重插刷新顺序（_extract 内），完整 LRU。
  - 附加：jobStatus 清理已实现（app.js:878-879 清除非存活任务键）。

### 7. 产物检查
通过：
- release/Arca.app 启动器 mtime 2026-10-22 19:18:45；dist DMG/pkg mtime 19:21:24/19:21:27，均为本轮新构建。
- SHA256 与 WORKLOG 记录一致：
  ```text
  f055b5eee1ccc3c951b4b296fe187b3a828c79647e8d337f123b411601c3ccb1  dist/Arca-1.0.0.dmg
  da1b81d30ae5080ab99ca8b7faccac683c89fe8bf96b1c7f1792e4b4097d6cc0  dist/Arca-1.0.0.pkg
  ```
- DMG 挂载（`diskutil image attach --mountPoint build/mnt-accept --mountOptions nobrowse`）抽查：卷内有 Arca.app + Applications；DMG 内启动器与 release/Arca.app 启动器 SHA256 一致（d993451a…179c），`CFBundleShortVersionString=1.0.0`、`CFBundleIdentifier=com.arca.archive`，启动器 mtime 19:18:45。
- 卸载：`hdiutil detach` 退出码 0（`"disk4" ejected.`），`hdiutil info` 中该挂载点记录数为 0，卸载干净。

## 遗留说明（不阻塞验收）
- WORKLOG 已如实记录：分卷 ZIP/7Z 读取因缺 fixture 跳过；浏览器无 native bridge，未实际调用 Finder reveal；未执行 PKG 系统安装（避免改动 /Applications）；旧 `scripts/update_test.py` 因固定旧版本断言未完整运行，但官方 26.05 真实下载+校验+安装端到端已通过（1/0）。
- git 基线未重置、未 commit（对方按约定未做 git 操作），以 WORKLOG + 产物 SHA256 作为交付凭证。

## 结论
全部验收项通过，交接任务判定为「验收通过」。
