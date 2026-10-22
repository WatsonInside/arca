# Arca 第五轮改进任务交接（2026-10-23）

> 交接对象：kimi code / GPT-6.1-sol。验收方：Arca 交接验收监视。
> 本轮范围（用户已拍板）：**A4 应用自更新 + D2 Touch ID 密码保护**。
> **A3 签名公证本轮不做**（等人工证书），但 A4 的验证策略必须预留签名槽位。
> 背景文档：`docs/handoff-2026-10-22.md`（§4 构建坑）、`docs/DESIGN.md`、
> `docs/ROADMAP.md`、`docs/acceptance-report-round3.md`（A6/keychain 现状）、
> `WORKLOG.md`。**动手前必读 DESIGN.md 与 handoff §4。**

---

## 0. 范围总览

| 编号 | 项目 | 要求 |
|---|---|---|
| A4 | 应用自更新（GitHub Releases） | 必做，见 §1 |
| D2 | Keychain 密码 Touch ID 保护 | 必做，见 §2 |
| A3 | 签名公证 | **本轮不做**（A4 预留槽位即可） |
| 其他 | A1/A2/A5、D1 后续、许可证文件 | 本轮不做 |

执行顺序：**D2 先做**（改动集中、可独立验证），**A4 后做**（涉及打包与发布流程）。
逐项门禁不变：每完成一项立即跑 §5 门禁，通过后才开下一项。

**本轮授权例外**：允许修改 `scripts/bundle.py`——但仅限 §2.3 的框架白名单扩展，
其余打包逻辑（目录结构、签名槽位注释、pyobjc 校验行除外）一律不动；
`scripts/package.py` 仍然不许改。

---

## 1. A4 · 应用自更新（GitHub Releases）

### 设计原则
复用引擎更新（updates.py）已验证的模式：**feed → 摘要校验 → 流式下载 → 安装 → 可回滚**，
URL 永不从请求体来（镜像 `_engine_apply` 的防伪造设计），摘要 fail closed。

### 方案
1. **新文件 `src/arca/appupdate.py`**（标准库 + 现有 util）：
   - `check(force=False)`：请求 GitHub Releases API（repo 地址来自 settings 新键
     `appUpdateRepo`，默认值写入 `settings.DEFAULTS` 并在设置页可编辑；为空 = 功能关闭）；
     取 `latest` release，比较版本（复用 `updates.parse_version/is_newer`，
     CFBundleShortVersionString 从 Info.plist 读）；
   - 资产约定：release 内必须有 `Arca-<版本>-mac.tar.xz` 与对应 `.sha256`
     sidecar（任务书附发布脚本建议，见 §1.3）；
   - `fetch_digest`：优先 sidecar，回退 GitHub API asset 的 `sha256:<64hex>` digest
     （updates.py:206 的既有模式）；
   - `Installer` 类：phase = check → download → verify → install → done，
     线程化 + snapshot，结构与 `updates.Installer` 对齐；
     `verify` 阶段流式 SHA256 比对 + **预留 `verify_signature()` 槽位**
     （A3 落地后在此加 codesign 校验，现在返回 True 并注释说明）；
   - `install` 阶段（关键，运行中自替换）：
     a. 下载的 tar.xz 先解包到 `bin_dir()` 旁的临时目录并**自校验**：
        新包 Info.plist 版本 == 期望版本、MacOS/Arca 可执行、`Contents/MacOS/Arca` codesign 校验槽位；
     b. `os.replace`/rename 当前 `Arca.app` → 同卷隐藏名 `.Arca-old-<时间戳>.app`
        （运行中进程不受 rename 影响）；
     c. rename 新包到原路径 → 写恢复标记文件（含旧包路径）；
     d. 返回"待重启"状态，UI 提示用户重启应用；下次启动时若标记存在且新包
        启动成功（server 起来 + 版本匹配），删除旧包目录；启动失败则提示
        用户手动从恢复标记路径还原。
2. **API**（server.py，镜像引擎更新三件套）：
   - `GET /api/appupdate`：check 结果 + installer snapshot；
   - `POST /api/appupdate/apply`：启动下载安装（repo/URL 只从缓存 check 读）；
   - `POST /api/appupdate/dismiss`：跳过此版本（复用 settings 的 skip 键模式，
     键名 `appSkipVersion`）。
3. **前端**（app.js/index.html）：
   - 设置页「关于/更新」组加「应用更新」行：当前版本、检查按钮、可用时
     显示「新版本 X.Y.Z · 下载并更新」按钮 + 进度（复用引擎更新进度条样式）；
   - 完成后提示「更新已就绪，重启应用生效」（不自动重启，保守）；
   - 遵守 DESIGN.md：不新增提示通道，文案不与引擎更新混淆（明确写「应用」）。
4. **发布脚本建议**（新建 `scripts/publish.py`，仅本地工具，不进 CI）：
   从 dist 产物打 `Arca-<版本>-mac.tar.xz` + `.sha256`，并输出 release notes 模板。
   本轮不要求真实 GitHub 发布，见验收方式。
5. **settings.py**：新增 `appUpdateRepo`（默认空串）、`appSkipVersion`（内部键，
   不进设置 UI 的 ALLOWED 白名单——参考 engineSkipVersion 的处理方式）。

### 测试（新建 `scripts/appupdate_test.py`，≥10 个夹具，全部 mock，不断网依赖）
- 版本比较（同版本不更新、旧→新更新、畸形版本安全处理）；
- 摘要解析（sidecar 优先 / API digest 回退 / 双缺失 fail closed / 大小写与
  Windows 路径 / 错名 / 非 hex）——复用 updates 的夹具风格；
- 安装自校验（版本不符拒绝、可执行缺失拒绝）；
- 替换顺序（mock os.replace：旧包 rename → 新包就位 → 恢复标记写入）；
- 恢复标记的启动成功/失败两条分支；
- repo 为空时 check 直接返回关闭状态；
- apply 的 URL 防伪造（伪造请求 URL 被忽略）。

### 红线
- 更新源仅 `appUpdateRepo` 配置的 GitHub repo，绝不接受请求传入的 URL；
- 摘要校验失败 = 弃用安装包并报「校验不符，已放弃更新」；
- 不自动重启进程；不删除旧包直到新版本确认启动成功；
- **不实现真实网络下载的强制测试**（mock 覆盖即可，真实下载作为可选手动步骤）。

---

## 2. D2 · Keychain 密码 Touch ID 保护

### 现状（第三轮 A6）
`keyring.py` 用 `/usr/bin/security` CLI 存取，条目对 Arca 进程静默可读；
`/usr/bin/security` CLI **不支持** access control，做生物识别必须换框架。

### 方案
1. **依赖**：venv 安装 `pyobjc-framework-Security`、`pyobjc-framework-LocalAuthentication`；
   `bundle.py` 框架白名单（:54）追加 `"Security", "LocalAuthentication"`
   （本轮唯一允许的打包脚本改动）；bundle 后自检行（:263）同步加这两个 import。
2. **keyring.py 扩展**：
   - 新增设置键 `passwordAuth`（`"off" | "touchid"`，默认 `"off"`）；
   - `touchid` 模式下 `SavedPasswords.remember()` 改走 Security.framework：
     `SecItemAdd` 带 `kSecAccessControlUserPresence` +
     `kSecAttrAccessibleWhenUnlockedThisDeviceOnly`（无 Touch ID 设备 userPresence
     自动回退登录密码）；service/account 命名与现有完全一致；
   - `lookup()` 改为 SecItemCopyMatching——**ACL 生效时系统自动弹 Touch ID 框，
     app 侧无需新 UI**；`localizedReason` 无法经 SecItemCopyMatching 定制时
     接受系统默认文案；
   - `off` 模式维持 CLI 路径不动（已有行为零变化）；两种模式共存读取时按
     先框架后 CLI 的顺序查找；
   - 老条目升级：设置页提示「开启后已记住的密码需重新保存才能受保护」，
     开启时不静默迁移；
   - Touch ID 重录/失效（biometryCurrentSet 失配）→ lookup 返回 errSecAuthFailed
     → 上报为「记住的密码未能解锁，请在设置中清除后重新保存」（app.js:989
     既有文案，无需新通道）；
   - 设置页 D2 开关行：说明文案含「无触控 ID 的 Mac 会改为要求登录密码」。
3. **测试（扩展 `scripts/keyring_test.py`，新增 ≥6 个夹具，全部 mock SecItem）**：
   - off 模式 CLI 路径回归不变；
   - touchid 模式写入带 ACL（断言查询参数含 access control 键）；
   - ACL 写入失败降级报错不落库；
   - lookup 认证失败（errSecAuthFailed mock）返回 None 且不清除条目；
   - 双模式共存读取顺序；
   - 设置切换时旧条目不迁移（仅提示）。

### 红线
- 绝不在无用户 opt-in 的情况下改变现有条目的保护级别；
- 不用 `biometryCurrentSet` 之外会静默放行的方式（如 `kNilOptions`）冒充保护；
- 密码不出现在日志/响应/明文的 A6 红线继续有效；
- LocalAuthentication 只用于未来 app-gated 路线预留，本轮实现走 ACL 自动弹窗。

---

## 3. 明确不做 / 不许动

- A3 签名/公证、`verify_signature` 之外的签名逻辑、MAS 相关；
- A1/A2/A5、D1 再扩展、许可证/NOTICE 文件；
- B 系（已验收）调度、渲染、zipfix 守卫、keyring 的 off 模式行为；
- package.py、CI 文件；不 git commit/reset；不触碰 `.workbuddy/`。

## 4. 本轮新增的打包脚本授权边界

仅 `scripts/bundle.py`：白名单两行 + 自检 import 一处。改前记录原 SHA256，
改后在 WORKLOG 记录新旧 SHA256 与 diff 原文。其余任何打包相关改动都算违规。

## 5. 门禁命令（每项做完必跑；收尾 all_test 全量）

```bash
node --check src/arca/web/app.js
build/venv/bin/python -S -E scripts/design_audit.py        # 0 findings
build/venv/bin/python -S -E scripts/all_test.py            # 当前 17 套件
# D2 完成后 keyring 套件应含新增夹具；A4 完成后并入 appupdate 套件（第 18 项）
```
单项开发期子集同第三轮任务书 §6。构建坑同 handoff §4。

## 6. 收尾与交付（全部必做项通过后）

1. 更新 `WORKLOG.md`：「第五轮（2026-10-23）」执行记录 + 新验收清单
   （含 bundle.py 授权改动的 SHA256 前后对照）；
2. 重新 bundle + package：确认嵌入 runtime 含 Security/LocalAuthentication
   （site-packages 列表证据）、记录 dist SHA256；
3. 挂载新 DMG 抽查（GBK/空包/重复解压回归 + **新增：D2 开启后 keychain 条目
   ACL 属性核查（security dump-keychain 或 SecItem 查询参数）、A4 设置行存在
   且 repo 为空时显示关闭**）后 detach 干净；
4. all_test 最终全量输出原文入 WORKLOG。

验收方将复核：all_test 独立复跑（18 套件）、A4 mock 全流程（含 URL 防伪造与
恢复标记两分支）、bundle.py 改动 diff 是否越界、嵌入框架证据、D2 开关行为
（off 模式零变化）、产物 SHA256 一致性、DMG 内 Security 框架存在性。
