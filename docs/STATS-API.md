# Arca 使用统计 · 数据接口（给网站端）

> 本文档面向 event.hushemeng.group 网站的维护方（或其 Agent）：Arca 桌面 App
> 的匿名使用统计由 App 仓库侧聚合为一份 JSON，网站统计页只需 fetch 它渲染。
> 接口自描述、带版本号，字段语义以下面为准，不依赖任何页面代码。

## 1. 数据从哪来（背景，一段话）

Arca App 在「首次启动」和「每日首次启动」时各发一条匿名 GET 请求，路径形如
`/ping/v1/install/1.0.7/mac15-arm64/<32位随机ID>`。请求打到本站的 COS 桶，
由 COS 访问日志记录；App 仓库侧的 `make stats` 拉取日志聚合成 JSON 并上传。
**不含任何个人数据**：随机安装 ID、版本号、系统版本、架构，仅此四项。
用户可在 App 设置里一键关闭（默认开启）。

## 2. 获取方式

- **URL**：`https://event.hushemeng.group/events/api/stats.json`（COS 对象
  `events/api/stats.json`，与统计页 `/events/stats.html` 同目录——页面用相对路径
  `fetch("api/stats.json")`，因此键必须与页面同目录；也可用 COS 直链）。
- **方法**：`GET`，无鉴权（对象为 public-read，与网站页面同策略）。
- **缓存**：响应带 `Cache-Control: public, max-age=300`，Cloudflare 与浏览器
  各自最多缓存 5 分钟；页面无需自己去重。
- **刷新频率**：由 App 侧 `make stats` 手动或定时触发，分钟级刷新无意义，
  页面按「小时级新鲜度」理解即可。
- **同域 fetch**：页面与 JSON 同桶同域时 `fetch('api/stats.json')` 直接可用，
  无 CORS 配置。若后台页部署在其他域名，需在 COS/Cloudflare 上为该 JSON 配
  `Access-Control-Allow-Origin`（目前未配）。

## 3. 数据结构（schema `arca-stats/1`）

```jsonc
{
  "schema": "arca-stats/1",            // 版本标识，渲染前请校验
  "generatedAt": "2026-10-23T23:40:03+08:00",  // 本份数据的生成时刻
  "window": {                          // 数据覆盖范围
    "first": "2026-10-17",             // 最早有记录的日期
    "last": "2026-10-23",              // 最晚有记录的日期
    "timezone": "Asia/Shanghai"        // 所有日期的聚合时区（东八区）
  },
  "totals": {                          // 大数字卡直接用这里
    "installs": 42,                    // 累计安装（去重安装 ID 数）
    "activeToday": 3,                  // 今日活跃（独立设备）
    "activeYesterday": 15,             // 昨日活跃
    "active7d": 60,                    // 近 7 天活跃合计（按天求和，非去重周活）
    "active30d": 90,                   // 近 30 天活跃合计（同上）
    "pings": 4567                      // 历史心跳总条数（含 install）
  },
  "days": [                            // 连续日序列，含零填充天，无空洞
    {
      "date": "2026-10-23",
      "installs": 5,                   // 当日新安装（去重）
      "active": 12,                    // 当日活跃（去重设备数）
      "pings": 30                      // 当日请求条数（≈当日启动次数上界）
    }
  ],                                   // 最多保留最近 400 天
  "versions": [                        // 按安装数降序
    {
      "id": "1.0.7",
      "installs": 80,                  // 在该版本上发生安装事件的次数
      "active30d": 40                  // 近 30 天内最后出现在该版本的设备数
    }
  ],
  "platforms": [                       // 形如 "mac15-arm64"（系统大版本-架构）
    {
      "id": "mac15-arm64",
      "installs": 100,                 // 该平台上的去重设备数
      "active30d": 50
    }
  ]
}
```

### 口径说明（画图前必读）

- **installs 的两处口径不同**：`totals.installs` 与 `platforms[].installs` 是
  **去重设备数**；`versions[].installs` 是**安装事件数**（用户从 1.0.5 升级到
  1.0.6 会被计进两个版本，因为发生了两次 install 事件）。`active30d` 统一为
  「近 30 天内最后出现在该 id 上的去重设备数」。
- **active7d / active30d 是按天求和**，不是周去重（周去重设备数 ≤ 该值）；
  需要严格周活请自行用 `days[].active` 计算（原始数据不足以跨天去重，
  这是匿名化的代价，也是承诺）。
- **days 无空洞**：没有记录的天也出现（数值为 0），图表直接按序画即可。
- **冷启动**：`days` / `versions` / `platforms` 可能为空数组、`window.first`
  可能为空串，页面需要给空态。
- 日期一律 `YYYY-MM-DD`、东八区。

## 4. 建议的页面形态（可选）

参考：顶部 4 张大数字卡（累计安装 / 今日活跃 / 昨日活跃 / 30 天合计），
中间近 30 天 `days[].active` 折线（可叠加 `installs` 柱），下方版本与平台
两个横向条形榜。页面可附一句声明：

> 数据来自匿名使用统计：每台设备每天至多一条记录，仅含版本与系统信息，
> 用户可在 App 设置中随时关闭。

## 5. 部署注意（开放问题）

1. `events/api/stats.json` 需要与网站页面相同的读取策略（public-read）；当前桶根
   返回 AccessDenied，若网站页面走的是签名访问，请告知，App 侧上传策略需要
   同步调整。
2. Cloudflare 已建议对 `/ping/*` 路径设置 Bypass cache（App 上报路径，避免
   边缘缓存吞掉日志记录）；`/events/api/stats.json` 保持默认即可（有 max-age=300）。
3. 上传由 App 仓库的 `make stats` 完成（对象键 `events/api/stats.json`、
   Content-Type 已设 `application/json; charset=utf-8`）；若网站侧希望换键名
   或加签名 URL，改动点只有 App 仓库 `build/cos.json` 的 `apiKey` 字段（注意
   保持与统计页同目录，页面是相对路径 fetch）。
