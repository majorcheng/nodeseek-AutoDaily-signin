# NodeSeek 自动签到 - Scrapling 版

基于 [Scrapling](https://github.com/D4Vinci/Scrapling) 的 NodeSeek 自动化脚本，
使用浏览器态持久化 + Stealth 会话完成登录态检测、签到、随机评论和
Telegram 汇总通知。

## ✨ 功能

- ✅ 自动签到，支持“试试手气”与“鸡腿 x 5”偏好
- 💬 随机评论 4-7 篇帖子，帖子间随机等待 1-2 分钟
- 👥 多账号支持，`NS_COOKIE` 用 `|` 分隔
- 🌐 代理优先 / 直连兜底，支持 `auto`、`proxy`、`direct`
- 🛡️ 使用 Scrapling `StealthySession`，支持 Cloudflare 处理
- 💾 每账号独立浏览器状态目录，便于复用 Cookie / local storage
- 📸 失败时保存截图到 `artifacts/`，并按需发送 Telegram 图片
- 📱 单账号 / 多账号统一 Telegram 汇总通知

## 🚀 快速开始

1. Fork 本仓库
2. 在 GitHub 仓库 `Settings → Secrets and variables → Actions` 中配置变量
3. Actions 会每天执行两次：
   - 北京时间 `00:10`
   - 北京时间 `12:20`
4. 运行链会自动：
   - 安装 Python 依赖
   - 执行 `scrapling install`
   - 启动脚本

本项目不再依赖 Selenium、ChromeDriver、`xvfb` 或系统 Chrome 安装步骤。

## 🍪 Cookie 获取方式

1. 打开浏览器并登录 [NodeSeek](https://www.nodeseek.com)
2. 按 `F12` 打开开发者工具
3. 在 `Network` 中刷新页面
4. 任选一个请求，在 `Headers` 中复制完整 `Cookie`

示例：

```text
session=abc123xyz; token=def456uvw; user_id=12345
```

多账号时，用 `|` 连接：

```text
账号1完整Cookie|账号2完整Cookie
```

## ⚙️ 配置说明

### Secrets

- `NS_COOKIE`
  - 必填
  - NodeSeek Cookie，多账号用 `|` 分隔
- `NS_RANDOM`
  - 可选
  - `true` 表示优先“试试手气”，`false` 表示优先“鸡腿 x 5”
- `NS_COMMENT_URL`
  - 可选
  - 评论区地址，默认是交易区
- `NS_PROXY_URL`
  - 可选
  - 浏览器流量代理地址
- `NS_USERNAME`
  - 可选
  - NodeSeek 登录用户名，多账号时用 `|` 与 `NS_COOKIE` / `NS_PASSWORD` 按顺序对齐
- `NS_PASSWORD`
  - 可选
  - NodeSeek 登录密码，多账号时用 `|` 与 `NS_COOKIE` / `NS_USERNAME` 按顺序对齐
- `TG_BOT_TOKEN`
  - 可选
  - Telegram Bot Token
- `TG_CHAT_ID`
  - 可选
  - Telegram Chat ID

### Variables

- `NS_DELAY_MIN`
  - 默认 `0`
  - 整轮任务开始前的随机延迟最小分钟数
- `NS_DELAY_MAX`
  - 默认 `10`
  - 整轮任务开始前的随机延迟最大分钟数
- `NS_EGRESS_MODE`
  - 默认 `auto`
  - `auto`：先代理，失败后直连；`proxy`：只走代理；`direct`：只走直连
- `NS_CF_WAIT_SECONDS`
  - 默认 `30`
  - Cloudflare 处理等待窗口；设为 `0` 时关闭求解等待
- `NS_CF_LOGIN_RETRIES`
  - 默认 `2`
  - 登录按钮触发后的 embedded Turnstile 主动求解重试次数
- `NS_HEADLESS`
  - 默认 `true`
  - 是否使用无头浏览器
- `NS_SKIP_COMMENTS`
  - 默认 `false`
  - 设为 `true` 时只做登录态校验 + 签到，跳过评论流程
- `NS_USER_AGENT`
  - 可选
  - 覆盖 Scrapling 浏览器默认 UA，便于复现指定浏览器环境
  - 仅用于普通页面调试；真实登录认证链路默认忽略该值，避免指纹自相矛盾
- `NS_EXTRA_HEADERS`
  - 可选
  - 使用 JSON 对象传入额外请求头，适合做 NodeSeek 调试
  - 常见场景：`Accept-Language`、`Referer`、`Sec-Fetch-*`
  - 仅用于普通页面调试；真实登录认证链路默认忽略这些自定义头
- `NS_BROWSER_STATE_DIR`
  - 默认 `.browser-state`
  - 浏览器状态缓存根目录，按账号拆分子目录

当 `NS_COOKIE` 缺失、为空，或首页检测判定 Cookie 已失效时，脚本会在当前账号同时配置了 `NS_USERNAME` + `NS_PASSWORD` 的情况下尝试真实登录回退。

如果你当前目标是先在本地验证“能签到”，建议优先准备一份当前有效的 `NS_COOKIE`；
再配合 `NS_EGRESS_MODE=direct` 与 `NS_SKIP_COMMENTS=true`，先把“登录态校验 + 签到”跑通。
账号密码真实登录链路仍然保留，便于后续继续攻 Cloudflare / Turnstile。

## 🌐 代理与缓存行为

- 代理只影响 NodeSeek 浏览器动作，不影响 Telegram 通知
- 如果目标是让 GitHub Hosted 无人值守跑通真实登录，建议 `NS_PROXY_URL` 使用支持 sticky session 的住宅 / 移动代理
- `NS_EGRESS_MODE=auto` 时：
  - 有 `NS_PROXY_URL`：先走代理，再在可重试失败时切直连
  - 无 `NS_PROXY_URL`：直接走直连
- 当前脚本只会在 `cf_challenge` 或启动失败时切换到下一个出口
- 浏览器状态缓存保存在 `NS_BROWSER_STATE_DIR` 下：
  - `account-1/`
  - `account-2/`
  - ...
- 当任一账号成功通过登录校验时，会写入
  `NS_BROWSER_STATE_DIR/.ns_browser_state_ok`，供 GitHub Actions 决定是否保存缓存

## 🧪 本地运行

先安装依赖：

```bash
python -m pip install -r requirements.txt
scrapling install
```

然后执行：

```bash
python nodeseek_daily.py
```

如果你是本地先调试签到，推荐先用有效 Cookie 跑一个最小闭环：

```bash
export NS_COOKIE='当前有效Cookie'
export NS_HEADLESS='true'
export NS_EGRESS_MODE='direct'
export NS_SKIP_COMMENTS='true'
export NS_DELAY_MIN='0'
export NS_DELAY_MAX='0'
python nodeseek_daily.py
```

这条命令的成功口径是：识别到已登录状态，并完成签到或返回“今日已签”；评论会明确标记为已跳过。

如需临时模拟指定浏览器请求头，可这样运行：

```bash
export NS_USER_AGENT='Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:149.0) Gecko/20100101 Firefox/149.0'
export NS_EXTRA_HEADERS='{"Accept-Language":"en-US,en;q=0.9","Referer":"https://www.nodeseek.com/","Sec-Fetch-Site":"same-origin"}'
python nodeseek_daily.py
```

注意：上面的 `NS_USER_AGENT` / `NS_EXTRA_HEADERS` 主要用于首页、帖子页等普通页面调试；
当脚本进入“账号密码真实登录”链路时，会默认切回浏览器原生 Chromium 指纹。

如需在 Cookie 失效时回退到真实登录，可再补充：

```bash
export NS_USERNAME='your-account'
export NS_PASSWORD='your-password'
python nodeseek_daily.py
```

如需让脚本在 Cookie 失效后尝试真实登录，可这样运行：

```bash
export NS_COOKIE='账号1完整Cookie|账号2完整Cookie'
export NS_USERNAME='账号1用户名|账号2用户名'
export NS_PASSWORD='账号1密码|账号2密码'
python nodeseek_daily.py
```

## 📸 诊断产物

运行失败时，截图会保存到：

```text
artifacts/account-1/
artifacts/account-2/
```

常见文件名：

- `login_stage_1_filled.png`：账号密码已填的现场页
- `login_stage_2_turnstile.png`：点击登录后，Turnstile / CF 校验出现的现场页
- `login_stage_3_failed_live.png`：登录失败当下的现场页
- `login_check_failed.png`：Cookie 引导失败时的首页/登录态复核图
- `login_recheck.png`：真实登录失败但现场截图缺失时的补抓页
- `login_attempt_summary.json`：登录尝试摘要，包含 cached/clean state、PAT 401、登录请求次数等关键信息
- `cf_block_sign.png`
- `sign_intro_error.png`
- `sign_exception.png`
- `comment_main_error.png`
- `comment_error_0.png`

GitHub Actions 会自动上传 `artifacts/**/*.png`。

## ❓ 常见问题

### 1. 为什么切到直连了？

因为 `auto` 模式下，只有代理出口命中 Cloudflare 挑战或初始化失败时，
才会切换到直连。

### 2. 为什么明明配置了 Cookie 还是失败？

常见原因：

- Cookie 过期
- 站点页面结构变更
- 命中 Cloudflare 挑战且超出等待时间
- 评论区或签到页改版，导致选择器失效

### 3. 为什么缓存没保存？

只有脚本成功通过登录态检测后，才会写入
`NS_BROWSER_STATE_DIR/.ns_browser_state_ok`。
如果整轮都没登录成功，Actions 不会保存浏览器状态缓存。

## License

MIT
