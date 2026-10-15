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
│  人声 / 伴奏分离  │   输出：纯人声轨 vocals.wav
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

### 命令行参数

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
| 未检出任何音符 | 调低 `--onset` / `--frame`；确认 `--f0-min` / `--f0-max` 覆盖了实际音域 |
| 转出结果有不少错音 | 属预期，见下节“效果预期” |

### Python 3.13 已知问题

basic-pitch 0.4.0 在 Python ≥ 3.11 的 Windows 上会强制安装 TensorFlow 2.15，而 TF 2.15 没有 3.13 的预编译包，直接 `pip install basic-pitch` 会失败。解决办法是改用其官方支持的 ONNX 后端：

```bash
pip install onnxruntime mir-eval
pip install --no-deps basic-pitch
```

（scipy / scikit-learn / resampy 等其余依赖通常已随 librosa 等包就位。）


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
