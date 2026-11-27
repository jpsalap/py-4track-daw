"""Shared PySide6 base dialog and precision float slider for Mini-DAW DSP effect popups."""

import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)


class FloatSlider(QSlider):
    """Horizontal QSlider mapped to floating-point values with double-click default reset."""

    floatValueChanged = Signal(float)

    def __init__(self, min_val, max_val, step, default_val, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._min = float(min_val)
        self._max = float(max_val)
        self._step = float(step)
        self._default = float(default_val)
        self._steps = max(1, int(round((self._max - self._min) / self._step)))

        self.setRange(0, self._steps)
        self.setSingleStep(1)
        self.setPageStep(max(1, self._steps // 10))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Drag to adjust · Double-click to reset to default")
        self._dragging = False
        self.valueChanged.connect(self._emit_float)

    def _x_to_step(self, x):
        margin = 8.0
        span = max(1.0, float(self.width()) - 2.0 * margin)
        frac = max(0.0, min(1.0, (float(x) - margin) / span))
        return int(round(frac * self._steps))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self.setValue(self._x_to_step(event.position().x()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.setValue(self._x_to_step(event.position().x()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.setValue(self._x_to_step(event.position().x()))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _to_int(self, val):
        clamped = max(self._min, min(self._max, float(val)))
        return int(round((clamped - self._min) / self._step))

    def _to_float(self, iv):
        val = self._min + iv * self._step
        return max(self._min, min(self._max, val))

    def _emit_float(self, iv):
        self.floatValueChanged.emit(self._to_float(iv))

    def value_float(self):
        return self._to_float(self.value())

    def set_value_float(self, val):
        self.setValue(self._to_int(val))

    def mouseDoubleClickEvent(self, event):
        self.set_value_float(self._default)
        event.accept()


class EffectDialogBase(QDialog):
    """Non-blocking dark-studio Qt6 parameter popup shared by all Mini-DAW DSP effects."""

    def __init__(
        self,
        title,
        bg_dark,
        bg_header,
        bg_well,
        accent,
        btn_reset_bg,
        btn_reset_hover,
        btn_close_bg,
        btn_close_hover,
        parent=None,
        on_close=None,
        width=350,
    ):
        self._standalone = parent is None
        self._owns_app = False
        if self._standalone and QApplication.instance() is None:
            self._app = QApplication(sys.argv)
            self._owns_app = True
        else:
            self._app = QApplication.instance()

        super().__init__(parent)
        self.on_close_callback = on_close
        self._is_open = True

        self.setWindowTitle(title)
        self.setMinimumWidth(width)
        self.setMaximumWidth(width + 80)
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        self.setStyleSheet(
            f"""
            QDialog, QWidget#fxBody {{
                background-color: {bg_dark};
                color: #f0f6fc;
            }}
            QFrame#fxHeader {{
                background-color: {bg_header};
                border-bottom: 1px solid {bg_well};
            }}
            QLabel#fxTitle {{
                background-color: transparent;
                color: {accent};
                font-size: 12px;
                font-weight: 700;
                letter-spacing: 0.8px;
            }}
            QLabel.paramName {{
                background-color: transparent;
                color: #dbe9f5;
                font-size: 11px;
                font-weight: 600;
            }}
            QLabel.paramVal {{
                color: {accent};
                background-color: {bg_well};
                border: 1px solid rgba(255, 255, 255, 26);
                border-radius: 4px;
                padding: 2px 7px;
                font-size: 11px;
                font-weight: 700;
            }}
            QSlider {{
                background-color: transparent;
            }}
            QSlider::groove:horizontal {{
                height: 6px;
                background: {bg_well};
                border-radius: 3px;
            }}
            QSlider::sub-page:horizontal {{
                background: {accent};
                border-radius: 3px;
            }}
            QSlider::handle:horizontal {{
                background: #f0f6fc;
                border: 2px solid {accent};
                width: 14px;
                height: 14px;
                margin: -5px 0;
                border-radius: 8px;
            }}
            QSlider::handle:horizontal:hover {{
                background: #ffffff;
            }}
            QPushButton#btnReset {{
                background-color: {btn_reset_bg};
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 28);
                border-radius: 5px;
                padding: 6px 16px;
                font-size: 11px;
                font-weight: 700;
            }}
            QPushButton#btnReset:hover {{
                background-color: {btn_reset_hover};
            }}
            QPushButton#btnClose {{
                background-color: {btn_close_bg};
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 36);
                border-radius: 5px;
                padding: 6px 16px;
                font-size: 11px;
                font-weight: 700;
            }}
            QPushButton#btnClose:hover {{
                background-color: {btn_close_hover};
            }}
            """
        )

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        header = QFrame(self)
        header.setObjectName("fxHeader")
        header.setFixedHeight(36)
        hdr_layout = QHBoxLayout(header)
        hdr_layout.setContentsMargins(14, 0, 14, 0)
        title_lbl = QLabel(title.upper(), header)
        title_lbl.setObjectName("fxTitle")
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hdr_layout.addWidget(title_lbl)
        root_layout.addWidget(header)

        body = QWidget(self)
        body.setObjectName("fxBody")
        self._body_layout = QVBoxLayout(body)
        self._body_layout.setContentsMargins(16, 14, 16, 14)
        self._body_layout.setSpacing(10)
        root_layout.addWidget(body, 1)

        self._params_layout = QVBoxLayout()
        self._params_layout.setSpacing(10)
        self._body_layout.addLayout(self._params_layout)
        self._body_layout.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 6, 0, 0)
        self.reset_btn = QPushButton("Reset", self)
        self.reset_btn.setObjectName("btnReset")
        self.reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn = QPushButton("Close", self)
        self.close_btn.setObjectName("btnClose")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.close)
        btn_row.addWidget(self.reset_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.close_btn)
        self._body_layout.addLayout(btn_row)

        self._mono_font = QFont("Menlo", 10)
        self._mono_font.setStyleHint(QFont.StyleHint.Monospace)
        self._mono_font.setBold(True)

    def add_param_slider(
        self,
        name,
        min_val,
        max_val,
        step,
        initial_val,
        default_val,
        formatter,
        on_change,
    ):
        """Add a labeled parameter slider row with tabular numeric readout."""
        block = QVBoxLayout()
        block.setSpacing(4)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        name_lbl = QLabel(name, self)
        name_lbl.setProperty("class", "paramName")
        val_lbl = QLabel(formatter(initial_val), self)
        val_lbl.setProperty("class", "paramVal")
        val_lbl.setFont(self._mono_font)
        val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(name_lbl)
        top.addStretch(1)
        top.addWidget(val_lbl)
        block.addLayout(top)

        slider = FloatSlider(min_val, max_val, step, default_val, self)
        slider.set_value_float(initial_val)

        def _handle(val):
            on_change(val)
            val_lbl.setText(formatter(val))

        slider.floatValueChanged.connect(_handle)
        block.addWidget(slider)
        self._params_layout.addLayout(block)
        return slider, val_lbl

    def finalize_and_show(self):
        self.adjustSize()
        self.setFixedHeight(self.sizeHint().height())
        self.show()
        if self._standalone and self._owns_app and self._app is not None:
            self._app.exec()

    def focus(self):
        if self.is_open():
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def is_open(self):
        return bool(self._is_open and self.isVisible())

    def _notify_close(self):
        self._is_open = False
        if self.on_close_callback is not None:
            cb = self.on_close_callback
            self.on_close_callback = None
            cb()

    def reject(self):
        """Ensure pressing Escape closes the dialog cleanly and runs on_close_callback."""
        self.close()

    def closeEvent(self, event):
        self._notify_close()
        super().closeEvent(event)
