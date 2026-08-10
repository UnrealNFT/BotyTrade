"""Structured logging utilities."""

import sys

from loguru import logger


def setup_logger(level: str = "INFO") -> None:
    """Configure loguru logger with UTF-8 output for Windows terminals."""
    logger.remove()
    # Force UTF-8 so emojis in token symbols don't crash the console handler.
    sink = sys.stdout
    if hasattr(sink, "reconfigure"):
        sink.reconfigure(encoding="utf-8")
    logger.add(
        sink,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>",
        backtrace=False,
        diagnose=False,
        enqueue=True,
    )


def get_logger(name: str):
    """Return a logger bound with a name and support %-style formatting."""
    bound = logger.bind(name=name)

    class LoggerWrapper:
        def __init__(self, underlying):
            self._logger = underlying

        def _fmt(self, msg, args):
            return msg % args if args else msg

        def debug(self, msg, *args, **kwargs):
            self._logger.debug(self._fmt(msg, args), **kwargs)

        def info(self, msg, *args, **kwargs):
            self._logger.info(self._fmt(msg, args), **kwargs)

        def warning(self, msg, *args, **kwargs):
            self._logger.warning(self._fmt(msg, args), **kwargs)

        def error(self, msg, *args, **kwargs):
            self._logger.error(self._fmt(msg, args), **kwargs)

        def exception(self, msg, *args, **kwargs):
            self._logger.exception(self._fmt(msg, args), **kwargs)

        def __getattr__(self, item):
            return getattr(self._logger, item)

    return LoggerWrapper(bound)
