# -*- coding: utf-8 -*-
"""核心模块：运行轨迹（trace）、编排（orchestrator）、验证（verifier）。

这些模块刻意不依赖 flask 与 langchain，只依赖标准库或 pandas/基础库，
以便 web 层、agent 层、调度器都可以安全导入，也便于单独测试。
"""
