# Arca 第四轮改进任务验收报告（D1 + B1–B4）

验收通过

- 验收时间：2026-10-23 04:35 前后（GMT+8）
- 验收依据：docs/improve-handoff-round4-2026-10-23.md
- 验收方式：all_test 独立复跑 + 源码抽查 + 行为探针 + 产物核验，未修改任何项目代码

## 1. 完成判定
WORKLOG.md（04:14 收笔）含「第四轮验收清单」，五项全做（D1 → B2 → B3 → B4 → B1，
顺序符合任务书）；dist DMG/pkg 04:00 重新打包；concurrency_test.py 及各新测试文件齐备。

## 2. all_test.py 独立复跑
`build/venv/bin/python -S -E scripts/all_test.py` 退出码 0，**17 个套件全过**
（build/accept4-all-test.log），较第三轮新增 compress-scan(11)、zip-inspect(11)、
extract-list(6)、concurrency(4)、zip-password(8) 五个套件；原有套件计数无回退：
menu 82/0、integrity 78/0、selftest 38/0、keyring 17/17、acceptance 22/0、
update 25/0、update-long 27/0、inspect-cache 12/12、checksum 13/13、
compress-space 14/14。测试进程无残留。

## 3. 行为探针（build/accept4_probe.py，经真实 /api，8 项）
`8 passed, 0 failed`：
- **D1**：非 ASCII 密码 zip 被预检即时拒绝（HTTP 400，文案「ZIP 格式暂不支持含
  中文等非 ASCII 字符的密码，请改用 7z 格式（同时会加密文件名，更安全）」，
  未创建任务）；ASCII 密码 zip 正常创建；非 ASCII 密码 7z 正常创建。
- **B1**：200 文件/20MB 归档×4 提交到 4 个不同 dest，全部 done，实测
  **max_concurrent = 2**（严格双 worker 并行）；同 dest 两任务（merge）严格串行，
  第二个 started ≥ 第一个 finished。
- 初次探针曾误报并行失败：任务过小（50ms 完成）采样粒度不足所致，加大任务体量
  后复测通过——非产品问题。

## 4. 源码抽查
- **B1**：jobs.py 文档字符串「two directory-isolated workers」；Job.bucket 在
  submit 时落定（realpath，:117）；worker 循环只取「桶未 busy」的队首任务
  （:171、:184）——分桶规则与写死 2 worker 符合红线，无配置化。
- **D1**：server.py:559-560 `str.isascii()` 预检返回 `eng.ZIP_PASSWORD_HINT`；
  engine.py:655 定义文案、:663 对 zip+密码+E_INVALIDARG 场景映射定向提示；
  app.js:319 前端同步提示——三层一致。
- **B2**：util.py 新增 `scan_inputs()`（去重/拒绝符号链接/on_progress/cancel），
  server 与任务卡共用，未复制粘贴。
- **B4**：app.js:633 `entries.slice(0, 200)` + :636 「显示全部 N 条」展开按钮。
- **回归守卫**：zipfix.py、keyring.py、updates.py、components.py 本轮未改；
  jobs.py 的 secrets/redact 机制保持；解包执行路径未动（B3 只改列表读取）。

## 5. 产物检查
- SHA256 与 WORKLOG 逐字一致：
  ```text
  add564fe9bb31f688e87edd79803b6a163d6b48de4bb154390d39396fe5bce31  dist/Arca-1.0.0.dmg
  fceb111759032a81aba5cfe569c93bb3f7ffb48ae5c2c78895f335953b14b32d  dist/Arca-1.0.0.pkg
  ```
- DMG 挂载抽查：卷内启动器与 release 启动器 SHA256 一致（d993451a…179c）；
  DMG 内 app.js == release app.js == src app.js（三处同一 SHA），engine.py 含
  ZIP_PASSWORD_HINT、app.js 含 x-more——第四轮代码确认进包。
  `hdiutil detach` 退出 0，无残留挂载记录。
- 打包脚本 bundle.py/package.py SHA256 与基线一致（前 16 位 27fb565f005a4cbc /
  0ec07846201ffe9c）；旧包同盘隐藏保留；WORKLOG 记录 DMG 挂载内四场景 UI
  测试连续两遍 10/0（含 ZIP 中文密码即时提示场景）。
- git diff --check 通过；`.workbuddy/` 未触碰。

## 6. 对方如实申报的边界（与验收无冲突）
目录桶仅保护等值 realpath 目录（不涵盖父子递归/提交后 symlink 变化）；
zip 快速路径不承诺 ZIP64（异常/分卷/加密保守回退 7zz）；扫描 UI 用受控握手
验证，非性能结论；update-long 单行清单滚动为条件 skip（长清单已过）；
未签名未公证（留第五轮）；pkg 未实际安装。

## 7. 结论
D1 三层（预检/映射/前端）一致且三场景实测通过；B1 并行行为实测符合保守设计
（双 worker、同桶串行）；B3/B4 实现到位且无回归；all_test 17 套件一键全绿。
**验收通过。**
