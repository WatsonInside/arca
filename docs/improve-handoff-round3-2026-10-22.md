# Arca 第三轮改进任务交接（2026-10-22）

> 交接对象：kimi code / GPT-6.1-sol。验收方：Arca 交接验收监视。
> 本轮范围（用户已拍板）：**功能项只做 A6；工程项 C1-C3 全做**。
> 性能项 B1-B4 留待第四轮（并行调度需要单独充分测试，本轮不要碰 jobs.py 的调度结构）。
> 背景文档：`docs/handoff-2026-10-22.md`（§4 构建坑）、`docs/DESIGN.md`、
> `docs/ROADMAP.md`（A6/C1-C3 的现状依据）、`docs/acceptance-report-round2.md`。
> **动手前必读 DESIGN.md 与 handoff §4。**

---

## 0. 范围总览

| 编号 | 项目 | 要求 |
|---|---|---|
| A6 | 加密包密码 Keychain 托管 | 必做，见 §1 |
| C1 | 门禁聚合脚本 all_test.py | 必做，见 §2 |
| C2 | 测试健壮性修复（跨度断言 + RAR 样本本地化） | 必做，见 §3 |
| C3 | 验收探针转正 | 必做，见 §4 |
| B1-B4 | 性能项 | **本轮不做**，不许顺手改调度、渲染策略 |
| 其他 | A1/A2/A4/A5、签名公证 | **本轮不做** |

逐项门禁不变：**每完成一项立即跑 §6 门禁，通过后才准开下一项。**

---

## 1. A6 · 加密包密码 Keychain 托管（必做）

### 现状
- 前端：`app.js:587`（#x-pw-row 显隐）、`app.js:1140`（extract 提交带
  `password: x.pw`）、`app.js:1179`（/api/test 验密）。每次解压加密包都要重输。
- 后端：`/api/extract`、`/api/inspect`、`/api/test` 均接受 `password` 字段。
- 本环境可用 `/usr/bin/security`（已确认存在），**不引入 PyObjC Security
  框架依赖**，统一走 `security` CLI（`find-generic-password` /
  `add-generic-password` / `delete-generic-password`），subprocess 调用。

### 方案
1. 新文件 `src/arca/keyring.py`（标准库 + security CLI）：
   - service 固定 `Arca`；account 用归档解析路径的 SHA256 前 16 位十六进制
     （不含文件名明文，避免 keychain 里泄露用户目录结构）；
   - `remember(path, password)`：仅当调用方显式传入 opt-in 标志才写；
   - `lookup(path) -> str | None`；`forget(path)`；`clear_all()`；
   - 条目数上限 50（超出淘汰最旧，由 server 层维护一份路径哈希索引，
     存在 settings 或独立 state 文件均可，说明选择即可）；
   - security CLI 不存在或调用失败时静默降级为「无此功能」，绝不阻塞解压。
2. API：
   - `/api/extract`、`/api/test` 请求体增加 `remember: bool`（默认 false）：
     仅 true 时在提交任务**前**存入 keychain（用户主动勾选，明示
     「密码可能不正确时可删除重存」）；
   - `/api/extract`、`/api/inspect`、`/api/test` 当 `password` 为空时先
     `lookup` 静默补齐（命中即用，不回传前端明文）；
   - `/api/settings` 增加只读摘要（记住的归档数量与文件名列表，**不含密码**）
     与 `clearSavedPasswords` 动作。
3. 前端（app.js）：
   - #x-pw 行旁加「记住此密码」勾选框（默认不勾，样式走现有 checkbox 令牌，
     遵守 DESIGN.md「一个控件一个提示通道」）；
   - 勾选后提交带 `remember: true`；命中已存密码时不自动填入输入框，
     而是在按钮/副标题提示「已使用记住的密码」；验密失败时提示可清除；
   - 设置面板折叠组加一行「已记住 N 个加密包的密码 · 全部清除」。

### 红线
- 绝不把密码写进日志、history.json、settings、inspect_cache 或任何明文文件；
- 绝不静默记忆（必须显式勾选）；绝不把密码出现在任何 GET 接口响应里；
- zipfix/引擎更新/调度/渲染策略不动。

---

## 2. C1 · 门禁聚合脚本（必做）

- 新建 `scripts/all_test.py`：按顺序执行并汇总，任一失败即非零退出：
  1. `node --check src/arca/web/app.js`
  2. design_audit.py（用 venv python，`-S -E`）
  3. `selftest.py`（先于需要实例的测试）
  4. `integrity_test.py`
  5. `checksum_test.py`、`compress_space_test.py`、`inspect_cache_test.py`
  6. **需要 headless 实例的部分**：自行组装/复用测试包（按 handoff §4.1 的
     ARCA_PYTHON 方案），`rm -f build/port-all.json` → 启动
     `--headless --port-file build/port-all.json` → 等端口文件就绪 →
     改造 `menu_test.py` 与 `update_test.py` 支持 `--port-file` 参数指定
     端口文件（默认值保持 `build/port-menu.json` / `build/port-update.json`
     不变，向后兼容）→ 运行 → ** finally 终止实例**；
  7. 汇总输出一份表格：每项 名称/结果/关键计数，末行总结 `X passed, Y failed`
     的套件数。
- 注意：update_test 完整跑需要 23.01 跨度设置（见 C2），all_test 默认跑
  update_test 的**非安装部分**（即当前 26.04 基线可过的 22 项 + C2 修正后
  的自适应断言），`--full-update` 开关才跑带真实安装的完整流程。

## 3. C2 · 测试健壮性修复（必做）

1. **update_test 跨度自适应**（`scripts/update_test.py:198、211`）：
   - 「逐版本清单覆盖全部跨越版本」改为：行数 == 用 `updates.fetch_history`
     计算的 CURRENT→LATEST 实际跨越版本数（缓存检查结果里已有）；
   - 「写明发布日期与版本跨度」「统计了安全修复数量」在跨度为 1 时放宽为
     接受「发布于 …」单版本文案；
   - 「长清单在面板内滚动」仅当行数 > 面板可视高度时断言 scrolls==True，
     否则断言无需滚动（跳过而非失败，输出 `skip（跨度 N 行）`）；
   - 用合成历史数据（fixture 注入 fetch_history 返回值）单独测一次 13+ 行
     滚动场景，保证长清单 UI 仍有覆盖——这也是 all_test 默认档跑的内容。
2. **RAR 样本本地化**：`selftest.py` 的 RAR 测试样本改为仓库内 vendor
   （生成一个几 KB 的 rar5 样本或从可靠源取一次后放入 `tests/fixtures/`，
   说明来源与校验值），断网也能跑；下载逻辑保留为 fallback。
3. 目标：`scripts/all_test.py` 在**断网 + 当前 26.04 基线**下全部通过，
   无 skip（除 RAR 创建类本就依赖 WinRAR 的既有 skip）。

## 4. C3 · 验收探针转正（必做）

- 将 `build/accept_probe.py`（GBK/空包/409/只读）与 `build/accept2_probe.py`
  （空间预检/缓存失效/GBK 回归）合并为 `scripts/acceptance_probe.py`：
  - 端口文件可参数化（默认 `build/port-menu.json`，与 all_test 兼容）；
  - 稀疏大文件探针改为参数化大小（默认 50GB，加 `--probe-size` 开关）；
  - 输出格式保持 `PASS/FAIL name [detail]` 与末行 `N passed, M failed`；
  - 加入 all_test 的可选段（默认跑，作为第 8 项）。
- build/ 下两个原探针文件删除，不留副本。

---

## 5. 明确不做 / 不许动

- jobs.py 调度结构、renderQueue 渲染策略、zipfix 接管守卫、modalize 签名
  （均已验收，不许顺手重构）；
- A1/A2/A4/A5 功能、B 系性能项、签名公证；
- 打包脚本 bundle.py/package.py；不 git commit/reset；不触碰 `.workbuddy/`。

## 6. 门禁命令（每项做完必跑；A6 涉及 UI，加跑 UI 回归）

```bash
node --check src/arca/web/app.js
build/venv/bin/python -S -E scripts/design_audit.py        # 0 findings

rm -f build/port-menu.json
build/Arca-test.app/Contents/MacOS/Arca --headless --port-file build/port-menu.json &
sleep 6
build/venv/bin/python -S -E scripts/menu_test.py           # 82 passed, 0 failed
build/venv/bin/python -S -E scripts/acceptance_probe.py    # C3 完成后
# 实例记得杀掉

build/venv/bin/python -S -E scripts/integrity_test.py      # 78 通过
build/venv/bin/python -S -E scripts/selftest.py            # 0 失败
build/venv/bin/python -S -E scripts/checksum_test.py       # 13 tests OK
build/venv/bin/python -S -E scripts/compress_space_test.py # 14 tests OK
build/venv/bin/python -S -E scripts/inspect_cache_test.py  # 12 tests OK
```

A6 专项补充测试（新建 `scripts/keyring_test.py`）：
- security CLI mock 夹具：存取删、路径哈希稳定、上限 50 淘汰、CLI 缺失降级、
  lookup 未命中返回 None；
- 至少 8 个夹具，密钥不落日志/明文的断言（捕获 subprocess 输出与写文件行为）。

构建坑同 handoff §4（ARCA_PYTHON 复制运行时、同盘隐藏名移旧包、
_rmtree_batched、hdiutil detach）。

## 7. 收尾与交付（全部必做项通过后）

1. 更新 `WORKLOG.md`：追加「第三轮（2026-10-22）」执行记录 + 新验收清单，
   每项含改动文件、门禁输出原文、A6 的 keychain 行为说明；
2. 重新 bundle + package，确认 dist DMG/pkg 时间戳更新并记录 SHA256；
3. 挂载新 DMG 抽查（GBK/空包/重复解压 + **新增加密包场景：记住密码后
   重启实例二次解压不再要求输入**）后 detach 干净；
4. 运行 `scripts/all_test.py` 全量并附完整输出原文。

验收方将逐项复核：门禁复跑、keyring 行为探针（含「不勾选绝不写入」与
「CLI 缺失降级」）、代码抽查（红线：密码不出现在任何日志/响应/明文文件）、
产物时间戳与 SHA256 一致性。
