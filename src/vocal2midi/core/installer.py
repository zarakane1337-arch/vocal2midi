"""依赖安装引擎（setup.exe 与「修复依赖」共用）。

流程：
    1. 测速选出最快的国内镜像（真实吞吐，见 core.mirrors）
    2. 检测 NVIDIA 显卡决定 torch 变体（cpu / cu128）
    3. 用多连接下载器从 PyTorch 镜像预先取回 torch / torchaudio 大轮子
       （它们不在 PyPI 简单索引里，必须单独取）
    4. 用 pip 的 `--dry-run --report` 解析出完整依赖闭包与精确 wheel URL
    5. 用 core.net 的多连接下载器把 wheel 并发拉到本地缓存（比 pip 单流快数倍）
    6. `pip install --no-index --find-links <缓存>` 离线装配
    7. 准备 ffmpeg（优先取 imageio-ffmpeg 轮子里的二进制）
    8. 全量导入自检，写 marker

任何一步失败都会切换到次快镜像重试；进度与日志通过回调 + 进度文件双通道输出，
后者供安装器轮询显示。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote

from .. import paths
from . import mirrors, net

log = logging.getLogger("vocal2midi.installer")

# ---------------------------------------------------------------------------
# 依赖清单
# ---------------------------------------------------------------------------

TORCH_VERSION = "2.9.1"
TORCHAUDIO_VERSION = "2.9.1"
PYTORCH_FLAVORS = ("cpu", "cu128")

# torch / torchaudio 的 +cpu / +cu128 轮子不在任何 PyPI 简单索引里（PyPI 上只有
# 不带本地版本号的 2.9.1），只存在于 PyTorch 官方下载站及其国内镜像。
# 实测阿里云是唯一提供「扁平目录列表」且速度可用的国内镜像（上交、华为的
# /pytorch-wheels/ 目录页是 JS 界面/包名索引，不能当 --find-links 用）。
PYTORCH_WHEEL_BASE = "https://mirrors.aliyun.com/pytorch-wheels"

# pip 单流拉 194MB / 2.7GB 的大轮子很慢，这里用多连接下载器直取，
# 再以本地 --find-links 喂给 pip，避免 pip 为了读元数据把 2.7GB 重下一遍。
TORCH_WHEEL_CONNECTIONS = 8


def _core_specs(flavor: str) -> list[str]:
    suffix = f"+{flavor}"
    return [
        f"torch=={TORCH_VERSION}{suffix}",
        f"torchaudio=={TORCHAUDIO_VERSION}{suffix}",
        "demucs==4.1.0",
        "swift-f0[audio,midi]==0.3.0",
        "onnxruntime>=1.19",
        "soundfile",
        "soxr",
        "mido",
        "PySide6-Essentials==6.9.3",
        "imageio-ffmpeg==0.6.0",
    ]


# 自检用的导入名 → 友好名
IMPORT_CHECKS: tuple[tuple[str, str], ...] = (
    ("torch", "PyTorch"),
    ("torchaudio", "TorchAudio"),
    ("demucs.api", "Demucs"),
    ("swift_f0", "SwiftF0"),
    ("onnxruntime", "ONNX Runtime"),
    ("soundfile", "soundfile"),
    ("soxr", "soxr"),
    ("mido", "mido"),
    ("numpy", "NumPy"),
    ("PySide6.QtWidgets", "PySide6"),
)

FFMPEG_WHEEL_GLOB = "ffmpeg-win64-*.exe"


# ---------------------------------------------------------------------------
# 进度总线
# ---------------------------------------------------------------------------


@dataclass
class Progress:
    """安装进度。既回调给界面，也落盘给安装器轮询。"""

    progress_file: Path | None = None
    on_progress: Callable[[float, str], None] | None = None
    on_log: Callable[[str], None] | None = None
    log_file: Path | None = None
    _started: float = field(default_factory=time.time)

    def log(self, message: str) -> None:
        log.info(message)
        if self.on_log:
            self.on_log(message)
        self._append_log(message)
        self._write(state="running", message=message)

    def _append_log(self, message: str) -> None:
        """把日志逐行追加到文件，供安装界面实时滚动显示。"""
        if not self.log_file:
            return
        try:
            stamp = time.strftime("%H:%M:%S")
            with self.log_file.open("a", encoding="utf-8") as fh:
                fh.write(f"[{stamp}] {message}\n")
        except OSError:
            pass

    def update(self, fraction: float, message: str) -> None:
        fraction = max(0.0, min(1.0, float(fraction)))
        if self.on_progress:
            self.on_progress(fraction, message)
        self._write(state="running", percent=round(fraction * 100, 1), message=message)

    def finish(self, message: str, ok: bool = True) -> None:
        if self.on_progress:
            self.on_progress(1.0 if ok else 0.0, message)
        self._append_log(("完成：" if ok else "失败：") + message)
        self._write(state="done" if ok else "failed", percent=100 if ok else 0, message=message)

    def _write(self, state: str, percent: float | None = None, message: str = "") -> None:
        if not self.progress_file:
            return
        payload = {
            "state": state,
            "percent": percent,
            "message": message,
            "elapsed": round(time.time() - self._started, 1),
        }
        try:
            tmp = self.progress_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.progress_file)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 环境探测
# ---------------------------------------------------------------------------


def has_nvidia_gpu() -> tuple[bool, str]:
    """用 nvidia-smi 判断是否有可用 NVIDIA 显卡，返回 (是否可用, 显卡名)。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        candidates = [
            Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe",
        ]
        exe = next((str(c) for c in candidates if c.exists()), None)
    if not exe:
        return False, ""
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    name = (out.stdout or "").strip().splitlines()[0].strip() if (out.stdout or "").strip() else ""
    return out.returncode == 0 and bool(name), name


def choose_torch_flavor(requested: str = "auto") -> tuple[str, str]:
    """返回 (flavor, 说明)。requested 为 auto/cpu/cu128。"""
    requested = (requested or "auto").strip().lower()
    if requested in PYTORCH_FLAVORS:
        return requested, f"已指定 {requested} 版本"
    ok, name = has_nvidia_gpu()
    if ok:
        return "cu128", f"检测到 NVIDIA 显卡（{name}），安装 CUDA 加速版本"
    return "cpu", "未检测到 NVIDIA 显卡，安装 CPU 版本"


def torch_state() -> dict[str, object]:
    """当前环境里 torch 的安装情况。"""
    state: dict[str, object] = {"installed": False, "version": None, "cuda": None, "gpu": False, "gpu_name": ""}
    try:
        import torch
    except Exception:  # noqa: BLE001
        ok, name = has_nvidia_gpu()
        state["gpu"], state["gpu_name"] = ok, name
        return state
    state["installed"] = True
    state["version"] = getattr(torch, "__version__", None)
    try:
        state["cuda"] = getattr(torch.version, "cuda", None)
        state["gpu"] = bool(torch.cuda.is_available())
        if state["gpu"]:
            state["gpu_name"] = torch.cuda.get_device_name(0)
    except Exception:  # noqa: BLE001
        pass
    return state


def missing_modules() -> list[tuple[str, str]]:
    """返回缺失的 (导入名, 友好名)。"""
    import importlib.util

    missing = []
    for module, label in IMPORT_CHECKS:
        try:
            if importlib.util.find_spec(module) is None:
                missing.append((module, label))
        except (ImportError, ValueError):
            missing.append((module, label))
    return missing


def deps_ready() -> bool:
    """依赖是否齐全（不实际导入，只查 spec + 标记）。"""
    if not paths.deps_marker().exists():
        return False
    return not missing_modules()


def mark_ready(flavor: str = "") -> None:
    marker = paths.deps_marker()
    paths.ensure_dir(marker.parent)
    marker.write_text(
        json.dumps(
            {
                "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "torch_flavor": flavor,
                "python": sys.version.split()[0],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# pip 辅助
# ---------------------------------------------------------------------------


def _pip_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    env.pop("PYTHONHOME", None)
    return env


def _pip_base(python: Path, mirror: mirrors.Mirror, cache_dir: Path, find_links: Iterable[Path] = ()) -> list[str]:
    args = [
        str(python),
        "-m",
        "pip",
        "install",
        "--no-input",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        "--index-url",
        mirror.index_url,
        "--cache-dir",
        str(cache_dir / "pip-cache"),
    ]
    for link in find_links:
        args += ["--find-links", str(link)]
    return args


def ensure_pip(python: Path, mirror: mirrors.Mirror, progress: Progress) -> None:
    """确保解释器里有 pip；没有就从一个 pip 轮子直接解包（免 get-pip.py）。"""
    probe = subprocess.run(
        [str(python), "-m", "pip", "--version"],
        capture_output=True,
        text=True,
        timeout=120,
        env=_pip_env(),
    )
    if probe.returncode == 0:
        progress.log("pip 已就绪：" + (probe.stdout or "").strip().splitlines()[0])
        return
    progress.log("解释器缺少 pip，正在从镜像安装 pip…")
    import re
    import zipfile

    index_url = mirror.index_url.rstrip("/") + "/pip/"
    req = net._request(index_url, method="GET", timeout=25)  # noqa: SLF001 - 同包内复用
    html = req.read().decode("utf-8", "ignore")
    names = re.findall(r'<a href="([^"#]+)[^"]*"[^>]*>([^<]+\.whl)</a>', html)
    wheels = [href for href, name in names if re.match(r"pip-\d[\w.]*-py3-none-any\.whl$", name)]
    if not wheels:
        raise RuntimeError("镜像上没有可用的 pip 轮子")
    url = wheels[-1]
    if not url.startswith("http"):
        # 镜像的轮子链接是相对 /simple/<pkg>/ 的路径，用 urljoin 解析（不要用 normpath，会吃掉 //）
        from urllib.parse import urljoin

        url = urljoin(index_url, url)
    target = cache_dir_download_dir() / url.rsplit("/", 1)[-1]
    net.download(url, target, connections=4)
    site = paths.site_packages()
    paths.ensure_dir(site)
    with zipfile.ZipFile(target) as zf:
        zf.extractall(site)
    progress.log(f"pip 已安装到 {site}")


def _wheel_filename(name: str, version: str, flavor: str) -> str:
    """拼出 cp312/win_amd64 轮子文件名。本地版本号里的 + 在 URL 里要写成 %2B。"""
    import sys as _sys

    tag = f"cp{_sys.version_info.major}{_sys.version_info.minor}"
    local = f"+{flavor}" if flavor else ""
    return f"{name}-{version}{local}-{tag}-{tag}-win_amd64.whl"


def prefetch_torch(
    flavor: str,
    cache_dir: Path,
    progress: Progress,
    on_progress_span: tuple[float, float] = (0.05, 0.2),
) -> Path:
    """把 torch / torchaudio 的大轮子用多连接下载器预先拉到本地目录。

    返回可作为 pip --find-links 的目录。这样 pip 解析时能在本地看到它们，
    不会为了读取 METADATA 而单流重下 194MB / 2.7GB。
    """
    target_dir = paths.ensure_dir(cache_dir / "torch-wheels")
    items = [
        ("torch", TORCH_VERSION, flavor),
        ("torchaudio", TORCHAUDIO_VERSION, flavor),
    ]
    base = f"{PYTORCH_WHEEL_BASE.rstrip('/')}/{flavor}/"
    lo, hi = on_progress_span
    for index, (name, version, local) in enumerate(items):
        filename = _wheel_filename(name, version, local)
        dest = target_dir / filename
        if dest.exists() and dest.stat().st_size > 1048576:
            progress.log(f"已缓存 {filename}")
            continue
        url = base + filename.replace("+", "%2B")
        progress.log(f"下载 {name} {version}+{flavor}（多连接）…")

        def report(done: int, size: int, _name=name, _i=index) -> None:
            span = (hi - lo) / len(items)
            frac = lo + span * _i + span * (done / size if size else 0)
            progress.update(frac, f"下载 {_name}（{done / 1048576:.0f}/{size / 1048576:.0f} MB）")

        try:
            net.download(url, dest, connections=TORCH_WHEEL_CONNECTIONS, on_progress=report, min_size=1048576)
        except net.DownloadError as exc:
            raise RuntimeError(f"torch 轮子下载失败（{flavor}）：{exc}") from exc
    progress.update(hi, "torch 轮子已就绪")
    return target_dir


def cache_dir_download_dir() -> Path:
    path = paths.user_cache_dir() / "wheels"
    paths.ensure_dir(path)
    return path


def resolve_wheels(
    python: Path,
    mirror: mirrors.Mirror,
    flavor: str,
    specs: Iterable[str],
    cache_dir: Path,
    progress: Progress,
    find_links: Iterable[Path] = (),
) -> list[dict]:
    """用 pip 的 --dry-run --report 解析依赖闭包，返回 wheel 清单。"""
    report = cache_dir / "report.json"
    report.unlink(missing_ok=True)
    args = _pip_base(python, mirror, cache_dir, find_links) + [
        "--ignore-installed",
        "--dry-run",
        "--report",
        str(report),
        "--only-binary",
        ":all:",
        *specs,
    ]
    progress.log("解析依赖闭包…")
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        env=_pip_env(),
    )
    if result.returncode != 0 or not report.exists():
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-6:]
        raise RuntimeError("依赖解析失败：" + " / ".join(tail))
    payload = json.loads(report.read_text(encoding="utf-8"))
    wheels = []
    for item in payload.get("install", []):
        info = item.get("download_info") or {}
        url = info.get("url")
        if not url:
            continue
        meta = item.get("metadata") or {}
        archive = info.get("archive_info") or {}
        hashes = archive.get("hashes") or {}
        sha = hashes.get("sha256") or (archive.get("hash", "").split("=")[-1] if archive.get("hash") else None)
        # 轮子文件名里的本地版本号在 URL 中是 %2B（torch-2.9.1%2Bcpu-...whl），
        # 落盘前必须解码，否则 pip 认不出这是轮子（Invalid wheel filename）。
        filename = unquote(url.rsplit("/", 1)[-1].split("#")[0])
        wheels.append(
            {
                "name": meta.get("name", "?"),
                "version": meta.get("version", "?"),
                "url": url,
                "sha256": sha,
                "filename": filename,
            }
        )
    progress.log(f"依赖闭包共 {len(wheels)} 个轮子")
    return wheels


def fetch_wheels(
    wheels: list[dict],
    cache_dir: Path,
    progress: Progress,
    on_progress_span: tuple[float, float] = (0.25, 0.8),
) -> Path:
    """并发多连接下载所有轮子到本地目录，返回该目录。"""
    target_dir = paths.ensure_dir(cache_dir / "downloads")
    total = len(wheels)
    if total == 0:
        return target_dir
    for index, wheel in enumerate(wheels, start=1):
        dest = target_dir / wheel["filename"]
        lo, hi = on_progress_span
        base_fraction = lo + (hi - lo) * (index - 1) / total
        span = (hi - lo) / total
        progress.log(f"[{index}/{total}] {wheel['name']} {wheel['version']} · {wheel['filename']}")
        if dest.exists():
            progress.update(base_fraction + span, f"已缓存 {wheel['filename']}")
            continue

        url = wheel["url"]
        if url.startswith("file:"):
            # pip 从本地 --find-links 目录解析出来的（torch/torchaudio 大轮子），
            # 已经在磁盘上，直接复制过去即可。
            from urllib.parse import unquote, urlparse

            source = Path(unquote(urlparse(url).path.lstrip("/")))
            if source.exists():
                try:
                    os.link(source, dest)
                except OSError:
                    shutil.copy2(source, dest)
            progress.update(base_fraction + span, f"已就绪 {wheel['filename']}")
            continue

        def report(done: int, size: int, _base=base_fraction, _span=span, _name=wheel["filename"]) -> None:
            frac = _base + (_span * (done / size if size else 0))
            progress.update(frac, f"下载 {_name}（{done / 1048576:.0f}/{size / 1048576:.0f} MB）")

        try:
            net.download(
                url,
                dest,
                connections=8,
                on_progress=report,
                expected_sha256=wheel.get("sha256"),
                min_size=128,
            )
        except net.DownloadError as exc:
            raise RuntimeError(f"下载失败 {wheel['filename']}：{exc}") from exc
    return target_dir


def install_from_dir(python: Path, wheel_dir: Path, progress: Progress) -> None:
    """离线装配已下载的轮子。"""
    progress.update(0.82, "安装依赖（离线装配）")
    args = [
        str(python),
        "-m",
        "pip",
        "install",
        "--no-input",
        "--disable-pip-version-check",
        "--no-index",
        "--find-links",
        str(wheel_dir),
        "--no-warn-script-location",
    ]
    result = subprocess.run(
        args + [str(p) for p in sorted(wheel_dir.glob("*.whl"))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3600,
        env=_pip_env(),
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-8:]
        raise RuntimeError("离线安装失败：" + " / ".join(tail))
    progress.log("依赖装配完成")


def setup_ffmpeg(python: Path, progress: Progress) -> Path | None:
    """准备 ffmpeg：优先从 imageio-ffmpeg 的轮子里取，失败则用系统 ffmpeg。"""
    target = paths.bin_dir() / "ffmpeg.exe"
    if target.exists() and target.stat().st_size > 1024:
        progress.log(f"ffmpeg 已就绪：{target}")
        return target
    probe = subprocess.run(
        [str(python), "-c", "import imageio_ffmpeg,sys;print(imageio_ffmpeg.get_ffmpeg_exe())"],
        capture_output=True,
        text=True,
        timeout=120,
        env=_pip_env(),
    )
    if probe.returncode == 0:
        candidate = Path((probe.stdout or "").strip())
        if candidate.exists():
            paths.ensure_dir(target.parent)
            shutil.copy2(candidate, target)
            progress.log(f"ffmpeg 已复制到 {target}")
            return target
    fallback = paths.ffmpeg_exe()
    if fallback:
        progress.log(f"使用系统 ffmpeg：{fallback}")
        return fallback
    progress.log("警告：未找到 ffmpeg，部分音频格式可能无法读取")
    return None


def self_check(python: Path, progress: Progress) -> tuple[bool, list[str]]:
    """逐项导入自检，返回 (是否全部通过, 失败项)。"""
    script = (
        "import importlib, json, sys\n"
        "mods = json.loads(sys.argv[1])\n"
        "bad = []\n"
        "for m in mods:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "    except Exception as exc:\n"
        "        bad.append(f'{m}: {exc.__class__.__name__}: {exc}')\n"
        "print(json.dumps(bad, ensure_ascii=False))\n"
    )
    mods = [m for m, _ in IMPORT_CHECKS]
    result = subprocess.run(
        [str(python), "-c", script, json.dumps(mods)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        env=_pip_env(),
    )
    if result.returncode != 0:
        return False, [(result.stderr or result.stdout or "").strip()[-400:]]
    try:
        bad = json.loads((result.stdout or "[]").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, ["自检输出无法解析"]
    return not bad, bad


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def install_dependencies(
    *,
    flavor: str = "auto",
    progress_file: str | Path | None = None,
    log_file: str | Path | None = None,
    on_progress: Callable[[float, str], None] | None = None,
    on_log: Callable[[str], None] | None = None,
    force_mirror: str | None = None,
    force: bool = False,
) -> bool:
    """安装全部依赖。返回是否成功。"""
    python = paths.python_exe(windowed=False)
    log_path = Path(log_file) if log_file else None
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8")
        except OSError:
            log_path = None
    progress = Progress(
        progress_file=Path(progress_file) if progress_file else None,
        on_progress=on_progress,
        on_log=on_log,
        log_file=log_path,
    )
    progress.update(0.01, "检查运行环境")

    if not force and deps_ready():
        progress.log("依赖已全部就绪，无需安装")
        progress.finish("依赖已就绪")
        return True

    if force_mirror:
        ranking = mirrors.MirrorRanking(scores=[mirrors.MirrorScore(mirrors.get_mirror(force_mirror), 0.0, 1.0)])
        progress.log(f"按指定镜像安装：{mirrors.get_mirror(force_mirror).name}")
    else:
        progress.update(0.03, "正在测速国内镜像…")
        ranking = mirrors.get_ranking(
            seconds=2.5,
            on_log=progress.log,
            force=True,
        )
    candidates = ranking.ordered[:3] if ranking.scores else list(mirrors.MIRRORS[:3])

    chosen_flavor, reason = choose_torch_flavor(flavor)
    progress.log(reason)
    progress.log(f"torch 版本：{TORCH_VERSION}+{chosen_flavor}")

    specs = _core_specs(chosen_flavor)
    cache_dir = paths.ensure_dir(paths.user_cache_dir() / "install")

    # torch / torchaudio 只存在于 PyTorch 官方镜像，与「最快 PyPI 镜像」是两件事：
    # 先用多连接把两个大轮子拉到本地，后面所有 pip 操作都带上这个本地 find-links。
    try:
        progress.update(0.05, f"下载 torch（{chosen_flavor}）")
        torch_dir = prefetch_torch(chosen_flavor, cache_dir, progress, on_progress_span=(0.05, 0.2))
    except Exception as exc:  # noqa: BLE001 - 这一步失败就没有继续的意义
        log.exception("torch 轮子下载失败")
        progress.finish(f"torch 轮子下载失败：{exc}")
        return False
    find_links: tuple[Path, ...] = (torch_dir,)

    last_error = ""

    for attempt, mirror in enumerate(candidates, start=1):
        progress.log(f"使用镜像：{mirror.name}（{mirror.index_url}）")
        try:
            ensure_pip(python, mirror, progress)
            progress.update(0.22, "解析依赖")
            wheels = resolve_wheels(python, mirror, chosen_flavor, specs, cache_dir, progress, find_links)
            progress.update(0.28, f"开始下载 {len(wheels)} 个依赖包")
            wheel_dir = fetch_wheels(wheels, cache_dir, progress, on_progress_span=(0.28, 0.8))
            install_from_dir(python, wheel_dir, progress)
            progress.update(0.88, "配置 ffmpeg")
            setup_ffmpeg(python, progress)
            progress.update(0.95, "自检依赖")
            ok, bad = self_check(python, progress)
            if not ok:
                raise RuntimeError("自检未通过：" + "；".join(bad[:3]))
            mark_ready(chosen_flavor)
            progress.log("全部依赖安装完成")
            progress.finish("依赖安装完成")
            return True
        except Exception as exc:  # noqa: BLE001 - 换源重试
            last_error = f"{mirror.name}: {exc}"
            log.exception("镜像 %s 安装失败", mirror.name)
            progress.log(f"镜像 {mirror.name} 安装失败：{exc}")
            if attempt < len(candidates):
                progress.log("切换到下一个镜像重试…")

    progress.finish(f"依赖安装失败：{last_error}")
    return False


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="vocal2midi-installer", description="vocal2midi 依赖安装引擎")
    parser.add_argument("--install", action="store_true", help="安装缺失依赖")
    parser.add_argument("--check", action="store_true", help="只检查依赖状态")
    parser.add_argument("--flavor", default="auto", choices=("auto", "cpu", "cu128"), help="torch 变体")
    parser.add_argument("--mirror", default=None, help="强制使用某个镜像（key）")
    parser.add_argument("--progress-file", default=None, help="进度 JSON 文件路径（供安装器轮询）")
    parser.add_argument("--log-file", default=None, help="日志文件路径（供安装器滚动显示）")
    parser.add_argument("--force", action="store_true", help="即使已就绪也重新安装")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.check:
        state = torch_state()
        print(json.dumps({"ready": deps_ready(), "torch": state, "missing": missing_modules()}, ensure_ascii=False, indent=2))
        return 0 if deps_ready() else 1

    if not args.install:
        parser.print_help()
        return 0

    ok = install_dependencies(
        flavor=args.flavor,
        progress_file=args.progress_file,
        log_file=args.log_file,
        force_mirror=args.mirror,
        force=args.force,
        on_progress=lambda frac, text: print(f"[{frac * 100:5.1f}%] {text}", flush=True),
        on_log=lambda text: print(text, flush=True),
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
