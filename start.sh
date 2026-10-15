#!/usr/bin/env bash
# vocal2midi 一键启动（macOS / Linux）
set -e
cd "$(dirname "$0")"

echo "=========================================="
echo "  vocal2midi - 一键启动"
echo "=========================================="

PY=""
for cand in python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys;sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,12) else 1)' 2>/dev/null; then
            PY="$cand"
            break
        fi
        [ -z "$PY" ] && PY="$cand"
    fi
done

if [ -z "$PY" ]; then
    echo "[错误] 未检测到 Python，请先安装 Python 3.10 ~ 3.12。"
    exit 1
fi
echo "使用解释器：$PY （$("$PY" --version 2>&1)）"

if [ ! -x ".venv/bin/python" ]; then
    echo "[1/3] 创建虚拟环境 .venv ..."
    "$PY" -m venv .venv
else
    echo "[1/3] 已存在虚拟环境 .venv，跳过创建"
fi

echo "[2/3] 安装依赖（首次约 2~3 GB，需要较长时间）..."
.venv/bin/python -m pip install --upgrade pip --quiet
# 国内用户可加上 -i https://mirrors.aliyun.com/pypi/simple/ 加速
.venv/bin/python -m pip install -r requirements.txt

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo
    echo "[警告] 未检测到 ffmpeg，处理 mp3 会失败。"
    echo "        macOS:  brew install ffmpeg"
    echo "        Ubuntu: sudo apt install ffmpeg"
    echo
fi

echo "[3/3] 启动网页界面...首次运行会自动下载模型权重（数百 MB）"
exec .venv/bin/python src/app.py
