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
