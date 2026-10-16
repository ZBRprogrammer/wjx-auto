"""生成文档用界面截图：python tools/gui_screenshot.py"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# 让 Tk 的坐标与屏幕物理像素一致，否则截图会错位（高 DPI 缩放）
try:
    import ctypes

    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from wjx import config as C  # noqa: E402
from wjx import logging_setup  # noqa: E402
from wjx.gui import MainWindow  # noqa: E402

cfg = C.load_config()
cfg.settings.tray_enabled = False
cfg.settings.popup_enabled = False   # 截图时不弹系统通知，避免遮挡
cfg.settings.sound_enabled = False
logging_setup.setup(to_console=False, gui=True)
app = MainWindow(cfg)


def shot():
    try:
        from PIL import ImageGrab

        app.lift()
        app.update()
        time.sleep(1.0)
        app.update()
        x, y = app.winfo_rootx(), app.winfo_rooty()
        w, h = app.winfo_width(), app.winfo_height()
        out = os.path.join(ROOT, "docs", "gui-main.png")
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(out)
        print("saved", out)
    finally:
        app.quit_app()


app.after(1500, shot)
app.mainloop()
