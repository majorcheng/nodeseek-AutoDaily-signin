# NodeSeek 自动签到 - GitHub Actions 版

基于 GitHub Actions 的 NodeSeek 论坛自动化工具，无需服务器，Fork 即用。

## ✨ 功能

- ✅ 自动签到 + 领取奖励（支持"试试手气"和"鸡腿 x 5"智能选择）
- 💬 随机评论帖子（3-5 篇，间隔 1-2 分钟）
- 👥 **多账号支持**（Cookie 用 `|` 分隔）
- 🎯 **可配置评论区域**
- ⏰ **随机延迟执行**（0-10 分钟，防止固定时间触发）
- 📱 Telegram 极简科技风通知
- 🔄 失败自动重试 + 智能容错
- 🔐 Cookie 过期 / Cloudflare 风控分型告警
- 🌐 代理优先、直连兜底的自动出口切换
- 💾 Chrome profile 缓存复用，尽量保留 `cf_clearance` 等浏览器状态

## 🚀 快速开始

1. Fork 本仓库
2. 在 `Settings → Secrets and variables → Actions` 中添加配置
3. Actions 将每天自动执行两次（默认使用 xvfb 下的有头 Chrome，而不是 headless）：
   - **北京时间 00:10**（签到 + 评论）
   - **北京时间 12:20**（签到 + 评论）

## 🍪 如何获取 NodeSeek Cookie

1. 打开浏览器，访问 [NodeSeek](https://www.nodeseek.com) 并登录
2. 按 `F12` 打开开发者工具，切换到 **Network（网络）** 标签
3. 刷新页面，在请求列表中点击任意一个请求
4. 在右侧 **Headers（标头）** 中找到 `Cookie` 字段
5. 复制整个 Cookie 值（一长串文本）

**示例：**
```
session=abc123xyz; token=def456uvw; user_id=12345
```

> ⚠️ Cookie 包含登录凭证，请勿泄露！

## ⚙️ 配置说明

### Secrets（敏感信息）

在 `Settings → Secrets and variables → Actions → Secrets` 中添加：

| 变量名 | 必填 | 说明 |
|--------|------|------|
| `NS_COOKIE` | ✅ | NodeSeek Cookie，多账号用 `\|` 分隔 |
| `NS_RANDOM` | ❌ | `true`(默认): 试试手气 / `false`: 鸡腿 x 5 |
| `NS_COMMENT_URL` | ❌ | 评论区域 URL（默认交易区） |
| `NS_PROXY_URL` | ❌ | 浏览器业务流量代理地址，支持 `http://host:port` 或 `https://host:port` |
| `TG_BOT_TOKEN` | ❌ | Telegram Bot Token |
| `TG_CHAT_ID` | ❌ | Telegram Chat ID |

### Variables（非敏感配置）

在 `Settings → Secrets and variables → Actions → Variables` 中添加：

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `NS_DELAY_MIN` | `0` | 随机延迟最小分钟 |
| `NS_DELAY_MAX` | `10` | 随机延迟最大分钟 |
| `NS_PROXY_INSECURE` | `true` | 仅在 `NS_PROXY_URL` 为 `https://...` 时生效；`true` 表示跳过上游代理证书校验 |
| `NS_EGRESS_MODE` | `auto` | 出口模式：`auto`(代理优先，失败再直连) / `proxy` / `direct` |
| `NS_CF_WAIT_SECONDS` | `30` | 首页或帖子命中 Cloudflare 挑战页后的最大等待秒数 |
| `NS_CHROME_PROFILE_DIR` | `.chrome-profile` | Chrome profile 缓存目录；默认配合 Actions cache 复用 |

## 🌐 代理配置说明

- 代理只影响 **NodeSeek 浏览器动作**，也就是签到、评论、Cookie 登录检测等 Selenium 流量。
- 不影响 `Telegram` 通知。
- 不影响 `apt` / `pip` 安装依赖，也不影响 `webdriver_manager` 下载驱动。
- 当前版本只支持 **无认证代理**。

### GitHub Settings 推荐配置

在 `Settings → Secrets and variables → Actions` 中添加：

- `Secrets`
  - `NS_PROXY_URL=https://your-proxy-host:port`
- `Variables`
  - `NS_PROXY_INSECURE=true`（默认即为 `true`；如需校验证书，可显式设为 `false`）
  - `NS_EGRESS_MODE=auto`（推荐，先代理，Cloudflare 挑战失败后再试直连）
  - `NS_CF_WAIT_SECONDS=30`

### 行为说明

- `NS_PROXY_URL` 未配置：`auto` 会直接走直连。
- `NS_EGRESS_MODE=auto`：优先走代理；只有首页阶段命中 `cf_challenge` 或出口初始化失败时，才会切到直连重试。
- `NS_EGRESS_MODE=proxy`：只走代理，不做直连对比。
- `NS_EGRESS_MODE=direct`：始终直连。
- `NS_PROXY_URL=http://...`：Chrome 直接走该 HTTP 代理。
- `NS_PROXY_URL=https://...`：脚本会先在本地启动一个代理桥，再转发到你的上游 HTTPS 代理。
- `NS_PROXY_INSECURE` 默认是 `true`，只会影响“**脚本连接上游 HTTPS 代理**”这一跳，不会关闭浏览器对 NodeSeek 网站本身的 HTTPS 校验。
- GitHub Hosted 默认通过 `xvfb-run` 运行 **有头 Chrome**，不再默认使用 `headless`。
- Actions 会恢复并保存 `.chrome-profile`，复用浏览器状态；只有脚本成功进入已登录状态后才会写入缓存保存标记。
- 每次运行后会上传诊断截图 artifact，方便区分 Cookie 问题、Cloudflare 挑战和页面结构变动。

## 📝 多账号配置示例

```
账号1的完整Cookie|账号2的完整Cookie
```

示例：
```
session=abc123; token=xyz|session=def456; token=uvw
```

## 📱 通知示例

### 单账号
```
NodeSeek 每日简报
━━━━━━━━━━━━━━━
👤 账号: 账号 1
🏆 奖励: 5 🍗
💬 评论: 4 条
━━━━━━━━━━━━━━━
✅ 状态: 已签到
🕒 2026-02-10 00:10:00
```

### 多账号
```
🎯 NodeSeek 多账号任务完成

👤 账号1: 签到✅ | 评论4条
👤 账号2: 签到✅ | 评论3条

⏰ 执行时间: 北京时间 2026-02-10 00:10:00
```

## ❓ 常见问题

**Q: Cookie 多久过期？**  
A: 一般 7-30 天，过期后会收到 Telegram 告警通知。

**Q: 如何手动运行测试？**  
A: 进入 Actions 页面，选择 workflow，点击 "Run workflow"。

## License

MIT
