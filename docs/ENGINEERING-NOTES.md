# 工程笔记：Arca 的实现细节与取舍

> 这份文件是 README 的长文版：界面规则、窗口拖动的实现、引擎/组件更新的策略、
> zstd 与 PAR2 的技术选择、完整自测清单、以及已知限制。
> 想快速了解产品，请看 [README](../README.md)；想改代码，从这里开始。

## 界面与交互

三栏式工作区，左侧固定导航（压缩 / 解压 / 任务），右侧内容区，底部为**浮动玻璃操作条**——  
主操作始终在拇指位置可见，不随内容滚动消失。

- **渐进披露**：默认只显示格式、级别、分卷、密码四行；"加密文件名""自定义分卷""保存位置"  
  只在与当前选择相关时出现（例如 ZIP 不支持隐藏文件名，该开关就自动隐藏）。
- **状态而非弹窗**：危险/错误信息以行内提示呈现（例：加密归档在读不到目录时，列表区  
  直接提示"目录已加密 — 输入密码后可查看内容"，而不是弹一个错误框）。
- **深浅色主题**：跟随系统或手动指定，颜色用 `color-mix()` 在语义变量上叠加，  
  切换时所有面板同步过渡；`prefers-reduced-motion` 下保留淡入淡出、去掉位移与缩放。
- **动效有纪律**：按压 120ms / hover 160ms / 菜单 132ms / 面板 300ms，进入用强
  ease-out，位移用 ease-in-out；`⌘1/⌘2/⌘3` 切页刻意**不做**动画——每天几十次的动作，
  动效只会读成卡顿。完整规则见 [`docs/DESIGN.md`](DESIGN.md)。
- **键盘可达**：所有可点元素有 2px 强调色 `:focus-visible` 环；鼠标用户看不到，键盘
  用户一定看得到。小字对比度按 4.5:1 校验（不在浅色背景上放浅灰字）。
- **品牌标记就是应用图标本身**：侧栏左上角显示的是包内那张 `.icns` 的位图，不是另画的
  近似图形——Dock、Finder、DMG 与界面里永远是同一张图，不会各自漂移。
- **压缩 / 解压成对**：共用「带盖的箱子」，只有上方箭头方向相反（进箱 / 出箱）。之前解压用的是
  「托盘 + 下箭头」——那是下载的语义，且和压缩的层叠图形毫无配对关系；换成
  「浅托盘 + 上箭头」又会撞上 macOS 的分享图标，所以加了箱盖做区分。
- **图标一套规则**：24 单位画布、统一 1.8 线宽（简单字形 2.2）、光学包围盒 14–20 单位、
  显示尺寸只有 11/13/14/16/22/30/44 七档，小尺寸由 CSS 补偿线宽——不是给某个尺寸
  复制一个新符号。规则与理由见 [`docs/DESIGN.md`](DESIGN.md)。
- **设置面板落在整数网格**：行高恒为偶数（分隔线用内阴影而不是 1px 边框），
  所以每个开关、下拉、按钮的垂直中心正好压在行中心上，不出现 0.5px 的浮动。
- **设置一屏可见**：5 个分区、默认 10 行；引擎三件套版本合并成一行（原来重复出现两次），
  「高级与排障」「逐项状态」两组折叠收起，版本号落在页脚。低频项一条不删，只是收起来
  ——README 的截图 `05-settings` 是折叠后的默认状态。
- **侧栏 208px，底部成簇**：导航只有 3 项，之前 232px 宽的侧栏有 53%（窗口 982px 高时 64%）
  是空带。现在收窄到 208px，并把「最近任务」（最近 3 条完成/失败，点击跳到任务页并高亮）
  与引擎卡、设置一起组成底部簇——空白落在导航与簇之间，成为间隔而不是「没做完」。
- **字号分三档**：正文 13 / 说明 12 / 元数据 11（展示级只有 15、23）。之前整套 UI 以 12.5
  为基准、说明只有 11，比 macOS 正文（13pt）小一档；「要读才知道后果」的句子（格式差异、
  冗余预估）现在不会再低于 12px。代价是首屏多出约 58px 滚动——因为隐藏的是折叠的「高级选项」，
  主按钮固定在底部，所以没有换回小字号。
- **表单有列宽**：控件封顶（滑块 340、密码框 380），说明紧贴控件 10px 而不是推到行尾——
  一行读作 `标签 | 控件 | 说明`，不会出现「控件 600px、右侧空 300px」的参差。行高统一 57px。
- **空态居中**：解压页未载入时把拖放目标垂直居中，不再把一整屏留白留在下方。
- **点击目标 ≥28px**：格式标签 30px、图标按钮 28px、滑块轨道视觉仍 4px 但命中区 28px。
- **数字排版**：进度百分比、速度、体积、剩余时间全部使用 `tabular-nums`，跳动时不抖动。
- **路径显示**：中间省略（`~/WorkBuddy/…/build/demo`），避免 RTL 省略号把路径顺序打乱。

快捷键：`⌘1` 压缩 · `⌘2` 解压 · `⌘3` 任务 · `⌘O` 选择文件 · `⌘↵` 开始 · `Esc` 关闭面板 / 菜单。
切页快捷键无动画（`setView(..., {instant:true})`），面板与菜单的 `transform-origin` 跟随
触发位置（菜单靠边时整体翻转）。

### 窗口拖动

窗口用了 `NSWindowStyleMaskFullSizeContentView`（内容铺满整块窗口，标题栏透明），  
好处是侧栏和页面标题可以顶到窗口上边缘；代价是 **WKWebView 盖住了标题栏，把鼠标按下  
全部吃掉，窗口就没法拖了**。WebKit 也不认 `-webkit-app-region: drag`——那是 Electron  
的私有约定。

现在的做法是在页面之上放一条原生接收层（`DragBar`，`src/arca/app.py`）：

- 位置由页面报告：覆盖顶部整条（含侧栏上方的空白带，也就是常规标题栏所在的那一条），  
  页面在加载完成、字体就绪、窗口尺寸变化时都会重新上报一次。
- 按下后交给 `-[NSWindow performWindowDragWithEvent:]`，也就是系统标题栏自己用的那段  
  拖拽循环，边缘吸附、拖动时的手型光标、拖出窗口外仍继续跟手都是原生行为。  
  万一 AppKit 拒收（它只信任真实硬件事件），还有一段按屏幕坐标的兜底循环。
- 双击按系统偏好处理（System Settings → 桌面与程序坞 → 双击标题栏：缩放 / 最小化 / 无）。
- **可按下与按钮共存**：页面同时上报这块区域里的控件矩形（外观按钮、品牌区），  
  落在这些矩形里的按下会照常穿透给页面——否则标题栏上的按钮会被这条接收层按死。
- 右键（或 Control-点按）在这条区域里仍然弹出应用自己的菜单，事件会被送回页面重新派发。
- 这块区域同时注册为拖放目标并把会话转交给 `DropShim`，所以往窗口顶部拖文件也照样收。
- **浮层出现时让位**：右键菜单和设置面板会盖在这条区域上，接收层若不退让就会把它们的  
  点击吃掉。页面在有浮层时把这条区域上报成高度 0，浮层关闭后再恢复——菜单组件通过  
  `arca:overlay` 事件通知，不需要两边互相引用。
- **红绿灯按钮不受影响**：`Titled` 样式下的关闭/最小化/缩放按钮在主题框架层，位于内容  
  视图之上，`hitTest:` 到这三个位置仍然命中按钮本身（已用同款样式窗口实测）。

### 引擎更新

应用内置的是 7-Zip 通用二进制（当前 26.05）。7-Zip 一年要发好几个版本，其中不少是压缩包解析器的
安全修复——而解析的对象正是别人发来的文件。所以启动时会静默查一次官网，有新版本就弹一个中文说明，  
由用户自己决定装不装。

```
打开软件 → 后台读取 7-zip.org/download.html + history.txt
         → 有更新：弹「引擎更新」，写清 23.01 → 26.04、发布日期、跨越版本数、安全修复条数
                 + 5 条中文亮点（安全 / 速度 / 格式 / 功能 / 修复各取一条）
                 + 逐版本中文摘要（24.01 – 26.04 全部人工整理）
         → 点「下载并更新」才开始下载，进度、体积、速度都在弹窗里
         → 校验：解包 → 运行新二进制读出真实版本号 → 必须 ≥ 目标版本才启用
         → 装到 ~/Library/Application Support/Arca/engines/7zz-<版本>，当前进程立即切换
```

几个刻意的决定：

- **不碰应用包**。arm64 上原地改写 `.app` 里的二进制会让签名失效（内核直接 SIGKILL），而且下次  
  安装应用就白更新了。所以新引擎装在 Application Support 里，应用启动时优先用它，卸载应用也一并清掉。
- **只提示，不自动装**。检查只读两个公开文本文件；下载必须由用户按「下载并更新」。启动检查、  
  手动检查（设置 → 检查更新…、侧栏右键 → 检查引擎更新…）走同一条链路。
- **中文说清变更**。`updates.NOTES_ZH` 是人工整理的中文条目（带 security/speed/format/feature/fix 分类），  
  亮点按分类各取一条而不是堆五条安全修复。没有中文摘要的版本会明确标注「以上为 7-Zip 官方原文」，  
  不硬翻、不编造。
- **「稍后」不是「跳过」**。稍后记 24 小时（`engineSnoozeUntil`），一天内不再打扰；跳过记版本号  
  （`engineSkipVersion`），这个版本永久不再提示；两者都能在设置里重新打开。
- **镜像回退**。官网现在把 macOS 包放在 GitHub Releases 上，而 github.com 在不少网络里不可达。  
  安装器先试 `www.7-zip.org/a/`，再试下载页给的链接；读超时且零字节就换下一个域名，不会卡死在第一个。
- **不信任下载内容**。只接受 `.tar.xz`，路径穿越条目直接拒绝安装，解包目录在临时目录里，  
  装完清理；二进制必须能跑起来并自报版本号，否则回退内置引擎（`updates.forget()`）。

设置里可以看到当前引擎来源（应用内置 / 已更新）并能一键「恢复内置」；侧栏引擎卡片在有更新时  
点变成琥珀色，标题里写明新版本号。同一套链路也覆盖 zstd 与 PAR2（见下文「组件更新」），  
侧栏右键的「检查组件更新…」会直接落到设置里的组件区。

界面截图见 `assets/shots/`（`01-compress` `02-extract` `03-queue` `04-dark` `05-settings`  
`06-menu-light` `07-menu-dark` `08-update` `09-update-done` `10-zst` `11-par2-menu`  
`12-components` `13-finder` `14-default` `15-extract-select`（A1 选中态）；
01-07、10-15 由 `make shots` 生成，
08-09 由更新套件生成——目录里不再保留没有生成者的历史截图）。

`make shots` 是**确定性**的，前置条件与保证：演示素材由 `seed_demo` 每次全量重建  
（先 rmtree 再生成），文件与**目录条目**的 mtime 一并钉到固定值——tar/7z 会把  
目录作为独立条目记录各自的时间戳，只固定文件时，表格「修改时间」列的目录行  
每轮都变。`._*` AppleDouble 残留同样在生成前后双向清理。隔离 HOME 下连跑两轮  
`make shots`，十二张图 sha256 逐字节一致（已验证）。

### Finder 右键与默认打开方式

macOS 给第三方 app 的右键入口是**服务（NSServices）**，但本机验证发现 macOS 27 的
pbs（服务守护进程）对提供者签名有硬性策略：**只信任 Apple 平台签名与 Developer ID
签名**——对照矩阵里 Terminal（平台签名）与 IINA（Developer ID）的服务都能投递，
而 ad-hoc 签名与本机自签证书签名的 app 一律被静默过滤（菜单都不出现）。付费
Developer ID 账号之外无解，所以 Arca 改走**快速操作（Quick Action）**：

- app 内（设置 → Finder 右键菜单 → 「安装快速操作」）把两个 `.workflow` 写进
  `~/Library/Services`：「用 Arca 压缩」「用 Arca 解压」。workflow 的执行者是系统
  自带的 WorkflowServiceRunner（Apple 签名），不受上述策略影响。
- workflow 收到选中的文件（可多选）后写下一个 10 秒有效的意图标志，再 `open -a Arca`
  把文件交给 app；app 的打开事件读取标志，明确走压缩或解压，而不是靠猜。
- 右键压缩**默认进压缩设置页**：文件已就位，你选好格式、级别、恢复记录再按「开始压缩」；
  想一键直压的在设置里打开「右键后直接开始压缩」。
- 源码运行也能用（安装动作只写用户目录，不碰 app bundle）。

「默认打开方式」做在设置里：列出九种格式各自当前的 handler（真实读取 LaunchServices），
一键「设为默认」把全部九种交给 Arca，可随时「恢复系统默认」。两个细节保证它真的能成：

- `.rar` / `.zst` 没有系统声明的类型，LaunchServices 只给它们**动态标识**（`dyn.age81y65y`），
  而 `LSSetDefaultRoleHandlerForContentType` 对动态标识直接返回 `-50`（paramErr）——
  也就是"设置成功但没生效"。所以 Info.plist 里**导出**了 `com.arca.rar-archive` 与
  `com.arca.zstd-stream` 两个自有类型，扩展名映射到稳定标识后这两项才能被设置。
- 每次读取的是真实 handler，所以设置页的「N/9 项为 Arca」不会骗人；界面里失败会给原因，
  不是只报「部分类型没能切换」。绑定要求 app 在 `/Applications` 这类正式位置
（LaunchServices 的硬性要求）。macOS 没有「默认压缩软件」的注册点，右键快速操作就是
它的正规替代，设置里如实说明。

启动器（`packaging/launcher.c`）为此做过一次重写：**不再 `execv` 内嵌解释器**，改为
`dlopen` libpython 后调 `Py_BytesMain`。`execv` 会把进程镜像换成 `python3.13`（连代码签名
身份一起换掉），即使策略放行，AppKit 也不会为这样的进程打开服务端口；保持进程为
`Contents/MacOS/Arca` 之后，窗口拖动适配还暴露了另一个 WebKit 硬约束：**主线程之外的
`evaluateJavaScript` 会被 WebKit 直接 SIGKILL**（EXC_BREAKPOINT），拖动条的适配探测改为
经由主线程派发。

**右键里找不到「用 Arca 压缩」的检查单**（设置 → Finder 右键菜单里有同款）：

1. 装了没有：设置 → 「安装快速操作」，芯片显示「已安装」。
2. 入口对不对：右键菜单的「快速操作」（或最底部「服务」子菜单），不在第一级。
3. 启用了没有：「系统设置 → 键盘 → 键盘快捷键」列表里勾选这两项（首次）。
4. 刚更新过 app：注销重登（或重启 Finder）刷新菜单；`killall pbs` 也能强制重建。

**给维护者的一条教训**：构建产物里只要存在一个带 `NSServices` 声明的 `Arca.app`
（例如 DMG 的 staging 目录），pbs 就会为它登记服务条目——之后即使正式包删掉了声明，
同名菜单项仍会被 pbs 路由到那个已消失的路径上（表现为请求超时、workflow 被架空）。
`package.py` 现在打包后立即注销并删除 staging 目录；排查此类问题时先
`pbs -dump | grep <bundle-id>` 看条目指向哪个路径。

### zstd：为什么是 `.tar.zst` 而不是「给 7z 加个编码器」

查过两次编解码器清单：`7zz i` 在 **23.01 与 26.04 里都没有 zstd**（26.04 只新增了 RISCV BCJ 与  
少量格式修复），而 `zstd` 也从未被纳入 7z 容器的方法号空间。所以没有"给 7-Zip 挂个 zstd 方法"  
这条路线可选——要么自己做 7z 插件（等于维护一个私有容器扩展，别的工具打不开），  
要么就走通用容器。选后者：

```
.tar.zst = tar 流（归档结构）+ zstd 压缩（字节流）
```

这样做的好处是所有工具都认（`tar --zstd -xf`、bsdtar、zstd CLI、7-Zip 24+ 也能解），  
代价是 zstd 本身不提供加密和分卷——它是纯字节流。界面因此**按能力位隐藏**这些行  
（`formats[].encrypt/split/method/solid` 为 `false` 时对应行直接不渲染），而不是显示出来再静默忽略。

实现上的三个坑，都记在代码注释里：

- **不落临时 tar 文件**：`tarfile.open(fileobj=sink, mode="w|")` 是流式写，边打 tar 边喂给  
  zstd 的 stdin，一个 20 GB 目录也不会先写出一个等体积的中间文件。
- **`--stream-size` 用不了**：它要求与实际输入字节**完全相等**（不等就退出码 11），而 tar 流  
  永远等于成员大小之和加上头部与填充，永远不等。解压后大小改由 tar 索引算出来（预览时已走过一遍）。
- **裸 `.zst` 与 `.tar.zst` 要分开**：只看前 512 字节会把任何 `.zst` 都当 tar。现在校验 tar 头  
  的校验和字段（`148..156` 的八进制值 vs 其余字节求和），对不上就按单流处理，解压出一个同名文件。

级别映射到 `-1 / -3 / -7 / -12 / -19`：界面上的六档速度位置不直接对应 zstd 的数字，  
`-20..-22` 需要 `--ultra` 且收益很小，所以最高档停在 19。

### 恢复能力：为什么是 PAR2，以及它救不了什么

结论先说：**值得配，但要 opt-in、要说清边界**。

- **压缩格式自带的恢复记录不可用**。只有 RAR5 有恢复记录，而它是 WinRAR 的私有特性——  
  macOS 上既不能创建（需要 `rar` 命令行）也不能修复（需要 WinRAR）。7z 和 zip 根本没有。
- **PAR2 是唯一跨工具的"事后追加"标准**。MultiPar、QuickPar、SABnzbd、各种 NAS 工具都认，  
  而且它是对**已完成文件**外加一组 `.par2` 文件，不改变归档本身——这正好符合"压缩完再补一层保险"。
- **比 PAR2 更新的方案都不解决这个问题**。zfec / Leopard-RS 这类 Reed-Solomon 库更快，  
  但没有面向最终用户的消费端；RAR5 的恢复记录不通用。引入它们只会让"我坏了怎么办"  
  变成"得装 Arca 才能修"——那正是要避免的。
- **默认关闭**。恢复记录要占体积，多数压缩场景不需要；开关默认关，3% / 5% / 10% / 15% 可选，  
  打开时直接给出冗余预估（`约 12.4 MB 冗余 + 元数据`）。

两个必须说清的边界：

1. **只保护归档文件本身**。它能修复"文件还在、但有字节被写坏/位翻转"这类损坏；某个分卷**整卷丢失**  
   时，能不能救取决于恢复块够不够（5% 冗余大约只能补回 5% 的数据块）。
2. **块数要压住**。PAR2 默认最多 2000 块，小文件会因此让**索引元数据**远大于冗余百分比  
   （实测 400 KB 文件 `-r5` 写出 349 KB，开销 87%）。现在按「约 1 块 / MiB、下限 200 块」传 `-b`，  
   同样文件降到 ~10%。这也是唯一一处我们偏离 par2cmdline 默认值的地方。

**分卷与单卷**。恢复记录默认按 PAR2 标准切成多个恢复卷（`压缩包.par2` 索引 +  
`压缩包.vol00+1 / vol01+2 / vol03+4 …`，按 2 的幂分布）：坏得少只需第一卷就能修，  
坏得多再叠加更多卷——分批上传/下载（网盘、Usenet、邮件）时可以只取需要的那一卷。  
打开「**单卷模式**」（`-n1`）后全部恢复数据合成一个 `.vol` 文件：目录更干净，  
每卷固定元数据只出一份（总开销略小，实测 300 KB 归档 37.7 KB vs 分卷 55.3 KB），  
代价是修复时必须整卷读取。**适合本机留档或一次性传输；要分批传输就保持默认分卷**。  
开关旁边的「**?**」按钮展开这段场景建议（点击/键盘 Enter 均可，不依赖鼠标悬停）；
开关下方的状态行只报告当前状态（`约 1.2 MB 冗余 + 元数据 · 单卷`）。校验与修复对两种形态
一视同仁（par2 靠索引自动找同目录的卷文件）。

校验与修复走任务队列（要重读整份数据，不能阻塞界面）：归档条目右键 → 「校验恢复记录」/  
「修复损坏的文件」，解压页头部也会显示「恢复记录 N」的标记。修复失败不会动原文件。

### 组件更新：和引擎更新同一条链路

zstd 与 PAR2 都是外部程序，同样会出新版本（par2cmdline 从 0.8.1 到 1.4.0 就修了不少  
边界问题），所以它们复用 7-Zip 引擎更新那一整套：**检查 → 中文说明 → 手动确认 → 安装 → 可回退**。

差别只在安装策略，因为上游形态不同：

| 组件          | 策略        | 过程                                                                     |
| ----------- | --------- | ---------------------------------------------------------------------- |
| 7-Zip       | `archive` | 下载官方预编译 `.tar.xz` → 解包 → 跑起来自报版本 → 装到 `engines/7zz-<版本>`               |
| zstd / par2 | `source`  | 下载 tag 源码 → 本机 `cc` / `c++` 编译成 universal → 跑起来自报版本 → 装到 `components/` |

- **装在哪里**：`~/Library/Application Support/Arca/components/`，应用包不动（原因同引擎更新：  
  arm64 上原地改写会让签名失效）。装好后当前进程立刻切换，设置里可「恢复内置」。
- **为什么源码编译而不是用包**：Homebrew 的 par2 bottle 依赖 `@@HOMEBREW_PREFIX@@/opt/libomp`，  
  换台机器就报 `___kmpc_barrier` 找不到；而 par2 的 CMake 构建又必须有一份 autotools 生成的  
  `config.h`。所以配方是显式的：编译命令、宏、需要手写的 `config.h` 都在  
  `scripts/build_thirdparty.py` 里，运行时更新用同一份配方（`components.SOURCE_RECIPES`）。
- **universal 的代价**：x86_64 切片需要 `-DZSTD_DISABLE_ASM`（该汇编符号在 universal 下无法解析），  
  这是唯一一处编译参数偏离上游默认。
- **不信任下载内容**：只接受 GitHub tag 源码包，解包路径做穿越检查，编译产物必须能跑并自报  
  版本号，否则不切换。
- **许可证随包走**：zstd 命令行是 GPL-2.0（库部分是 BSD-3-Clause），par2cmdline 是 GPL-2.0，  
  7-Zip 是 LGPL-2.1 + unRAR 限制条款。`vendor/GPL-2.0.txt`、`vendor/zstd-LICENSE.txt`、  
  `vendor/7-Zip-License.txt` 与 `vendor/THIRD-PARTY.md` 一并打进应用包。

### 右键菜单

每个有意义的对象都有自己的菜单，而不是全窗口一套：

| 右键位置         | 菜单内容                                                                                      |
| ------------ | ----------------------------------------------------------------------------------------- |
| Finder（系统服务） | **用 Arca 压缩 · 用 Arca 解压**——选中文件 → 右键 → 服务；首次需在系统设置勾选（见上文）                                 |
| 待压缩文件行       | 开始压缩 · 仅压缩此项 · 在 Finder 中显示 · 拷贝路径 · 从列表移除 · 清空列表                                         |
| 压缩页空白        | 添加文件… · 添加文件夹… · 从剪贴板添加（N 项）· 清空列表 · 检查引擎更新… · 检查组件更新… · 设置…                              |
| 归档条目行        | 拷贝条目名称 · 在 Finder 中显示压缩包 · 拷贝压缩包路径 · 解压到… · 仅校验 · **校验恢复记录 · 修复损坏的文件**（同目录有 `.par2` 时才出现） |
| 任务卡          | 在 Finder 中显示 · 打开归档 / 所在位置 · 拷贝路径 · 再次压缩这些文件 · **校验恢复记录 / 修复**（该任务产出或使用过恢复记录时）· 移除        |
| 侧栏、空白区       | 切页 · 外观（自动 / 浅色 / 深色，带勾选）· 检查引擎更新… · 检查组件更新… · 设置…                                        |

恢复记录相关项**按存在性出现**而不是常驻灰显：没有 `.par2` 的归档不会看到"修复"这种点了必然失败的动作。

设计上守住的几条：

- **自绘而非系统菜单**。系统菜单拿不到应用的圆角、字体与语义色；自绘后深浅主题、  
  危险操作标红、勾选态、快捷键提示都能与界面一致。
- **同一帧弹出**。菜单在 `contextmenu` 事件里同步构建——任何 `await` 都会让菜单晚一帧出现，  
  所以"从剪贴板添加"的项数是提前缓存的，真正的剪贴板读取发生在点击时（拿不到就提示，不会误禁用）。
- **键盘可达**。`↑` `↓` `Home` `End` 移动，`Enter` 执行，`Esc` / 点按外部 / 窗口尺寸变化 /  
  页面滚动都会关闭；右键不抢焦点，输入框里按方向键不受影响。
- **贴边翻转**。靠近右/下边缘时菜单整体翻转并夹在视口内，`transform-origin` 跟着翻转，  
  动画从光标方向展开。
- **文本输入框放行**。密码、名称、重命名等输入框内保留系统菜单——那是唯一能用"拷贝 / 粘贴 /  
  查询"的地方。
- **实色背景**。早期版本用了半透明 + `backdrop-filter`，菜单会透出背后卡片的边缘，视觉上像是  
  下半部分被提亮了；改成实色后深浅两套都是均匀的。

### 部分解压（A1，2026-10-26）

解压页的条目表现在有选择列：默认全选，行内 checkbox 与行焦点 + `Space` 都能切换，
「全选 / 清空选择 / 已选 N 项」在表格页脚；主按钮是「解压选中 N 项」，0 项时禁用。
默认全选时前端**不传 entries**，仍然走原来的整包解压请求，所以既有行为与性能不变。

API（`/api/extract` 的可选 `entries`）规则，服务端在创建任务前校验并返回干净的 400：

- `entries` 必须是字符串数组；**空数组 ≠ 整包解压**（400）。
- 只接受 `/` 分隔；拒绝绝对路径、盘符、空段、`.`、`..`、NUL/控制字符、结尾斜杠、
  通配符 `* ? [ ]`、以 `-`/`@` 开头（7-Zip 会当开关或列表文件）、重复项。
- 名称必须真实存在于归档；校验用 `engine.list_entries()`（完整列表，不受 UI 800 条
  上限影响），不是用显示列表反推。
- 目录条目表示"该目录整棵子树"（7-Zip / WinRAR 自身对目录名的规则）。
- 前端选择模型是**树形三态**：目录行代表整棵子树；勾选/取消目录会联动全部后代，
  子项变动会向上刷新祖先（全选中变勾选、部分选中变半选、全取消变未选）。发送请求时
  只包含「未选中的文件」和「完整选中的最上层目录」；**半选目录绝不作为 entries 下发**，
  否则引擎会把父目录整棵子树重新展开，刚刚取消的子项就会被解压出来。
- API 仍保留"显式目录=subtree"契约；若调用方同时传祖先目录和其后代，服务端在成员
  校验后剪掉被祖先覆盖的冗余后代（不是重复项错误），结果与只传祖先一致。

实现路径：

- 7z / ZIP / 分卷：`engine.build_extract_cmd` 在 7zz 参数末尾追加 `-- name…`；
  RAR（有 WinRAR 命令行时）按 `rar x archive [files…] dest/` 的顺序把条目放在目标目录
  之前。本条只有 fake CLI 的 argv 单元测试，**没有真实 WinRAR 端到端验证**。
- zipfix（GBK/非 UTF-8 ZIP）：选择的解码名通过 `zipfix.plan` 的 raw→decoded 映射过滤，
  不会把显示名直接当 CLI 参数。
- 加密 ZIP：名称可见时先校验成员、任务内用密码解压；错误密码让任务失败并按既有回滚
  规则处理。加密文件名的 7z 在错误密码下列不出目录，会在建任务前返回 400。
- `tar.zst` / 裸 `.zst`：单流格式，明确禁用选择并在接口与界面都说明，不会偷偷整包解压。
- 条目超过 UI 上限（800）时同样禁用选择，避免"全选"只覆盖可见子集。

重试沿用 `_record_retry` 保存的原始请求体，因此 entries 语义自动保留。

**合并目录的回滚（2026-10-26 返修，2026-10-27 补齐异常路径）**：解压任务开始时先对
已存在的目标目录做一次文件/目录清单快照；失败、取消或 engine.run() 抛异常时，fresh
目标目录整体删除；merge 目标只删除快照之后新出现的文件与本次新建的空目录，既有文件、
其他任务产物和无法归属的路径一律保留。普通 7zz 路径与 zstd/zipfix 共用同一个
guarded 回滚；engine.run 在读出/回调异常时会先 terminate/kill + wait 子进程、关闭
stdout，再让 jobs 回滚，避免子进程在回滚后继续写盘。
不做 mtime 盲删，也不对既有目录 rmtree。被 overwrite 模式覆盖的旧文件内容**不保证
恢复**（没有备份），这一点不在界面或文档里夸大。

**元数据/日志硬上限（T04c，2026-10-29）**：列表/预览这类"先读元数据"的路径原来用
`subprocess.run` 无界收集输出，一个伪造的归档可以让进程吃满内存。现在
`engine._run_sync()` 改为流式读取：单次命令输出超过 64 MiB 立即终止子进程并给出
中文错误；7zz `l -slt` 与 zipfile 快速路径在逐条解析前先检查条目数，超过 200,000
条直接拒绝（"归档条目数超过上限"）。上限固定、没有设置项，正常归档（现有 1,000
条目样本）远低于阈值；`engine.list_entries()`（部分解压成员校验用）复用同一路径。
未验证项：裁剪版 7zz 的输出差异、真实 20 万条归档的端到端（单测通过替换常量验证
判定分支），也不把这条当成对压缩比/耗时的防护。

**写入边界与链接策略（T04a，2026-10-29）**：解压前不再信任归档内条目名。
Python 路径（zipfix、zst）用 `util.safe_join()` 拒绝绝对路径、盘符、NUL 与 `..`
段——跳过并在任务结果的 `skipped` 里计数，不静默改名；`util.path_within()` 在每级
写入前对解压目录内**已存在**的每一段做 `lstat`，遇到符号链接即拒绝继续写入，
挡住"目录里预先放好链接把文件引到外面"的做法。ZIP 的 Unix 符号链接条目直接跳过，
tar 的 symlink/hardlink 成员不跟随（原先静默跳过，现在计数），单文件 `.zst` 的目标
若已是符号链接则拒绝写入。
7zz/RAR 路径在任务开始（重试同样生效）调用 `engine.precheck_extract()`：先用
zipfile 快速路径或 `7zz l -slt` 列出条目，路径不安全的归档直接拒绝（"归档包含
不安全路径"）；含符号链接条目的归档也拒绝（"归档包含符号链接……已拒绝自动解压
以避免路径逃逸"）。这是保守契约：7zz 会把绝对链接目标重写进解压目录、把相对链接
写成空文件，行为不一致，无法承诺安全重建，所以宁可拒绝并让用户换隔离工具。
局限（明确未验证）：7zz 自己写盘时是否会临时创建链接、检查之后目录被外部进程
替换的 TOCTOU 竞争、以及真实恶意 RAR 的链接行为都未实测；预检针对整包，部分解压
即使只选中无关条目，也可能因包里别处有链接而被拒绝。fresh/merge 回滚逻辑不变，
仍按 T00 处理失败与取消。回归在 `scripts/extract_safety_test.py`（10 项真实 fixture：
`../`、绝对路径、symlink 条目、既有 symlink 父目录、tar 穿越/链接、7zz 预检、
干净归档、辅助函数），已接入 `scripts/all_test.py`（30 套件）。

**覆盖压缩的可恢复替换（T06，2026-10-29）**：覆盖写不再"先删旧档再压缩"。新集合
写到目标同目录的隐藏 staging 名（`.<原名>.arca-txn-<job>-<rand>`），压缩完成后先用
`7zz t` / `zstd -t` 回读校验，再把旧集合 rename 到备份名、staging rename 成正式名，
最后删备份并生成 PAR2。切换开始前把 manifest 落盘到
`~/Library/Application Support/Arca/archive-txns/<id>.json`（记录 target、staging、
旧文件→备份映射、PAR2 配置、phase）。进程在切换中崩溃时，下次启动 `JobManager`
构造阶段调用 `archtxn.recover_pending()` 幂等前滚：phase=staging 说明压缩没跑完，
丢弃 staging（旧集合从未被碰过）；phase=switching/cleanup 则补齐"旧→备份"和新名
rename，删备份、清 manifest。取消/失败只清理 staging 与 manifest，旧集合原样保留。
PAR2 采用 best-effort：归档切换成功即算成功，PAR2 失败保留新归档、结果带
`par2Error`，旧 PAR2 随旧集合一起删掉，不出现新旧恢复记录混用。
边界与限制：这是**单任务级**事务，不宣称多文件集合"单次原子"——能保证的是任一点
崩溃后可以确定地前滚或丢弃 staging，且不丢旧集合；staging 与目标必须同卷（跨卷
`os.replace` 会失败，按切换失败回滚处理）；旧集合是否被外部进程改动、manifest 被
手工篡改、断电导致目录项未刷盘等情形未实测；回读校验对 7z/zip/rar 走 `7zz t`、
对 tar.zst 走 `zstd -t`，加密档需要密码且只校验可读性，不保证语义等价。
测试钩子：`ARCA_TXN_FAIL_AT=switch-manifest|switch-old|switch-new|switch-cleanup`
配合 `ARCA_TXN_CRASH=1` 在指定切换点让进程直接 `_exit(70)`，用于真实重启恢复验证；
只给测试使用，不是用户设置。回归在 `scripts/overwrite_txn_test.py`（11 项：提交/
中止/取消、切换前旧档保留、staging 阶段恢复、switch-manifest/old/new 三处崩溃前滚、
分卷后缀映射、PAR2 失败 best-effort、回读校验拒绝坏档、重复恢复幂等），已接入
`scripts/all_test.py`（29 套件）。

**同 HOME 单实例锁（T12-lite，2026-10-29）**：`state/instance.lock` 上用
`flock(LOCK_EX|LOCK_NB)` 取进程级排他锁，锁在 `Settings/JobManager` 初始化与
消费共享状态之前获取；文件里写一行 `pid=<n> home=<路径>` 便于诊断。第二个实例
拿到锁失败时：headless 直接 stderr 说明并以退出码 3 结束（测试/脚本可判定）；
GUI 弹 NSAlert 后同样退出 3，提示切到已有窗口或设置 `ARCA_HOME` 使用独立数据
目录。锁随进程退出由 atexit 释放，异常退出由内核自动释放；不同 HOME（或
`ARCA_HOME`）互不阻塞。边界：不做请求转交、不做跨用户锁；`open -n` 仍可能受
LaunchServices 行为影响（锁是最后一道防线）；共用同一目录的两份不同版本 App
也按同一规则拒绝。回归 `scripts/single_instance_test.py`（5 项真实 `.app
--headless`：同 HOME 第二实例退出 3、不同 HOME 并行、退出后释放、锁文件 0600、
并发启动唯一胜者），已接入 `scripts/all_test.py`（30 套件）。

**中断任务预填重开（T10，2026-10-29）**：journal 升到 schema 2，只写入
白名单安全参数（路径/目标/格式/级别/分割/par2 等），**不含密码、不含 remember
标记、不含请求体原文**；只额外记录 `passwordRequired: true/false`。崩溃恢复时
把这些参数落到 `state/retry-requests/<id>.json`，并在 history 记录里标
`result.retryable = true`；旧 schema（v1）不会被猜成可重试。

UI 的「重试」不再直接排队：`POST /api/jobs/retry-plan` 只读返回预填计划，
页面把草稿填好（压缩页或解压预览）后由用户点「开始压缩/开始解压」才提交。
预填前会校验：源文件/归档不存在 → 明确报错；部分输入消失 → 移除并警告；
部分解压的条目选择失效 → 回退为整包并警告；密码任务提示重新输入（密码永不
回读）。`retry-requests` 随 history 清理与 40 条窗口修剪，避免无界增长。

旧的后端 `POST /api/jobs/retry` 仍保留（供既有客户端/套件显式调用），但 UI
不再走它；两者都不会在无人确认时自动重放。回归 `scripts/retry_plan_test.py`
（8 项：白名单、journal v2、恢复 retryable、v1 不重试、缺失源/条目契约），已
接入 `scripts/all_test.py`（32 套件）。

### 压缩草稿生命周期与竞态防护（2026-10-27）

压缩页的草稿（待压缩列表、名称、保存位置）与后端请求是异步的，这里修过三类真实的
时序缺陷，全部由 `scripts/compress_draft_test.py`（真实 headless UI → HTTP → 磁盘）
与 `scripts/jobwait_test.py`（mock 单元）锁住：

- **草稿生命周期**：`/api/compress` 接受后立即清掉**已提交**的那批输入（回到压缩页
  不能再显示旧的「待压缩」）；请求失败则原样保留。`draftVersion` 是页面生命周期的
  代际号：提交时对当前代快照，响应只作用于同代草稿——在途期间的新增输入、新名称、
  新目录不会被旧响应清掉。自动名称/目录始终跟随当前输入重算；用户手动改过的名称
  （`touchedName`）与显式选择的目录（`destTouched`）只在当前草稿内存活，
  清空列表、「再次压缩这些文件」都产生全新草稿，不泄漏上一份的目录。
- **nameRev / destRev ABA**：旧实现把代际标记重置为 0，一个更早提交的快照可能与
  新草稿的手动编辑「相等」，旧响应于是覆盖了新名称/新目录。修复后 `nameRev`/
  `destRev` 只单调递增、永不归零，快照比较天然免疫同值复用（ABA）。
- **preflight 乱序**：`/api/preflight` 的响应按 `preflightSeq` 序号与请求键
  （目录 + 名称 + 格式）双重校验，过期响应（含错误响应）直接丢弃，不再覆盖新草稿的
  输出路径显示；同代内仅切换格式产生的旧响应同样被拒绝。
- **cancelled 双 l**：后端任务终态一直是 `cancelled`（拼写见 `src/arca/jobs.py`），
  前端有三处比较写成了 `canceled`，导致取消的任务卡不折叠、最近任务不显示——
  已统一，`jobwait.py` 的终态识别以双 l 为准。

### 中断恢复：任务日志与启动收尾（2026-10-27）

崩溃/中断后**不做自动续传**。目标是"数据与状态安全 + 状态可见"，实现分三部分：

- **任务日志（journal）**：任务提交时在
  `~/Library/Application Support/Arca/pending-jobs/<job-id>.json` 写一条小记录，
  到达终态（done/error/cancelled）时删除（连 macOS 外置卷自动生成的 `._<id>.json`
  AppleDouble sidecar 一起删）。只记录 id / kind / 标题 / 来源 / 目标 / 计划产物 /
  格式 / 大小 / pid / 创建时间，**不记录密码、请求体或错误文本**——它不是重放日志。
  写失败不阻止任务运行（best-effort）。
- **启动识别**：`JobManager` 构造时消费遗留日志，生成一条 `status="interrupted"` 的
  历史记录（不会出现在 running/queued 里，也绝不标 done）。note 按 fresh/merge
  归属给出"产物可能不完整"的说明；同一 id 已在历史（说明任务正常结束后、删日志前
  崩溃）时直接丢弃日志。每条日志只消费一次，二次启动不会重复产生中断记录。
  若日志里的 pid 仍存活，则保留不动——第二个实例不把另一实例正在跑的任务判成中断。
- **前端呈现**：任务页与侧栏"最近任务"把中断记录显示为「中断 · 应用退出时未完成 ·
  产物可能不完整」，启动时若存在中断记录会 toast 提示；不提供"重试"按钮，因为原始
  请求（含密码）只存在上一进程内存中，重启即丢弃，**不会静默重放**。用户需要时自行
  重新发起。

边界保持不变，且都是"保留 + 说明"而不是"删除半成品"：

- 崩溃时没有进程执行回滚，所以 fresh 目标的半成品、merge 目标的新增半成品都会留在
  磁盘上；应用不删除、不按 mtime 猜测归属、不对既有目录 rmtree。merge 目标里本任务
  之前就存在的文件与其他任务产物不受影响（自动回归锁定）；被 overwrite 覆盖过的旧
  文件内容仍不保证恢复（没有备份，这一点未改变）。
- 只有应用自有状态文件（journal）会被消费/清理；用户可见产物一律保留。

已知限制（不夸大）：

1. journal 是 best-effort：状态目录不可写、或进程在写第一条日志之前就崩溃时，没有
   可识别记录。
2. 极端 PID 复用可能让一条旧日志多保留一轮（只影响提示时机，不影响磁盘内容）；同一
   HOME 下多实例共享 `history.json` 的既有竞态仍在，本轮不扩展为多实例协调。
3. 不实现自动续传、断点续解或恢复队列；"中断"只表示状态与水印，不承诺自动补完。

自动回归：`scripts/interrupt_recovery_test.py`（4 个场景，真实
`build/Arca-test.app --headless` + 每场景独立 HOME/TMPDIR；"仅 App 进程 SIGKILL"与
"整个专用进程组 SIGKILL"分开；只对记录的 PID/进程组发信号，结束后断言进程组与端口
回收）。已接入 `scripts/all_test.py`（R2 起全量 27 套件；T04a 起 28 套件、T06 起 29 套件、T12-lite 起 30 套件、T10 起 31 套件、R5b 起 32 套件，含 `shutdown`、`overwrite-txn`、`single-instance`、`retry-plan` 与 `components`）。

### 取消与退出：子进程注册、有界收尾（R2，2026-10-29）

- 子进程登记：`src/arca/procs.py` 按 worker 线程绑定当前 job id；`engine.run`、
  `zst`（压缩流/解压流/单文件解压）、`par2._run` 在 Popen 后注册、finally 注销。
  取消线程只操作已注册的直接子进程，不按进程名或进程组广播。
- 收尾顺序：terminate → wait 3s → kill → wait → reap。被终止的子进程让阻塞
  `read()` 返回 EOF，任务线程得以展开收尾并关闭管道；取消线程不跨线程 close。
- `JobManager.cancel()`：queued 任务立即标记 cancelled（不执行任务体、不产出
  文件）；running 任务置 `正在取消…` 并在有界时间内回收子进程。压缩取消会
  清理本任务刚创建、可判定为未完成的归档/分卷；解压保留已解出的文件。
- `JobManager.shutdown()`：先拒绝新任务，再取消 queued/running、有界等待
  worker 收尾并清 journal。UI 关最后窗口/Cmd-Q 与 headless SIGTERM/SIGINT
  走同一入口；语义契约见 [`CANCEL-ROLLBACK.md`](CANCEL-ROLLBACK.md)。
- 已知边界：活动任务退出时不做“等待/取消”询问，直接取消并退出（有界 8s）；
  Cmd-Q 人工 E2E 未执行；zstd 的 tar 流取消状态以任务旗标为准，异常文案可能
  是 ReadError 而非“已取消”。

### App 自更新两阶段就绪（T05，2026-10-29）

- 旧逻辑：`Server.start()` 在发现恢复标记后同步向自己的 `/api/settings`
  发一次 GET，HTTP 200 就当作"新版本启动成功"，随即删除旧包。这只证明
  HTTP 服务活着，不证明页面/JS/引擎可用；而且发生在窗口创建之前，超时还
  会拖慢启动。
- 现在：安装阶段仍写 `appupdate-recovery.json`（schema/路径/dev-ino/版本
  校验保持不变）。页面 `init()` 完成后由前端主动 POST `/api/ui-ready`，上报
  四项真实自检：`bridge`（WKWebView 的 `window.webkit.messageHandlers.arca`
  存在）、`dom`（`#engine-card`/`#c-format`/`#set-updnow` 已渲染）、`meta`
  （`/api/meta` 成功返回）、`engine`（引擎版本字段非空）。四项全为真才
  `set()` 事件。
- 主进程在后台线程里等这个事件（默认 20 s），不阻塞 server/window 创建；
  超时按"未就绪"处理：保留旧包和恢复标记，并把收到的 checks 打到 stderr。
  headless 进程没有页面，不会等：直接按未就绪处理，旧包留给下次启动或
  按恢复标记人工处理。
- 幂等：重复/迟到的 ui-ready 只覆盖 checks 并 `set()`；确认线程只启动一次；
  `Updater.startup()` 在标记已删除时直接做一次旧备份清扫并返回，不会重复
  删除或报错。
- 已知边界：ui-ready 是页面自报（有 token 保护，但不防本机恶意进程）；"页面
  存在但 JS 语法错误" 会落到超时分支（保守保留旧包）；超时后标记仍在，下次
  正常启动会再次等待页面确认；真实 Cmd-Q/窗口创建失败场景未做人工 E2E。
- 测试：`scripts/appupdate_test.py` 新增 `ConfirmUpdateTests`（headless 不清理、
  超时保留、就绪确认、无标记不动作、无 updater 不动作），并把
  `test_http_server_cached_apply_end_to_end` 改为真实 POST ui-ready 后等待
  异步清理；套件共 34 项通过。

### 更新链路加固（R5b：T13/T14/T15，2026-10-29）

- **App 自更新磁盘缓存**（`arca/appupdate.py`）：`state/appupdate-cache.json` 按 repo
  隔离保存 release 快照与 ETag/Last-Modified；命中 TTL 直接返回，过期用条件请求，
  304 复用缓存；repo 切换不会误用别的仓库缓存；损坏缓存直接忽略。`apply` 前如果缓存
  过期（600 s）或上次检查已被标 stale，会强制在线重新校验，失败即拒绝安装
  （「更新信息已过期，请联网后重新检查应用更新」）；stale 只用于展示。
- **7-Zip 双源**（`arca/updates.py`）：官网页面仍是主源，解析失败才回退
  `api.github.com` 的 releases/latest；两个来源的下载地址都必须与预期资产名一致，
  且落在 `www.7-zip.org` / `github.com` / `objects.githubusercontent.com` /
  `release-assets.githubusercontent.com` 的 HTTPS 域内；预发布/草稿版本一律排除。
  GitHub 资产没有可信 digest 时无法据此安装（官网来源才有 SHA256 摘要；未引入
  不可验证的“弱校验”）。
- **组件更新**（`arca/components.py`）：`Zstd`/`Par2` 的 remote 现在会转发 `force`；
  GitHub releases 列表过滤 draft/prerelease、校验 tag 格式，网络失败时返回带
  `stale` 的缓存（只展示，安装仍须联网重新确认）；API 响应上限 5 MiB，源码包下载
  上限 64 MiB，Content-Length 与实际读取都检查，截断/超限会删除 `.part` 重来；
  安装二进制改为「临时文件 → chmod/fsync → 同卷 `os.replace`」，失败保留旧版。
  7-Zip 引擎更新安装也补了 fsync 与临时文件清理。
- 新增 `components` 套件 15 项（双源/白名单/缓存/上限/原子安装），`appupdate` 套件
  扩到 41 项（磁盘缓存重启、304、repo 隔离、403/429 stale、安装前强制刷新）。
- 缺口：GitHub API 与官网解析都只在本地 fixture/mocked 响应下验证，未做真实断网/限流
  端到端；`zstd`/`par2` 的“源码编译安装”未在本轮真实编译验证（沿用既有 recipe）；
  自定义 repo（非 owner/repo）仍是信任输入，不做额外签名校验（设计如此：自更新资产
  校验 SHA256，但信任来源是配置的 GitHub 仓库）。

### 性能基准与可靠性工具（P2，2026-10-26）

- `scripts/benchmark.py`：固定合成语料的 quick/full 两档基准，输出原始 JSON（机器、
  参数、字节数、耗时、CPU、峰值内存）。2026-10-27 两档均已实跑（full 为 512 MiB /
  20,000 文件 / 1,000 目录 / 8 路并发，墙钟 63.20 s），原始 JSON 与日志在 `build/`；
  数据与 B4 判定见 [`docs/BENCHMARK.md`](BENCHMARK.md)。
- `scripts/benchmark_ui.py`：真实 headless Chrome 里测 200→800 行的渲染路径。
  2026-10-27 实测主动展开 800 行 111.7 ms（低于 300 ms 交互预算），故 B4 保持现状并
  记录触发条件（更早轮次的 155.7 / 245.7 ms 已不再作为结论引用）。
- `scripts/fuzz_soak_test.py`：默认仍是 3 轮、几秒的有界冒烟（损坏/截断归档、路径穿越、
  超长名、mock 低磁盘、被杀引擎进程、compress→extract 哈希闭环），属于 `make test`
  的短门禁。新增 `--soak-seconds N`（≥30 分钟，低于则需显式 `--allow-short` 调试）
  的长时模式：同样的场景类循环执行，按 `--heartbeat` 秒数输出结构化心跳
  （轮数、各场景计数、失败数、未终结任务、RSS/FD、子进程、临时目录体积），结束时写
  结构化 JSON（实际墙钟、轮数、场景计数、失败、峰值）。长时模式**不**进 `make test`。
- `make verify`：`node --check` + design_audit + 关键快速套件的本地预检；`make test`
  仍是完整可信信号，本仓库没有远端 CI 运行记录。
- 诊断包本轮**未实现**（现有日志不等于诊断包）；`make verify` 也不代表 CI 已通过。

## 能力矩阵

| 能力    | 7Z                                              | ZIP                               | TAR.ZST           | RAR                |
| ----- | ----------------------------------------------- | --------------------------------- | ----------------- | ------------------ |
| 创建    | ✅（lzma2 / lzma / ppmd / bzip2 / deflate / copy） | ✅（deflate / bzip2 / lzma，AES-256） | ✅（zstd 1–19，速度最快） | ⚠️ 需系统装 WinRAR 命令行 |
| 解压    | ✅                                               | ✅                                 | ✅（含裸 `.zst` 单文件）  | ✅（RAR4 / RAR5）     |
| 只解压选中条目 | ✅                                          | ✅                                 | — 单流格式              | ✅（有引擎时）            |
| 分卷    | ✅ `-v`                                          | ✅ `-v`                            | — 流格式不支持          | ✅（有引擎时）            |
| 加密内容  | ✅ AES-256                                       | ✅ AES-256                         | — 流格式不支持          | ✅（有引擎时）            |
| 加密文件名 | ✅ `-mhe=on`                                     | — 格式限制                            | —                 | ✅（有引擎时）            |
| 恢复记录  | ✅ 可选 PAR2                                       | ✅ 可选 PAR2                         | ✅ 可选 PAR2         | ⚠️ 需 WinRAR 的恢复记录  |


「—」不是"还没做"，是格式能力边界：zstd 是纯字节流，没有密码与分卷的概念，界面会把这些行隐藏掉。

RAR 创建为什么要 WinRAR：RAR 是专有格式，只有官方 `rar` 命令行能压缩。应用会自动在
`/usr/local/bin`、`/opt/homebrew/bin`、`/Applications/WinRAR.app` 等位置探测，并**验证
版本输出**（避免误认同名脚本）；找不到时按钮显式变灰并说明原因，而不是执行到一半才失败。
RAR 标签因此不做成 `disabled`，而是 `aria-disabled` + 标签内的「**需配置**」：看起来是不可选
状态，但可聚焦、可点击；点它**不会改变当前格式**，而是直接打开设置、展开「高级与排障」、
把 WinRAR 那行滚入视野并高亮，焦点落在「选择…」上（回车即可选文件）。悬停或聚焦 RAR 时，
格式说明会临时换成它自己那句「解压可用 · 创建需 WinRAR 命令行」——这句原本永远显示不出来，
因为说明槽只跟随"已选中"的格式。选好 `rar` 后标签立即变可用，不用重启。

入口曾经放在格式说明文字之后（`兼容性最好 · 全平台可解　启用 RAR 创建…`），实测发现它会跟着
**当前所选格式**的说明移动，同字号同颜色、间距与"控件→说明"一致，被读成说明的续句——改挂到
RAR 标签本身后，位置漂移与归属错位一起消失。

## 技术架构

```
Arca.app/Contents/
├── MacOS/Arca                 C 启动器（dlopen 内嵌 libpython 并运行 main.py，进程保持 MacOS/Arca）
├── Resources/
│   ├── python/                内嵌 CPython 3.13 运行时（含 pyobjc 框架）
│   └── app/
│       ├── main.py            入口
│       ├── arca/engine.py     7-Zip 封装：命令构造、进度解析、错误文案映射
│       ├── arca/zst.py        .tar.zst 全链路：流式 tar → zstd stdin、从 tar 索引读目录
│       ├── arca/par2.py       PAR2 生成 / 校验 / 修复（含块数控制与输出解析）
│       ├── arca/components.py 组件登记表 + 更新器（archive / source 两种安装策略）
│       ├── arca/jobs.py       双 worker 任务队列（目录分桶：同桶串行、异桶并行；可取消、进度/速度/ETA、历史持久化）
│       ├── arca/server.py     本地 HTTP API（127.0.0.1 随机端口 + X-Arca-Token 鉴权）
│       ├── arca/app.py        NSWindow / WKWebView / 原生面板 / 菜单 / 主题
│       ├── arca/web/          纯前端（无框架：app.js 状态渲染 + menu.js 右键菜单）
│       └── vendor/            7zz 26.05 · zstd 1.5.7 · par2 1.4.0（均为 universal 二进制 + 许可证）
```

几个关键决定：

- **不用 PyInstaller**。它的 bootloader 会重写自身签名，需要 `codesign` 反复重签，
  在受限环境里极不稳定。改为手工组装：保留每个二进制的原始签名，只新增一个由链接器
  ad-hoc 签名的 C 启动器。产物 49 MB / 664 个文件（含三个引擎二进制），零外部依赖。
- **UI 用 WKWebView 而不是原生控件**。压缩工具的价值在信息密度与状态表达（进度、速度、
  分卷列表、密码强度、覆盖策略），Web 层做这些更干净；同时仍保留原生窗口、原生文件面板、
  原生菜单与系统外观联动。
- **本地 HTTP + 随机 token**。前端与后端解耦，顺手获得一个可自动化的接口
  （`scripts/selftest.py` 与截图脚本都直接调它），也让"打开归档"这类异步操作不会阻塞 UI。
- **进度来自 `-bsp1`**。7-Zip 只在开启该开关时输出百分比；速度用 EMA 平滑，
  避免瞬时抖动把 ETA 算飞。
- **可取消的任务**。取消时先置位 `Event` 再终止子进程，并按任务开始时间清理半成品
  （`.tmp` 与残缺分卷），状态记为 `cancelled` 而不是 `error`。

## 自测结果

`scripts/selftest.py` 端到端跑真实进程与真实文件（2026-10-27 实跑）：

```
engine      : 7-Zip 26.05  (vendor/7zz)
rar encoder : RAR 只读（未安装 WinRAR 命令行）

[1] ZIP · AES-256 加密     11 项全通过（含逐字节比对、错误密码报错）
[2] 7Z · 加密文件名         5 项全通过（无密码不可见目录、有密码可列出、校验通过）
[3] 分卷归档（读取）       13 项全通过（ZIP/7Z 分卷识别/完整性/解压/逐字节一致，无 AppleDouble 残留）
[4] 分卷创建                3 项全通过（3 个分卷、以 .001 开头、可重新读取）
[5] 取消任务               ok（状态为 cancelled，半成品被清理）
[6] RAR 读取                2 项全通过（vendored seektest.rar，SHA256 校验后解压）
[7] 边界情况                4 项全通过

通过 39   失败 0   跳过 0
```

`[4]` 早前版本被跳过，是因为构建脚本当时把 `TMPDIR` 指到了被策略拦截的位置；用受控的
临时目录重跑后，7-Zip 写完 `.tmp` 分卷再重命名的完整流程走通，3 个分卷均能重新读取。

RAR 解压样本已本地 vendor 化（`tests/fixtures/seektest.rar`，2026-10-27，C2 项）：
先校验 SHA256 再使用，下载仅作兜底；早期轮次因官方样本 404 / TLS 中断曾跳过该组，
当前不再依赖外网。

右键菜单、侧栏与设置面板共 103 项 UI 断言（`scripts/menu_test.py`，CDP 驱动真实页面、真实鼠标与键盘事件）：

```
通过 103   失败 0            # + 提示通道统一 / pill 不重播 / 键盘连切 / RAR 跳转 / 侧栏最近任务
                             # + A1 嵌套选择/重选（三态目录行、半选不下发、焦点保持）
```

引擎更新另有 25 项 UI 断言（`scripts/update_test.py`）；加 `--install` 会真的下载官方包、校验、
切换引擎、再用新引擎压一次包，全程 37 项：

```
通过 25   失败 0            # 对话框 / 稍后 / 跳过 / 恢复内置
通过 37   失败 0            # 上一轮真实下载 26.04 时
```

断言里的版本号一律取接口回报的 `latest`，不写死；上游刚发的 **26.05**（2026-10-21）因此
自动进入清单（16 行），并补了人工中文摘要。写这一轮时本机代理对 `7-zip.org` 返回
502，`--install` 停在下载阶段（32/37，5 项失败全是下载失败导致），属网络环境问题；
非安装流程 25 项全通过，`08-update.png` 展示的是检查更新对话框（版本号随上游实时变化，由更新套件重新生成）。

`scripts/design_audit.py` 把 `docs/DESIGN.md` 里能机械判定的规则跑一遍（不需要模型、
可进 CI）：动效（`transition: all`、UI 用 ease-in、`scale(0)` 进入、交互反馈超 300ms）、
对比度（正文 7:1 / 次要 5:1 / 提示 4.5:1，深浅两套独立校验）、无障碍（图标按钮必须有
`aria-label`、必须有 `:focus-visible` 与 reduced-motion 块）、数字字体、长文本重排、
裸 z-index。当前 **0 findings**。

`scripts/integrity_test.py` 专测引擎链路与 Finder 集成接口，97 项断言全部走真实二进制与真实文件（2026-10-27 实跑）：

```
[1] 元数据 · 格式能力位 · 组件注册    13 项（ZST 的 encrypt/split/method/solid 必须为 false）
[2] TAR.ZST · 压缩 → 列举 → 解压     14 项（305 KB 包 → 解压后 305 KB 逐字节一致）
[3] ZST · 单文件（无 tar 头）          6 项（识别为 ZST 而非 TAR.ZST）
[4] PAR2 · 校验 → 损坏 → 修复         23 项（干净 → 损坏 1 字节 → 报可修复 → 修复 → 哈希复原；
                                      含单卷模式（-n1）闭环与 API 传参）
[5] Finder 集成                       16 项（服务状态、收件箱判定/排空/去重、默认打开方式 dry-run）
[6] 组件接口                          6 项（未知组件被拒、7zz 的 reset 走引擎重置）
[7] ZST 子进程资源收尾               19 项（正常/取消/注入异常路径下子进程被收尸且管道关闭、
                                      无 ResourceWarning 泄漏、收尾异常不覆盖原始异常）

通过 97   失败 0   跳过 0
```

PAR2 那段不是在验"文件生成了"，而是把闭环走完：`flip_byte()` 把归档中间一个字节取反，
再校验必须报 `damaged` 且 `repairable`，修复后**哈希要回到原始值**，最后再解压一遍确认
内容没被修复过程破坏。

三个引擎在成品包内也复核过（`Contents/Resources/app/vendor/`）：
`zstd 1.5.7 / par2 0.8.1 / 7zz 23.01`，`source` 均为 `bundled`，
并用包内解释器跑通了 `.tar.zst` 压缩 → 解压比对 + PAR2 生成 → 校验。

覆盖：命中区域、光标定位、右下角翻转、`↑↓/Home/End/Enter/Esc`、点击外部与尺寸变化关闭、
单实例、危险项配色、深浅两套主题下的前景/背景亮度、文件行/归档条目/任务卡/侧栏四类上下文、
以及"移除 / 仅压缩此项 / 清空 / 外观切换"这些动作真的改变了界面。

应用层另有窗口探针（`Arca --probe-window`），从**成品 DMG 内**启动并回报：

```json
{"visible": true, "key": true, "width": 1120, "height": 764,
 "title": "Arca", "url": "http://127.0.0.1:62445/index.html?t=…",
 "appearance": "NSAppearanceNameAqua", "dropShim": "DropShim", "dragShim": "DragBar",
 "drop": {"dragOperation": 1, "dropped": true,
          "overlayOnDuringDrag": true, "overlayOffAfterDrop": true,
          "rowsInPage": 2, "names": "拖放测试-A.txt|拖放测试-B.bin",
          "bridge": {"pasteboardRead": ["…/拖放测试-A.txt", "…/拖放测试-B.bin"],
                     "bridge": ["…/拖放测试-A.txt", "…/拖放测试-B.bin"]}},
 "menu": {"open": true, "items": 5, "at": [670, 140], "rect": [670, 140, 204, 156],
          "labels": ["添加文件…", "添加文件夹…", "从剪贴板添加", "清空列表", "设置…"],
          "prevented": true, "bg": "rgb(255, 255, 255)", "radius": "12px",
          "theme": "light", "hit": "card-head"},
 "drag": {"present": true, "rect": [0, 0, 1120, 94], "frame": [0, 670, 1120, 94],
          "holes": [[14, 54, 203, 37], [1056, 30, 30, 34], [1056, 34, 30, 30]],
          "headerCenterHits": true, "bandHits": true,
          "belowHits": true, "belowLeftHits": true, "topmost": true,
          "holesPassThrough": true, "blockedHoles": [],
          "api": "performWindowDragWithEvent:",
          "overlayCollapse": {"band": 94, "withMenu": 0, "after": 94, "ok": true},
          "branches": ["performWindowDragWithEvent:", "context", "double-click"]}}
```

`drag` 那段是用 AppKit 的方式验的，不是"看一眼像不像"：直接拿 **superview 坐标**调
`hitTest:`——页头中心与标题栏带都命中接收层（`headerCenterHits` / `bandHits`），
页头下方与左下角都返回 `nil`（`belowHits` / `belowLeftHits`，即页面照常收到事件），
上报的每个控件矩形都是 `nil`（`holesPassThrough`，否则外观按钮会被按死），
接收层在子视图里排在最上（`topmost`）；再用合成鼠标事件走一遍
`mouseDown:` 的三个分支，确认单击走 `performWindowDragWithEvent:`、Control-点按走右键菜单、
双击走标题栏双击偏好。`overlayCollapse` 是真开一个菜单再读回高度：`94 → 0 → 94`。

加 `--probe-drag` 会多打一行，把鼠标按下投进应用自己的事件队列，看它落到了哪：

```json
{"dragMove": {"clickAt": [720, 724], "height": 94,
              "shimEvents": ["performWindowDragWithEvent:"], "moved": [0, 0]}}
```

`shimEvents` 说明按下确实被接收层拿走并走了系统拖拽分支；`moved` 为 0 是预期的 ——
AppKit 的抓取循环只认硬件鼠标事件，投递进来的事件会让它打印
`Window move completed without beginning` 后原样返回。窗口真的会动，需要一只手去拖。

即窗口真的创建、WKWebView 真的加载并执行了页面脚本（`title` 来自页面内脚本执行结果），
**拖放整条链路**被走了一遍（拖拽剪贴板放两个文件 → 原生接收层返回 Copy → 页面显示遮罩 →
松开后列表多出两行），**右键菜单也在 WebKit 里真实渲染**：事件派发后菜单出现在光标处
（`at` 与 `rect` 左上角一致）、`preventDefault` 生效（`prevented: true`，系统菜单不会同时弹出）、
图标 `<use>` 在 WebKit 下解析出非零尺寸、实色背景与圆角与设计值一致。

### 拖放为什么要原生接管

WKWebView 不会把从 Finder 拖入的本地文件路径交给一个 `http://` 页面 —— DOM 的
`drop` 事件里 `dataTransfer` 是被剥离过的，所以纯前端的"读 `text/uri-list`"
在 Safari/WebKit 内核下永远拿不到路径（在 Chrome 里能，这也是它容易被漏测的原因）。
Arca 因此做了两层：

1. `DropShim`：覆盖在 WKWebView 之上的透明层，注册 file URL 类型，`hitTest:` 返回
   nil 让所有鼠标事件穿透，只接管拖放；拿到 `NSDraggingInfo` 后直接把路径推给页面。
2. 兜底：`dragenter`/`dragover` 时页面通过原生桥问一次"当前拖拽的剪贴板里有什么"，
   原生读 `NSDragPboard` 回答。WebKit 抢走拖拽会话时走这条。

两条路互斥（哪一方接收到拖放，另一方就不会触发），万一都触发，前端按"同批路径
800ms 内只处理一次"去重。

## 从源码重建

```bash
python3 -m venv build/venv && build/venv/bin/pip install pyobjc-framework-Cocoa pyobjc-framework-WebKit
build/venv/bin/python scripts/build_thirdparty.py # 从上游 tag 编译 vendor/zstd 与 vendor/par2（universal）
build/venv/bin/python scripts/bundle.py      # 组装 release/Arca.app（或 ARCA_OUT=... 指定输出）
build/venv/bin/python scripts/package.py     # 生成 dist/Arca-1.0.0.dmg 与 .pkg
build/venv/bin/python scripts/selftest.py    # 端到端自测
build/venv/bin/python scripts/integrity_test.py # zstd / PAR2（含单卷）/ Finder 集成 97 项断言
build/venv/bin/python scripts/menu_test.py   # 右键菜单与设置面板 103 项 UI 断言（需先起 --headless 实例）
build/venv/bin/python scripts/update_test.py # 引擎更新 25 项 UI 断言（加 --install 走真实下载）
build/venv/bin/python scripts/shots.py       # 通过 CDP 驱动真实 UI 截图
build/venv/bin/python scripts/design_audit.py # 设计规则确定性检查（0=干净，2=有违规）
```

`build_thirdparty.py` 是**必需的一步**：`vendor/zstd` 与 `vendor/par2` 不进版本库，
没有它们 `bundle.py` 会直接报错（而不是默默打出一个缺组件的包）。
配方（版本号、源码 URL、编译参数、par2 需要手写的 `config.h`）以数据形式写在脚本里，
运行期更新（`arca/components.py` 的 `SOURCE_RECIPES`）用的是同一份配方。

其他脚本：`assets/make_icon.py`（手写 ICNS + DMG 背景）、`scripts/cdp.py`（无依赖的
Chrome DevTools 协议客户端）。

## 已知限制

1. **未签名公证**：首次打开需右键"打开"，见上文。
2. **RAR 创建**依赖本机 WinRAR 命令行；RAR 解压则开箱可用（7-Zip 内置 Rar/Rar5 解码器）。
3. `dist/Arca.app` 与 `dist/` 下的 `xar.*`、`port*.json` 是构建中间产物（本机沙箱禁止删除文件，
   无法清理）；可用的应用在 `release/Arca.app`，安装优先用 DMG / PKG。
4. **引擎更新的中文摘要覆盖 24.01 – 26.05**（本项目写作时官网最新为 26.05）。更晚的版本会正常
   检测、正常安装，但说明区显示 7-Zip 官方原文并注明「暂无中文摘要」，需要时在 `updates.NOTES_ZH`
   里补一条即可。
5. **引擎更新需要联网**；无网络时启动检查静默失败，手动检查会提示原因（如「无法连接更新服务器」）。
6. **不要原地覆盖 `.app` 里的文件**：arm64 上内核会缓存二进制签名，原地重写会让内嵌
   Python 直接收到 SIGKILL。更新应用请整个目录换位置（本仓库的打包脚本已按此处理）。
7. **`.tar.zst` 不支持加密与分卷**：zstd 是纯字节流格式，没有密码与卷的概念。需要保密请用
   7Z / ZIP，需要分卷请用 7Z / ZIP / RAR。
8. **PAR2 保护的是"归档文件"，不是"任意丢失"**：能修字节级损坏；整卷丢失时取决于恢复块数量
   （5% 冗余大约只覆盖 5% 的数据块）。冗余越大越安全，也越占体积。
9. **组件更新需要本机编译器**（Xcode Command Line Tools 的 `cc` / `c++`）：zstd 与 par2 是在
   本机编译安装的，没装命令行工具时安装阶段会失败并给出提示，内置版本继续可用。
10. **Finder 服务的首次勾选**：「系统设置 → 键盘 → 键盘快捷键 → 服务」里勾选
   「用 Arca 压缩 / 解压」（macOS 对所有服务一视同仁的开关，设置里有直达按钮）；
   app 需要在 `/Applications` 这类正式位置才能被系统绑定为默认打开方式。
11. **macOS 27 的服务签名策略**：pbs 只显示并投递 Apple 平台 / Developer ID 签名 app
   的服务，ad-hoc 与自签一律被过滤（对照实验见「Finder 右键与默认打开方式」）。
   Arca 的右键入口因此由**快速操作 workflow** 承担（系统 runner 执行，不受限），
   app 内一键安装；若未来拿到 Developer ID 证书，可恢复 app bundle 服务。
12. **崩溃/中断不自动续传**：应用退出或崩溃后，重开会在任务页给出一条「中断」记录，
   说明产物可能不完整及其所在位置；半成品与既有文件都保留、不会自动删除，也不提供
   一键重试（原始请求含密码，只存在进程内存里，重启即丢弃）。日志是 best-effort，
   状态目录不可写时可能没有中断记录。详见「中断恢复：任务日志与启动收尾」。

