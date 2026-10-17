@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
chcp 65001 >nul 2>nul

set "MIRROR=https://mirrors.aliyun.com/pypi/simple/"

echo.
echo ==========================================
echo   vocal2midi - 一键启动
echo ==========================================
echo.

REM ---------- 0) 项目内已有可用的 .venv 就直接用，不依赖系统 Python ----------
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import demucs, swift_f0, gradio" >nul 2>nul
    if not errorlevel 1 (
        echo [OK] 使用项目内的 .venv，无需系统 Python。
        set "VPY=.venv\Scripts\python.exe"
        if /i "%~1"=="--check" goto :done
        goto :check_ffmpeg
    )
)

REM ---------- 1) 挑选 Python 解释器（优先官方 3.10~3.12）----------
set "PYCMD="
for %%V in (3.12 3.11 3.10) do (
    if not defined PYCMD (
        py -%%V --version >nul 2>nul
        if not errorlevel 1 set "PYCMD=py -%%V"
    )
)
if not defined PYCMD (
    python --version >nul 2>nul
    if not errorlevel 1 set "PYCMD=python"
)
if not defined PYCMD goto :no_python

%PYCMD% -c "import sys;print('  解释器：'+sys.executable);print('  版本：'+sys.version.split()[0])"

REM 0=范围外但可用 2=版本过低 3=MSYS2/Cygwin 自带 Python（需确认）
%PYCMD% -c "import sys;p=sys.executable.lower();sys.exit(3 if ('msys' in p or 'cygwin' in p or 'msys64' in p) else (0 if (3,10)<=sys.version_info[:2] else 2))" 2>nul
set "RC=%errorlevel%"
if "%RC%"=="0" goto :version_check
if "%RC%"=="2" goto :bad_version
if "%RC%"=="3" goto :warn_msys
goto :no_python

:warn_msys
echo.
echo [警告] 检测到的是 MSYS2 / Cygwin 自带的 Python。
echo        它不是官方 Python，安装 torch 时可能因 DLL 冲突失败。
echo        若安装失败，请改用官方 Python： py install 3.12
echo.
set "ANS="
set /p "ANS=仍要使用它继续吗？(Y/n) "
if /i "%ANS%"=="n" goto :fail

:version_check
set "PYMINOR=0"
for /f %%i in ('%PYCMD% -c "import sys;print(sys.version_info[1])"') do set "PYMINOR=%%i"
if not defined PYMINOR set "PYMINOR=0"
if %PYMINOR% GEQ 13 (
    echo.
    echo [提示] 检测到较新的 Python，依赖将全部从预编译轮子安装。
    echo        注意：PyTorch 对新版本 Python 的预编译包可能尚未发布，
    echo        若 demucs 安装失败，请改装官方 Python 3.12： py install 3.12
)

:py_ok
echo.
echo [OK] 解释器检查通过。
if /i "%~1"=="--check" goto :done

:no_python
echo.
echo [错误] 没有找到可用的 Python。
echo        安装官方 Python 3.12： py install 3.12
echo        或从 https://www.python.org/downloads/ 下载（安装时勾选 Add to PATH）。
goto :fail

:bad_version
echo.
echo [错误] Python 版本过低，需要 3.10 及以上。
echo.
echo        推荐： py install 3.12
goto :fail

:py_ok
echo.
echo [OK] 解释器检查通过。
if /i "%~1"=="--check" goto :done

REM ---------- 2) 虚拟环境 ----------
if exist ".venv\Scripts\python.exe" goto :have_venv
echo.
echo [1/3] 创建虚拟环境 .venv ...
%PYCMD% -m venv .venv
if not exist ".venv\Scripts\python.exe" goto :venv_failed

:have_venv
set "VPY=.venv\Scripts\python.exe"
"%VPY%" --version >nul 2>nul
if errorlevel 1 goto :venv_failed

REM 依赖已就绪则跳过安装（重复启动不用再等）
"%VPY%" -c "import demucs, swift_f0, gradio" >nul 2>nul
if not errorlevel 1 (
    echo.
    echo [2/3] 依赖已就绪，跳过安装。
    goto :check_ffmpeg
)

REM ---------- 3) 安装依赖 ----------
echo.
echo [2/3] 安装依赖（首次约 2~3 GB，耗时较长，请勿关闭窗口）...

"%VPY%" -m pip install --upgrade pip --quiet
"%VPY%" -m pip install -r requirements.txt -i %MIRROR%
if errorlevel 1 (
    echo [提示] 镜像源安装失败，改用官方源重试...
    "%VPY%" -m pip install -r requirements.txt
)

:check_result
"%VPY%" -c "import demucs, swift_f0, gradio" 2>nul
if errorlevel 1 goto :deps_failed

:check_ffmpeg
REM ---------- 4) ffmpeg ----------
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo.
    echo [警告] 未检测到 ffmpeg，处理 mp3 会失败。
    echo        安装命令：winget install Gyan.FFmpeg
    echo        装完后重新运行本脚本。
    echo.
    pause
)

REM ---------- 5) 启动界面 ----------
echo.
echo [3/3] 启动界面...首次转换会自动下载模型权重（数百 MB）
echo        浏览器将自动打开 http://127.0.0.1:7860
echo        关闭页面后程序会在 60 秒后自动退出；也可直接关闭本窗口。
echo.
"%VPY%" src\app.py
echo.
echo 程序已退出。
goto :done

:venv_failed
echo.
echo [错误] 创建虚拟环境失败，请把本窗口内容或 start.log 发给开发者。
goto :fail

:deps_failed
echo.
echo [错误] 依赖安装不完整，请检查网络后重新运行本脚本。
goto :fail

:fail
echo.
pause
exit /b 1

:done
endlocal
