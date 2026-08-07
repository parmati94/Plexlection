"""Colorised logging setup.

setup_logging() must be called before any other project import so that
module-level `logger = get_logger(__name__)` calls inherit the configured
root handler.
"""
import logging

import colorlog

from backend.common.config import config

_CONFIGURED = False

# Libraries that log per-request or per-chunk and would drown the scan output.
_NOISY = (
    "sse_starlette.sse",
    "apscheduler.scheduler",
    "apscheduler.executors.default",
    "plexapi",
    "urllib3.connectionpool",
    "httpx",
    "httpcore",
)


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = colorlog.StreamHandler()
    handler.setFormatter(
        colorlog.ColoredFormatter(
            "%(log_color)s%(levelname)-8s%(reset)s %(asctime)s %(name)-28s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            log_colors={
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "red,bg_white",
            },
        )
    )

    root = logging.getLogger()
    # Drop uvicorn's default handlers so records aren't emitted twice.
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
