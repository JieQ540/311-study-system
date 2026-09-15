@echo off
chcp 65001 >nul
title 311 备考系统
echo ============================================
echo   311 教育学专业基础 · 模拟系统
echo ============================================
echo.
echo   正在启动服务，请稍候...
echo   启动后浏览器会自动打开 http://127.0.0.1:8765
echo.
echo   ★ 关闭这个黑窗口 = 关闭系统
echo.

cd /d "%~dp0app"

start "" http://127.0.0.1:8765
python -m uvicorn server:app --host 127.0.0.1 --port 8765

echo.
echo 服务已停止。按任意键关闭。
pause >nul
