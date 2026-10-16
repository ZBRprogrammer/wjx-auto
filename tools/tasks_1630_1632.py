"""按用户要求配置两个任务的定时与条件（开发用，不入库）。

需求：
  16:30 先填原问卷 https://v.wjx.cn/vm/mYv3i3D.aspx
  16:32 新问卷     https://v.wjx.cn/vm/QFhwmjf.aspx
  条件：原问卷填写不成功，才填新问卷
"""
import sys
from datetime import datetime

sys.path.insert(0, ".")
from wjx import config as C
from wjx.models import Task

cfg = C.load_config()
today = datetime.now().strftime("%Y-%m-%d")

# 原问卷（定时填写已开启 -> 16:30）
old = Task(
    name="原问卷",
    url="https://v.wjx.cn/vm/mYv3i3D.aspx",
    start_at=f"{today} 16:30:00",
    group="今日问卷",
    priority=10,
    start_rule="",                       # 无条件：先填它
    reminder_minutes=5,
    prepare_minutes=5,
    preopen=True,
    retry_on_full=1,
    retry_interval_seconds=20,
    submit_retries=2,
    submit_retry_delay=2.0,
)
old.id = "oldq"

# 新问卷（16:32 开放）—— 仅当原问卷没有提交成功时才填
new = Task(
    name="新问卷",
    url="https://v.wjx.cn/vm/QFhwmjf.aspx",
    start_at=f"{today} 16:32:00",
    group="今日问卷",
    priority=5,
    start_rule="not submitted('原问卷')",   # 原问卷不成功 -> 继续填新问卷
    reminder_minutes=5,
    prepare_minutes=5,
    preopen=True,
    retry_on_full=1,
    retry_interval_seconds=20,
    submit_retries=2,
    submit_retry_delay=2.0,
)
new.id = "newq"

cfg.tasks = [old, new]
cfg.settings.backend = "selenium"
cfg.settings.browser = "edge"
cfg.settings.headless = True
cfg.settings.auto_answer = True
cfg.settings.poll_seconds = 2
cfg.settings.fill_delay_ms = 150
cfg.settings.submit_wait_seconds = 8
cfg.settings.respect_min_interval = 3
cfg.settings.sound_enabled = True
cfg.settings.popup_enabled = True
C.save_config(cfg)

print("已写入任务：")
for t in cfg.tasks:
    print(f"  {t.name:6} {t.start_at}  组={t.group} 优先级={t.priority} "
          f"条件={t.start_rule or '(无)'} 提前打开={t.prepare_minutes}分钟")
print("\n设置：后端=%s 浏览器=%s 无头=%s 自动答题=%s 轮询=%ss"
      % (cfg.settings.backend, cfg.settings.browser, cfg.settings.headless,
         cfg.settings.auto_answer, cfg.settings.poll_seconds))
