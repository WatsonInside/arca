# Arca 第五轮改进任务验收报告（A4 应用自更新 + D2 Touch ID）

验收通过（D2 的 touchid 模式存在 A3 签名依赖，见 §4，非代码缺陷）

- 验收时间：2026-10-23 06:40 前后（GMT+8）
- 验收依据：docs/improve-handoff-round5-2026-10-23.md
- 验收方式：all_test 独立复跑 + 源码抽查 + 实弹探针 + 产物核验，未修改任何项目代码

## 1. 完成判定与门禁
- WORKLOG（06:17 收笔）含「第五轮验收清单」；dist 05:57 重新打包（晚于 04:29 下发）。
- `scripts/all_test.py` 独立复跑退出码 0，**18 个套件全过**（build/accept5-all-test.log）：
  keyring 53、appupdate 26、menu 82/0、acceptance 22/0、integrity 78/0、selftest 38/0，
  update 25/0、update-long 27/0 等，零回退。测试进程无残留。

## 2. A4 应用自更新（源码抽查 + 实弹探针 6/6）
源码确认红线：
- 更新源仅来自 settings `appUpdateRepo`，资产 URL 由 `asset_url(repo, tag, name)` 派生，
  **不接受请求传入的 URL**；sidecar 的 browser_download_url 还要与派生值逐字相等（appupdate.py:101）。
- 摘要 `sha256:<64hex>` 校验 fail closed（:110「缺少可信 SHA256 摘要，已放弃更新」）；
  解包有 abs/`..`/反斜杠/软链检查与大小上限；`verify_signature` 为**返回 True 的明确槽位**（:115，注释指向 A3）。
- 替换序列：路径非软链且 canonical 校验 → 新旧版本一致校验 → 新包版本+签名槽位自校验 →
  `os.replace(current, .Arca-old-*)` → `os.replace(new, current)` → 写恢复标记（含 dev/ino）→
  失败自动回滚替换，回滚失败给出人工恢复路径；**不自动重启**（提示「重启应用生效」）。
  `startup()` 有恢复标记的清理/报警分支。

实弹探针（build/accept5_a4_probe.py）6/6：
- 未配置仓库 → `enabled=false`；非法仓库格式被拒；合法仓库（WatsonInside/arca）写入成功；
- 真实检查走通 api.github.com 并**优雅返回**（该仓库暂无 release，错误文案「应用更新仅在 Arca.app 中可用」——
  测试包名非 Arca.app 触发保护性拒绝，属预期）；
- 未检查直接 apply 被拒（版本/缓存一致性校验）；仓库配置可清空回关闭。

## 3. D2 Touch ID 保护（源码抽查 + 探针）
- `keyring._protected_remember`：`SecAccessControlCreateWithFlags(WhenUnlockedThisDeviceOnly,
  kSecAccessControlUserPresence)` → SecItemAdd/Update 带 ACL（keyring.py:40-56）；
  `lookup(auth="touchid")` 走 SecItemCopyMatching（系统自动弹认证），item 不存在才回退 CLI；
  **off 模式原 CLI 路径逐字未变**。
- 探针 6/8 通过：默认 off、可切 touchid、**切换不迁移旧索引**（saved-passwords.json 哈希不变）、
  非法模式忽略、探针条目清理干净（security rc=44）、恢复 off。
- 2 项失败为**同一根因且与对方披露一致**：真实 ACL 写入返回 `-34018`（errSecMissingEntitlement）。
  对照实验：同一 release 运行时**无 ACL** 写入返回 0 —— 证明框架与 API 链正常，
  `-34018` 由「未签名、无 keychain-access-groups entitlement」导致，**属 A3 依赖**：
  未签名的构建里 touchid 模式无法真正生效（remember 会失败并静默降级）。
  我方独立复现了 kimi 申报的同一现象，其「不把 mock ACL 写成通过」态度属实。

## 4. 关键依赖结论
**D2 的功能可用性依赖 A3**：钥匙串 ACL 条目必须由已签名（带 entitlement）的应用写入。
当前形态下 D2 只在签名后才有实际保护效果；建议把这条写进 A3 任务书的前置说明，
并在 A3 完成后补一次真人 Touch ID 端到端验收（写入→读取弹框→成功后解压）。

## 5. 产物检查
- 7 个 SHA 与 WORKLOG 逐字一致：dmg `e15f9b38…`、pkg `fde17daa…`、
  tar.xz `2b0310d5…`、sidecar `c6a33308…`（sidecar 内容与实际 tar.xz 摘要相等）、
  release-notes `07cf3ee5…`、bundle.py `dd029409…`、package.py `0ec07846…`（未改）。
- **bundle.py diff 恰为授权的两行**（SITE_WHITELIST 加 Security/LocalAuthentication + smoke import），无越界。
- release 与 DMG 的嵌入运行时均 `import Security, LocalAuthentication` 成功；
  DMG 内 app.js 与 release 逐字节一致；tar.xz 解包得 Arca.app v1.0.0（app.js 同一哈希）。
- DMG 挂载抽查后 `hdiutil detach` 退出 0，无残留挂载记录。旧产物隐藏备份齐全。

## 6. 需要记录的三项小问题（不阻塞验收，建议下轮修）
1. **测试包未重建**：`build/Arca-test.app` 的 site-packages 缺 Security/LocalAuthentication
   （release 与 DMG 正常）。后果是 all_test 里所有 UI 套件跑的是「无 D2 框架」的测试包，
   D2 相关路径只能靠 DMG 人工链覆盖。下轮收尾时应重新 bundle 测试包。
2. **A4 错误返回的卫生问题**：非法仓库格式返回 HTTP 500 且文案带异常类名
   （`ValueError: GitHub 仓库格式应为 owner/repo`）；apply 前置校验失败同样 500 +
   `UpdateError:` 前缀。应改为 400 且去掉类名前缀（与 I4 的 friendly_error 约定一致）。
3. **appUpdateRepo 默认值为空**：仓库 `WatsonInside/arca` 已就绪，建议发布首个 release 后
   把默认值定为该仓库（或留空并在设置页给出占位提示），否则开箱功能是关闭的。

## 7. 结论
A4 完整实现且探针通过；D2 实现正确、off 模式零变化、无静默迁移，touchid 模式受 A3 签名依赖
（已独立复现 -34018 并证明根因）；门禁 18/18、产物与记录一致、bundle.py 改动未越界。
**验收通过**，D2 的端到端功能待 A3 后复验。

---

## 附录：验收后维护操作（2026-10-23 06:50，验收方执行）

1. **`appUpdateRepo` 默认值改为 `WatsonInside/arca`**（settings.py:23），并实测：
   隔离 HOME 启动新装 → 默认值生效；本机既有 settings.json 仍为空（既有配置覆盖默认，
   已通过 API 将本机设置同步为该仓库）。
2. **重新构建产物**（bundle + package + publish），新 SHA256：
   dmg `ac112612…f0978c`、pkg `8db52546…b32420`、tar.xz `f4739d7b…b98e09`（sidecar 自洽）。
   新 DMG 内 app 通过全部 22 项验收探针；运行时含 Security/LocalAuthentication。
3. **GitHub 仓库上线**：`github.com/WatsonInside/arca`（Public，Apache-2.0 已识别）。
   提交历史 5 个语义提交 + tag `v1.0.0`；基线中的 .tmp/dist/release 垃圾已用
   git-filter-repo 从历史移除（pack 264.6MB → 11.1MB），工作区文件未受影响。
   Release `v1.0.0` 已发布，四个资产摘要与本地逐字一致。
4. **A4 真实端到端**：以 release app 检查线上 Release → `current=1.0.0, latest=1.0.0,
   available=false, error=None`（正确判定已是最新）。
   环境观察（非产品缺陷）：本沙箱内应用进程默认拿不到可用出网代理，检查会以
   「The read operation timed out」优雅失败；显式注入 HTTPS_PROXY 后立即成功。
   该项错误文案可再友好化（原始 urllib 文案直接透出），建议下轮顺手处理。
