# vocal2midi

把一首完整歌曲中的人声旋律提取为 MIDI 文件。

输入一段含伴奏的音频，程序会先分离出人声轨，再将人声转写为 MIDI 音符序列，输出可直接导入 DAW（Ableton Live、Logic Pro、FL Studio 等）的 `.mid` 文件及音符明细表。

## 技术原理

![vocal2midi 管线原理图](docs/pipeline.svg)

代码按"两层 + 两个入口"组织：**分离层**与**转写层**互不耦合，
`main.py`（命令行）和 `app.py`（网页界面）都只是这两层的调用方。

## 使用的库

| 库 | 版本要求 | 角色 |
|---|---|---|
| [Demucs](https://github.com/adefossez/demucs) | `>=4.0.1` | 源分离：从混音中剥离人声（默认模型 `htdemucs_ft`） |
| [SwiftF0](https://github.com/lars76/swift-f0) | `>=0.3.0` | 音高跟踪：人声 → 连续 F0 → MIDI（含 pitch bend） |
| [Gradio](https://gradio.app) | `>=5.0` | 网页界面（`app.py`） |
| [PyTorch](https://pytorch.org) | 随 demucs 自动带入 | Demucs 的推理后端；装 CUDA 版可 GPU 加速 |
| soundfile / torchaudio | 随上列包带入 | 读写 wav、音频解码 |
| onnxruntime | 随 swift-f0 自动带入 | SwiftF0 的推理后端（CPU 即可，无需 GPU） |
| ffmpeg（系统依赖） | — | mp3/flac 解码，Demucs 读非 wav 格式时必需 |

Python 包清单见 `requirements.txt`。

> 为什么不用 basic-pitch：两者在 Demucs 分离后的单人声轨上实测过一轮，
> SwiftF0 的帧级音高中位误差 7.1¢（basic-pitch 25.8¢）、音符 F1 0.83（basic-pitch 0.60）、
> 端到端快 1.9×，且能保住颤音与滑音。完整数据见 [`docs/swiftf0-vs-basic-pitch.md`](docs/swiftf0-vs-basic-pitch.md)。

## 项目结构

```
vocal2midi/
├── src/
│   ├── app.py            # 入口 1：Gradio 网页 UI
│   ├── main.py           # 入口 2：命令行（编排：分离 → 转写 → 落盘）
│   ├── separation.py     # 分离层：Demucs 混音 → 人声 wav
│   └── transcription.py  # 转写层：人声 wav → 音符 → MIDI / CSV（SwiftF0）
├── docs/
│   ├── pipeline.svg      # README 上方的管线原理图（可编辑，深色模式自动换色）
│   ├── swiftf0-vs-basic-pitch.md        # SwiftF0 与 basic-pitch 的完整实测对比报告
│   ├── swiftf0_vs_basicpitch_pitch.png  # 逐帧音高对比（连续轨迹 vs 半音台阶）
│   ├── swiftf0_vs_basicpitch_metrics.png # 音准误差 CDF + 关键指标面板
│   └── release-notes-v1.0.0.md          # v1.0.0 的 GitHub Release 文案
├── start.bat             # Windows 一键启动（自动建虚拟环境、装依赖、开界面）
├── start.sh              # macOS / Linux 一键启动
├── requirements.txt      # Python 依赖
├── CHANGELOG.md          # 更新日志
├── LICENSE
├── README.md
└── out/                  # 运行时生成：人声轨、MIDI、CSV
```

### 分层约定

- `src/main.py` 与 `src/app.py` 只调用两层的公开接口：
  **分离层** `separation.py` → `separate_vocals()`；**转写层** `transcription.py` → `transcribe_to_midi()` / `write_notes_csv()`。
- 两层可以各自单独运行调试，不必跑整条管线：

  ```bash
  python src/separation.py "song.mp3" -o out/stems            # 只跑分离
  python src/transcription.py out/stems/song_vocals.wav -o out  # 只跑转写（可复用现成人声轨）
  ```

- 换转写引擎（例如把 SwiftF0 换成别的 F0/转录模型）时只改 `transcription.py` 内部，
  保持 `transcribe_to_midi()` 的签名与事件格式 `(start_s, end_s, pitch_midi, amplitude, [bend…])` 不变，
  `main.py` / `app.py` 一行都不用改。
- `main.py` 兼容 `python src/main.py` 与 `python -m src.main` 两种启动方式；
  `from main import separate_vocals` 这类旧写法仍然有效（`__all__` 已转发）。

输出目录固定为项目根目录的 `out/`，与启动时的工作目录无关。

## 环境要求

- **Python 3.10 – 3.12**（3.13 及以上部分依赖无预编译包，详见「Python 3.13 已知问题」）
- **ffmpeg**：须安装并加入 PATH（mp3 解码、Demucs 读非 wav 格式时依赖）
- 可选：NVIDIA GPU + CUDA 版 PyTorch（可将 Demucs 分离从数分钟缩短到数十秒）
- 磁盘：首次运行会下载模型权重（Demucs 约数百 MB；SwiftF0 的 ONNX 模型仅 135 KB，随包分发）

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

首次运行时 Demucs 会自动下载模型权重，需保持联网（SwiftF0 的模型已随 PyPI 包分发，无需额外下载）。

## 快速开始

### 网页界面（推荐）

```bash
python src/app.py
```

浏览器自动打开 `http://127.0.0.1:7860`，操作流程：

1. 拖入歌曲（mp3 / wav / flac）；若上传的已是干声人声，勾选「上传的已是人声轨（跳过 Demucs 分离）」
2. 点击「开始转换」，主界面进度条依次显示：
   `检查输入 → Demucs 分离中 → SwiftF0 转写 → 导出音符表 → 完成`
3. 右侧可试听人声轨，并下载 MIDI 与音符 CSV
4. 不想用了点「退出程序」直接关掉服务

界面可切换 Demucs 模型（`htdemucs_ft` / `htdemucs` / `mdx_extra_q`）、计算设备（`auto` / `cpu` / `cuda`）、
是否忽略人声轨缓存；转写参数折叠在页面下方的「高级参数」里。

**UI 启动参数**

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--port` | `7860` | 监听端口 |
| `--idle-timeout` | `60` | 页面全部关闭且无任务运行 N 秒后自动退出、释放端口；`0` 关闭该功能 |
| `--no-browser` | 关 | 启动时不自动打开浏览器 |

> 关闭页面后自动释放端口：浏览器与服务端之间没有"标签页已关闭"的原生通知，
> 因此页面每 5 秒发一次心跳、关闭/刷新时用 `sendBeacon` 发一次告别，
> 服务端看门狗确认"曾有过页面 + 无任务在跑 + 超时"后退出进程。

### 命令行

```bash
# 完整流程：分离 + 转写
python src/main.py "浮夸.mp3"

# 已有人声轨，仅转写（跳过最慢的分离步骤）
python src/main.py --vocals out/stems/浮夸_vocals.wav

# 常用调优示例：抑制碎音与鬼音
python src/main.py "浮夸.mp3" --onset 0.8 --min-note-len 100
```

## 输出说明

所有产物输出至 `out/` 目录：

| 文件 | 说明 |
|---|---|
| `stems/<歌名>_vocals.wav` | Demucs 分离出的人声轨（自动缓存，重复运行不重算；可在 UI 中试听） |
| `<歌名>_vocals.mid` | 最终人声 MIDI，含 pitch bend，可直接拖入 DAW |
| `<歌名>_vocals_notes.csv` | 音符事件明细（起始 / 结束 / 时长 / 音高 / 幅度 / 弯音），可用 Excel 打开核对 |

## 参数参考

### 命令行参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `input` | — | 输入歌曲文件（`mp3` / `wav` / `flac` …） |
| `-o, --out` | `out` | 输出目录 |
| `--vocals` | — | 指定已分离的人声文件，跳过 Demucs |
| `--model` | `htdemucs_ft` | Demucs 模型（fine-tuned 版，效果优于默认 `htdemucs`） |
| `--device` | `auto` | 计算设备：`auto` / `cpu` / `cuda`（只影响分离层） |
| `--overwrite` | 关 | 强制重新分离，忽略人声轨缓存 |
| `--onset` | `0.5` | 音符切分灵敏度（映射到 SwiftF0 的 `pitch_hold_ms`，调高音符更长更少） |
| `--frame` | `0.3` | 发声置信度阈值（音符覆盖帧的平均置信度低于此值则丢弃） |
| `--min-note-len` | `50` | 最短音符时长（毫秒） |
| `--f0-min` / `--f0-max` | `70` / `900` | 人声基频范围（Hz），`0` 为不限制 |
| `-v, --verbose` | 关 | 输出调试日志 |

### 调优指南

| 症状 | 建议调整 |
|---|---|
| 短碎音、鬼音较多 | `--onset 0.7~0.9`（音符切分更保守），`--min-note-len 100~150` |
| 音符拖尾、该断不断 | `--onset 0.3~0.4`（切得更细） |
| 出现八度/五度错音 | 收紧 `--f0-min` / `--f0-max` 至实际人声音域 |
| 弱唱、气声音符漏检 | `--frame 0.2`，放宽 `--f0-min` |
| 伴奏残留过多 | 确认使用 `--model htdemucs_ft`，或改用 `mdx_extra_q` 对比 |

## 常见问题

| 问题 | 原因与解决 |
|---|---|
| `ffmpeg not found` 或 mp3 解码失败 | ffmpeg 未安装或不在 PATH，重开终端验证 `ffmpeg -version` |
| 分离阶段卡在下载模型 | 首次运行需联网拉取权重；若走代理，确认代理放行了模型下载域名 |
| 分离非常慢 | 运行在 CPU 上；有 N 卡时安装 CUDA 版 torch 并选 `--device cuda` |
| 提示未安装 swift-f0 | `pip install "swift-f0[audio,midi]"` |
| `No module named 'gradio'` | 终端里的 `python` 与装依赖的解释器不是同一个，用同一解释器重装依赖 |
| 关闭页面后端口仍被占用 | 用 `--idle-timeout`（默认 60 秒）等待自动退出，或调小该值 |
| 未检出任何音符 | 调低 `--onset` / `--frame`；确认 `--f0-min` / `--f0-max` 覆盖了实际音域 |
| 转出结果有不少错音 | 属预期，见「效果预期」 |

### Python 3.13 说明

改用 SwiftF0 后不再有 TensorFlow 依赖，Python 3.13 上不再需要特殊安装步骤（旧版 basic-pitch 必须绕开 TF，问题是 TF 2.15 没有 3.13 的轮子）。SwiftF0 要求 Python ≥ 3.8，纯 onnxruntime + numpy，3.13 可直接 `pip install -r requirements.txt`。

## 效果预期

- SwiftF0 是单声部 F0 跟踪器，正好匹配"Demucs 已把伴奏剥掉"的前提；它的输出是连续音高，写进 MIDI 时通过 pitchwheel 保留颤音与滑音。
- 实测（210 秒日文歌曲人声轨）：回放 MIDI 对 pYIN 参照的中位误差 9.2¢、91% 的音符在 ±50¢ 内、八度错误 0.75%。
- 人声转谱本身不存在唯一正确答案。Spotify 的 vocadito 数据集实验显示，两位专业乐手独立标注同一段人声，互相一致性 F-measure 仅 0.64。因此本类工具的准确率上限约 70~80%。
- 本项目不做时间量化（保留演唱的真实节奏），对拍、量化请在 DAW 中完成。

## 更新日志

见 [`CHANGELOG.md`](CHANGELOG.md)。当前版本 **v1.0.0**（转写引擎换成 SwiftF0）。

## 许可证

本项目代码按 MIT 许可证发布。所用第三方组件的许可证如下：

| 组件 | 许可证 |
|---|---|
| Demucs | MIT |
| SwiftF0 | MIT |
| Gradio | Apache 2.0 |
| PyTorch | BSD-3-Clause |
