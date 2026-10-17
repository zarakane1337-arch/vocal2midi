"""vocal2midi —— 人声分离与 MIDI 转写工具。

分层结构（自下而上，互不越界）：

    core.params        参数注册表（UI 滑动条 / 输入框 / 自定义参数共用同一份定义）
    core.separation    Demucs 人声分离
    core.transcription SwiftF0 人声 → MIDI
    core.pipeline      串联两阶段，暴露进度回调
    core.net           多连接断点续传下载器
    core.mirrors       国内镜像测速与选源
    core.installer     依赖解析、下载、安装（setup.exe 与应用自检共用）
    core.weights       分离模型权重下载
    core.environment   运行环境探测（ffmpeg / 依赖就绪标记）
    ui.*               PySide6 界面（主题、控件、主窗口、依赖对话框）
    app                启动编排，由安装目录下的 vocal2midi.exe 调起
"""

__all__ = ["__version__", "APP_NAME", "APP_TITLE"]

__version__ = "2.0.0"
APP_NAME = "vocal2midi"
APP_TITLE = "vocal2midi · 人声转 MIDI"
APP_ID = "8F3A2C71-4D6B-4E19-9A87-2C5E1F0B3D64"
