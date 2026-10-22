# Arca 第四轮改进任务交接（2026-10-23）

> 交接对象：kimi code / GPT-6.1-sol。验收方：Arca 交接验收监视。
> 本轮范围（用户已拍板）：**性能项 B1–B4 全做 + 修复第三轮新发现的 zip 非 ASCII 密码缺陷（D1）**。
> A3 签名公证仍留待人工证书；A1/A2/A4/A5 不在本轮。
> 背景文档：`docs/handoff-2026-10-22.md`（§4 构建坑）、`docs/DESIGN.md`、`docs/ROADMAP.md`、
> `docs/acceptance-report-round3.md`（D1 的定位依据）、`WORKLOG.md`。
> **动手前必读 DESIGN.md 与 handoff §4。**

---

## 0. 范围总览

| 编号 | 项目 | 要求 |
|---|---|---|
| D1 | zip 非 ASCII 密码缺陷修复 + 错误穿透 | 必做，见 §1 |
| B1 | 任务队列保守并行 | 必做，见 §2（本轮最大项，最后做） |
| B2 | 压缩扫描阶段进度 | 必做，见 §3 |
| B3 | zip 内存 inspect 主路径 | 必做，见 §4 |
| B4 | 条目表渲染上限 | 必做，见 §5 |
| 其他 | A1/A2/A4/A5、A3 签名、keychain 扩展 | **本轮不做** |

执行顺序建议：D1 → B2 → B3 → B4 → B1（把改动面最大的调度放最后，前四项各自独立可验）。
逐项门禁不变：**每完成一项立即跑 §7 门禁，通过后才准开下一项。**

---

## 1. D1 · zip 非 ASCII 密码缺陷（必做）

### 现象（第三轮验收实测定位）
- 7zz macOS 构建对 `-p<非ASCII密码>` 创建 **zip** 报 `E_INVALIDARG`（rc=2）；
  **7z 格式同密码正常**；ASCII 密码正常；与 locale 无关（LC_ALL 无效）。
- 产品表现：`/api/compress` 用 CJK 密码建 zip → 任务失败，error 仅为「操作失败」，
  无可行动提示（`engine.py` 的错误描述未穿透底层原因）。

### 方案
1. **压缩前预检**（`server._compress`）：`fmt == "zip"` 且密码含非 ASCII 字符时，
   直接拒绝并给出定向文案：「ZIP 格式暂不支持含中文等非 ASCII 字符的密码，
   请改用 7z 格式（同时会加密文件名，更安全）」；
2. **UI 联动**：压缩设置页当格式为 zip 且密码非 ASCII 时实时提示同一文案
   （走现有提示通道，遵守 DESIGN.md「一个控件一个提示通道」）；
3. **错误穿透补漏**：`engine.py` 的 7zz 失败映射里，把 `E_INVALIDARG` 与
   zip+密码场景映射为上述定向提示，替换裸「操作失败」；
4. 加回归夹具：ASCII 密码 zip 正常创建；非 ASCII 密码 zip 被预检拒绝（文案匹配）；
   非 ASCII 密码 7z 正常创建；错误映射单测。

### 红线
不要试图绕过 7zz 限制（如改编码传参、临时文件喂密码）——上游行为不可控，
定向提示 + 引导 7z 是唯一稳妥解。7z/rar/tar.zst 路径不动。

---

## 2. B1 · 任务队列保守并行（必做，最后做）

### 现状
- 单工作线程（`jobs.py` JobManager），大任务排队时其余全部串行。

### 方案（严格保守版）
1. worker 池固定 **2**；
2. **分桶规则**：每个任务取桶键 = `os.path.realpath(输出目录)`（压缩取目标文件
   所在目录，解压取 dest_root，PAR2 取归档所在目录）；**同桶串行、异桶并行**；
   桶键计算在 submit 时完成，入队即定；
3. 线程安全复核（改动前先通读）：
   - JobManager 的队列/历史/bucket 索引全部在锁内操作；
   - `inspect_cache`（server 层）确认 handler 线程安全性，必要时加锁；
   - keyring SavedPasswords 已有锁；settings 读写确认；
   - zipfix / engine 每任务独立子进程，无共享可变状态，不需改。
4. UI：`nav-count` 已支持多 active；任务卡增量更新路径（QNODES/patchJobEl）
   不动，仅需确认多任务并发更新时渲染正确。

### 验收测试（新建 `scripts/concurrency_test.py`）
- 4 个解压任务提交到 4 个不同 dest：观察到 ≥2 个任务同时 running（并行生效）；
- 2 个任务同一 dest：严格串行（第二个的 started 晚于第一个的 finished）；
- 并行中取消其一：其余任务不受影响、半成品回滚正确；
- 全部完成后历史/计数正确。
此测试并入 all_test（第 13 项）。

### 红线
不引入可配置并发数（写死 2）；不做任务优先级；不动取消/回滚语义；
jobs.py 的 redact/secrets 机制（第三轮验收项）保持原样。

---

## 3. B2 · 压缩扫描阶段进度（必做）

- 现状：大目录压缩开头只有 indet 动画，无信息。
- 方案：`make_compress_task` 在开工扫描阶段周期性
  `job.update(item="已扫描 N 个文件 · 约 X GB")`（复用 D1 之前 C2 轮做的
  输入统计逻辑，抽成共享函数，勿复制粘贴）；进度条仍走 indet，不伪造百分比；
  扫描结束进入正式压缩时恢复 7zz 字节进度。
- 验收：大目录（构造 2000+ 小文件）压缩任务卡副标题能看到递增的扫描计数；
  menu_test 全量不回归。

---

## 4. B3 · zip 内存 inspect 主路径（必做）

- 现状：zipfix 已验证纯 Python 读中央目录可靠，但只对 GBK 修正包生效；
  普通 zip 仍起 7zz 子进程。
- 方案：
  1. `engine.inspect` 对 zip 一律先走 Python zipfile 读中央目录（含 UTF-8 标志
     名单、加密位检测、目录条目判定），条目字段与现结构完全一致
     （path/size/packed/modified/isDir/encrypted/crc/method）；
  2. zipfix 的 GBK 解码逻辑保持为该路径上的名字修正步骤（不回退）；
  3. zipfile 打不开（损坏）或加密 zip 需要头信息时回退 7zz——**加密 zip 的
     密码校验路径（verifyPassword）行为不变**；
  4. `solid/method/physical size` 等 meta：zip 无此概念时维持现有默认值，
     不虚构；
  5. 添加对拍测试：同一组 fixture（UTF-8 名、GBK 名、加密、空包、含目录层级）
     zipfile 路径与 7zz 路径的 entries/counts/uncompressed 逐字段一致。
- 收益预期：zip 列表显示不再起子进程（尤其测试包冷启动场景显著提速）。
- 红线：`/api/inspect` 响应结构不变；解压路径（7zz/zipfix 分流）不动——
  本项只改「列表读取」，不改「解包执行」。

---

## 5. B4 · 条目表渲染上限（必做）

- 现状：`app.js:619` 附近条目表一次性 innerHTML 渲染（上限 800 条）。
- 方案：默认渲染前 200 条 + 表尾「显示全部 N 条」按钮（点击再渲染余量，
  仍一次渲染、不做虚拟滚动——800 条一次性可接受，避免过度工程）；
  「显示全部」后 A1 勾选类未来功能不受影响；行结构与测试选择器不变。
- 验收：构造 800 条 zip，列表首屏 200 行 + 按钮可见，点击后 800 行全渲染；
  menu_test 全量不回归。

---

## 6. 明确不做 / 不许动

- 调度并发数固定 2 之外的任何配置化；虚拟滚动；7zz 参数绕路；
- zipfix 接管守卫、modalize 签名、renderQueue 增量结构、keyring 行为
  （均已验收）；A1/A2/A4/A5、A3、MAS 相关；
- 打包脚本；不 git commit/reset；不触碰 `.workbuddy/`。

## 7. 门禁命令（每项做完必跑；全部完成后跑 all_test 全量）

```bash
node --check src/arca/web/app.js
build/venv/bin/python -S -E scripts/design_audit.py        # 0 findings
build/venv/bin/python -S -E scripts/all_test.py            # C1 轮交付：一键全量
# 全量含 menu 82 项、acceptance 探针、keyring 17 项、concurrency 新增项
```
单项开发期可用子集（同第三轮任务书 §6），但收尾必须 all_test 全绿。

构建坑同 handoff §4（ARCA_PYTHON 复制运行时、同盘隐藏名移旧包、
_rmtree_batched、hdiutil detach）。

## 8. 收尾与交付（全部必做项通过后）

1. 更新 `WORKLOG.md`：追加「第四轮（2026-10-23）」执行记录 + 新验收清单，
   每项含改动文件、门禁输出原文（D1 附错误映射前后对比）；
2. 重新 bundle + package，记录 dist 产物 SHA256；
3. 挂载新 DMG 抽查（GBK/空包/重复解压 + **新增：非 ASCII 密码 zip 的
   预检提示场景**）后 detach 干净；
4. `all_test.py` 最终全量输出原文入 WORKLOG。

验收方将复核：all_test 独立复跑（含 concurrency 项）、D1 三场景探针
（ASCII zip 成功 / 非 ASCII zip 定向拒绝 / 非 ASCII 7z 成功）、
B3 对拍测试抽查、并行行为实测（不同 dest 并行、同 dest 串行）、
代码红线（调度写死 2、解包路径未动）、产物 SHA256 一致性。
