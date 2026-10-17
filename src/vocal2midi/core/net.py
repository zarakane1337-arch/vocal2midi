"""多连接断点续传下载器。

国内镜像单连接常常只有 0.3~1 MB/s，但对同一文件的多个 Range 请求可以叠加带宽
（实测 PyTorch 轮子 8 连接可达 20 MB/s）。这里实现一个纯标准库的分片下载器：

    * HEAD 探测文件大小与 Range 支持
    * 按连接数切分字节区间，每片独立写临时文件
    * 失败自动重试 + 指数退避
    * 中途取消时保留已下载分片，下次可续传
    * 全部完成后按序拼接，并校验 sha256（可选）
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("vocal2midi.net")

USER_AGENT = "vocal2midi-installer/2.0 (+https://github.com/)"
CHUNK = 1024 * 256
DEFAULT_CONNECTIONS = 8


class DownloadError(RuntimeError):
    """下载失败。"""


@dataclass
class DownloadResult:
    path: Path
    bytes_written: int
    seconds: float
    from_cache: bool = False

    @property
    def mib_per_s(self) -> float:
        return (self.bytes_written / 1048576.0) / self.seconds if self.seconds > 0 else 0.0


ProgressFn = Callable[[int, int], None]  # (已下载字节, 总字节)


def _request(url: str, *, method: str = "GET", headers: dict | None = None, timeout: float = 30.0):
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "*/*")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    return urllib.request.urlopen(req, timeout=timeout)


def remote_size(url: str, timeout: float = 20.0) -> tuple[int, bool]:
    """返回 (文件字节数, 是否支持 Range)；探测失败时抛 DownloadError。"""
    try:
        with _request(url, method="HEAD", timeout=timeout) as resp:
            size = int(resp.headers.get("Content-Length") or 0)
            accept = (resp.headers.get("Accept-Ranges") or "").lower()
            return size, "bytes" in accept
    except Exception:  # noqa: BLE001 - 部分服务器不支持 HEAD，退回 GET 取头
        pass
    try:
        with _request(url, method="GET", headers={"Range": "bytes=0-0"}, timeout=timeout) as resp:
            if resp.status == 206:
                content_range = resp.headers.get("Content-Range") or ""
                total = content_range.split("/")[-1] if "/" in content_range else "0"
                return int(total or 0), True
            return int(resp.headers.get("Content-Length") or 0), False
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(f"无法获取文件大小：{url}（{exc}）") from exc


def _download_range(
    url: str,
    start: int,
    end: int,
    part_path: Path,
    cancel: threading.Event,
    on_chunk: Callable[[int], None],
    retries: int = 4,
) -> None:
    """下载 [start, end] 闭区间到 part_path，已存在则续传。"""
    expected = end - start + 1
    attempt = 0
    while True:
        have = part_path.stat().st_size if part_path.exists() else 0
        if have >= expected:
            return
        if cancel.is_set():
            raise DownloadError("已取消")
        offset = start + have
        headers = {"Range": f"bytes={offset}-{end}"}
        try:
            with _request(url, method="GET", headers=headers, timeout=60.0) as resp:
                if resp.status not in (200, 206):
                    raise DownloadError(f"HTTP {resp.status}")
                mode = "ab" if have else "wb"
                with part_path.open(mode) as fh:
                    while True:
                        if cancel.is_set():
                            raise DownloadError("已取消")
                        block = resp.read(CHUNK)
                        if not block:
                            break
                        fh.write(block)
                        have += len(block)
                        on_chunk(len(block))
                        if have >= expected:
                            break
            if part_path.stat().st_size >= expected:
                return
            raise DownloadError("连接提前结束")
        except Exception as exc:  # noqa: BLE001
            attempt += 1
            if attempt > retries:
                raise DownloadError(f"分片下载失败（{start}-{end}）：{exc}") from exc
            log.debug("分片重试 %d/%d：%s", attempt, retries, exc)
            time.sleep(min(6.0, 0.8 * attempt))


def download(
    url: str,
    dest: str | Path,
    *,
    connections: int = DEFAULT_CONNECTIONS,
    on_progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    expected_sha256: str | None = None,
    timeout: float = 20.0,
    min_size: int = 1024,
) -> DownloadResult:
    """把 url 下载到 dest（自动多连接 + 续传 + 校验）。"""
    dest = Path(dest)
    cancel = cancel or threading.Event()
    if dest.exists() and dest.stat().st_size >= min_size:
        if not expected_sha256 or _sha256(dest) == expected_sha256.lower():
            size = dest.stat().st_size
            if on_progress:
                on_progress(size, size)
            return DownloadResult(dest, size, 0.001, from_cache=True)
        log.warning("缓存文件校验不符，重新下载：%s", dest)

    dest.parent.mkdir(parents=True, exist_ok=True)
    total, ranged = remote_size(url, timeout=timeout)
    if total <= 0:
        raise DownloadError(f"文件大小未知：{url}")

    started = time.monotonic()
    parts_dir = dest.parent / (dest.name + ".parts")
    parts_dir.mkdir(parents=True, exist_ok=True)

    workers = max(1, int(connections)) if ranged else 1
    # 小于 4MB 的文件不值得多连接
    if total < 4 * 1048576:
        workers = 1
    span = (total + workers - 1) // workers

    downloaded = [0] * workers
    lock = threading.Lock()
    done_bytes = 0

    def report(delta: int) -> None:
        nonlocal done_bytes
        with lock:
            done_bytes += delta
            if on_progress:
                on_progress(min(done_bytes, total), total)

    def worker(index: int) -> None:
        start = index * span
        end = min(total, start + span) - 1
        if start > end:
            return
        part = parts_dir / f"{dest.name}.{index:04d}.part"
        # 续传时把已存在分片计入总进度
        if part.exists():
            have = min(part.stat().st_size, end - start + 1)
            downloaded[index] = have
            report(have)
        try:
            _download_range(url, start, end, part, cancel, report)
        except Exception as exc:  # noqa: BLE001 - 取消/失败都交给主线程统一处理
            errors.append(exc)

    errors: list[Exception] = []
    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if cancel.is_set():
        raise DownloadError("已取消")
    if errors:
        raise DownloadError(str(errors[0]))

    # 拼接分片
    tmp = dest.with_suffix(dest.suffix + ".assembling")
    with tmp.open("wb") as out:
        for index in range(workers):
            part = parts_dir / f"{dest.name}.{index:04d}.part"
            if not part.exists():
                raise DownloadError(f"缺少分片：{part.name}")
            with part.open("rb") as fh:
                while True:
                    block = fh.read(1048576)
                    if not block:
                        break
                    out.write(block)
    assembled = tmp.stat().st_size
    if assembled != total:
        tmp.unlink(missing_ok=True)
        raise DownloadError(f"文件大小不符：期望 {total}，实际 {assembled}")
    if expected_sha256:
        actual = _sha256(tmp)
        if actual != expected_sha256.lower():
            tmp.unlink(missing_ok=True)
            raise DownloadError(f"sha256 校验失败：期望 {expected_sha256[:12]}…，实际 {actual[:12]}…")
    os.replace(tmp, dest)
    for index in range(workers):
        (parts_dir / f"{dest.name}.{index:04d}.part").unlink(missing_ok=True)
    try:
        parts_dir.rmdir()
    except OSError:
        pass

    seconds = max(0.001, time.monotonic() - started)
    log.info("下载完成 %s（%.1f MB / %.1fs）", dest.name, total / 1048576, seconds)
    return DownloadResult(dest, total, seconds)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(1048576)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def fetch_text(url: str, *, timeout: float = 20.0, max_bytes: int = 4 * 1048576) -> str:
    """取一个文本资源（索引页等），限制最大读取量避免踩到大文件。"""
    with _request(url, method="GET", timeout=timeout) as resp:
        if resp.status != 200:
            raise DownloadError(f"HTTP {resp.status}：{url}")
        buffer = bytearray()
        while len(buffer) < max_bytes:
            block = resp.read(64 * 1024)
            if not block:
                break
            buffer.extend(block)
    return buffer.decode("utf-8", "replace")


def measure_throughput(url: str, *, seconds: float = 3.0, max_bytes: int = 24 * 1048576) -> float:
    """限时读取 url 前若干字节，返回 MiB/s（失败返回 0）。

    优先用 Range 请求；服务器不支持 Range 时退回普通 GET，
    只要在时限内读到足够字节就提前断开——测的是「下载速度」而不是「能否成功」。
    """
    attempts: tuple[dict | None, ...] = ({"Range": f"bytes=0-{max_bytes - 1}"}, None)
    for headers in attempts:
        started = time.monotonic()
        received = 0
        try:
            with _request(url, method="GET", timeout=max(4.0, seconds + 2), headers=headers) as resp:
                if resp.status not in (200, 206):
                    continue
                while True:
                    if time.monotonic() - started >= seconds:
                        break
                    block = resp.read(64 * 1024)
                    if not block:
                        break
                    received += len(block)
                    if received >= max_bytes:
                        break
        except Exception:  # noqa: BLE001
            continue
        elapsed = max(0.001, time.monotonic() - started)
        if received > 0:
            return (received / 1048576.0) / elapsed
    return 0.0


def measure_latency(url: str, timeout: float = 4.0) -> float:
    """往返耗时（秒）；HEAD 被拒时退回 Range GET。全部失败返回 inf。"""
    started = time.monotonic()
    try:
        with _request(url, method="HEAD", timeout=timeout) as resp:
            resp.read(1)
        return time.monotonic() - started
    except Exception:  # noqa: BLE001 - 不少镜像对 HEAD 返回 429/405
        pass
    started = time.monotonic()
    try:
        with _request(url, method="GET", headers={"Range": "bytes=0-0"}, timeout=timeout) as resp:
            resp.read(1)
        return time.monotonic() - started
    except Exception:  # noqa: BLE001
        return float("inf")
