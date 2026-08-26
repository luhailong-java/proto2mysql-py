"""把 stdout/stderr 钉成 UTF-8。三个对拍工具共用。

为什么需要：Windows 上 stdout 一旦被**重定向**（CI、``| tee``、subprocess 捕获），
Python 用的是 locale 编码（zh-CN 是 cp936、en 是 cp1252），而对拍成功那行打的是
``✅``（U+2705）——直接 UnicodeEncodeError、退出码变 1。

也就是说这是**成功路径专属**的崩溃：逐字节一致被报成失败，报的还是跟 SQL
毫无关系的编码栈，排查方向从一开始就是错的。

``errors="replace"`` 再兜一层：终端字体渲染不了也只是显示成问号，
不该让门禁挂掉。
"""

from __future__ import annotations

import sys


def force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:  # 被换成非 TextIOWrapper 时（pytest capsys）没有这个方法
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # 已经分离/关闭的流
                pass
