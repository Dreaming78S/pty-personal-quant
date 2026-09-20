import pytest

from quant.config import Settings

ENV_KEYS = [
    "ALIYUN_RDS_HOST", "ALIYUN_RDS_PORT", "ALIYUN_RDS_USER",
    "ALIYUN_RDS_PASSPORT", "ALIYUN_RDS_DATABASE", "TUSHARE_TOKEN",
]


def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("ALIYUN_RDS_HOST", "db.example.com")
    monkeypatch.setenv("ALIYUN_RDS_USER", "user1")
    monkeypatch.setenv("ALIYUN_RDS_PASSPORT", "pw")
    monkeypatch.setenv("ALIYUN_RDS_DATABASE", "quant")
    monkeypatch.setenv("TUSHARE_TOKEN", "tok")
    monkeypatch.delenv("ALIYUN_RDS_PORT", raising=False)

    s = Settings(_env_file=None)

    assert s.aliyun_rds_host == "db.example.com"
    assert s.aliyun_rds_port == 3306
    assert s.tushare_token == "tok"


def test_settings_missing_required_raises(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(Exception):
        Settings(_env_file=None)


def _set_required(monkeypatch):
    monkeypatch.setenv("ALIYUN_RDS_HOST", "db")
    monkeypatch.setenv("ALIYUN_RDS_USER", "u")
    monkeypatch.setenv("ALIYUN_RDS_PASSPORT", "p")
    monkeypatch.setenv("ALIYUN_RDS_DATABASE", "d")
    monkeypatch.setenv("TUSHARE_TOKEN", "t")


def test_settings_reads_bot_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)

    s = Settings(_env_file=None)

    assert s.feishu_app_id == "cli_x"
    assert s.feishu_app_secret == "sec"
    assert s.deepseek_api_key == "sk-x"
    assert s.deepseek_base_url == "https://api.deepseek.com"
    assert s.deepseek_model == "deepseek-flash"


def test_settings_bot_keys_optional(monkeypatch):
    _set_required(monkeypatch)
    for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    s = Settings(_env_file=None)

    assert s.feishu_app_id is None
    assert s.deepseek_api_key is None
