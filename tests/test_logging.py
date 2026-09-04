import logging
import logging.handlers

from simcoach import config, paths
from simcoach.logging_setup import apply_log_level, configure_logging


def test_configure_logging_writes_to_rotating_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    configure_logging()

    logging.getLogger("simcoach").info("hello")
    for h in logging.getLogger().handlers:
        h.flush()

    log_file = paths.logs_dir() / "server.log"
    assert log_file.exists()
    assert "hello" in log_file.read_text()


def test_configure_logging_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    configure_logging()
    configure_logging()

    file_handlers = [
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) == 1


def test_rotating_file_handler_params(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    configure_logging()

    file_handlers = [
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    handler = file_handlers[0]
    assert handler.maxBytes == 5_000_000
    assert handler.backupCount == 3


def test_configure_logging_applies_configured_level(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    config.save_config({"log_level": "DEBUG"})
    configure_logging()
    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_invalid_level_defaults_to_info(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    config.save_config({"log_level": "NOT_A_LEVEL"})
    configure_logging()
    assert logging.getLogger().level == logging.INFO


def test_apply_log_level_changes_root_and_handlers(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    configure_logging()

    apply_log_level("WARNING")

    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("uvicorn").level == logging.WARNING
    for h in logging.getLogger().handlers:
        if getattr(h, "_simcoach_handler", False):
            assert h.level == logging.WARNING


def test_apply_log_level_invalid_defaults_to_info(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    configure_logging()

    apply_log_level("BOGUS")

    assert logging.getLogger().level == logging.INFO
