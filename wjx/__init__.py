"""wjx-auto: 问卷星定时自动填写助手（demo）。

模块划分:
    models    任务/状态数据模型
    config    配置持久化
    rules     条件规则引擎与优先级决策
    drivers   填写后端（mock 演示 / 问卷星 selenium）
    scheduler 调度器（提前提醒 + 到点执行）
    notify    弹窗 / 提示音
    gui       tkinter 图形界面
    mockserver 本地模拟问卷（离线 demo 用）
    instance   GUI 单实例控制（多开保护：只允许一个主窗口）
"""

__version__ = "0.8.1"
APP_NAME = "wjx-auto"
