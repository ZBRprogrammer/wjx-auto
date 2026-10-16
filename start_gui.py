"""双击即用：启动图形界面（等价于 python main.py gui）。"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["gui"]))
