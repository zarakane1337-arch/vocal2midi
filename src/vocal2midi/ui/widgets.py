"""可复用控件。

    Card            圆角卡片容器（统一内边距与标题）
    ParamRow        一行参数控件：滑块 + 可编辑输入框，双向同步
    SectionHeader   小标题
    DropArea        拖放接收区（由主窗口复用）
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QDoubleValidator, QIntValidator
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..core.params import ParamSpec


class Card(QFrame):
    """圆角卡片：可带标题与说明，内容放进 `body` 布局。"""

    def __init__(
        self,
        title: str = "",
        hint: str = "",
        parent: QWidget | None = None,
        spacing: int = 10,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(16, 14, 16, 14)
        self._layout.setSpacing(spacing)

        self.title_label: QLabel | None = None
        self.hint_label: QLabel | None = None
        self._header: QHBoxLayout | None = None
        if title:
            self._ensure_header()
            assert self._header is not None
            self.title_label = QLabel(title)
            self.title_label.setObjectName("CardTitle")
            self._header.insertWidget(0, self.title_label)
        if hint:
            self.hint_label = QLabel(hint)
            self.hint_label.setObjectName("CardHint")
            self.hint_label.setWordWrap(True)
            self._layout.addWidget(self.hint_label)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(spacing)
        self._layout.addLayout(self.body)

    def _ensure_header(self) -> QHBoxLayout:
        """延迟创建标题行；`add_header_widget` 在没有标题时也能长出一行。"""
        if self._header is not None:
            return self._header
        header = QHBoxLayout()
        header.setSpacing(8)
        # 插到 hint/body 之前
        self._layout.insertLayout(0, header)
        header.addStretch(1)
        self._header = header
        return header

    def add_header_widget(self, widget: QWidget) -> None:
        """把控件放到标题行右侧（例如「恢复默认」按钮）。"""
        header = self._ensure_header()
        header.addWidget(widget)
        header.setStretch(header.count() - 1, 0)


class SectionHeader(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("CardHint")


class ParamRow(QWidget):
    """参数行：名称 + 滑块 + 输入框（可直接键入任意值）。

    滑块与输入框双向同步：拖滑块会刷新文本，键入数字会移动滑块；
    键入超出范围的值会被夹到边界并立即回显，避免出现「看起来生效其实没有」的状态。

    布局是紧凑的单行：名称 + 滑块 + 输入框；外层页面带滚动条，
    所以参数再多也不会被压扁。
    """

    valueChanged = Signal(str, object)  # (key, value)

    def __init__(self, spec: ParamSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        self._value: Any = spec.default
        self._emitting = False

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 3, 0, 3)
        outer.setSpacing(12)

        self.label = QLabel(spec.label)
        self.label.setMinimumWidth(120)
        self.label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        if spec.help:
            self.label.setToolTip(spec.help)
        outer.addWidget(self.label)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, spec.slider_steps())
        self.slider.setPageStep(max(1, spec.slider_steps() // 20))
        self.slider.setSingleStep(1)
        self.slider.setToolTip(spec.help)
        self.slider.setMinimumHeight(22)
        self.slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        outer.addWidget(self.slider, 1)

        self.editor = QLineEdit()
        self.editor.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.editor.setFixedWidth(84)
        self.editor.setMinimumHeight(28)
        self.editor.setToolTip(f"{spec.help}\n可直接输入数值（{spec.minimum:g} ~ {spec.maximum:g}{spec.unit}）".strip())
        if spec.integer:
            validator = QIntValidator(int(spec.minimum), int(spec.maximum), self.editor)
        else:
            validator = QDoubleValidator(float(spec.minimum), float(spec.maximum), max(2, spec.decimals), self.editor)
            validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        self.editor.setValidator(validator)
        outer.addWidget(self.editor)

        self.unit = QLabel(spec.unit)
        self.unit.setObjectName("Faint")
        self.unit.setFixedWidth(40)
        outer.addWidget(self.unit)

        self.slider.valueChanged.connect(self._on_slider)
        self.editor.editingFinished.connect(self._on_editor_commit)
        self.set_value(spec.default, emit=False)

    # ---- 取值 -----------------------------------------------------------
    @property
    def value(self) -> Any:
        return self._value

    def set_value(self, value: Any, emit: bool = True) -> None:
        try:
            coerced = self.spec.coerce(value)
        except (TypeError, ValueError):
            coerced = self.spec.default
        coerced = self._clamp(coerced)
        changed = coerced != self._value
        self._value = coerced

        self.editor.blockSignals(True)
        self.editor.setText(self.spec.format(coerced))
        self.editor.blockSignals(False)

        self.slider.blockSignals(True)
        self.slider.setValue(self.spec.to_slider(coerced))
        self.slider.blockSignals(False)

        self._mark_invalid(False)
        if changed and emit:
            self._emit()

    def reset(self) -> None:
        self.set_value(self.spec.default)

    def _clamp(self, value: Any) -> Any:
        if self.spec.choices:
            return value
        return self.spec.from_slider(self.spec.to_slider(self.spec.clamp(float(value))))

    def _emit(self) -> None:
        if self._emitting:
            return
        self._emitting = True
        try:
            self.valueChanged.emit(self.spec.key, self._value)
        finally:
            self._emitting = False

    # ---- 事件 -----------------------------------------------------------
    def _on_slider(self, steps: int) -> None:
        self.set_value(self.spec.from_slider(steps))

    def _on_editor_commit(self) -> None:
        text = self.editor.text().strip().replace("，", ".").replace(",", ".")
        if not text:
            self.set_value(self._value, emit=False)
            return
        try:
            parsed = float(text)
        except ValueError:
            self._mark_invalid(True)
            self.set_value(self._value, emit=False)
            return
        clamped = max(float(self.spec.minimum), min(float(self.spec.maximum), parsed))
        self.set_value(clamped)
        if clamped != parsed:
            self._mark_invalid(True)

    def _mark_invalid(self, invalid: bool) -> None:
        self.editor.setProperty("invalid", "true" if invalid else "false")
        self.editor.style().unpolish(self.editor)
        self.editor.style().polish(self.editor)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        return QSize(420, 34)


class ChoiceRow(QWidget):
    """枚举参数行：标签 + 下拉框（用于量化等单位不连续的参数）。"""

    valueChanged = Signal(str, object)

    def __init__(self, spec: ParamSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QComboBox

        self.spec = spec
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(8)
        label = QLabel(spec.label)
        label.setMinimumWidth(96)
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        label.setToolTip(spec.help)
        layout.addWidget(label, 1)
        self.combo = QComboBox()
        for choice in spec.choices:
            self.combo.addItem(choice)
        self.combo.setToolTip(spec.help)
        self.combo.setFixedWidth(132)
        layout.addWidget(self.combo, 0)
        self.combo.currentTextChanged.connect(lambda text: self.valueChanged.emit(spec.key, text))

    def set_value(self, value: Any) -> None:
        text = str(value)
        index = self.combo.findText(text)
        if index >= 0:
            self.combo.blockSignals(True)
            self.combo.setCurrentIndex(index)
            self.combo.blockSignals(False)

    @property
    def value(self) -> Any:
        return self.combo.currentText()


def scroll_page(content: QWidget = None) -> QScrollArea:  # type: ignore[assignment]
    """把页面内容包进可滚动区域，窗口变矮时参数不会被压扁。"""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setObjectName("ScrollPage")
    if content is not None:
        area.setWidget(content)
    return area


def make_path_label(path_text: str, elide: int = 60) -> str:
    """中间省略的长路径，避免撑破布局。"""
    if len(path_text) <= elide:
        return path_text
    head = elide // 2 - 2
    tail = elide - head - 3
    return f"{path_text[:head]}…{path_text[-tail:]}"


def spinbox_style(widget: QAbstractSpinBox) -> None:  # pragma: no cover - 保留扩展点
    widget.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
