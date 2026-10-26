# 可达性走查记录（2026-10-26，P3）

> 本文件区分**实际执行**与**未执行**。ARIA/对比度的静态断言不能冒充 VoiceOver 真人走查。

## 已实际执行（自动化，可复现）

| 项目 | 方式 | 结果 |
| --- | --- | --- |
| 颜色对比度（浅色/深色两套） | `scripts/design_audit.py`（正文 7:1 / 次要 5:1 / 提示 4.5:1） | 0 findings |
| 焦点可见环 | `menu_test`：真实 Tab 后检查 `:focus-visible` 与 2px outline | 通过 |
| 键盘菜单/切页/列表导航 | `menu_test`：↑↓ Home End Enter Esc、⌘1/2/3、行 ↑↓ | 通过 |
| 深浅主题 | `menu_test`：浅色/深色菜单前景背景亮度 | 通过 |
| 减弱动效 | `design_audit` 检查 `prefers-reduced-motion` 块存在；未做真人观感走查 | 静态通过 |
| A1 选择性解压 | `menu_test`：选择列、默认全选、取消/全选/清空、主按钮禁用、Space 切换、`aria-selected`、`aria-label`、焦点保持 | 通过（7 项断言） |
| 设置页一屏可见 | `menu_test`：`.sheet-body` 不滚动 | 通过 |

命令：

```bash
build/venv/bin/python -S -E -B scripts/design_audit.py
build/venv/bin/python -S -E -B scripts/menu_test.py --port-file build/port-*.json
make test
```

## 未执行（明确记录，不伪造）

1. **VoiceOver 真人走查：未执行。**
   实测探针（只读）：`pgrep -x VoiceOver` → 未运行；`osascript` 读
   "System Events / UI elements enabled" → `false`（辅助功能 UI 脚本未授权）。
   因此既没有可核验的语音输出，也无法安全驱动 VoiceOver 会话（启用会改变全局
   输入行为）。当前执行环境是非交互式 CLI Agent，不能替代真人听取播报。
   需要人工按下面清单走查：
   - 压缩页：导航、格式分段控件、滑块、密码框、开始按钮；
   - 解压页 + A1：归档条目表的选择列、全选/清空、已选计数、「解压选中 N 项」禁用态；
   - 任务页：运行中/完成/失败卡片、取消、重试；
   - 设置页：折叠组、开关、下拉、关闭；
   - 弹层/错误提示：菜单、更新对话框、行内错误与 toast。
   记录实际朗读文本、焦点顺序与问题；发现问题后补 `aria-*` 或文案。
2. **200% 文本放大：未执行。**
   WKWebView 没有可由 CDP 驱动的"仅文字缩放"开关；用 `zoom`/页面缩放模拟会同时缩放
   布局，不能替代系统文字大小/浏览器文字缩放的真实重排验证。需要人工在系统辅助功能
   或 Safari 文字大小下检查（重点：设置面板一屏、A1 表格列宽、按钮文字不截断）。
3. **系统"增强对比度"：未执行。**
   自动化只校验了应用自己的浅色/深色令牌；系统 `Increase contrast` 下的真实渲染需人工检查。
4. **VoiceOver + 弹层联动：未执行**（同上，依赖 1）。

## 已知可达性风险

- A1 的行选择使用原生 checkbox（`tabindex="-1"`）加行级 `tabindex="0"` + Space 切换。
  读屏浏览模式下 checkbox 可读、可操作；若后续支持表格多选语义，需评估
  `role="grid"`/`aria-multiselectable` 的完整键盘模型，本轮未改。
- 条目超过 800 条时（后端列表截断）选择能力被显式关闭，界面文字说明"当前格式不支持
  部分解压，将解压整包"；这句在 VoiceOver 下仍需真人确认可读。
