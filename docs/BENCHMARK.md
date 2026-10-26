# 性能基准与 B4 判定（2026-10-26）

> 本文件只记录**实际跑过**的数据。没有同条件前后对比，因此不宣称任何性能提升。
> 原始 JSON 写入仓库 `build/`（`build/benchmark-*.json` / `build/benchmark-ui-*.json`）。
> 2026-10-26 那次外部磁盘脱机期间的历史归档保留在 `.tmp/p1p3-logs/`；2026-10-27 返修
> 已在原仓库 `build/` 正常复跑，下方 UI 数据即本轮实际结果。

## 怎么复现

```bash
build/venv/bin/python -S -E -B scripts/benchmark.py --mode quick
build/venv/bin/python -S -E -B scripts/benchmark.py --mode full
build/venv/bin/python -S -E -B scripts/benchmark_ui.py
```

- `quick`：1000 条 ZIP inspect、64 MiB 随机文件 7z 压缩+解压、2000 文件/200 目录扫描、4 路并发 inspect。
- `full`：同样的用例，512 MiB / 20000 文件 / 1000 目录 / 8 路并发。**不是** 1 GB 或 10 万文件的测试。
- 语料全部由固定脚本生成在 `build/`（`build/bench-*`、`build/bench-ui-*`，Chrome profile 在 `build/chrome-bench-*`；UI 基准的 HOME 用系统临时目录），跑完自动删除；不读取用户文件。
- 磁盘不足时脚本明确输出 `SKIPPED` 并退出 0，不会编造数字。
- 峰值内存取 `resource.getrusage(RUSAGE_SELF).ru_maxrss`（macOS 单位为字节），CPU 取 `time.process_time()`。

## 基线（本机，2026-10-26）

- 机器：macOS 27.0.1 / arm64 / 10 CPU / 16 GB / Python 3.13.12
- 引擎：内置 7-Zip 26.05

### quick（`.tmp/p1p3-logs/benchmark-quick-final.json`）

| 用例 | 样本 | 耗时 | CPU | 峰值内存 |
| --- | --- | --- | --- | --- |
| ZIP inspect | 1000 条（返回 800 条，JSON 132,523 B，截断 200） | 0.005 s | 0.005 s | 33.0 MB |
| 7z 压缩 | 64 MiB 随机数据，`-mx1` | 1.921 s（产物 67,113,211 B，接近不可压） | 0.002 s | 100.3 MB |
| 7z 解压 | 64 MiB | 0.142 s | 0.003 s | 100.3 MB |
| 扫描 | 2000 文件 / 200 目录 / 128,000 B | 0.020 s | 0.017 s | 100.3 MB |
| 并发 inspect | 4 × 1000 条 ZIP | 串行 0.027 s / 并行 0.029 s（speedup 0.93） | — | — |

同条件重复跑（2026-10-26 17:06 与 17:22）的耗时在 0.005–0.012 s（inspect）、
1.9–3.3 s（64 MiB 压缩）之间波动，属于页缓存与机器负载差异；本轮只报告测量值，
不宣称任何"变快/变慢"。

注：压缩解的 CPU 时间远小于墙钟时间，说明 7zz 子进程的 CPU 没有计入本进程
`process_time`，这是预期行为；不要把它读成"压缩不耗 CPU"。

**并发结论**：4 个 1000 条 ZIP inspect 的串行与 4 线程几乎相同（0.93×）。这些小
归档的 inspect 是毫秒级 CPU/IO 混合任务，线程池收益被调度开销抵消；这次数据**不**
支持"并发 inspect 更快"的结论，也不作为 B1 并行任务队列的证据（那需要真实压缩/解压
任务与不同输出目录，未在本轮范围内）。

### UI 列表渲染（`build/benchmark-ui-20261027-001830.json`）

用真实 headless Chrome 加载当前源码构建的页面，打开 1000 条 ZIP（后端返回 800 条）。
下表是本轮实际复跑的结果（单次）：

| 阶段 | 结果 |
| --- | --- |
| 初始 200 行（含 `/api/inspect` 往返） | 45.9 ms |
| 点「显示全部」→ 800 行（layout + 两帧后的 paint） | 111.7 ms，6,400 个节点 |

低于 300 ms 交互预算，B4 判定不变。之前的 155.7 / 245.7 ms 是更早轮次的数据，
不再作为本轮结论引用。

### B4 判定（不改造，保留现状）

现有实现首屏固定渲染 200 行；800 行只在用户主动点「显示全部」时一次性渲染。
本轮实测 111.7 ms，低于仓库交互反馈预算（DESIGN.md §4：交互 ≤ 300 ms），且不发生在
默认浏览路径上。**因此本轮不引入分页/虚拟滚动**：收益未被数据证明，而改造会直接
触碰 A1 的选中状态、焦点保持与现有 `extract_list_test`/`menu_test` 断言。

后续触发条件（满足任一条再重做基准并考虑改造）：

1. 同机同命令 `expand_to_800` 的 `painted_ms` > 300 ms 连续两次；
2. 后端列表上限从 800 提高（例如 2,000+ 条），或首屏行数提高到 400+；
3. 真机/低配机走查出现可感知滚动卡顿，或 A1 的勾选操作在展开后出现掉帧。

---

## 环境事故说明（2026-10-26 17:10 前后）

执行本文件与最终门禁时，仓库 `build`（tracked symlink → `/Volumes/My PSSD/WorkBuddyCache/workspace-2026-10-21-build`）
所在外置 SSD 脱机且 `diskutil mount` 失败。`make test` 因此无法在原路径运行；
本轮在 `/tmp/arca-p1p3-run` 的**源码同哈希副本**（rsync 当前工作树，单独建 `build/`）
中完成两轮 24/24 门禁与基准，原始日志/JSON 已复制回 `.tmp/p1p3-logs/`。仓库 `build`
symlink 本身未被修改。外置盘恢复后建议在原路径复跑一次 `make test` 以消除副本变量。

## 未运行的基准

- `full` 档（512 MiB / 20,000 文件）：**本轮未运行**（quick 基线已足以回答 B4；
  full 需要约 2.5 GB 临时空间，留给需要大数据集的后续轮次）。
- 30–60 分钟 soak：**未运行**。`scripts/fuzz_soak_test.py` 只做 3 轮、约 0.1 s 的
  有界冒烟，并在输出里打印实际轮数与时长。
- 覆盖率工具：本机未安装、本轮未运行；不提供百分比。若需复现，可自行安装
  `coverage` 后运行 `coverage run -m unittest discover scripts`，但这会引入开发期
  依赖，仓库默认不安装。
