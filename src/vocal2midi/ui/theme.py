"""主题引擎：深色 / 浅色 / 跟随系统。

三块内容：
    Palette    调色板常量（两套）
    build_qss  生成完整 QSS（控件样式统一在这里维护）
    ThemeManager  负责应用主题、响应系统主题变化、发信号

跟随系统通过 Qt 6.5+ 的 `QStyleHints.colorScheme()` 实现；旧版 Qt 或
Windows 的「应用模式」不可用时退回浅色，并由用户在设置里手动切换。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import QObject, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication

from .. import paths

log = logging.getLogger("vocal2midi.theme")

THEME_SYSTEM = "system"
THEME_LIGHT = "light"
THEME_DARK = "dark"


@dataclass(frozen=True)
class Palette:
    name: str
    window: str
    surface: str
    surface_alt: str
    surface_hover: str
    border: str
    border_strong: str
    text: str
    text_muted: str
    text_faint: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_text: str
    success: str
    warning: str
    danger: str
    track: str


LIGHT = Palette(
    name="light",
    window="#f4f5f7",
    surface="#ffffff",
    surface_alt="#f7f8fa",
    surface_hover="#eef1f5",
    border="#d8dde3",
    border_strong="#c2c9d2",
    text="#1b1f24",
    text_muted="#5b6572",
    text_faint="#8a94a1",
    accent="#2f6feb",
    accent_hover="#3d7bf0",
    accent_pressed="#255ecb",
    accent_text="#ffffff",
    success="#1a7f4b",
    warning="#b26a00",
    danger="#c8362f",
    track="#e3e7ec",
)

DARK = Palette(
    name="dark",
    window="#16181d",
    surface="#1e2127",
    surface_alt="#24282f",
    surface_hover="#2c313a",
    border="#333944",
    border_strong="#454c58",
    text="#e8eaed",
    text_muted="#a3abb8",
    text_faint="#7a8291",
    accent="#4d8df7",
    accent_hover="#5f9bfa",
    accent_pressed="#3b79dd",
    accent_text="#0b1220",
    success="#3fbf7f",
    warning="#e0a33a",
    danger="#f2705f",
    track="#2f353f",
)

PALETTES = {LIGHT.name: LIGHT, DARK.name: DARK}


def _arrow_icon(p: Palette) -> str:
    """画一个下拉箭头，返回 QSS 可用的文件路径（正斜杠）。

    Qt 的 `QComboBox::down-arrow` 只要被 QSS 碰过，默认箭头就不再绘制；
    而用 border 拼三角形在 Qt 的盒模型里会被压扁。所以这里直接生成 PNG，
    按主题缓存，样式表引用绝对路径。
    """
    cache = paths.user_cache_dir() / "icons"
    target = cache / f"chevron-{p.name}.png"
    if target.exists() and target.stat().st_size > 32:
        return target.as_posix()
    if QApplication.instance() is None:
        # 没有 QApplication 时 QPixmap 不可用（单元测试/只构建样式表的场景）
        return ""
    try:
        paths.ensure_dir(cache)
        scale = 3  # 高 DPI 下也清晰
        size = 10 * scale
        pixmap = QPixmap(size, size)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(p.text_muted))
        pen.setWidthF(1.6 * scale)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        left = 2.2 * scale
        right = size - 2.2 * scale
        mid_y = size * 0.42
        painter.drawPolyline(
            [
                QPointF(left, mid_y),
                QPointF(size / 2.0, size * 0.66),
                QPointF(right, mid_y),
            ]
        )
        painter.end()
        tmp = target.with_suffix(".tmp.png")
        pixmap.save(str(tmp), "PNG")
        tmp.replace(target)
    except OSError:
        return ""
    return target.as_posix()


def build_qss(p: Palette) -> str:
    """生成整窗 QSS。所有控件外观都在这里，界面文件不带散落的样式字符串。"""
    return f"""
* {{
    outline: 0;
}}
QWidget {{
    background: {p.window};
    color: {p.text};
    font-size: 13px;
}}
QMainWindow, QDialog {{
    background: {p.window};
}}
QToolTip {{
    background: {p.surface_alt};
    color: {p.text};
    border: 1px solid {p.border_strong};
    padding: 6px 8px;
    border-radius: 6px;
}}

/* ---------- 卡片 ---------- */
QFrame#Card {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 12px;
}}
QFrame#Card QWidget {{
    background: transparent;
}}
QLabel#CardTitle {{
    color: {p.text};
    font-size: 14px;
    font-weight: 600;
    background: transparent;
}}
QLabel#CardHint {{
    color: {p.text_muted};
    font-size: 12px;
    background: transparent;
}}
QLabel#Muted {{
    color: {p.text_muted};
    background: transparent;
}}
QLabel#Faint {{
    color: {p.text_faint};
    font-size: 12px;
    background: transparent;
}}
QLabel#Title {{
    color: {p.text};
    font-size: 22px;
    font-weight: 700;
    background: transparent;
}}
QLabel#Subtitle {{
    color: {p.text_muted};
    font-size: 12px;
    background: transparent;
}}
QLabel#Status {{
    color: {p.text_muted};
    background: transparent;
}}
QLabel#StatusOk {{ color: {p.success}; background: transparent; }}
QLabel#StatusWarn {{ color: {p.warning}; background: transparent; }}
QLabel#StatusError {{ color: {p.danger}; background: transparent; }}

/* ---------- 按钮 ---------- */
QPushButton {{
    background: {p.surface_alt};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 7px 14px;
    min-height: 20px;
}}
QPushButton:hover {{
    background: {p.surface_hover};
    border-color: {p.border_strong};
}}
QPushButton:pressed {{
    background: {p.track};
}}
QPushButton:disabled {{
    color: {p.text_faint};
    background: {p.surface_alt};
    border-color: {p.border};
}}
QPushButton#Primary {{
    background: {p.accent};
    color: {p.accent_text};
    border: 1px solid {p.accent};
    font-weight: 600;
    padding: 10px 22px;
    font-size: 14px;
}}
QPushButton#Primary:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
QPushButton#Primary:pressed {{ background: {p.accent_pressed}; border-color: {p.accent_pressed}; }}
QPushButton#Primary:disabled {{ background: {p.track}; color: {p.text_faint}; border-color: {p.border}; }}
QPushButton#Ghost {{
    background: transparent;
    border: 1px solid transparent;
    color: {p.text_muted};
    padding: 6px 10px;
}}
QPushButton#Ghost:hover {{
    color: {p.text};
    background: {p.surface_alt};
    border-color: {p.border};
}}
QPushButton#MenuDots {{
    background: transparent;
    border: 1px solid transparent;
    color: {p.text_muted};
    font-size: 16px;
    padding: 4px 2px;
}}
QPushButton#MenuDots:hover {{
    color: {p.text};
    background: {p.surface_alt};
    border-color: {p.border};
}}
QPushButton#MenuDots::menu-indicator {{
    image: none;
    width: 0;
}}

/* ---------- 输入控件 ---------- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {p.surface_alt};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 6px 10px;
    selection-background-color: {p.accent};
    selection-color: {p.accent_text};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {p.accent};
    background: {p.surface};
}}
QLineEdit:disabled, QPlainTextEdit:disabled, QSpinBox:disabled,
QDoubleSpinBox:disabled, QComboBox:disabled {{
    color: {p.text_faint};
    background: {p.window};
}}
QLineEdit[invalid="true"] {{
    border-color: {p.danger};
}}
QComboBox::drop-down {{
    border: none;
    background: transparent;
    width: 22px;
}}
QComboBox::down-arrow {{
    image: url({_arrow_icon(p)});
    width: 10px;
    height: 10px;
    margin-right: 6px;
}}
QComboBox QAbstractItemView {{
    background: {p.surface};
    color: {p.text};
    border: 1px solid {p.border_strong};
    border-radius: 8px;
    selection-background-color: {p.accent};
    selection-color: {p.accent_text};
    padding: 4px;
    outline: 0;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    width: 0px;
    border: none;
}}

/* ---------- 滑动条 ---------- */
QSlider::groove:horizontal {{
    height: 6px;
    background: {p.track};
    border-radius: 3px;
}}
QSlider::sub-page:horizontal {{
    background: {p.accent};
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: {p.surface};
    border: 2px solid {p.accent};
    width: 14px;
    height: 14px;
    margin: -6px 0;
    border-radius: 9px;
}}
QSlider::handle:horizontal:hover {{
    background: {p.accent};
}}
QSlider::handle:horizontal:disabled {{
    border-color: {p.border_strong};
    background: {p.window};
}}
QSlider::sub-page:horizontal:disabled {{
    background: {p.border_strong};
}}

/* ---------- 复选框 ---------- */
QCheckBox, QRadioButton {{
    background: transparent;
    spacing: 8px;
    color: {p.text};
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {p.border_strong};
    border-radius: 4px;
    background: {p.surface_alt};
}}
QRadioButton::indicator {{
    border-radius: 8px;
}}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {p.accent};
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {p.accent};
    border-color: {p.accent};
    image: none;
}}

/* ---------- 进度条 ---------- */
QProgressBar {{
    background: {p.track};
    border: none;
    border-radius: 5px;
    height: 10px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: {p.accent};
    border-radius: 5px;
}}

/* ---------- 分组框 ---------- */
QGroupBox {{
    background: transparent;
    border: 1px solid {p.border};
    border-radius: 10px;
    margin-top: 14px;
    padding-top: 8px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {p.text_muted};
    font-weight: 600;
}}

/* ---------- 滚动区 ---------- */
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {p.border_strong};
    border-radius: 4px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.text_faint}; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {p.border_strong};
    border-radius: 4px;
    min-width: 28px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- 下拉菜单 ---------- */
QMenu {{
    background: {p.surface};
    color: {p.text};
    border: 1px solid {p.border_strong};
    border-radius: 8px;
    padding: 6px;
}}
QMenu::item {{
    padding: 6px 22px 6px 12px;
    border-radius: 6px;
}}
QMenu::item:selected {{
    background: {p.accent};
    color: {p.accent_text};
}}
QMenu::separator {{
    height: 1px;
    background: {p.border};
    margin: 4px 8px;
}}

/* ---------- 日志 ---------- */
QPlainTextEdit#Log {{
    background: {p.surface_alt};
    color: {p.text_muted};
    border: 1px solid {p.border};
    border-radius: 8px;
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 12px;
}}

/* ---------- 标签页 ---------- */
QTabWidget::pane {{
    border: 1px solid {p.border};
    border-radius: 10px;
    background: {p.surface};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {p.text_muted};
    padding: 8px 16px;
    border: 1px solid transparent;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
}}
QTabBar::tab:selected {{
    color: {p.text};
    background: {p.surface};
    border-color: {p.border};
    border-bottom-color: {p.surface};
    font-weight: 600;
}}
QTabBar::tab:hover:!selected {{
    color: {p.text};
}}
"""


def _system_is_dark(app: QApplication) -> bool:
    """读取系统当前是否为深色。Qt 6.5+ 优先；否则看调色板亮度。"""
    try:
        hints = app.styleHints()
        scheme = hints.colorScheme() if hasattr(hints, "colorScheme") else None
        if scheme is not None:
            if scheme == Qt.ColorScheme.Dark:
                return True
            if scheme == Qt.ColorScheme.Light:
                return False
    except Exception as exc:  # noqa: BLE001 - 旧版 Qt 没有该 API
        log.debug("colorScheme 不可用：%s", exc)
    palette = app.palette()
    window = palette.color(QPalette.ColorRole.Window)
    return window.lightness() < 128


def detect_system_theme(app: QApplication) -> str:
    return THEME_DARK if _system_is_dark(app) else THEME_LIGHT


class ThemeManager(QObject):
    """应用/切换主题，并在「跟随系统」时监听系统变化。"""

    themeChanged = Signal(str)  # 实际生效的主题名（light/dark）

    def __init__(self, app: QApplication, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._app = app
        self._mode = THEME_SYSTEM
        self._effective = THEME_LIGHT
        self._listening = False

    # ---- 状态 -----------------------------------------------------------
    @property
    def mode(self) -> str:
        return self._mode

    @property
    def effective(self) -> str:
        return self._effective

    @property
    def palette(self) -> Palette:
        return PALETTES[self._effective]

    # ---- 应用 -----------------------------------------------------------
    def apply(self, mode: str) -> str:
        """应用主题模式（system/light/dark），返回实际生效主题。"""
        mode = mode if mode in (THEME_SYSTEM, THEME_LIGHT, THEME_DARK) else THEME_SYSTEM
        self._mode = mode
        target = detect_system_theme(self._app) if mode == THEME_SYSTEM else mode
        self._apply_palette(PALETTES[target])
        self._listen_system(mode == THEME_SYSTEM)
        if target != self._effective:
            self._effective = target
        self._apply_native_hints(target == THEME_DARK)
        self.themeChanged.emit(target)
        return target

    def refresh_system(self) -> None:
        """系统主题变化时重算（仅在跟随系统模式下有效）。"""
        if self._mode != THEME_SYSTEM:
            return
        target = detect_system_theme(self._app)
        if target == self._effective:
            return
        log.info("系统主题变化 → %s", target)
        self._effective = target
        self._apply_palette(PALETTES[target])
        self._apply_native_hints(target == THEME_DARK)
        self.themeChanged.emit(target)

    # ---- 内部 -----------------------------------------------------------
    def _apply_palette(self, palette: Palette) -> None:
        self._effective = palette.name
        self._app.setStyleSheet(build_qss(palette))
        qt_palette = QPalette()
        qt_palette.setColor(QPalette.ColorRole.Window, QColor(palette.window))
        qt_palette.setColor(QPalette.ColorRole.WindowText, QColor(palette.text))
        qt_palette.setColor(QPalette.ColorRole.Base, QColor(palette.surface))
        qt_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(palette.surface_alt))
        qt_palette.setColor(QPalette.ColorRole.Text, QColor(palette.text))
        qt_palette.setColor(QPalette.ColorRole.Button, QColor(palette.surface_alt))
        qt_palette.setColor(QPalette.ColorRole.ButtonText, QColor(palette.text))
        qt_palette.setColor(QPalette.ColorRole.Highlight, QColor(palette.accent))
        qt_palette.setColor(QPalette.ColorRole.HighlightedText, QColor(palette.accent_text))
        qt_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(palette.surface_alt))
        qt_palette.setColor(QPalette.ColorRole.ToolTipText, QColor(palette.text))
        qt_palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(palette.text_faint))
        self._app.setPalette(qt_palette)

    def _apply_native_hints(self, dark: bool) -> None:
        """让标题栏 / 原生控件也跟随深浅色（Windows 10 1809+ / 11）。"""
        try:
            self._app.styleHints().setColorScheme(
                Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light
            )
        except Exception:  # noqa: BLE001 - Qt < 6.5
            pass

    def _listen_system(self, enable: bool) -> None:
        if enable == self._listening:
            return
        hints = self._app.styleHints()
        if not hasattr(hints, "colorSchemeChanged"):
            return
        try:
            if enable:
                hints.colorSchemeChanged.connect(self._on_system_scheme_changed)
            else:
                hints.colorSchemeChanged.disconnect(self._on_system_scheme_changed)
        except (RuntimeError, TypeError) as exc:  # noqa: BLE001
            log.debug("系统主题监听切换失败：%s", exc)
        else:
            self._listening = enable

    def _on_system_scheme_changed(self, *_args) -> None:
        self.refresh_system()


def pick_ui_font() -> QFont:
    """挑一个在中文 Windows 上观感稳定的字体。"""
    families = set(QFontDatabase.families())
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI Variable Text", "Segoe UI", "Noto Sans SC"):
        if name in families:
            font = QFont(name)
            font.setPointSizeF(9.5)
            return font
    font = QFont()
    font.setPointSizeF(9.5)
    return font
