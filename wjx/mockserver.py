"""本地模拟问卷服务：离线复现"问卷星"的答题页、提交跳转页与"人数过多"弹窗。

用途
----
1. demo 默认后端（mock）通过 HTTP 与该服务交互，跑通"填写→提交→判定"全流程；
2. 浏览器直接打开 ``http://127.0.0.1:8765/vm/A.aspx`` 也能看到一份可交互的问卷，
   手动填写即可看到 mock 记录到的答案与提交结果；
3. 用 ``?force=full`` / ``?force=fail`` / ``?force=ok`` 手工触发三种结局便于测试。

接口
----
``GET  /vm/<id>.aspx``                 HTML 问卷页
``GET  /vm/<id>.aspx?format=json``     表单结构（供驱动解析，等价于真实问卷的 DOM）
``POST /vm/<id>.aspx``                 提交答案；成功 -> 302 跳转 /vm/<id>_done.aspx
                                       人数已满 -> 200 + 弹窗"填写人数过多"
                                       必填缺失 -> 200 + 停留原页面并标红
``GET  /join/<id>_done.aspx``          成功页（含"提交成功"字样）
``GET  /quota/<id>``                   当前配额状态（调试用）
``GET  /stats``                        记录到的所有提交（调试/GUI 用）
``POST /reset``                        清空统计并重置人数
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_PORT = 8765

# --------------------------------------------------------------------------- #
# 问卷定义：A 为"稳成功"，B 为"可能人数已满"，用于演示条件与优先级
# --------------------------------------------------------------------------- #
SURVEYS: Dict[str, Dict[str, Any]] = {
    "A": {
        "title": "问卷A（演示：正常可提交）",
        "full_rate": 0.0,          # 人数已满的概率
        "capacity": 5000,
        "fields": [
            {
                "key": "q1",
                "label": "1. 你更倾向哪一种方案？",
                "type": "radio",
                "required": True,
                "options": [
                    {"value": "1", "label": "方案一"},
                    {"value": "2", "label": "方案二"},
                    {"value": "3", "label": "方案三"},
                ],
            },
            {
                "key": "q2",
                "label": "2. 请填写你的默认备注",
                "type": "text",
                "required": True,
                "options": [],
            },
        ],
    },
    "B": {
        "title": "问卷B（演示：可能提示填写人数过多）",
        "full_rate": 0.4,
        "capacity": 5000,
        "fields": [
            {
                "key": "q1",
                "label": "1. 你更倾向哪一种方案？",
                "type": "radio",
                "required": True,
                "options": [
                    {"value": "1", "label": "方案一"},
                    {"value": "2", "label": "方案二"},
                    {"value": "3", "label": "方案三"},
                ],
            },
            {
                "key": "q2",
                "label": "2. 请填写你的默认备注",
                "type": "text",
                "required": True,
                "options": [],
            },
        ],
    },
}

_STATE_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {"counts": {}, "records": [], "started": time.time()}


def _survey(sid: str) -> Optional[Dict[str, Any]]:
    return SURVEYS.get(sid.upper())


def _spec(sid: str, url: str) -> Dict[str, Any]:
    s = _survey(sid) or {}
    return {
        "id": sid.upper(),
        "title": s.get("title", f"问卷{sid}"),
        "url": url,
        "fields": s.get("fields", []),
        "prefix": "",
        "success_url_hint": f"/join/{sid.upper()}_done.aspx",
    }


# --------------------------------------------------------------------------- #
# HTML 渲染
# --------------------------------------------------------------------------- #
PAGE_CSS = """
body{font-family:"Microsoft YaHei",Arial,sans-serif;background:#f2f4f7;margin:0;padding:24px}
.wrap{max-width:760px;margin:0 auto;background:#fff;border-radius:10px;padding:24px 28px;
      box-shadow:0 2px 12px rgba(0,0,0,.08)}
h1{font-size:20px;margin:0 0 6px}
.sub{color:#888;font-size:13px;margin-bottom:18px}
.field{padding:14px 0;border-bottom:1px dashed #e5e7eb}
.field-label{font-size:14px;color:#333;margin-bottom:8px}
.opt{display:block;margin:6px 0;font-size:14px;color:#444}
input[type=text]{width:100%;padding:8px;border:1px solid #d0d5dd;border-radius:6px;font-size:14px}
button{margin-top:18px;background:#2b7de9;color:#fff;border:0;border-radius:6px;
       padding:10px 26px;font-size:15px;cursor:pointer}
.missing{border-color:#e53935!important}
#alert{display:block;background:#fff4f4;border:1px solid #f5c2c2;color:#c62828;
       padding:12px 14px;border-radius:8px;margin:14px 0;font-size:14px}
#alert.hidden{display:none}
.err{color:#c62828;font-size:13px;margin-top:12px}
.done{max-width:560px;margin:60px auto;background:#fff;border-radius:10px;
      padding:36px;text-align:center;box-shadow:0 2px 12px rgba(0,0,0,.08)}
.done h2{color:#1a7f37;margin:0 0 10px}
code{background:#f3f4f6;padding:2px 6px;border-radius:4px}
"""


def render_form(sid: str, action: str, errors: List[str] = (), prefill: Dict[str, str] = None,
                alert_text: str = "") -> str:
    prefill = prefill or {}
    rows = []
    for f in (_survey(sid) or {}).get("fields", []):
        key, ftype = f["key"], f["type"]
        current = prefill.get(key, "")
        opts = []
        if ftype == "radio":
            for o in f["options"]:
                checked = " checked" if current == str(o["value"]) else ""
                opts.append(
                    f'<label class="opt"><input type="radio" name="{key}" value="{o["value"]}"{checked}> '
                    f'{o["label"]}</label>'
                )
            control = "".join(opts)
        elif ftype == "checkbox":
            chosen = set(str(current).split(",")) if current else set()
            for o in f["options"]:
                checked = " checked" if str(o["value"]) in chosen else ""
                opts.append(
                    f'<label class="opt"><input type="checkbox" name="{key}" value="{o["value"]}"{checked}> '
                    f'{o["label"]}</label>'
                )
            control = "".join(opts)
        else:
            cls = ' class="missing"' if f["label"] in errors else ""
            control = f'<input type="text" name="{key}" value="{current}"{cls}>'
        rows.append(
            f'<div class="field"><div class="field-label">{f["label"]}'
            f'{" <span style=color:#e53935>*</span>" if f.get("required") else ""}</div>{control}</div>'
        )
    alert_html = (
        f'<div id="alert">{alert_text}</div>' if alert_text else '<div id="alert" class="hidden"></div>'
    )
    err_html = f'<div class="err">提交失败：{"；".join(errors)}</div>' if errors else ""
    title = (_survey(sid) or {}).get("title", sid)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title>
<style>{PAGE_CSS}</style></head>
<body><div class="wrap">
<h1>{title}</h1>
<div class="sub">模拟问卷 · 由 wjx-auto mockserver 提供 · 编号 {sid.upper()}</div>
{alert_html}
<form method="post" action="{action}">
{''.join(rows)}
<button type="submit" id="submit_button">提交</button>
{err_html}
</form>
<div class="sub" style="margin-top:20px">提示：在网址后加 <code>?force=full</code> /
<code>?force=fail</code> / <code>?force=ok</code> 可强制指定提交结果。</div>
</div></body></html>"""


def render_done(sid: str, answers: Dict[str, str]) -> str:
    items = "".join(f"<li>{k} = {v}</li>" for k, v in answers.items()) or "<li>（无）</li>"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>提交成功</title><style>{PAGE_CSS}</style></head>
<body><div class="done">
<h2>提交成功，感谢您的参与！</h2>
<p class="sub">问卷 {sid.upper()} 已记录你的答卷。</p>
<ul style="text-align:left;color:#555;font-size:14px">{items}</ul>
<p><a href="/vm/{sid.upper()}.aspx">再填一份</a></p>
</div></body></html>"""


def render_full(sid: str) -> str:
    """人数已满：停留原页面 + 弹窗提示（真实问卷星行为）。"""
    return render_form(
        sid,
        f"/vm/{sid.upper()}.aspx",
        alert_text="抱歉，该问卷填写人数过多，暂时无法提交，请稍后再试或选择其他问卷。",
    )


# --------------------------------------------------------------------------- #
# 请求处理
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "wjx-mock/0.1"

    # -- 工具 ------------------------------------------------------------ #
    def log_message(self, fmt: str, *args: Any) -> None:  # 安静运行
        return

    def _send(self, code: int, body: str = "", ctype: str = "text/html; charset=utf-8",
              headers: Dict[str, str] = None) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _query(self) -> Dict[str, str]:
        parsed = urllib.parse.urlparse(self.path)
        return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    # -- GET ------------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = self._query()

        if path == "/stats":
            with _STATE_LOCK:
                payload = json.dumps(_STATE, ensure_ascii=False)
            return self._send(200, payload, "application/json; charset=utf-8")
        if path.startswith("/quota/"):
            sid = path.rsplit("/", 1)[-1].upper()
            with _STATE_LOCK:
                count = _STATE["counts"].get(sid, 0)
            return self._send(200, json.dumps({"id": sid, "count": count}, ensure_ascii=False),
                              "application/json; charset=utf-8")
        if path in ("/", "/index.html"):
            links = "".join(
                f'<li><a href="/vm/{k}.aspx">{v["title"]}</a></li>' for k, v in SURVEYS.items()
            )
            return self._send(200, f"<h3>wjx-auto 模拟问卷</h3><ul>{links}</ul>")

        m = re.match(r"^/vm/([A-Za-z0-9_\-]+)\.aspx$", path)
        if m:
            return self._handle_get_form(m.group(1), query)

        m = re.match(r"^/join/([A-Za-z0-9_\-]+)_done\.aspx$", path)
        if m:
            return self._send(200, render_done(m.group(1), {}))

        return self._send(404, "<h3>404</h3>")

    def _handle_get_form(self, sid: str, query: Dict[str, str]) -> None:
        if _survey(sid) is None:
            return self._send(404, "<h3>问卷不存在</h3>")
        url = f"http://{self.headers.get('Host', '127.0.0.1')}{self.path}"
        if query.get("format") == "json":
            return self._send(200, json.dumps(_spec(sid, url), ensure_ascii=False),
                              "application/json; charset=utf-8")
        if query.get("prefill") == "1":
            answers = {"q1": "1", "q2": "通过 URL 自动填写的默认内容"}
            return self._submit(sid, answers, force=query.get("force", ""), source="url")
        return self._send(200, render_form(sid, f"/vm/{sid.upper()}.aspx"))

    # -- POST ------------------------------------------------------------ #
    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

        if parsed.path == "/reset":
            with _STATE_LOCK:
                _STATE["counts"].clear()
                _STATE["records"].clear()
            return self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8")

        m = re.match(r"^/vm/([A-Za-z0-9_\-]+)\.aspx$", parsed.path)
        if not m:
            return self._send(404, "<h3>404</h3>")
        sid = m.group(1)
        if _survey(sid) is None:
            return self._send(404, "<h3>问卷不存在</h3>")
        force = form.pop("__force__", "") or self._query().get("force", "")
        return self._submit(sid, form, force=force, source="post")

    # -- 提交逻辑 --------------------------------------------------------- #
    def _submit(self, sid: str, answers: Dict[str, str], force: str = "", source: str = "post") -> None:
        survey = _survey(sid) or {}
        sid_u = sid.upper()
        errors: List[str] = []
        for f in survey.get("fields", []):
            if f.get("required") and not (answers.get(f["key"]) or "").strip():
                errors.append(f["label"])

        full = False
        if force == "full":
            full = True
        elif force == "ok":
            full = False
        elif errors:
            full = False
        else:
            with _STATE_LOCK:
                count = _STATE["counts"].get(sid_u, 0)
            if count >= int(survey.get("capacity", 10 ** 9)):
                full = True
            else:
                full = random.random() < float(survey.get("full_rate", 0.0))

        if full:
            with _STATE_LOCK:
                _STATE["records"].append(
                    {"time": _now(), "id": sid_u, "result": "full", "answers": answers, "source": source}
                )
            return self._send(200, render_full(sid_u))

        if errors:
            with _STATE_LOCK:
                _STATE["records"].append(
                    {"time": _now(), "id": sid_u, "result": "validation", "answers": answers, "source": source}
                )
            return self._send(200, render_form(sid_u, f"/vm/{sid_u}.aspx", errors=errors, prefill=answers))

        with _STATE_LOCK:
            _STATE["counts"][sid_u] = _STATE["counts"].get(sid_u, 0) + 1
            _STATE["records"].append(
                {"time": _now(), "id": sid_u, "result": "success", "answers": answers, "source": source}
            )
        return self._send(302, "", headers={"Location": f"/join/{sid_u}_done.aspx"})


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# 启停
# --------------------------------------------------------------------------- #
def create_server(port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="wjx-auto 模拟问卷服务")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--full-rate", type=float, default=None, help="覆盖 B 卷的'人数已满'概率")
    args = parser.parse_args(argv)

    if args.full_rate is not None:
        SURVEYS["B"]["full_rate"] = max(0.0, min(1.0, args.full_rate))

    httpd = create_server(args.port, args.host)
    print(f"模拟问卷服务已启动: http://{args.host}:{args.port}/vm/A.aspx")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
