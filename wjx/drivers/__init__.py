"""填写后端工厂：按设置与网址选择后端。

* ``mock``      走本地模拟问卷服务（离线 demo/自测）
* ``selenium``  真实浏览器填写问卷星
* ``auto``      默认：装了 selenium 就用真实浏览器，否则回落 mock

另外做了一层**按网址路由**：即使是 mock 后端，只要任务网址不在模拟服务域名下
（例如 https://v.wjx.cn/vm/xxx.aspx），就自动改用真实浏览器后端；
若此时 selenium 不可用，则抛出带安装说明的 DriverUnavailable，
而不是把真实问卷悄悄发给模拟服务（那样只会得到 404/无意义结果）。
"""

from __future__ import annotations

from typing import Any, Optional

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
    "is_mock_url",
]


def selenium_available() -> bool:
    try:
        import selenium  # type: ignore  # noqa: F401

        return True
    except Exception:
        return False


def is_mock_url(url: str, settings: Any) -> bool:
    """任务网址是否指向本地模拟问卷服务（本机回环地址都算）。"""
    if not url:
        return False
    low = url.lower()
    base = str(getattr(settings, "mock_base_url", "") or "").rstrip("/").lower()
    if base and low.startswith(base):
        return True
    # 端口不同（例如自测用的临时端口）也按模拟问卷处理
    return any(low.startswith(p) for p in ("http://127.0.0.1:", "http://localhost:", "http://[::1]:"))


def resolve_backend(settings: Any, url: Optional[str] = None) -> str:
    """决定实际使用的后端名称。"""
    backend = (getattr(settings, "backend", "auto") or "auto").lower()
    if backend == "mock" and url and not is_mock_url(url, settings):
        return "selenium"
    if backend == "auto":
        if url and is_mock_url(url, settings):
            return "mock"
        return "selenium" if selenium_available() else "mock"
    return backend


def create_driver(settings: Any, url: Optional[str] = None) -> FillDriver:
    backend = resolve_backend(settings, url)
    if backend == "selenium":
        if not selenium_available():
            mock_base = getattr(settings, "mock_base_url", "") or ""
            raise DriverUnavailable(
                f"任务网址 {url or ''} 需要真实浏览器后端，但当前环境没有安装 selenium。\n"
                "安装方法（任选其一）：\n"
                "  1) pip install --target vendor selenium   然后重新启动本程序\n"
                "  2) pip install selenium\n"
                "并准备与本机浏览器版本匹配的驱动（Edge: msedgedriver / Chrome: chromedriver）。\n"
                f"如果想先用离线流程验证，请把任务网址改成模拟问卷地址 {mock_base}/vm/A.aspx"
            )
        from .selenium_driver import SeleniumDriver

        return SeleniumDriver(settings)
    return MockDriver(settings)
