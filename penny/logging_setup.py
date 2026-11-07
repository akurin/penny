"""Structured diagnostics; no raw exceptions, HTTP bodies, or request headers."""
import contextvars
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import time
import traceback
from contextlib import contextmanager

request_id = contextvars.ContextVar("request_id", default="-")
logger = logging.getLogger("penny")


def redact(value):
    text = str(value)
    for key in ("TELEGRAM_BOT_TOKEN", "MODEL_API_KEY", "MODEL_PROXY",
                "STORE_PROXY", "TELEGRAM_PROXY"):
        secret = os.getenv(key)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(?:socks5h?|https?)://[^\s]+", "[URL]", text)
    text = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b|\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    return text[:2000]


def event(name, level=logging.INFO, **fields):
    logger.log(level, json.dumps({"event": name, "request_id": request_id.get(), **fields}, ensure_ascii=False))


def content(name, value):
    if os.getenv("PENNY_LOG_CONTENT", "0") == "1":
        event(name, value=redact(value))


def failure(stage, exc):
    from .diagnostics import summary
    frames = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
              for frame in traceback.extract_tb(exc.__traceback__)]
    event("failure", logging.ERROR, stage=stage, error=summary(stage, exc), frames=frames)


@contextmanager
def span(stage):
    started = time.monotonic()
    event("stage_started", logging.DEBUG, stage=stage)
    try:
        yield
    except Exception as exc:
        failure(stage, exc)
        raise
    finally:
        event("stage_finished", stage=stage, elapsed_ms=round((time.monotonic() - started) * 1000))


def configure():
    level = os.getenv("PENNY_LOG_LEVEL", "INFO").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise SystemExit("PENNY_LOG_LEVEL must be DEBUG, INFO, WARNING, or ERROR")
    logger.setLevel(level)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    handlers = [logging.StreamHandler()]
    path = os.getenv("PENNY_LOG_FILE", "logs/penny.log")
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    # Do not enable HTTP wire logs or framework logs containing message contents.
    for name in ("httpx", "httpcore", "aiogram"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    event("startup", content_logging=os.getenv("PENNY_LOG_CONTENT", "0") == "1")
