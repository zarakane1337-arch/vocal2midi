"""人声转 MIDI 参数注册表。

同一份定义同时驱动三件事，保证界面与参数解析永远一致：
    1. 界面上的滑动条范围 / 步长 / 小数位
    2. 界面上的数字输入框
    3. 「自定义参数」输入框的文本解析（`key=value` 或 `--key value`）

新增一个参数只需在 `TRANSCRIPTION_PARAMS` 里加一条，UI 会自动长出对应行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class ParamSpec:
    key: str  # 内部键名（与 transcribe_to_midi 参数同名）
    label: str  # 界面显示名
    default: float | int | bool | str
    minimum: float = 0.0
    maximum: float = 1.0
    step: float = 0.01
    decimals: int = 2
    unit: str = ""  # 显示单位
    help: str = ""  # 悬停提示
    aliases: tuple[str, ...] = ()  # 自定义参数里可接受的别名
    integer: bool = False
    choices: tuple[str, ...] = ()  # 非空时表示枚举型参数

    # ---- 数值换算 -------------------------------------------------------
    def clamp(self, value: float) -> float:
        return max(self.minimum, min(self.maximum, value))

    def coerce(self, value: Any) -> Any:
        """把任意来源（文本、滑块、JSON）的值规整成该参数的类型。"""
        if self.choices:
            text = str(value).strip().lower()
            for choice in self.choices:
                if text == choice.lower():
                    return choice
            raise ValueError(f"{self.key} 只能是 {'/'.join(self.choices)} 之一，收到 {value!r}")
        if self.integer:
            return int(round(float(value)))
        return float(value)

    def format(self, value: Any) -> str:
        if self.choices:
            return str(value)
        if self.integer:
            return str(int(value))
        return f"{float(value):.{self.decimals}f}"

    def slider_steps(self) -> int:
        span = (self.maximum - self.minimum) / self.step if self.step else 1
        return max(1, int(round(span)))

    def to_slider(self, value: Any) -> int:
        return int(round((float(value) - self.minimum) / self.step)) if self.step else 0

    def from_slider(self, steps: int) -> float | int:
        value = self.minimum + steps * self.step
        value = self.clamp(value)
        return int(round(value)) if self.integer else round(value, self.decimals)


TRANSCRIPTION_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec(
        key="onset_threshold",
        label="音符切分灵敏度",
        default=0.5,
        minimum=0.3,
        maximum=0.9,
        step=0.01,
        decimals=2,
        help="调高会让音符更长更少，调低则更细碎（内部映射为 SwiftF0 的音高保持时长）",
        aliases=("onset", "onset_thr", "切分"),
    ),
    ParamSpec(
        key="frame_threshold",
        label="发声置信度阈值",
        default=0.3,
        minimum=0.05,
        maximum=0.95,
        step=0.01,
        decimals=2,
        help="低于该置信度的帧视为静音或清音，背景噪声大时可上调",
        aliases=("frame", "confidence", "conf", "置信度"),
    ),
    ParamSpec(
        key="min_note_length_ms",
        label="最短音符时长",
        default=50.0,
        minimum=10.0,
        maximum=400.0,
        step=1.0,
        decimals=0,
        unit="ms",
        integer=True,
        help="短于该时长的音符会被丢弃，用于去除抖动产生的碎音",
        aliases=("min_note", "min_note_len", "minlen", "最短音符"),
    ),
    ParamSpec(
        key="f0_min",
        label="最低基频",
        default=70.0,
        minimum=40.0,
        maximum=400.0,
        step=1.0,
        decimals=0,
        unit="Hz",
        integer=True,
        help="人声基频下限；男低音可下探到 60Hz 左右",
        aliases=("fmin", "f0_low", "最低"),
    ),
    ParamSpec(
        key="f0_max",
        label="最高基频",
        default=900.0,
        minimum=200.0,
        maximum=2000.0,
        step=5.0,
        decimals=0,
        unit="Hz",
        integer=True,
        help="人声基频上限；女高音/假声可上调到 1100Hz 以上",
        aliases=("fmax", "f0_high", "最高"),
    ),
    ParamSpec(
        key="pitch_hold_ms",
        label="音高保持时长",
        default=0.0,
        minimum=0.0,
        maximum=200.0,
        step=1.0,
        decimals=0,
        unit="ms",
        integer=True,
        help="0 = 自动（由音符切分灵敏度推导）；手动指定后覆盖该推导值",
        aliases=("hold", "hold_ms", "保持"),
    ),
    ParamSpec(
        key="velocity_floor",
        label="最低力度",
        default=0.0,
        minimum=0.0,
        maximum=0.6,
        step=0.01,
        decimals=2,
        help="抬升最弱音符的力度，避免在 DAW 里轻到听不见",
        aliases=("vel_floor", "力度"),
    ),
    ParamSpec(
        key="bend_range",
        label="弯音量程",
        default=2.0,
        minimum=0.0,
        maximum=24.0,
        step=1.0,
        decimals=0,
        unit="半音",
        integer=True,
        help="MIDI 弯音轮的量程；0 = 不写弯音，只保留整数音高",
        aliases=("bend", "pitch_bend_range", "弯音"),
    ),
    ParamSpec(
        key="tempo",
        label="MIDI 速度",
        default=120.0,
        minimum=40.0,
        maximum=240.0,
        step=1.0,
        decimals=0,
        unit="BPM",
        integer=True,
        help="写入 .mid 的速度标记，也会决定时间轴刻度",
        aliases=("bpm", "速度"),
    ),
    ParamSpec(
        key="quantize",
        label="音符对齐",
        default="off",
        choices=("off", "1/4", "1/8", "1/16", "1/32"),
        help="把音符起点吸附到节拍网格，便于在 DAW 中继续编排",
        aliases=("quant", "对齐"),
    ),
    ParamSpec(
        key="transpose",
        label="整体移调",
        default=0,
        minimum=-24,
        maximum=24,
        step=1,
        decimals=0,
        unit="半音",
        integer=True,
        help="整轨升降调，正数为升高",
        aliases=("shift", "移调"),
    ),
)

PARAM_INDEX: dict[str, ParamSpec] = {spec.key: spec for spec in TRANSCRIPTION_PARAMS}


def _alias_index() -> dict[str, ParamSpec]:
    table: dict[str, ParamSpec] = {}
    for spec in TRANSCRIPTION_PARAMS:
        table[spec.key.lower()] = spec
        for alias in spec.aliases:
            table[alias.lower()] = spec
    return table


ALIAS_INDEX: dict[str, ParamSpec] = _alias_index()


def default_params() -> dict[str, Any]:
    return {spec.key: spec.default for spec in TRANSCRIPTION_PARAMS}


def normalize(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """把外部传入的参数字典补全并规整类型；未知键忽略。"""
    result = default_params()
    if not params:
        return result
    for key, value in params.items():
        spec = ALIAS_INDEX.get(str(key).strip().lower())
        if spec is None:
            continue
        try:
            result[spec.key] = spec.coerce(value)
        except (TypeError, ValueError):
            continue
    return result


# ---------------------------------------------------------------------------
# 自定义参数文本解析
# ---------------------------------------------------------------------------

_SPLIT_RE = re.compile(r"[\s,;，；\n\r]+")
_ARG_RE = re.compile(r"^--?([A-Za-z_][\w-]*)$")


@dataclass
class CustomParseResult:
    values: dict[str, Any] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def message(self) -> str:
        parts: list[str] = []
        if self.errors:
            parts.append("；".join(self.errors))
        if self.unknown:
            parts.append("已忽略未知参数：" + "、".join(self.unknown))
        return "；".join(parts)


def parse_custom_params(text: str) -> CustomParseResult:
    """解析「自定义参数」输入框。

    支持三种写法，可混用：
        key=value          onset_threshold=0.6
        --key value        --onset_threshold 0.6
        key value          onset_threshold 0.6
    分隔符可用空格 / 逗号 / 分号 / 换行。枚举参数可写 `quantize=1/16`。
    """
    result = CustomParseResult()
    tokens = [t for t in _SPLIT_RE.split((text or "").strip()) if t]
    i = 0
    while i < len(tokens):
        token = tokens[i]
        spec: ParamSpec | None = None
        if "=" in token:
            raw_key, raw_value = token.split("=", 1)
            i += 1
            spec = ALIAS_INDEX.get(raw_key.strip().lower())
        elif ":" in token and not token.startswith("--"):
            raw_key, raw_value = token.split(":", 1)
            i += 1
            spec = ALIAS_INDEX.get(raw_key.strip().lower())
        else:
            match = _ARG_RE.match(token)
            raw_key = match.group(1) if match else token
            spec = ALIAS_INDEX.get(raw_key.strip().lower())
            if spec is None:
                result.errors.append(f"无法识别：{token}")
                i += 1
                continue
            if i + 1 >= len(tokens):
                result.errors.append(f"{raw_key} 缺少取值")
                i += 1
                continue
            raw_value = tokens[i + 1]
            i += 2

        if spec is None:
            result.unknown.append(raw_key.strip())
            continue
        try:
            result.values[spec.key] = spec.coerce(raw_value)
        except (TypeError, ValueError) as exc:
            result.errors.append(str(exc))
    return result


def describe_params(params: Mapping[str, Any]) -> str:
    """把参数渲染成一行人类可读文本，用于日志与状态栏。"""
    chunks: list[str] = []
    for spec in TRANSCRIPTION_PARAMS:
        value = params.get(spec.key, spec.default)
        if value == spec.default:
            continue
        text = spec.format(value)
        chunks.append(f"{spec.label} {text}{spec.unit}")
    return "，".join(chunks) if chunks else "全部为默认值"


def iter_specs() -> Iterable[ParamSpec]:
    return iter(TRANSCRIPTION_PARAMS)
