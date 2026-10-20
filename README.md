# vocal2midi

把歌曲里的人声旋律提取为 MIDI 文件——一次可以排入多首，让它自己跑完。

输入含伴奏的音频，程序会先分离出人声轨，再将人声转写为 MIDI 音符序列，输出可直接导入 DAW（Ableton Live、Logic Pro、FL Studio 等）的 `.mid` 文件及音符明细表。

## 技术原理

![vocal2midi 管线原理图](docs/pipeline.svg)

代码按「核心层 + 界面层」组织：**分离层**与**转写层**互不耦合，
桌面界面只是核心层的调用方，`core.batch` 负责按队列顺序把多首曲目逐个跑完。

## 使用的库

| 库 | 版本要求 | 角色 |
|---|---|---|
| [Demucs](https://github.com/adefossez/demucs) | `4.1.0` | 源分离：从混音中剥离人声（默认模型 `htdemucs_ft`，权重按需下载） |
| [SwiftF0](https://github.com/lars76/swift-f0) | `0.3.0` | 音高跟踪：人声 → 连续 F0 → MIDI（含 pitch bend） |
| [PySide6](https://www.qt.io) | `6.9.3`（Essentials） | 桌面界面，原生窗口，无需浏览器 |
| [PyTorch](https://pytorch.org) | 随 demucs 自动带入 | Demucs 的推理后端；装 CUDA 版可 GPU 加速 |
| soundfile | 随 demucs 带入 | 读写 wav |
| onnxruntime | 随 swift-f0 带入 | SwiftF0 的推理后端（CPU 即可，无需 GPU） |
| ffmpeg | imageio-ffmpeg 提供 | mp3/flac 解码；安装版自带 `runtime/bin/ffmpeg.exe` |

Python 包清单见 `requirements.txt`。

> 为什么不用 basic-pitch：两者在 Demucs 分离后的单人声轨上实测过一轮，
> SwiftF0 的帧级音高中位误差 7.1¢（basic-pitch 25.8¢）、音符 F1 0.83（basic-pitch 0.60）、
> 端到端快 1.9×，且能保住颤音与滑音。完整数据见 [`docs/swiftf0-vs-basic-pitch.md`](docs/swiftf0-vs-basic-pitch.md)。

## 项目结构

```
vocal2midi/
├── src/vocal2midi/          # 应用包（v2.0.0 起的目录结构）
│   ├── __main__.py          # 由 vocal2midi.exe 调起的入口
│   ├── app.py               # 启动编排：环境准备 → Qt 界面
│   ├── paths.py             # 所有路径决策集中在这里（安装态/开发态都适用）
│   ├── assets/              # 程序图标（ico + 三种尺寸 png）
│   ├── core/                # 业务层，不 import PySide6，可单独测试
│   │   ├── params.py        # 参数注册表：11 个可调参数 + 自定义文本解析
│   │   ├── separation.py    # 分离层：Demucs 混音 → 人声 wav
│   │   ├── transcription.py # 转写层：人声 wav → 音符 → MIDI / CSV（SwiftF0）
│   │   ├── pipeline.py      # 单首曲目的两阶段流水线：进度 / 日志 / 取消
│   │   ├── queue.py         # 队列：曲目、状态、增删排序、入队校验
│   │   ├── batch.py         # 批次执行器：按队列顺序跑完一批，逐首上报
│   │   ├── names.py         # 产物命名：wav / mid / csv 三处共用一套规则
│   │   ├── cancellation.py  # 取消信号（分离层、流水线、批次共用）
│   │   ├── net.py           # 多连接断点续传下载器
│   │   ├── mirrors.py       # 国内镜像测速与排序
│   │   ├── installer.py     # 依赖安装引擎（setup.exe 也调用它）
│   │   ├── weights.py       # demucs 权重按需下载
│   │   ├── environment.py   # 环境自检报告
│   │   └── settings.py      # 用户配置读写（含队列与每首的状态）
│   └── ui/                  # 界面层，只调用 core.*
│       ├── main_window.py   # 主窗口
│       ├── queue_panel.py   # 队列列表、增删、上移下移、取件
│       ├── batch_worker.py  # 把批次执行器放到 QThread 上跑
│       ├── deps_dialog.py   # 依赖安装进度对话框
│       ├── theme.py         # 深/浅/跟随系统主题引擎
│       └── widgets.py       # 卡片、参数行（滑动条 + 输入框联动）
├── tests/                   # pytest：队列、批次执行器、分离层、流水线
│   ├── conftest.py          # 自建临时目录（本机 0o700 目录不可访问，见文件注释）
│   └── test_*.py
├── docs/
│   ├── pipeline.svg         # 管线原理图（本文开头引用）
│   ├── swiftf0-vs-basic-pitch.md      # 转写引擎选型实测报告
│   ├── swiftf0_vs_basicpitch_*.png    # 报告里的两张图
│   ├── adr/                 # 架构决策记录（为什么这样设计）
│   └── release-notes/       # 各版本 GitHub Release 正文草稿
├── pytest.ini               # 测试配置（testpaths、pythonpath=src）
├── requirements.txt         # 用户侧运行时清单，安装器据此下载依赖
├── requirements-dev.txt     # 开发/测试额外依赖（pytest），不进运行时清单
├── GLOSSARY.md              # 领域术语表：曲目、批次、队列、产物主名
├── CHANGELOG.md
└── LICENSE
```

跑完的产物在 `.gitignore` 覆盖的 `out/<时间戳>/` 下，不在这里列。

以下目录**不纳入版本库**（已写进 `.gitignore`）：

| 目录 | 内容 |
|---|---|
| `.venv/` | 开发机虚拟环境，约 5.5 GB；测试命令写死了 `.venv/Scripts/python.exe`，不要挪 |
| `.cache/` | 构建时下载的 python embed zip 等临时文件 |
| `build/` | `installer/build_installer.ps1` 的输出：payload 与 setup.exe |
| `models/` | htdemucs_ft 权重（401 MB），开发机上也不需要放在仓库里 |
| `out/` | 跑出来的产物，随时可删 |
| `installer/` | 安装包构建脚本与 C# 启动器，本仓库只收录源码 |
| `tools/` | `make_icon.py`：生成程序图标 |
| `.scratch/` | 本地 issue tracker（spec 与工单）与一次性脚本 |
| `.dsh-progress/` | 长任务的进度面板数据 |
| `.worktrees/` | 并行开发的 git worktree 挂载点 |
| `AGENTS.md`、`docs/agents/` | 本机 agent 协作约定，每台机器各自生成 |

### 分层约定

- 界面层只调用 `core.batch.run_batch()`，业务层通过回调上报
  「进度 / 日志 / 曲目状态 / 取消」，界面逻辑与业务逻辑彻底解耦。
- 批次执行器有两个业务依赖（分离、转写），测试时换成内存假实现就能整台跑起来，
  不需要真的加载模型。
- 换转写引擎只改 `core/transcription.py` 内部，保持 `transcribe_file()` 的签名与
  事件格式 `(start_s, end_s, pitch_midi, amplitude, [bend…])` 不变，界面一行都不用改。
- 所有路径决策集中在 `paths.py`：同一份代码在「开发态的 src/ 布局」和
  「安装态的 runtime/ + app/ 布局」下都能找到正确位置。

### 跑测试

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
```

全套测试不加载 torch / demucs、不碰 GPU，一秒内跑完；每条命令都能在
「进度 / 日志 / 状态」上断言，不必真的跑一首歌。`pytest` 只进
`requirements-dev.txt`，不污染用户侧的 `requirements.txt`。

## 环境要求

- 安装包**不需要预装 Python 和 ffmpeg**，一切随包/自动下载
- 可选：NVIDIA GPU + CUDA 版 PyTorch（分离从数分钟缩短到数十秒）
- 磁盘：安装约 30 MB；依赖 CPU 版约 1.2 GB，GPU 版约 3.8 GB；模型权重 401 MB 按需下载

## 安装

从 [Releases](https://github.com/zarakane1337-arch/vocal2midi/releases/latest) 下载最新版的
`vocal2midi-<版本>-setup.exe` 双击安装即可（文件名里的版本号就是安装包内程序的版本号）。
**安装包只有 10.8 MB**，因为它不再打包 torch / demucs / PySide6 这些库，只装程序本体：

| 内容 | 体积 |
|---|---|
| Python 3.12 embed 运行时（含标准库） | 21.5 MB |
| 应用本体（含全部源码、图标） | 0.22 MB |
| 启动器 `vocal2midi.exe` | 52 KB |
| 文档 | 28 KB |
| payload 合计 | 21.75 MB |
| **安装包压缩后** | **10.8 MB** |

依赖在安装时自动处理：

1. 安装器**实测国内镜像的真实下载速度**（不是只 ping 延迟），排出快慢顺序
2. 从最快的镜像下载全部依赖；失败会自动换下一个镜像（共尝试前 3 名）
3. 自动识别显卡：有 NVIDIA 显卡就装 CUDA 版 torch（约 2.7 GB），否则装 CPU 版（约 105 MB）
4. 全部装完做一次导入自检，通过后写标记文件，之后启动不再检查

安装向导支持**深色/浅色自动跟随系统**。安装完成后开始菜单有「vocal2midi」；
也可以直接运行安装目录下的 `vocal2midi.exe`。

> 依赖装到一半可以取消；下次运行 `vocal2midi.exe --repair` 会接着装。

## 快速开始

双击桌面或开始菜单里的 **vocal2midi** 即可打开原生窗口（无需浏览器）：

1. 把歌曲拖进窗口（也可以点「添加歌曲…」多选，mp3 / wav / flac 都行）——可以一次给多首，
   它们按顺序排进队列；如果文件已是干声人声，先在「人声分离」页勾选「已是人声轨（跳过分离）」
2. 点「开始转换」，整批按队列顺序逐首跑，进度条显示整体进度与
   `第 3/8 首 · 分离中` 这样的文字；某一首失败不会弄停其余
3. 每跑完一首，队列里那一行就从「待转换」变成「完成」，双击能直接打开它的产物
4. 整批结束会给出汇总：成功几首、失败几首、失败的分别是什么原因
5. 关闭窗口即退出程序；下次打开时队列、顺序与每首的状态都还在，
   已完成的不会重跑，只需再点一次「开始转换」处理剩下的

**界面分为一个队列卡片和三个标签页：**

| 区域 | 内容 |
|---|---|
| 歌曲队列 | 曲目列表（曲名 + 状态），添加 / 删除 / 清空 / 上移 / 下移 / 打开批次目录 |
| 人声分离 | 分离模型、计算设备、输出目录、运行选项 |
| 转写参数 | 11 个参数，每个都是「滑动条 + 可编辑输入框」联动 |
| 自定义参数 | 自由文本，批量覆写参数（见下） |

## 队列

队列是长期存在的对象，批次只是它的一次执行。

- **加歌**：点「添加歌曲…」多选，或直接把文件拖到窗口任意位置（一次拖多个也会全部进队）。
  跑批期间也可以继续拖——新歌排到队尾，等下一批才跑。
- **排序**：选中一行用「上移」/「下移」。顺序会被保存，重启后仍是调整后的顺序。
- **删除**：选中（可多选）后点「删除」，或「清空」整队。
- **取件**：「打开批次目录」一次处理整批产物；也可以双击某一行只打开它自己的产物。
- **状态**：`· 待转换` → `▶ 转换中` → `✓ 完成`，出错是 `! 失败`（行尾带着原因）。
  双击已完成的行可以直接打开它的产物，右键菜单里也有。
- **再点一次「开始转换」**：只跑「待转换」与「失败」的曲目，已完成的直接跳过；
  如果全都完成了，会提示「没有待转换的曲目」。
- **重试**：失败的曲目重跑时会回到它自己那个批次目录。如果上次已经分离出了人声轨，
  就只重跑转写——分离是整条流水线里最贵的一步，能省则省。
- **取消**：点「取消」后，正在分离的那一首会在当前分段结束时停下，退回「待转换」；
  它不会算作失败，也不弹错误框。已完成的曲目保持不动，后面的仍是「待转换」。
- **参数**：一次批次的曲目共用同一套参数与输出目录；跑批期间这些控件会锁住，
  免得中途改变已跑与没跑曲目的结果。

## 界面与参数

**主题**：标题栏右侧的下拉框可选「跟随系统 / 浅色 / 深色」。选择「跟随系统」时，
程序会监听 Windows 的浅色/深色切换并实时跟着变。

**转写参数**：每个参数都有一个滑动条和一个输入框，两者实时联动 ——
拖动滑块时输入框同步更新，也可以在输入框里直接键入精确数值（回车或失去焦点生效）。
超出范围会自动夹到边界并标红提示。

| 参数 | 范围 | 默认 | 说明 |
|---|---|---|---|
| `onset_threshold` 音符切分灵敏度 | 0.3 – 0.9 | 0.5 | 调高 → 音符更长更少，抑制碎音 |
| `frame_threshold` 发音置信度阈值 | 0.05 – 0.95 | 0.3 | 低于该置信度的音符被丢弃 |
| `min_note_length_ms` 最短音符时长 | 10 – 400 | 50 | 毫秒 |
| `f0_min` / `f0_max` 基频范围 | 40 – 400 / 200 – 2000 | 70 / 900 | Hz，超出人声音域容易出鬼音 |
| `pitch_hold_ms` 音高保持时长 | 0 – 200 | 0 | 0 = 按灵敏度自动推导 |
| `velocity_floor` 力度下限 | 0 – 0.6 | 0 | 避免弱唱音符力度过低 |
| `bend_range` 弯音范围 | 0 – 24 | 2 | 半音数，0 = 不写 pitch bend |
| `tempo` 节拍速度 | 40 – 240 | 120 | BPM，写入 MIDI 的 set_tempo |
| `quantize` 量化精度 | off / 1/4 / 1/8 / 1/16 / 1/32 | off | 对齐到网格 |
| `transpose` 移调 | -24 – 24 | 0 | 半音 |

**自定义参数**：在「自定义参数」页里可以一次写多个参数，支持四种写法
（空格、逗号、分号、换行都能分隔）：

```
onset_threshold=0.45 min_note_length_ms=60 quantize=1/16 transpose=-2
--f0_min 55, --f0_max 1200
tempo: 96
bend_range 4
```

输入时会实时解析并显示识别结果；写错的键名会单独提示，不会影响其它参数生效。
自定义参数优先级高于滑动条。

## 输出说明

所有产物输出至 `out/` 目录（安装版为安装目录下的 `out/`），**每一批开一个新的时间戳子目录**：

```
out/
├── 20251020-214530/          # 一次「开始转换」= 一个批次目录
│   ├── 歌名_vocals.wav
│   ├── 歌名_vocals.mid
│   └── 歌名_vocals_notes.csv
└── 20251021-091502/
    └── …
```

哪怕这一批只有一首歌，也会单独建一个子目录。这样两批里的同名歌曲永远不会读到对方的
人声轨，也不会互相覆盖产物。

| 文件 | 说明 |
|---|---|
| `<歌名>_vocals.wav` | Demucs 分离出的人声轨（重试时自动复用，不重算） |
| `<歌名>_vocals.mid` | 最终人声 MIDI，含 pitch bend，可直接拖入 DAW |
| `<歌名>_vocals_notes.csv` | 音符事件明细（起始 / 结束 / 时长 / 音高 / 幅度 / 弯音），可用 Excel 打开核对 |

同一批里出现两首同名歌曲（比如两张专辑里的 `song.mp3`）时，后一首的产物主名会
追加序号（`song-2`），两份产物都完整保留、互不覆盖。产物主名在曲目首次进入批次时确定
并记在曲目上，所以重试仍会落回同一个主名，不会把自己的旧产物变成孤儿文件。

同名 wav 已存在时会被复用，换参数要重算时在界面上勾选覆盖，或先删掉对应的 wav。

> **从 2.x 升级**：以前的产物平铺在 `out/` 下，升级后不会自动迁移，也不会被删掉。
> 需要旧文件就自己挪走；新版本跑出来的东西一律在 `out/<时间戳>/` 下。

## 参数参考

### 参数优先级

`默认值 < 滑动条/输入框 < 自定义参数文本`

### 调优指南

| 症状 | 建议调整 |
|---|---|
| 短碎音、鬼音较多 | 切分灵敏度 `0.7~0.9`（更保守），最短音符 `100~150` ms |
| 音符拖尾、该断不断 | 切分灵敏度 `0.3~0.4`（切得更细） |
| 出现八度/五度错音 | 收紧最低/最高音高至实际人声音域 |
| 弱唱、气声音符漏检 | 置信度阈值 `0.2`，并适当提高力度下限 |
| 音符对不上节拍 | `quantize` 设为 `1/16`，并把 `tempo` 设成歌曲实际 BPM |
| 伴奏残留过多 | 确认使用 `htdemucs_ft`，或改用 `mdx_extra_q` 对比 |

## 常见问题

| 问题 | 原因与解决 |
|---|---|
| `ffmpeg not found` 或 mp3 解码失败 | 安装版自带 `runtime/bin/ffmpeg.exe`，不应出现；若被清理可运行 `vocal2midi.exe --repair` 补回 |
| 首次启动在测速/安装 | 正常：程序在实测国内镜像速度并下载依赖（GPU 版 torch 约 2.7 GB） |
| 安装依赖失败 | 运行 `vocal2midi.exe --repair` 重新测速并续装 |
| 分离非常慢 | 用的是 CPU 版 torch；`--repair` 时选 `cu128`，或在界面里把设备设成 `cuda` |
| 安装 CUDA 版后仍显示 CPU | 确认显卡驱动支持 CUDA 12.8 以上（RTX 50 系必须）；旧驱动请升级 |
| 卡在「加载模型」很久 | 首次运行要下载 401 MB 权重；国内直连官方源很慢，程序会优先走 HF 镜像 |
| 提示未安装 swift-f0 | 运行 `vocal2midi.exe --repair` 补装依赖 |
| `No module named 'PySide6'` | 运行 `vocal2midi.exe --repair` 补装依赖 |
| 模型权重占空间 | 缓存在 `%LOCALAPPDATA%\vocal2midi\cache\torch`，可随时删除，下次运行会重新下载 |
| 未检出任何音符 | 调低切分/帧判定阈值；确认音高范围覆盖实际音域 |
| 转出结果有不少错音 | 属预期，见「效果预期」 |

### Python 3.13 说明

改用 SwiftF0 后不再有 TensorFlow 依赖。SwiftF0 要求 Python ≥ 3.8，纯 onnxruntime + numpy。
安装包已内置 Python 3.12 运行时，不受本机 Python 版本影响。

## 效果预期

- SwiftF0 是单声部 F0 跟踪器，正好匹配"Demucs 已把伴奏剥掉"的前提；它的输出是连续音高，写进 MIDI 时通过 pitchwheel 保留颤音与滑音。
- 实测（210 秒日文歌曲人声轨）：回放 MIDI 对 pYIN 参照的中位误差 9.2¢、91% 的音符在 ±50¢ 内、八度错误 0.75%。
- 人声转谱本身不存在唯一正确答案。Spotify 的 vocadito 数据集实验显示，两位专业乐手独立标注同一段人声，互相一致性 F-measure 仅 0.64。因此本类工具的准确率上限约 70~80%。
- 默认不做时间量化（保留演唱的真实节奏）；需要对齐时把「量化精度」设为 `1/16` 之类即可。

## 更新日志

见 [`CHANGELOG.md`](CHANGELOG.md)。

版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)，标签写作 `V主.次.补丁`（大写 V）：

| 位 | 什么时候加 | 判断标准 |
|---|---|---|
| 主版本 | 不兼容变更 | 用户必须做点什么才能继续用：安装方式/目录结构变了、参数语义变了、输出格式变了、入口被移除 |
| 次版本 | 向后兼容的新增 | 不改变已有用法就能升级：新参数、新功能、换引擎但接口与事件格式不变、质量与性能提升 |
| 补丁号 | 仅修复 | 同一输入得到的结果符合原有预期，只是修好了不该出问题的地方 |

口诀：**不需要任何用户动作就能升级 → 至少次版本；需要改操作、重装或改参数值 → 主版本；
同一输入的结果变了（哪怕更好）→ 至少次版本，不算补丁。**

版本号的唯一真源是 `src/vocal2midi/__init__.py` 里的 `__version__`。
`installer/build_installer.ps1` 从那里读取，并传给安装包（`/DAppVersion`）
和启动器（自动生成的汇编特性文件），三处不会各写各的。

### 发版检查表

1. 改 `src/vocal2midi/__init__.py` 的 `__version__`，按上表判断加哪一位。
2. 在 `CHANGELOG.md` 顶部把「未发布」改成 `## [x.y.z] - YYYY-MM-DD`，
   并在文件末尾的链接块补一行 `[x.y.z]: …/releases/tag/Vx.y.z`，
   同时把 `[未发布]` 的 compare 起点改成新标签。
3. 跑一遍 `.venv\Scripts\python.exe -m pytest`，再跑 `installer\build_installer.ps1`
   出安装包。构建脚本会自己核对版本资源，版本号对不上会直接失败，
   不会产出一个版本号是假的 exe。
4. 打标签并推送：`git tag Vx.y.z && git push origin Vx.y.z`（大写 V，与已有标签一致）。
5. 在 GitHub 上以该标签建 Release，正文贴 CHANGELOG 里对应的一节，
   上传 `vocal2midi-x.y.z-setup.exe`，并附上 `Get-FileHash` 算出的 SHA256。
6. 校验安装包版本：右键属性 →「详细信息」，文件版本与产品版本都应是 x.y.z；
   命令行可跑 `vocal2midi.exe --version`（输出 `vocal2midi x.y.z`）。

## 许可证

本项目代码按 MIT 许可证发布。所用第三方组件的许可证如下：

| 组件 | 许可证 |
|---|---|
| Demucs | MIT |
| SwiftF0 | MIT |
| PySide6 | LGPL-3.0 |
| PyTorch | BSD-3-Clause |
