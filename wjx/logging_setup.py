"""日志：轮转文件日志 + 可挂载的 GUI 处理器。

后台静默运行时日志文件是唯一的排错依据，因此默认记录到 logs/wjx.log。
"""

from __future__ import annotations

import logging
import logging.handlers
import queue
import sys
from typing import Optional

from .paths import LOG_FILE, ensure_dirs

_CONFIGURED = False
EVENTS: "queue.Queue[str]" = queue.Queue(maxsize=2000)


class QueueHandler(logging.Handler):
    """把日志文本塞进队列，由 GUI 定时取走显示（避免跨线程操作控件）。"""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            EVENTS.put_nowait(self.format(record))
        except queue.Full:
            pass
        except Exception:
            pass


def setup(level: int = logging.INFO, to_console: bool = True, gui: bool = False) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("wjx")
    if _CONFIGURED:
        return logger
    ensure_dirs()
    logger.setLevel(level)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    if to_console and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        logger.addHandler(stream)

    if gui:
        qh = QueueHandler()
        qh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        logger.addHandler(qh)
    _CONFIGURED = True
    return logger


def get_logger(name: str = "wjx") -> logging.Logger:
    return logging.getLogger(name)


def enable_gui_handler() -> None:
    logger = logging.getLogger("wjx")
    if any(isinstance(h, QueueHandler) for h in logger.handlers):
        return
    qh = QueueHandler()
    qh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    logger.addHandler(qh)


def drain_events(limit: int = 200) -> list:
    out = []
    for _ in range(limit):
        try:
            out.append(EVENTS.get_nowait())
        except queue.Empty:
            break
    return out
