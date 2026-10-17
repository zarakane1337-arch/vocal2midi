"""国内镜像源登记与测速选源。

设计要点：
    * 每个镜像是一条 `Mirror`，携带 PyPI 索引、PyTorch 轮子目录、文档/文件树。
    * 测速是「真实吞吐」而不是只测延迟：并发下载若干字节并计时，得到能反映
      实际 pip 安装速度的排名（延迟再低但单流 100KB/s 一样会很慢）。
    * 结果会缓存到 `%LOCALAPPDATA%\\vocal2midi\\cache\\mirrors.json`，有效期内复用，
      避免每次启动都重新测速。
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths
from . import net

log = logging.getLogger("vocal2midi.mirrors")

CACHE_TTL_SECONDS = 6 * 3600


@dataclass(frozen=True)
class Mirror:
    key: str
    name: str
    index_url: str  # pip --index-url
    pytorch_url: str = ""  # pip --find-links（torch/torchaudio 轮子目录）
    homepage: str = ""

    def index_page(self, package: str) -> str:
        return self.index_url.rstrip("/") + "/" + package.strip("/") + "/"

    def probe_url(self) -> str:
        """测速用的兜底探测页（拿不到具体文件时退回这里）。"""
        return self.index_page("numpy")

    def pytorch_dir(self, flavor: str) -> str:
        """返回某个 torch 变体（cpu/cu128…）的 find-links 地址。"""
        if not self.pytorch_url:
            return ""
        return f"{self.pytorch_url.rstrip('/')}/{flavor}/"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "index_url": self.index_url,
            "pytorch_url": self.pytorch_url,
            "homepage": self.homepage,
        }


# 只保留实测可用的国内镜像；豆瓣/华为云实测已失效或限流，不再列入。
MIRRORS: tuple[Mirror, ...] = (
    Mirror(
        key="aliyun",
        name="阿里云",
        index_url="https://mirrors.aliyun.com/pypi/simple/",
        pytorch_url="https://mirrors.aliyun.com/pytorch-wheels",
        homepage="https://developer.aliyun.com/mirror/pypi",
    ),
    Mirror(
        key="ustc",
        name="中国科学技术大学",
        index_url="https://mirrors.ustc.edu.cn/pypi/simple/",
        pytorch_url="https://mirrors.ustc.edu.cn/pytorch-wheels",
        homepage="https://mirrors.ustc.edu.cn/help/pypi.html",
    ),
    Mirror(
        key="tuna",
        name="清华大学",
        index_url="https://pypi.tuna.tsinghua.edu.cn/simple/",
        pytorch_url="https://mirrors.tuna.tsinghua.edu.cn/pytorch-wheels",
        homepage="https://mirrors.tuna.tsinghua.edu.cn/help/pypi/",
    ),
    Mirror(
        key="tencent",
        name="腾讯云",
        index_url="https://mirrors.cloud.tencent.com/pypi/simple/",
        pytorch_url="https://mirrors.cloud.tencent.com/pytorch-wheels",
        homepage="https://mirrors.cloud.tencent.com/help/pypi.html",
    ),
    Mirror(
        key="huawei",
        name="华为云",
        index_url="https://repo.huaweicloud.com/repository/pypi/simple/",
        pytorch_url="https://repo.huaweicloud.com/pytorch-wheels",
        homepage="https://mirrors.huaweicloud.com/home",
    ),
)

MIRROR_INDEX: dict[str, Mirror] = {m.key: m for m in MIRRORS}


@dataclass
class MirrorScore:
    mirror: Mirror
    latency_s: float
    throughput_mib_s: float
    ok: bool = True

    @property
    def effective(self) -> float:
        """综合评分：吞吐为主，延迟做小幅惩罚。"""
        if not self.ok or self.throughput_mib_s <= 0:
            return 0.0
        latency_penalty = 1.0 / (1.0 + max(0.0, self.latency_s) * 2.0)
        return self.throughput_mib_s * latency_penalty

    def describe(self) -> str:
        if not self.ok:
            return f"{self.mirror.name}：不可用"
        return f"{self.mirror.name}：{self.throughput_mib_s:.1f} MiB/s（延迟 {self.latency_s * 1000:.0f}ms）"


@dataclass
class MirrorRanking:
    scores: list[MirrorScore] = field(default_factory=list)
    measured_at: float = 0.0

    @property
    def best(self) -> Mirror:
        for score in self.scores:
            if score.ok and score.throughput_mib_s > 0:
                return score.mirror
        return MIRRORS[0]

    @property
    def ordered(self) -> list[Mirror]:
        """按评分从高到低，含备用顺序。"""
        usable = [s.mirror for s in self.scores if s.ok and s.throughput_mib_s > 0]
        rest = [m for m in MIRRORS if m not in usable]
        return usable + rest

    def describe(self) -> str:
        return "，".join(s.describe() for s in self.scores)


class _ProbeTarget:
    """测速目标：优先用镜像上真实存在的 numpy 轮子，退回索引页。"""

    __slots__ = ("url", "size_hint")

    def __init__(self, url: str, size_hint: int = 0) -> None:
        self.url = url
        self.size_hint = size_hint


_probe_cache: dict[str, _ProbeTarget] = {}


def _resolve_probe_target(mirror: Mirror) -> _ProbeTarget:
    """在镜像索引里找一个 numpy win_amd64 轮子，用真实文件测吞吐。"""
    cached = _probe_cache.get(mirror.key)
    if cached is not None:
        return cached
    page = mirror.index_page("numpy")
    target = _ProbeTarget(page)
    try:
        html = net.fetch_text(page, timeout=12.0)
        import re
        from urllib.parse import urljoin

        pattern = re.compile(r'href="([^"#]+\.whl)(?:#[^"]*)?"[^>]*>([^<]*numpy[^<]*\.whl)<')
        best: tuple[int, str] | None = None
        for href, name in pattern.findall(html):
            if "cp312" not in name or "win_amd64" not in name:
                continue
            url = urljoin(page, href)
            if best is None or len(name) < len(best[1]):
                best = (len(name), url)
        if best is not None:
            target = _ProbeTarget(best[1])
    except Exception as exc:  # noqa: BLE001 - 探测失败退回索引页
        log.debug("解析 %s 的 numpy 轮子失败：%s", mirror.name, exc)
    _probe_cache[mirror.key] = target
    return target


def _probe(mirror: Mirror, seconds: float) -> MirrorScore:
    latency = net.measure_latency(mirror.index_url)
    if latency == float("inf"):
        return MirrorScore(mirror, latency_s=float("inf"), throughput_mib_s=0.0, ok=False)
    target = _resolve_probe_target(mirror)
    throughput = net.measure_throughput(target.url, seconds=seconds)
    if throughput <= 0 and target.url != mirror.probe_url():
        throughput = net.measure_throughput(mirror.probe_url(), seconds=seconds)
    return MirrorScore(mirror, latency_s=latency, throughput_mib_s=throughput, ok=throughput > 0)


def rank_mirrors(seconds: float = 2.5, on_log=None) -> MirrorRanking:
    """并发测速所有镜像，返回排名（顺序即评分从高到低）。"""
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(MIRRORS)) as pool:
        scores = list(pool.map(lambda m: _probe(m, seconds), MIRRORS))
    scores.sort(key=lambda s: s.effective, reverse=True)
    ranking = MirrorRanking(scores=scores, measured_at=time.time())
    if on_log:
        for score in scores:
            on_log(score.describe())
    log.info("镜像测速完成（%.1fs）：%s", time.monotonic() - started, ranking.describe())
    return ranking


def _cache_file() -> Path:
    return paths.user_cache_dir() / "mirrors.json"


def save_ranking(ranking: MirrorRanking) -> None:
    try:
        path = paths.ensure_dir(_cache_file().parent) / _cache_file().name
        payload = {
            "measured_at": ranking.measured_at,
            "scores": [
                {
                    "key": s.mirror.key,
                    "latency_s": s.latency_s if s.latency_s != float("inf") else -1,
                    "throughput_mib_s": s.throughput_mib_s,
                    "ok": s.ok,
                }
                for s in ranking.scores
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        log.debug("镜像测速结果写入失败：%s", exc)


def load_ranking(max_age: float = CACHE_TTL_SECONDS) -> MirrorRanking | None:
    path = _cache_file()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    measured_at = float(payload.get("measured_at", 0))
    if time.time() - measured_at > max_age:
        return None
    scores: list[MirrorScore] = []
    for entry in payload.get("scores", []):
        mirror = MIRROR_INDEX.get(str(entry.get("key")))
        if mirror is None:
            continue
        latency = float(entry.get("latency_s", -1))
        scores.append(
            MirrorScore(
                mirror=mirror,
                latency_s=float("inf") if latency < 0 else latency,
                throughput_mib_s=float(entry.get("throughput_mib_s", 0)),
                ok=bool(entry.get("ok", False)),
            )
        )
    if not scores:
        return None
    return MirrorRanking(scores=scores, measured_at=measured_at)


def get_ranking(seconds: float = 2.5, on_log=None, force: bool = False) -> MirrorRanking:
    """带缓存地取排名：缓存有效则直接复用，否则重新测速。"""
    if not force:
        cached = load_ranking()
        if cached is not None:
            if on_log:
                on_log("复用上次测速结果：" + cached.describe())
            return cached
    ranking = rank_mirrors(seconds=seconds, on_log=on_log)
    save_ranking(ranking)
    return ranking


def get_mirror(key: str | None) -> Mirror:
    if key and key in MIRROR_INDEX:
        return MIRROR_INDEX[key]
    return MIRRORS[0]


def pick_fastest(seconds: float = 2.5, on_log=None) -> Mirror:
    return get_ranking(seconds=seconds, on_log=on_log).best
