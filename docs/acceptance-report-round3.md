# Arca 第三轮改进任务验收报告（A6 + C1–C3）

验收通过（附一项新发现的基线缺陷，见 §7）

- 验收时间：2026-10-23 01:50 前后（GMT+8）
- 验收依据：docs/improve-handoff-round3-2026-10-22.md（A6 + C1-C3，B 系与 A1/A2/A4/A5 明确不做）
- 验收方式：独立复跑 all_test + 源码抽查 + 实弹行为探针 + 产物核验，未修改任何项目代码

## 1. 完成判定
WORKLOG.md（01:38 收笔）含「第三轮验收清单」；dist DMG/pkg 00:56 重新打包（晚于 21:47 任务下发）；
keyring.py / all_test.py / acceptance_probe.py / keyring_test.py / seektest.rar fixture 均存在。

## 2. all_test.py 独立复跑（C1 的核心成果）
`build/venv/bin/python -S -E scripts/all_test.py` 退出码 0，12 个套件全过（build/accept3-all-test.log）：
```text
node PASS · design PASS(0 finding) · selftest 通过 38 失败 0
integrity 通过 78 失败 0 · checksum 13 OK · compress-space 14 OK
inspect-cache 12 OK · keyring 17 OK · menu 82 passed, 0 failed
acceptance 22 passed, 0 failed · update 25/0 · update-long 27/0
12 passed, 0 failed
```
要点：selftest 从上轮的 36/0/1 恢复为 38/0——C2 的 RAR 样本本地 vendor 化生效；
update-long（合成 14 行历史 + 真实滚动）通过——C2 的跨度自适应断言生效；
一条命令完成全部门禁——C1 目标达成。测试进程无残留。

## 3. A6 源码抽查（红线逐条核对）
- `keyring.py`（139 行）：account = sha256(realpath) 前 16 位十六进制（keychain 中无明文路径）；
  `remember` 严格要求 `opt_in is True`；stderr 丢弃、输出不做日志；CLI 缺失/超时静默降级；
  本地索引 saved-passwords.json 只存 account 哈希 + basename，密码永不离 keychain。
- `server.py`：`_password()` 空密码时 `keyring.lookup` 静默补齐（:125-132）；
  handler 级 `_secrets` → 所有 `_json` 响应统一 `keyring.redact`（:121-123）；
  `remember` 仅在 `body.remember is True` 时执行（:685-686、:699-700）；
  `/api/settings` 摘要只含 count + 文件名（:226-228），`clearSavedPasswords` 动作存在（:266）。
- `jobs.py`：仅新增 `_secrets` 槽位与 to_dict/update/progress 三处 redact（8+/6-），
  **单 worker 调度结构未动**——红线遵守。
- 前端：`#x-remember` 勾选框（aria-checked、默认不勾）、提示文案、设置页
  「已记住 N 个加密包的密码（仅保存在 macOS 钥匙串，最多 50 个）」+ 全部清除（app.js:1783、:1909）。

## 4. A6 实弹探针（build/accept3_probe.py，真实 Keychain，12 项）
`12 passed, 0 failed`：
- 验密 + 显式勾选记住 → passwordRemembered=true，响应无密码明文；
- `/usr/bin/security find-generic-password` 实测条目存在且密码一致（account 为路径哈希）；
- /api/settings 摘要 `{'count': 1, 'files': ['secret.zip']}`，无密码无全路径；
- 不带密码解压 → lookup 回退自动解锁，落地内容正确，任务列表已脱敏；
- clearSavedPasswords → keychain 条目删除（rc 44）、摘要归零。
验收后 keychain 无任何 Arca 残留条目（find rc 44），探针自身材料即用即删。

## 5. C2 / C3 抽查
- C2：update_test 断言已按实际跨度自适应（单版本跨度下通过，25/0）；
  长清单滚动用合成 14 行历史 fixture 覆盖（update-long 27/0）；
  seektest.rar + RAR-SOURCE.md 已 vendor，selftest 0 skip（38/0/0）。
- C3：`scripts/acceptance_probe.py` 转正并并入 all_test（22/0，覆盖 GBK/空包/409/
  只读/空间预检/缓存失效）；build/ 下两个旧探针已删除。

## 6. 产物检查
- SHA256 与 WORKLOG 逐字一致：
  ```text
  e21b503acd70aed88cfd0a6e786117fb915890c5f05c11db36618e7c92e87b22  dist/Arca-1.0.0.dmg
  b82d45fc3b6e65d0b3bda6d36be291656d763be55414170772ff83b75c02b06c  dist/Arca-1.0.0.pkg
  ```
- DMG 挂载（build/mnt-r3，nobrowse）：卷内启动器与 release/Arca.app 启动器
  SHA256 一致（d993451a…179c）；DMG 内 app.js 含 x-remember、keyring.py 含
  remember 实现——A6 代码确认进包。`hdiutil detach` 退出 0，无残留挂载记录。
- 打包脚本 bundle.py/package.py SHA256 与基线一致（未改动）；旧包同盘隐藏保留；
  WORKLOG 记录 --full-update 真实联网安装（23.01→26.05 含校验/安装/压缩/恢复）通过。
- 不区分范围的红线复核：git diff --check 通过；`.workbuddy/` 未触碰。

## 7. 新发现的基线缺陷（非本轮回归，建议列入下一轮）
**ZIP 格式 + 非 ASCII 密码创建失败**：7zz macOS 构建对 `-p` 传非 ASCII 参数报
`E_INVALIDARG`（zip 编码器路径特有；7z 格式同密码正常、ASCII 密码正常、与
locale 无关）。产品实测：/api/compress 用「R3中文-pw」创建 zip → 任务失败，
报错仅为「操作失败」，无可行动提示。建议下一轮处理：
1. 引擎侧捕获该错误时给出定向提示「ZIP 格式暂不支持非 ASCII 密码，建议改用 7z」；
   或压缩前预检直接引导选 7z（7z 同时加密文件名，本就更优）；
2. 顺带修复错误信息穿透：`操作失败` 未映射底层原因，与 I4 的友好错误目标不符。

## 8. 结论
A6 三条红线（不静默记忆、密码不出现在日志/响应/明文、降级不阻塞）全部满足，
C1-C3 交付质量高（all_test 一键 12 套件、断网全绿、探针转正），产物与记录一致。
**验收通过。**
