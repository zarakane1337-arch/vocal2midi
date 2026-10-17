# GitHub Release 文案草稿（v2.0.0）

> 用法：打开 https://github.com/zarakane1337-arch/vocal2midi/releases/new
> → Tag 填 `V2.0.0`（选 Create new tag on publish）→ Release title 填 `v2.0.0 — 安装包从 420 MB 降到 11 MB`
> → 正文直接粘贴下面 `---` 之间的内容 → 附件上传 `vocal2midi-2.0.0-setup.exe`。

---

## 安装包从 420 MB 降到 11 MB，依赖改为安装时从最快的国内镜像获取

这个版本重写了程序的底层架构和整套安装方式。旧版把 PyTorch、Demucs、PySide6、
模型权重全部打进安装包，装出来 560 MB、下载 420 MB；现在安装包里**只有程序本体**，
真正的依赖在安装时现场测速、从最快的镜像拉取。

### 安装包组成

| 内容 | 体积 |
|---|---|
| Python 3.12 embed 运行时（含标准库） | 21.46 MB |
| 应用本体（全部源码 + 图标） | 0.22 MB |
| 启动器 `vocal2midi.exe` | 52 KB |
| 文档 | 28 KB |
| payload 合计 | 21.75 MB |
| **安装包（LZMA2 压缩后）** | **10.83 MB** |

对比 1.1.0 的安装包 420.2 MB，体积压到 **1/39**。

`torch` / `torchaudio` / `demucs` / `PySide6` / `onnxruntime` / `numpy` 和 401 MB 模型权重
**全部不在安装包里**。

### 安装时到底发生了什么

1. **实测镜像真实吞吐量** —— 不是 ping 延迟，而是下载一段真实 wheel 计时，
   按 `吞吐量 / (1 + 2 × 延迟)` 排序。失败自动换下一个，最多尝试前 3 名
2. **自动识别显卡** —— 有 NVIDIA 显卡装 CUDA 版 torch（`cu128`，约 2.7 GB），
   否则装 CPU 版（约 105 MB）。安装向导里也能手动指定
3. **8 连接分片下载** —— 同一份 torch wheel 单连接实测 1.8 MB/s、8 连接 20 MB/s，
   带断点续传和 sha256 校验
4. **导入自检** —— 全部装完逐个 import 一遍，通过才写标记文件，之后启动不再检查

依赖装到一半可以取消，下次运行 `vocal2midi.exe --repair` 会接着装。

### torch 源的坑

`torch==2.9.1+cpu` / `+cu128` 这类带本地版本号的 wheel **只存在于 pytorch-wheels 专用镜像**，
普通 PyPI 镜像上只有不带后缀的 `2.9.1`，直接 pin 本地版本号会解析失败
（`ERROR: No matching distribution found for torch==2.9.1+cpu`）。

现在的做法是两条源解耦：torch / torchaudio 先从 `pytorch-wheels/<flavor>/` 预取到本地，
其余依赖用 `pip --dry-run --report` 拿到精确的 wheel 地址与 sha256，
最后 `--no-index --find-links` 离线装配。这样 2.7 GB 的 CUDA wheel 只下载一次。

### 架构重构

`src/` 下原来 6 个平铺模块（`main.py` / `gui.py` / `separation.py` / `transcription.py` /
`bootstrap.py` / `bootstrap_dialog.py`）拆成了有明确分层的包：

```
src/vocal2midi/
├── core/     params  separation  transcription  pipeline
│             net  mirrors  installer  weights  environment  settings  paths
├── ui/       main_window  theme  widgets  worker  deps_dialog
├── app.py    启动编排
└── paths.py  所有路径决策的唯一来源
```

- 界面层只调用 `core.pipeline.run_pipeline()`，靠回调拿「进度 / 日志 / 取消」
- 换转写引擎只改 `core/transcription.py`，保持事件格式
  `(start_s, end_s, pitch_midi, amplitude, [bend…])` 不变
- `paths.py` 同时适应「开发态 `src/` 布局」和「安装态 `runtime/` + `app/` 布局」

### 界面

- **深色 / 浅色 / 跟随系统**三档主题，跟随系统用 `QStyleHints.colorScheme()`，
  系统切换时实时生效。安装向导本身也是 `WizardStyle=modern dynamic`，跟着系统走
- 转写参数改成 **11 个滑动条 + 输入框双向联动**，拖滑块数值同步变，直接输数字滑块跟着动
- 新增「自定义参数」标签页，支持 `key=value` / `--key value` / `key: value` / `key value`
  四种写法，一次改多个参数

### 参数

11 个参数（滑动条可调）：

| 参数 | 含义 | 默认 |
|---|---|---|
| `onset_threshold` | 音符切分灵敏度 | 0.50 |
| `frame_threshold` | 发音置信度阈值 | 0.30 |
| `min_note_length_ms` | 最短音符时长 | 50 ms |
| `f0_min` / `f0_max` | 基频搜索范围 | 70 / 900 Hz |
| `pitch_hold_ms` | 音高保持时长（0 = 按 onset 自动） | 0 |
| `velocity_floor` | 力度下限 | 0 |
| `bend_range` | 弯音范围（半音） | 2 |
| `tempo` | MIDI 速度 | 120 |
| `quantize` | 量化精度（关 / 1/4 / 1/8 / 1/16 / 1/32） | 关 |
| `transpose` | 移调 | 0 |

新增的 `pitch_hold_ms`、`velocity_floor`、`bend_range`、`tempo`、`quantize`、`transpose`
是这一版才有的，以前只能改前五个。

### 取消的东西

- **取消命令行界面** —— 目标用户不需要，程序只提供图形界面
- **取消源码运行方式** —— 删掉 `start.bat` / `start.sh`，用户只需运行安装后的 `vocal2midi.exe`

### 修复

- **卡在「加载模型」几十秒** —— demucs 4.1.0 的 `get_model()` 会先探测 HuggingFace
  hub，国内直连会挂很久才回退到本地 `.th` 缓存。现在强制 `HF_HUB_OFFLINE=1`，
  加载时间从 40 s+ 降到 1.1 s
- **权重重复下载** —— `TORCH_HOME` 原来只在 GUI 入口设置，命令行或直接调用
  `separate_vocals()` 时会走默认的 `~/.cache/torch`，导致 401 MB 权重下两遍
- **`PipelineOptions.params` 传 dict 崩溃** —— 直接构造时没做类型转换，
  运行到 `params.hold_ms` 抛 `AttributeError`
- **参数行被压成 14 px** —— 参数变多后卡片被布局纵向挤压，滑块和输入框全挤在一起；
  现在每个标签页都有独立滚动区
- **下拉框箭头变成小方块** —— QSS 一旦碰了 `QComboBox::drop-down`，Qt 就不再画内置箭头

### 校验

```
文件：vocal2midi-2.0.0-setup.exe
大小：11,354,168 字节（10.83 MB）
SHA256：FBF0E4F72FA86CE0401C3CF641193E07A366EAE171C947FB6B62244415078164
```

Windows PowerShell 校验：

```powershell
Get-FileHash .\vocal2midi-2.0.0-setup.exe -Algorithm SHA256
```

### 已知限制

- 首次启动需要联网下载依赖（CPU 版约 105 MB，GPU 版约 2.7 GB）和 401 MB 模型权重
- 安装包没有代码签名证书，Windows 会提示「未知发布者」
- 人声转谱没有唯一正确答案，Spotify vocadito 数据集的实验显示两位专业乐手独立标注
  同一段人声，互相一致性 F-measure 只有 0.64，这类工具的天花板大约在 70~80%
- RTX 50 系（Blackwell，算力 12.0）需要 CUDA 12.8 及以上，所以 GPU 版固定用 `cu128`

**Full Changelog**: https://github.com/zarakane1337-arch/vocal2midi/compare/V1.0.0...V2.0.0

---
