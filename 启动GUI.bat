@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动 问卷星自动填写助手 ...
start "" pythonw start_gui.py
exit /b 0
