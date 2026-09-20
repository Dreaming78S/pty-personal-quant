from types import SimpleNamespace

from typer.testing import CliRunner

from quant.cli import app

runner = CliRunner()


def test_bot_ask_prints_answer_and_sql(monkeypatch):
    from quant.bot.qa import Answer

    monkeypatch.setattr("quant.config.get_settings", lambda: SimpleNamespace(
        deepseek_api_key="sk-x", deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-flash"))
    monkeypatch.setattr("quant.bot.qa.answer",
                        lambda question, llm, runner_: Answer(
                            "博敏电子近 3 日命中 2 次", "SELECT 1 LIMIT 200", 2, True))

    result = runner.invoke(app, ["bot", "ask", "博敏电子最近几天命中策略的情况"])

    assert result.exit_code == 0
    assert "博敏电子近 3 日命中 2 次" in result.output
    assert "SELECT 1 LIMIT 200" in result.output
    assert "返回 2 行" in result.output


def test_bot_ask_exits_nonzero_on_failure(monkeypatch):
    from quant.bot.qa import Answer

    monkeypatch.setattr("quant.config.get_settings", lambda: SimpleNamespace(
        deepseek_api_key="sk-x", deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-flash"))
    monkeypatch.setattr("quant.bot.qa.answer",
                        lambda question, llm, runner_: Answer(
                            "拒绝执行：不允许的关键字：DELETE",
                            "DELETE FROM daily", 0, False))

    result = runner.invoke(app, ["bot", "ask", "删掉数据"])

    assert result.exit_code == 1
    assert "拒绝执行" in result.output


def test_bot_ask_requires_api_key(monkeypatch):
    monkeypatch.setattr("quant.config.get_settings", lambda: SimpleNamespace(
        deepseek_api_key=None, deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-flash"))

    result = runner.invoke(app, ["bot", "ask", "问题"])

    assert result.exit_code == 1
    assert "DEEPSEEK_API_KEY" in result.output


def test_bot_serve_requires_credentials(monkeypatch):
    monkeypatch.setattr("quant.config.get_settings", lambda: SimpleNamespace(
        feishu_app_id=None, feishu_app_secret=None, deepseek_api_key=None))
    from quant.bot import feishu as feishu_module
    monkeypatch.setattr(feishu_module, "get_settings", lambda: SimpleNamespace(
        feishu_app_id=None, feishu_app_secret=None, deepseek_api_key=None))

    result = runner.invoke(app, ["bot", "serve"])

    assert result.exit_code == 1
    assert "FEISHU_APP_ID" in result.output
