"""``python -m dly.gui`` 的入口。

用法：
    python -m dly.gui            # 独立桌面窗口
    python -m dly.gui --web      # 用系统浏览器打开
    python -m dly.gui --debug    # 桌面窗口 + DevTools
    python -m dly.gui --login bilibili   # 只开登录窗（主程序会这样调起自己）
"""

import sys

from dly.gui import launch

if __name__ == "__main__":
    if "--login" in sys.argv:
        from dly.gui import login_window

        raise SystemExit(login_window.main())
    launch(debug="--debug" in sys.argv, web="--web" in sys.argv)
