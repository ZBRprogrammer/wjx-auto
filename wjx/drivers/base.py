"""填写后端：统一接口 + 提交结果判定。

后端
----
``mock``      本地模拟问卷（默认，离线可跑通全流程，用于 demo/自测）
``selenium``  真实浏览器填写问卷星（需要 selenium + Edge/Chrome 驱动）

判定规则（需求原文）
--------------------
* 成功：点击提交后跳转到另一个界面（URL 改变 / 出现"提交成功"页）。
* 失败：点击提交后停留在原页面，并且弹出提示"该问卷填写人数过多"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..models import RunResult

# 问卷星"人数已满"类提示的常见文案（弹窗 / 页内横幅）
FULL_PATTERNS: Sequence[str] = (
    "填写人数过多",
    "人数过多",
    "已达上限",
    "已达最大",
    "份数已满",
    "已满",
    "停止收集",
    "已停止",
    "问卷已结束",
    "该问卷已结束",
    "感谢参与人数已满",
)

SUCCESS_PATTERNS: Sequence[str] = (
    "提交成功",
    "问卷已提交",
    "感谢您的参与",
    "感谢参与",
    "答卷已提交",
)

VALIDATION_PATTERNS: Sequence[str] = (
    "必填",
    "请填写",
    "请选择",
    "不能为空",
    "题目未答",
)

BLOCKED_PATTERNS: Sequence[str] = (
    "需要登录",
    "请先登录",
    "无权访问",
)


class DriverError(RuntimeError):
    """后端不可用或执行异常。"""


class DriverUnavailable(DriverError):
    """缺少依赖（例如未安装 selenium）。"""


@dataclass
class FillOutcome:
    result: str = RunResult.UNKNOWN.value
    url_before: str = ""
    url_after: str = ""
    question_count: int = 0
    filled_count: int = 0
    unfilled: List[str] = field(default_factory=list)
    dialog_text: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.result == RunResult.SUCCESS.value

    @property
    def is_full(self) -> bool:
        return self.result == RunResult.FULL.value


# --------------------------------------------------------------------------- #
# 判定辅助（两个后端共用）
# --------------------------------------------------------------------------- #
def _norm(url: str) -> str:
    return re.sub(r"#.*$", "", (url or "").strip().rstrip("/")).lower()


def classify_post_submit(
    url_before: str,
    url_after: str,
    page_text: str = "",
    dialog_text: str = "",
    success_url_hint: str = "",
) -> tuple[str, str]:
    """依据提交后的页面状态判定结果，返回 (RunResult, 说明)。

    优先级：跳转成功 > 人数已满 > 校验失败 > 未知。
    """
    blob = f"{dialog_text}\n{page_text}"
    for pat in FULL_PATTERNS:
        if pat and pat in blob:
            return RunResult.FULL.value, f"检测到提示：{_snippet(blob, pat)}"

    navigated = bool(url_after) and _norm(url_after) != _norm(url_before)
    if success_url_hint and url_after and success_url_hint.lower() in url_after.lower():
        navigated = True
    if navigated:
        # 跳转也可能跳到"已结束"页，需再排除
        for pat in FULL_PATTERNS:
            if pat and pat in blob and pat in ("问卷已结束", "该问卷已结束", "已停止收集"):
                return RunResult.FULL.value, f"跳转到结束页：{_snippet(blob, pat)}"
        for pat in SUCCESS_PATTERNS:
            if pat in blob:
                return RunResult.SUCCESS.value, f"跳转成功页并检测到「{pat}」"
        return RunResult.SUCCESS.value, "提交后页面已跳转（URL 变化）"

    for pat in BLOCKED_PATTERNS:
        if pat in blob:
            return RunResult.BLOCKED.value, f"问卷不可用：{_snippet(blob, pat)}"
    for pat in VALIDATION_PATTERNS:
        if pat in blob:
            return RunResult.VALIDATION_FAILED.value, f"仍停留原页面：{_snippet(blob, pat)}"
    if dialog_text:
        return RunResult.VALIDATION_FAILED.value, f"仍停留原页面，弹窗内容：{dialog_text[:120]}"
    return RunResult.UNKNOWN.value, "仍停留原页面，未识别到明确提示"


def _snippet(blob: str, pat: str, width: int = 60) -> str:
    idx = blob.find(pat)
    if idx < 0:
        return pat
    start = max(0, idx - width // 2)
    text = blob[start:idx + len(pat) + width // 2].replace("\n", " ").strip()
    return text


# --------------------------------------------------------------------------- #
# 后端接口
# --------------------------------------------------------------------------- #
class FillDriver:
    """填写后端接口。"""

    name = "base"

    def __init__(self, settings: Any):
        self.settings = settings

    def prepare(self, task: Any) -> Optional[Any]:
        """提前打开页面（后台预加载）。返回句柄，失败可返回 None。"""
        return None

    def close_prepared(self, handle: Any) -> None:
        try:
            if handle is not None:
                handle.quit()
        except Exception:
            pass

    def submit(self, task: Any, prepared: Any = None) -> FillOutcome:
        raise NotImplementedError

    def describe_target(self, task: Any) -> str:
        return task.url


# --------------------------------------------------------------------------- #
# 答案 -> 表单字段的映射（mock 与 selenium 共用语义）
# --------------------------------------------------------------------------- #
def build_form_payload(fields: Sequence[Dict[str, Any]], answers: Dict[str, Any], prefix: str = "") -> Dict[str, str]:
    """把任务答案映射为 {字段名: 提交值}。

    * 单选/多选：值可以是选项序号("1")、序号列表["1","3"] 或选项文案("A")；
      序号与文案都会被翻译成该选项的 ``value``，多选用逗号连接。
    * 下拉：同单选。
    * 填空：原样文本；``{"text": "..."}`` 亦可。
    """
    payload: Dict[str, str] = {}
    unresolved: List[str] = []

    for f in fields:
        key = f.get("key") or f.get("name") or ""
        if not key:
            continue
        name = f"{prefix}{key}"
        ftype = (f.get("type") or "text").lower()
        raw = answers.get(key, answers.get(name))
        if raw is None:
            continue
        options: List[Dict[str, Any]] = f.get("options") or []
        values: List[str] = []

        if ftype in ("radio", "select", "checkbox") and options:
            picks = raw if isinstance(raw, (list, tuple)) else [raw]
            wanted = [str(p).strip() for p in picks if str(p).strip() != ""]
            for want in wanted:
                hit = _match_option(options, want)
                if hit is None:
                    unresolved.append(f"{key}={want}")
                else:
                    values.append(str(hit.get("value")))
            if ftype == "checkbox":
                payload[name] = ",".join(values)
            else:
                if values:
                    payload[name] = values[0]
        else:
            if isinstance(raw, (list, tuple)):
                payload[name] = ",".join(str(x) for x in raw)
            elif isinstance(raw, dict):
                payload[name] = str(raw.get("text", raw.get("value", "")))
            else:
                payload[name] = str(raw)

    if unresolved:
        payload["__unresolved__"] = "; ".join(unresolved)
    return payload


def _match_option(options: Sequence[Dict[str, Any]], want: str) -> Optional[Dict[str, Any]]:
    w = want.strip()
    # 1) 精确 value
    for o in options:
        if str(o.get("value")) == w:
            return o
    # 2) 序号（1 起）
    if w.isdigit():
        idx = int(w)
        if 1 <= idx <= len(options):
            return options[idx - 1]
    # 3) 选项文案（含 label 完全匹配 / 子串匹配）
    wl = w.lower()
    for o in options:
        if str(o.get("label", "")).strip().lower() == wl:
            return o
    for o in options:
        if wl and wl in str(o.get("label", "")).strip().lower():
            return o
    return None
