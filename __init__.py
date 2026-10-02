# -*- coding: utf-8 -*-
"""生活频率 life-frequency 插件包。

Runner 既可能把插件目录当包加载（相对导入可用），也可能只把 plugin.py 当顶层
模块加载（此时目录需在 sys.path 上）。plugin.py 里的自建模块统一用双路径导入
兼容两种情形：

    try:
        from .life_sim import ...      # 包式加载（Runner 真机）
    except ImportError:
        from life_sim import ...       # 平铺兜底（脚本直跑 / 旧测试）

本文件的存在是为了让包式加载下的相对导入稳定可解析。
"""
