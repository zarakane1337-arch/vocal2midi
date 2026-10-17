# GitHub Release 文案（v2.0.0）

> 用法：打开 https://github.com/zarakane1337-arch/vocal2midi/releases/new
> → Tag 填 `V2.0.0`（选 Create new tag on publish）
> → Release title 填 `v2.0.0` → 正文粘贴下面 `---` 之间的内容
> → 附件上传 `vocal2midi-2.0.0-setup.exe`
>
> 正文即 `CHANGELOG.md` 的 `## [2.0.0]` 段，只在末尾追加「校验」小节。
> 发新版本时改这里之前，先改 CHANGELOG，再整段同步过来。

---

## [2.0.0] - 2026-10-18

**摘要：底层重构为 `src/vocal2midi/` 包；首次提供一键安装包，只有 10.8 MB ——
torch / demucs / PySide6 / 模型权重都不再打包，改为安装时实测国内镜像速度、
从最快的源自动下载；界面新增深色 / 浅色 / 跟随系统主题；转写参数全部支持
「滑动条 + 输入框」双向调节，并新增自由文本自定义参数；程序改为纯图形界面，
不再提供命令行与源码运行方式。**

### 架构

- **重写为 `src/vocal2midi/` 包**，按职责分层：

  | 层 | 模块 | 职责 |
  |---|---|---|
  | 业务 | `core/params.py` | 参数注册表，11 个参数的定义只有这一份 |
  | 业务 | `core/separation.py` | Demucs 混音 → 人声 wav |
  | 业务 | `core/transcription.py` | 人声 wav → 音符 → MIDI / CSV（SwiftF0） |
  | 业务 | `core/pipeline.py` | 两阶段流水线，进度 / 日志 / 取消通过回调上报 |
  | 业务 | `core/net.py` `core/mirrors.py` | 多连接下载器、镜像测速选源 |
  | 业务 | `core/installer.py` `core/weights.py` | 依赖安装引擎、权重按需下载 |
  | 界面 | `ui/main_window.py` `ui/theme.py` `ui/widgets.py` | 主窗口、主题引擎、卡片与参数行 |
  | 路径 | `paths.py` | 所有路径决策集中在此 |

- 界面层只调用 `core.pipeline.run_pipeline()`，业务层不 import PySide6，可以单独测试；
  换转写引擎只改 `core/transcription.py` 内部，保持事件格式
  `(start_s, end_s, pitch_midi, amplitude, [bend…])` 不变即可。
- `paths.py` 同时适应「开发态 `src/` 布局」与「安装态 `runtime/` + `app/` 布局」。

### 安装包 10.8 MB

- payload 只有三样东西：内置 Python 3.12 embed 运行时（21.5 MB）、应用本体（0.2 MB）、
  C# 启动器（52 KB），合计 21.75 MB，经 LZMA2 压到 10.8 MB（压缩比 0.5）。
- **torch / torchaudio / demucs / PySide6 / onnxruntime / numpy / 模型权重全部不打包。**
- ffmpeg 改由 `imageio-ffmpeg` 提供，安装后复制到 `runtime/bin/ffmpeg.exe`，
  终端用户无需预装 Python、ffmpeg 或任何环境。
- 界面只用 QtCore / QtGui / QtWidgets，因此装 **PySide6-Essentials** 而不是完整版
  PySide6，省掉 153 MB 的 Addons。

### 依赖改为安装时获取

- **实测镜像真实吞吐量**（下载一段真实 wheel 测速，而不是只 ping 延迟），
  按 `吞吐量 / (1 + 2 × 延迟)` 排序；装失败自动换下一个，最多尝试前 3 名。
  镜像列表：阿里云 / 中科大 / 清华 / 腾讯云 / 华为云。
- **torch 源与 PyPI 源解耦**：`torch==2.9.1+cpu` / `+cu128` 这类带本地版本号的
  wheel 只存在于 pytorch-wheels 专用镜像，普通 PyPI 镜像上只有不带后缀的 `2.9.1`，
  直接 pin 本地版本号会解析失败。现在 torch / torchaudio 先从
  `pytorch-wheels/<flavor>/` 预取到本地，其余依赖用 `pip --dry-run --report`
  拿到精确的 wheel 地址与 sha256，最后 `--no-index --find-links` 离线装配。
- **自动选择 torch 版本**：检测到 NVIDIA 显卡装 CUDA 版（`cu128`，约 2.7 GB），
  否则装 CPU 版（约 105 MB）；安装向导里也能手动指定。
- **下载器重写为多连接分片**：8 连接并行 + 断点续传 + sha256 校验。
  实测同一份 torch wheel 单连接 1.8 MB/s、8 连接 20 MB/s。
- 安装进度写入 JSON 进度文件，由一个 C# 进度窗口实时显示（Inno Setup 的
  Pascal Script 既没有 `TTimer` 也没有消息泵，无法在向导内部刷新进度）。
- 安装向导支持**深色 / 浅色自动跟随系统**（`WizardStyle=modern dynamic`），界面为中文。

### 界面

- 界面从上一版的 Gradio 网页窗口迁移到 PySide6 原生桌面窗口：不再需要浏览器、
  端口与空闲看门狗，`src/app.py`（Gradio 版）一并退役。
- **深色 / 浅色 / 跟随系统主题**：标题栏下拉切换；「跟随系统」会监听 Windows 的
  深浅色切换并实时跟随。三套配色都做了完整的 QSS + QPalette 双通道适配。
- **转写参数面板**：11 个参数每个都是「滑动条 + 可编辑输入框」双向联动 ——
  拖滑块时输入框同步更新，在输入框键入精确值（回车或失焦生效）时滑块跟着走；
  超出范围自动夹到边界并标红提示。
- **自定义参数标签页**：用自由文本批量覆写参数，支持 `key=value`、`--key value`、
  `key: value`、`key value` 四种写法，空格 / 逗号 / 分号 / 换行都可分隔。
  输入时实时解析，写错的键名单独提示，不影响其它参数生效。
- 参数优先级：`默认值 < 滑动条 < 自定义参数`。
- 新增参数：`pitch_hold_ms`（音高保持时长）、`velocity_floor`（力度下限）、
  `bend_range`（弯音范围）、`tempo`（BPM）、`quantize`（off / 1/4 / 1/8 / 1/16 / 1/32）、
  `transpose`（移调）。
- 界面重排为卡片式分区，三个标签页各自可滚动；输出路径与运行日志收在「输出」卡片里。

### 启动器

- 安装目录下只有一个 `vocal2midi.exe`（C# WinForms，无控制台窗口），负责：
  检查依赖就绪标记 → 缺依赖时弹进度窗口自动安装 → 用 `pythonw -m vocal2midi` 拉起界面。
- 依赖损坏时运行 `vocal2midi.exe --repair` 补装（唯一的恢复入口）。
- 安装时若跳过依赖，第一次启动会自动引导安装，也可以在「⋯ → 安装·修复依赖」重试。

### 修复

- **模型加载卡住几十秒**：demucs 4.x 的 `pretrained.get_model()` 会先尝试
  HuggingFace hub，失败后才回退到本地缓存，国内访问 HF 会长时间挂起。
  现在统一在 `paths.apply_runtime_env()` 里固定 `TORCH_HOME` 并关闭 HF 在线探测
  （`HF_HUB_OFFLINE=1`），模型加载从「挂起 40 秒以上」降到 1.1 秒。
- **权重被下载两次**：`TORCH_HOME` 原先只在一个入口里设置，直接调用
  `separate_vocals()` 时 demucs 会去默认的 `~/.cache/torch` 找权重，
  与 `core.weights` 下载的目录不同，导致缓存失效并回源重下 400 MB。
- `PipelineOptions.params` 声明为 `TranscriptionParams` 但直接传 dict 时不转换，
  导致 `AttributeError: 'dict' object has no attribute 'hold_ms'`。
  新增 `__post_init__` 统一转换，`transcribe_file()` 也接受 dict。
- **参数面板被竖向挤压**：卡片高度不足时每行被压到 14px，滑块和输入框文字被裁切。
  改为每个标签页包裹 `QScrollArea`，参数行固定最小高度。
- **卡片头部按钮错位**：无标题的卡片调用 `add_header_widget()` 时按钮会跑进内容区，
  改为按需惰性创建头部布局。
- **下拉框箭头变成小方块**：QSS 一旦改写 `QComboBox::drop-down`，Qt 就不再绘制内置箭头。
  改为用 QPainter 生成 chevron 图标（深浅两套各一张）并通过 `image:` 引用。
- **启动器重定向输出时没有任何输出**：`AttachConsole` 会抢走已重定向的 stdout 句柄；
  现在检测到重定向就不再附加父控制台，并改用异步管道读取子进程输出。
- 权重下载失败时错误信息里的「实际大小」永远是 0（先 unlink 再 stat），已修正。

### 移除

- **命令行界面**：删除 `src/vocal2midi/cli.py`。启动器保留隐藏的 `--check`
  （供安装器读取环境报告），不作为面向用户的功能。
- **源码运行方式**：删除 `start.bat` / `start.sh`，程序只提供安装版。
- 旧扁平模块 `src/main.py` / `src/gui.py` / `src/separation.py` /
  `src/transcription.py`。现在只有 `src/vocal2midi/` 一个包。
- `src/bootstrap.py` / `src/bootstrap_dialog.py` / `src/install_pyside6.py`
  （功能并入 `core.installer`）。
- 不再打包 `models/` 权重目录与 79 MB 的 ffmpeg-static 二进制。

### 升级注意

- 本版本是新的安装包结构，**建议先卸载旧版再安装**（安装目录与依赖位置都变了）。
- 旧版生成的 `out/*.mid` 不会被自动覆盖。
- 依赖装到一半可以取消，下次运行 `vocal2midi.exe --repair` 会接着装。
- 人声分离模型（htdemucs_ft，约 401 MB）在第一次执行分离时才按需下载，有进度也可取消。

### 校验

| 项目 | 值 |
|---|---|
| 文件 | `vocal2midi-2.0.0-setup.exe` |
| 大小 | 11,354,168 字节（10.83 MB） |
| SHA256 | `FBF0E4F72FA86CE0401C3CF641193E07A366EAE171C947FB6B62244415078164` |

Windows PowerShell 校验：

```powershell
Get-FileHash .\vocal2midi-2.0.0-setup.exe -Algorithm SHA256
```

**Full Changelog**: https://github.com/zarakane1337-arch/vocal2midi/compare/V1.0.0...V2.0.0

---