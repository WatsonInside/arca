# Arca 改进任务交接（第二轮 · 2026-10-22）

> 交接对象：kimi code / GPT-6.1-sol。
> 本文档定义本轮改进的范围、验收门禁与执行顺序。验收方：Arca 交接验收监视（自动化）。
> 上轮交接背景见 `docs/handoff-2026-10-22.md`，上轮执行记录见 `WORKLOG.md`。
> **动手前必读：`docs/DESIGN.md`（尤其第 7、8 节）与 `docs/handoff-2026-10-22.md` §4（本环境构建坑）。**

---

## 0. 范围总览

| 编号 | 项目 | 优先级 | 要求 |
|---|---|---|---|
| C1 | 组件下载补 SHA256 校验 | 必做 | 见 §1 |
| C2 | 压缩方向磁盘空间预检 | 必做 | 见 §2 |
| C3 | inspect_cache 陈旧失效 | 必做 | 见 §3 |
| C4 | 测试欠账清偿 | 必做 | 见 §4 |
| C5 | 单 worker 保守并行 | 可选 | 见 §5，时间不足可不做 |
| C6 | 代码签名 / 公证 | **本轮不做** | 需要 Apple 开发者账号证书，无法由 AI 完成，留待人工 |

逐项门禁：**每完成一项立即跑该项目的门禁（见 §7），通过后才准开下一项**。上轮已因「提前推进未过门禁」返工过一次，勿再犯。

---

## 1. C1 · 组件下载补 SHA256 校验（必做）

- 现状：`src/arca/updates.py` 已实现引擎更新的流式 SHA256 校验（:435-439 比对
  hexdigest；sidecar `.sha256` 优先、GitHub release asset `sha256:<64hex>` 回退、
  fail closed），但 `src/arca/components.py` 的 zstd / par2cmdline 下载安装
  **没有任何完整性校验**。
- 要求：
  1. 把 updates.py 的「取期望摘要 + 流式计算 + 比对」抽成可复用函数（放
     `updates.py` 导出或新建 `verify.py`，二选一，说明理由即可）；
  2. components.py 两个组件接入：摘要获取不到 = fail closed 报
     「无法获取校验值」；不匹配 = 报「校验不符，已放弃安装」且不落盘生效；
  3. 参考 updates.py 的处理：zstd / par2 各自的官方发布页是否有 `.sha256`
     sidecar 或 GitHub digest，先实际核对再写解析逻辑，不要硬编码摘要值；
  4. mock 夹具测试参考上轮 updates 的做法（大小写、Windows 路径、错名、
     非 hex、不匹配、缺失、正确通过），至少 6 个夹具。

## 2. C2 · 压缩方向磁盘空间预检（必做）

- 现状：`server.py:592` 附近只有解压路径做了 `disk_usage` 对比；压缩一个大
  目录到快满的磁盘会中途失败且半成品行为不明确。
- 要求：
  1. `_compress()` 开工前估算输出大小（可用输入总大小 × 格式系数，7z/zip
     压缩只会更小，用输入原大小做上限最保守）+ 与解压一致的 64MB 余量；
  2. 不足时直接拒绝，文案风格对齐解压侧：「磁盘剩余空间不足：还需要约
     X，可用 Y」（用 `util.human_size`）；
  3. 注意：估算不准时宁可放行（预检只拦明显不够的情况）。

## 3. C3 · inspect_cache 陈旧失效（必做）

- 现状：`server.py` `_inspect()` 的缓存只按路径存 `uncompressed/size/ts`，
  不校验归档是否被替换；同名文件换内容后，解压空间预检会用旧值。
- 要求：
  1. 写入与读取命中时用 `os.stat` 的 `(st_mtime_ns, st_size)` 做失效键，
     不一致即视为 miss 重新 inspect；
  2. stat 失败（文件已删）按 miss 处理；
  3. 保持现有 64 条 LRU 行为不变（本轮上轮刚做完，勿回退）。

## 4. C4 · 测试欠账清偿（必做）

1. **分卷 ZIP / 7Z 读取 fixture**：selftest 里两项 SKIPPED 因 fixture 缺失。
   用脚本生成小型分卷测试包（7z 的 `-v` 开关即可，几 KB 就够），落到
   测试 fixtures 目录，让这两项真正跑起来。
2. **`scripts/update_test.py` 动态版本断言**：现固定断言旧 26.04 导致不能
   跑。改成从 `updates.check` 实际结果取版本断言（或 fixture 固定本地
   检查响应），让整套旧更新 UI 测试恢复运行。
3. 完成后完整跑一遍 §7 全部命令，给出各项计数。

## 5. C5 · 单 worker 保守并行（可选，谨慎）

- 现状：`jobs.py:99` 单工作线程，大任务排队时全部串行。
- 若做，只允许保守方案：**任务按目标输出目录分桶，不同桶可并行、同桶仍
  串行**（worker 池 2 个即可）；同输出目录并发写 7zz 是上轮明确识别的风险，
  不许引入。UI 多 active 计数 `nav-count` 已支持。
- 若评估后不做，在 WORKLOG 写明理由即可，不算失败项。

## 6. 明确不做 / 不许动

- 代码签名与公证（C6，等证书）。
- `zipfix.py` 的接管守卫逻辑、`modalize` 签名、renderQueue 增量更新——
  上轮验收通过项，不许顺手重构。
- P3 三个观察项（255 字节截断、`.tar.zst` unique_path、编码手动开关）维持现状。
- 不 git commit / reset，不触碰 `.workbuddy/memory/`，不改打包脚本。

## 7. 门禁命令（每项做完必跑；改 app.js 的项加跑 UI 回归）

```bash
node --check src/arca/web/app.js
/Users/watsoninside/.workbuddy/binaries/python/versions/3.13.12/bin/python3 scripts/design_audit.py   # 必须 0 findings

# UI 回归 82 项（menu_test 固定读 build/port-menu.json）
rm -f build/port-menu.json
build/Arca-test.app/Contents/MacOS/Arca --headless --port-file build/port-menu.json &
sleep 6
build/venv/bin/python -S -E scripts/menu_test.py     # 期望 82 passed, 0 failed，跑完杀掉实例

# 引擎/集成
build/venv/bin/python -S -E scripts/integrity_test.py   # 期望 78 通过
build/venv/bin/python -S -E scripts/selftest.py         # C4 完成后期望 0 失败、0 跳过
```

**本环境构建坑（同上轮，全文见 handoff §4）**：
- 沙箱 shim 会挡 `cp -R` 批量复制：重组装测试包前先
  `cp -R dist/Arca.app/Contents/Resources/python build/python-runtime`，
  再 `ARCA_PYTHON=$PWD/build/python-runtime ARCA_OUT=$PWD/build/Arca-test.app
  build/venv/bin/python -S -E scripts/bundle.py`；
- bundle.py 不清旧包：先同盘 `mv` 走旧 `release/Arca.app` 为隐藏名；
- 跨卷 mv 会 EXDEV；批量删除用 `scripts/package.py` 的 `_rmtree_batched()`；
- 打包：`build/venv/bin/python scripts/package.py`（读 release/，产物在 dist/）；
- 验证完 DMG 必须 `hdiutil detach`（不能只 diskutil unmount）。

## 8. 收尾与交付（全部必做项通过后）

1. 更新 `WORKLOG.md`：追加「改进轮（2026-10-22）」执行记录 + 新「验收清单」，
   每项含改动文件、门禁输出原文、（如适用）产物 SHA256；
2. C1-C4 全过后重新 bundle + package，确认 `dist/Arca-1.0.0.dmg/.pkg` 时间戳更新；
3. 挂载新 DMG 抽查（`diskutil image attach --mountPoint ... --mountOptions
   nobrowse`）后 `hdiutil detach`，确认 `hdiutil info` 无残留记录；
4. 验收方将逐项复核：语法/设计审计/82 项 UI/完整性测试/缓存与预检行为探针/
   代码抽查/产物时间戳与 SHA256 一致性。
