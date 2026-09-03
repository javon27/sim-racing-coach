import logging
import logging.handlers

from simcoach import paths
from simcoach.logging_setup import configure_logging


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
