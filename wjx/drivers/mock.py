"""模拟问卷后端：通过本地 mock server 跑通"填写 -> 提交 -> 判定"全流程。

它请求 mock server 暴露的表单描述（等价于真实问卷的 DOM 结构），
复用同一套答案映射与结果判定逻辑，因此在没有浏览器/没有网络时
也能完整演示需求里的所有行为（成功、人数已满、校验失败、条件跳过）。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from ..models import RunResult
from .base import (
    FillDriver,
    FillOutcome,
    classify_post_submit,
    build_form_payload,
)


class MockDriver(FillDriver):
    name = "mock"
    TIMEOUT = 10

    # ------------------------------------------------------------------ #
    def _base(self) -> str:
        return str(getattr(self.settings, "mock_base_url", "http://127.0.0.1:8765")).rstrip("/")

    def _fetch_form(self, url: str) -> Dict[str, Any]:
        """GET <url>?format=json 取得表单结构；返回 {'action','fields','title'}"""
        sep = "&" if "?" in url else "?"
        probe = f"{url}{sep}format=json"
        try:
            with urllib.request.urlopen(probe, timeout=self.TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise ConnectionError(
                f"模拟问卷服务返回 {exc.code}，该网址不是模拟问卷（{_base_of(url)}/vm/A.aspx）"
            ) from exc
        except urllib.error.URLError as exc:
            raise ConnectionError(f"无法连接模拟问卷服务 ({self._base()}): {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConnectionError(f"模拟问卷服务返回了非 JSON 内容: {raw[:120]}") from exc
        if not isinstance(data, dict) or "fields" not in data:
            raise ConnectionError("模拟问卷服务未返回表单结构，请确认 mock 服务已启动且版本一致")
        return data

    # ------------------------------------------------------------------ #
    def prepare(self, task: Any) -> Optional[Any]:
        """提前"打开"页面：请求表单结构并缓存，等价于浏览器预加载。"""
        try:
            spec = self._fetch_form(task.url)
        except Exception:
            return None
        return {"spec": spec, "url": task.url}

    def close_prepared(self, handle: Any) -> None:
        return None

    # ------------------------------------------------------------------ #
    def submit(self, task: Any, prepared: Any = None,
               answers: Optional[Dict[str, Any]] = None) -> FillOutcome:
        use_answers: Dict[str, Any] = answers if answers is not None else dict(getattr(task, "answers", {}) or {})
        spec = (prepared or {}).get("spec")
        if not spec:
            try:
                spec = self._fetch_form(task.url)
            except Exception as exc:
                # 打不开问卷（服务未启动、网址错误）——与真实后端保持一致的判定
                return FillOutcome(
                    result=RunResult.NAVIGATION_ERROR.value,
                    url_before=task.url,
                    message=f"无法打开网址: {exc}",
                )
        fields: List[Dict[str, Any]] = spec.get("fields", [])
        payload = build_form_payload(fields, use_answers, prefix=spec.get("prefix", ""))
        unresolved = payload.pop("__unresolved__", "")
        unfilled = [f["key"] for f in fields if f.get("required") and not payload.get(f.get("key"))]

        # 模拟"填写"过程：一次性提交所有答案（对应真人点完所有选项后点提交）
        data = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(
            task.url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "wjx-auto/mock-driver",
            },
        )
        url_before = task.url
        url_after = task.url
        body = ""
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=self.TIMEOUT) as resp:
                url_after = resp.geturl()
                body = resp.read().decode("utf-8", "replace")
                if getattr(resp, "status", 200) in (301, 302, 303, 307, 308):
                    url_after = resp.headers.get("Location", url_after)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            url_after = exc.headers.get("Location", url_before) or url_before
        except urllib.error.URLError as exc:
            return FillOutcome(
                result=RunResult.NAVIGATION_ERROR.value,
                url_before=url_before,
                message=f"无法打开网址: {exc}",
                question_count=len(fields),
            )

        page_text, dialog = _extract_text(body, spec)
        result, message = classify_post_submit(
            url_before, url_after, page_text, dialog, spec.get("success_url_hint", "")
        )
        extra: List[str] = []
        if unresolved:
            extra.append(f"未匹配选项: {unresolved}")
        if unfilled:
            extra.append(f"必填未填: {', '.join(unfilled[:6])}")

        return FillOutcome(
            result=result,
            url_before=url_before,
            url_after=url_after,
            question_count=len(fields),
            filled_count=len(payload),
            unfilled=unfilled,
            dialog_text=dialog,
            message="；".join([message] + extra),
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不自动跟随跳转，便于检测"是否跳到了另一个界面"。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _base_of(url: str) -> str:
    import re

    m = re.match(r"^(https?://[^/]+)", url or "")
    return m.group(1) if m else ""


def _extract_text(body: str, spec: Dict[str, Any]) -> tuple:
    """从返回的 HTML 中提取页面文本与"弹窗"文本。

    模拟页面用 ``<div id="alert">`` 表示弹窗，真实问卷星用 ``.layui-layer`` 等 layer 弹窗。
    """
    import re as _re

    text = _re.sub(r"<script.*?</script>", " ", body, flags=_re.S | _re.I)
    text = _re.sub(r"<style.*?</style>", " ", text, flags=_re.S | _re.I)
    plain = _re.sub(r"<[^>]+>", " ", text)
    plain = _re.sub(r"\s+", " ", plain).strip()

    dialog = ""
    match = _re.search(r'id="(?:alert|dialog|popup)"[^>]*>(.*?)</', body, flags=_re.S | _re.I)
    if match:
        dialog = _re.sub(r"<[^>]+>", " ", match.group(1))
        dialog = _re.sub(r"\s+", " ", dialog).strip()
    return plain, dialog
