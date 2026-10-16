"""填写后端工厂。"""

from __future__ import annotations

from typing import Any

from .base import DriverError, DriverUnavailable, FillDriver, FillOutcome
from .mock import MockDriver

__all__ = [
    "FillDriver",
    "FillOutcome",
    "DriverError",
    "DriverUnavailable",
    "MockDriver",
    "create_driver",
    "selenium_available",
]


def selenium_available() -> bool:
    try:
        import selenium  # type: ignore  # noqa: F401

        return True
    except Exception:
        return False


def create_driver(settings: Any) -> FillDriver:
    """按设置创建后端。auto: 有 selenium 用 selenium，否则 mock。"""
    backend = (getattr(settings, "backend", "mock") or "mock").lower()
    if backend == "auto":
        backend = "selenium" if selenium_available() else "mock"
    if backend == "selenium":
        from .selenium_driver import SeleniumDriver

        return SeleniumDriver(settings)
    return MockDriver(settings)
