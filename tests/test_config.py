"""Configuration loading, validation and secret hygiene."""

from __future__ import annotations

from pathlib import Path

from havoc.config import (
    Config,
    ConfigIssue,
    format_issues,
    has_blocking,
    load_config,
    parse_env_file,
    validate,
)


def test_env_file_parsing_handles_quotes_comments_and_export(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        """
# comment
API_ID=12345
export API_HASH="0123456789abcdef0123456789abcdef"
PHONE='+49 123 456'
COMMAND_PREFIX=.   # trailing comment
EMPTY=
BROKEN LINE WITHOUT EQUALS
""",
        encoding="utf-8",
    )
    values = parse_env_file(env)
    assert values["API_ID"] == "12345"
    assert values["API_HASH"] == "0123456789abcdef0123456789abcdef"
    assert values["PHONE"] == "+49 123 456"
    assert values["COMMAND_PREFIX"] == "."
    assert values["EMPTY"] == ""
    assert "BROKEN" not in " ".join(values)


def test_real_environment_wins_over_dotenv(tmp_path: Path, monkeypatch) -> None:
    env = tmp_path / ".env"
    env.write_text("DEFAULT_LANGUAGE=fa\n", encoding="utf-8")
    monkeypatch.setenv("DEFAULT_LANGUAGE", "en")
    config, _issues = load_config(dotenv_path=env)
    assert config.default_language == "en"


def test_load_config_defaults_are_safe() -> None:
    config, issues = load_config(env={})
    assert config.dry_run is False
    assert config.plugins_enabled is True
    assert config.default_language == "en"
    assert config.command_prefix == "."
    assert config.ai_provider == "none"
    assert config.translate_provider == "none"
    # credentials missing -> blocking issue
    assert has_blocking(issues)


def test_validation_catches_bad_values() -> None:
    config = Config(
        api_id=-5,
        api_hash="nothex",
        command_prefix="",
        default_language="de",
        timezone="Mars/Olympus",
        log_level="LOUD",
        missed_job_policy="maybe",
    )
    issues = validate(config)
    fields = {issue.field for issue in issues}
    assert {"API_ID", "API_HASH", "COMMAND_PREFIX", "DEFAULT_LANGUAGE", "TIMEZONE", "LOG_LEVEL", "MISSED_JOB_POLICY"} <= fields
    assert any(issue.severity == "warning" for issue in issues)


def test_valid_config_has_no_issues() -> None:
    config = Config(
        api_id=123,
        api_hash="a" * 32,
        command_prefix=".",
        default_language="fa",
        timezone="Asia/Tehran",
    )
    assert validate(config) == []


def test_redacted_hides_secrets() -> None:
    config = Config(api_id=1, api_hash="secret-hash-value", session_string="x" * 80, ai_api_key="sk-abc")
    redacted = config.redacted()
    assert redacted["api_hash"] == "***set***"
    assert redacted["session_string"] == "***set***"
    assert redacted["ai_api_key"] == "***set***"
    assert "secret-hash-value" not in str(redacted)


def test_bool_and_int_parsing_warnings() -> None:
    _config, issues = load_config(env={"API_ID": "not-a-number", "DRY_RUN": "banana", "BACKUP_RETENTION": "many"})
    fields = {issue.field for issue in issues}
    assert "API_ID" in fields
    assert "DRY_RUN" in fields
    assert all(issue.severity == "warning" for issue in issues if issue.field in {"DRY_RUN", "BACKUP_RETENTION"})


def test_session_file_suffix_and_paths(tmp_path: Path) -> None:
    config, _ = load_config(
        env={
            "API_ID": "1",
            "API_HASH": "a" * 32,
            "DATA_DIR": str(tmp_path / "d"),
            "SESSION_PATH": str(tmp_path / "d" / "havoc"),
        }
    )
    assert config.session_file.suffix == ".session"
    assert config.session_file.parent == tmp_path / "d"


def test_prefix_and_language_env() -> None:
    config, issues = load_config(
        env={
            "API_ID": "1",
            "API_HASH": "a" * 32,
            "COMMAND_PREFIX": "!",
            "DEFAULT_LANGUAGE": "FA",
            "TIMEZONE": "Asia/Tehran",
            "OWNER_IDS": "1, 2, 3",
        }
    )
    assert config.command_prefix == "!"
    assert config.default_language == "fa"
    assert config.owner_ids == (1, 2, 3)
    assert validate(config) == []
    assert issues == []


def test_format_issues_is_readable() -> None:
    text = format_issues([ConfigIssue("API_ID", "missing or not numeric")])
    assert "API_ID" in text and "error" in text


def test_ai_availability_flags() -> None:
    disabled = Config(ai_provider="none", ai_api_key="key")
    assert disabled.ai_available is False
    enabled = Config(ai_provider="openai_compatible", ai_api_key="key")
    assert enabled.ai_available is True
    no_key = Config(ai_provider="openai_compatible", ai_api_key="")
    assert no_key.ai_available is False
    libretranslate = Config(translate_provider="libretranslate", libretranslate_url="")
    assert libretranslate.translation_available is False
    mymemory = Config(translate_provider="mymemory")
    assert mymemory.translation_available is True


# ---------------------------------------------------------------------------
# Telegram connection proxy (MTProto is blocked on some networks)
# ---------------------------------------------------------------------------
def test_no_proxy_by_default() -> None:
    from havoc.config import Config

    assert Config().proxy_configured is False


def test_proxy_requires_host_and_port() -> None:
    from havoc.config import Config, validate

    issues = [issue for issue in validate(Config(proxy_type="socks5")) if "PROXY" in issue.field]
    fields = {issue.field for issue in issues}
    assert "PROXY_HOST" in fields
    assert "PROXY_PORT" in fields


def test_unknown_proxy_type_is_rejected() -> None:
    from havoc.config import Config, validate

    config = Config(proxy_type="wireguard", proxy_host="127.0.0.1", proxy_port=1080)
    assert any("PROXY_TYPE" == issue.field for issue in validate(config))


def test_mtproxy_requires_a_secret() -> None:
    from havoc.config import Config, validate

    config = Config(proxy_type="mtproxy", proxy_host="1.2.3.4", proxy_port=443)
    assert any("PROXY_SECRET" == issue.field for issue in validate(config))


def test_valid_socks5_proxy_passes_and_strips_from_repr() -> None:
    from havoc.config import Config, validate

    config = Config(
        proxy_type="socks5",
        proxy_host="127.0.0.1",
        proxy_port=1080,
        proxy_username="user",
        proxy_password="hunter2",
    )
    assert config.proxy_configured is True
    assert not [issue for issue in validate(config) if "PROXY" in issue.field]
    # Credentials must never reach a log line or a crash report.
    assert "hunter2" not in repr(config)
