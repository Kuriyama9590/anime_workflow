# -*- coding: utf-8 -*-
"""python -m pipeline 入口（转发 cli.main）。

缺本文件时 `python -m pipeline` 直接报
"No module named pipeline.__main__; pipeline is a package and cannot be
directly executed"（exit=1，已实测复现，docs §1/§9）——不可删。
"""
import sys

from pipeline.cli import main

if __name__ == "__main__":
    sys.exit(main())
