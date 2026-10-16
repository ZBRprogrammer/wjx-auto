"""条件规则引擎 + 任务优先级决策。

条件语法（给 GUI 用户写的，尽量像自然语言）::

    status('问卷A') == 'success'
    result('问卷A') != 'full'
    attempts('问卷B') < 3
    enabled('问卷A') and not submitted('问卷A')
    submitted('A') or result('A') == 'validation'

可用函数
--------
status(t)    聚合状态: idle/waiting/running/success/failed/skipped/error
result(t)    最近一次业务结果: success/full/validation/unknown/blocked/...
attempts(t)  已执行次数（整数）
enabled(t)   任务是否启用（true/false）
submitted(t) 是否提交成功（true/false）
name(t)      任务名称（字符串，便于与 url 比较）
url(t)       任务网址
group(t)     任务分组

任务引用可以是 id、名称（不区分大小写）或名称子串。
比较运算: == != < <= > >= ；逻辑运算: and or not ；支持括号。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# 词法分析
# --------------------------------------------------------------------------- #
TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<num>-?\d+(?:\.\d+)?)
  | (?P<str>'[^']*'|"[^"]*")
  | (?P<op>==|!=|<=|>=|<|>)
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<comma>,)
  | (?P<ident>[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff\-\.]*)
    """,
    re.VERBOSE,
)

KEYWORDS = {"and", "or", "not", "true", "false"}


class RuleError(ValueError):
    """条件表达式语法错误。"""


@dataclass
class Token:
    kind: str
    value: Any
    pos: int


def tokenize(expr: str) -> List[Token]:
    tokens: List[Token] = []
    pos = 0
    while pos < len(expr):
        m = TOKEN_RE.match(expr, pos)
        if not m:
            raise RuleError(f"无法识别的字符: {expr[pos]!r} (位置 {pos})")
        pos = m.end()
        kind = m.lastgroup or ""
        text = m.group()
        if kind == "ws":
            continue
        if kind == "num":
            tokens.append(Token("num", float(text) if "." in text else int(text), m.start()))
        elif kind == "str":
            tokens.append(Token("str", text[1:-1], m.start()))
        elif kind == "ident":
            tokens.append(Token("ident", text, m.start()))
        else:
            tokens.append(Token(kind, text, m.start()))
    tokens.append(Token("eof", None, len(expr)))
    return tokens


# --------------------------------------------------------------------------- #
# 运行上下文
# --------------------------------------------------------------------------- #
class RuleContext:
    """把任务/执行状态暴露给条件表达式。"""

    def __init__(self, tasks: Sequence[Any]):
        self.tasks = list(tasks)

    # -- 解析任务引用 ------------------------------------------------------ #
    def resolve(self, key: Any) -> Any:
        if isinstance(key, (int, float)):
            key = str(int(key))
        key = str(key).strip()
        key_l = key.lower()
        for t in self.tasks:
            if t.id == key:
                return t
        for t in self.tasks:
            if t.name.strip().lower() == key_l:
                return t
        for t in self.tasks:
            if key_l and key_l in t.name.strip().lower():
                return t
        return None

    @staticmethod
    def _last(t) -> Optional[Any]:
        return getattr(t, "last_run", None)

    # -- 暴露给表达式的函数 ------------------------------------------------ #
    def fn_status(self, key: Any) -> str:
        t = self.resolve(key)
        return t.status if t else "missing"

    def fn_result(self, key: Any) -> str:
        t = self.resolve(key)
        rec = self._last(t) if t else None
        return rec.result if rec else "none"

    def fn_attempts(self, key: Any) -> int:
        t = self.resolve(key)
        return int(t.attempts) if t else 0

    def fn_enabled(self, key: Any) -> bool:
        t = self.resolve(key)
        return bool(t.enabled) if t else False

    def fn_submitted(self, key: Any) -> bool:
        return self.fn_status(key) == "success"

    def fn_name(self, key: Any) -> str:
        t = self.resolve(key)
        return t.name if t else ""

    def fn_url(self, key: Any) -> str:
        t = self.resolve(key)
        return (t.url if t else "") or ""

    def fn_group(self, key: Any) -> str:
        t = self.resolve(key)
        return (t.group if t else "") or ""

    FUNCTIONS: Dict[str, Tuple[Callable[..., Any], int]] = {
        "status": (fn_status, 1),
        "result": (fn_result, 1),
        "attempts": (fn_attempts, 1),
        "enabled": (fn_enabled, 1),
        "submitted": (fn_submitted, 1),
        "name": (fn_name, 1),
        "url": (fn_url, 1),
        "group": (fn_group, 1),
    }

    # 供帮助文本使用：函数 -> 说明
    HELP: Dict[str, str] = {
        "status": "状态: idle/waiting/running/success/failed/skipped/error",
        "result": "最近结果: success/full/validation/unknown/blocked",
        "attempts": "已执行次数（整数）",
        "enabled": "任务是否启用（true/false）",
        "submitted": "是否提交成功（true/false）",
        "name": "任务名称",
        "url": "任务网址",
        "group": "任务分组",
    }

    def call(self, fname: str, args: List[Any]) -> Any:
        entry = self.FUNCTIONS.get(fname)
        if entry is None:
            raise RuleError(f"未知函数: {fname}()")
        fn, argc = entry
        if len(args) != argc:
            raise RuleError(f"{fname}() 需要 {argc} 个参数，收到 {len(args)} 个")
        return fn(self, *args)


# --------------------------------------------------------------------------- #
# 递归下降语法分析 / 求值（边解析边求值，规则很短）
# --------------------------------------------------------------------------- #
class _Parser:
    def __init__(self, tokens: List[Token], context: RuleContext):
        self.tokens = tokens
        self.i = 0
        self.ctx = context

    # -- 基础工具 ---------------------------------------------------------- #
    @property
    def cur(self) -> Token:
        return self.tokens[self.i]

    def advance(self) -> Token:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def accept(self, kind: str, value: Any = None) -> Optional[Token]:
        tok = self.cur
        if tok.kind == kind and (value is None or str(tok.value).lower() == str(value).lower()):
            return self.advance()
        return None

    def expect(self, kind: str, what: str) -> Token:
        tok = self.accept(kind)
        if tok is None:
            raise RuleError(f"期望 {what}，实际是 {self.cur.value!r}")
        return tok

    def peek_ident_value(self) -> str:
        return str(self.cur.value).lower() if self.cur.kind == "ident" else ""

    # -- 语法 -------------------------------------------------------------- #
    def parse(self) -> Any:
        value = self.parse_or()
        if self.cur.kind != "eof":
            raise RuleError(f"多余的内容: {self.cur.value!r}")
        return value

    def parse_or(self) -> Any:
        left = self.parse_and()
        while self.peek_ident_value() == "or":
            self.advance()
            right = self.parse_and()
            left = bool(left) or bool(right)
        return left

    def parse_and(self) -> Any:
        left = self.parse_not()
        while self.peek_ident_value() == "and":
            self.advance()
            right = self.parse_not()
            left = bool(left) and bool(right)
        return left

    def parse_not(self) -> Any:
        if self.peek_ident_value() == "not":
            self.advance()
            return not bool(self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self) -> Any:
        left = self.parse_primary()
        tok = self.cur
        if tok.kind == "op":
            self.advance()
            right = self.parse_primary()
            return _compare(tok.value, left, right)
        if tok.kind == "ident" and str(tok.value).lower() in ("is", "in"):
            op = str(tok.value).lower()
            self.advance()
            right = self.parse_primary()
            if op == "is":
                return _compare("==", left, right)
            return str(left) in str(right)
        return left

    def parse_primary(self) -> Any:
        tok = self.cur
        if tok.kind == "lparen":
            self.advance()
            value = self.parse_or()
            self.expect("rparen", "')'")
            return value
        if tok.kind == "num":
            self.advance()
            return tok.value
        if tok.kind == "str":
            self.advance()
            return tok.value
        if tok.kind == "ident":
            word = str(tok.value)
            low = word.lower()
            if low in ("true", "false"):
                self.advance()
                return low == "true"
            # 函数调用
            if self.tokens[self.i + 1].kind == "lparen":
                self.advance()  # 函数名
                self.advance()  # '('
                args: List[Any] = []
                if self.cur.kind != "rparen":
                    args.append(self.parse_or())
                    while self.accept("comma"):
                        args.append(self.parse_or())
                self.expect("rparen", "')'")
                return self.ctx.call(low, args)
            self.advance()
            # 裸标识符：优先当作任务引用，取其状态；否则当字符串（如 success）
            if self.ctx.resolve(word) is not None:
                return self.ctx.fn_status(word)
            return word
        if tok.kind == "op":  # 一元负号已被 num 处理；其余为错误
            raise RuleError(f"意外的运算符 {tok.value!r}")
        raise RuleError(f"表达式不完整，意外结束" if tok.kind == "eof" else f"意外符号 {tok.value!r}")


def _compare(op: str, left: Any, right: Any) -> bool:
    if op in ("==", "!="):
        equal = _loose_eq(left, right)
        return equal if op == "==" else not equal
    # 数值优先，其次字符串
    try:
        lv, rv = float(left), float(right)
    except (TypeError, ValueError):
        lv, rv = str(left), str(right)
    if op == "<":
        return lv < rv
    if op == "<=":
        return lv <= rv
    if op == ">":
        return lv > rv
    if op == ">=":
        return lv >= rv
    raise RuleError(f"不支持的运算符 {op}")


def _loose_eq(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return _to_bool(left) == _to_bool(right)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return str(left).strip().lower() == str(right).strip().lower()


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def evaluate_rule(expr: str, tasks_or_context: Any) -> bool:
    """在任务列表（或 RuleContext）上求值。语法错误抛 RuleError。"""
    ctx = tasks_or_context if isinstance(tasks_or_context, RuleContext) else RuleContext(tasks_or_context)
    parser = _Parser(tokenize(expr), ctx)
    return bool(parser.parse())


def safe_evaluate_rule(expr: str, tasks_or_context: Any, default: bool = True) -> Tuple[bool, str]:
    """返回 (结果, 错误信息)。条件写错时按 default 处理，避免误伤任务。"""
    if not (expr or "").strip():
        return True, ""
    try:
        return evaluate_rule(expr, tasks_or_context), ""
    except RuleError as exc:
        return default, str(exc)
    except Exception as exc:  # pragma: no cover - 防御性
        return default, f"{type(exc).__name__}: {exc}"


def validate_rule(expr: str, tasks: Sequence[Any] = ()) -> str:
    """校验语法，返回空字符串表示通过。"""
    if not (expr or "").strip():
        return ""
    try:
        evaluate_rule(expr, tasks)
        return ""
    except RuleError as exc:
        return str(exc)
    except Exception as exc:  # pragma: no cover
        return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# 优先级 / 互斥决策
# --------------------------------------------------------------------------- #
@dataclass
class Decision:
    """对一组"同时可运行"任务的裁决结果。"""

    run: List[str]          # 应当启动的任务 id
    skip: List[str]         # 应当跳过的任务 id
    reasons: Dict[str, str]  # 任务 id -> 原因（给 GUI 展示）


def decide(tasks: Sequence[Any], context: Any, dry_run: bool = False) -> Decision:
    """在候选任务集合上应用启动条件与优先级。

    规则:
      1. ``start_rule`` 为假 -> 跳过（例：问卷A已提交 => 跳过问卷B）。
      2. 同 ``group`` 内所有通过条件的任务里，只选 ``priority`` 最高者启动，
         其余记为"待定"（不是跳过）：一旦首选失败，下一轮它们仍可运行。
    """
    run: List[str] = []
    skip: List[str] = []
    reasons: Dict[str, str] = {}

    passed: List[Any] = []
    for t in tasks:
        ok, err = safe_evaluate_rule(t.start_rule, context, default=True)
        if err:
            reasons[t.id] = f"条件语法错误({err})，已放行"
            passed.append(t)
        elif not ok:
            skip.append(t.id)
            reasons[t.id] = f"条件不满足: {t.start_rule.strip()}"
        else:
            passed.append(t)

    # 分组裁决
    groups: Dict[str, List[Any]] = {}
    for t in passed:
        if t.group.strip():
            groups.setdefault(t.group.strip(), []).append(t)

    for gname, members in groups.items():
        if len(members) < 2:
            continue
        ranked = sorted(members, key=lambda x: (-int(x.priority), x.name))
        winner = ranked[0]
        run.append(winner.id)
        reasons[winner.id] = f"组「{gname}」优先级最高({winner.priority})，本次启动"
        for loser in ranked[1:]:
            # 落选者保持"待定"，等下一轮（首选成功后其条件通常会使其跳过）
            reasons[loser.id] = (
                f"组「{gname}」优先级 {loser.priority} 低于「{winner.name}」({winner.priority})，本轮待定"
            )
        passed = [t for t in passed if t not in members]

    run.extend(t.id for t in passed)
    return Decision(run=run, skip=skip, reasons=reasons)


def describe_functions() -> str:
    """给 GUI/CLI 的帮助文本。"""
    lines = ["可用函数（参数可以是任务名称、名称片段或 id）:"]
    for name in RuleContext.FUNCTIONS:
        lines.append(f"  {name}(任务)   {RuleContext.HELP.get(name, '')}")
    lines += [
        "",
        "比较: ==  !=  <  <=  >  >=      逻辑: and  or  not  ( )",
        "",
        "示例:",
        "  status('问卷A') == 'success'                  # A 已提交",
        "  result('问卷B') == 'full'                     # B 上次遇到人数已满",
        "  not submitted('问卷A')                        # A 未提交成功",
        "  submitted('A') or attempts('B') >= 2",
    ]
    return "\n".join(lines)
