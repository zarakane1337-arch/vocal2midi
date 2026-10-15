# vocal2midi

把一首完整歌曲中的人声旋律提取为 MIDI 文件。

输入一段含伴奏的音频，程序会先分离出人声轨，再将人声转写为 MIDI 音符序列，输出可直接导入 DAW（Ableton Live、Logic Pro、FL Studio 等）的 `.mid` 文件及音符明细表。

## 技术原理

```
输入歌曲 (mp3/wav/flac)
      │
      ▼
┌──────────────────┐
│  Demucs 分离引擎  │   模型：htdemucs_ft（Meta AI）
│  人声 / 伴奏分离   │   输出：纯人声轨 vocals.wav
└──────────────────┘
      │
      ▼
┌──────────────────┐
│  basic-pitch     │   模型：ICASSP 2022（Spotify）
│  音频转写         │   输出：MIDI 音符事件（含弯音）
└──────────────────┘
      │
      ▼
.midi 文件 + 音符 CSV 明细
```

| 组件 | 角色 |
|---|---|
| [Demucs](https://github.com/adefossez/demucs) (htdemucs_ft) | 源分离：从混音中剥离人声 |
| [basic-pitch](https://github.com/spotify/basic-pitch) | 音符转写：人声 → MIDI（含 pitch bend） |
| [Gradio](https://gradio.app) | 网页界面 |

## 项目结构

```
vocal2midi/
├── src/
│   ├── app.py            # Gradio 网页 UI 入口
│   └── main.py           # 命令行入口 + 核心管线函数
├── start.bat             # Windows 一键启动（自动建虚拟环境、装依赖、开界面）
├── start.sh              # macOS / Linux 一键启动
├── requirements.txt      # Python 依赖
├── tools/
│   └── build_release.py  # 打包分发包
├── README.md
└── out/                  # 运行时生成：人声轨、MIDI、CSV
```

`src/app.py` 直接复用 `src/main.py` 中的管线函数，两条入口共享同一套逻辑。
输出目录固定为项目根目录的 `out/`，与启动时的工作目录无关。

## 环境要求

- **Python 3.10 – 3.12**（3.13 及以上部分依赖无预编译包，详见下方“已知问题”）
- **ffmpeg**：须安装并加入 PATH（音频解码、Demucs 读非 wav 格式时依赖）
- 可选：NVIDIA GPU + CUDA 版 PyTorch（可将 Demucs 分离从数分钟缩短到数十秒）

## 安装

### 1. 安装 ffmpeg

```bash
winget install Gyan.FFmpeg        # Windows
brew install ffmpeg               # macOS
sudo apt install ffmpeg           # Debian / Ubuntu
```

安装后**新开一个终端**，确认 `ffmpeg -version` 能正常输出。

### 2. 安装 Python 依赖

```bash
python -m venv .venv

.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux

pip install -r requirements.txt
```

> **GPU 用户**：请先按 [pytorch.org](https://pytorch.org) 官方指引安装与显卡匹配的 CUDA 版 torch，再执行 `pip install -r requirements.txt`，避免被依赖解析器覆盖回 CPU 版。

首次运行时 Demucs 与 basic-pitch 会自动下载模型权重（约数百 MB），需保持联网。

## 快速开始

### 网页界面（推荐）

```bash
python src/app.py
```

浏览器自动打开 `http://127.0.0.1:7860`，操作流程：

1. 拖入歌曲文件（mp3 / wav / flac）
2. 点击「开始转换」，观察主界面进度条：
   `检查输入 → Demucs 分离中 → basic-pitch 转写 → 导出`
3. 在右侧预览区试听人声轨，下载 MIDI 文件与音符 CSV

界面内可切换 Demucs 模型、计算设备（auto/cpu/cuda），高级转写参数折叠在页面下方。
若输入已是分离好的人声（干声），勾选「上传的已是人声轨」可跳过分离步骤。

#### 关闭页面后自动释放端口

浏览器不会通知服务端“标签页已关闭”，因此程序用两层机制自行判断：

1. 页面每 5 秒向 `/vocal2midi/heartbeat` 上报一次心跳；
2. 关闭或刷新页面时用 `sendBeacon` 发送一次告别信号。

服务端看门狗线程在「曾经有页面连过、但已连续 N 秒没有心跳、且当前没有转换任务在跑」
时自动退出进程、释放端口。默认 N = 60 秒，可用 `--idle-timeout` 调整：

```bash
python src/app.py                        # 默认：页面离开 60 秒后自动退出
python src/app.py --idle-timeout 20      # 20 秒，退出更快
python src/app.py --idle-timeout 0       # 禁用该功能，一直驻留
python src/app.py --port 8000 --no-browser
```

行为说明：

| 场景 | 结果 |
|---|---|
| 关闭标签页 | 约 N 秒后自动退出，端口释放 |
| 刷新页面 / 短暂切换 | **不会**退出（保留 5 秒宽限 + 新页面立刻续上心跳） |
| 正在进行转换 | **绝不会**退出（任务期间看门狗暂停判定），可放心切到别的窗口 |
| 启动后从未打开过页面 | 不会退出（从未收到心跳则不启动判定） |
| 想立刻退出 | 点界面上的「退出程序」按钮，或直接关闭启动它的终端窗口 |

#### 不使用 start.bat，直接启动

```bat
:: 用已经装好依赖的那个解释器（换成你自己的路径）
"路径\到\python.exe" src\app.py

:: 常用参数
"路径\到\python.exe" src\app.py --port 8000        :: 换端口
"路径\到\python.exe" src\app.py --idle-timeout 0   :: 关闭自动退出，一直驻留
"路径\到\python.exe" src\app.py --no-browser       :: 不自动打开浏览器

:: 命令行版
"路径\到\python.exe" src\main.py "浮夸.mp3"
```

> **注意**：`python` 这个命令在不同终端里可能指向不同解释器（官方 Python、微软商店版、
> MSYS2/Cygwin 的 Python…）。依赖只装在其中一个里，用另一个启动就会报
> `ModuleNotFoundError`。先确认你用的是哪个：
>
> ```bat
> python -c "import sys; print(sys.executable)"
> ```
>
> 用哪个解释器启动，就要用同一个装依赖：`"<上面输出的路径>" -m pip install -r requirements.txt`

### 命令行

```bash
# 完整流程：分离 + 转写
python src/main.py "浮夸.mp3"

# 已有人声轨，仅转写（跳过最慢的分离步骤）
python src/main.py --vocals out/stems/浮夸_vocals.wav

# 常用调优示例：抑制碎音与鬼音
python src/main.py "浮夸.mp3" --onset 0.7 --min-note-len 150
```

## 输出说明

所有产物输出至 `out/` 目录：

| 文件 | 说明 |
|---|---|
| `stems/<歌名>_vocals.wav` | Demucs 分离出的人声轨（自动缓存，重复运行不重算；可在 UI 中试听） |
| `<歌名>_vocals.mid` | 最终人声 MIDI，含 pitch bend，可直接拖入 DAW |
| `<歌名>_vocals_notes.csv` | 音符事件明细（起始/结束/时长/音高/幅度/弯音），可用 Excel 打开核对 |

## 参数参考

### 界面参数（`src/app.py`）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--port` | `7860` | 监听端口 |
| `--idle-timeout` | `60` | 页面关闭后多少秒自动退出并释放端口，`0` 为禁用 |
| `--no-browser` | 关 | 启动时不自动打开浏览器 |

### 命令行参数（`src/main.py`）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-o, --out` | `out` | 输出目录 |
| `--vocals` | — | 指定已分离的人声文件，跳过 Demucs |
| `--model` | `htdemucs_ft` | Demucs 模型（fine-tuned 版，效果优于默认 `htdemucs`） |
| `--device` | `auto` | 计算设备：`auto` / `cpu` / `cuda` |
| `--onset` | `0.5` | 音符起始检测阈值 |
| `--frame` | `0.3` | 音符持续判定阈值 |
| `--min-note-len` | `100` | 最短音符时长（毫秒） |
| `--f0-min` / `--f0-max` | `70` / `900` | 人声基频范围（Hz），`0` 为不限制 |
| `--overwrite` | 关 | 强制重新分离，忽略人声轨缓存 |
| `-v, --verbose` | 关 | 输出调试日志 |

### 调优指南

| 症状 | 建议调整 |
|---|---|
| 短碎音、鬼音较多 | `--onset 0.6~0.8`，`--min-note-len 150~200` |
| 音符拖尾、该断不断 | `--frame 0.4~0.5` |
| 出现八度/五度错音 | 收紧 `--f0-min` / `--f0-max` 至实际人声音域 |
| 弱唱、气声音符漏检 | `--onset 0.4`、`--frame 0.2`，放宽 `--f0-min` |
| 伴奏残留过多 | 确认使用 `--model htdemucs_ft` |

## 常见问题

| 问题 | 原因与解决 |
|---|---|
| `ffmpeg not found` 或 mp3 解码失败 | ffmpeg 未安装或不在 PATH，重开终端验证 `ffmpeg -version` |
| 分离阶段卡在下载模型 | 首次运行需联网拉取权重；若走代理，确认代理放行了模型下载域名 |
| 分离非常慢 | 运行在 CPU 上；有 N 卡时安装 CUDA 版 torch 并选 `--device cuda` |
| 提示未安装 basic-pitch | `pip install basic-pitch`（详见下方 Python 3.13 注意事项） |
| `ModuleNotFoundError: No module named 'gradio'`（依赖明明装过） | 启动用的解释器和装依赖的解释器不是同一个。先看 `python -c "import sys; print(sys.executable)"` 输出的路径，再用该路径 `-m pip install -r requirements.txt`；或直接用装好依赖的那个解释器启动 |
| 未检出任何音符 | 调低 `--onset` / `--frame`；确认 `--f0-min` / `--f0-max` 覆盖了实际音域 |
| 关闭页面后端口仍被占用 | 见「关闭页面后自动释放端口」；必要时调小 `--idle-timeout`。若进程已残留，用 `netstat -ano \| findstr 7860` 找到 PID 后 `taskkill /PID <PID> /F` |
| 换端口启动报错 | 7860 被其他程序占用，用 `--port 8000` 换一个 |
| 转出结果有不少错音 | 属预期，见下节“效果预期” |

## Python 版本与解释器

| 解释器 | 能否直接用 | 说明 |
|---|---|---|
| 官方 Python 3.10 ~ 3.12 | ✅ 推荐 | `py install 3.12` 或从 python.org 安装 |
| 官方 Python 3.13 | ⚠️ 可运行 | basic-pitch 需走 ONNX 后端（见下） |
| 官方 Python 3.14 | ⚠️ 未验证 | PyTorch 等未必有对应预编译包 |
| 微软商店版 Python | ❌ | 沙箱化路径，安装二进制依赖常失败 |
| MSYS2 / Cygwin 的 Python | ❌ | 与系统 DLL 冲突，torch 安装易失败 |
| 项目内 `.venv` | ✅ 最佳 | 依赖隔离，VS Code 与 start.bat 都会自动识别 |

### Python 3.13 已知问题

basic-pitch 0.4.0 在 Python ≥ 3.11 的 Windows 上会强制安装 TensorFlow 2.15，而 TF 2.15 没有 3.13 的预编译包，直接 `pip install basic-pitch` 会失败。解决办法是改用其官方支持的 ONNX 后端：

```bash
pip install onnxruntime mir-eval
pip install --no-deps basic-pitch
```

（scipy / scikit-learn / resampy 等其余依赖通常已随 librosa 等包就位。）

## 分发给他人

### 打包发布包

```bash
python tools/build_release.py     # 生成 dist/vocal2midi-v0.1.0.zip
```

发布包内只有 7 个文件（约 50 KB），**不包含**虚拟环境、模型权重和输出目录——
接收方首次运行时自动安装依赖并下载模型。

### 接收方使用步骤

1. 解压 zip
2. 安装 [Python 3.10~3.12](https://www.python.org/downloads/)（Windows 安装时须勾选 *Add Python to PATH*）
3. 安装 ffmpeg：`winget install Gyan.FFmpeg`（macOS：`brew install ffmpeg`；Ubuntu：`sudo apt install ffmpeg`）
4. 双击 `start.bat`（macOS/Linux 执行 `bash start.sh`）

脚本会自动创建虚拟环境、安装依赖、启动网页界面。首次安装需下载约 2~3 GB 依赖，
首次转换需额外下载数百 MB 模型权重，请预留磁盘空间与网络时间。

### 其他分发方式

| 方式 | 说明 |
|---|---|
| **源码 zip**（推荐） | 上面的做法。体积小、跨平台；代价是接收方要装 Python 和 ffmpeg |
| **PyInstaller 单文件 exe** | 理论上可做到双击即用，但需打包 torch + TensorFlow，产物体积约 2~4 GB，构建耗时且易踩坑，不推荐 |
| **在线 Demo（Hugging Face Spaces / Colab）** | 零安装，对方只需浏览器；适合分给非技术用户，但需联网且算力受限 |

### 需要发送的文件夹

只需 `src/main.py`、`src/app.py`、`requirements.txt`、`README.md`、`LICENSE`、`start.bat`、`start.sh`。
**不要**发送 `.venv/`（含本机绝对路径，拷过去无法使用）、`out/`（输出产物）、
`.workbuddy/`（本地项目记忆）、`__pycache__/`。

## 效果预期

- basic-pitch 官方定位是“转写的起点”：对单声部人声效果较好，但颤音段落、气声起音仍会出现误差，通常需要在 DAW 中人工修正 20~40%。
- 人声转谱本身不存在唯一正确答案。Spotify 的 vocadito 数据集实验显示，两位专业乐手独立标注同一段人声，互相一致性 F-measure 仅 0.64。因此本类工具的准确率上限约 70~80%。
- 本项目不做时间量化（保留演唱的真实节奏），对拍、量化请在 DAW 中完成。

## 许可证

本项目代码按 MIT 许可证发布。所用第三方组件的许可证如下：

| 组件 | 许可证 |
|---|---|
| Demucs | MIT |
| basic-pitch | Apache 2.0 |
| Gradio | Apache 2.0 |
