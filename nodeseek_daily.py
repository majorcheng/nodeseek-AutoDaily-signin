# -- coding: utf-8 --
"""
Copyright (c) 2024 [Hosea]
Licensed under the MIT License.
See LICENSE file in the project root for full license information.
"""
import os
import random
import select
import socket
import ssl
import threading
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from socketserver import ThreadingMixIn
from webdriver_manager.chrome import ChromeDriverManager


class Config:
    """配置类 - 统一管理所有环境变量"""
    
    def __init__(self):
        # Cookie 配置（支持多账号，用 | 分隔）
        raw_cookie = os.environ.get("NS_COOKIE") or os.environ.get("COOKIE") or ""
        self.cookies = [c.strip() for c in raw_cookie.split("|") if c.strip()]
        
        # 基础配置
        ns_random_env = os.environ.get("NS_RANDOM", "")
        # 如果未设置或设置为空字符串，默认 true；否则根据设置的值判断
        self.ns_random = (ns_random_env.lower() == "true") if ns_random_env else True
        self.headless = os.environ.get("HEADLESS", "true").lower() == "true"
        
        # Telegram 通知配置
        self.tg_bot_token = os.environ.get("TG_BOT_TOKEN")
        self.tg_chat_id = os.environ.get("TG_CHAT_ID")
        
        # 评论区域配置（处理空字符串）
        comment_url_env = os.environ.get("NS_COMMENT_URL", "") or ""
        self.comment_url = comment_url_env.strip() if comment_url_env.strip() else "https://www.nodeseek.com/categories/trade"
        
        # 随机延迟配置（分钟）
        delay_min_str = os.environ.get("NS_DELAY_MIN", "") or "0"
        delay_max_str = os.environ.get("NS_DELAY_MAX", "") or "10"
        self.delay_min = int(delay_min_str)
        self.delay_max = int(delay_max_str)

        # 浏览器业务流量代理配置（仅影响 Selenium 的 Chrome）
        proxy_url_env = os.environ.get("NS_PROXY_URL", "") or ""
        self.proxy_url = proxy_url_env.strip()
        proxy_insecure_env = os.environ.get("NS_PROXY_INSECURE", "true") or "true"
        self.proxy_insecure = proxy_insecure_env.strip().lower() == "true"
        egress_mode_env = os.environ.get("NS_EGRESS_MODE", "auto") or "auto"
        self.egress_mode = egress_mode_env.strip().lower() or "auto"
        if self.egress_mode not in {"auto", "proxy", "direct"}:
            self.egress_mode = "auto"

        cf_wait_env = os.environ.get("NS_CF_WAIT_SECONDS", "30") or "30"
        self.cf_wait_seconds = max(0, int(cf_wait_env))
        profile_dir_env = os.environ.get("NS_CHROME_PROFILE_DIR", ".chrome-profile") or ".chrome-profile"
        self.chrome_profile_dir = profile_dir_env.strip() or ".chrome-profile"
    
    @property
    def account_count(self):
        return len(self.cookies)
    
    def get_random_delay_seconds(self):
        """获取随机延迟秒数"""
        if self.delay_max <= 0:
            return 0
        # 确保 min <= max
        actual_min = min(self.delay_min, self.delay_max)
        actual_max = max(self.delay_min, self.delay_max)
        delay_minutes = random.randint(actual_min, actual_max)
        return delay_minutes * 60


# 全局配置实例
config = Config()

# 随机评论内容
randomInputStr = ["bd","绑定","帮顶",":xhj007: BD","好价","过来看一下"," :xhj025: 嚯","咕噜咕噜","可以","  :xhj003: 可以","还可以","楼下"," :xhj010: 顶","bd一下"," :xhj027: 哦"]

EGRESS_PROXY = "proxy"
EGRESS_DIRECT = "direct"
LOGIN_STATUS_OK = "ok"
LOGIN_STATUS_CF_CHALLENGE = "cf_challenge"
LOGIN_STATUS_LOGIN_PAGE = "login_page"
LOGIN_STATUS_COOKIE_INVALID = "cookie_invalid"
LOGIN_STATUS_UNKNOWN_PAGE = "unknown_page"
LOGIN_STATUS_EGRESS_FAILED = "egress_failed"
PROFILE_SUCCESS_MARKER = ".ns_profile_ok"


def get_profile_root_dir():
    return Path(config.chrome_profile_dir)


def build_profile_dir(account_index):
    return get_profile_root_dir() / f"account-{account_index + 1}"


def get_profile_success_marker_path():
    return get_profile_root_dir() / PROFILE_SUCCESS_MARKER


def clear_profile_success_marker():
    marker_path = get_profile_success_marker_path()
    try:
        marker_path.unlink(missing_ok=True)
    except TypeError:
        if marker_path.exists():
            marker_path.unlink()


def mark_profile_success():
    marker_path = get_profile_success_marker_path()
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text("ok\n", encoding="utf-8")


def build_attempt_result(status_code, reason=None, egress_mode=None):
    return {
        "status_code": status_code,
        "reason": reason,
        "egress_mode": egress_mode,
    }


def is_cloudflare_challenge_page(driver):
    try:
        title = (driver.title or "").strip()
    except Exception:
        title = ""

    if "Just a moment" in title or "Attention Required" in title:
        return True

    try:
        body_text = driver.find_element(By.TAG_NAME, 'body').text
    except Exception:
        body_text = ""

    challenge_keywords = [
        "Performing security verification",
        "This website uses a security service",
        "verifies you are not a bot",
        "Cloudflare",
    ]
    return any(keyword in body_text for keyword in challenge_keywords)


def wait_for_cloudflare_clearance(driver, wait_seconds):
    if wait_seconds <= 0:
        if is_cloudflare_challenge_page(driver):
            return False, "未等待，仍停留在 Cloudflare 挑战页"
        return True, None

    deadline = time.time() + wait_seconds
    last_title = ""
    while time.time() <= deadline:
        if not is_cloudflare_challenge_page(driver):
            return True, None

        try:
            current_title = driver.title or ""
        except Exception:
            current_title = ""

        if current_title != last_title:
            print(f"⏳ 等待 Cloudflare 挑战完成中... 当前标题: {current_title}")
            last_title = current_title
        time.sleep(2)

    return False, f"等待 {wait_seconds} 秒后仍停留在 Cloudflare 挑战页"


def build_egress_candidates():
    if config.egress_mode == EGRESS_PROXY:
        return [EGRESS_PROXY]
    if config.egress_mode == EGRESS_DIRECT:
        return [EGRESS_DIRECT]
    if config.proxy_url:
        return [EGRESS_PROXY, EGRESS_DIRECT]
    return [EGRESS_DIRECT]


def should_retry_with_next_egress(status_code):
    return status_code in {LOGIN_STATUS_CF_CHALLENGE, LOGIN_STATUS_EGRESS_FAILED}


def describe_egress_mode(egress_mode):
    return "代理" if egress_mode == EGRESS_PROXY else "直连"


def build_stealth_script():
    return """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en-US', 'en']});
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
  window.navigator.permissions.query = (parameters) => (
    parameters && parameters.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : originalQuery(parameters)
  );
}
if (typeof WebGLRenderingContext !== 'undefined') {
  const originalGetParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
    return originalGetParameter.call(this, parameter);
  };
}
if (typeof WebGL2RenderingContext !== 'undefined') {
  const originalGetParameter2 = WebGL2RenderingContext.prototype.getParameter;
  WebGL2RenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
    return originalGetParameter2.call(this, parameter);
  };
}
"""


def mask_proxy_url(proxy_url):
    """隐藏代理地址中的敏感信息，避免日志泄露"""
    if not proxy_url:
        return "<未配置>"

    try:
        parsed = urlparse(proxy_url)
        if not parsed.scheme or not parsed.hostname:
            return "<无效代理地址>"

        netloc = parsed.hostname
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"

        if parsed.username or parsed.password:
            return f"{parsed.scheme}://***:***@{netloc}"

        return f"{parsed.scheme}://{netloc}"
    except Exception:
        return "<无效代理地址>"


def _recv_until_header_end(sock, max_bytes=65536):
    """读取 HTTP 响应头，直到遇到空行"""
    buffer = b""
    while b"\r\n\r\n" not in buffer and len(buffer) < max_bytes:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buffer += chunk
    return buffer


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """支持多线程的本地 HTTP 代理桥"""

    daemon_threads = True
    allow_reuse_address = True


class UpstreamProxyBridgeHandler(BaseHTTPRequestHandler):
    """把本地请求转发到上游 HTTP/HTTPS 代理"""

    protocol_version = "HTTP/1.1"
    server_version = "NodeSeekProxyBridge/1.0"

    def log_message(self, format, *args):
        return

    def do_CONNECT(self):
        self.server.bridge.handle_connect(self)

    def do_GET(self):
        self.server.bridge.handle_forward_request(self)

    def do_POST(self):
        self.server.bridge.handle_forward_request(self)

    def do_HEAD(self):
        self.server.bridge.handle_forward_request(self)

    def do_OPTIONS(self):
        self.server.bridge.handle_forward_request(self)

    def do_PUT(self):
        self.server.bridge.handle_forward_request(self)

    def do_PATCH(self):
        self.server.bridge.handle_forward_request(self)

    def do_DELETE(self):
        self.server.bridge.handle_forward_request(self)


class BrowserProxyRuntime:
    """为 Chrome 构建浏览器代理运行时，必要时启动本地桥接代理"""

    def __init__(self, proxy_url, proxy_insecure=False):
        self.proxy_url = proxy_url
        self.proxy_insecure = proxy_insecure
        self.parsed_proxy = self._parse_proxy_url(proxy_url)
        self.server = None
        self.server_thread = None
        self.browser_proxy_url = None

    @staticmethod
    def _parse_proxy_url(proxy_url):
        parsed = urlparse(proxy_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("当前仅支持 http:// 或 https:// 代理")
        if not parsed.hostname:
            raise ValueError("代理地址缺少主机名")
        if parsed.username or parsed.password:
            raise ValueError("当前版本仅支持无认证代理")
        return parsed

    def start(self):
        if self.parsed_proxy.scheme == "http":
            self.browser_proxy_url = self._normalized_upstream_proxy_url()
            print(f"🌐 浏览器业务流量将通过 HTTP 代理: {mask_proxy_url(self.browser_proxy_url)}")
            return self.browser_proxy_url

        if self.proxy_insecure:
            print("⚠️ 已启用 NS_PROXY_INSECURE=true，将跳过上游 HTTPS 代理证书校验")

        self._probe_https_proxy()
        self.server = ThreadedHTTPServer(("127.0.0.1", 0), UpstreamProxyBridgeHandler)
        self.server.bridge = self
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        local_port = self.server.server_address[1]
        self.browser_proxy_url = f"http://127.0.0.1:{local_port}"
        print(
            "🌐 浏览器业务流量将通过本地代理桥接到上游 HTTPS 代理: "
            f"{mask_proxy_url(self.proxy_url)} -> {self.browser_proxy_url}"
        )
        return self.browser_proxy_url

    def stop(self):
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception as exc:
                print(f"关闭本地代理桥时出错: {str(exc)}")
            finally:
                self.server = None

        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(timeout=2)
        self.server_thread = None

    def _normalized_upstream_proxy_url(self):
        host = self.parsed_proxy.hostname
        port = self.parsed_proxy.port or (80 if self.parsed_proxy.scheme == "http" else 443)
        return f"{self.parsed_proxy.scheme}://{host}:{port}"

    def open_upstream_socket(self):
        host = self.parsed_proxy.hostname
        port = self.parsed_proxy.port or (80 if self.parsed_proxy.scheme == "http" else 443)
        raw_sock = socket.create_connection((host, port), timeout=10)

        if self.parsed_proxy.scheme == "http":
            return raw_sock

        context = ssl.create_default_context()
        if self.proxy_insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            server_hostname = None
        else:
            server_hostname = host

        return context.wrap_socket(raw_sock, server_hostname=server_hostname)

    def _probe_https_proxy(self):
        target = "www.nodeseek.com:443"
        upstream_sock = None
        try:
            upstream_sock = self.open_upstream_socket()
            connect_request = (
                f"CONNECT {target} HTTP/1.1\r\n"
                f"Host: {target}\r\n"
                "Proxy-Connection: Keep-Alive\r\n\r\n"
            ).encode("utf-8")
            upstream_sock.sendall(connect_request)
            response_head = _recv_until_header_end(upstream_sock)
            if not response_head:
                raise RuntimeError("上游 HTTPS 代理未返回响应")

            status_line = response_head.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
            if " 200 " not in f" {status_line} ":
                raise RuntimeError(f"上游 HTTPS 代理 CONNECT 失败: {status_line}")

            print(f"✅ 上游 HTTPS 代理探测成功: {mask_proxy_url(self.proxy_url)}")
        finally:
            if upstream_sock:
                try:
                    upstream_sock.close()
                except Exception:
                    pass

    def handle_connect(self, handler):
        upstream_sock = None
        try:
            target = handler.path
            upstream_sock = self.open_upstream_socket()
            connect_request = (
                f"CONNECT {target} HTTP/1.1\r\n"
                f"Host: {target}\r\n"
                "Proxy-Connection: Keep-Alive\r\n\r\n"
            ).encode("utf-8")
            upstream_sock.sendall(connect_request)
            response_head = _recv_until_header_end(upstream_sock)
            if not response_head:
                raise RuntimeError("上游代理未返回 CONNECT 响应")

            status_line = response_head.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
            if " 200 " not in f" {status_line} ":
                raise RuntimeError(status_line)

            handler.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._relay_bidirectional(handler.connection, upstream_sock)
        except Exception as exc:
            try:
                handler.send_error(502, f"上游代理 CONNECT 失败: {str(exc)}")
            except Exception:
                pass
        finally:
            if upstream_sock:
                try:
                    upstream_sock.close()
                except Exception:
                    pass

    def handle_forward_request(self, handler):
        upstream_sock = None
        try:
            content_length = int(handler.headers.get("Content-Length", "0") or "0")
            request_body = handler.rfile.read(content_length) if content_length > 0 else b""

            header_lines = []
            for key, value in handler.headers.items():
                if key.lower() in ("proxy-connection", "connection"):
                    continue
                header_lines.append(f"{key}: {value}\r\n")

            header_lines.append("Connection: close\r\n")
            header_lines.append("Proxy-Connection: close\r\n")
            request_bytes = (
                f"{handler.command} {handler.path} {handler.request_version}\r\n".encode("utf-8")
                + "".join(header_lines).encode("utf-8")
                + b"\r\n"
                + request_body
            )

            upstream_sock = self.open_upstream_socket()
            upstream_sock.sendall(request_bytes)
            while True:
                chunk = upstream_sock.recv(65536)
                if not chunk:
                    break
                handler.wfile.write(chunk)
        except Exception as exc:
            try:
                handler.send_error(502, f"上游代理转发失败: {str(exc)}")
            except Exception:
                pass
        finally:
            if upstream_sock:
                try:
                    upstream_sock.close()
                except Exception:
                    pass

    @staticmethod
    def _relay_bidirectional(client_sock, upstream_sock):
        sockets = [client_sock, upstream_sock]
        for sock in sockets:
            try:
                sock.settimeout(None)
            except Exception:
                pass

        while True:
            readable, _, errored = select.select(sockets, [], sockets, 60)
            if errored or not readable:
                break

            for source_sock in readable:
                try:
                    payload = source_sock.recv(65536)
                except OSError:
                    return

                if not payload:
                    return

                target_sock = upstream_sock if source_sock is client_sock else client_sock
                target_sock.sendall(payload)


def build_browser_proxy_runtime(egress_mode):
    """按指定出口初始化浏览器代理；失败时抛出异常，由上层决定是否切换出口"""
    if egress_mode == EGRESS_DIRECT:
        print("🌐 当前出口: 直连")
        return None

    if not config.proxy_url:
        raise RuntimeError("当前出口模式要求代理，但未配置 NS_PROXY_URL")

    print(f"🌐 当前出口: 代理 {mask_proxy_url(config.proxy_url)}")
    runtime = BrowserProxyRuntime(config.proxy_url, config.proxy_insecure)
    runtime.start()
    return runtime


def send_telegram_message(message):
    """
    发送 Telegram 消息通知
    如果未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，则静默跳过
    """
    if not config.tg_bot_token or not config.tg_chat_id:
        print("未配置 Telegram 通知，跳过发送")
        return False
    
    try:
        url = f"https://api.telegram.org/bot{config.tg_bot_token}/sendMessage"
        payload = {
            "chat_id": config.tg_chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print("Telegram 通知发送成功")
            return True
        else:
            print(f"Telegram 通知发送失败: {response.text}")
            return False
    except Exception as e:
        print(f"Telegram 通知发送出错: {str(e)}")
        return False

def send_telegram_photo(photo_path, caption=None):
    """
    发送图片到 Telegram
    """
    if not config.tg_bot_token or not config.tg_chat_id:
        return False
        
    try:
        url = f"https://api.telegram.org/bot{config.tg_bot_token}/sendPhoto"
        with open(photo_path, 'rb') as photo:
            payload = {'chat_id': config.tg_chat_id}
            if caption:
                payload['caption'] = caption
            files = {'photo': photo}
            response = requests.post(url, data=payload, files=files, timeout=20)
            
        if response.status_code == 200:
            print("Telegram 图片发送成功")
            return True
        else:
            print(f"Telegram 图片发送失败: {response.text}")
            return False
    except Exception as e:
        print(f"Telegram 图片发送出错: {str(e)}")
        return False

def retry(max_attempts=3, delay=5):
    """
    重试装饰器
    :param max_attempts: 最大重试次数
    :param delay: 重试间隔（秒）
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        print(f"[{func.__name__}] 第 {attempt + 1} 次尝试失败: {str(e)}")
                        print(f"等待 {delay} 秒后重试...")
                        time.sleep(delay)
                    else:
                        print(f"[{func.__name__}] 已达最大重试次数 ({max_attempts})")
            raise last_exception
        return wrapper
    return decorator

def build_cookie_payload(name, value):
    """按站点实际行为构造 Cookie，避免把 host-only Cookie 错写成顶级域 Cookie"""
    payload = {
        'name': name,
        'value': value,
        'path': '/'
    }

    if name == 'cf_clearance':
        payload['domain'] = '.nodeseek.com'

    return payload


def capture_login_diagnostics(driver, reason):
    """登录检测失败时输出诊断信息，便于区分 Cookie 失效、风控或页面结构变化"""
    current_url = '<unknown>'
    title = '<unknown>'
    body_preview = '<empty>'

    try:
        current_url = driver.current_url
    except Exception:
        pass

    try:
        title = driver.title
    except Exception:
        pass

    try:
        body_text = driver.find_element(By.TAG_NAME, 'body').text
        normalized = ' '.join(body_text.split())
        body_preview = normalized[:300] if normalized else '<empty>'
    except Exception as exc:
        body_preview = f'<获取页面文本失败: {str(exc)}>'

    print(f"⚠️ 登录检测失败原因: {reason}")
    print(f"当前URL: {current_url}")
    print(f"页面标题: {title}")
    print(f"页面预览: {body_preview}")

    try:
        screenshot_path = 'login_check_failed.png'
        driver.save_screenshot(screenshot_path)
        print(f"已保存登录检测截图: {screenshot_path}")
    except Exception as exc:
        print(f"保存登录检测截图失败: {str(exc)}")


def check_login_status(driver):
    """
    检测 Cookie 是否有效（用户是否已登录）
    返回: (status_code, reason)
    """
    try:
        print("正在检测登录状态...")
        current_url = driver.current_url or ''

        if is_cloudflare_challenge_page(driver):
            reason = '登录检测阶段遭遇 Cloudflare/风控页'
            capture_login_diagnostics(driver, reason)
            return LOGIN_STATUS_CF_CHALLENGE, reason

        user_elements = driver.find_elements(By.CSS_SELECTOR, '.avatar, .nsk-user-avatar, [class*="avatar"]')
        login_buttons = driver.find_elements(By.XPATH, "//span[contains(text(), '登录')]")

        if len(user_elements) > 0 and len(login_buttons) == 0:
            print("✅ 登录状态有效")
            return LOGIN_STATUS_OK, None

        try:
            page_text = driver.find_element(By.TAG_NAME, 'body').text
        except Exception:
            page_text = ''

        if '/login' in current_url:
            reason = '跳转到了登录页，Cookie 可能失效'
            capture_login_diagnostics(driver, reason)
            return LOGIN_STATUS_LOGIN_PAGE, reason

        if '登录' in page_text and '注册' in page_text and '个人中心' not in page_text:
            reason = '页面出现登录/注册提示，Cookie 可能失效'
            capture_login_diagnostics(driver, reason)
            return LOGIN_STATUS_COOKIE_INVALID, reason

        reason = '未识别到登录态，可能是风控、页面结构变化或 Cookie domain 不匹配'
        capture_login_diagnostics(driver, reason)
        return LOGIN_STATUS_UNKNOWN_PAGE, reason
    except Exception as e:
        reason = f'检测登录状态时出错: {str(e)}'
        print(reason)
        return LOGIN_STATUS_UNKNOWN_PAGE, reason


def _parse_reward_from_text(text):
    """从文本中解析鸡腿数量"""
    import re
    # 匹配多种格式: "获得 5 鸡腿", "鸡腿 5 个", "获得鸡腿5个", "踩到鸡腿5个"
    match = re.search(r"获得\s*(\d+)\s*鸡腿|鸡腿\s*(\d+)\s*个|踩到鸡腿\s*(\d+)\s*个|得鸡腿(\d+)个", text)
    if match:
        return match.group(1) or match.group(2) or match.group(3) or match.group(4)
    # 再尝试最宽泛的匹配：任意位置的"数字+鸡腿"或"鸡腿+数字"
    match2 = re.search(r"(\d+)\s*(?:个?\s*鸡腿|鸡腿)", text)
    if match2:
        return match2.group(1)
    return "未知"

def _parse_reward_from_page(driver):
    """从当前页面解析签到奖励数量"""
    try:
        # 优先从 .board-intro 面板解析
        intros = driver.find_elements(By.CSS_SELECTOR, ".board-intro")
        if intros:
            text = intros[0].text
            print(f"签到后面板文本: {text}")
            result = _parse_reward_from_text(text)
            if result != "未知":
                return result
        
        # 其次从全局文本解析
        body_text = driver.find_element(By.TAG_NAME, "body").text
        return _parse_reward_from_text(body_text)
    except Exception as e:
        print(f"解析奖励时出错: {str(e)}")
        return "未知"

def click_sign_icon(driver):
    """
    尝试点击签到图标并完成签到
    返回: (status, message)
    - status: "success" | "already" | "failed"
    - message: 签到获得的鸡腿数量或状态描述
    """
    try:
        print("开始查找签到图标...")
        
        # 方案 A: 直接跳转到签到页面
        print("直接访问签到页面...")
        driver.get("https://www.nodeseek.com/board")
        time.sleep(3)
        
        current_url = driver.current_url
        print(f"当前页面URL: {current_url}")
        
        # 0. 检查 Cloudflare
        if is_cloudflare_challenge_page(driver):
            wait_ok, wait_reason = wait_for_cloudflare_clearance(driver, config.cf_wait_seconds)
            if not wait_ok:
                print(f"❌ 检测到 Cloudflare 拦截: {wait_reason}")
                driver.save_screenshot("cf_block_sign.png")
                send_telegram_photo("cf_block_sign.png", caption=f"❌ 签到时遭遇 Cloudflare 拦截\n{wait_reason}")
                return "failed", "Cloudflare 拦截"
            current_url = driver.current_url
            print(f"挑战结束后当前URL: {current_url}")

        # 1. 检查是否被重定向回首页
        if "/board" not in current_url and "nodeseek.com" in current_url and len(current_url) < 30:
            print("⚠️ 似乎跳转回了首页，尝试在首页寻找签到入口...")
            try:
                sign_icon = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, "//span[@title='签到']"))
                )
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", sign_icon)
                time.sleep(0.5)
                driver.execute_script("arguments[0].click();", sign_icon)
                print("首页签到图标点击成功")
                time.sleep(3)
            except Exception as e:
                print(f"首页签到图标未找到: {str(e)}")
        
        # 2. 尝试定位签到面板（.board-intro）
        try:
            # 等待签到面板加载（黄色背景区域）
            # 缩短等待时间，因为如果没加载出来，可能是已签到或者样式变了
            board_intro = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".board-intro"))
            )
            print("签到面板加载成功")
            
            # 检查面板文本
            intro_text = board_intro.text
            print(f"面板文本内容: {intro_text}")
            
            # 优先检查是否存在"已签到"关键词
            if "获得" in intro_text or "排名" in intro_text or "已签到" in intro_text:
                print("✅ 检测到已签到关键词")
                count = _parse_reward_from_text(intro_text)
                return "already", count
            
            # 检查是否有按钮
            buttons = board_intro.find_elements(By.TAG_NAME, "button")
            if buttons:
                print(f"发现 {len(buttons)} 个按钮")
                target_button = None
                
                # 根据配置选择按钮
                for btn in buttons:
                    text = btn.text
                    if config.ns_random:
                        if "手气" in text:
                            target_button = btn
                            print("已选择 '试试手气' 按钮 (NS_RANDOM=true)")
                            break
                    else:
                        if "鸡腿" in text or "x 5" in text:
                            target_button = btn
                            print("已选择 '鸡腿 x 5' 按钮 (NS_RANDOM=false)")
                            break
                
                # 如果没找到偏好的按钮，默认选第一个
                if not target_button:
                    print("未找到首选按钮，使用第一个可用按钮")
                    target_button = buttons[0]
                
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target_button)
                time.sleep(0.5)
                driver.execute_script("arguments[0].click();", target_button)
                print("签到按钮点击成功")
                time.sleep(3)
                
                # 点击后解析奖励数量
                count = _parse_reward_from_page(driver)
                return "success", count
                
            if "还未签到" in intro_text:
                print("❌ 检测到'还未签到'文本，但未找到按钮")
                return "failed", "未找到按钮"
                
            print("❌ 无法确认签到状态 (面板无按钮且无明确已签到文本)")
            return "failed", "无法确认状态"

        except TimeoutException:
            print("⚠️ 未找到签到面板 (.board-intro)，尝试全局文本搜索...")
            
            # 3. 兜底策略：全局搜索文本和按钮
            print("尝试直接查找签到按钮...")
            try:
                target_button = None
                if config.ns_random:
                    print("配置为随机签到，优先查找 '试试手气'...")
                    btns = driver.find_elements(By.XPATH, "//button[contains(text(), '手气')]")
                    if btns: target_button = btns[0]
                else:
                    print("配置为固定签到，优先查找 '鸡腿 x 5'...")
                    btns = driver.find_elements(By.XPATH, "//button[contains(text(), '鸡腿')]")
                    if btns: target_button = btns[0]
                
                # 如果没找到，尝试找另一个
                if not target_button:
                    print("首选按钮未找到，尝试查找任意签到按钮...")
                    btns = driver.find_elements(By.XPATH, "//button[contains(text(), '鸡腿') or contains(text(), '手气')]")
                    if btns: target_button = btns[0]
                    
                if target_button:
                    print(f"✅ 全局查找发现按钮: {target_button.text}，尝试点击...")
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target_button)
                    time.sleep(0.5)
                    driver.execute_script("arguments[0].click();", target_button)
                    print("全局按钮点击成功")
                    time.sleep(3)
                    
                    # 点击后解析奖励数量
                    count = _parse_reward_from_page(driver)
                    return "success", count
            except Exception as e:
                print(f"全局按钮查找失败: {str(e)}")

            # 有时候 .board-intro 加载慢或者结构变了，直接找关键文本确认是否已签到
            try:
                success_msg = driver.find_elements(By.XPATH, "//*[contains(text(), '今日签到获得') or contains(text(), '当前排名')]")
                if success_msg:
                    print(f"✅ 通过文本发现已签到信息: {success_msg[0].text}")
                    count = _parse_reward_from_text(success_msg[0].text)
                    return "already", count
            except:
                pass

            page_text = driver.find_element(By.TAG_NAME, "body").text
            if "今日已签到" in page_text or "签到成功" in page_text or "本次获得" in page_text:
                print("✅ 全局文本检测到 '已签到' 相关字样")
                count = _parse_reward_from_text(page_text)
                return "already", count
                
            if "登录" in page_text and "注册" in page_text and "个人中心" not in page_text:
                print("❌ 检测到页面包含'登录/注册'，可能是Cookie失效")
                return "failed", "Cookie可能失效"

            print("❌ 无法确认签到状态")
            screenshot_path = "sign_intro_error.png"
            driver.save_screenshot(screenshot_path)
            send_telegram_photo(screenshot_path, caption=f"❌ 签到状态未知\nURL: {current_url}")
            return "failed", "状态未知"
            
    except Exception as e:
        print(f"签到过程中出错: {str(e)}")
        traceback.print_exc()
        try:
            driver.save_screenshot("sign_exception.png")
            send_telegram_photo("sign_exception.png", caption=f"❌ 签到异常: {str(e)}")
        except:
            pass
        return "failed", f"异常: {str(e)}"

def setup_driver_and_cookies(cookie_str, account_index, egress_mode):
    """
    初始化浏览器并设置 Cookie
    返回: (driver, proxy_runtime, attempt_result)
    """
    proxy_runtime = None
    driver = None
    attempt_result = build_attempt_result(LOGIN_STATUS_EGRESS_FAILED, '浏览器初始化失败', egress_mode)
    try:
        if not cookie_str:
            reason = '未找到 cookie 配置'
            print(reason)
            return None, None, build_attempt_result(LOGIN_STATUS_COOKIE_INVALID, reason, egress_mode)

        profile_dir = build_profile_dir(account_index)
        profile_dir.mkdir(parents=True, exist_ok=True)

        print(f"开始初始化浏览器... 账号={account_index + 1}, 出口={describe_egress_mode(egress_mode)}")
        print(f"浏览器 profile 目录: {profile_dir}")
        options = Options()
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-software-rasterizer')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--lang=zh-CN')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')
        options.add_argument(f'--user-data-dir={profile_dir.resolve()}')
        options.add_experimental_option('excludeSwitches', ['enable-automation'])
        options.add_experimental_option('useAutomationExtension', False)

        if config.headless:
            print('启用无头模式...')
            options.add_argument('--headless=new')
        else:
            print('启用有头模式（通过 xvfb 提供虚拟显示）...')

        proxy_runtime = build_browser_proxy_runtime(egress_mode)
        if proxy_runtime and proxy_runtime.browser_proxy_url:
            options.add_argument(f"--proxy-server={proxy_runtime.browser_proxy_url}")

        print('正在启动 Chrome...')
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': build_stealth_script()
        })

        driver.set_window_size(1920, 1080)
        print('Chrome 启动成功')

        print('正在访问首页并注入 Cookie...')
        driver.get('https://www.nodeseek.com')
        time.sleep(5)
        print('Cookie 域名策略: cf_clearance -> .nodeseek.com，其余 Cookie -> 当前主机 www.nodeseek.com')

        for cookie_item in cookie_str.split(';'):
            try:
                name, value = cookie_item.strip().split('=', 1)
                cookie_payload = build_cookie_payload(name.strip(), value.strip())
                driver.add_cookie(cookie_payload)
            except Exception as e:
                print(f"设置 Cookie 出错: {name if 'name' in locals() else '<unknown>'} | {str(e)}")
                continue

        print('刷新页面...')
        driver.refresh()
        time.sleep(3)
        wait_ok, wait_reason = wait_for_cloudflare_clearance(driver, config.cf_wait_seconds)
        if not wait_ok:
            capture_login_diagnostics(driver, wait_reason)
            attempt_result = build_attempt_result(LOGIN_STATUS_CF_CHALLENGE, wait_reason, egress_mode)
            return driver, proxy_runtime, attempt_result

        attempt_result = build_attempt_result(LOGIN_STATUS_OK, None, egress_mode)
        return driver, proxy_runtime, attempt_result

    except Exception as e:
        reason = f'浏览器初始化或首页引导失败: {str(e)}'
        print(reason)
        print('详细错误信息:')
        print(traceback.format_exc())
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        if proxy_runtime:
            proxy_runtime.stop()
        return None, None, build_attempt_result(LOGIN_STATUS_EGRESS_FAILED, reason, egress_mode)


def nodeseek_comment(driver):
    """执行评论任务，返回成功评论数量"""
    comment_count = 0
    try:
        print(f"正在访问评论区域: {config.comment_url}")
        driver.get(config.comment_url)
        print("等待页面加载...")
        
        # 获取初始帖子列表
        posts = WebDriverWait(driver, 30).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, '.post-list-item'))
        )
        print(f"成功获取到 {len(posts)} 个帖子")
        
        # 过滤掉置顶帖
        valid_posts = [post for post in posts if not post.find_elements(By.CSS_SELECTOR, '.pined')]
        # 随机选择 4-7 个帖子
        post_count = random.randint(4, 7)
        selected_posts = random.sample(valid_posts, min(post_count, len(valid_posts)))
        
        # 存储已选择的帖子URL
        selected_urls = []
        for post in selected_posts:
            try:
                post_link = post.find_element(By.CSS_SELECTOR, '.post-title a')
                selected_urls.append(post_link.get_attribute('href'))
            except:
                continue
        
        # 使用URL列表进行操作
        consecutive_failures = 0  # 连续失败计数器
        for i, post_url in enumerate(selected_urls):
            # 如果连续失败 2 次，可能是浏览器状态异常，停止评论
            if consecutive_failures >= 2:
                print(f"⚠️ 连续失败 {consecutive_failures} 次，停止评论任务以避免更多错误")
                break
            
            try:
                print(f"正在处理第 {i+1} 个帖子")
                driver.get(post_url)
                time.sleep(3)  # 增加等待时间确保页面完全加载
                
                # 检查页面是否正常加载
                if is_cloudflare_challenge_page(driver):
                    wait_ok, wait_reason = wait_for_cloudflare_clearance(driver, config.cf_wait_seconds)
                    if not wait_ok:
                        print(f"⚠️ 页面加载遭遇 Cloudflare，跳过此帖子: {wait_reason}")
                        consecutive_failures += 1
                        continue

                if "error" in driver.title.lower():
                    print(f"⚠️ 页面加载异常，跳过此帖子")
                    consecutive_failures += 1
                    continue
                
                # 等待 CodeMirror 编辑器加载
                editor = WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, '.CodeMirror'))
                )
                
                # 使用 JS 点击编辑器获取焦点（避免元素遮挡）
                driver.execute_script("arguments[0].click();", editor)
                time.sleep(0.5)
                input_text = random.choice(randomInputStr)

                # 使用 JS 直接设置编辑器内容（更稳定）
                try:
                    driver.execute_script("""
                        var cm = arguments[0].CodeMirror;
                        if (cm) {
                            cm.setValue(arguments[1]);
                        }
                    """, editor, input_text)
                except:
                    # 如果 JS 注入失败，回退到 ActionChains
                    actions = ActionChains(driver)
                    for char in input_text:
                        actions.send_keys(char)
                        actions.pause(random.uniform(0.1, 0.3))
                    actions.perform()
                
                # 等待确保内容已经输入
                time.sleep(2)
                
                # 使用更精确的选择器定位提交按钮
                submit_button = WebDriverWait(driver, 30).until(
                 EC.element_to_be_clickable((By.XPATH, "//button[contains(@class, 'submit') and contains(@class, 'btn') and contains(text(), '发布评论')]"))
                )
                # 确保按钮可见
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", submit_button)
                time.sleep(0.5)
                # 使用 JavaScript 点击避免遮挡问题
                driver.execute_script("arguments[0].click();", submit_button)
                
                print(f"已在帖子 {post_url} 中完成评论")
                comment_count += 1
                consecutive_failures = 0  # 重置连续失败计数器
                
                # 随机等待 1-2 分钟后处理下一个帖子
                wait_minutes = random.uniform(1, 2)
                print(f"等待 {wait_minutes:.1f} 分钟后继续...")
                time.sleep(wait_minutes * 60)
                
            except Exception as e:
                print(f"处理帖子时出错: {str(e)}")
                consecutive_failures += 1
                # 尝试截图分析
                try:
                    screenshot_path = f"comment_error_{i}.png"
                    driver.save_screenshot(screenshot_path)
                    print(f"已保存错误截图: {screenshot_path}")
                    # 只发送第一张评论错误截图，避免刷屏
                    if i == 0:
                        send_telegram_photo(screenshot_path, caption=f"❌ 评论失败截图\n帖子: {post_url}\n错误: {str(e)}")
                except:
                    pass
                
                # 尝试恢复浏览器状态（导航到一个安全页面）
                try:
                    driver.get("https://www.nodeseek.com")
                    time.sleep(2)
                except:
                    print("⚠️ 浏览器状态可能已崩溃")
                    break
                continue
                
        print("评论任务完成")
        return comment_count
                
    except Exception as e:
        print(f"NodeSeek评论出错: {str(e)}")
        print("详细错误信息:")
        # 尝试截图分析
        try:
            screenshot_path = "comment_main_error.png"
            driver.save_screenshot(screenshot_path)
            send_telegram_photo(screenshot_path, caption=f"❌ 评论任务致命错误\n错误: {str(e)}")
        except:
            pass
            
        traceback.print_exc()
        return comment_count


def run_for_account(cookie_str, account_index):
    """为单个账号执行任务"""
    result = {
        "sign_in": "failed",
        "reward": "0",
        "comments": 0,
        "error": None,
        "egress_mode": None,
        "failure_class": None,
    }

    print(f"\n{'='*50}")
    print(f"开始处理账号 {account_index + 1}")
    print(f"{'='*50}")

    egress_candidates = build_egress_candidates()
    last_attempt = build_attempt_result(LOGIN_STATUS_UNKNOWN_PAGE, '未开始尝试', None)

    for attempt_index, egress_mode in enumerate(egress_candidates, start=1):
        driver = None
        proxy_runtime = None
        print(f"🚀 第 {attempt_index}/{len(egress_candidates)} 次尝试，出口={describe_egress_mode(egress_mode)}")
        try:
            driver, proxy_runtime, bootstrap_result = setup_driver_and_cookies(cookie_str, account_index, egress_mode)
            result['egress_mode'] = egress_mode
            result['failure_class'] = bootstrap_result['status_code']
            last_attempt = bootstrap_result

            if not driver:
                result['error'] = bootstrap_result['reason'] or '浏览器初始化失败'
                if should_retry_with_next_egress(bootstrap_result['status_code']) and attempt_index < len(egress_candidates):
                    print('⚠️ 当前出口初始化失败，准备切换到下一个出口重试')
                    continue
                return result

            if bootstrap_result['status_code'] != LOGIN_STATUS_OK:
                result['error'] = bootstrap_result['reason'] or '首页引导失败'
                if should_retry_with_next_egress(bootstrap_result['status_code']) and attempt_index < len(egress_candidates):
                    print('⚠️ 首页引导阶段命中可重试失败，准备切换出口重试')
                    continue
                return result

            login_status, login_reason = check_login_status(driver)
            result['failure_class'] = login_status
            if login_status != LOGIN_STATUS_OK:
                result['error'] = login_reason or '登录状态异常'
                if should_retry_with_next_egress(login_status) and attempt_index < len(egress_candidates):
                    print('⚠️ 登录检测阶段命中可重试失败，准备切换出口重试')
                    continue
                return result

            mark_profile_success()
            result['failure_class'] = None
            result['error'] = None

            status, reward = click_sign_icon(driver)
            result['sign_in'] = status
            result['reward'] = reward
            if status in ('success', 'already'):
                mark_profile_success()

            result['comments'] = nodeseek_comment(driver)
            return result
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            if proxy_runtime:
                proxy_runtime.stop()

    result['error'] = last_attempt['reason'] or '所有出口均尝试失败'
    result['failure_class'] = last_attempt['status_code']
    result['egress_mode'] = last_attempt['egress_mode']
    return result


if __name__ == "__main__":
    print("开始执行 NodeSeek 自动任务...")
    
    # 检查配置
    print(
        f"当前配置: NS_RANDOM={config.ns_random}, HEADLESS={config.headless}, "
        f"PROXY={mask_proxy_url(config.proxy_url)}, EGRESS_MODE={config.egress_mode}, "
        f"CF_WAIT={config.cf_wait_seconds}s, PROFILE_DIR={config.chrome_profile_dir}"
    )
    clear_profile_success_marker()
    if config.account_count == 0:
        print("未配置 Cookie，退出")
        send_telegram_message("❌ <b>NodeSeek 自动任务失败</b>\n\n未配置 NS_COOKIE 环境变量")
        exit(1)
    
    print(f"检测到 {config.account_count} 个账号")
    
    # 随机延迟执行
    delay_seconds = config.get_random_delay_seconds()
    if delay_seconds > 0:
        delay_minutes = delay_seconds / 60
        print(f"随机延迟执行: 等待 {delay_minutes:.1f} 分钟...")
        time.sleep(delay_seconds)
    
    # 为每个账号执行任务
    all_results = []
    for i, cookie in enumerate(config.cookies):
        result = run_for_account(cookie, i)
        all_results.append(result)
    
    print(f"\n{'='*50}")
    print("所有账号任务执行完成")
    print(f"{'='*50}")
    
    # 获取北京时间 (UTC+8)
    beijing_tz = timezone(timedelta(hours=8))
    beijing_time = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")
    
    # 构建汇报消息
    if config.account_count == 1:
        # 单账号汇报
        r = all_results[0]
        if r["error"]:
            egress_label = describe_egress_mode(r["egress_mode"]) if r["egress_mode"] else '未知'
            failure_label = r['failure_class'] or 'unknown'
            report_message = f"""<b>NodeSeek 每日简报</b>
━━━━━━━━━━━━━━━
❌ <b>任务失败</b>
━━━━━━━━━━━━━━━
🌐 <b>出口</b>: {egress_label}
🧭 <b>分类</b>: {failure_label}
⚠️ <b>错误</b>: {r["error"]}
🕒 {beijing_time}"""
        else:
            if r["sign_in"] == "success":
                sign_status = "✅ 成功"
                sign_result = "已签到"
            elif r["sign_in"] == "already":
                sign_status = "✅ 成功"
                sign_result = "今日已签"
            else:
                sign_status = "❌ 失败"
                sign_result = "签到失败"
                
            egress_label = describe_egress_mode(r["egress_mode"]) if r["egress_mode"] else '未知'
            report_message = f"""<b>NodeSeek 每日简报</b>
━━━━━━━━━━━━━━━
👤 <b>账号</b>: 账号 1
🌐 <b>出口</b>: {egress_label}
🏆 <b>奖励</b>: <b>{r["reward"]}</b> 🍗
💬 <b>评论</b>: {r["comments"]} 条
━━━━━━━━━━━━━━━
{sign_status} <b>状态</b>: {sign_result}
🕒 {beijing_time}"""
    else:
        # 多账号汇报（极简科技风）
        account_lines = []
        for i, r in enumerate(all_results):
            if r["error"]:
                egress_label = describe_egress_mode(r['egress_mode']) if r['egress_mode'] else '未知'
                failure_label = r['failure_class'] or 'unknown'
                account_lines.append(f"\u274c \u8d26\u53f7{i+1}: {egress_label} | {failure_label} | {r['error']}")
            else:
                if r["sign_in"] in ("success", "already"):
                    sign = f"\u2705 +{r['reward']}\ud83c\udf57"
                else:
                    sign = "\u274c"
                egress_label = describe_egress_mode(r['egress_mode']) if r['egress_mode'] else '未知'
                account_lines.append(f"\ud83d\udc64 \u8d26\u53f7{i+1}: {egress_label} | {sign} | \ud83d\udcac {r['comments']}\u6761")
        accounts_str = "\n".join(account_lines)
        report_message = f"""<b>NodeSeek \u6bcf\u65e5\u7b80\u62a5</b>
\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501
{accounts_str}
\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501
\ud83d\udd52 {beijing_time}"""
    
    send_telegram_message(report_message)
