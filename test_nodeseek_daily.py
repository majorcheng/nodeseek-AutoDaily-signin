import importlib
from typing import Any

ENV_KEYS = (
    "NS_COOKIE",
    "NS_USERNAME",
    "NS_PASSWORD",
    "NS_PROXY_URL",
    "NS_PROXY_INSECURE",
)


def reload_module_with_env(monkeypatch: Any, **env: str):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    module = importlib.import_module("nodeseek_daily")
    return importlib.reload(module)


def test_config_from_env_with_empty_values_has_no_accounts_or_proxy():
    import nodeseek_daily

    config = nodeseek_daily.Config.from_env(
        {
            "NS_COOKIE": "",
            "NS_USERNAME": "",
            "NS_PASSWORD": "",
            "NS_PROXY_URL": "",
        }
    )

    assert config.cookies == []
    assert config.usernames == []
    assert config.passwords == []
    assert config.account_count == 0
    assert config.proxy_url == ""
    assert config.proxy_insecure is True


def test_config_from_env_uses_injected_credentials_and_proxy_when_cookie_absent():
    import nodeseek_daily

    config = nodeseek_daily.Config.from_env(
        {
            "NS_COOKIE": "",
            "NS_USERNAME": "majorcheng",
            "NS_PASSWORD": "comventer001",
            "NS_PROXY_URL": "https://70.166.233.156:443",
            "NS_PROXY_INSECURE": "true",
        }
    )

    assert config.cookies == []
    assert config.account_count == 1
    assert config.get_cookie(0) == ""
    assert config.get_credentials(0) == ("majorcheng", "comventer001")
    assert config.proxy_url == "https://70.166.233.156:443"
    assert config.proxy_insecure is True


def test_module_level_config_has_no_accounts_when_env_absent(monkeypatch: Any):
    module = reload_module_with_env(monkeypatch)

    assert module.config.cookies == []
    assert module.config.usernames == []
    assert module.config.passwords == []
    assert module.config.account_count == 0
    assert module.config.proxy_url == ""
    assert module.config.proxy_insecure is True


def test_module_level_config_respects_injected_env_values(monkeypatch: Any):
    module = reload_module_with_env(
        monkeypatch,
        NS_USERNAME="env_user",
        NS_PASSWORD="env_pass",
        NS_PROXY_URL="http://10.0.0.2:9000",
        NS_PROXY_INSECURE="0",
    )

    assert module.config.account_count == 1
    assert module.config.get_credentials(0) == ("env_user", "env_pass")
    assert module.config.proxy_url == "http://10.0.0.2:9000"
    assert module.config.proxy_insecure is False


def test_build_turnstile_click_positions_stays_within_iframe_bounds():
    import nodeseek_daily

    positions = nodeseek_daily.build_turnstile_click_positions(300, 65)

    assert len(positions) >= 4
    for x_pos, y_pos in positions:
        assert 0 < x_pos < 300
        assert 0 < y_pos < 65


def test_should_retry_clean_login_skips_duplicate_pat401_retries():
    import nodeseek_daily

    should_retry = nodeseek_daily.should_retry_clean_login(
        {
            "sign_in_request_count": 0,
            "challenge_pat_401_count": 2,
            "challenge_request_count": 6,
            "turnstile_frame_found": True,
            "captcha_container_present": True,
        },
        {"status_code": nodeseek_daily.LOGIN_STATUS_CF_CHALLENGE},
    )

    assert should_retry is False
