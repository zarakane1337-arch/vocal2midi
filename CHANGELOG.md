# 更新日志

本项目的所有重要变更都记录在此文件。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。


## [未发布]

### 计划中

- 批量处理：一次导入多首歌
- 分离阶段的音轨拆分（保留伴奏 / 鼓 / 贝斯）

## [2.0.0] - 2026-10-18

**摘要：底层架构全面重构为 `src/vocal2midi/` 包；安装包从 420 MB 降到 11 MB
（不再打包 torch / demucs / PySide6 / 模型权重）；依赖改为安装时自动测速国内镜像
并从最快源下载；界面新增深色/浅色/跟随系统主题；人声转 MIDI 的每个参数都支持
滑动条 + 输入框联动，并新增自由文本自定义参数。**

### 新增

- **深色 / 浅色 / 跟随系统主题**：标题栏下拉框可切换；「跟随系统」会监听 Windows
  的深浅色切换并实时跟随（`QStyleHints.colorSchemeChanged`）。三套配色都做了
  完整的 QSS + QPalette 双通道适配。
- **转写参数面板**：11 个参数每个都是「滑动条 + 可编辑输入框」双向联动。
  拖动滑块时输入框同步更新；直接在输入框键入精确值（回车或失焦生效），
  超出范围自动夹到边界并标红提示。
- **自定义参数**：新增「自定义参数」标签页，支持 `key=value`、`--key value`、
  `key: value`、`key value` 四种写法，空格/逗号/分号/换行均可分隔。
  输入时实时解析并显示识别结果，写错的键名单独提示，不影响其它参数生效。
- 新增参数：`pitch_hold_ms`、`velocity_floor`、`bend_range`、`tempo`、
  `quantize`（off / 1/4 / 1/8 / 1/16 / 1/32）、`transpose`。
- 新增环境自检（内部用，供安装器与启动器读取）：`python -m vocal2midi.core.installer --check`，
  输出 JSON 报告（Python / ffmpeg / torch / CUDA / 缺失模块）。
- 新增 `vocal2midi.exe --repair`：一键补装或修复依赖（依赖损坏时的恢复入口）。

### 变更

- **安装包 420 MB → 11 MB**：payload 只含 Python 3.12 embed 运行时（11 MB）、
  应用本体（0.6 MB）和启动器（51 KB）。torch / demucs / PySide6 / onnxruntime /
  numpy / demucs 权重**全部不再打包**。
- **依赖改由安装程序自动获取**：安装时实测各国内镜像的**真实下载速度**
  （不是只测延迟），排序后从最快的镜像安装，失败自动换下一个（共尝试前 3 名）。
  镜像列表：阿里云 / 中科大 / 清华 / 腾讯云 / 华为云。
  torch 走 pytorch 专用镜像（含 `+cpu` / `+cu128` 本地版本号），
  PyPI 镜像与之解耦，各自独立测速排序。
- **自动选择 torch 版本**：检测到 NVIDIA 显卡装 CUDA 版（`cu128`，约 2.7 GB），
  否则装 CPU 版（约 105 MB）。
- **下载器改为多连接分片**：8 连接并行 + 断点续传 + sha256 校验。
  实测阿里云 pytorch 镜像聚合 20 MB/s（单连接仅 1.8 MB/s）。
- 架构重构为 `src/vocal2midi/` 包，按 `core`（业务）/ `ui`（界面）/ `paths`
  （路径决策）分层。界面层只调用 `core.pipeline.run_pipeline()`，
  通过回调接收进度/日志/取消。
- 依赖从完整版 PySide6 改为 **PySide6-Essentials**（界面只用 QtCore/QtGui/QtWidgets，
  省掉 153 MB 的 Addons）。
- ffmpeg 改由 `imageio-ffmpeg` 提供并复制到 `runtime/bin/ffmpeg.exe`，
  不再依赖系统安装 ffmpeg。
- 安装向导支持深色模式（`WizardStyle=modern dynamic`，跟随系统）。
- 安装界面改为中文（内置 ChineseSimplified.isl）。
- 启动入口统一为一个 `vocal2midi.exe`（安装目录），它负责环境检查、依赖补装与拉起界面。
- `requirements.txt` 改为可手动 `pip install` 的形式，torch 不再写入（需本地版本号）。

### 修复

- **模型加载卡住几十秒**：demucs 4.x 的 `pretrained.get_model()` 会先尝试
  HuggingFace hub，失败后才回退到本地缓存。国内访问 HF 会长时间挂起。
  现在统一在 `paths.apply_runtime_env()` 里固定 `TORCH_HOME` 并关闭 HF 在线探测
  （`HF_HUB_OFFLINE=1`），模型加载从「挂起 40 秒以上」降到 1.1 秒。
- **权重被下载两次**：`TORCH_HOME` 原先只在 `app.py` 里设置，直接调用
  `separate_vocals()` 时 demucs 会去默认的 `~/.cache/torch` 找权重，
  与 `core.weights` 下载的目录不同，导致缓存失效并回源下载 400 MB。
- `PipelineOptions.params` 声明为 `TranscriptionParams` 但直接传 dict 时不转换，
  导致 `AttributeError: 'dict' object has no attribute 'hold_ms'`。
  新增 `__post_init__` 统一转换；`transcribe_file()` 也接受 dict。
- **安装包在特定目录下无法安装**：Inno 的引导程序在解包时需要删除权限，
  在带 Deny-Delete ACL 的目录下会静默失败。已确认与安装包本身无关（同一文件
  在其它目录正常），此为测试环境限制。
- 参数面板布局：卡片被竖向挤压导致滑块只有 14px 高、输入框文字被裁切。
  改为每个标签页包裹 `QScrollArea`，参数行固定最小高度。
- 卡片头部按钮错位：无标题的卡片调用 `add_header_widget()` 时按钮会跑进内容区。
  改为按需惰性创建头部布局。
- 无控制台窗口的启动器在重定向输出时没有任何输出。
  修正 `AttachConsole` 逻辑：检测到 stdout 已重定向时不再附加父控制台，
  并改用异步管道读取子进程输出。

### 移除

- **取消命令行界面**：删除 `src/vocal2midi/cli.py`，程序只提供图形界面。
  启动器保留隐藏的 `--check`（供安装器读取环境报告），不作为面向用户的功能。
- **取消源码运行方式**：删除 `start.bat` / `start.sh` 与旧扁平模块
  `src/main.py` / `src/gui.py` / `src/separation.py` / `src/transcription.py`。
  用户只需运行安装后的 `vocal2midi.exe`。
- 移除 `src/bootstrap.py` / `src/bootstrap_dialog.py` / `src/install_pyside6.py`
  （功能已并入 `core.installer`）。
- 不再打包 `models/` 权重目录与 79 MB 的 ffmpeg-static 二进制。

## [1.1.0] - 2026-10-17

**摘要：界面从 Gradio 网页版换成 PySide6 原生桌面窗口；demucs 权重内置进包体；
新增依赖引导（检测 torch/swift-f0 → 国内镜像测速 → 最快镜像自动安装）。**

### 新增

- `src/gui.py`：PySide6 桌面界面，Gradio UI 的原生替代。文件选择/拖放、模型与设备切换、
  「已是人声轨」「强制重新分离」复选、高级参数（切分阈值/帧判定/最短音符/音高范围）、
  进度条 + 阶段文案、完成后直显 MIDI/CSV 路径。转换在后台线程跑，关窗即退出，
  不再需要浏览器、端口、空闲看门狗。
- `src/bootstrap.py`：依赖引导层。`--check` 打印环境报告（Python/ffmpeg/torch/swift-f0/PySide6/内置权重）；
  `--install` 并发测速国内五大镜像（清华/阿里/中科大/腾讯/豆瓣）并从最快者安装缺失依赖。
- demucs 权重内置：`models/htdemucs_ft/`（5 个 .th 共 401MB），首次分离不再联网下载；
  `bootstrap.link_bundled_weights()` 负责挂载到 torch hub 缓存。
- `start.bat` / `start.sh` 对接新入口 `src/gui.py`；无 .venv 时把依赖安装交给首启引导。
- `installer/`：Inno Setup 安装包。`build_installer.ps1` 一条龙下载 embed Python 3.12.10、
  装 pip、预装除 torch/PySide6 外的全部依赖、取 ffmpeg-static 并编译出
  `vocal2midi-1.1.0-setup.exe`（420 MB）。安装器自带 Python 与 ffmpeg，终端用户无需预装任何环境。
  PySide6 为可勾选组件（安装时测速国内镜像再装）；torch 不打进包体，由首启按 CPU/GPU 安装。
- `installer/vocal2midi.bat`：安装版启动器，把包内 `bin\` 放到 PATH 最前，优先用内置 ffmpeg。
- `src/install_pyside6.py`：安装器勾选 PySide6 时静默执行，测速镜像并安装，日志写
  `%TEMP%\vocal2midi-pyside6.log`。
- `src/bootstrap.py` 新增 `--link-weights`：只挂载内置权重（安装器在 ssPostInstall 阶段调用）。
- `installer/verify_installer.py`：构建后校验 payload 完整性、依赖可 import、ffmpeg 可执行、
  确认未误打包 torch/PySide6/pip，并核对 setup 体积。

> 已知限制：`setup.exe` 的静默参数（`/VERYSILENT`、`/SILENT`）在无交互桌面的环境里会立即
> 以退出码 1 结束（去掉 `[Code]` 段复测同样如此，非脚本问题）；带界面的正常安装路径未受
> 影响。CI/自动化部署需要的话，改用 Inno 的 `/LOADINF`+有桌面会话的 runner。

### 变更

- 默认入口从 `src/app.py`（Gradio 网页）改为 `src/gui.py`（桌面窗口）。
- `requirements.txt`：`gradio>=5.0` → `PySide6>=6.5`。
- 修复 `start.bat` 的 `:py_ok` 标签重复缺陷：无 .venv 的新用户此前会落入
  「没有找到可用的 Python」的错误分支，永远装不了依赖。

### 移除

- `src/app.py`（Gradio 网页 UI）与 `src/desktop.py`（WebView 壳）退役删除。
  命令行入口 `src/main.py` 不受影响。

## [1.0.0] - 2026-10-17

**摘要：转写引擎从 basic-pitch 换成 SwiftF0。** 在同一段 Demucs 分离好的单人声轨上，
音准中位误差从 25.8¢ 降到 9.2¢、音符 F1 从 0.60 升到 0.83、速度约快 1.8 倍，
并且终于能把颤音和滑音完整写进 MIDI。

### 新增

- `docs/swiftf0-vs-basic-pitch.md`：SwiftF0 与 basic-pitch 的完整实测对比报告，
  含逐帧音准、音符级 F1、音符边界、速度、basic-pitch 的结构性问题分析，以及全部复现命令。
- `docs/swiftf0_vs_basicpitch_pitch.png`：同一段人声的逐帧音高对比图（连续轨迹 vs 半音台阶）。
- `docs/swiftf0_vs_basicpitch_metrics.png`：音准误差累积分布 + 关键指标面板。
- 自建 MIDI 写入器：整数音符 + 音符窗内连续 pitchwheel 弯音，力度由 `loudness_db` 折算，
  不再受 SwiftF0 内置 `export_to_midi()` 的限制（它不写弯音、力度写死 80）。

### 变更

- **转写引擎：basic-pitch → SwiftF0**（14K 参数的 ONNX 模型，16 ms 一帧，MIT 许可）。
  实测对比（210 秒日文歌人声轨，参照 librosa pYIN）：

  | 指标 | SwiftF0 | basic-pitch |
  |---|---|---|
  | 帧级音高中位误差 | **9.2¢** | 25.8¢ |
  | 音准在 ±50¢ 内 | **91.2%** | 80.2% |
  | 八度错误率 | **0.75%** | 4.98% |
  | 音符 F1 | **0.833** | 0.602 |
  | 推理耗时（热） | **1.32 s** | 2.33 s |

  > 音准一栏是两端都「落盘成 MIDI 再回放」后的结果，口径一致；直接比较原始帧级 F0
  > 时 SwiftF0 是 7.1¢、basic-pitch 是 25.8¢，后者几乎不受弯音影响，说明差距来自模型本身而非 MIDI 量化。

- `transcribe_to_midi()` 的签名与事件格式 `(start_s, end_s, pitch_midi, amplitude, [bend…])`
  保持不变，`main.py` / `app.py` 的调用处一行未改。
- `--min-note-len` / `min_note_length_ms` 默认值 **100 → 50**：SwiftF0 的音符分段自带噪声地板，
  50 ms 是对齐实测最优的那一档。
- `--onset` 的含义从 basic-pitch 的 onset 阈值改为映射到 SwiftF0 的 `pitch_hold_ms`
  （`30 × onset / 0.5`，默认 0.5 → 30 ms）。**注意这是行为变化**：现在调高 `--onset` 会让音符更长更少，
  而不再是「更严格地要求起音」。
- 命令行参数分组标题 `basic-pitch 转写参数` → `SwiftF0 转写参数`。
- 依赖大幅瘦身：`basic-pitch>=0.4.0` → `swift-f0[audio,midi]>=0.3.0`。
  basic-pitch 会拖进 TensorFlow / ONNX 以及 librosa、scipy、scikit-learn、resampy、
  pretty_midi、mir_eval 一整条链；SwiftF0 只需要 `onnxruntime` + `numpy`（重采样用 `soxr`，写 MIDI 用 `mido`）。
- README、`docs/pipeline.svg`、`start.bat` 中的引擎说明与依赖检测同步更新。
- `start.bat` 移除已无意义的 Python 3.13 专用 ONNX 安装分支（不再有 TensorFlow 依赖冲突）。

### 修复

- 曲名为中文/日文时写出 MIDI 会抛
  `UnicodeEncodeError: 'latin-1' codec can't encode characters`。
  MIDI 的 meta 文本按规范是 latin-1，现降级为 ASCII 后的 track name。
- Python 3.13 上不再需要「先装 onnxruntime、再 `pip install --no-deps basic-pitch`」的特殊步骤，
  直接 `pip install -r requirements.txt` 即可。

### 移除

- 对 basic-pitch 的运行时依赖。代码不再 import 它；若想保留旧输出做对比，
  可另行 `pip install basic-pitch`。

### 升级注意

```bash
pip install -r requirements.txt
```

- 旧版生成的 `out/*.mid` 不会被自动覆盖，重新跑一次转写即可得到新结果。
- 如果你习惯用 `--min-note-len 150` 压制碎音，换成 SwiftF0 后建议改为 `--onset 0.8 --min-note-len 100`。

## [0.1.0] - 2026-10-15

首个版本：一首歌 → 人声 MIDI。

### 新增

- **分离层** `src/separation.py`：Demucs 从混音中剥离人声，
  默认模型 `htdemucs_ft`，支持 `auto` / `cpu` / `cuda` 设备选择与结果缓存。
- **转写层** `src/transcription.py`：basic-pitch 把单人声轨转成 MIDI 音符（含 pitch bend），
  同时导出音符明细 CSV。
- **命令行入口** `src/main.py`：编排「分离 → 转写 → 落盘」，支持 `--vocals` 跳过分离、
  `--onset` / `--frame` / `--min-note-len` / `--f0-min` / `--f0-max` 调参。
- **网页界面** `src/app.py`：Gradio 界面，带分段进度条、人声轨试听、MIDI 与 CSV 下载；
  Demucs 模型与计算设备可切换。
- 关闭页面后自动释放端口：页面每 5 秒发心跳、关闭时用 `sendBeacon` 告辞，
  服务端看门狗确认「曾有页面 + 无任务 + 超时」后退出进程。
- `start.bat` / `start.sh` 一键启动：自动选解释器、建虚拟环境、装依赖、开界面。
- 分层约定：两个入口只调用两层的公开接口，换转写引擎时只改 `transcription.py` 内部。

[未发布]: https://github.com/zarakane1337-arch/vocal2midi-A-tool-which-can-transform-vocal-into-midi-file/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/zarakane1337-arch/vocal2midi-A-tool-which-can-transform-vocal-into-midi-file/releases/tag/v1.0.0
[0.1.0]: https://github.com/zarakane1337-arch/vocal2midi-A-tool-which-can-transform-vocal-into-midi-file/releases/tag/v0.1.0
