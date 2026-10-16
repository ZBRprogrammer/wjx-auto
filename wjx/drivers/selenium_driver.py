"""真实浏览器后端：用 Selenium 打开问卷星、按默认内容填写、点击提交并判定结果。

适配的问卷星页面结构（多模板兼容）
----------------------------------
题目容器（按顺序尝试）::

    div.field[data-role="fieldcontain"]
    div.field
    fieldset
    .qnr-question, .div_question

控件：容器内 ``input[type=radio|checkbox]``、``select``、``textarea``、``input[type=text]``。
注意：问卷星手机版模板里控件本身是 ``display:none``，可见的是 ``a.jqradio`` /
``div.label`` / ``label``，因此**不能按可见性过滤 input**，而要按点击目标依次尝试。

提交按钮::

    #submit_button   #ctlNext   .submitbtn   input[type=submit]   文本含"提交"

提交后判定：URL 变化 => 成功；页面出现 layer 弹窗且文本含"人数过多" => 失败。
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

from ..models import RunResult
from ..paths import SHOT_DIR, ensure_dirs
from .base import (
    DriverError,
    DriverUnavailable,
    FillDriver,
    FillOutcome,
    classify_post_submit,
    _match_option,
)

# 题目容器选择器（按优先级）
CONTAINER_SELECTORS: Sequence[str] = (
    'div.field[data-role="fieldcontain"]',
    "div.field",
    "fieldset",
    ".qnr-question",
    ".div_question",
)

# 提交按钮选择器
SUBMIT_SELECTORS: Sequence[str] = (
    "#submit_button",
    "#ctlNext",
    "#btnSubmit",
    "input#submit_button",
    "a#submit_button",
    ".submitbtn",
    "input[type=submit]",
    "button[type=submit]",
)

# 弹窗容器选择器（问卷星使用 layer / layui）
DIALOG_SELECTORS: Sequence[str] = (
    ".layui-layer-content",
    ".layui-layer",
    ".layer-content",
    "#popup_container",
    "#divTip",
    ".jq-toast-single",
    ".ui-dialog-content",
    "#alert",
)

ANCHOR_JS = """
const c = arguments[0];
const el = arguments[1];
if (!c || !el) return;
el.scrollIntoView({block:'center'});
"""
FORM_SPEC_JS = r"""
const sels = arguments[0];
let containers = [];
for (const s of sels) {
  const found = Array.from(document.querySelectorAll(s));
  if (found.length) { containers = found; break; }
}
const fields = containers.map((c, i) => {
  let label = '';
  const lab = c.querySelector('.field-label, .topichtml, .div_title_question, legend, .qtitle');
  if (lab) label = (lab.innerText || '').trim();
  if (!label) {
    const t = (c.innerText || '').split('\n')[0];
    label = (t || '').trim();
  }
  const radios = Array.from(c.querySelectorAll('input[type=radio]'));
  const checks = Array.from(c.querySelectorAll('input[type=checkbox]'));
  const selects = Array.from(c.querySelectorAll('select'));
  const textareas = Array.from(c.querySelectorAll('textarea'));
  const texts = Array.from(c.querySelectorAll('input[type=text], input:not([type])'));
  let type = 'text', inputs = [];
  if (radios.length) { type = 'radio'; inputs = radios; }
  else if (checks.length) { type = 'checkbox'; inputs = checks; }
  else if (selects.length) { type = 'select'; inputs = selects; }
  else if (textareas.length) { type = 'textarea'; inputs = textareas; }
  else if (texts.length) { type = 'text'; inputs = texts; }
  let options = [];
  if (type === 'select') {
    options = Array.from(selects[0].options).map(o => ({value: o.value, label: (o.text||'').trim()}));
  } else {
    options = inputs.map(inp => {
      let optLabel = '';
      const lb = inp.closest('label');
      if (lb) optLabel = (lb.innerText || '').trim();
      if (!optLabel) {
        const nx = inp.nextElementSibling;
        if (nx) optLabel = (nx.innerText || '').trim();
      }
      if (!optLabel && inp.parentElement) optLabel = (inp.parentElement.innerText || '').trim();
      return {value: inp.value, label: optLabel};
    });
  }
  const key = (inputs[0] && inputs[0].name) ? inputs[0].name : ('q' + (i + 1));
  return {index: i, key: key, label: label, type: type,
          options: options, required: c.innerText.includes('*')};
});
return {url: location.href, title: document.title, fields: fields,
        submit: (document.querySelector('#submit_button, #ctlNext, .submitbtn, input[type=submit]')||{}).outerHTML || ''};
"""


class SeleniumDriver(FillDriver):
    name = "selenium"

    def __init__(self, settings: Any):
        super().__init__(settings)
        self._webdriver = None

    # ------------------------------------------------------------------ #
    # Selenium 延迟导入：不跑真实问卷时完全不占用内存
    # ------------------------------------------------------------------ #
    @staticmethod
    def _import_webdriver():
        try:
            from selenium import webdriver  # type: ignore
            from selenium.webdriver.chrome.options import Options as ChromeOptions  # type: ignore
            from selenium.webdriver.chrome.service import Service as ChromeService  # type: ignore
            from selenium.webdriver.edge.options import Options as EdgeOptions  # type: ignore
            from selenium.webdriver.edge.service import Service as EdgeService  # type: ignore
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise DriverUnavailable(
                "未安装 selenium，无法使用真实浏览器后端。\n"
                "安装: pip install --target vendor selenium\n"
                "或改用模拟后端(mock)先验证流程。"
            ) from exc
        return webdriver, ChromeOptions, ChromeService, EdgeOptions, EdgeService

    def _new_browser(self):
        webdriver, ChromeOptions, ChromeService, EdgeOptions, EdgeService = self._import_webdriver()
        browser = (getattr(self.settings, "browser", "edge") or "edge").lower()
        headless = bool(getattr(self.settings, "headless", True))
        if browser == "chrome":
            opts, service_cls = ChromeOptions(), ChromeService
        else:
            opts, service_cls = EdgeOptions(), EdgeService
        if headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1280,900")
        opts.add_argument("--log-level=3")
        opts.add_experimental_option("excludeSwitches", ["enable-logging"]) if browser == "chrome" else None

        driver_path = (getattr(self.settings, "driver_path", "") or "").strip()
        if driver_path:
            service = service_cls(executable_path=driver_path) if os.path.exists(driver_path) else service_cls(driver_path)
        else:
            service = service_cls()
        driver = webdriver.Edge(service=service, options=opts) if browser != "chrome" else webdriver.Chrome(service=service, options=opts)
        driver.set_page_load_timeout(int(getattr(self.settings, "page_load_timeout", 30)))
        return driver

    # ------------------------------------------------------------------ #
    def prepare(self, task: Any) -> Optional[Any]:
        """提前打开浏览器并加载页面，让人工还有时间干预。"""
        try:
            driver = self._new_browser()
            driver.get(task.url)
            _sleep(1.0)
            return driver
        except DriverUnavailable:
            raise
        except Exception as exc:
            raise DriverError(f"提前打开页面失败: {exc}") from exc

    def close_prepared(self, handle: Any) -> None:
        try:
            if handle is not None:
                handle.quit()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    def submit(self, task: Any, prepared: Any = None) -> FillOutcome:
        from selenium.common.exceptions import WebDriverException  # type: ignore
        from selenium.webdriver.common.by import By  # type: ignore

        driver = prepared
        own_driver = False
        if driver is None:
            driver = self._new_browser()
            own_driver = True
            try:
                driver.get(task.url)
            except WebDriverException as exc:
                driver.quit()
                return FillOutcome(result=RunResult.NAVIGATION_ERROR.value,
                                   url_before=task.url, message=f"无法打开网址: {exc}")
        try:
            spec = driver.execute_script(FORM_SPEC_JS, list(CONTAINER_SELECTORS))
            url_before = spec.get("url") or driver.current_url
            fields: List[Dict[str, Any]] = spec.get("fields", [])
            unfilled: List[str] = []
            filled = 0
            delay = max(0, int(getattr(self.settings, "fill_delay_ms", 120))) / 1000.0

            for field in fields:
                key = field.get("key")
                raw = (task.answers or {}).get(key)
                if raw is None or key is None:
                    continue
                ok, err = self._fill_field(driver, field, raw, By)
                if ok:
                    filled += 1
                else:
                    unfilled.append(f"{key}({err})")
                if delay:
                    time.sleep(delay)

            # 提交
            clicked = self._click_submit(driver, By)
            wait_seconds = max(1, int(getattr(self.settings, "submit_wait_seconds", 6)))
            dialog_text = ""
            url_after = driver.current_url
            page_text = ""
            deadline = time.time() + wait_seconds
            while time.time() < deadline:
                time.sleep(0.3)
                try:
                    dialog_text = self._read_dialog(driver, By) or dialog_text
                    url_after = driver.current_url
                    if dialog_text:
                        break
                    if _url_changed(url_before, url_after):
                        break
                except WebDriverException:
                    break
            try:
                page_text = driver.find_element(By.TAG_NAME, "body").text or ""
            except Exception:
                page_text = ""

            result, message = classify_post_submit(url_before, url_after, page_text, dialog_text)
            outcome = FillOutcome(
                result=result,
                url_before=url_before,
                url_after=url_after,
                question_count=len(fields),
                filled_count=filled,
                unfilled=unfilled,
                dialog_text=dialog_text,
                message=message + ("" if clicked else "（未找到提交按钮，可能已提交过或页面结构异常）"),
            )
            if not outcome.ok:
                outcome.message = f"{outcome.message}｜截图: {self._screenshot(driver, task)}"
            return outcome
        finally:
            if own_driver:
                try:
                    driver.quit()
                except Exception:
                    pass

    # ------------------------------------------------------------------ #
    def _fill_field(self, driver, field: Dict[str, Any], raw: Any, By) -> tuple:
        ftype = (field.get("type") or "text").lower()
        options: List[Dict[str, Any]] = field.get("options") or []
        index = int(field.get("index", 0))
        container = driver.find_elements(By.CSS_SELECTOR, ",".join(CONTAINER_SELECTORS))[index]

        if ftype in ("radio", "checkbox"):
            picks = raw if isinstance(raw, (list, tuple)) else [raw]
            chosen = 0
            for want in picks:
                want_s = str(want).strip()
                if not want_s:
                    continue
                if ftype == "radio":
                    # 问卷星单选题点击后会重排选项，必须每次重新取容器
                    container = driver.find_elements(By.CSS_SELECTOR, ",".join(CONTAINER_SELECTORS))[index]
                matched = _match_option(options, want_s)
                if matched is None:
                    return False, f"选项 '{want_s}' 未找到"
                pos = _option_position(options, matched)
                if not self._click_option(driver, container, By, ftype, pos):
                    return False, f"选项 '{want_s}' 点击失败"
                chosen += 1
            return chosen > 0, "未选择" if chosen == 0 else ""

        if ftype == "select":
            from selenium.webdriver.support.ui import Select  # type: ignore

            matched = _match_option(options, str(raw))
            if matched is None:
                return False, "下拉项未找到"
            Select(container.find_elements(By.TAG_NAME, "select")[0]).select_by_value(str(matched.get("value")))
            return True, ""

        # 文本类
        value = "" if raw is None else str(raw)
        try:
            if ftype == "textarea":
                el = container.find_elements(By.TAG_NAME, "textarea")[0]
            else:
                els = container.find_elements(By.CSS_SELECTOR, "input[type=text], input:not([type])")
                el = els[0]
            driver.execute_script(ANCHOR_JS, container, el)
            el.clear()
            el.send_keys(value)
            return True, ""
        except Exception as exc:
            return False, f"填写失败 {type(exc).__name__}"

    def _click_option(self, driver, container, By, ftype: str, pos: int) -> bool:
        """依次尝试多个可见点击目标，并回读选中状态确认。"""
        inputs = container.find_elements(By.CSS_SELECTOR, f'input[type={ftype}]')
        if pos < 0 or pos >= len(inputs):
            return False
        target = inputs[pos]
        label = "radio" if ftype == "radio" else "checkbox"
        candidates = []
        try:
            candidates.append(target.find_element(By.XPATH, "ancestor::label[1]"))
        except Exception:
            pass
        try:
            candidates.append(target.find_element(By.XPATH, "following-sibling::a[1]"))
        except Exception:
            pass
        try:
            candidates.append(
                target.find_element(
                    By.XPATH,
                    'ancestor::span[contains(@class,"wrapper")][1]',
                )
            )
        except Exception:
            pass
        try:
            candidates.append(
                target.find_element(
                    By.XPATH,
                    'ancestor::div[contains(@class,"ui-radio") or contains(@class,"ui-checkbox")][1]'
                    '//div[contains(@class,"label")][1]',
                )
            )
        except Exception:
            pass
        try:
            candidates.append(target.find_element(By.XPATH, 'following-sibling::div[contains(@class,"label")][1]'))
        except Exception:
            pass

        for cand in candidates:
            try:
                if not cand.is_displayed():
                    continue
                driver.execute_script(ANCHOR_JS, container, cand)
                cand.click()
                if target.is_selected():
                    return True
            except Exception:
                continue
        try:  # 兜底：强制勾选
            driver.execute_script("arguments[0].click();", target)
            return bool(target.is_selected())
        except Exception:
            return False

    def _click_submit(self, driver, By) -> bool:
        from selenium.webdriver.common.keys import Keys  # type: ignore

        # 先滚动到底部，问卷星的提交按钮常在底部
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.4)
        except Exception:
            pass
        for sel in SUBMIT_SELECTORS:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        if el.is_displayed():
                            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                            el.click()
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        # 文本兜底
        try:
            for el in driver.find_elements(By.XPATH, "//*[self::a or self::button or self::div][contains(text(),'提交')]"):
                try:
                    if el.is_displayed() and (el.text or "").strip() in ("提交", "提交答卷", "提交问卷"):
                        el.click()
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _read_dialog(self, driver, By) -> str:
        for sel in DIALOG_SELECTORS:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        if el.is_displayed():
                            text = (el.text or "").strip()
                            if text:
                                return text
                    except Exception:
                        continue
            except Exception:
                continue
        return ""

    def _screenshot(self, driver, task: Any) -> str:
        ensure_dirs()
        safe = "".join(ch for ch in task.name if ch.isalnum() or ch in "-_") or task.id
        path = SHOT_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{safe}.png"
        try:
            driver.save_screenshot(str(path))
            return str(path)
        except Exception:
            return ""


def _url_changed(before: str, after: str) -> bool:
    import re

    strip = lambda u: re.sub(r"#.*$", "", (u or "").strip().rstrip("/")).lower()
    return bool(after) and strip(after) != strip(before)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)
