# -*- coding: utf-8 -*-
"""让纯模块可以被平铺导入（``import life_sim``）。

真机是包式加载（``from .life_sim import ...``），这里用平铺路径测同一个源码，
两条路径都必须在 plugin.py 的 try/except 里成立。
"""

import pathlib
import sys

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
