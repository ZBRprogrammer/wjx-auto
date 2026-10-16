"""问卷结构提取：拿到"每题问的是什么 + 题目对应哪个字段"。

两种来源
--------
* ``fetch_questions_static(url)``  直接 HTTP 取页面，用 BeautifulSoup 解析（不需要浏览器）
* ``extract_questions_js()``       注入到 selenium 页面里执行，返回同样的结构（最准确）

统一结构 :class:`Question`::

    Question(topic=1, label="11-4", name="q1", qtype="text",
             required=True, options=[], container_index=0)

真实问卷星的 DOM（本项目实测，2026-10）::

    <fieldset class='fieldset' id='fieldset1'>
      <div class='field ui-field-contain' topic='1' id='div1' req='1' type='1'
           data-role='fieldcontain'>
        <div class='field-label'><span class='req'>*</span>
          <div class='topicnumber'>1.</div>
          <div class='topichtml'>11-4</div></div>
        <div class='ui-input-text'><input type='text' id='q1' name='q1'/></div>
      </div>
      ...
    </fieldset>
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .logging_setup import get_logger

log = get_logger()

# 题目容器选择器（按优先级依次尝试，命中即用）
CONTAINER_SELECTORS: Sequence[str] = (
    "div.field[data-role=fieldcontain]",
    "div.field.ui-field-contain",
    "div.field",
    "fieldset > div.field",
    "fieldset",
    ".qnr-question",
    ".div_question",
)

# 题干所在元素的选择器（依次尝试）
LABEL_SELECTORS: Sequence[str] = (
    ".topichtml",
    ".field-label .topichtml",
    ".div_title_question",
    ".field-label",
    "legend",
    ".qtitle",
)

NUMBER_PREFIX_RE = re.compile(r"^\s*[\(（]?\s*\d{1,3}\s*[\.、\)）:：]\s*")

# 问卷尚未到开放时间（问卷星"定时开放"）——实测文案：
#   "很抱歉，此问卷将于2026-10-16 16:32（北京时间）开放，请到时再进入此页面进行填写！"
#   "距离开始还有0天0时9分59秒"
NOT_OPEN_STRONG: Sequence[str] = (
    "开放，请到时再进入", "请到时再进入", "距离开始还有", "尚未开始",
    "问卷尚未开始", "还未开始", "未到开放时间", "不在开放时间",
)
# 问卷已结束 / 停止收集
CLOSED_PATTERNS: Sequence[str] = (
    "已结束", "已停止", "停止收集", "问卷已关闭", "不在收集", "已暂停",
)
# "将于 <时间> 开放" 形态（时间可以是具体时刻或"倒计时"）
NOT_OPEN_OPEN_RE = re.compile(r"将于.{0,24}?(?:开放|开始)")
_DATETIME_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}")
_DHM_RE = re.compile(r"\d+\s*天\s*\d+\s*时\s*\d+\s*分")


def detect_page_state(html: str) -> str:
    """从页面文本判断问卷状态：'not_open' | 'closed' | 'ok' | 'unknown'。

    调用时机：**解析不出任何题目**时，用来区分"尚未开放/已结束"和"解析失败"；
    有题目的正常页面由调用方直接按 ok 处理。
    """
    text = re.sub(r"<script.*?</script>", " ", html or "", flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    if not text.strip():
        return "unknown"
    if any(p in text for p in NOT_OPEN_STRONG):
        return "not_open"
    if NOT_OPEN_OPEN_RE.search(text) and (_DATETIME_RE.search(text) or _DHM_RE.search(text)):
        return "not_open"
    if any(p in text for p in CLOSED_PATTERNS):
        return "closed"
    return "ok"


def extract_open_time(html: str) -> str:
    """尽量从提示页里提取开放时间（用于日志与提醒）。"""
    text = re.sub(r"<[^>]+>", " ", html or "")
    m = re.search(r"将于\s*(\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)", text)
    if m:
        return m.group(1)
    m = _DATETIME_RE.search(text)
    return m.group(0) if m else ""


@dataclass
class Question:
    topic: int = 0
    label: str = ""
    name: str = ""
    qtype: str = "text"            # text | textarea | radio | checkbox | select | matrix | unknown
    required: bool = False
    options: List[Dict[str, str]] = field(default_factory=list)
    container_index: int = 0
    container_selector: str = ""
    page: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Question":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (d or {}).items() if k in known})

    def option_labels(self) -> List[str]:
        return [str(o.get("label", "")) for o in self.options]


# --------------------------------------------------------------------------- #
# 清洗
# --------------------------------------------------------------------------- #
def clean_label(text: str) -> str:
    """去掉题号前缀、必填星号与多余空白，但**保留算式里的乘号**。

    例: ``"* 5*8"`` -> ``"5*8"``；``"1. 11-4"`` -> ``"11-4"``
    """
    if not text:
        return ""
    s = text.replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s)
    # 只把"独立出现"的星号当必填标记删掉，保留 5*8 里的乘号
    s = re.sub(r"(?:(?<=\s)|^)\*(?=\s|$)", " ", s)
    s = re.sub(r"\s*\*\s*", "*", s) if re.search(r"\d\s*\*\s*\d", s) else s
    s = s.strip().strip("*").strip() if not re.search(r"\d\s*\*\s*\d", s) else s.strip()
    s = NUMBER_PREFIX_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- #
# 静态解析（HTTP + BeautifulSoup）
# --------------------------------------------------------------------------- #
def parse_questions_html(html: str) -> List[Question]:
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:  # pragma: no cover
        log.warning("未安装 beautifulsoup4，无法静态解析题目；请用 probe --browser 或安装 bs4")
        return []

    soup = BeautifulSoup(html, "html.parser")

    containers: List[Any] = []
    used_selector = ""
    for sel in CONTAINER_SELECTORS:
        found = soup.select(sel)
        # 过滤掉明显不是题目的（没有输入控件）
        found = [c for c in found if c.find(["input", "textarea", "select"])]
        if found:
            containers, used_selector = found, sel
            break

    questions: List[Question] = []
    for idx, c in enumerate(containers):
        q = _parse_container(c, idx, used_selector)
        if q is not None:
            questions.append(q)
    return questions


def _parse_container(c: Any, index: int, selector: str) -> Optional[Question]:
    controls = c.find_all(["input", "textarea", "select"])
    controls = [x for x in controls if (x.get("type") or "").lower() != "hidden"]
    if not controls:
        return None

    label = ""
    for sel in LABEL_SELECTORS:
        node = c.select_one(sel)
        if node:
            text = clean_label(node.get_text(" ", strip=True))
            if text:
                label = text
                break
    if not label:
        label = clean_label(c.get_text(" ", strip=True))[:200]

    # 题型判定
    types = {(x.get("type") or "").lower() for x in controls if x.name == "input"}
    radios = [x for x in controls if (x.get("type") or "").lower() == "radio"]
    checks = [x for x in controls if (x.get("type") or "").lower() == "checkbox"]
    selects = [x for x in controls if x.name == "select"]
    textareas = [x for x in controls if x.name == "textarea"]
    texts = [x for x in controls if x.name == "input" and (x.get("type") or "text").lower() in ("text", "")]

    if radios:
        qtype, primary = "radio", radios
    elif checks:
        qtype, primary = "checkbox", checks
    elif selects:
        qtype, primary = "select", selects
    elif textareas:
        qtype, primary = "textarea", textareas
    elif texts:
        qtype, primary = "text", texts
    else:
        qtype, primary = "unknown", controls

    names = {x.get("name") for x in primary if x.get("name")}
    if len(names) > 1 and qtype in ("radio", "checkbox"):
        qtype = "matrix"

    # 选项
    options: List[Dict[str, str]] = []
    if qtype == "select" and selects:
        for opt in selects[0].find_all("option"):
            options.append({"value": str(opt.get("value", "")), "label": opt.get_text(strip=True)})
    elif qtype in ("radio", "checkbox"):
        for x in primary:
            opt_label = ""
            lb = x.find_parent("label")
            if lb:
                opt_label = lb.get_text(" ", strip=True)
            if not opt_label and x.parent is not None:
                nxt = x.find_next_sibling()
                if nxt is not None:
                    opt_label = nxt.get_text(" ", strip=True)
            if not opt_label and x.parent is not None:
                opt_label = x.parent.get_text(" ", strip=True)
            options.append({"value": str(x.get("value", "")), "label": clean_label(opt_label)})

    name = next((x.get("name") for x in primary if x.get("name")), "")
    required = bool(c.get("req") == "1" or c.select_one(".req"))
    topic_raw = c.get("topic")
    try:
        topic = int(topic_raw) if topic_raw is not None else index + 1
    except (TypeError, ValueError):
        topic = index + 1

    return Question(topic=topic, label=label, name=name or f"q{topic}",
                    qtype=qtype, required=required, options=options,
                    container_index=index, container_selector=selector)


def fetch_questions_static(url: str, timeout: int = 20) -> List[Question]:
    """不启动浏览器，直接取页面解析题目。"""
    html = fetch_html(url, timeout)
    return parse_questions_html(html)


def fetch_html(url: str, timeout: int = 20) -> str:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise ConnectionError(f"无法打开问卷页面: {exc}") from exc


# --------------------------------------------------------------------------- #
# 浏览器内提取（selenium 用，最准确）
# --------------------------------------------------------------------------- #
EXTRACT_JS = r"""
const selectors = arguments[0];
let containers = [], used = '';
for (const s of selectors) {
  let found = Array.from(document.querySelectorAll(s));
  found = found.filter(c => c.querySelector('input:not([type=hidden]), textarea, select'));
  if (found.length) { containers = found; used = s; break; }
}
const labelSels = arguments[1];
const clean = t => {
  let s = (t || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  // 只删"独立出现"的必填星号，保留 5*8 的乘号
  if (!/\d\s*\*\s*\d/.test(s)) {
    s = s.replace(/(^|\s)\*(?=\s|$)/g, ' ').replace(/^\*+|\*+$/g, ' ').replace(/\s+/g, ' ').trim();
  } else {
    s = s.replace(/\s*\*\s*/g, '*');
  }
  return s.replace(/^[\(（]?\s*\d{1,3}\s*[\.、\)）:：]\s*/, '').trim();
};

return containers.map((c, i) => {
  let label = '';
  for (const ls of labelSels) {
    const n = c.querySelector(ls);
    if (n) { const t = clean(n.innerText || n.textContent); if (t) { label = t; break; } }
  }
  if (!label) label = clean(c.innerText || '');
  const all = Array.from(c.querySelectorAll('input:not([type=hidden]), textarea, select'));
  const byType = t => all.filter(e => (e.getAttribute('type') || e.tagName.toLowerCase()) === t);
  const radios = byType('radio'), checks = byType('checkbox'),
        selects = all.filter(e => e.tagName === 'SELECT'),
        areas = all.filter(e => e.tagName === 'TEXTAREA'),
        texts = all.filter(e => e.tagName === 'INPUT' && ['text','', null].includes(e.getAttribute('type')));
  let qtype = 'unknown', primary = all;
  if (radios.length) { qtype = 'radio'; primary = radios; }
  else if (checks.length) { qtype = 'checkbox'; primary = checks; }
  else if (selects.length) { qtype = 'select'; primary = selects; }
  else if (areas.length) { qtype = 'textarea'; primary = areas; }
  else if (texts.length) { qtype = 'text'; primary = texts; }
  const names = new Set(primary.map(e => e.name).filter(Boolean));
  if (names.size > 1 && (qtype === 'radio' || qtype === 'checkbox')) qtype = 'matrix';
  let options = [];
  if (qtype === 'select' && selects.length) {
    options = Array.from(selects[0].options).map(o => ({value: o.value, label: clean(o.text)}));
  } else if (qtype === 'radio' || qtype === 'checkbox') {
    options = primary.map(e => {
      let lab = '';
      const lb = e.closest('label');
      if (lb) lab = clean(lb.innerText);
      if (!lab) { const nx = e.nextElementSibling; if (nx) lab = clean(nx.innerText); }
      if (!lab && e.parentElement) lab = clean(e.parentElement.innerText);
      return {value: e.value, label: lab};
    });
  }
  const firstName = primary.map(e => e.name).find(Boolean) || ('q' + (i + 1));
  return {
    topic: parseInt(c.getAttribute('topic') || String(i + 1), 10),
    label: label, name: firstName, qtype: qtype,
    required: c.getAttribute('req') === '1' || !!c.querySelector('.req'),
    options: options, container_index: i, container_selector: used,
  };
});
"""


def extract_questions_browser(driver: Any) -> List[Question]:
    """在已打开的 selenium 页面上提取题目。"""
    raw = driver.execute_script(EXTRACT_JS, list(CONTAINER_SELECTORS), list(LABEL_SELECTORS))
    return [Question.from_dict(d) for d in (raw or [])]


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def questions_report(questions: Sequence[Question]) -> str:
    lines = [f"共识别到 {len(questions)} 道题："]
    for q in questions:
        opts = ""
        if q.options:
            opts = "  选项: " + " / ".join(
                f"{o['label']}({o['value']})" for o in q.options[:8]
            )
        lines.append(
            f"  {q.topic}. [{q.qtype}{'*' if q.required else ''}] {q.label}  -> 字段 {q.name}{opts}"
        )
    return "\n".join(lines)
