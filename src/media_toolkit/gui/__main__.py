"""``python -m media_toolkit.gui`` 的入口。

用法：
    python -m media_toolkit.gui            # 独立桌面窗口
    python -m media_toolkit.gui --web      # 用系统浏览器打开
    python -m media_toolkit.gui --debug    # 桌面窗口 + DevTools
"""

import sys

from media_toolkit.gui import launch

if __name__ == "__main__":
    launch(debug="--debug" in sys.argv, web="--web" in sys.argv)
