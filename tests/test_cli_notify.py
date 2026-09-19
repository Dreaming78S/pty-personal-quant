from typer.testing import CliRunner

from quant.cli import app
from quant.engine import notify

runner = CliRunner()


def test_notify_dry_run_prints_messages(monkeypatch):
    message = notify.FeishuMessage("【ma_volume】2026-09-18",
                                   ("今日无命中",), False)
    co_message = notify.FeishuMessage("【多策略共振】2026-09-18",
                                      ("今日无共振",), False)
    monkeypatch.setattr(
        "quant.engine.notify.build_messages",
        lambda strategy, date, include_co=True: {
            "ma_volume": message, "多策略共振": co_message})

    result = runner.invoke(app, ["notify", "-s", "ma_volume",
                                 "-d", "2026-09-18", "--dry-run"])

    assert result.exit_code == 0
    assert "----- ma_volume -----" in result.output
    assert "【ma_volume】2026-09-18" in result.output
    assert "今日无命中" in result.output
    assert "----- 多策略共振 -----" in result.output
    assert "今日无共振" in result.output


def test_notify_dry_run_can_skip_co(monkeypatch):
    captured = {}

    def fake_build(strategy, date, include_co=True):
        captured["include_co"] = include_co
        return {}

    monkeypatch.setattr("quant.engine.notify.build_messages", fake_build)

    result = runner.invoke(app, ["notify", "--no-co", "--dry-run"])

    assert result.exit_code == 0
    assert captured == {"include_co": False}


def test_notify_success_reports_sent(monkeypatch):
    called = {}

    def fake_notify(strategy="all", date=None, include_co=True):
        called.update(strategy=strategy, date=date, include_co=include_co)
        return {"ma_volume": "已发送"}

    monkeypatch.setattr("quant.engine.notify.notify_hits", fake_notify)

    result = runner.invoke(app, ["notify", "-s", "ma_volume",
                                 "-d", "2026-09-18"])

    assert result.exit_code == 0
    assert called == {"strategy": "ma_volume", "date": "20260918",
                      "include_co": True}
    assert "ma_volume: 已发送" in result.output


def test_notify_failure_exits_nonzero(monkeypatch):
    monkeypatch.setattr(
        "quant.engine.notify.notify_hits",
        lambda strategy="all", date=None, include_co=True: {
            "ma_volume": "失败：boom"})

    result = runner.invoke(app, ["notify", "-s", "ma_volume"])

    assert result.exit_code == 1
    assert "失败：boom" in result.output
