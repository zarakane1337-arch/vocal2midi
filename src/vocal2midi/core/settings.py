"""用户设置持久化（%APPDATA%\\vocal2midi\\settings.json）。

界面只跟 `Settings` 打交道：读写字段、保存、恢复默认。字段新增时自动补默认值，
旧配置文件不会因为缺字段而报错。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .. import paths

log = logging.getLogger("vocal2midi.settings")

THEMES = ("system", "light", "dark")


@dataclass
class Settings:
    # 主题
    theme: str = "system"  # system / light / dark

    # 人声分离
    model: str = "htdemucs_ft"
    device: str = "auto"
    vocals_only: bool = False
    overwrite: bool = False

    # 输出
    output_dir: str = ""
    write_csv: bool = True

    # 转写参数（键名与 core.params 一致）
    params: dict[str, Any] = field(default_factory=dict)

    # 最近使用的文件
    recent_files: list[str] = field(default_factory=list)

    # 窗口状态
    window_width: int = 980
    window_height: int = 760

    # 自定义参数输入框内容（保留上次文本，方便复用）
    custom_params: str = ""

    def __post_init__(self) -> None:
        if self.theme not in THEMES:
            self.theme = "system"
        self.recent_files = [p for p in self.recent_files if isinstance(p, str)][:8]

    # ---- 读写 -----------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or paths.settings_file()
        if not path.exists():
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("设置文件读取失败，使用默认值：%s", exc)
            return cls()
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in payload.items() if k in known}
        if isinstance(data.get("params"), dict):
            data["params"] = {str(k): v for k, v in data["params"].items()}
        else:
            data.pop("params", None)
        try:
            return cls(**data)
        except TypeError as exc:
            log.warning("设置文件字段异常，使用默认值：%s", exc)
            return cls()

    def save(self, path: Path | None = None) -> None:
        path = path or paths.settings_file()
        try:
            paths.ensure_dir(path.parent)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.warning("设置保存失败：%s", exc)

    # ---- 便捷方法 -------------------------------------------------------
    def resolved_output_dir(self) -> Path:
        if self.output_dir:
            candidate = Path(self.output_dir)
            if candidate.is_absolute():
                return candidate
        return paths.default_output_dir()

    def remember_file(self, file_path: str | Path) -> None:
        text = str(file_path)
        self.recent_files = [text] + [p for p in self.recent_files if p != text]
        self.recent_files = self.recent_files[:8]

    @property
    def is_dark(self) -> bool:
        return self.theme == "dark"
