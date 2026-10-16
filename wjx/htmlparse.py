"""内置轻量 HTML 解析（只用标准库），用于"不启动浏览器"地读取问卷结构。

为什么需要它
------------
打包成 exe 时不想把 beautifulsoup4 + lxml（约 30MB）塞进包里，而纯 HTTP 模式
（``probe --static``）仍然要能解析问卷星页面。这里用 ``html.parser.HTMLParser``
重建一棵最小 DOM 树，提供与调用处一致的接口（find_all / select / get_text / get）。

只实现项目真正用到的那部分 CSS：
    tag
    .class
    tag.class
    tag[attr=value]
    A B（后代）
    A, B（并列）
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


class Node:
    """极简 DOM 节点。"""

    __slots__ = ("tag", "attrs", "children", "parent", "text_parts")

    def __init__(self, tag: str, attrs: Optional[Dict[str, str]] = None,
                 parent: Optional["Node"] = None):
        self.tag = (tag or "").lower()
        self.attrs: Dict[str, str] = attrs or {}
        self.children: List["Node"] = []
        self.parent = parent
        self.text_parts: List[str] = []

    # -- 属性 ---------------------------------------------------------- #
    @property
    def name(self) -> str:
        """与 BeautifulSoup 兼容：节点标签名。"""
        return self.tag

    def get(self, name: str, default: Any = None) -> Any:
        return self.attrs.get(name, default)

    def __getitem__(self, name: str) -> str:
        return self.attrs[name]

    # -- 文本 ---------------------------------------------------------- #
    def get_text(self, separator: str = "", strip: bool = False) -> str:
        chunks: List[str] = []

        def walk(node: "Node") -> None:
            for part in node.text_parts:
                chunks.append(part)
            for child in node.children:
                walk(child)

        walk(self)
        text = separator.join(c for c in chunks if c) if separator else "".join(chunks)
        if strip:
            return re.sub(r"\s+", " ", text).strip()
        return text

    @property
    def text(self) -> str:
        return self.get_text()

    # -- 查找 ---------------------------------------------------------- #
    def _iter_descendants(self) -> Iterable["Node"]:
        stack = list(reversed(self.children))
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))

    def find_all(self, tags: Any) -> List["Node"]:
        wanted = {tags} if isinstance(tags, str) else set(tags)
        return [n for n in self._iter_descendants() if n.tag in wanted]

    def find(self, tags: Any) -> Optional["Node"]:
        found = self.find_all(tags)
        return found[0] if found else None

    def select(self, selector: str) -> List["Node"]:
        return [n for n in self._iter_descendants() if _matches_selector(n, selector)]

    def select_one(self, selector: str) -> Optional["Node"]:
        found = self.select(selector)
        return found[0] if found else None

    def find_parent(self, tag: str) -> Optional["Node"]:
        node = self.parent
        while node is not None:
            if node.tag == tag:
                return node
            node = node.parent
        return None

    def find_next_sibling(self) -> Optional["Node"]:
        """与 BeautifulSoup 兼容：同一父节点下的下一个兄弟。"""
        if self.parent is None:
            return None
        siblings = self.parent.children
        try:
            idx = siblings.index(self)
        except ValueError:
            return None
        return siblings[idx + 1] if idx + 1 < len(siblings) else None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Node {self.tag} class={self.attrs.get('class', '')!r}>"


# --------------------------------------------------------------------------- #
# 选择器
# --------------------------------------------------------------------------- #
def _split_selector(selector: str) -> List[str]:
    """按逗号切分并列选择器（忽略引号内逗号）。"""
    parts, buf, quote = [], [], ""
    for ch in selector:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _matches_simple(node: Node, part: str) -> bool:
    """判断单个复合选择器（不含空格/逗号）是否匹配节点。"""
    tag = re.match(r"^[a-zA-Z][\w-]*", part)
    if tag and node.tag != tag.group(0).lower():
        return False

    for cls in re.findall(r"\.([\w-]+)", part):
        classes = (node.attrs.get("class") or "").split()
        if cls not in classes:
            return False

    for attr, op, value in re.findall(r"\[([\w-]+)\s*([~^$*|]?=)\s*[\"']?([^\]\"']*)[\"']?\]", part):
        actual = node.attrs.get(attr)
        if actual is None:
            return False
        if op == "=" and actual != value:
            return False
        if op == "~=" and value not in actual.split():
            return False
        if op == "^=" and not actual.startswith(value):
            return False
        if op == "$=" and not actual.endswith(value):
            return False
        if op == "*=" and value not in actual:
            return False

    for pseudo in re.findall(r":([\w-]+)", part):
        if pseudo not in ("first-child", "last-child"):
            return False
    return True


def _matches_selector(node: Node, selector: str) -> bool:
    for one in _split_selector(selector):
        chain = one.split()
        if not chain or not _matches_simple(node, chain[-1]):
            continue
        # 其余部分按"祖先链"从右往左匹配（只支持后代关系）
        current = node.parent
        remaining = list(reversed(chain[:-1]))
        idx = 0
        while current is not None and idx < len(remaining):
            if _matches_simple(current, remaining[idx]):
                idx += 1
            current = current.parent
        if idx == len(remaining):
            return True
    return False


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("[document]")
        self.current = self.root

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.current)
        self.current.children.append(node)
        if tag.lower() not in VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.current)
        self.current.children.append(node)

    def handle_endtag(self, tag: str) -> None:
        node = self.current
        while node is not None and node.tag != tag.lower():
            node = node.parent
        if node is not None and node.parent is not None:
            self.current = node.parent

    def handle_data(self, data: str) -> None:
        if data and data.strip():
            self.current.text_parts.append(data)


def parse_html(html: str) -> Node:
    """解析 HTML，返回文档根节点。"""
    builder = _TreeBuilder()
    try:
        builder.feed(html or "")
        builder.close()
    except Exception:
        # HTMLParser 对畸形 HTML 很宽容，这里只是防御
        pass
    return builder.root


# --------------------------------------------------------------------------- #
# 便捷：直接拿"节点列表"
# --------------------------------------------------------------------------- #
class Soup:
    """薄包装，让调用处可以像用 BeautifulSoup 一样 ``soup.select(...)``。"""

    def __init__(self, html: str):
        self.root = parse_html(html)

    def select(self, selector: str) -> List[Node]:
        return self.root.select(selector)

    def select_one(self, selector: str) -> Optional[Node]:
        return self.root.select_one(selector)

    def find_all(self, tags: Any) -> List[Node]:
        return self.root.find_all(tags)

    def find(self, tags: Any) -> Optional[Node]:
        return self.root.find(tags)

    def get_text(self, separator: str = "", strip: bool = False) -> str:
        return self.root.get_text(separator, strip)
