# 取消 / 回滚 / 清理归属契约（T00）

状态：R2 定稿（2026-10-29），供 T01 / T04a / T06 依赖。实现状态见文末
「落地情况」；本文件定义的是**契约**，代码尚未做到的部分不得在 UI 文案或
README 里宣称已实现。

## 1. 任务状态与允许操作

| 状态 | 含义 | 允许操作 | 持久化 |
| --- | --- | --- | --- |
| `queued` | 已提交，尚未被 worker 取走 | 取消 | 内存 + journal |
| `running` | worker 正在执行 | 取消 | 内存 + journal |
| `cancelled` | 用户取消且已收尾（含子进程回收） | 重试、移除 | 内存；进 history 仅 `done` |
| `error` | 任务失败且未取消 | 重试、移除 | 同上 |
| `done` | 成功完成并写入 history | 移除 | 内存 + history |
| `interrupted` | 上次进程非正常退出遗留的任务记录 | 只读展示（不可重试，见 T10） | history（启动时写入） |

- `cancelling` 是 `running` 的**瞬时阶段**（`phase="正在取消…"`），不是独立
  status：避免 UI、history 和 journal 三处状态机分叉。若将来需要独立
  status，必须先统一这三处。
- 取消是幂等的；对已终态任务返回 `False`，不报错、不改变产物。
- 冷启动时 `pending-jobs/*.json` 只用于生成 `interrupted` 记录，**绝不自动
  重放**。重试只能由用户主动点击触发，且只在本进程 `ctx.requests` 仍持有
  原始参数时可用（应用重启后该参数丢失，UI 提示“重试信息已失效”）。

## 2. 取消发生在什么时候，产物归谁

1. **未开始（queued）**：取消后任务直接标记 `cancelled`，不产生任何文件，
   journal 删除。
2. **进行中（running，切换点之前）**：取消只停止继续写入。压缩会清理
   **本任务刚创建、可判定为未完成的归档/分卷**（mtime 不早于任务开始且
   匹配 planned 输出名；半截 7z/zip 无法打开，留着只会误导）；解压**保留**
   已解出的文件；两者都不得动既有文件、其他任务或其他实例的产物。用户在
   任务页可对 cancelled 任务主动重试。
3. **切换前 / 切换中 / 切换后**（T06 引入可恢复替换后）：
   - 切换前取消：新集合丢弃（或保留为临时集合等待清理），**旧集合不得
     移动/删除**；
   - 切换中崩溃/取消：由 manifest + backup 恢复旧集合（T06）；
   - 切换成功后：新集合已是正式输出，取消无意义，旧集合的清理按 T06 契约
     在提交点执行。
4. 任何情况下，取消线程只负责 **terminate/wait/kill/reap 子进程**，不直接
   删除产物；产物清理由任务所有者线程在收尾时按归属判断。

## 3. fresh / merge 与既有文件边界

- `fresh`：目标目录由本任务创建（运行前不存在）。取消/失败后目录里的
  文件可能不完整，保留并提示；重试会合并到该目录。
- `merge`：目标目录原本存在。重试同样以 `onExists="merge"` 执行：**同名
  文件可能被覆盖且不可撤销**，不同名文件与既有文件不受影响。UI 的取消/
  重试文案必须说明这一点（R1 T02 已实现）。
- 其他任务、其他实例产物：不属于本任务的清理范围；即使同目录也不删除。
- 重试请求服务端强制 `onExists="merge"`，前端不提供“覆盖并删除旧目录”的
  隐式动作。

## 4. job → 子进程注册与收尾

- 每个引擎子进程在 `Popen` 后注册到 `src/arca/procs.py`（按 worker 线程
  绑定的 job id）。注册的只是**直接子进程**。
- 取消/退出由取消调用线程（HTTP 线程或信号处理后的主线程）执行：
  `terminate → 有界 wait（默认 3s）→ kill → 有界 wait → reap`；随后阻塞中
  的 `read()` 会因 EOF/异常返回，任务线程得以展开收尾并关闭管道。
- 管道（stdin/stdout/stderr）由**任务所有者线程**在 `finally` 关闭，取消
  线程不跨线程 close，避免 double-close 与竞态。
- 只终止已注册的 PID；**不按进程名、不做进程组广播**。zstd/7zz/par2 都是
  单进程命令，子进程再由系统回收。
- 不自动重跑；不因取消把任务写成 `done`。

## 5. 临时集合、备份、manifest、同卷 rename 与清理责任

- T06 之前：没有通用事务框架。每个任务直接写目标位置；取消/失败保留半
  成品，清理责任见 §2。
- T06 起仅对“同卷归档/分卷 + PAR2”引入：新集合写临时位置 → 同卷
  rename → manifest 标记 → 清理旧集合与旧 PAR2。磁盘需求按“旧集合 + 新
  集合 + PAR2 + 安全余量”计算。
- 备份目录只用于切换窗口期；切换完成且 manifest 提交后由任务线程清理。
- 归属不明（无 journal、无 manifest、路径不在任务 dest/planned 下）→ 一律
  保留并提示，不删除。

## 6. journal、单实例、UI 重试的关系

- journal 是**归属标记**不是回放日志：只记录 kind、路径、planned、format、
  mode、total 等安全字段；不含密码、Keychain 凭据、完整请求体或敏感错误
  文本。写入 best-effort（T03：原子写 + 失败可见降级）。
- 单实例锁（T12-lite）在共享状态初始化前获取；第二实例只提示后退出，不做
  请求转发。不同 HOME 互不阻塞。
- UI 重试只对 `error`/`cancelled` 可见，payload 来自本进程 `ctx.requests`；
  journal 不提供自动重试入口。

## 7. PAR2 默认 best-effort（已由用户批准）

- 归档成功即成功；PAR2 生成/校验失败只写 `result.par2Error` 并给 warning，
  **不把成功归档自动改判为失败**。
- 失败/取消时必须移除或避免旧 PAR2 与新归档混用；取消不得切换旧归档。
- 若将来要改为“全事务（PAR2 失败即整体回滚）”，必须由用户明确批准后再
  实施，并同步修改本文档与验收清单。

## 8. 正常 UI / headless / 崩溃 / 重启

| 场景 | 行为 |
| --- | --- |
| UI 关最后窗口 / Cmd-Q | 走 `applicationShouldTerminate_` → 取消活动任务、有界 drain、回收子进程后退出（R2 落地；无交互询问，见文末差距） |
| headless SIGTERM/SIGINT | 与 UI 一致：调用同一个 `JobManager.shutdown()` 后返回 |
| 取消后正常退出 | cancelled 任务 journal 删除；history 只收 `done` |
| 进程崩溃 | 下次启动把残留 journal 转成 `interrupted` history，保留产物，不重放 |
| 重启后重试 | 原始参数已丢失：提示“重试信息已失效”，只能重新拖入/选择 |

## 落地情况（R2）

- ✅ §1 状态机、§2 取消归属、§3 fresh/merge 文案与重试、§4 进程注册/回收、
  §6 journal 语义、§8 信号与关窗收尾。
- ✅ T01 回归：`scripts/shutdown_test.py` 8 项（queued 立即取消、running
  terminate/kill/reap、忽略 SIGTERM 升级、管道解除阻塞、shutdown 拒绝新任务、
  queued 不执行、幂等、无关进程不受影响）+ `interrupt-recovery` 的运行中
  SIGTERM 场景（有界退出、无孤儿、journal 清理、重启无 interrupted 记录）。
- ⏳ §5 的文件集合替换/备份由 T06 实现；目前仍是“直接写目标 + 压缩清理未完成
  归档、解压保留半成品”。
- ⏳ §6 单实例锁由 T12-lite 实现。
- ⏳ §8 活动任务的“等待/取消并退出/继续工作”交互选择尚未做：R2 当前行为
  是**取消活动任务后退出**（有界 8 秒），且未做 GUI 关闭窗口的人工 E2E。
- T01 的 7zz/par2 静默子进程与取消时延有单元回归；真实 app 的 Cmd-Q 人工
  验收尚未执行。

## 7. 单实例边界（T12-lite，2026-10-29）

同一 App Support 目录（默认 `~/Library/Application Support/Arca`，可用
`ARCA_HOME` 替换主目录）只允许一个 Arca 进程持有 `instance.lock` 上的
`flock` 排他锁；锁在 `Settings`/`JobManager` 初始化与消费历史、journal、
组件缓存之前获取。

- 第二个实例：headless 以退出码 3 结束并在 stderr 说明；GUI 弹提示后同样退出 3。
- 不同 HOME/`ARCA_HOME`：互不阻塞，可并行（例如测试）。
- 不做请求转交、不做跨用户锁；`open -n` 依赖 LaunchServices 与锁两层防护。
- 锁文件为 0600，内容为 `pid=<n> home=<路径>`，退出（含异常退出）由内核释放。

## 8. 中断任务预填重开边界（T10，2026-10-29）

崩溃或强退后，journal（schema 2）中的安全参数会被转存到
`retry-requests/<id>.json` 并在 history 标记 `retryable`；旧 schema 不猜测。
「重试」只预填、不排队：用户必须回到压缩页/解压预览确认后自行提交。

- 允许自动恢复的只有「识别 + 预填」；自动续传、自动重放一律不做。
- 密码永不持久化；加密任务只记录 `passwordRequired` 标记，预填后要求重新输入。
- 源文件/归档已移动或删除：明确报错，不提交；部分输入缺失：移除并警告。
- 部分解压的条目选择不持久化语义，预填时回退为整包并提示重新勾选。
- `retry-requests` 随 history 清理与 40 条窗口修剪，不无界增长。
