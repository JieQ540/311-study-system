@echo off
chcp 65001 >nul
title 311 备考系统
echo ============================================
echo   311 教育学专业基础 · 模拟系统
echo ============================================
echo.

rem 先判断再 cd：否则 cd 失败时 cmd 会先吐一句「系统找不到指定的路径」，很难看
if not exist "%~dp0app\server.py" (
    echo   [错误] 这个脚本要和 app 文件夹放在一起（没找到 app\server.py）。
    echo   请从仓库根目录双击「启动系统.bat」。
    echo.
    pause
    exit /b 1
)
cd /d "%~dp0app"

rem ---------- 1. 有没有 Python ----------
where python >nul 2>nul
if errorlevel 1 (
    echo   [错误] 没有找到 python。
    echo.
    echo   请先安装 Python 3.10 或更高版本： https://www.python.org/downloads/
    echo   安装时务必勾选 "Add python.exe to PATH"，然后重新双击本脚本。
    echo.
    echo   如果明明装过还是报这个错，通常是被 Windows 应用商店的占位程序挡住了：
    echo     设置 ^> 应用 ^> 高级应用设置 ^> 应用执行别名 ^> 关掉 python.exe / python3.exe
    echo.
    pause
    exit /b 1
)

rem ---------- 2. 依赖装了没（没装就自动装，省掉一次「看不懂的英文报错」） ----------
python -c "import fastapi, uvicorn" >nul 2>nul
if errorlevel 1 (
    echo   第一次运行，正在安装依赖（只需一次，大约 1 分钟）...
    echo.
    python -m pip install fastapi "uvicorn[standard]"
    if errorlevel 1 (
        echo.
        echo   [错误] 依赖安装失败。请手动执行下面这行，成功后再重新双击本脚本：
        echo       python -m pip install fastapi "uvicorn[standard]"
        echo.
        pause
        exit /b 1
    )
    echo.
    echo   依赖安装完成。
    echo.
)

rem ---------- 3. 已经在跑就直接开浏览器（双击两次不会互相抢端口） ----------
netstat -ano | findstr /r /c:"TCP.*:8765 .*LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo   服务已经在运行，直接打开浏览器。
    start "" http://127.0.0.1:8765
    exit /b 0
)

echo   正在启动服务，请稍候...
echo   启动后浏览器会自动打开 http://127.0.0.1:8765
echo.
echo   ★ 关闭这个黑窗口 = 关闭系统
echo.

rem 等服务真的起来了再开浏览器。旧版本是「先开浏览器、再起服务」，
rem 用户第一眼看到的往往是「无法访问此网站」。
start "" /min cmd /c "timeout /t 5 /nobreak >nul & start "" http://127.0.0.1:8765"

python -m uvicorn server:app --host 127.0.0.1 --port 8765

echo.
echo 服务已停止。按任意键关闭。
pause >nul
