#!/usr/bin/env python
"""vocal2midi UI —— Gradio 网页界面。

启动：
    python app.py
然后浏览器打开 http://127.0.0.1:7860（默认自动弹出）。

页面关闭后自动释放端口：
    浏览器和服务端之间没有“标签页已关闭”的原生通知，因此采用两层机制——
    1) 页面每 5 秒向 /vocal2midi/heartbeat 打一次心跳；
    2) 关闭/刷新页面时用 sendBeacon 向 /vocal2midi/bye 发一次告别。
    服务端看门狗线程发现“曾有过页面、但已连续 N 秒没心跳、且当前没有转换任务在跑”，
    就自动退出进程、释放端口。N 由 --idle-timeout 控制（0 = 关闭该功能）。

依赖：pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import gradio as gr
except ModuleNotFoundError as _exc:
    sys.stderr.write(
        f"\n[错误] 当前解释器缺少依赖：{_exc.name}\n"
        f"       解释器路径：{sys.executable}\n\n"
        f"原因：python 在不同终端里可能指向不同解释器，依赖不会自动共享。\n"
        f"解决（任选其一）：\n"
        f"  1) 用同一解释器安装依赖（首次约 2~3 GB）：\n"
        f'     "{sys.executable}" -m pip install -r requirements.txt\n'
        f"  2) 换成已经装好依赖的那个解释器启动，例如：\n"
        f'     "路径\\到\\python.exe" src\\app.py\n'
        f"  3) 为项目建独立环境（推荐）：\n"
        f"     py install 3.12                              # 安装官方 Python 3.12\n"
        f"     py -3.12 -m venv .venv                       # 建虚拟环境\n"
        f"     .venv\\Scripts\\python.exe -m pip install -r requirements.txt\n"
        f"     之后 VS Code 选择 .venv 解释器，双击 start.bat 也会自动用它。\n\n"
    )
    raise SystemExit(1) from None

from main import (
    DEMUCS_MODEL,
    resolve_device,
    separate_vocals,
    transcribe_to_midi,
    write_notes_csv,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("vocal2midi.ui")

OUT_DIR = Path(__file__).resolve().parent.parent / "out"

DEFAULT_PORT = 7860
DEFAULT_IDLE_TIMEOUT = 60.0  # 关闭页面后多少秒自动退出（0 = 禁用）

# ── 空闲自动退出状态 ────────────────────────────────────────────────
_state_lock = threading.Lock()
_last_seen = 0.0
_busy = 0

HEARTBEAT_JS = """
() => {
  const beat = () => fetch('/vocal2midi/heartbeat', {cache: 'no-store'}).catch(() => {});
  beat();
  setInterval(beat, 5000);
  document.addEventListener('visibilitychange', beat);
  const bye = () => navigator.sendBeacon('/vocal2midi/bye');
  window.addEventListener('pagehide', bye);
  window.addEventListener('beforeunload', bye);
}
"""


def register_idle_routes(app, idle_timeout: float) -> None:
    """把心跳与告别接口挂到外层 FastAPI 上。

    注意：不能挂在 demo.app 上 —— Gradio 的 launch() 会重建应用、丢掉后加的路由，
    所以这里配合 mount_gradio_app 自建服务器（见 main）。
    """

    @app.get("/vocal2midi/heartbeat")
    def _heartbeat():
        global _last_seen
        with _state_lock:
            _last_seen = time.time()
        return {"ok": True}

    @app.post("/vocal2midi/bye")
    def _bye():
        global _last_seen
        with _state_lock:
            _last_seen = time.time() - max(0.0, idle_timeout - 5.0)
        return {"ok": True}


def _start_watchdog(idle_timeout: float, port: int) -> None:
    """看门狗：页面都走了且没有任务在跑时，退出进程以释放端口。"""

    def loop() -> None:
        while True:
            time.sleep(2.0)
            if idle_timeout <= 0:
                continue
            with _state_lock:
                last, busy = _last_seen, _busy
            if last <= 0:
                continue
            if busy > 0:
                continue
            idle = time.time() - last
            if idle > idle_timeout:
                log.info("页面已离开 %.0f 秒且无任务在跑，自动退出并释放端口 %d", idle, port)
                time.sleep(0.5)
                os._exit(0)

    threading.Thread(target=loop, name="idle-watchdog", daemon=True).start()


def progress_bar(pct: float, label: str) -> str:
    """主界面常驻进度条（HTML），pct 取 0~100。"""
    return (
        f'<div style="font-size:14px;margin-bottom:6px;color:#111">{label}</div>'
        f'<div style="background:#e5e7eb;border-radius:6px;height:16px;overflow:hidden">'
        f'<div style="background:#3b82f6;height:100%;width:{pct:.0f}%;'
        f'transition:width .4s ease"></div></div>'
        f'<div style="font-size:12px;color:#6b7280;text-align:right;margin-top:2px">{pct:.0f}%</div>'
    )


def run(
    audio_file: str | None,
    model: str,
    device: str,
    is_vocals: bool,
    overwrite: bool,
    onset: float,
    frame: float,
    min_note_len: float,
    f0_min: float,
    f0_max: float,
):
    """管线编排（生成器）：主界面进度条 + 状态 + 人声试听 + MIDI/CSV 下载。"""
    started = time.time()

    def elapsed() -> str:
        return f"已用时 {time.time() - started:.0f} 秒"

    if not audio_file:
        raise gr.Error("请先上传音频文件（mp3 / wav / flac…）")

    global _busy
    with _state_lock:
        _busy += 1  # 任务期间看门狗不退出，避免长时间分离被误杀
    try:
        src = Path(audio_file)
        stem = src.stem.replace("_vocals", "") if is_vocals else src.stem
        real_device = resolve_device(device)

        yield progress_bar(5, "检查输入…"), elapsed(), None, None, None

        if is_vocals:
            vocals = src
        else:
            yield (
                progress_bar(12, f"Demucs 分离人声中（{model} / {real_device}）——CPU 上需数分钟，请耐心等待"),
                elapsed() + " · 这是整个流程最慢的一步",
                None,
                None,
                None,
            )
            try:
                vocals = separate_vocals(
                    src, OUT_DIR / "stems", model=model, device=real_device, overwrite=overwrite
                )
            except (ImportError, KeyError, RuntimeError, ValueError) as exc:
                raise gr.Error(f"人声分离失败：{exc}") from exc

        yield progress_bar(60, "basic-pitch 转写中…"), elapsed(), None, None, None
        try:
            midi_path, events = transcribe_to_midi(
                vocals,
                OUT_DIR / f"{stem}_vocals.mid",
                onset_threshold=onset,
                frame_threshold=frame,
                min_note_length_ms=min_note_len,
                f0_min=f0_min or None,
                f0_max=f0_max or None,
            )
        except ImportError as exc:
            raise gr.Error(f"未安装 basic-pitch：{exc}") from exc

        yield progress_bar(90, "导出音符表…"), elapsed(), None, None, None
        csv_path = write_notes_csv(events, OUT_DIR / f"{stem}_vocals_notes.csv")

        status = (
            f"完成，共 {len(events)} 个音符，总用时 {time.time() - started:.0f} 秒（设备 {real_device}）\n"
            f"人声轨已缓存于 {vocals}，下次同一文件会自动复用。\n"
            "如碎音/鬼音偏多，把「起始阈值」调到 0.6~0.8、最短音符调到 150ms 再试。"
        )
        yield progress_bar(100, "完成 ✓"), status, str(vocals), str(midi_path), str(csv_path)
    finally:
        with _state_lock:
            _busy -= 1


def quit_app() -> str:
    """退出程序（延迟一下让本次响应先发出去，否则页面看不到提示）。"""
    threading.Timer(1.0, lambda: os._exit(0)).start()
    return "程序正在退出，端口即将释放，现在可以关闭本页面。"


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="vocal2midi · 人声转 MIDI") as demo:
        gr.Markdown(
            "# vocal2midi\n"
            "上传一首歌 → Demucs 分离人声 → basic-pitch 转写 → 下载人声 MIDI。\n"
            "CPU 上一首 4 分钟的歌约需数分钟；分离结果会缓存，同一文件重复转换无需重跑。\n"
            "关闭浏览器页面后，程序会在空闲一段时间后自动退出并释放端口。"
        )

        with gr.Row():
            with gr.Column(scale=1):
                audio_in = gr.Audio(sources=["upload"], type="filepath", label="输入歌曲")
                is_vocals = gr.Checkbox(
                    value=False,
                    label="上传的已是人声轨（跳过 Demucs 分离）",
                )
                model_in = gr.Dropdown(
                    choices=["htdemucs_ft", "htdemucs", "mdx_extra_q"],
                    value=DEMUCS_MODEL,
                    label="Demucs 模型",
                )
                device_in = gr.Dropdown(choices=["auto", "cpu", "cuda"], value="auto", label="计算设备")
                overwrite = gr.Checkbox(value=False, label="强制重新分离（忽略缓存）")
                with gr.Row():
                    btn = gr.Button("开始转换", variant="primary")
                    quit_btn = gr.Button("退出程序", variant="secondary")

            with gr.Column(scale=1):
                bar = gr.HTML(progress_bar(0, "等待开始"))
                status = gr.Textbox(label="状态", lines=3, interactive=False)
                vocals_out = gr.Audio(label="人声轨试听", type="filepath")
                midi_out = gr.File(label="MIDI 文件下载")
                csv_out = gr.File(label="音符明细 CSV")

        with gr.Accordion("高级参数（转写效果不理想时再调）", open=False):
            with gr.Row():
                onset = gr.Slider(0.3, 0.9, value=0.5, step=0.05, label="起始阈值（调高减少碎音/鬼音）")
                frame = gr.Slider(0.1, 0.6, value=0.3, step=0.05, label="持续阈值（调高音符更短更少）")
            with gr.Row():
                min_note_len = gr.Slider(50, 300, value=100, step=10, label="最短音符时长（毫秒）")
                f0_min = gr.Slider(0, 200, value=70, step=10, label="最低基频 Hz（0 = 不限制）")
                f0_max = gr.Slider(200, 2000, value=900, step=50, label="最高基频 Hz")

        btn.click(
            run,
            inputs=[audio_in, model_in, device_in, is_vocals, overwrite, onset, frame, min_note_len, f0_min, f0_max],
            outputs=[bar, status, vocals_out, midi_out, csv_out],
        )
        quit_btn.click(quit_app, outputs=[status])

        # 页面加载时注入心跳脚本，用于判断页面是否还在
        try:
            demo.load(None, None, None, js=HEARTBEAT_JS)
        except Exception as exc:  # noqa: BLE001 - 注入失败时只是无法自动退出
            log.warning("心跳脚本注入失败（%s），空闲自动退出不可用", exc)

    return demo


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vocal2midi-ui",
        description="vocal2midi 网页界面",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口")
    p.add_argument(
        "--idle-timeout",
        type=float,
        default=DEFAULT_IDLE_TIMEOUT,
        help="页面关闭后多少秒自动退出并释放端口，0 表示不自动退出",
    )
    p.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    return p


def main(argv: list[str] | None = None) -> int:
    import webbrowser

    import uvicorn
    from fastapi import FastAPI

    try:
        from gradio.routes import mount_gradio_app
    except ImportError:  # 旧版 Gradio 的导出位置
        mount_gradio_app = gr.mount_gradio_app  # type: ignore[attr-defined]

    args = build_parser().parse_args(argv)
    OUT_DIR.mkdir(exist_ok=True)

    demo = build_ui().queue()

    # 自建外层应用并挂载 Gradio：这样自定义路由不会在 launch() 重建应用时丢失
    app = FastAPI()
    if args.idle_timeout > 0:
        register_idle_routes(app, args.idle_timeout)
        _start_watchdog(args.idle_timeout, args.port)
        log.info("空闲自动退出已启用：页面离开 %.0f 秒后自动释放端口", args.idle_timeout)
    else:
        log.info("空闲自动退出已禁用（--idle-timeout 0）")
    app = mount_gradio_app(app, demo, path="/")

    url = f"http://127.0.0.1:{args.port}"
    log.info("界面地址：%s", url)
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
