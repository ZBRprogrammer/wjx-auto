"""默认答案库：按"题目内容"检索并给出答案。

设计目标（来自实际使用场景）
--------------------------
* 题目**顺序会变** → 一律按题目文本匹配，字段名(q1/q2/...)只作辅助线索。
* 题目**会增加** → 未命中的题目会被单独收集（pending），便于事后补充答案。
* 题目**基本固定** → 答案库是显式规则表，命中即用，可人工维护。

答案库（data/answer_bank.json）::

    {
      "version": 2,
      "settings": {"unknown_policy": "leave_blank", "remember_unknown": true},
      "rules": [
        {"id": "r1", "match": "text",  "pattern": "你更倾向哪一种方案", "answer": "方案一"},
        {"id": "r2", "match": "regex", "pattern": "^\\s*性别", "answer": "男"},
        {"id": "r3", "match": "expr",  "pattern": "arithmetic", "answer": "表达式计算结果"},
        {"id": "r4", "match": "text",  "pattern": "多选题示例",
         "answer": ["A", "C"]},
        {"id": "r5", "match": "text",  "pattern": "填空题", "answer": "固定文本"}
      ],
      "remembered": {
        "11-4": {"answer": "7", "rule": "arithmetic:11-4", "hits": 3, "updated_at": "..."}
      }
    }

匹配优先级（从高到低）
----------------------
1. 任务自带的 answers（按字段名）——临时覆盖
2. ``remembered``：按题目原文精确命中的"记住的答案"
3. ``rules``：text 精确 > text 子串 > regex > expr
4. 未命中 → 按 ``unknown_policy`` 处理（默认留空 + 记入 pending）
"""

from __future__ import annotations

import ast
import json
import operator
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .logging_setup import get_logger
from .paths import DATA_DIR

log = get_logger()

BANK_FILE = DATA_DIR / "answer_bank.json"
PENDING_FILE = DATA_DIR / "pending_questions.json"

CONFIDENCE_TEXT = {
    "override": "任务覆盖",
    "remembered": "记住的答案",
    "rule-text": "答案库(文本)",
    "rule-regex": "答案库(正则)",
    "rule-expr": "答案库(表达式)",
    "arithmetic": "自动计算",
    "unknown": "未命中",
}


# --------------------------------------------------------------------------- #
# 算术表达式求值（安全，不用 eval）
# --------------------------------------------------------------------------- #
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

# 允许出现的字符（含中文运算符与常见全角符号）
_EXPR_CHARS = re.compile(r"^[\s0-9\.\+\-\*/%×÷xX（）\(\)\^]+$")

_CN_OPS = [
    ("加上", "+"), ("加", "+"), ("减去", "-"), ("减", "-"),
    ("乘以", "*"), ("乘", "*"), ("除以", "/"), ("除", "/"),
    ("×", "*"), ("x", "*"), ("X", "*"), ("÷", "/"), ("（", "("), ("）", ")"),
    ("等于多少", ""), ("等于几", ""), ("等于", ""), ("是多少", ""), ("多少", ""),
    ("＝", "="), ("=", "="), ("？", ""), ("?", ""),
]

ARITHMETIC_HINT_RE = re.compile(r"\d\s*(?:[\+\-\*/%×÷]|[加减乘除])\s*\d")


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return float(node.value)
        raise ValueError(f"不支持的常量: {node.value!r}")
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ValueError("不支持的运算符")
        return op(_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError("不支持的一元运算符")
        return op(_eval_node(node.operand))
    raise ValueError(f"不支持的表达式节点: {type(node).__name__}")


def normalize_expr(text: str) -> Optional[str]:
    """把题目文本规范成可计算的算术式；不是算术题则返回 None。"""
    if not text:
        return None
    s = text.strip()
    # 去掉题号前缀 "1." / "1、" / "(1)" 与空格
    s = re.sub(r"^\s*[\(（]?\d{1,3}\s*[\.、\)）:：]\s*", "", s)
    # 中文运算符 -> 符号
    for cn, sym in _CN_OPS:
        s = s.replace(cn, sym)
    s = s.replace("^", "**").replace(" ", "")
    # 只保留 "=" 之后没有内容的情况（如 "6+7=" / "6+7=?"）
    if "=" in s:
        left, _, right = s.partition("=")
        if right.strip():
            return None            # 形如 "1+1=2" 是判断题，不是求值题
        s = left
    if not s or not _EXPR_CHARS.match(s):
        return None
    if not ARITHMETIC_HINT_RE.search(s):
        return None
    try:
        tree = ast.parse(s, mode="eval")
    except SyntaxError:
        return None
    try:
        _eval_node(tree)            # 预检：确保可求值
    except Exception:
        return None
    return s


def eval_arithmetic(text: str) -> Optional[str]:
    """计算算术题的答案；成功返回字符串结果（整数不带小数点）。"""
    expr = normalize_expr(text)
    if not expr:
        return None
    try:
        value = _eval_node(ast.parse(expr, mode="eval"))
    except Exception:
        return None
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.6g}"


def looks_arithmetic(text: str) -> bool:
    return normalize_expr(text) is not None


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class AnswerRule:
    id: str = ""
    match: str = "text"            # text | regex | expr
    pattern: str = ""
    answer: Any = ""               # str | list[str] | {"option": ...}
    note: str = ""
    enabled: bool = True
    _regex: Any = field(default=None, repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AnswerRule":
        rule = cls(
            id=str(d.get("id") or ""),
            match=str(d.get("match") or "text").lower(),
            pattern=str(d.get("pattern") or ""),
            answer=d.get("answer", ""),
            note=str(d.get("note") or ""),
            enabled=bool(d.get("enabled", True)),
        )
        if rule.match == "regex":
            try:
                rule._regex = re.compile(rule.pattern, re.I)
            except re.error as exc:
                log.warning("答案库正则无效 id=%s: %s", rule.id, exc)
                rule._regex = None
        elif rule.match == "text" and rule.pattern:
            rule._regex = re.compile(re.escape(rule.pattern), re.I)
        return rule

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "match": self.match, "pattern": self.pattern,
            "answer": self.answer, "note": self.note, "enabled": self.enabled,
        }

    def matches(self, label: str) -> bool:
        if not self.enabled or not self.pattern:
            return False
        if self.match == "expr":
            return self.pattern == "arithmetic" and looks_arithmetic(label)
        if self._regex is None:
            return False
        return bool(self._regex.search(label or ""))


@dataclass
class Remembered:
    answer: Any
    rule: str = ""
    hits: int = 0
    updated_at: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Remembered":
        return cls(answer=d.get("answer", ""), rule=str(d.get("rule") or ""),
                   hits=int(d.get("hits") or 0), updated_at=str(d.get("updated_at") or ""))

    def to_dict(self) -> Dict[str, Any]:
        return {"answer": self.answer, "rule": self.rule, "hits": self.hits,
                "updated_at": self.updated_at}


@dataclass
class Match:
    answer: Any = ""
    source: str = "unknown"        # override | remembered | rule-text/... | arithmetic
    confidence: int = 0            # 0~100，便于 GUI 显示
    note: str = ""
    question: str = ""
    field: str = ""

    @property
    def ok(self) -> bool:
        if self.source == "unknown" or self.answer in ("", None, []):
            return False
        # "未配置" 占位答案只用于提示，不会真的提交
        if isinstance(self.answer, str) and self.answer.startswith("未配置"):
            return False
        if isinstance(self.answer, (list, tuple)) and not self.answer:
            return False
        return True

    def describe(self) -> str:
        return f"{CONFIDENCE_TEXT.get(self.source, self.source)}({self.confidence}%)"


# --------------------------------------------------------------------------- #
# 答案库
# --------------------------------------------------------------------------- #
class AnswerBank:
    def __init__(self, rules: Optional[List[AnswerRule]] = None,
                 remembered: Optional[Dict[str, Remembered]] = None,
                 settings: Optional[Dict[str, Any]] = None,
                 path: Optional[Path] = None):
        self.rules: List[AnswerRule] = rules or []
        self.remembered: Dict[str, Remembered] = remembered or {}
        self.settings: Dict[str, Any] = settings or {}
        self.path = path or BANK_FILE

    # -- 读写 -------------------------------------------------------------- #
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AnswerBank":
        path = path or BANK_FILE
        if not path.exists():
            bank = cls(path=path)
            bank.rules = default_rules()
            return bank
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            log.error("答案库无法解析，使用内置默认规则: %s (%s)", path, exc)
            bank = cls(path=path)
            bank.rules = default_rules()
            return bank
        rules = [AnswerRule.from_dict(r) for r in data.get("rules", [])]
        remembered = {k: Remembered.from_dict(v) for k, v in (data.get("remembered") or {}).items()}
        return cls(rules=rules, remembered=remembered,
                   settings=data.get("settings") or {}, path=path)

    def save(self, path: Optional[Path] = None) -> None:
        target = path or self.path
        from .config import atomic_write_json

        atomic_write_json(target, self.to_dict())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": 2,
            "settings": self.settings or {"unknown_policy": "leave_blank", "remember_unknown": True},
            "rules": [r.to_dict() for r in self.rules],
            "remembered": {k: v.to_dict() for k, v in self.remembered.items()},
        }

    # -- 维护 -------------------------------------------------------------- #
    def _unique_id(self, base: str) -> str:
        """生成不与现有规则冲突的 id（中文题目也要能得到可读 id）。"""
        slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", base or "")[:12]
        if not slug:
            slug = "rule"
        candidate = f"r{len(self.rules) + 1}-{slug}"
        existing = {r.id for r in self.rules}
        if candidate not in existing:
            return candidate
        n = 2
        while f"{candidate}-{n}" in existing:
            n += 1
        return f"{candidate}-{n}"

    def add_rule(self, pattern: str, answer: Any, match: str = "text",
                 note: str = "", rule_id: str = "") -> AnswerRule:
        """新增规则；pattern 相同且答案也相同的规则会被视为重复而跳过（幂等）。

        同一题目重复点"新增"时，会**更新**已有规则的答案，而不是插入重复项。
        """
        match = (match or "text").lower()
        for existing in self.rules:
            if existing.pattern == pattern and existing.match == match:
                existing.answer = answer
                existing.note = note or existing.note
                existing.enabled = True
                if match == "regex":
                    try:
                        existing._regex = re.compile(pattern, re.I)
                    except re.error:
                        existing._regex = None
                elif match == "text":
                    existing._regex = re.compile(re.escape(pattern), re.I)
                return existing

        rule = AnswerRule(id=rule_id or self._unique_id(pattern), match=match,
                          pattern=pattern, answer=answer, note=note)
        if match == "regex":
            rule._regex = re.compile(pattern, re.I)
        elif match == "text":
            rule._regex = re.compile(re.escape(pattern), re.I)
        self.rules.append(rule)
        return rule

    def add_rule_safe(self, pattern: str, answer: Any, match: str = "text",
                      note: str = "", rule_id: str = "") -> Tuple[AnswerRule, bool]:
        """返回 (规则, 是否新建)。正则非法时抛 re.error 由调用方提示。"""
        before = len(self.rules)
        rule = self.add_rule(pattern, answer, match, note, rule_id)
        return rule, len(self.rules) > before

    def remove_rule(self, key: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.id != key and r.pattern != key]
        return len(self.rules) != before

    def remember(self, question: str, answer: Any, source: str = "") -> None:
        """把"题目原文 -> 答案"记下来，下次顺序变化/字段变化也能命中。"""
        if not question or answer in ("", None, []):
            return
        entry = self.remembered.get(question)
        if entry:
            entry.answer = answer
            entry.hits += 1
            entry.updated_at = _now()
            if source:
                entry.rule = source
        else:
            self.remembered[question] = Remembered(answer=answer, rule=source, hits=1, updated_at=_now())

    # -- 检索 -------------------------------------------------------------- #
    def resolve(self, question: str, field_name: str = "", options: Sequence[Any] = (),
                qtype: str = "", override: Any = None) -> Match:
        """为一道题检索答案。override 为任务级临时覆盖（按字段名给出）。"""
        text = (question or "").strip()
        m = Match(question=text, field=field_name)

        # 1) 任务覆盖
        if override not in (None, "", []):
            m.answer, m.source, m.confidence = override, "override", 100
            m.note = "任务自定义答案"
            return m

        # 2) 记住的答案（题目原文精确命中）
        entry = self.remembered.get(text)
        if entry and entry.answer not in ("", None, []):
            m.answer, m.source, m.confidence = entry.answer, "remembered", 95
            m.note = f"命中 {entry.hits} 次（来源 {entry.rule or '-'}）"
            return m

        # 3) 规则：text 精确 > text 子串 > regex > expr
        candidates: List[Tuple[int, AnswerRule]] = []
        for rule in self.rules:
            if not rule.matches(text):
                continue
            if rule.match == "text":
                score = 98 if rule.pattern.strip().lower() == text.lower() else 90
            elif rule.match == "regex":
                score = 80
            else:
                score = 70
            candidates.append((score, rule))
        if candidates:
            candidates.sort(key=lambda x: -x[0])
            score, rule = candidates[0]
            answer = rule.answer
            src = f"rule-{rule.match}" if rule.match != "expr" else "rule-expr"
            if rule.match == "expr":
                computed = eval_arithmetic(text)
                if computed is None:
                    m.answer, m.source, m.confidence = "", "unknown", 0
                    m.note = f"表达式规则 {rule.id} 无法计算此题"
                    return m
                answer = computed
                score = 99
            m.answer, m.source, m.confidence = answer, src, score
            m.note = rule.note or f"规则 {rule.id}"
            if isinstance(answer, str) and answer.startswith("未配置"):
                m.confidence = 40
                m.note = f"{m.note}（该条规则尚未配置真实答案）"
            return m

        # 4) 兜底：题目本身就是算术式（无需在库里登记）
        computed = eval_arithmetic(text)
        if computed is not None:
            m.answer, m.source, m.confidence = computed, "arithmetic", 90
            m.note = f"自动计算 {normalize_expr(text)}"
            return m

        m.answer, m.source, m.confidence = "", "unknown", 0
        m.note = "答案库中未找到匹配项"
        return m

    def resolve_many(self, questions: Iterable[Any],
                     overrides: Optional[Dict[str, Any]] = None) -> List[Match]:
        overrides = overrides or {}
        out: List[Match] = []
        for q in questions:
            label = getattr(q, "label", "") or (q.get("label") if isinstance(q, dict) else "")
            name = getattr(q, "name", "") or (q.get("name") if isinstance(q, dict) else "")
            options = getattr(q, "options", None)
            if options is None and isinstance(q, dict):
                options = q.get("options") or []
            qtype = getattr(q, "qtype", "") or (q.get("qtype") if isinstance(q, dict) else "")
            out.append(self.resolve(label, name, options or [], qtype,
                                    overrides.get(name) if name else None))
        return out


# --------------------------------------------------------------------------- #
# 默认规则
# --------------------------------------------------------------------------- #
def default_rules() -> List[AnswerRule]:
    """出厂规则：算术题自动计算 + 一题一规则的占位示例。

    ``answer`` 以 ``"未配置"`` 开头时会被视为"尚未配置"（confidence 40），
    这样可以直观看到答案库的用法，又不会把占位文本当成真答案悄悄提交。
    """
    raw = [
        {"id": "arith", "match": "expr", "pattern": "arithmetic", "answer": "",
         "note": "四则运算等算术题：自动计算题目算式"},
        {"id": "example-text", "match": "regex", "pattern": "请填写你的默认备注",
         "answer": "未配置：请在答案库中把本题的答案改成你要填的内容",
         "note": "示例：按题目文字匹配（把 answer 改成自己的答案即生效）"},
        {"id": "example-choice", "match": "regex", "pattern": "你更倾向哪一种方案",
         "answer": "方案一",
         "note": "示例：单选题直接写选项文字（也可写序号 1）"},
    ]
    return [AnswerRule.from_dict(r) for r in raw]


# --------------------------------------------------------------------------- #
# 未命中题目收集
# --------------------------------------------------------------------------- #
def record_pending(entries: Sequence[Dict[str, Any]], url: str = "",
                   path: Optional[Path] = None) -> int:
    """把未命中的题目写到 data/pending_questions.json，供人工补答案。

    按**题干**去重（同一道题反复运行只记一条）。
    """
    path = path or PENDING_FILE
    data: Dict[str, Any] = {"updated_at": _now(), "items": []}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass
    items = data.setdefault("items", [])
    known = {(it.get("question") or "").strip() for it in items}
    added = 0
    for e in entries:
        question = (e.get("question") or "").strip()
        if not question or question in known:
            continue
        items.append({
            "question": question,
            "field": e.get("field", ""),
            "qtype": e.get("qtype", ""),
            "options": e.get("options", []),
            "url": url or e.get("url", ""),
            "first_seen": _now(),
        })
        known.add(question)
        added += 1
    data["updated_at"] = _now()
    from .config import atomic_write_json

    atomic_write_json(path, data)
    return added


def load_pending(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    path = path or PENDING_FILE
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return list(json.load(fh).get("items", []))
    except (json.JSONDecodeError, OSError):
        return []


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
