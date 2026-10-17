# 更新日志

本项目的所有重要变更都记录在此文件。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 计划中

- 批量处理：一次导入多首歌
- 导出时可选的时间量化（对齐到 1/16 音符）

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
