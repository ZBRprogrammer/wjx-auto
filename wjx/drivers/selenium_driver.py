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
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..models import RunResult
from ..paths import SHOT_DIR, ensure_dirs, find_bundled_driver, setup_environment
from .base import (
    FULL_PATTERNS,
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
        self.last_questions: List[Any] = []      # 最近一次提取到的题目
        self.last_matches: List[Any] = []        # 最近一次答案检索结果
        self.last_resolution: Optional[Any] = None

    # ------------------------------------------------------------------ #
    # 按"题目内容"自动检索答案库并填写（顺序变化也能应对）
    # ------------------------------------------------------------------ #
    def auto_fill(self, task: Any, bank: Any, prepared: Any = None,
                  record_pending: bool = True) -> FillOutcome:
        from ..answers import record_pending as _record_pending
        from ..questionnaire import extract_questions_browser

        driver = prepared
        own = False
        if driver is None:
            driver = self._new_browser()
            own = True
            try:
                driver.get(task.url)
                time.sleep(1.0)
            except Exception as exc:
                try:
                    driver.quit()
                finally:
                    pass
                return FillOutcome(result=RunResult.NAVIGATION_ERROR.value,
                                   url_before=task.url, message=f"无法打开网址: {exc}")
        try:
            questions = extract_questions_browser(driver)
            if not questions:
                return FillOutcome(
                    result=RunResult.VALIDATION_FAILED.value,
                    url_before=driver.current_url,
                    message="未能识别到任何题目（页面可能已结束、需登录或模板未适配）",
                )
            matches = bank.resolve_many(questions, overrides=getattr(task, "answers", {}) or {})
            self.last_questions, self.last_matches = questions, matches

            answers = {q.name: m.answer for q, m in zip(questions, matches) if m.ok and q.name}
            unresolved = [q for q, m in zip(questions, matches) if not m.ok]
            if record_pending and unresolved:
                _record_pending(
                    [{"question": q.label, "field": q.name, "qtype": q.qtype,
                      "options": q.options} for q in unresolved],
                    url=task.url,
                )
            outcome = self.submit(task, driver, answers=answers)
            outcome.question_count = len(questions)
            outcome.filled_count = len(answers)
            outcome.unfilled = [q.label for q in unresolved]
            if unresolved:
                outcome.message = (
                    f"{outcome.message}｜{len(unresolved)} 道题未在答案库中找到："
                    + "; ".join(q.label[:20] for q in unresolved[:4])
                )
            return outcome
        finally:
            if own:
                try:
                    driver.quit()
                except Exception:
                    pass

    # ------------------------------------------------------------------ #
    # Selenium 延迟导入：不跑真实问卷时完全不占用内存
    # ------------------------------------------------------------------ #
    @staticmethod
    def _import_webdriver():
        setup_environment()          # vendor/ 与 selenium-manager 缓存目录
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
        """启动浏览器；遇到"profile 被上次崩溃残留锁住"时自动清理并重试一次。"""
        last_error: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                return self._start_browser()
            except DriverUnavailable as exc:
                last_error = exc
                if attempt == 1 and _clean_profile_locks(self._profile_dir()):
                    log.warning("浏览器启动失败，已清理残留锁文件后重试：%s", str(exc)[:160])
                    time.sleep(1.5)
                    continue
                raise
        raise DriverUnavailable(str(last_error) if last_error else "浏览器启动失败")

    def _profile_dir(self) -> Path:
        from ..paths import ROOT

        browser = (getattr(self.settings, "browser", "edge") or "edge").lower()
        return ROOT / "run" / f"browser-{browser}"

    def _start_browser(self):
        setup_environment()
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
        opts.add_argument("--disable-extensions")
        opts.add_argument("--disable-background-networking")
        # 独立的浏览器数据目录：避免与用户正在使用的浏览器互相影响
        user_data = setup_environment().parent / "run" / f"browser-{browser}"
        user_data.mkdir(parents=True, exist_ok=True)
        opts.add_argument(f"--user-data-dir={user_data}")
        if browser == "chrome":
            opts.add_experimental_option("excludeSwitches", ["enable-logging"])

        driver_path = (getattr(self.settings, "driver_path", "") or "").strip()
        if not driver_path:
            driver_path = find_bundled_driver(browser)
        try:
            if driver_path and os.path.exists(driver_path):
                service = service_cls(executable_path=driver_path)
            elif driver_path:
                service = service_cls(driver_path)
            else:
                service = service_cls()
            driver = (webdriver.Edge(service=service, options=opts) if browser != "chrome"
                      else webdriver.Chrome(service=service, options=opts))
        except Exception as exc:
            raise DriverUnavailable(
                f"无法启动 {browser} 浏览器：{type(exc).__name__}: {str(exc)[:300]}\n"
                "排查建议：\n"
                "  1) 确认已安装 Edge/Chrome；\n"
                "  2) 把与本机浏览器版本匹配的驱动放到项目的 drivers\\ 目录"
                "（msedgedriver.exe / chromedriver.exe）；\n"
                "  3) 或在「设置 → 浏览器驱动路径」中指定驱动位置。"
            ) from exc
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

    @staticmethod
    def is_alive(handle: Any) -> bool:
        """预打开的浏览器是否仍然可用。

        实测：浏览器被回收/崩溃后句柄还在但 session 已失效，
        此时必须丢弃句柄重新启动，否则任务会以 driver_error 失败。
        """
        if handle is None:
            return False
        try:
            _ = handle.current_url
            _ = handle.title
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    def submit(self, task: Any, prepared: Any = None,
               answers: Optional[Dict[str, Any]] = None) -> FillOutcome:
        from selenium.common.exceptions import WebDriverException  # type: ignore
        from selenium.webdriver.common.by import By  # type: ignore

        use_answers: Dict[str, Any] = answers if answers is not None else dict(getattr(task, "answers", {}) or {})

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
            # 进入页面后先点掉"是否继续上次回答"之类的拦路弹窗，否则无法填题
            entry_dialog = _dismiss_dialogs(driver, By)
            if entry_dialog:
                time.sleep(0.5)
            spec = driver.execute_script(FORM_SPEC_JS, list(CONTAINER_SELECTORS))
            url_before = spec.get("url") or driver.current_url
            fields: List[Dict[str, Any]] = spec.get("fields", [])
            unfilled: List[str] = []
            filled = 0
            delay = max(0, int(getattr(self.settings, "fill_delay_ms", 120))) / 1000.0

            for field in fields:
                key = field.get("key")
                raw = use_answers.get(key)
                if raw is None or key is None:
                    continue
                ok, err = self._fill_field(driver, field, raw, By)
                # 有些弹窗会在填写过程中出现（如"是否继续上次回答"填写后才弹）
                if _dismiss_dialogs(driver, By, rounds=1):
                    time.sleep(0.4)
                # 回读实际值，确认填写是否真的生效
                actual = self._read_field_value(driver, field, By)
                if actual:
                    filled += 1
                else:
                    unfilled.append(f"{key}({err})" if err else key)
                if delay:
                    time.sleep(delay)

            # 提交（问卷星偶尔会返回"需要安全校验，请重新提交"，会重试几次）
            pre_submit_dialog = _dismiss_dialogs(driver, By)
            clicked = False
            dialog_text = pre_submit_dialog or ""
            captcha = False
            url_after = driver.current_url
            page_text = ""
            retries = max(0, int(getattr(task, "submit_retries",
                                         getattr(self.settings, "submit_retries", 3))))
            retry_delay = float(getattr(task, "submit_retry_delay",
                                        getattr(self.settings, "submit_retry_delay", 3.0)))
            wait_seconds = max(1, int(getattr(self.settings, "submit_wait_seconds", 6)))

            for attempt in range(retries + 1):
                clicked = self._click_submit(driver, By) or clicked
                deadline = time.time() + wait_seconds
                while time.time() < deadline:
                    time.sleep(0.3)
                    try:
                        _dismiss_dialogs(driver, By, rounds=1)
                        new_dialog = self._read_dialog(driver, By)
                        if new_dialog:
                            dialog_text = new_dialog
                        url_after = driver.current_url
                        if _url_changed(url_before, url_after):
                            break
                        if new_dialog and any(p in new_dialog for p in FULL_PATTERNS):
                            break
                    except WebDriverException:
                        break
                if _url_changed(url_before, url_after):
                    break
                captcha = _has_captcha(driver, By)
                if captcha:
                    break
                try:
                    page_text = driver.find_element(By.TAG_NAME, "body").text or ""
                except Exception:
                    page_text = ""
                transient = "安全校验" in page_text or "请重新提交" in page_text
                if not transient or attempt >= retries:
                    break
                log.info("提交被要求安全校验，%.1fs 后重试（第 %d/%d 次）",
                         retry_delay, attempt + 1, retries)
                time.sleep(retry_delay)

            try:
                page_text = driver.find_element(By.TAG_NAME, "body").text or ""
            except Exception:
                page_text = ""

            if captcha:
                dialog_text = dialog_text or "需要安全校验，请重新提交"
                result, message = RunResult.CAPTCHA.value, (
                    "问卷触发了安全校验（阿里云验证码）："
                    f"{_captcha_title(driver, By) or '请人工完成验证后重试'}。"
                    "本工具不会绕过验证码；可改用人机交互模式（设置里关闭无头模式）后手动完成一次验证。"
                )
            else:
                result, message = classify_post_submit(url_before, url_after, page_text, dialog_text)
            note = "" if clicked else "（未找到提交按钮，可能已提交过或页面结构异常）"
            if not clicked and not page_text.strip():
                note = "（未找到提交按钮，且页面无内容：问卷可能已被暂停/结束）"
            if unfilled:
                note += f"（{len(unfilled)} 个字段回读为空: {', '.join(unfilled[:4])}）"
            outcome = FillOutcome(
                result=result,
                url_before=url_before,
                url_after=url_after,
                question_count=len(fields),
                filled_count=filled,
                unfilled=unfilled,
                dialog_text=dialog_text,
                message=message + note,
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
    def _fill_field(self, driver, field: Dict[str, Any], raw: Any, By,
                    container: Any = None) -> tuple:
        """按**字段名**定位控件并填写（比按位置索引可靠：问卷星会重排/隐藏题目）。

        返回 (是否填写成功, 错误说明)。
        """
        ftype = (field.get("type") or "text").lower()
        options: List[Dict[str, Any]] = field.get("options") or []
        key = str(field.get("key") or "")
        if container is None:
            container = self._find_container(driver, field, By)
        if container is None:
            return False, "未找到题目容器"

        if ftype in ("radio", "checkbox") and key:
            picks = raw if isinstance(raw, (list, tuple)) else [raw]
            chosen = 0
            for want in picks:
                want_s = str(want).strip()
                if not want_s:
                    continue
                matched = _match_option(options, want_s)
                if matched is None:
                    return False, f"选项 '{want_s}' 未找到"
                pos = _option_position(options, matched)
                if not self._click_option_name(driver, By, ftype, key, pos):
                    return False, f"选项 '{want_s}' 点击失败"
                chosen += 1
            return chosen > 0, ("" if chosen else "未选择")

        if ftype == "select" and key:
            from selenium.webdriver.support.ui import Select  # type: ignore

            matched = _match_option(options, str(raw))
            if matched is None:
                return False, "下拉项未找到"
            els = driver.find_elements(By.CSS_SELECTOR, f'select[name="{key}"]')
            if not els:
                return False, "未找到下拉框"
            Select(els[0]).select_by_value(str(matched.get("value")))
            return True, ""

        # 文本类
        value = "" if raw is None else str(raw)
        els = []
        if key:
            els = driver.find_elements(By.CSS_SELECTOR, f'input[name="{key}"], textarea[name="{key}"]')
        if not els:
            els = self._fallback_inputs(container, By, ftype)
        if not els:
            return False, "未找到输入框"
        el = els[0]
        try:
            driver.execute_script(ANCHOR_JS, container, el)
            el.clear()
            el.send_keys(value)
            return True, ""
        except Exception as exc:
            return False, f"填写失败 {type(exc).__name__}"

    @staticmethod
    def _fallback_inputs(container: Any, By, ftype: str) -> List[Any]:
        try:
            if ftype == "textarea":
                return container.find_elements(By.TAG_NAME, "textarea")
            return container.find_elements(By.CSS_SELECTOR, "input[type=text], input:not([type])")
        except Exception:
            return []

    @staticmethod
    def _find_container(driver, field: Dict[str, Any], By) -> Any:
        """优先按控件 name 反查其所属题目容器；失败再退回位置索引。"""
        key = str(field.get("key") or "")
        if key:
            for sel in (f'input[name="{key}"]', f'textarea[name="{key}"]',
                        f'select[name="{key}"]'):
                try:
                    els = driver.find_elements(By.CSS_SELECTOR, sel)
                except Exception:
                    els = []
                for el in els:
                    try:
                        return el.find_element(
                            By.XPATH,
                            "ancestor::div[contains(@class,'field')][1] | ancestor::fieldset[1]",
                        )
                    except Exception:
                        continue
        try:
            return driver.find_elements(By.CSS_SELECTOR, ",".join(CONTAINER_SELECTORS))[
                int(field.get("index", 0))
            ]
        except Exception:
            return None

    def _click_option_name(self, driver, By, ftype: str, name: str, pos: int) -> bool:
        """按 name 找到第 pos 个选项并点击（兼容 display:none 的控件）。"""
        try:
            inputs = driver.find_elements(By.CSS_SELECTOR, f'input[type={ftype}][name="{name}"]')
        except Exception:
            return False
        if pos < 0 or pos >= len(inputs):
            return False
        return self._click_option_element(driver, inputs[pos], By, ftype)

    def _click_option(self, driver, container, By, ftype: str, pos: int) -> bool:
        """按容器内位置点击选项（保留给矩阵题等场景）。"""
        inputs = container.find_elements(By.CSS_SELECTOR, f'input[type={ftype}]')
        if pos < 0 or pos >= len(inputs):
            return False
        return self._click_option_element(driver, inputs[pos], By, ftype)

    def _click_option_element(self, driver, target, By, ftype: str) -> bool:
        """依次尝试多个可见点击目标，并回读选中状态确认。"""
        label = "radio" if ftype == "radio" else "checkbox"
        candidates = []
        for xpath in (
            "ancestor::label[1]",
            "following-sibling::a[1]",
            'ancestor::span[contains(@class,"wrapper")][1]',
            'ancestor::div[contains(@class,"ui-radio") or contains(@class,"ui-checkbox")][1]'
            '//div[contains(@class,"label")][1]',
            'following-sibling::div[contains(@class,"label")][1]',
        ):
            try:
                candidates.append(target.find_element(By.XPATH, xpath))
            except Exception:
                continue

        for cand in candidates:
            try:
                if not cand.is_displayed():
                    continue
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", cand)
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

    def _read_field_value(self, driver, field: Dict[str, Any], By) -> str:
        """回读字段当前值，用于确认填写真的生效（空值会被报告为未填）。"""
        ftype = (field.get("type") or "text").lower()
        key = str(field.get("key") or "")
        try:
            if ftype in ("radio", "checkbox") and key:
                els = driver.find_elements(By.CSS_SELECTOR, f'input[type={ftype}][name="{key}"]')
                return ",".join((e.get_attribute("value") or "") for e in els if e.is_selected())
            if key:
                els = driver.find_elements(By.CSS_SELECTOR, f'input[name="{key}"], textarea[name="{key}"]')
                if els:
                    return (els[0].get_attribute("value") or "").strip()
            container = self._find_container(driver, field, By)
            if container is None:
                return ""
            if ftype == "select":
                from selenium.webdriver.support.ui import Select  # type: ignore

                return Select(container.find_elements(By.TAG_NAME, "select")[0]).first_selected_option.text
            els = self._fallback_inputs(container, By, ftype)
            return (els[0].get_attribute("value") or "").strip() if els else ""
        except Exception:
            return ""

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


def _clean_profile_locks(profile: Path) -> bool:
    """删除 Chromium 用户目录里的残留单实例锁（浏览器崩溃后会导致再也起不来）。"""
    removed = False
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        for path in (profile / name, profile / "Default" / name):
            try:
                if path.exists() or path.is_symlink():
                    path.unlink()
                    removed = True
            except OSError:
                continue
    return removed


def _url_changed(before: str, after: str) -> bool:
    import re

    strip = lambda u: re.sub(r"#.*$", "", (u or "").strip().rstrip("/")).lower()
    return bool(after) and strip(after) != strip(before)


def _option_position(options: Sequence[Dict[str, Any]], matched: Dict[str, Any]) -> int:
    """给定选项对象，返回它在选项列表中的下标（用于按 name 找到第 N 个 input）。"""
    for i, o in enumerate(options):
        if o is matched:
            return i
        if str(o.get("value")) == str(matched.get("value")) and \
                str(o.get("label")) == str(matched.get("label")):
            return i
    return 0


# 问卷星常见"拦路"弹窗（点掉才能继续填）
# 注意："确认" 和 "确定" 都要有 —— 实测"是否继续上次回答"弹窗的按钮是【确认】，
# 漏掉它就会去点【取消】，于是每次进入页面都把恢复作答取消掉、提交按钮也就点不到。
DIALOG_ACCEPT_TEXTS: Sequence[str] = (
    "继续上次回答", "继续回答", "继续填写", "继续", "确认", "确定", "好的", "是", "同意",
)
DIALOG_REJECT_TEXTS: Sequence[str] = (
    "重新填写", "放弃", "取消", "否",
)

DISMISS_DIALOG_JS = r"""
const accept = arguments[0], reject = arguments[1];
const vis = el => {
  if (!el) return false;
  const s = window.getComputedStyle(el);
  if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0) return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
};
const textOf = el => (el.innerText || el.value || '').trim();

// 1) 先按常见弹窗容器找（layui/layer/ui-dialog 等）
let containers = Array.from(document.querySelectorAll(
  '.layui-layer, .layui-layer-content, .layer-content, .ui-dialog, .popup, ' +
  '#popup_container, #divTip, .modal, .jq-toast-single, [class*=dialog], [class*=popup]'))
  .filter(vis);

// 2) 容器没找到（各问卷模板 class 不同）时，退化为"按提示文字定位"：
//    找到含提示文字的最小可见元素，用它的祖先作为弹窗范围
if (!containers.length) {
  const hints = ['是否继续上次回答', '继续上次', '继续回答', '是否继续'];
  let node = null;
  for (const el of document.querySelectorAll('body *')) {
    if (!vis(el)) continue;
    const t = textOf(el);
    if (!t || t.length > 60) continue;
    if (hints.some(h => t.includes(h))) { node = el; break; }
  }
  if (node) {
    let box = node;
    for (let i = 0; i < 4 && box.parentElement; i++) {
      box = box.parentElement;
      const btns = Array.from(box.querySelectorAll('a,button,div,span,input'))
        .filter(e => vis(e) && /继续|确定|好的|重新|取消/.test(textOf(e)));
      if (btns.length) break;
    }
    containers = [box];
  }
}
const texts = containers.map(c => textOf(c)).filter(Boolean).join(' | ');
if (!containers.length) return {text: '', clicked: '', scan: 'none'};

// 3) 在整个文档里找可点按钮（不限容器），优先"继续"，其次"确定/好的"
const all = Array.from(document.querySelectorAll('a,button,div,span,input,li')).filter(vis);
const pick = (words) => {
  // 只取"最小"的可点元素：按钮文字通常就写在最内层元素里，
  // 取外层容器会把整段弹窗文字也算进来，容易误判。
  const hits = [];
  for (const el of all) {
    const t = textOf(el);
    if (!t || t.length > 14) continue;
    const w = words.find(word => t === word || t.includes(word));
    if (w) hits.push({el: el, text: t, rank: t === w ? 0 : 1, len: t.length});
  }
  hits.sort((a, b) => a.rank - b.rank || a.len - b.len);
  return hits[0] || null;
};
const picked = pick(accept);
let target = picked ? picked.el : null;
let clicked = '';
if (target) clicked = picked.text;
else { const r = pick(reject); if (r) { target = r.el; clicked = r.text; } }
if (target) {
  try { target.scrollIntoView({block: 'center'}); } catch (e) {}
  target.click();
}
return {text: texts.slice(0, 200), clicked: clicked};
"""


def _dismiss_dialogs(driver, By, prefer_accept: bool = True, rounds: int = 3) -> str:
    """点掉问卷星的"是否继续上次回答"等拦路弹窗，返回最后一次弹窗文本。"""
    last_text = ""
    for _ in range(rounds):
        try:
            info = driver.execute_script(DISMISS_DIALOG_JS,
                                         list(DIALOG_ACCEPT_TEXTS), list(DIALOG_REJECT_TEXTS))
        except Exception:
            return last_text
        if not info or not info.get("text"):
            return last_text
        last_text = info.get("text") or last_text
        if not info.get("clicked"):
            return last_text
        time.sleep(0.6)
    return last_text


# 阿里云验证码（问卷星的"安全校验"）出现标志：只检测、不破解
CAPTCHA_SELECTORS: Sequence[str] = (
    "#captchaWrap", "#captchaOut", "#captchabtn", "#aliyunCaptcha",
    ".nc-container", "#nc_1_wrapper", ".aliyun-captcha",
)


def _has_captcha(driver, By) -> bool:
    for sel in CAPTCHA_SELECTORS:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if el.is_displayed():
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _captcha_title(driver, By) -> str:
    try:
        for sel in ("#captchaTit", "#captchaWrap"):
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                text = (el.text or "").strip()
                if text:
                    return text[:120]
    except Exception:
        pass
    return ""


def _sleep(seconds: float) -> None:
    time.sleep(seconds)
