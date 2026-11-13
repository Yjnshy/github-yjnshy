"""``python -m dly.gui`` 的入口。

用法：
    python -m dly.gui            # 独立桌面窗口
    python -m dly.gui --web      # 用系统浏览器打开
    python -m dly.gui --debug    # 桌面窗口 + DevTools
"""

import sys

from dly.gui import launch

if __name__ == "__main__":
    launch(debug="--debug" in sys.argv, web="--web" in sys.argv)
