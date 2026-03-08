from __future__ import annotations

import json
import os
import random
import re
import shutil
import tempfile
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlparse

import requests

SCRAPLING_IMPORT_ERROR: Exception | None = None
try:
    from scrapling.fetchers import StealthySession
except Exception as exc:  # pragma: no cover - 允许未装依赖时做静态校验
    StealthySession = Any  # type: ignore[assignment]
    SCRAPLING_IMPORT_ERROR = exc


HOME_URL = "https://www.nodeseek.com"
LOGIN_URL = f"{HOME_URL}/signIn.html"
BOARD_URL = f"{HOME_URL}/board"
DEFAULT_COMMENT_URL = f"{HOME_URL}/categories/trade"
ARTIFACT_ROOT = Path("artifacts")
STATE_SUCCESS_MARKER = ".ns_browser_state_ok"

EGRESS_PROXY = "proxy"
EGRESS_DIRECT = "direct"

LOGIN_STATUS_OK = "ok"
LOGIN_STATUS_CF_CHALLENGE = "cf_challenge"
LOGIN_STATUS_LOGIN_PAGE = "login_page"
LOGIN_STATUS_COOKIE_INVALID = "cookie_invalid"
LOGIN_STATUS_UNKNOWN_PAGE = "unknown_page"
LOGIN_STATUS_EGRESS_FAILED = "egress_failed"
LOGIN_STATUS_LOGIN_FAILED = "login_failed"

COMMENT_POOL = [
    "bd",
    "绑定",
    "帮顶",
    ":xhj007: BD",
    "好价",
    "过来看一下",
    ":xhj025: 嚯",
    "咕噜咕噜",
    "可以",
    ":xhj003: 可以",
    "还可以",
    "楼下",
    ":xhj010: 顶",
    "bd一下",
    ":xhj027: 哦",
]

CHALLENGE_TITLES = ("Just a moment", "Attention Required")
CHALLENGE_KEYWORDS = (
    "Performing security verification",
    "This website uses a security service",
    "verifies you are not a bot",
    "Cloudflare",
)


def parse_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def parse_extra_headers(raw_value: str) -> dict[str, str]:
    raw_value = raw_value.strip()
    if not raw_value:
        return {}

    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"NS_EXTRA_HEADERS 不是合法 JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("NS_EXTRA_HEADERS 必须是 JSON 对象")

    headers: dict[str, str] = {}
    for key, value in payload.items():
        header_name = str(key).strip()
        if not header_name or value is None:
            continue

        header_value = str(value).strip()
        if not header_value:
            continue

        headers[header_name] = header_value

    return headers


def parse_account_values(raw_value: str) -> list[str]:
    return [item.strip() for item in raw_value.split("|") if item.strip()]


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    url: str
    title: str
    body_text: str


@dataclass(frozen=True, slots=True)
class LoginEvidence:
    snapshot: PageSnapshot
    avatar_count: int
    login_button_count: int
    login_link_count: int = 0
    register_link_count: int = 0


@dataclass(slots=True)
class Config:
    cookies: list[str]
    usernames: list[str]
    passwords: list[str]
    ns_random: bool
    headless: bool
    tg_bot_token: str | None
    tg_chat_id: str | None
    comment_url: str
    delay_min: int
    delay_max: int
    proxy_url: str
    egress_mode: str
    cf_wait_seconds: int
    cf_login_retries: int
    proxy_insecure: bool
    skip_comments: bool
    browser_state_dir: str
    user_agent: str | None
    extra_headers: dict[str, str]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        source = env or os.environ
        raw_cookie = source.get("NS_COOKIE", "") or ""
        cookies = parse_account_values(raw_cookie)
        usernames = parse_account_values(source.get("NS_USERNAME", "") or "")
        passwords = parse_account_values(source.get("NS_PASSWORD", "") or "")
        comment_url_env = (source.get("NS_COMMENT_URL", "") or "").strip()
        egress_mode = ((source.get("NS_EGRESS_MODE", "auto") or "auto").strip().lower() or "auto")
        if egress_mode not in {"auto", "proxy", "direct"}:
            egress_mode = "auto"

        def bool_from_source(name: str, default: bool) -> bool:
            raw = source.get(name)
            if raw is None or not raw.strip():
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        def int_from_source(name: str, default: int) -> int:
            raw = source.get(name)
            if raw is None or not raw.strip():
                return default
            try:
                return int(raw.strip())
            except ValueError:
                return default

        try:
            extra_headers = parse_extra_headers((source.get("NS_EXTRA_HEADERS", "") or "").strip())
        except ValueError as exc:
            print(f"⚠️ {exc}")
            extra_headers = {}

        return cls(
            cookies=cookies,
            usernames=usernames,
            passwords=passwords,
            ns_random=bool_from_source("NS_RANDOM", True),
            headless=bool_from_source("NS_HEADLESS", True),
            tg_bot_token=(source.get("TG_BOT_TOKEN") or "").strip() or None,
            tg_chat_id=(source.get("TG_CHAT_ID") or "").strip() or None,
            comment_url=comment_url_env or DEFAULT_COMMENT_URL,
            delay_min=int_from_source("NS_DELAY_MIN", 0),
            delay_max=int_from_source("NS_DELAY_MAX", 10),
            proxy_url=(source.get("NS_PROXY_URL", "") or "").strip(),
            egress_mode=egress_mode,
            cf_wait_seconds=max(0, int_from_source("NS_CF_WAIT_SECONDS", 30)),
            cf_login_retries=max(1, int_from_source("NS_CF_LOGIN_RETRIES", 2)),
            proxy_insecure=bool_from_source("NS_PROXY_INSECURE", True),
            skip_comments=bool_from_source("NS_SKIP_COMMENTS", False),
            browser_state_dir=((source.get("NS_BROWSER_STATE_DIR", ".browser-state") or ".browser-state").strip() or ".browser-state"),
            user_agent=(source.get("NS_USER_AGENT", "") or "").strip() or None,
            extra_headers=extra_headers,
        )

    @property
    def account_count(self) -> int:
        return max(len(self.cookies), len(self.usernames), len(self.passwords))

    def get_cookie(self, account_index: int) -> str:
        if account_index >= len(self.cookies):
            return ""
        return self.cookies[account_index]

    def get_credentials(self, account_index: int) -> tuple[str, str]:
        username = self.usernames[account_index] if account_index < len(self.usernames) else ""
        password = self.passwords[account_index] if account_index < len(self.passwords) else ""
        return username, password

    def get_random_delay_seconds(self) -> int:
        if self.delay_max <= 0:
            return 0
        actual_min = min(self.delay_min, self.delay_max)
        actual_max = max(self.delay_min, self.delay_max)
        return random.randint(actual_min, actual_max) * 60


config = Config.from_env()


def get_state_root_dir() -> Path:
    return Path(config.browser_state_dir)


def build_account_state_dir(account_index: int) -> Path:
    return get_state_root_dir() / f"account-{account_index + 1}"


def build_login_temp_root_dir() -> Path:
    path = get_state_root_dir() / ".login-temp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_clean_login_state_dir(account_index: int) -> Path:
    return Path(
        tempfile.mkdtemp(
            prefix=f"account-{account_index + 1}-clean-",
            dir=str(build_login_temp_root_dir()),
        )
    )


def get_state_success_marker_path() -> Path:
    return get_state_root_dir() / STATE_SUCCESS_MARKER


def clear_state_success_marker() -> None:
    marker_path = get_state_success_marker_path()
    try:
        marker_path.unlink(missing_ok=True)
    except TypeError:
        if marker_path.exists():
            marker_path.unlink()


def mark_state_success() -> None:
    marker_path = get_state_success_marker_path()
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text("ok\n", encoding="utf-8")


def build_account_artifact_dir(account_index: int) -> Path:
    path = ARTIFACT_ROOT / f"account-{account_index + 1}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_account_artifact_path(account_index: int, filename: str) -> Path:
    return build_account_artifact_dir(account_index) / filename


def cleanup_temp_state_dir(state_dir: Path | None) -> None:
    if state_dir is None:
        return
    try:
        shutil.rmtree(state_dir, ignore_errors=True)
    except Exception as exc:
        print(f"清理临时浏览器状态目录失败: {exc}")


def persist_browser_state(source_dir: Path, target_dir: Path) -> None:
    if source_dir.resolve() == target_dir.resolve():
        return

    ignore_patterns = shutil.ignore_patterns("Singleton*", "DevToolsActivePort")
    target_dir.mkdir(parents=True, exist_ok=True)
    for item in source_dir.iterdir():
        if item.name in {"SingletonLock", "SingletonSocket", "SingletonCookie", "DevToolsActivePort"}:
            continue

        destination = target_dir / item.name
        try:
            if item.is_dir():
                shutil.copytree(item, destination, dirs_exist_ok=True, ignore=ignore_patterns)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, destination)
        except Exception as exc:
            print(f"同步浏览器状态失败: {item} -> {destination}: {exc}")


def clear_login_attempt_artifacts(account_index: int) -> None:
    for filename in (
        "login_check_failed.png",
        "login_recheck.png",
        "login_attempt_summary.json",
        "login_stage_1_filled.png",
        "login_stage_2_turnstile.png",
        "login_stage_3_failed_live.png",
    ):
        artifact_path = build_account_artifact_path(account_index, filename)
        try:
            artifact_path.unlink(missing_ok=True)
        except TypeError:
            if artifact_path.exists():
                artifact_path.unlink()


def build_attempt_result(status_code: str, reason: str | None = None, egress_mode: str | None = None) -> dict[str, str | None]:
    return {
        "status_code": status_code,
        "reason": reason,
        "egress_mode": egress_mode,
    }


def is_https_proxy_url(proxy_url: str) -> bool:
    if not proxy_url:
        return False
    try:
        return (urlparse(proxy_url).scheme or "").lower() == "https"
    except Exception:
        return False


def should_ignore_proxy_tls_errors(egress_mode: str, proxy_url: str | None = None) -> bool:
    if egress_mode != EGRESS_PROXY:
        return False
    upstream_proxy = proxy_url if proxy_url is not None else config.proxy_url
    return bool(config.proxy_insecure and is_https_proxy_url(upstream_proxy or ""))


def build_proxy_failure_reason(exc: Exception, egress_mode: str) -> str:
    reason = f"浏览器初始化或首页引导失败: {exc}"
    if "ERR_PROXY_CERTIFICATE_INVALID" not in str(exc):
        return reason

    proxy_label = mask_proxy_url(config.proxy_url)
    if egress_mode != EGRESS_PROXY:
        return f"{reason}（检测到代理证书错误，但当前出口不是代理；代理={proxy_label}）"
    if not is_https_proxy_url(config.proxy_url):
        return f"{reason}（当前代理不是 HTTPS 代理；代理={proxy_label}）"
    if config.proxy_insecure:
        return f"{reason}（已启用 NS_PROXY_INSECURE=true，但代理证书仍不被 Chromium 接受；代理={proxy_label}）"
    return f"{reason}（当前为 HTTPS 代理，且 NS_PROXY_INSECURE=false；可改用有效证书代理，或显式开启 NS_PROXY_INSECURE；代理={proxy_label}）"


def mask_proxy_url(proxy_url: str) -> str:
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


def build_egress_candidates(egress_mode: Optional[str] = None, proxy_url: Optional[str] = None) -> list[str]:
    mode = egress_mode if egress_mode is not None else config.egress_mode
    upstream_proxy = proxy_url if proxy_url is not None else config.proxy_url
    if mode == EGRESS_PROXY:
        return [EGRESS_PROXY]
    if mode == EGRESS_DIRECT:
        return [EGRESS_DIRECT]
    if upstream_proxy:
        return [EGRESS_PROXY, EGRESS_DIRECT]
    return [EGRESS_DIRECT]


def should_retry_with_next_egress(status_code: str) -> bool:
    return status_code in {LOGIN_STATUS_CF_CHALLENGE, LOGIN_STATUS_EGRESS_FAILED}


def describe_egress_mode(egress_mode: str | None) -> str:
    if egress_mode == EGRESS_PROXY:
        return "代理"
    if egress_mode == EGRESS_DIRECT:
        return "直连"
    return "未知"


def browser_timeout_ms() -> int:
    if config.cf_wait_seconds <= 0:
        return 30_000
    return max(60_000, config.cf_wait_seconds * 1000)


def solve_cloudflare_enabled() -> bool:
    return config.cf_wait_seconds > 0


def should_retry_clean_login(snapshot: dict[str, Any], attempt_result: dict[str, str | None]) -> bool:
    if attempt_result.get("status_code") != LOGIN_STATUS_CF_CHALLENGE:
        return False
    if int(snapshot.get("sign_in_request_count", 0) or 0) > 0:
        return False
    if int(snapshot.get("challenge_pat_401_count", 0) or 0) > 0:
        return True
    return bool(snapshot.get("turnstile_frame_found") or snapshot.get("captcha_container_present"))


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.split())


def build_cookie_payloads(cookie_str: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for cookie_item in cookie_str.split(";"):
        cookie_item = cookie_item.strip()
        if not cookie_item or "=" not in cookie_item:
            continue

        name, value = cookie_item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue

        if name == "cf_clearance":
            payloads.append(
                {
                    "name": name,
                    "value": value,
                    "domain": ".nodeseek.com",
                    "path": "/",
                    "secure": True,
                }
            )
        else:
            payloads.append(
                {
                    "name": name,
                    "value": value,
                    "url": HOME_URL,
                }
            )
    return payloads


def parse_cookie_string(cookie_str: str) -> list[dict[str, Any]]:
    return build_cookie_payloads(cookie_str)


def seed_session_cookies(session: StealthySession, cookie_str: str) -> int:
    payloads = build_cookie_payloads(cookie_str)
    if not payloads:
        return 0

    context = getattr(session, "context", None)
    if context is None:
        raise RuntimeError("Scrapling 会话未暴露浏览器上下文，无法预注入 Cookie")

    context.clear_cookies()
    context.add_cookies(payloads)
    return len(payloads)


def safe_title(page: Any) -> str:
    try:
        return (page.title() or "").strip()
    except Exception:
        return ""


def safe_body_text(page: Any) -> str:
    try:
        return normalize_text(page.locator("body").inner_text(timeout=5_000))
    except Exception:
        return ""


def safe_count(page: Any, selector: str) -> int:
    try:
        return page.locator(selector).count()
    except Exception:
        return 0


def save_page_screenshot(page: Any, path: Path) -> str | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception as exc:
        print(f"保存截图失败: {exc}")
        return None


def save_login_stage_screenshot(
    page: Any,
    snapshot: dict[str, Any],
    account_index: int,
    filename: str,
    stage_label: str,
) -> str | None:
    # 登录阶段的证据图优先保留现场页，避免后续补抓页覆盖现场状态。
    screenshot_path = build_account_artifact_path(account_index, filename)
    screenshot = save_page_screenshot(page, screenshot_path)
    if screenshot:
        stage_screenshots = snapshot.setdefault("stage_screenshots", {})
        if isinstance(stage_screenshots, dict):
            stage_screenshots[stage_label] = screenshot
        snapshot["screenshot_path"] = screenshot
        print(f"已保存登录阶段截图[{stage_label}]: {screenshot}")
    return screenshot


def sanitize_login_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    stage_screenshots = snapshot.get("stage_screenshots")
    body_text = snapshot.get("body_text", "") or ""
    return {
        "login_state_mode": snapshot.get("login_state_mode"),
        "auth_fingerprint_mode": snapshot.get("auth_fingerprint_mode"),
        "status_code": snapshot.get("status_code"),
        "reason": snapshot.get("reason"),
        "url": snapshot.get("url"),
        "title": snapshot.get("title"),
        "body_preview": body_text[:300],
        "turnstile_frame_found": snapshot.get("turnstile_frame_found"),
        "turnstile_token_present": snapshot.get("turnstile_token_present"),
        "captcha_container_present": snapshot.get("captcha_container_present"),
        "sign_in_request_count": snapshot.get("sign_in_request_count"),
        "sign_in_response_status": snapshot.get("sign_in_response_status"),
        "challenge_pat_401_count": snapshot.get("challenge_pat_401_count"),
        "embedded_solver_attempts": snapshot.get("embedded_solver_attempts"),
        "used_clean_state": snapshot.get("used_clean_state"),
        "live_failure_screenshot_path": snapshot.get("live_failure_screenshot_path"),
        "stage_screenshots": stage_screenshots if isinstance(stage_screenshots, dict) else {},
    }


def write_login_attempt_summary(account_index: int, egress_mode: str, snapshots: Sequence[dict[str, Any]]) -> str | None:
    if not snapshots:
        return None

    summary_path = build_account_artifact_path(account_index, "login_attempt_summary.json")
    payload = {
        "account_index": account_index + 1,
        "egress_mode": egress_mode,
        "attempt_count": len(snapshots),
        "attempts": [sanitize_login_snapshot(snapshot) for snapshot in snapshots],
    }
    try:
        summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入登录尝试摘要: {summary_path}")
        return str(summary_path)
    except Exception as exc:
        print(f"写入登录尝试摘要失败: {exc}")
        return None


def focus_page_area(page: Any, selector: str) -> None:
    try:
        locator = page.locator(selector)
        if locator.count() == 0:
            return
        locator.first.scroll_into_view_if_needed(timeout=2_000)
        page.wait_for_timeout(300)
    except Exception:
        return


def count_auth_controls(page: Any) -> dict[str, int]:
    script = r"""
    () => {
        const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim()
        const isVisible = (element) => !!(element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length))
        const allElements = Array.from(document.querySelectorAll('a, button, span'))
        const loginLinkCount = allElements.filter((element) => {
            if (!isVisible(element)) return false
            const text = normalize(element.textContent)
            const href = normalize(element.getAttribute('href'))
            return text === '登录' || href.includes('/signIn') || href.includes('/login')
        }).length
        const registerLinkCount = allElements.filter((element) => {
            if (!isVisible(element)) return false
            const text = normalize(element.textContent)
            const href = normalize(element.getAttribute('href'))
            return text === '注册' || href.includes('/register')
        }).length
        const loginButtonCount = allElements.filter((element) => {
            if (!isVisible(element)) return false
            return normalize(element.textContent) === '登录'
        }).length
        return {
            login_button_count: loginButtonCount,
            login_link_count: loginLinkCount,
            register_link_count: registerLinkCount,
        }
    }
    """
    try:
        result = page.evaluate(script)
        if isinstance(result, dict):
            return {
                'login_button_count': int(result.get('login_button_count', 0) or 0),
                'login_link_count': int(result.get('login_link_count', 0) or 0),
                'register_link_count': int(result.get('register_link_count', 0) or 0),
            }
    except Exception:
        pass
    return {
        'login_button_count': 0,
        'login_link_count': 0,
        'register_link_count': 0,
    }


def update_snapshot_from_page(page: Any, snapshot: dict[str, Any], include_login_signals: bool = False) -> None:
    snapshot["url"] = getattr(page, "url", "") or ""
    snapshot["title"] = safe_title(page)
    snapshot["body_text"] = safe_body_text(page)
    if include_login_signals:
        snapshot["avatar_count"] = safe_count(page, ".avatar, .nsk-user-avatar, [class*='avatar']")
        auth_counts = count_auth_controls(page)
        snapshot["login_button_count"] = auth_counts["login_button_count"]
        snapshot["login_link_count"] = auth_counts["login_link_count"]
        snapshot["register_link_count"] = auth_counts["register_link_count"]


def is_cloudflare_snapshot(snapshot: dict[str, Any]) -> bool:
    title = snapshot.get("title", "") or ""
    body_text = snapshot.get("body_text", "") or ""
    if any(keyword in title for keyword in CHALLENGE_TITLES):
        return True
    return any(keyword in body_text for keyword in CHALLENGE_KEYWORDS)


def evaluate_login_evidence(evidence: LoginEvidence) -> tuple[str, str | None]:
    snapshot = {
        "url": evidence.snapshot.url,
        "title": evidence.snapshot.title,
        "body_text": evidence.snapshot.body_text,
        "avatar_count": evidence.avatar_count,
        "login_button_count": evidence.login_button_count,
        "login_link_count": evidence.login_link_count,
        "register_link_count": evidence.register_link_count,
    }
    return classify_login_snapshot(snapshot)


def classify_login_snapshot(snapshot: dict[str, Any]) -> tuple[str, str | None]:
    current_url = snapshot.get("url", "") or ""
    body_text = snapshot.get("body_text", "") or ""
    avatar_count = int(snapshot.get("avatar_count", 0) or 0)
    login_button_count = int(snapshot.get("login_button_count", 0) or 0)
    login_link_count = int(snapshot.get("login_link_count", 0) or 0)
    register_link_count = int(snapshot.get("register_link_count", 0) or 0)

    if is_cloudflare_snapshot(snapshot):
        return LOGIN_STATUS_CF_CHALLENGE, "登录检测阶段遭遇 Cloudflare/风控页"

    if "/login" in current_url or "/signIn" in current_url:
        return LOGIN_STATUS_LOGIN_PAGE, "跳转到了登录页，Cookie 可能失效"

    if login_link_count > 0 and register_link_count > 0:
        return LOGIN_STATUS_COOKIE_INVALID, "页面出现登录/注册链接，Cookie 可能失效"

    if login_link_count > 0 and "登录" in body_text:
        return LOGIN_STATUS_COOKIE_INVALID, "页面仍出现登录入口，Cookie 可能失效"

    if avatar_count > 0 and login_button_count == 0 and login_link_count == 0 and register_link_count == 0:
        return LOGIN_STATUS_OK, None

    if "登录" in body_text and "注册" in body_text and "个人中心" not in body_text:
        return LOGIN_STATUS_COOKIE_INVALID, "页面出现登录/注册提示，Cookie 可能失效"

    return LOGIN_STATUS_UNKNOWN_PAGE, "未识别到登录态，可能是风控、页面结构变化或 Cookie 不匹配"


def print_login_diagnostics(reason: str, snapshot: dict[str, Any]) -> None:
    print(f"⚠️ 登录检测失败原因: {reason}")
    print(f"当前URL: {snapshot.get('url', '<unknown>') or '<unknown>'}")
    print(f"页面标题: {snapshot.get('title', '<unknown>') or '<unknown>'}")
    if snapshot.get("login_state_mode"):
        print(f"登录状态目录模式: {snapshot.get('login_state_mode')}")
    if snapshot.get("auth_fingerprint_mode"):
        print(f"认证指纹模式: {snapshot.get('auth_fingerprint_mode')}")
    body_text = snapshot.get("body_text", "") or ""
    print(f"页面预览: {body_text[:300] if body_text else '<empty>'}")
    if "turnstile_frame_found" in snapshot:
        print(f"Turnstile Frame: {snapshot.get('turnstile_frame_found')}")
    if "turnstile_token_present" in snapshot:
        print(f"Turnstile Token: {snapshot.get('turnstile_token_present')}")
    if "sign_in_request_count" in snapshot:
        print(f"登录请求次数: {snapshot.get('sign_in_request_count')}")
    if snapshot.get("sign_in_response_status") is not None:
        print(f"登录接口状态: HTTP {snapshot.get('sign_in_response_status')}")
    if snapshot.get("challenge_pat_401_count"):
        print(f"PAT 401 次数: {snapshot.get('challenge_pat_401_count')}")
    if snapshot.get("embedded_solver_attempts"):
        print(f"embedded solver 次数: {snapshot.get('embedded_solver_attempts')}")
    stage_screenshots = snapshot.get("stage_screenshots")
    if isinstance(stage_screenshots, dict):
        for stage_label, stage_path in stage_screenshots.items():
            print(f"登录阶段截图[{stage_label}]: {stage_path}")
    if snapshot.get("screenshot_path"):
        print(f"已保存登录检测截图: {snapshot['screenshot_path']}")


def capture_login_diagnostics(
    session: StealthySession,
    account_index: int,
    reason: str,
    base_snapshot: dict[str, Any],
    artifact_name: str = "login_check_failed.png",
) -> None:
    snapshot = dict(base_snapshot)
    screenshot_path = build_account_artifact_path(account_index, artifact_name)

    def action(page: Any) -> None:
        update_snapshot_from_page(page, snapshot, include_login_signals=True)
        screenshot = save_page_screenshot(page, screenshot_path)
        if screenshot:
            snapshot["screenshot_path"] = screenshot

    target_url = snapshot.get("url") or HOME_URL
    try:
        session.fetch(
            target_url,
            page_action=action,
            wait_selector="body",
            timeout=browser_timeout_ms(),
            google_search=False,
            load_dom=True,
        )
    except Exception as exc:
        print(f"补抓登录诊断页面失败: {exc}")
    print_login_diagnostics(reason, snapshot)


def send_telegram_message(message: str) -> bool:
    if not config.tg_bot_token or not config.tg_chat_id:
        print("未配置 Telegram 通知，跳过发送")
        return False

    try:
        url = f"https://api.telegram.org/bot{config.tg_bot_token}/sendMessage"
        response = requests.post(
            url,
            json={
                "chat_id": config.tg_chat_id,
                "text": message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        if response.status_code == 200:
            print("Telegram 通知发送成功")
            return True
        print(f"Telegram 通知发送失败: {response.text}")
        return False
    except Exception as exc:
        print(f"Telegram 通知发送出错: {exc}")
        return False


def send_telegram_photo(photo_path: str | Path, caption: str | None = None) -> bool:
    if not config.tg_bot_token or not config.tg_chat_id:
        return False

    try:
        url = f"https://api.telegram.org/bot{config.tg_bot_token}/sendPhoto"
        with open(photo_path, "rb") as photo:
            payload = {"chat_id": config.tg_chat_id}
            if caption:
                payload["caption"] = caption
            response = requests.post(url, data=payload, files={"photo": photo}, timeout=20)
        if response.status_code == 200:
            print("Telegram 图片发送成功")
            return True
        print(f"Telegram 图片发送失败: {response.text}")
        return False
    except Exception as exc:
        print(f"Telegram 图片发送出错: {exc}")
        return False


def create_session(
    account_index: int,
    egress_mode: str,
    *,
    user_data_dir: Path | None = None,
    use_custom_fingerprint: bool = True,
) -> StealthySession:
    if SCRAPLING_IMPORT_ERROR is not None:
        raise RuntimeError(
            "未检测到 Scrapling 运行依赖，请先执行 `pip install -r requirements.txt`，"
            "然后执行 `scrapling install`。"
        ) from SCRAPLING_IMPORT_ERROR

    if egress_mode == EGRESS_PROXY and not config.proxy_url:
        raise RuntimeError("当前出口模式要求代理，但未配置 NS_PROXY_URL")

    account_state_dir = user_data_dir or build_account_state_dir(account_index)
    account_state_dir.mkdir(parents=True, exist_ok=True)
    proxy = config.proxy_url if egress_mode == EGRESS_PROXY else None
    print(
        f"开始初始化 Scrapling 会话... 账号={account_index + 1}, "
        f"出口={describe_egress_mode(egress_mode)}, state={account_state_dir}"
    )
    if proxy:
        print(f"🌐 浏览器业务流量将通过代理: {mask_proxy_url(proxy)}")
    else:
        print("🌐 浏览器业务流量将直连")

    if use_custom_fingerprint and config.user_agent:
        print("🧪 已启用自定义 User-Agent")
    if use_custom_fingerprint and config.extra_headers:
        print(f"🧪 附加请求头: {', '.join(sorted(config.extra_headers))}")
    if not use_custom_fingerprint:
        print("🧪 认证流已切换为浏览器原生指纹，忽略自定义 UA / Headers")

    additional_args: dict[str, Any] | None = None
    extra_flags: list[str] | None = None
    if should_ignore_proxy_tls_errors(egress_mode, proxy):
        additional_args = {"ignore_https_errors": True}
        extra_flags = ["--ignore-certificate-errors"]
        print("🧪 已为 HTTPS 代理启用证书忽略（NS_PROXY_INSECURE=true）")
    elif egress_mode == EGRESS_PROXY and is_https_proxy_url(proxy or ""):
        print(f"🔒 当前 HTTPS 代理将严格校验证书（NS_PROXY_INSECURE={config.proxy_insecure}）")

    return StealthySession(
        headless=config.headless,
        solve_cloudflare=solve_cloudflare_enabled(),
        user_data_dir=str(account_state_dir.resolve()),
        network_idle=True,
        timeout=browser_timeout_ms(),
        google_search=False,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        proxy=proxy,
        load_dom=True,
        useragent=config.user_agent if use_custom_fingerprint else None,
        extra_headers=(config.extra_headers or None) if use_custom_fingerprint else None,
        additional_args=additional_args,
        extra_flags=extra_flags,
    )


def bootstrap_session(
    session: StealthySession,
    cookie_str: str,
    account_index: int,
    egress_mode: str,
) -> tuple[dict[str, Any], dict[str, str | None]]:
    if not cookie_str:
        reason = "未找到 cookie 配置"
        print(reason)
        return {}, build_attempt_result(LOGIN_STATUS_COOKIE_INVALID, reason, egress_mode)

    snapshot: dict[str, Any] = {}

    def action(page: Any) -> None:
        update_snapshot_from_page(page, snapshot, include_login_signals=True)

    try:
        seeded_cookie_count = seed_session_cookies(session, cookie_str)
        if seeded_cookie_count <= 0:
            reason = "Cookie 字符串无法解析出有效键值对"
            print(reason)
            return snapshot, build_attempt_result(LOGIN_STATUS_COOKIE_INVALID, reason, egress_mode)
        print(f"🍪 已预注入 {seeded_cookie_count} 个 Cookie 到浏览器上下文")
        session.fetch(
            HOME_URL,
            page_action=action,
            wait_selector="body",
            timeout=browser_timeout_ms(),
            google_search=False,
            load_dom=True,
        )
    except Exception as exc:
        reason = build_proxy_failure_reason(exc, egress_mode)
        print(reason)
        print(traceback.format_exc())
        return snapshot, build_attempt_result(LOGIN_STATUS_EGRESS_FAILED, reason, egress_mode)

    status_code, reason = classify_login_snapshot(snapshot)
    if status_code == LOGIN_STATUS_OK:
        print("✅ 登录状态有效")
        return snapshot, build_attempt_result(LOGIN_STATUS_OK, None, egress_mode)

    capture_login_diagnostics(session, account_index, reason or "登录检测失败", snapshot)
    return snapshot, build_attempt_result(status_code, reason, egress_mode)


def extract_login_feedback_text(page: Any) -> str:
    selectors = (
        ".msc-toast",
        ".msc-alert",
        ".el-message",
        ".message",
        ".error",
        ".alert",
    )
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if locator.count() == 0:
                continue
            text = normalize_text(locator.first.inner_text(timeout=1_000))
            if text:
                return text
        except Exception:
            continue
    return ""


def find_turnstile_frame(page: Any) -> Any | None:
    for frame in getattr(page, "frames", []) or []:
        frame_url = getattr(frame, "url", "") or ""
        if "challenges.cloudflare.com" in frame_url and "turnstile" in frame_url:
            return frame
    return None


def read_turnstile_state(page: Any) -> dict[str, Any]:
    try:
        state = page.evaluate(
            """
            () => {
                const hiddenInput = document.querySelector("input[name='cf-turnstile-response']")
                const tokenFromInput = hiddenInput?.value || ""
                let tokenFromApi = ""
                let hasTurnstileObject = false
                try {
                    hasTurnstileObject = typeof window.turnstile !== "undefined"
                    if (hasTurnstileObject && typeof window.turnstile.getResponse === "function") {
                        tokenFromApi = window.turnstile.getResponse() || ""
                    }
                } catch (error) {
                    tokenFromApi = ""
                }

                return {
                    token_from_input: tokenFromInput,
                    token_from_api: tokenFromApi,
                    token_present: Boolean(tokenFromInput || tokenFromApi),
                    has_turnstile_object: hasTurnstileObject,
                    captcha_container_present: Boolean(document.querySelector("#captcha-container")),
                }
            }
            """
        )
        if isinstance(state, dict):
            return state
    except Exception:
        pass
    return {
        "token_from_input": "",
        "token_from_api": "",
        "token_present": False,
        "has_turnstile_object": False,
        "captcha_container_present": False,
    }


def login_with_credentials(
    session: StealthySession,
    username: str,
    password: str,
    account_index: int,
    egress_mode: str,
    state_mode: str = "cached_state",
) -> tuple[dict[str, Any], dict[str, str | None]]:
    if not username or not password:
        reason = "未配置账号密码，无法执行真实登录"
        print(reason)
        return {}, build_attempt_result(LOGIN_STATUS_LOGIN_FAILED, reason, egress_mode)

    snapshot: dict[str, Any] = {
        "stage_screenshots": {},
        "login_state_mode": state_mode,
        "auth_fingerprint_mode": "browser_native",
        "used_clean_state": state_mode == "clean_state",
        "embedded_solver_attempts": 0,
    }
    network_state: dict[str, Any] = {
        "sign_in_request_count": 0,
        "sign_in_response_status": None,
        "sign_in_response_ok": None,
        "challenge_pat_401_count": 0,
    }

    def action(page: Any) -> None:
        print("开始执行真实登录流程...")
        page.wait_for_load_state("domcontentloaded", timeout=browser_timeout_ms())
        page.wait_for_timeout(2_000)
        turnstile_stage_captured = False
        embedded_solver_attempts = 0

        def sync_network_state(turnstile_state: dict[str, Any] | None = None) -> None:
            snapshot.update(network_state)
            snapshot["embedded_solver_attempts"] = embedded_solver_attempts
            if turnstile_state is not None:
                snapshot["turnstile_token_present"] = bool(turnstile_state.get("token_present"))
                snapshot["turnstile_has_object"] = bool(turnstile_state.get("has_turnstile_object"))
                snapshot["captcha_container_present"] = bool(turnstile_state.get("captcha_container_present"))

        def capture_live_failure(reason: str, status_code: str) -> None:
            sync_network_state()
            update_snapshot_from_page(page, snapshot, include_login_signals=True)
            snapshot["status_code"] = status_code
            snapshot["reason"] = reason
            live_screenshot = save_login_stage_screenshot(
                page,
                snapshot,
                account_index,
                "login_stage_3_failed_live.png",
                "失败现场",
            )
            if live_screenshot:
                snapshot["live_failure_screenshot_path"] = live_screenshot

        def maybe_capture_turnstile_stage(turnstile_state: dict[str, Any]) -> Any | None:
            nonlocal turnstile_stage_captured
            turnstile_frame = find_turnstile_frame(page)
            snapshot["turnstile_frame_found"] = turnstile_frame is not None
            snapshot["captcha_container_present"] = bool(turnstile_state.get("captcha_container_present"))
            if turnstile_stage_captured:
                return turnstile_frame
            if turnstile_frame is None and not turnstile_state.get("captcha_container_present"):
                return turnstile_frame
            focus_page_area(page, "#captcha-container")
            turnstile_screenshot = save_login_stage_screenshot(
                page,
                snapshot,
                account_index,
                "login_stage_2_turnstile.png",
                "Turnstile出现",
            )
            if turnstile_screenshot:
                turnstile_stage_captured = True
            return turnstile_frame

        def wait_for_challenge_settle() -> None:
            wait_network_idle = getattr(session, "_wait_for_networkidle", None)
            wait_page_stability = getattr(session, "_wait_for_page_stability", None)
            try:
                if callable(wait_network_idle):
                    wait_network_idle(page, timeout=5_000)
            except Exception:
                page.wait_for_timeout(1_000)
            try:
                if callable(wait_page_stability):
                    wait_page_stability(page, True, False)
            except Exception:
                page.wait_for_timeout(500)

        def click_turnstile_box(turnstile_frame: Any | None) -> bool:
            strategies: list[tuple[str, Any | None, bool]] = []
            if turnstile_frame is not None:
                try:
                    frame_element = turnstile_frame.frame_element()
                    strategies.append(("iframe_box", frame_element.bounding_box(), True))
                except Exception:
                    pass
                try:
                    turnstile_frame.locator("label.cb-lb").first.hover(timeout=5_000)
                    page.wait_for_timeout(300)
                    turnstile_frame.locator("label.cb-lb").first.click(timeout=5_000)
                    snapshot["embedded_solver_last_strategy"] = "frame_label"
                    return True
                except Exception:
                    pass
                try:
                    turnstile_frame.locator("input[type='checkbox']").first.click(timeout=5_000)
                    snapshot["embedded_solver_last_strategy"] = "frame_checkbox"
                    return True
                except Exception:
                    pass

            for selector in ("#captcha-container iframe", "#captcha-container", "#cf_turnstile", "#cf-turnstile", ".turnstile"):
                try:
                    locator = page.locator(selector)
                    if locator.count() == 0:
                        continue
                    strategies.append((selector, locator.last.bounding_box(), False))
                except Exception:
                    continue

            for strategy_name, bbox, requires_focus in strategies:
                if not bbox:
                    continue
                try:
                    click_x = float(bbox["x"]) + min(max(float(bbox["width"]) * 0.18, 20.0), 32.0)
                    click_y = float(bbox["y"]) + min(max(float(bbox["height"]) * 0.5, 20.0), 32.0)
                    if requires_focus:
                        page.mouse.move(click_x - 10, click_y - 6, steps=12)
                    else:
                        page.mouse.move(click_x - 8, click_y - 4, steps=10)
                    page.mouse.move(click_x, click_y, steps=8)
                    page.mouse.click(click_x, click_y, delay=random.randint(120, 220), button="left")
                    snapshot["embedded_solver_last_strategy"] = strategy_name
                    return True
                except Exception:
                    continue
            return False

        def run_embedded_turnstile_solver(turnstile_state: dict[str, Any]) -> dict[str, Any]:
            nonlocal embedded_solver_attempts
            max_solver_attempts = max(1, config.cf_login_retries)
            current_state = turnstile_state
            while embedded_solver_attempts < max_solver_attempts:
                if current_state.get("token_present") or network_state["sign_in_request_count"]:
                    break
                turnstile_frame = maybe_capture_turnstile_stage(current_state)
                if turnstile_frame is None and not current_state.get("captcha_container_present"):
                    break
                embedded_solver_attempts += 1
                snapshot["embedded_solver_attempts"] = embedded_solver_attempts
                print(f"🧩 开始执行 embedded Turnstile 求解，第 {embedded_solver_attempts}/{max_solver_attempts} 次")
                if not click_turnstile_box(turnstile_frame):
                    break
                wait_for_challenge_settle()
                page.wait_for_timeout(1_000)
                current_state = read_turnstile_state(page)
                sync_network_state(current_state)
            return current_state

        def on_request(request: Any) -> None:
            request_url = getattr(request, "url", "") or ""
            if "/api/account/signIn" in request_url:
                network_state["sign_in_request_count"] = int(network_state["sign_in_request_count"] or 0) + 1

        def on_response(response: Any) -> None:
            response_url = getattr(response, "url", "") or ""
            response_status = getattr(response, "status", None)
            if "/api/account/signIn" in response_url:
                network_state["sign_in_response_status"] = response_status
                if response_status is not None:
                    network_state["sign_in_response_ok"] = int(response_status) < 400
            if response_status == 401 and (
                "private-access-token" in response_url
                or "challenge-platform" in response_url
                or "pat" in response_url.lower()
            ):
                network_state["challenge_pat_401_count"] = int(network_state["challenge_pat_401_count"] or 0) + 1

        page.on("request", on_request)
        page.on("response", on_response)

        username_input = page.locator("#stacked-email")
        password_input = page.locator("#stacked-password")
        login_button = page.locator("xpath=//button[contains(text(), '登录')]")

        if username_input.count() == 0 or password_input.count() == 0 or login_button.count() == 0:
            capture_live_failure("登录页结构变化，未找到账号密码输入框或登录按钮", LOGIN_STATUS_LOGIN_FAILED)
            return

        username_input.first.click(timeout=3_000)
        username_input.first.fill("")
        username_input.first.type(username, delay=80)
        password_input.first.click(timeout=3_000)
        password_input.first.fill("")
        password_input.first.type(password, delay=100)
        page.wait_for_timeout(500)
        focus_page_area(page, "#stacked-email")
        save_login_stage_screenshot(
            page,
            snapshot,
            account_index,
            "login_stage_1_filled.png",
            "账号密码已填",
        )

        # NodeSeek 登录按钮不会直接提交表单，而是先渲染 Turnstile，
        # 等回调拿到 token 后再自动 POST /api/account/signIn。
        login_button.first.hover(timeout=3_000)
        page.wait_for_timeout(300)
        login_button.first.click(timeout=5_000)
        page.wait_for_timeout(1_500)

        turnstile_state = read_turnstile_state(page)
        maybe_capture_turnstile_stage(turnstile_state)
        turnstile_state = run_embedded_turnstile_solver(turnstile_state)

        token_wait_seconds = max(config.cf_wait_seconds, 10)
        for _ in range(token_wait_seconds):
            page.wait_for_timeout(1_000)
            turnstile_state = read_turnstile_state(page)
            maybe_capture_turnstile_stage(turnstile_state)
            if turnstile_state.get("token_present") or network_state["sign_in_request_count"]:
                break
            turnstile_state = run_embedded_turnstile_solver(turnstile_state)

        sync_network_state(turnstile_state)

        if not turnstile_state.get("token_present") and not network_state["sign_in_request_count"]:
            if network_state["challenge_pat_401_count"]:
                capture_live_failure(
                    "Turnstile 未通过，PAT/Challenge 请求返回 401，登录请求没有真正发出",
                    LOGIN_STATUS_CF_CHALLENGE,
                )
            else:
                capture_live_failure(
                    "Turnstile 未产出 token，登录请求没有真正发出",
                    LOGIN_STATUS_CF_CHALLENGE,
                )
            return

        if network_state["sign_in_response_status"] is not None and int(network_state["sign_in_response_status"]) >= 400:
            capture_live_failure(
                f"登录接口返回 HTTP {network_state['sign_in_response_status']}",
                LOGIN_STATUS_LOGIN_FAILED,
            )
            return

        try:
            page.wait_for_url(f"{HOME_URL}/**", timeout=10_000)
        except Exception:
            page.wait_for_timeout(3_000)

        update_snapshot_from_page(page, snapshot, include_login_signals=True)
        feedback = extract_login_feedback_text(page)
        if feedback:
            snapshot["feedback"] = feedback

        status_code, reason = classify_login_snapshot(snapshot)
        snapshot["status_code"] = status_code
        snapshot["reason"] = reason
        if status_code == LOGIN_STATUS_OK:
            return
        if feedback:
            snapshot["status_code"] = LOGIN_STATUS_LOGIN_FAILED
            snapshot["reason"] = feedback
        capture_live_failure(snapshot["reason"], snapshot["status_code"])

    try:
        session.fetch(
            LOGIN_URL,
            page_action=action,
            wait_selector="body",
            timeout=browser_timeout_ms(),
            google_search=False,
            load_dom=True,
        )
    except Exception as exc:
        reason = f"真实登录流程执行失败: {exc}"
        print(reason)
        print(traceback.format_exc())
        return snapshot, build_attempt_result(LOGIN_STATUS_EGRESS_FAILED, reason, egress_mode)

    status_code = str(snapshot.get("status_code") or LOGIN_STATUS_UNKNOWN_PAGE)
    reason = snapshot.get("reason") or "真实登录后仍未识别到登录态"
    if status_code == LOGIN_STATUS_OK:
        print("✅ 真实登录成功")
        return snapshot, build_attempt_result(LOGIN_STATUS_OK, None, egress_mode)

    if snapshot.get("live_failure_screenshot_path"):
        print_login_diagnostics(reason, snapshot)
    else:
        capture_login_diagnostics(session, account_index, reason, snapshot, artifact_name="login_recheck.png")
    return snapshot, build_attempt_result(status_code, reason, egress_mode)


def parse_reward_from_text(text: str) -> str:
    match = re.search(r"获得\s*(\d+)\s*鸡腿|鸡腿\s*(\d+)\s*个|踩到鸡腿\s*(\d+)\s*个|得鸡腿(\d+)个", text)
    if match:
        return match.group(1) or match.group(2) or match.group(3) or match.group(4)
    match2 = re.search(r"(\d+)\s*(?:个?\s*鸡腿|鸡腿)", text)
    if match2:
        return match2.group(1)
    return "未知"


def choose_sign_button_index(button_texts: Sequence[str], prefer_random: bool | None = None) -> int | None:
    use_random = config.ns_random if prefer_random is None else prefer_random
    for idx, text in enumerate(button_texts):
        if use_random and "手气" in text:
            return idx
        if not use_random and ("鸡腿" in text or "x 5" in text or "x5" in text):
            return idx
    if button_texts:
        return 0
    return None


def pick_comment_targets(post_urls: Sequence[str], rng: Any = random) -> list[str]:
    deduped = list(dict.fromkeys(post_urls))
    if not deduped:
        return []
    selected_count = rng.randint(4, 7)
    return rng.sample(deduped, min(selected_count, len(deduped)))


def pick_global_sign_button(page: Any) -> tuple[Any | None, str]:
    preferred_selectors = []
    if config.ns_random:
        preferred_selectors.append("xpath=//button[contains(text(), '手气')]")
        preferred_selectors.append("xpath=//button[contains(text(), '鸡腿')]")
    else:
        preferred_selectors.append("xpath=//button[contains(text(), '鸡腿') or contains(text(), 'x 5')]")
        preferred_selectors.append("xpath=//button[contains(text(), '手气')]")
    preferred_selectors.append("xpath=//button[contains(text(), '鸡腿') or contains(text(), '手气')]")

    for selector in preferred_selectors:
        locator = page.locator(selector)
        if locator.count() > 0:
            button = locator.first
            try:
                text = normalize_text(button.inner_text())
            except Exception:
                text = ""
            return button, text
    return None, ""


def click_sign_icon(session: StealthySession, account_index: int) -> tuple[str, str]:
    details: dict[str, Any] = {"status": "failed", "message": "状态未知"}
    cf_screenshot = build_account_artifact_path(account_index, "cf_block_sign.png")
    unknown_screenshot = build_account_artifact_path(account_index, "sign_intro_error.png")
    exception_screenshot = build_account_artifact_path(account_index, "sign_exception.png")

    def action(page: Any) -> None:
        try:
            print("开始执行签到流程...")
            page.wait_for_load_state("domcontentloaded", timeout=browser_timeout_ms())
            update_snapshot_from_page(page, details)

            if is_cloudflare_snapshot(details):
                screenshot = save_page_screenshot(page, cf_screenshot)
                if screenshot:
                    details["screenshot_path"] = screenshot
                details["status"] = "failed"
                details["message"] = "Cloudflare 拦截"
                return

            current_url = details.get("url", "") or ""
            if "/board" not in current_url and "nodeseek.com" in current_url and len(current_url) < 40:
                sign_icon = page.locator("xpath=//span[@title='签到']")
                if sign_icon.count() > 0:
                    print("⚠️ 似乎被重定向到了首页，尝试点击首页签到入口")
                    sign_icon.first.scroll_into_view_if_needed()
                    sign_icon.first.click()
                    page.wait_for_timeout(1_500)

            board_intro = page.locator(".board-intro").first
            intro_text = ""
            if board_intro.count() > 0:
                try:
                    board_intro.wait_for(state="visible", timeout=10_000)
                except Exception:
                    pass
                try:
                    intro_text = normalize_text(board_intro.inner_text())
                except Exception:
                    intro_text = ""
                details["intro_text"] = intro_text

            if intro_text and any(keyword in intro_text for keyword in ("获得", "排名", "已签到")):
                details["status"] = "already"
                details["message"] = parse_reward_from_text(intro_text)
                update_snapshot_from_page(page, details)
                return

            if board_intro.count() > 0:
                buttons = board_intro.locator("button")
                button_count = buttons.count()
                if button_count > 0:
                    button_texts = []
                    for idx in range(button_count):
                        try:
                            button_texts.append(normalize_text(buttons.nth(idx).inner_text()))
                        except Exception:
                            button_texts.append("")
                    target_index = choose_sign_button_index(button_texts)
                    if target_index is not None:
                        target_button = buttons.nth(target_index)
                        print(f"准备点击签到按钮: {button_texts[target_index] or '<无文本按钮>'}")
                        target_button.scroll_into_view_if_needed()
                        target_button.click()
                        page.wait_for_timeout(2_500)
                        update_snapshot_from_page(page, details)
                        reward_source = f"{details.get('body_text', '')} {details.get('intro_text', '')}".strip()
                        details["status"] = "success"
                        details["message"] = parse_reward_from_text(reward_source)
                        return

                if "还未签到" in intro_text:
                    details["status"] = "failed"
                    details["message"] = "未找到签到按钮"

            global_button, button_text = pick_global_sign_button(page)
            if global_button is not None:
                print(f"兜底点击全局签到按钮: {button_text or '<无文本按钮>'}")
                global_button.scroll_into_view_if_needed()
                global_button.click()
                page.wait_for_timeout(2_500)
                update_snapshot_from_page(page, details)
                details["status"] = "success"
                details["message"] = parse_reward_from_text(details.get("body_text", ""))
                return

            update_snapshot_from_page(page, details)
            body_text = details.get("body_text", "") or ""
            if any(keyword in body_text for keyword in ("今日签到获得", "当前排名", "今日已签到", "签到成功", "本次获得")):
                details["status"] = "already"
                details["message"] = parse_reward_from_text(body_text)
                return

            if "登录" in body_text and "注册" in body_text and "个人中心" not in body_text:
                details["status"] = "failed"
                details["message"] = "Cookie可能失效"
            else:
                details["status"] = "failed"
                details["message"] = "状态未知"
            screenshot = save_page_screenshot(page, unknown_screenshot)
            if screenshot:
                details["screenshot_path"] = screenshot
        except Exception as exc:
            details["status"] = "failed"
            details["message"] = f"异常: {exc}"
            details["traceback"] = traceback.format_exc()
            screenshot = save_page_screenshot(page, exception_screenshot)
            if screenshot:
                details["screenshot_path"] = screenshot

    try:
        session.fetch(
            BOARD_URL,
            page_action=action,
            wait_selector="body",
            timeout=browser_timeout_ms(),
            google_search=False,
            load_dom=True,
        )
    except Exception as exc:
        print(f"签到过程中出错: {exc}")
        print(traceback.format_exc())
        return "failed", f"异常: {exc}"

    if details.get("status") == "failed" and details.get("screenshot_path"):
        caption = f"❌ 签到失败\n原因: {details.get('message', '未知错误')}"
        send_telegram_photo(details["screenshot_path"], caption=caption)

    return str(details.get("status", "failed")), str(details.get("message", "状态未知"))


def collect_comment_post_urls(session: StealthySession, account_index: int) -> tuple[list[str], str | None]:
    details: dict[str, Any] = {"post_urls": []}
    screenshot_path = build_account_artifact_path(account_index, "comment_main_error.png")

    def action(page: Any) -> None:
        try:
            print(f"开始加载评论区: {config.comment_url}")
            page.wait_for_selector(".post-list-item", state="attached", timeout=30_000)
            update_snapshot_from_page(page, details)
            if is_cloudflare_snapshot(details):
                details["error"] = "评论列表页面遭遇 Cloudflare 拦截"
                details["screenshot_path"] = save_page_screenshot(page, screenshot_path)
                return

            posts = page.locator(".post-list-item")
            total = posts.count()
            print(f"成功获取到 {total} 个帖子")
            urls: list[str] = []
            for idx in range(total):
                post = posts.nth(idx)
                if post.locator(".pined").count() > 0:
                    continue
                link = post.locator(".post-title a")
                if link.count() == 0:
                    continue
                href = link.first.get_attribute("href")
                if not href:
                    continue
                urls.append(urljoin(HOME_URL, href))
            details["post_urls"] = urls
        except Exception as exc:
            details["error"] = f"加载评论列表失败: {exc}"
            details["traceback"] = traceback.format_exc()
            details["screenshot_path"] = save_page_screenshot(page, screenshot_path)

    try:
        session.fetch(
            config.comment_url,
            page_action=action,
            wait_selector="body",
            timeout=browser_timeout_ms(),
            google_search=False,
            load_dom=True,
        )
    except Exception as exc:
        return [], f"评论列表请求失败: {exc}"

    if details.get("error"):
        if details.get("screenshot_path"):
            send_telegram_photo(details["screenshot_path"], caption=f"❌ 评论列表加载失败\n错误: {details['error']}")
        return [], str(details["error"])

    post_urls = details.get("post_urls", []) or []
    if not post_urls:
        return [], "未找到可评论的帖子"
    return [str(url) for url in post_urls], None


def fill_codemirror(page: Any, text: str) -> bool:
    script = """
    ({ selector, value }) => {
      const root = document.querySelector(selector);
      if (!root) return false;

      if (root.CodeMirror) {
        root.CodeMirror.setValue(value);
        root.CodeMirror.focus();
        return true;
      }

      const inner = root.querySelector('.CodeMirror');
      if (inner && inner.CodeMirror) {
        inner.CodeMirror.setValue(value);
        inner.CodeMirror.focus();
        return true;
      }

      const textarea = root.querySelector('textarea');
      if (textarea) {
        textarea.value = value;
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
        textarea.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      }

      return false;
    }
    """
    try:
        return bool(page.evaluate(script, {"selector": ".CodeMirror", "value": text}))
    except Exception:
        return False


def comment_on_post(
    session: StealthySession,
    account_index: int,
    post_url: str,
    comment_text: str,
    comment_index: int,
) -> tuple[bool, str | None, str | None]:
    details: dict[str, Any] = {}
    screenshot_path = build_account_artifact_path(account_index, f"comment_error_{comment_index}.png")

    def action(page: Any) -> None:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=browser_timeout_ms())
            update_snapshot_from_page(page, details)

            if is_cloudflare_snapshot(details):
                details["error"] = "页面加载遭遇 Cloudflare"
                details["screenshot_path"] = save_page_screenshot(page, screenshot_path)
                return

            if "error" in (details.get("title", "") or "").lower():
                details["error"] = "页面加载异常"
                details["screenshot_path"] = save_page_screenshot(page, screenshot_path)
                return

            page.wait_for_selector(".CodeMirror", state="attached", timeout=30_000)
            editor = page.locator(".CodeMirror").first
            editor.click()
            page.wait_for_timeout(500)

            if not fill_codemirror(page, comment_text):
                page.keyboard.type(comment_text, delay=random.randint(80, 200))

            page.wait_for_timeout(1_000)
            submit_button = page.locator(
                "xpath=//button[contains(@class, 'submit') and contains(@class, 'btn') and contains(text(), '发布评论')]"
            ).first
            if submit_button.count() == 0:
                details["error"] = "未找到发布评论按钮"
                details["screenshot_path"] = save_page_screenshot(page, screenshot_path)
                return

            submit_button.scroll_into_view_if_needed()
            submit_button.click()
            page.wait_for_timeout(2_000)
            update_snapshot_from_page(page, details)
            details["ok"] = True
        except Exception as exc:
            details["error"] = f"处理帖子时出错: {exc}"
            details["traceback"] = traceback.format_exc()
            details["screenshot_path"] = save_page_screenshot(page, screenshot_path)

    try:
        session.fetch(
            post_url,
            page_action=action,
            wait_selector="body",
            timeout=browser_timeout_ms(),
            google_search=False,
            load_dom=True,
        )
    except Exception as exc:
        return False, f"评论请求失败: {exc}", None

    if details.get("ok"):
        return True, None, None
    return False, str(details.get("error", "评论失败")), details.get("screenshot_path")


def nodeseek_comment(session: StealthySession, account_index: int) -> int:
    comment_count = 0
    selected_urls, error = collect_comment_post_urls(session, account_index)
    if error:
        print(f"NodeSeek 评论列表阶段失败: {error}")
        return comment_count

    pick_count = random.randint(4, 7)
    selected_urls = random.sample(selected_urls, min(pick_count, len(selected_urls)))
    consecutive_failures = 0

    for index, post_url in enumerate(selected_urls):
        if consecutive_failures >= 2:
            print(f"⚠️ 连续失败 {consecutive_failures} 次，停止评论任务")
            break

        print(f"正在处理第 {index + 1} 个帖子: {post_url}")
        comment_text = random.choice(COMMENT_POOL)
        ok, error_message, screenshot_path = comment_on_post(session, account_index, post_url, comment_text, index)
        if ok:
            comment_count += 1
            consecutive_failures = 0
            print(f"已在帖子中完成评论: {post_url}")
            wait_minutes = random.uniform(1, 2)
            print(f"等待 {wait_minutes:.1f} 分钟后继续...")
            time.sleep(wait_minutes * 60)
            continue

        consecutive_failures += 1
        print(error_message or "评论失败")
        if screenshot_path and index == 0:
            send_telegram_photo(screenshot_path, caption=f"❌ 评论失败截图\n帖子: {post_url}\n错误: {error_message}")

    print("评论任务完成")
    return comment_count


def format_comment_result(result: dict[str, Any]) -> str:
    if result.get("comments_skipped"):
        return "已跳过"
    return f"{result['comments']} 条"


def finalize_authenticated_session(
    session: StealthySession,
    account_index: int,
    result: dict[str, Any],
) -> dict[str, Any]:
    mark_state_success()
    result["failure_class"] = None
    result["error"] = None

    sign_status, reward = click_sign_icon(session, account_index)
    result["sign_in"] = sign_status
    result["reward"] = reward
    if sign_status in {"success", "already"}:
        mark_state_success()

    if config.skip_comments:
        print("⏭️ 已启用 NS_SKIP_COMMENTS，跳过评论流程")
        result["comments"] = 0
        result["comments_skipped"] = True
        return result

    result["comments"] = nodeseek_comment(session, account_index)
    result["comments_skipped"] = False
    return result


def run_for_account(cookie_str: str, username: str, password: str, account_index: int) -> dict[str, Any]:
    result = {
        "sign_in": "failed",
        "reward": "0",
        "comments": 0,
        "comments_skipped": False,
        "error": None,
        "egress_mode": None,
        "failure_class": None,
    }

    print(f"\n{'=' * 50}")
    print(f"开始处理账号 {account_index + 1}")
    print(f"{'=' * 50}")
    clear_login_attempt_artifacts(account_index)

    egress_candidates = build_egress_candidates()
    last_attempt = build_attempt_result(LOGIN_STATUS_UNKNOWN_PAGE, "未开始尝试", None)

    for attempt_index, egress_mode in enumerate(egress_candidates, start=1):
        print(f"🚀 第 {attempt_index}/{len(egress_candidates)} 次尝试，出口={describe_egress_mode(egress_mode)}")
        login_attempt_snapshots: list[dict[str, Any]] = []
        try:
            bootstrap_result = build_attempt_result(LOGIN_STATUS_LOGIN_FAILED, "未尝试登录", egress_mode)
            if cookie_str:
                with create_session(account_index, egress_mode, use_custom_fingerprint=True) as session:
                    _, bootstrap_result = bootstrap_session(session, cookie_str, account_index, egress_mode)
                    print(f"🧪 Cookie 引导结果: {bootstrap_result['status_code']}")
                    if bootstrap_result["status_code"] == LOGIN_STATUS_OK:
                        result["egress_mode"] = egress_mode
                        last_attempt = bootstrap_result
                        return finalize_authenticated_session(session, account_index, result)
            else:
                print("⚠️ 当前账号未配置 Cookie，直接尝试真实登录")

            if not (username and password):
                result["egress_mode"] = egress_mode
                result["failure_class"] = bootstrap_result["status_code"]
                last_attempt = bootstrap_result
                result["error"] = bootstrap_result["reason"] or "登录失败"
                if should_retry_with_next_egress(str(bootstrap_result["status_code"])) and attempt_index < len(egress_candidates):
                    print("⚠️ 当前出口命中可重试失败，准备切换到下一个出口")
                    continue
                return result

            print("🔐 Cookie 未带出登录态，开始尝试账号密码登录")
            login_variants: list[tuple[str, Path, bool]] = [
                ("cached_state", build_account_state_dir(account_index), False),
            ]

            clean_state_dir: Path | None = None
            login_result = bootstrap_result
            for state_mode, state_dir, is_clean_state in login_variants:
                try:
                    with create_session(
                        account_index,
                        egress_mode,
                        user_data_dir=state_dir,
                        use_custom_fingerprint=False,
                    ) as auth_session:
                        login_snapshot, login_result = login_with_credentials(
                            auth_session,
                            username,
                            password,
                            account_index,
                            egress_mode,
                            state_mode=state_mode,
                        )
                        login_attempt_snapshots.append(login_snapshot)
                        last_attempt = login_result
                        result["egress_mode"] = egress_mode
                        result["failure_class"] = login_result["status_code"]

                        if login_result["status_code"] == LOGIN_STATUS_OK:
                            if is_clean_state:
                                persist_browser_state(state_dir, build_account_state_dir(account_index))
                            write_login_attempt_summary(account_index, egress_mode, login_attempt_snapshots)
                            return finalize_authenticated_session(auth_session, account_index, result)

                        if is_clean_state or not should_retry_clean_login(login_snapshot, login_result):
                            break

                        print("♻️ 命中 PAT/Challenge 401，切换到干净浏览器状态重试真实登录")
                finally:
                    if clean_state_dir is not None and clean_state_dir == state_dir:
                        cleanup_temp_state_dir(clean_state_dir)

                if not is_clean_state and should_retry_clean_login(login_snapshot, login_result):
                    clean_state_dir = create_clean_login_state_dir(account_index)
                    login_variants.append(("clean_state", clean_state_dir, True))

            write_login_attempt_summary(account_index, egress_mode, login_attempt_snapshots)
            result["egress_mode"] = egress_mode
            result["failure_class"] = login_result["status_code"]
            last_attempt = login_result

            if login_result["status_code"] != LOGIN_STATUS_OK:
                result["error"] = login_result["reason"] or "登录失败"
                if should_retry_with_next_egress(str(login_result["status_code"])) and attempt_index < len(egress_candidates):
                    print("⚠️ 当前出口命中可重试失败，准备切换到下一个出口")
                    continue
                return result
        except Exception as exc:
            reason = f"账号执行异常: {exc}"
            print(reason)
            print(traceback.format_exc())
            last_attempt = build_attempt_result(LOGIN_STATUS_EGRESS_FAILED, reason, egress_mode)
            result["error"] = reason
            result["failure_class"] = LOGIN_STATUS_EGRESS_FAILED
            result["egress_mode"] = egress_mode
            if should_retry_with_next_egress(LOGIN_STATUS_EGRESS_FAILED) and attempt_index < len(egress_candidates):
                print("⚠️ 当前出口初始化失败，准备切换到下一个出口重试")
                continue
            return result

    result["error"] = last_attempt["reason"] or "所有出口均尝试失败"
    result["failure_class"] = last_attempt["status_code"]
    result["egress_mode"] = last_attempt["egress_mode"]
    return result


def build_report_message(all_results: list[dict[str, Any]]) -> str:
    beijing_tz = timezone(timedelta(hours=8))
    beijing_time = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")

    if len(all_results) == 1:
        result = all_results[0]
        if result["error"]:
            egress_label = describe_egress_mode(result["egress_mode"])
            failure_label = result["failure_class"] or "unknown"
            return f"""<b>NodeSeek 每日简报</b>
━━━━━━━━━━━━━━━
❌ <b>任务失败</b>
━━━━━━━━━━━━━━━
🌐 <b>出口</b>: {egress_label}
🧭 <b>分类</b>: {failure_label}
⚠️ <b>错误</b>: {result['error']}
🕒 {beijing_time}"""

        if result["sign_in"] == "success":
            sign_status = "✅ 成功"
            sign_result = "已签到"
        elif result["sign_in"] == "already":
            sign_status = "✅ 成功"
            sign_result = "今日已签"
        else:
            sign_status = "❌ 失败"
            sign_result = "签到失败"

        egress_label = describe_egress_mode(result["egress_mode"])
        return f"""<b>NodeSeek 每日简报</b>
━━━━━━━━━━━━━━━
👤 <b>账号</b>: 账号 1
🌐 <b>出口</b>: {egress_label}
🏆 <b>奖励</b>: <b>{result['reward']}</b> 🍗
💬 <b>评论</b>: {format_comment_result(result)}
━━━━━━━━━━━━━━━
{sign_status} <b>状态</b>: {sign_result}
🕒 {beijing_time}"""

    account_lines = []
    for idx, result in enumerate(all_results):
        if result["error"]:
            egress_label = describe_egress_mode(result["egress_mode"])
            failure_label = result["failure_class"] or "unknown"
            account_lines.append(f"❌ 账号{idx + 1}: {egress_label} | {failure_label} | {result['error']}")
            continue

        sign = f"✅ +{result['reward']}🍗" if result["sign_in"] in {"success", "already"} else "❌"
        egress_label = describe_egress_mode(result["egress_mode"])
        account_lines.append(f"👤 账号{idx + 1}: {egress_label} | {sign} | 💬 {format_comment_result(result)}")

    accounts_str = "\n".join(account_lines)
    return f"""<b>NodeSeek 每日简报</b>
━━━━━━━━━━━━━━━
{accounts_str}
━━━━━━━━━━━━━━━
🕒 {beijing_time}"""


def main() -> int:
    print("开始执行 NodeSeek 自动任务（Scrapling 版）...")
    print(
        f"当前配置: NS_RANDOM={config.ns_random}, NS_HEADLESS={config.headless}, "
        f"PROXY={mask_proxy_url(config.proxy_url)}, PROXY_INSECURE={config.proxy_insecure}, EGRESS_MODE={config.egress_mode}, "
        f"CF_WAIT={config.cf_wait_seconds}s, CF_LOGIN_RETRIES={config.cf_login_retries}, "
        f"SKIP_COMMENTS={config.skip_comments}, STATE_DIR={config.browser_state_dir}"
    )

    clear_state_success_marker()

    if config.account_count == 0:
        print("未配置 Cookie 或账号密码，退出")
        send_telegram_message("❌ <b>NodeSeek 自动任务失败</b>\n\n未配置 NS_COOKIE 或 NS_USERNAME/NS_PASSWORD 环境变量")
        return 1

    print(f"检测到 {config.account_count} 个账号")
    delay_seconds = config.get_random_delay_seconds()
    if delay_seconds > 0:
        print(f"随机延迟执行: 等待 {delay_seconds / 60:.1f} 分钟...")
        time.sleep(delay_seconds)

    all_results = []
    for account_index in range(config.account_count):
        cookie = config.get_cookie(account_index)
        username, password = config.get_credentials(account_index)
        all_results.append(run_for_account(cookie, username, password, account_index))

    print(f"\n{'=' * 50}")
    print("所有账号任务执行完成")
    print(f"{'=' * 50}")
    send_telegram_message(build_report_message(all_results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
