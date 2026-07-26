import json

from app.config import settings
from app.core.logging import bind_job, configure_logging, get_logger


def test_bind_job_injects_job_id_without_explicit_arg(capsys) -> None:  # type: ignore[no-untyped-def]
    object.__setattr__(settings, "env", "staging")
    configure_logging()
    log = get_logger("test")
    with bind_job("abc"):
        log.info("some.event")
    captured = capsys.readouterr()
    line = json.loads(captured.out.strip().splitlines()[-1])
    assert line["job_id"] == "abc"
    object.__setattr__(settings, "env", "local")
    configure_logging()


def test_log_level_warning_suppresses_info(capsys) -> None:  # type: ignore[no-untyped-def]
    object.__setattr__(settings, "log_level", "WARNING")
    configure_logging()
    log = get_logger("test")
    log.info("suppressed.event")
    log.warning("visible.event")
    captured = capsys.readouterr()
    assert "suppressed.event" not in captured.out
    assert "visible.event" in captured.out
    object.__setattr__(settings, "log_level", "INFO")
    configure_logging()


def test_non_local_env_renders_json(capsys) -> None:  # type: ignore[no-untyped-def]
    object.__setattr__(settings, "env", "staging")
    configure_logging()
    log = get_logger("test")
    log.info("json.render.check")
    captured = capsys.readouterr()
    parsed = json.loads(captured.out.strip().splitlines()[-1])
    assert parsed["event"] == "json.render.check"
    object.__setattr__(settings, "env", "local")
    configure_logging()


def test_local_env_renders_console_not_json(capsys) -> None:  # type: ignore[no-untyped-def]
    object.__setattr__(settings, "env", "local")
    configure_logging()
    log = get_logger("test")
    log.info("console.render.check")
    captured = capsys.readouterr()
    line = captured.out.strip().splitlines()[-1]
    try:
        json.loads(line)
        is_json = True
    except json.JSONDecodeError:
        is_json = False
    assert not is_json
