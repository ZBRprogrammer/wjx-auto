"""自动填写流程编排：题目提取 → 答案库检索 → 填写提交 → 结果记录。

所有入口（GUI / CLI / 调度器）都走这里，保证行为一致：

1. 先按题目**内容**检索答案库（顺序变化、题目增减都能应对）；
2. 命中就用答案库的答案，未命中则按题库策略留空并记入 pending 队列；
3. 任务自身的 ``answers``（按字段名）优先级最高，可临时覆盖答案库；
4. 填写提交后按"跳转=成功 / 停留+人数过多=失败"判定并写入历史。

``dry_run=True`` 时不提交，只做检索与报告（GUI 的"预演"、CLI 的 ``resolve``）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .answers import AnswerBank, Match, record_pending
from .drivers import DriverError, DriverUnavailable, FillDriver, FillOutcome, create_driver
from .logging_setup import get_logger
from .models import AppConfig, RunRecord, RunResult, Task
from .questionnaire import (Question, extract_questions_browser, fetch_questions_static,
                            parse_questions_html)

log = get_logger()


@dataclass
class Resolution:
    """一次"题目 → 答案"的检索结果。"""

    url: str = ""
    source: str = ""                 # browser | static | mock
    state: str = "ok"                # ok | not_open | closed | unknown
    open_time: str = ""              # 问卷尚未开放时的开放时间（如 "2026-10-16 16:32"）
    questions: List[Question] = field(default_factory=list)
    matches: List[Match] = field(default_factory=list)
    error: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return (not self.error) and self.state == "ok" and bool(self.questions)

    @property
    def not_open(self) -> bool:
        return self.state == "not_open"

    @property
    def answers(self) -> Dict[str, Any]:
        return {q.name: m.answer for q, m in zip(self.questions, self.matches)
                if m.ok and q.name}

    @property
    def unresolved(self) -> List[Question]:
        return [q for q, m in zip(self.questions, self.matches) if not m.ok]

    @property
    def hit_rate(self) -> float:
        if not self.questions:
            return 0.0
        return (len(self.questions) - len(self.unresolved)) / len(self.questions)

    def report(self, show_options: bool = True) -> str:
        if self.state == "not_open":
            when = f"（开放时间 {self.open_time}）" if self.open_time else ""
            return f"问卷尚未开放{when}：{self.url}"
        if self.state == "closed":
            return f"问卷已结束/停止收集：{self.url}"
        if self.error:
            return f"检索失败（{self.source}）: {self.error}"
        lines = [f"题目来源: {self.source}    共 {len(self.questions)} 题    "
                 f"命中 {len(self.questions) - len(self.unresolved)}/{len(self.questions)}"]
        for q, m in zip(self.questions, self.matches):
            mark = "✔" if m.ok else "✘"
            lines.append(f"  {mark} {q.topic}. [{q.qtype}{'*' if q.required else ''}] {q.label}")
            detail = f"        字段 {q.name}  答案: {_fmt(m.answer)}  ← {m.describe()}"
            if m.note:
                detail += f"（{m.note}）"
            lines.append(detail)
            if show_options and q.options:
                lines.append("        选项: " + " / ".join(
                    f"{o.get('label')}={o.get('value')}" for o in q.options[:10]))
        return "\n".join(lines)


def _fmt(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return f"{value!r}"


# --------------------------------------------------------------------------- #
# 题目提取（统一入口）
# --------------------------------------------------------------------------- #
def probe_questions(cfg: AppConfig, url: str, prefer_browser: bool = True,
                    bank: Optional[AnswerBank] = None, driver: Any = None,
                    html_for_state: str = "") -> Resolution:
    """提取题目并检索答案。prefer_browser=True 时用真实浏览器（最准确）。

    若问卷还没到开放时间（问卷星定时开放），会返回 state='not_open' 而不是报错，
    由调用方决定是否等待重试。
    """
    started = time.time()
    res = Resolution(url=url)
    bank = bank or AnswerBank.load()
    from .drivers import is_mock_url
    from .questionnaire import detect_page_state, extract_open_time, fetch_html

    try:
        if is_mock_url(url, cfg.settings):
            questions, source = _probe_mock(cfg, url), "mock"
        elif prefer_browser:
            questions, source = _probe_browser(cfg, url, driver), "browser"
        else:
            page = fetch_html(url)
            res.state = detect_page_state(page)
            res.open_time = extract_open_time(page) if res.state == "not_open" else ""
            if res.state in ("not_open", "closed"):
                res.source = "static"
                res.elapsed = time.time() - started
                return res
            questions, source = parse_questions_html(page), "static"
    except DriverUnavailable as exc:
        res.error = str(exc)
        res.source = "browser"
        res.elapsed = time.time() - started
        return res
    except Exception as exc:
        res.error = f"{type(exc).__name__}: {exc}"
        res.source = "browser" if prefer_browser else "static"
        res.elapsed = time.time() - started
        return res

    res.source = source
    res.questions = list(questions)
    if not res.questions:
        # 没有题目：区分"尚未开放""已结束"和"解析失败"
        html = html_for_state
        if not html:
            try:
                html = fetch_html(url)
            except Exception:
                html = ""
        if html:
            res.state = detect_page_state(html)
            if res.state == "not_open":
                res.open_time = extract_open_time(html)
        if res.state == "unknown":
            res.state = "unknown"
    res.matches = bank.resolve_many(res.questions)
    res.elapsed = time.time() - started
    return res


# 问卷尚未开放时的等待策略
NOT_OPEN_POLL_SECONDS = 10.0
NOT_OPEN_MAX_WAIT_SECONDS = 240.0


def probe_until_open(cfg: AppConfig, url: str, bank: Optional[AnswerBank] = None,
                     prefer_browser: bool = True, max_wait: float = NOT_OPEN_MAX_WAIT_SECONDS,
                     poll: float = NOT_OPEN_POLL_SECONDS,
                     on_wait: Optional[Any] = None) -> Resolution:
    """若问卷尚未开放，则每隔 poll 秒重试一次，最多等 max_wait 秒。"""
    deadline = time.time() + max(0.0, max_wait)
    attempt = 0
    while True:
        attempt += 1
        res = probe_questions(cfg, url, prefer_browser=prefer_browser, bank=bank)
        if not res.not_open:
            return res
        remaining = deadline - time.time()
        if remaining <= 1:
            log.warning("问卷仍未开放（开放时间 %s），已等待到上限", res.open_time or "未知")
            return res
        if on_wait:
            try:
                on_wait(res, remaining, attempt)
            except Exception:
                pass
        log.info("问卷尚未开放（开放时间 %s），%.0f 秒后重试（第 %d 次，剩余等待 %.0f 秒）",
                 res.open_time or "未知", poll, attempt, remaining)
        time.sleep(min(poll, max(0.5, remaining)))
    return res


def _probe_browser(cfg: AppConfig, url: str, driver: Any = None) -> List[Question]:
    own = driver is None
    if own:
        driver = create_driver(cfg.settings, url)
        if not hasattr(driver, "_new_browser"):
            raise DriverUnavailable("当前后端不支持浏览器题目提取")
        driver = driver._new_browser()
    try:
        driver.get(url)
        time.sleep(1.0)
        return extract_questions_browser(driver)
    finally:
        if own:
            try:
                driver.quit()
            except Exception:
                pass


def _probe_mock(cfg: AppConfig, url: str) -> List[Question]:
    """模拟问卷：用 mock 后端暴露的表单结构转成 Question（与真实流程同构）。"""
    import json
    import urllib.request

    sep = "&" if "?" in url else "?"
    with urllib.request.urlopen(f"{url}{sep}format=json", timeout=10) as resp:
        spec = json.loads(resp.read().decode("utf-8", "replace"))
    out: List[Question] = []
    for i, f in enumerate(spec.get("fields", [])):
        out.append(Question(
            topic=i + 1,
            label=str(f.get("label") or f.get("key") or ""),
            name=str(f.get("key") or f"q{i + 1}"),
            qtype=str(f.get("type") or "text"),
            required=bool(f.get("required")),
            options=[{"value": str(o.get("value")), "label": str(o.get("label"))}
                     for o in (f.get("options") or [])],
            container_index=i,
        ))
    return out


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
def run_task(cfg: AppConfig, task: Task, bank: Optional[AnswerBank] = None,
             driver: Any = None, dry_run: bool = False,
             prepared: Any = None) -> RunRecord:
    """执行一个任务：自动检索答案库并填写提交（dry_run 时只报告不提交）。"""
    bank = bank or AnswerBank.load()
    rec = RunRecord(started_at=_now(), dry_run=dry_run)
    rec.url_before = task.url

    res = probe_questions(cfg, task.url, prefer_browser=True, bank=bank, driver=driver)
    if res.state == "not_open":
        rec.result = RunResult.NOT_OPEN.value
        rec.message = res.report()
        rec.finished_at = _now()
        if not dry_run:
            task.record(rec)
        return rec
    if res.state == "closed":
        rec.result = RunResult.CLOSED.value
        rec.message = res.report()
        rec.finished_at = _now()
        if not dry_run:
            task.record(rec)
        return rec
    if not res.ok:
        rec.result = _classify_probe_error(res.error, res.state)
        rec.message = res.error or (
            "未能识别到任何题目（页面可能已结束、需登录或模板未适配）"
            if res.state == "unknown" else "未能识别到题目"
        )
        rec.finished_at = _now()
        if not dry_run:
            task.record(rec)
        return rec

    if res.unresolved:
        record_pending(
            [{"question": q.label, "field": q.name, "qtype": q.qtype,
              "options": q.options} for q in res.unresolved],
            url=task.url,
        )

    rec.message = (f"识别 {len(res.questions)} 题，答案库命中 "
                   f"{len(res.questions) - len(res.unresolved)} 题")
    if res.unresolved:
        rec.message += "；未命中: " + "; ".join(q.label[:24] for q in res.unresolved[:4])

    if dry_run:
        rec.result = RunResult.SUCCESS.value if not res.unresolved else RunResult.UNKNOWN.value
        rec.message = "（预演，未提交）" + rec.message
        rec.finished_at = _now()
        return rec

    fill_driver = driver or create_driver(cfg.settings, task.url)
    answers = dict(res.answers)
    # 任务自定义答案优先级最高（按字段名），用于临时覆盖答案库
    for k, v in (task.answers or {}).items():
        if v not in (None, "", []):
            answers[k] = v

    task.answers_for_run = answers
    try:
        if isinstance(fill_driver, FillDriver) or hasattr(fill_driver, "submit"):
            try:
                outcome = fill_driver.submit(task, prepared, answers=answers)
            except TypeError:
                # 后端 submit 不支持 answers 参数时，写回任务上再提交
                task.answers = answers
                outcome = fill_driver.submit(task, prepared)
        else:  # pragma: no cover
            raise DriverError("后端未实现 submit")
    except DriverUnavailable as exc:
        rec.result = RunResult.DRIVER_ERROR.value
        rec.message = str(exc)
        rec.finished_at = _now()
        task.record(rec)
        return rec
    except DriverError as exc:
        rec.result = RunResult.DRIVER_ERROR.value
        rec.message = str(exc)
        rec.finished_at = _now()
        task.record(rec)
        return rec
    except Exception as exc:
        rec.result = RunResult.DRIVER_ERROR.value
        rec.message = f"{type(exc).__name__}: {exc}"
        rec.finished_at = _now()
        task.record(rec)
        return rec

    rec.result = outcome.result
    rec.url_after = outcome.url_after
    rec.dialog_text = outcome.dialog_text
    rec.message = f"{outcome.message}｜{rec.message}"
    if outcome.unfilled:
        rec.message += f"｜未填: {', '.join(str(x)[:16] for x in outcome.unfilled[:4])}"
    rec.finished_at = _now()
    task.record(rec)
    return rec


def _now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 判定"打不开页面"的关键字（与 scheduler 中的处理保持一致）
UNREACHABLE_HINTS = (
    "ConnectionError", "无法连接", "无法打开", "refused", "timed out", "超时",
    "URLError", "NameNotResolved", "404", "不是模拟问卷", "HTTP Error",
    "net::ERR", "ERR_",
)


def _classify_probe_error(error: str, state: str = "") -> str:
    """把"读题失败"归类到具体结果，便于条件规则区分处理。"""
    if state == "not_open":
        return RunResult.NOT_OPEN.value
    if state == "closed":
        return RunResult.CLOSED.value
    text = error or ""
    if any(k in text for k in UNREACHABLE_HINTS):
        return RunResult.NAVIGATION_ERROR.value
    if "安全校验" in text or "验证码" in text:
        return RunResult.CAPTCHA.value
    return RunResult.DRIVER_ERROR.value
