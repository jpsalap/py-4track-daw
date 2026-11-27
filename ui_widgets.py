"""High-performance custom PySide6 DAW widgets for Mini-DAW:
- ChannelMeter: Fast cached-geometry segmented LED dBFS VU meter with peak hold (30 FPS)
- PeakReadout: Custom-painted tabular monospace dBFS peak / clip badge (zero QSS churn)
- DawFader & FaderWithScale: Custom-painted studio console fader with aligned dB ticks
- PanSlider: Center-detented bipolar stereo pan slider
- InsertSlotWidget: Custom-painted DAW effect insert slot (LED bypass + ⚙ editor, zero QSS churn)
- TogglePadButton: Custom-painted color-coded Solo / Mute / Phase / Rec pad (pixel-locked alignment)
- TransportButton: Vector-painted Stop / Play / Pause / Rec transport pad
- ChannelFileWell: Clickable + drag-and-drop WAV file bar with elided filename & clear button
- WaveformTimeline: QPixmap-cached 4-track Peak+RMS arrange view (O(1) playhead blit, zoom & drop)
"""

import math
import os
import time

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollBar,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

MIN_FADER_DB = -60.0
MAX_FADER_DB = 6.0


def format_timecode(seconds):
    """Format seconds as MM:SS."""
    sec_int = max(0, int(seconds))
    mins, secs = divmod(sec_int, 60)
    return f"{mins:02d}:{secs:02d}"


def format_ruler_time(seconds, step_sec):
    """Format timeline ruler label, adapting precision from whole seconds down to 10 ms when zoomed in."""
    sec = max(0.0, float(seconds))
    if step_sec < 0.1:
        mins = int(sec // 60)
        rem = sec - mins * 60
        return f"{mins:02d}:{rem:05.2f}"
    if step_sec < 1.0:
        mins = int(sec // 60)
        rem = sec - mins * 60
        return f"{mins:02d}:{rem:04.1f}"
    return format_timecode(sec)


def format_peak_db(linear_peak):
    """Format a linear sample peak as dBFS (e.g. '-11.3 dB', '+0.1 dB', '-∞ dB')."""
    val = float(linear_peak)
    if val <= 1e-4:
        return "-∞ dB"
    db = 20.0 * math.log10(val)
    if db <= -60.0:
        return "-∞ dB"
    if db >= 0.0:
        return f"+{db:.1f} dB" if db >= 0.05 else "0.0 dB"
    return f"{db:.1f} dB"


def _mono_font(size=9, bold=True):
    f = QFont("Menlo", size)
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setBold(bold)
    return f


def _extract_dropped_wav(mime_data):
    """Return the first local .wav file path from a QMimeData drop payload, or None."""
    if not mime_data.hasUrls():
        return None
    for url in mime_data.urls():
        if url.isLocalFile():
            path = url.toLocalFile()
            if path.lower().endswith(".wav") and os.path.isfile(path):
                return path
    return None


class ChannelMeter(QWidget):
    """Fast segmented vertical LED dBFS level meter with peak-hold indicator (30 FPS)."""

    def __init__(self, parent=None, width=16, height=154, segments=22):
        super().__init__(parent)
        self._segments = int(segments)
        self._min_w = int(width)
        self._min_h = int(height)
        self.setMinimumSize(self._min_w, max(90, self._min_h - 30))
        self.setFixedWidth(self._min_w)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

        self._lit_colors = []
        self._dim_colors = []
        self._clip_idx = int(self._segments * 0.88)
        for idx in range(self._segments):
            frac = (idx + 1) / float(self._segments)
            if frac >= 0.88:
                self._lit_colors.append(QColor("#f24957"))
                self._dim_colors.append(QColor("#361920"))
            elif frac >= 0.70:
                self._lit_colors.append(QColor("#f5c542"))
                self._dim_colors.append(QColor("#332d18"))
            else:
                self._lit_colors.append(QColor("#2ecc71"))
                self._dim_colors.append(QColor("#163026"))

        self._bg_col = QColor("#0e1924")
        self._border_pen = QPen(QColor("#36536e"), 1)
        self._peak_clip_col = QColor("#ff7a85")
        self._peak_norm_col = QColor("#eef7ff")

        self._seg_rects = []
        self._displayed_level = 0.0
        self._peak_hold = 0.0
        self._hold_ticks = 0
        self._lit_count = 0
        self._peak_seg = -1

    def sizeHint(self):
        return QSize(self._min_w, self._min_h)

    def _rebuild_rects(self):
        w = self.width()
        h = self.height()
        pad_x = 3
        pad_y = 3
        usable_h = max(10.0, float(h - 2 * pad_y))
        step = usable_h / float(self._segments)
        gap = max(1.0, min(1.8, step * 0.22))
        seg_w = max(2, w - 2 * pad_x)
        rects = []
        for idx in range(self._segments):
            y_bot = h - pad_y - idx * step
            y_top = y_bot - step + gap
            rects.append(QRect(pad_x, int(round(y_top)), seg_w, max(1, int(round(y_bot - y_top)))))
        self._seg_rects = rects

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rebuild_rects()

    @staticmethod
    def linear_to_norm(linear_peak):
        """Convert linear amplitude peak to normalized [0.0, 1.0] on a -60..0 dBFS scale."""
        if linear_peak <= 1e-4:
            return 0.0
        db = 20.0 * math.log10(linear_peak)
        if db <= -60.0:
            return 0.0
        if db >= 0.0:
            return 1.0
        return (db + 60.0) / 60.0

    def update_level(self, linear_level):
        """Update LED segments with smooth ~300 ms VU ballistics and peak hold (30 FPS)."""
        target = self.linear_to_norm(linear_level)

        if target >= self._displayed_level:
            self._displayed_level += 0.24 * (target - self._displayed_level)
        else:
            self._displayed_level += 0.18 * (target - self._displayed_level)

        if self._displayed_level < 0.008 and target == 0.0:
            self._displayed_level = 0.0

        # Peak-hold indicator (~0.8 s hold at 30 FPS)
        if target >= self._peak_hold:
            self._peak_hold = target
            self._hold_ticks = 24
        elif self._hold_ticks > 0:
            self._hold_ticks -= 1
        else:
            self._peak_hold *= 0.92
            if self._peak_hold < 0.01:
                self._peak_hold = 0.0

        new_lit = min(self._segments, int(round(self._displayed_level * self._segments)))
        new_peak = (
            min(self._segments - 1, int(round(self._peak_hold * self._segments)) - 1)
            if self._peak_hold > 0.02
            else -1
        )

        if new_lit != self._lit_count or new_peak != self._peak_seg:
            self._lit_count = new_lit
            self._peak_seg = new_peak
            self.update()

    def reset(self):
        self._displayed_level = 0.0
        self._peak_hold = 0.0
        self._hold_ticks = 0
        self._lit_count = 0
        self._peak_seg = -1
        self.update()

    def paintEvent(self, _event):
        if not self._seg_rects:
            self._rebuild_rects()
        p = QPainter(self)
        w = self.width()
        h = self.height()
        p.fillRect(0, 0, w, h, self._bg_col)
        p.setPen(self._border_pen)
        p.drawRect(0, 0, w - 1, h - 1)

        lit_count = self._lit_count
        peak_seg = self._peak_seg
        clip_idx = self._clip_idx
        lit_colors = self._lit_colors
        dim_colors = self._dim_colors

        for idx, rect in enumerate(self._seg_rects):
            if idx < lit_count:
                p.fillRect(rect, lit_colors[idx])
            elif idx == peak_seg:
                p.fillRect(rect, self._peak_clip_col if idx >= clip_idx else self._peak_norm_col)
            else:
                p.fillRect(rect, dim_colors[idx])


class PeakReadout(QWidget):
    """Custom-painted tabular-monospace dBFS peak indicator (2.5s auto-clear & clip alert, zero QSS churn)."""

    HOLD_SECONDS = 2.5

    _STATE_COLORS = {
        "idle": (QColor("#12202d"), QColor("#6886a3"), QColor("#2c465e")),
        "normal": (QColor("#12202d"), QColor("#9be7c4"), QColor("#345470")),
        "hot": (QColor("#12202d"), QColor("#ffd166"), QColor("#8a6f28")),
        "clip": (QColor("#b82530"), QColor("#ffffff"), QColor("#ff858f")),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._held_peak = 0.0
        self._hold_until = 0.0
        self._last_text = "-∞ dB"
        self._last_state = "idle"
        self._font = _mono_font(9, bold=True)

        self.setFixedHeight(22)
        self.setMinimumWidth(56)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("True sample peak (dBFS) · Click to clear clip/peak hold")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clear()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.mousePressEvent(event)

    def update_peak(self, linear_peak, now=None):
        """Feed latest true sample peak; holds highest peak for 2.5s then auto-clears."""
        if now is None:
            now = time.monotonic()

        pk = float(linear_peak)
        if pk >= self._held_peak and pk > 1e-4:
            self._held_peak = pk
            self._hold_until = now + self.HOLD_SECONDS
        elif now >= self._hold_until:
            if pk > 1e-4:
                self._held_peak = pk
                self._hold_until = now + self.HOLD_SECONDS
            else:
                self._held_peak = 0.0

        self._render()

    def clear(self):
        self._held_peak = 0.0
        self._hold_until = 0.0
        self._render()

    def _render(self):
        pk = self._held_peak
        txt = format_peak_db(pk)
        if pk <= 1e-4 or txt == "-∞ dB":
            state = "idle"
        elif pk >= 0.999:
            state = "clip"
        else:
            db = 20.0 * math.log10(pk)
            state = "hot" if db >= -3.0 else "normal"

        if txt != self._last_text or state != self._last_state:
            self._last_text = txt
            self._last_state = state
            self.update()

    def paintEvent(self, _event):
        bg, fg, bd = self._STATE_COLORS[self._last_state]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        p.setPen(QPen(bd, 1.0))
        p.setBrush(bg)
        p.drawRoundedRect(r, 4.0, 4.0)
        p.setFont(self._font)
        p.setPen(fg)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self._last_text)


class FaderBadge(QWidget):
    """Custom-painted tabular-monospace fader dB badge (zero QSS churn while dragging faders)."""

    _FG_COLORS = {
        "muted": QColor("#9cb8d1"),
        "boost": QColor("#fc8181"),
        "unity": QColor("#ffd166"),
        "normal": QColor("#f0f6fc"),
    }

    def __init__(self, initial_text="0.0 dB", parent=None):
        super().__init__(parent)
        self._text = initial_text
        self._state = "unity"
        self._bg = QColor("#14222f")
        self._bd = QColor("#2c465e")
        self._font = _mono_font(9, bold=True)
        self.setFixedHeight(22)
        self.setMinimumWidth(56)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_db_display(self, text, db):
        if db <= MIN_FADER_DB:
            state = "muted"
        elif db > 0.05:
            state = "boost"
        elif abs(db) <= 0.05:
            state = "unity"
        else:
            state = "normal"
        if text != self._text or state != self._state:
            self._text = text
            self._state = state
            self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        p.setPen(QPen(self._bd, 1.0))
        p.setBrush(self._bg)
        p.drawRoundedRect(r, 4.0, 4.0)
        p.setFont(self._font)
        p.setPen(self._FG_COLORS[self._state])
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self._text)


class DawFader(QSlider):
    """Custom-painted vertical studio fader (+6.0 dB to -60 dB / -∞) with direct click/drag & unity reset."""

    dbChanged = Signal(float)

    def __init__(self, accent_color="#4da3ff", parent=None):
        super().__init__(Qt.Orientation.Vertical, parent)
        self._accent = QColor(accent_color)
        self._accent.setAlpha(185)
        self._steps_per_db = 2  # 0.5 dB resolution
        self.setRange(
            int(round(MIN_FADER_DB * self._steps_per_db)),
            int(round(MAX_FADER_DB * self._steps_per_db)),
        )
        self.setSingleStep(1)
        self.setPageStep(6)
        self.setValue(0)
        self.setFixedWidth(28)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("Volume fader (+6.0 to -∞ dB) · Double-click to reset to 0.0 dB unity")
        self._dragging = False
        self.valueChanged.connect(self._emit_db)

    def _emit_db(self, iv):
        self.dbChanged.emit(iv / float(self._steps_per_db))

    def db_value(self):
        return self.value() / float(self._steps_per_db)

    def set_db(self, db):
        clamped = max(MIN_FADER_DB, min(MAX_FADER_DB, float(db)))
        self.setValue(int(round(clamped * self._steps_per_db)))

    def _y_to_db(self, y):
        top_y, bot_y, _ = self._track_bounds()
        frac = max(0.0, min(1.0, (float(y) - top_y) / max(1.0, bot_y - top_y)))
        return MAX_FADER_DB - frac * (MAX_FADER_DB - MIN_FADER_DB)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self.set_db(self._y_to_db(event.position().y()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.set_db(self._y_to_db(event.position().y()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.set_db(self._y_to_db(event.position().y()))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        self._dragging = False
        self.set_db(0.0)
        event.accept()

    def _track_bounds(self):
        cap_h = 22.0
        top_y = cap_h * 0.5 + 2.0
        bot_y = max(top_y + 10.0, float(self.height()) - cap_h * 0.5 - 2.0)
        return top_y, bot_y, cap_h

    def db_to_y(self, db):
        top_y, bot_y, _ = self._track_bounds()
        frac = (MAX_FADER_DB - float(db)) / (MAX_FADER_DB - MIN_FADER_DB)
        frac = max(0.0, min(1.0, frac))
        return top_y + frac * (bot_y - top_y)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = float(self.width())
        cx = w * 0.5
        top_y, bot_y, cap_h = self._track_bounds()

        # Recessed fader track slot
        slot_w = 5.0
        slot_rect = QRectF(cx - slot_w * 0.5, top_y - 3.0, slot_w, (bot_y - top_y) + 6.0)
        p.setPen(QPen(QColor("#36536e"), 1.0))
        p.setBrush(QColor("#0d1721"))
        p.drawRoundedRect(slot_rect, 2.5, 2.5)

        # Unity 0.0 dB gold notch
        unity_y = self.db_to_y(0.0)
        p.setPen(QPen(QColor("#ffd166"), 1.5))
        p.drawLine(QPointF(2.0, unity_y), QPointF(cx - 5.0, unity_y))
        p.drawLine(QPointF(cx + 5.0, unity_y), QPointF(w - 2.0, unity_y))

        # Active level highlight in groove below cap
        db = self.db_value()
        cap_y = self.db_to_y(db)
        if bot_y - cap_y > 1.0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._accent)
            p.drawRoundedRect(
                QRectF(cx - 1.5, cap_y, 3.0, (bot_y + 2.0) - cap_y),
                1.5,
                1.5,
            )

        # Studio console fader cap
        cap_w = w - 4.0
        cap_rect = QRectF(cx - cap_w * 0.5, cap_y - cap_h * 0.5, cap_w, cap_h)
        grad = QLinearGradient(cap_rect.topLeft(), cap_rect.bottomLeft())
        grad.setColorAt(0.0, QColor("#6b8aa6"))
        grad.setColorAt(0.42, QColor("#3c5873"))
        grad.setColorAt(0.50, QColor("#283d52"))
        grad.setColorAt(0.58, QColor("#3c5873"))
        grad.setColorAt(1.0, QColor("#56738e"))

        p.setPen(QPen(QColor("#8cb3d6") if (self._dragging or self.underMouse()) else QColor("#1b2b3b"), 1.1))
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(cap_rect, 3.5, 3.5)

        if abs(db) <= 0.05:
            line_col = QColor("#ffd166")
        elif db > 0.05:
            line_col = QColor("#fc8181")
        else:
            line_col = QColor("#f0f6fc")
        p.setPen(QPen(line_col, 2.0))
        p.drawLine(QPointF(cap_rect.left() + 3.0, cap_y), QPointF(cap_rect.right() - 3.0, cap_y))


class _FaderScaleTicks(QWidget):
    """Responsive painted dB tick marks aligned dynamically to a companion DawFader."""

    def __init__(self, fader, ticks, parent=None):
        super().__init__(parent)
        self._fader = fader
        self._ticks = list(ticks)
        self._font_reg = QFont("Helvetica", 8)
        self._font_bold = QFont("Helvetica", 8)
        self._font_bold.setBold(True)
        self.setFixedWidth(28)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        for tick_db, tick_txt in self._ticks:
            y = self._fader.db_to_y(tick_db)
            is_unity = tick_db == 0.0
            col = QColor("#ffd166") if is_unity else QColor("#8eaac4")
            p.setPen(QPen(col, 1.2 if is_unity else 1.0))
            p.drawLine(QPointF(0.0, y), QPointF(4.0, y))
            p.setFont(self._font_bold if is_unity else self._font_reg)
            p.drawText(
                QRectF(6.0, y - 6.0, self.width() - 6.0, 12.0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                tick_txt,
            )


class FaderWithScale(QWidget):
    """Combined DawFader + aligned painted dB scale."""

    def __init__(self, ticks, accent_color="#4da3ff", parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.fader = DawFader(accent_color=accent_color, parent=self)
        self._ticks = _FaderScaleTicks(self.fader, ticks, parent=self)
        layout.addWidget(self.fader)
        layout.addWidget(self._ticks)


class PanSlider(QSlider):
    """Center-detented bipolar stereo pan slider (0.0 = L, 0.5 = C, 1.0 = R)."""

    panChanged = Signal(float)

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setRange(0, 100)
        self.setValue(50)
        self.setFixedHeight(20)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("Stereo Pan (L / C / R) · Double-click to reset to Center")
        self._dragging = False
        self.valueChanged.connect(lambda iv: self.panChanged.emit(iv / 100.0))

    def pan_value(self):
        return self.value() / 100.0

    def set_pan(self, val):
        clamped = max(0.0, min(1.0, float(val)))
        self.setValue(int(round(clamped * 100)))

    def _x_to_pan(self, x):
        margin = 7.0
        span = max(1.0, float(self.width()) - 2.0 * margin)
        return max(0.0, min(1.0, (float(x) - margin) / span))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self.set_pan(self._x_to_pan(event.position().x()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.set_pan(self._x_to_pan(event.position().x()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.set_pan(self._x_to_pan(event.position().x()))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        self._dragging = False
        self.set_pan(0.5)
        event.accept()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = float(self.width())
        h = float(self.height())
        cy = h * 0.5
        margin = 7.0
        span = max(10.0, w - 2.0 * margin)
        mid_x = margin + span * 0.5
        val_x = margin + (self.value() / 100.0) * span

        p.setPen(QPen(QColor("#36536e"), 1.0))
        p.setBrush(QColor("#111e2b"))
        p.drawRoundedRect(QRectF(margin, cy - 3.0, span, 6.0), 3.0, 3.0)

        p.setPen(QPen(QColor("#7899b8"), 1.2))
        p.drawLine(QPointF(mid_x, cy - 6.0), QPointF(mid_x, cy + 6.0))

        if abs(val_x - mid_x) > 1.0:
            x0 = min(mid_x, val_x)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#5aa9f5"))
            p.drawRoundedRect(QRectF(x0, cy - 2.0, abs(val_x - mid_x), 4.0), 2.0, 2.0)

        is_center = abs(self.value() - 50) <= 1
        p.setPen(QPen(QColor("#ffd166") if is_center else QColor("#8bc5fa"), 1.5))
        p.setBrush(QColor("#e8f2fc"))
        p.drawEllipse(QPointF(val_x, cy), 5.5, 5.5)


class InsertSlotWidget(QWidget):
    """Custom-painted DAW insert slot: LED+name bypass toggle + ⚙ editor button (zero QSS churn)."""

    toggled = Signal(bool)
    editClicked = Signal()

    EDIT_W = 28

    def __init__(
        self,
        fx_label,
        active_bg,
        led_color,
        inactive_bg,
        hover_bg,
        parent=None,
    ):
        super().__init__(parent)
        self._label = fx_label
        self._active_bg = QColor(active_bg)
        self._led_color = QColor(led_color)
        self._inactive_bg = QColor(inactive_bg)
        self._hover_bg = QColor(hover_bg)
        self._dim_led = QColor("#4c6780")
        self._dim_border = QColor("#38546e")
        self._enabled = False
        self._hover_zone = None  # None | "toggle" | "edit"

        self._font = QFont("Helvetica", 10)
        self._font.setBold(True)
        self._gear_font = QFont("Helvetica", 12)

        self.setFixedHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"Click name to bypass/enable {fx_label} · Click ⚙ to edit parameters")

    def is_enabled(self):
        return self._enabled

    def set_enabled(self, enabled):
        val = bool(enabled)
        if val != self._enabled:
            self._enabled = val
            self.update()

    def _zone_at(self, x):
        return "edit" if x >= (self.width() - self.EDIT_W) else "toggle"

    def mouseMoveEvent(self, event):
        zone = self._zone_at(event.position().x())
        if zone != self._hover_zone:
            self._hover_zone = zone
            self.update()

    def leaveEvent(self, _event):
        if self._hover_zone is not None:
            self._hover_zone = None
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            zone = self._zone_at(event.position().x())
            if zone == "edit":
                self.editClicked.emit()
            else:
                self._enabled = not self._enabled
                self.update()
                self.toggled.emit(self._enabled)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.mousePressEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = float(self.width())
        h = float(self.height())
        sep_x = w - self.EDIT_W

        base_bg = self._active_bg if self._enabled else self._inactive_bg
        border_col = self._led_color if self._enabled else self._dim_border

        # Base rounded body
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(base_bg)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), 4.0, 4.0)

        # Hover highlight on toggle or edit half
        if self._hover_zone == "toggle" and not self._enabled:
            p.setBrush(self._hover_bg)
            p.drawRoundedRect(QRectF(1.0, 1.0, sep_x - 1.0, h - 2.0), 3.0, 3.0)
        elif self._hover_zone == "edit":
            p.setBrush(self._hover_bg)
            p.drawRoundedRect(QRectF(sep_x, 1.0, self.EDIT_W - 1.0, h - 2.0), 3.0, 3.0)

        # Outer border & separator
        p.setPen(QPen(border_col, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), 4.0, 4.0)
        p.drawLine(QPointF(sep_x, 3.0), QPointF(sep_x, h - 3.0))

        # Status LED dot
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._led_color if self._enabled else self._dim_led)
        p.drawEllipse(QPointF(11.0, h * 0.5), 3.2, 3.2)

        # Effect name
        p.setFont(self._font)
        p.setPen(QColor("#ffffff") if (self._enabled or self._hover_zone == "toggle") else QColor("#bfd4e8"))
        p.drawText(
            QRectF(20.0, 0.0, sep_x - 22.0, h),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._label,
        )

        # Gear icon
        p.setFont(self._gear_font)
        p.setPen(QColor("#ffffff") if (self._enabled or self._hover_zone == "edit") else QColor("#9cb8d1"))
        p.drawText(QRectF(sep_x, 0.0, self.EDIT_W, h), Qt.AlignmentFlag.AlignCenter, "⚙")


class TogglePadButton(QAbstractButton):
    """Custom-painted DAW toggle switch (Solo / Mute / Phase / Rec) — pixel-locked alignment, zero QSS churn."""

    def __init__(
        self,
        text,
        accent_color,
        off_bg,
        off_hover_bg,
        off_fg,
        off_border,
        on_bg,
        on_fg,
        on_border,
        parent=None,
    ):
        super().__init__(parent)
        self.setText(text)
        self._accent = QColor(accent_color)
        self._off_bg = QColor(off_bg)
        self._off_hover_bg = QColor(off_hover_bg)
        self._off_fg = QColor(off_fg)
        self._off_border = QColor(off_border)
        self._on_bg = QColor(on_bg)
        self._on_fg = QColor(on_fg)
        self._on_border = QColor(on_border)
        self._blink_lo_bg = QColor("#59222a")
        self._blink_lo_fg = QColor("#ffb3ba")
        self._flash_mode = None
        self._hovered = False

        self._font = QFont("Helvetica", 10)
        self._font.setBold(True)

        self.setCheckable(True)
        self.setFixedHeight(27)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggled.connect(self._on_toggled)

    def sizeHint(self):
        return QSize(72, 27)

    def _on_toggled(self, checked):
        if not checked:
            self._flash_mode = None
        self.update()

    def set_flash_mode(self, mode):
        if self._flash_mode != mode:
            self._flash_mode = mode
            self.update()

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = float(self.width())
        h = float(self.height())
        is_on = self.isChecked()

        if is_on and self._flash_mode == "blink_lo":
            bg = self._blink_lo_bg
            fg = self._blink_lo_fg
            border = self._accent
            stripe = self._accent
        elif is_on:
            bg = self._on_bg
            fg = self._on_fg
            border = self._on_border
            stripe = self._on_border
        else:
            bg = self._off_hover_bg if self._hovered else self._off_bg
            fg = QColor("#ffffff") if self._hovered else self._off_fg
            border = self._accent if self._hovered else self._off_border
            stripe = self._accent

        r = QRectF(0.5, 0.5, w - 1.0, h - 1.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(r, 4.0, 4.0)

        # Left color accent stripe
        p.setBrush(stripe)
        p.drawRoundedRect(QRectF(1.0, 1.0, 4.0, h - 2.0), 2.0, 2.0)

        # Crisp border
        p.setPen(QPen(border, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, 4.0, 4.0)

        # Centered label
        p.setFont(self._font)
        p.setPen(fg)
        p.drawText(QRectF(5.0, 0.0, w - 6.0, h), Qt.AlignmentFlag.AlignCenter, self.text())


class TransportButton(QAbstractButton):
    """Crisp anti-aliased vector studio transport pad (Stop / Play / Pause / Rec) with active glow."""

    def __init__(
        self,
        kind,
        caption,
        icon_color,
        hover_icon_color,
        lit_bg=None,
        lit_border=None,
        lit_icon_color="#ffffff",
        parent=None,
    ):
        super().__init__(parent)
        self._kind = kind
        self._caption = caption
        self._icon_color = QColor(icon_color)
        self._hover_icon_color = QColor(hover_icon_color)
        self._lit_bg = QColor(lit_bg) if lit_bg else None
        self._lit_border = QColor(lit_border) if lit_border else None
        self._lit_icon_color = QColor(lit_icon_color)
        self._mode = "off"
        self._hovered = False

        self._cap_font = QFont("Helvetica", 8)
        self._cap_font.setBold(True)

        self.setFixedSize(50, 48)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self):
        return QSize(50, 48)

    def set_mode(self, mode):
        if self._mode != mode:
            self._mode = mode
            self.update()

    def set_lit(self, is_lit):
        self.set_mode("solid" if is_lit else "off")

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = float(self.width())
        h = float(self.height())
        pressed = self.isDown()
        mode = self._mode

        if pressed:
            bg = QColor("#162636")
            bd = QColor("#638db3")
            ic = self._lit_icon_color if mode in ("solid", "blink_hi") else self._hover_icon_color
            tx = QColor("#ffffff")
        elif mode in ("solid", "blink_hi") and self._lit_bg is not None:
            bg = self._lit_bg
            bd = self._lit_border or QColor("#638db3")
            ic = self._lit_icon_color
            tx = QColor("#ffffff")
        elif mode == "blink_lo":
            bg = QColor("#3d1c22")
            bd = QColor("#9e3843")
            ic = self._icon_color
            tx = QColor("#ff9ea6")
        elif self._hovered:
            bg = QColor("#2e4b66")
            bd = QColor("#638db3")
            ic = self._hover_icon_color
            tx = QColor("#f0f6fc")
        else:
            bg = QColor("#23384c")
            bd = QColor("#426382")
            ic = self._icon_color
            tx = QColor("#b9d2e8")

        p.setPen(QPen(bd, 1.2))
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(1.0, 1.0, w - 2.0, h - 2.0), 6.0, 6.0)

        cx = w * 0.5
        cy = 17.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ic)

        if self._kind == "stop":
            p.drawRoundedRect(QRectF(cx - 6.0, cy - 6.0, 12.0, 12.0), 1.5, 1.5)
        elif self._kind == "play":
            tri = QPolygonF(
                [
                    QPointF(cx - 5.5, cy - 7.0),
                    QPointF(cx - 5.5, cy + 7.0),
                    QPointF(cx + 7.0, cy),
                ]
            )
            p.drawPolygon(tri)
        elif self._kind == "pause":
            p.drawRoundedRect(QRectF(cx - 6.0, cy - 6.5, 4.2, 13.0), 1.2, 1.2)
            p.drawRoundedRect(QRectF(cx + 1.8, cy - 6.5, 4.2, 13.0), 1.2, 1.2)
        else:  # "rec"
            p.drawEllipse(QPointF(cx, cy), 6.2, 6.2)

        p.setFont(self._cap_font)
        p.setPen(tx)
        p.drawText(QRectF(0.0, h - 18.0, w, 14.0), Qt.AlignmentFlag.AlignCenter, self._caption)


class _ElidingFileButton(QAbstractButton):
    """Lightweight clickable filename button that automatically elides text cleanly without left clipping."""

    def __init__(self, text="(click or drop .wav)", parent=None):
        super().__init__(parent)
        self._full_text = text
        self._hovered = False
        self._font = QFont("Helvetica", 10)
        self._font.setBold(True)
        self.setFixedHeight(22)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def setText(self, text):
        self._full_text = str(text)
        self.update()

    def text(self):
        return self._full_text

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(0.0, 0.0, self.width(), self.height())
        if self._hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#243a4f"))
            p.drawRoundedRect(r, 3.0, 3.0)
        p.setFont(self._font)
        fm = QFontMetrics(self._font)
        avail = max(12, self.width() - 8)
        elided = fm.elidedText(self._full_text, Qt.TextElideMode.ElideRight, avail)
        p.setPen(QColor("#ffffff") if self._hovered else QColor("#dcecf9"))
        p.drawText(QRectF(4.0, 0.0, avail, self.height()), Qt.AlignmentFlag.AlignCenter, elided)


class ChannelFileWell(QFrame):
    """Clickable + drag-and-drop WAV loader box with auto-eliding filename, format badge, and ✕ clear button."""

    loadClicked = Signal()
    clearClicked = Signal()
    fileDropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setObjectName("fileWell")
        self._drag_hover = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 5, 6, 5)
        layout.setSpacing(2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(4)

        self.file_btn = _ElidingFileButton("(click or drop .wav)", self)
        self.file_btn.setToolTip("Click to browse for a .wav file, or drag & drop one here")
        self.file_btn.clicked.connect(self.loadClicked.emit)

        self.clear_btn = QPushButton("✕", self)
        self.clear_btn.setObjectName("fileClearBtn")
        self.clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.setFixedSize(22, 22)
        self.clear_btn.setToolTip("Clear loaded audio from this channel")
        self.clear_btn.clicked.connect(self.clearClicked.emit)

        top.addWidget(self.file_btn, 1)
        top.addWidget(self.clear_btn)
        layout.addLayout(top)

        self.meta_lbl = QLabel("—", self)
        self.meta_lbl.setObjectName("fileMetaLbl")
        self.meta_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.meta_lbl)

        self._apply_style()

    def set_file_text(self, text):
        self.file_btn.setText(text)

    def set_meta_text(self, text):
        self.meta_lbl.setText(text)

    def _apply_style(self):
        border = "#63b3ed" if self._drag_hover else "#38546e"
        bg = "#1e354a" if self._drag_hover else "#182836"
        self.setStyleSheet(
            f"""
            QFrame#fileWell {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 5px;
            }}
            QPushButton#fileClearBtn {{
                background-color: #261d24;
                color: #b87c85;
                border: 1px solid #4a333b;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 700;
                padding: 0px;
            }}
            QPushButton#fileClearBtn:hover {{
                background-color: #7a2a36;
                color: #ffffff;
                border-color: #ff858f;
            }}
            QLabel#fileMetaLbl {{
                color: #7cc0f7;
                font-size: 10px;
                background: transparent;
                border: none;
            }}
            """
        )

    def dragEnterEvent(self, event):
        if _extract_dropped_wav(event.mimeData()):
            self._drag_hover = True
            self._apply_style()
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, _event):
        self._drag_hover = False
        self._apply_style()

    def dropEvent(self, event):
        self._drag_hover = False
        self._apply_style()
        path = _extract_dropped_wav(event.mimeData())
        if path:
            self.fileDropped.emit(path)
            event.acceptProposedAction()
        else:
            event.ignore()


class _TimelineCanvas(QWidget):
    """QPixmap-cached 4-track waveform arrange canvas: O(1) playhead blit during playback!"""

    seekRequested = Signal(float)
    positionScrubbed = Signal(float)
    zoomChanged = Signal(float)
    detailRequested = Signal(float, float, int)
    fileDroppedOnChannel = Signal(int, str)

    MAX_ZOOM = 50.0

    # (accent_stripe, clip_bg, header_bg, peak_fill, rms_fill)
    TRACK_THEMES = [
        ("#4da3ff", "#152a3f", "#1d3b59", "#3478bd", "#6eb8ff"),  # Ch 1: Sky Blue
        ("#48d18a", "#142f26", "#1b4436", "#2c8f5e", "#6ee3a5"),  # Ch 2: Emerald
        ("#f5c542", "#2f2916", "#453b1c", "#b58d26", "#ffe07d"),  # Ch 3: Amber Gold
        ("#a375ff", "#251c3a", "#352654", "#7049bf", "#bfa0ff"),  # Ch 4: Violet
    ]
    DIM_THEME = ("#586e82", "#17222d", "#1e2c3a", "#364b5e", "#4e677d")
    HEADER_W = 60
    RULER_H = 20
    CLIP_HDR_H = 13
    PAD_RIGHT = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(188)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

        self._duration = 0.0
        self._position = 0.0
        self._last_drawn_px = -999.0
        self._dragging = False
        self._zoom = 1.0
        self._scroll_frac = 0.0

        self._tracks = [None, None, None, None]
        self._track_details = [None, None, None, None]  # (bins, start_sec, end_sec) for zoomed high-res slice
        self._audible = [True, True, True, True]

        self._rec_armed = []
        self._rec_peaks = []
        self._rec_elapsed = 0.0
        self._rec_active = False

        self._drop_hover_ch = -1

        # Offscreen Retina cache of static ruler + lanes + all 4 waveforms
        self._static_cache = None
        self._cache_dpr = 0.0
        self._cache_dirty = True

        # Debounced high-res zoomed waveform detail request
        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.setInterval(55)
        self._detail_timer.timeout.connect(self._emit_detail_request)

    def _schedule_detail_refresh(self):
        if self._zoom > 1.5 and self._duration > 0.0 and any(t is not None for t in self._tracks):
            self._detail_timer.start()
        else:
            self._detail_timer.stop()
            self._track_details = [None, None, None, None]

    def _emit_detail_request(self):
        if self._zoom <= 1.5 or self._duration <= 0.0:
            return
        start_sec, end_sec, _ = self._visible_window()
        if end_sec > start_sec:
            target_bins = max(400, int(self._lane_span() * 1.25))
            self.detailRequested.emit(start_sec, end_sec, target_bins)

    def invalidate_cache(self):
        self._cache_dirty = True
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._cache_dirty = True

    def _effective_duration(self):
        if self._rec_active:
            return max(self._duration, self._rec_elapsed, 8.0)
        return self._duration

    def _visible_window(self):
        eff = self._effective_duration()
        if eff <= 0.0:
            return 0.0, 0.0, 0.0
        vis_dur = eff / self._zoom
        max_start = max(0.0, eff - vis_dur)
        start_sec = max(0.0, min(max_start, self._scroll_frac * eff))
        return start_sec, start_sec + vis_dur, eff

    def _lane_span(self):
        return max(1.0, float(self.width() - self.HEADER_W - self.PAD_RIGHT))

    def _sec_to_x(self, sec):
        start_sec, end_sec, _ = self._visible_window()
        vis_dur = end_sec - start_sec
        if vis_dur <= 0.0:
            return float(self.HEADER_W)
        span = self._lane_span()
        return self.HEADER_W + ((sec - start_sec) / vis_dur) * span

    def _x_to_sec(self, x):
        start_sec, end_sec, eff = self._visible_window()
        vis_dur = end_sec - start_sec
        if eff <= 0.0 or vis_dur <= 0.0:
            return 0.0
        span = self._lane_span()
        frac = max(0.0, min(1.0, (x - self.HEADER_W) / span))
        return max(0.0, min(eff, start_sec + frac * vis_dur))

    def _y_to_channel(self, y):
        if y < self.RULER_H:
            return -1
        lane_h = max(1.0, (self.height() - self.RULER_H) / 4.0)
        idx = int((y - self.RULER_H) / lane_h)
        return idx if 0 <= idx < 4 else -1

    def set_zoom(self, zoom, anchor_frac=None):
        new_zoom = max(1.0, min(self.MAX_ZOOM, float(zoom)))
        if abs(new_zoom - self._zoom) < 1e-4:
            return
        old_vis = 1.0 / self._zoom
        new_vis = 1.0 / new_zoom
        if anchor_frac is None:
            eff = self._effective_duration()
            if eff > 0.0:
                play_frac = self._position / eff
                if self._scroll_frac <= play_frac <= self._scroll_frac + old_vis:
                    anchor_frac = (play_frac - self._scroll_frac) / old_vis
                else:
                    anchor_frac = 0.5
            else:
                anchor_frac = 0.0
        anchor_global = self._scroll_frac + anchor_frac * old_vis
        self._zoom = new_zoom
        self._scroll_frac = max(0.0, min(1.0 - new_vis, anchor_global - anchor_frac * new_vis))
        self.zoomChanged.emit(self._zoom)
        self.invalidate_cache()
        self._schedule_detail_refresh()

    def set_scroll_frac(self, frac):
        max_scroll = max(0.0, 1.0 - 1.0 / self._zoom)
        clamped = max(0.0, min(max_scroll, float(frac)))
        if abs(clamped - self._scroll_frac) > 1e-5:
            self._scroll_frac = clamped
            self.invalidate_cache()
            self._schedule_detail_refresh()

    def zoom(self):
        return self._zoom

    def scroll_frac(self):
        return self._scroll_frac

    def ensure_playhead_visible(self):
        if self._zoom <= 1.001:
            return
        eff = self._effective_duration()
        if eff <= 0.0:
            return
        pos_frac = self._position / eff
        vis_frac = 1.0 / self._zoom
        if pos_frac < self._scroll_frac or pos_frac > self._scroll_frac + vis_frac * 0.96:
            max_scroll = max(0.0, 1.0 - vis_frac)
            self._scroll_frac = max(0.0, min(max_scroll, pos_frac - vis_frac * 0.15))
            self._cache_dirty = True
            self.zoomChanged.emit(self._zoom)
            self._schedule_detail_refresh()

    def _update_playhead_region(self):
        """Trigger a minimal dirty-rect repaint covering only old + new playhead x positions."""
        eff_dur = self._effective_duration()
        new_px = self._sec_to_x(self._position) if eff_dur > 0.0 else float(self.HEADER_W)
        if not self._cache_dirty and abs(new_px - self._last_drawn_px) < 0.5:
            return
        if self._cache_dirty or self._last_drawn_px < -100.0:
            self.update()
            return
        h = self.height()
        x_old = int(self._last_drawn_px) - 10
        x_new = int(new_px) - 10
        self.update(QRect(x_old, 0, 20, h))
        self.update(QRect(x_new, 0, 20, h))

    # ---- Mouse / Wheel / Drag-and-Drop ----

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            x = event.position().x()
            if not self._rec_active and self._duration > 0.0 and x >= self.HEADER_W:
                self._dragging = True
                self._position = self._x_to_sec(x)
                self.positionScrubbed.emit(self._position)
                self._update_playhead_region()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging and self._duration > 0.0:
            self._position = self._x_to_sec(event.position().x())
            self.positionScrubbed.emit(self._position)
            self._update_playhead_region()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            if self._duration > 0.0:
                self._position = self._x_to_sec(event.position().x())
                self.positionScrubbed.emit(self._position)
                self._update_playhead_region()
                self.seekRequested.emit(self._position)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def event(self, ev):
        if ev.type() == QEvent.Type.NativeGesture:
            if ev.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                if self._effective_duration() > 0.0:
                    anchor = max(0.0, min(1.0, (ev.position().x() - self.HEADER_W) / self._lane_span()))
                    factor = max(0.5, min(2.0, 1.0 + float(ev.value())))
                    self.set_zoom(self._zoom * factor, anchor_frac=anchor)
                ev.accept()
                return True
        return super().event(ev)

    def wheelEvent(self, event):
        if self._effective_duration() <= 0.0:
            return
        dy = event.angleDelta().y()
        dx = event.angleDelta().x()
        if dy == 0 and dx == 0:
            dy = event.pixelDelta().y() * 3
            dx = event.pixelDelta().x() * 3
        mods = event.modifiers()

        if (abs(dx) > abs(dy) or (mods & Qt.KeyboardModifier.ShiftModifier)) and self._zoom > 1.0:
            delta = dx if abs(dx) > abs(dy) else dy
            step = -delta / 1200.0 / self._zoom
            self.set_scroll_frac(self._scroll_frac + step)
            self.zoomChanged.emit(self._zoom)
            event.accept()
            return

        if dy != 0:
            anchor = max(0.0, min(1.0, (event.position().x() - self.HEADER_W) / self._lane_span()))
            # Smooth proportional zoom: works naturally for both trackpads and mouse wheels
            factor = max(0.76, min(1.32, 1.003 ** dy))
            self.set_zoom(self._zoom * factor, anchor_frac=anchor)
            event.accept()

    def dragEnterEvent(self, event):
        if _extract_dropped_wav(event.mimeData()):
            self._drop_hover_ch = self._y_to_channel(event.position().y())
            self.invalidate_cache()
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if _extract_dropped_wav(event.mimeData()):
            ch = self._y_to_channel(event.position().y())
            if ch != self._drop_hover_ch:
                self._drop_hover_ch = ch
                self.invalidate_cache()
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, _event):
        self._drop_hover_ch = -1
        self.invalidate_cache()

    def dropEvent(self, event):
        ch = self._y_to_channel(event.position().y())
        self._drop_hover_ch = -1
        self.invalidate_cache()
        path = _extract_dropped_wav(event.mimeData())
        if path and 0 <= ch < 4:
            self.fileDroppedOnChannel.emit(ch, path)
            event.acceptProposedAction()
        else:
            event.ignore()

    # ---- Cached Static Layer Rendering ----

    def _rebuild_static_cache(self):
        w_int = max(1, self.width())
        h_int = max(1, self.height())
        dpr = max(1.0, float(self.devicePixelRatioF()))
        pix = QPixmap(int(round(w_int * dpr)), int(round(h_int * dpr)))
        pix.setDevicePixelRatio(dpr)

        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = float(w_int)
        h = float(h_int)
        span = self._lane_span()
        lane_h = max(24.0, (h - self.RULER_H) / 4.0)
        start_sec, end_sec, eff_dur = self._visible_window()
        vis_dur = max(1e-6, end_sec - start_sec)

        p.fillRect(QRectF(0.0, 0.0, w, h), QColor("#0e1822"))

        # Time ruler
        p.fillRect(QRectF(0.0, 0.0, w, self.RULER_H), QColor("#162534"))
        p.setPen(QPen(QColor("#2e4963"), 1.0))
        p.drawLine(QPointF(0.0, self.RULER_H - 0.5), QPointF(w, self.RULER_H - 0.5))

        hdr_font = QFont("Helvetica", 9)
        hdr_font.setBold(True)
        for ch in range(4):
            y0 = self.RULER_H + ch * lane_h
            y1 = y0 + lane_h
            mid_y = (y0 + y1) * 0.5
            is_aud = self._audible[ch]
            base_stripe = QColor(self.TRACK_THEMES[ch][0])

            lane_bg = QColor("#0f1a25") if ch % 2 == 0 else QColor("#121f2d")
            if self._drop_hover_ch == ch:
                lane_bg = QColor("#1c354c")
            p.fillRect(QRectF(self.HEADER_W, y0, w - self.HEADER_W, lane_h), lane_bg)

            p.fillRect(QRectF(0.0, y0, self.HEADER_W, lane_h), QColor("#192a3b"))
            p.fillRect(
                QRectF(0.0, y0 + 1.0, 4.0, lane_h - 2.0),
                base_stripe if is_aud else QColor("#4a5e70"),
            )
            p.setPen(QPen(QColor("#263d54"), 1.0))
            p.drawRect(QRectF(0.5, y0 + 0.5, self.HEADER_W - 1.0, lane_h - 1.0))

            p.setFont(hdr_font)
            p.setPen(QColor("#d4e5f5") if is_aud else QColor("#6f869c"))
            p.drawText(QRectF(6.0, y0, self.HEADER_W - 8.0, lane_h), Qt.AlignmentFlag.AlignCenter, f"CH {ch + 1}")

            p.setPen(QPen(QColor("#172737"), 1.0))
            p.drawLine(QPointF(self.HEADER_W, mid_y), QPointF(w - self.PAD_RIGHT, mid_y))
            p.setPen(QPen(QColor("#22364a"), 1.0))
            p.drawLine(QPointF(0.0, y1 - 0.5), QPointF(w, y1 - 0.5))

            if self._drop_hover_ch == ch:
                p.setPen(QPen(QColor("#63b3ed"), 1.8, Qt.PenStyle.DashLine))
                p.drawRect(QRectF(self.HEADER_W + 2.0, y0 + 2.0, span - 4.0, lane_h - 4.0))

        # Grid lines & ruler time ticks
        if eff_dur > 0.0:
            num_ticks = max(4, min(12, int(span // 110)))
            step_sec = vis_dur / float(num_ticks)
            tick_font = _mono_font(8, bold=False)
            p.setFont(tick_font)
            for t_idx in range(num_ticks + 1):
                frac = t_idx / float(num_ticks)
                gx = self.HEADER_W + frac * span
                p.setPen(QPen(QColor("#172838"), 1.0))
                p.drawLine(QPointF(gx, self.RULER_H), QPointF(gx, h))
                if t_idx < num_ticks:
                    sec_val = start_sec + frac * vis_dur
                    p.setPen(QColor("#7899b8"))
                    p.drawText(
                        QRectF(gx + 4.0, 0.0, 62.0, self.RULER_H),
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                        format_ruler_time(sec_val, step_sec),
                    )

        # Loaded clip waveforms
        p.save()
        p.setClipRect(QRectF(self.HEADER_W, self.RULER_H, span + self.PAD_RIGHT, h - self.RULER_H))

        clip_font = QFont("Helvetica", 8)
        clip_font.setBold(True)
        fm = QFontMetrics(clip_font)

        if eff_dur > 0.0:
            for ch in range(4):
                track = self._tracks[ch]
                if track is None:
                    continue
                envelope, track_dur, clip_label = track
                if not envelope or track_dur <= 0.0:
                    continue

                x_start = self._sec_to_x(0.0)
                x_end = self._sec_to_x(track_dur)
                if x_end < self.HEADER_W or x_start > w:
                    continue

                y0 = self.RULER_H + ch * lane_h
                y1 = y0 + lane_h
                cy0 = y0 + 2.0
                cy1 = y1 - 2.0
                wf_y0 = cy0 + self.CLIP_HDR_H
                wf_mid = (wf_y0 + cy1) * 0.5
                max_amp = max(2.0, (cy1 - wf_y0) * 0.5 - 1.5)

                is_aud = self._audible[ch]
                stripe_c, clip_bg_c, hdr_bg_c, peak_c, rms_c = (
                    self.TRACK_THEMES[ch] if is_aud else self.DIM_THEME
                )

                clip_rect = QRectF(x_start, cy0, max(6.0, x_end - x_start), cy1 - cy0)
                p.setPen(QPen(QColor(stripe_c), 1.0))
                p.setBrush(QColor(clip_bg_c))
                p.drawRoundedRect(clip_rect, 3.0, 3.0)

                hdr_rect = QRectF(
                    clip_rect.left() + 1.0,
                    clip_rect.top() + 1.0,
                    max(0.0, clip_rect.width() - 2.0),
                    self.CLIP_HDR_H - 1.0,
                )
                p.fillRect(hdr_rect, QColor(hdr_bg_c))

                if clip_label and clip_rect.width() > 36.0:
                    vis_left = max(clip_rect.left() + 5.0, float(self.HEADER_W) + 5.0)
                    avail_w = int(clip_rect.right() - vis_left - 4.0)
                    if avail_w > 24:
                        elided = fm.elidedText(clip_label, Qt.TextElideMode.ElideRight, avail_w)
                        p.setFont(clip_font)
                        p.setPen(QColor("#e6f2fc") if is_aud else QColor("#8aa0b5"))
                        p.drawText(
                            QRectF(vis_left, cy0 + 0.5, avail_w, self.CLIP_HDR_H),
                            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                            elided,
                        )

                p.setPen(QPen(QColor("#243c54"), 1.0))
                p.drawLine(QPointF(clip_rect.left() + 1.0, wf_mid), QPointF(clip_rect.right() - 1.0, wf_mid))

                vis_t0 = max(0.0, start_sec)
                vis_t1 = min(track_dur, end_sec)
                if vis_t1 <= vis_t0:
                    continue

                vx0 = self._sec_to_x(vis_t0)
                vx1 = self._sec_to_x(vis_t1)
                vis_w = max(2.0, vx1 - vx0)

                # Use high-res zoomed detail slice if it covers the visible window, else base envelope
                src_env = envelope
                src_t0 = 0.0
                src_t1 = track_dur
                detail = self._track_details[ch]
                if detail is not None:
                    d_bins, d_s, d_e = detail
                    if d_bins and d_e > d_s:
                        tol = max(0.02, (vis_t1 - vis_t0) * 0.05)
                        if d_s <= vis_t0 + tol and d_e >= vis_t1 - tol:
                            src_env = d_bins
                            src_t0 = d_s
                            src_t1 = d_e

                n_bins = len(src_env)
                src_span = max(1e-6, src_t1 - src_t0)
                n_pts = max(2, min(int(vis_w / 1.25), max(2, n_bins)))

                b_start = max(0.0, (vis_t0 - src_t0) / src_span) * (n_bins - 1)
                b_end = min(1.0, (vis_t1 - src_t0) / src_span) * (n_bins - 1)
                bins_per_pt = (b_end - b_start) / float(max(1, n_pts - 1))

                pk_top = []
                pk_bot = []
                rms_top = []
                rms_bot = []

                for idx_p in range(n_pts):
                    frac_p = idx_p / float(max(1, n_pts - 1))
                    center_b = b_start + frac_p * (b_end - b_start)
                    if bins_per_pt > 1.2:
                        # Aggregate true min/max/RMS across all bins mapped to this pixel (zero aliasing!)
                        i0 = max(0, int(math.floor(center_b - 0.5 * bins_per_pt)))
                        i1 = min(n_bins - 1, int(math.ceil(center_b + 0.5 * bins_per_pt)))
                        lo_v = 0.0
                        hi_v = 0.0
                        rms_v = 0.0
                        for bi in range(i0, i1 + 1):
                            it = src_env[bi]
                            if isinstance(it, tuple):
                                l_b, h_b, r_b = it
                            else:
                                v = float(it)
                                l_b, h_b, r_b = -v, v, v * 0.65
                            if l_b < lo_v:
                                lo_v = l_b
                            if h_b > hi_v:
                                hi_v = h_b
                            if r_b > rms_v:
                                rms_v = r_b
                    else:
                        # Linearly interpolate between adjacent bins when zoomed past bin resolution
                        i0 = max(0, min(n_bins - 1, int(math.floor(center_b))))
                        i1 = min(n_bins - 1, i0 + 1)
                        f_b = max(0.0, min(1.0, center_b - i0))
                        it0 = src_env[i0]
                        it1 = src_env[i1]
                        if isinstance(it0, tuple):
                            l0, h0, r0 = it0
                        else:
                            v = float(it0)
                            l0, h0, r0 = -v, v, v * 0.65
                        if isinstance(it1, tuple):
                            l1, h1, r1 = it1
                        else:
                            v = float(it1)
                            l1, h1, r1 = -v, v, v * 0.65
                        lo_v = l0 + (l1 - l0) * f_b
                        hi_v = h0 + (h1 - h0) * f_b
                        rms_v = r0 + (r1 - r0) * f_b

                    px = vx0 + frac_p * vis_w
                    y_hi = wf_mid - max(0.0, hi_v) * max_amp
                    y_lo = wf_mid - min(0.0, lo_v) * max_amp
                    if y_lo - y_hi < 1.2:
                        y_hi = wf_mid - 0.6
                        y_lo = wf_mid + 0.6
                    pk_top.append(QPointF(px, y_hi))
                    pk_bot.append(QPointF(px, y_lo))

                    r_amp = max(0.5, rms_v * max_amp)
                    rms_top.append(QPointF(px, wf_mid - r_amp))
                    rms_bot.append(QPointF(px, wf_mid + r_amp))

                pk_poly = QPolygonF(pk_top + list(reversed(pk_bot)))
                rms_poly = QPolygonF(rms_top + list(reversed(rms_bot)))

                p.setPen(QPen(QColor(peak_c), 0.7))
                p.setBrush(QColor(peak_c))
                p.drawPolygon(pk_poly)

                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(rms_c))
                p.drawPolygon(rms_poly)

                seam_col = QColor(clip_bg_c)
                seam_col.setAlpha(150)
                p.setPen(QPen(seam_col, 0.8))
                p.drawLine(QPointF(vx0, wf_mid), QPointF(vx1, wf_mid))

        p.restore()
        p.end()
        self._static_cache = pix
        self._cache_dpr = dpr
        self._cache_dirty = False

    def paintEvent(self, event):
        cur_dpr = max(1.0, float(self.devicePixelRatioF()))
        if self._cache_dirty or self._static_cache is None or abs(cur_dpr - self._cache_dpr) > 1e-3:
            self._rebuild_static_cache()

        p = QPainter(self)
        # Fast C++ blit of the dirty rect from our Retina QPixmap cache!
        dirty = event.rect()
        dpr = self._static_cache.devicePixelRatio()
        src = QRectF(dirty.x() * dpr, dirty.y() * dpr, dirty.width() * dpr, dirty.height() * dpr)
        p.drawPixmap(QRectF(dirty), self._static_cache, src)

        w = float(self.width())
        h = float(self.height())
        eff_dur = self._effective_duration()

        # Live recording overlay (only painted while actively recording)
        if self._rec_active and self._rec_armed and eff_dur > 0.0:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            span = self._lane_span()
            lane_h = max(24.0, (h - self.RULER_H) / 4.0)
            p.save()
            p.setClipRect(QRectF(self.HEADER_W, self.RULER_H, span + self.PAD_RIGHT, h - self.RULER_H))

            rx0 = self._sec_to_x(0.0)
            rx1 = self._sec_to_x(self._rec_elapsed)
            rec_w = max(6.0, rx1 - rx0)
            peaks = self._rec_peaks
            n_peaks = len(peaks)
            clip_font = QFont("Helvetica", 8)
            clip_font.setBold(True)

            for ch in self._rec_armed:
                if not (0 <= ch < 4):
                    continue
                y0 = self.RULER_H + ch * lane_h
                y1 = y0 + lane_h
                cy0 = y0 + 2.0
                cy1 = y1 - 2.0
                wf_y0 = cy0 + self.CLIP_HDR_H
                wf_mid = (wf_y0 + cy1) * 0.5
                max_amp = max(2.0, (cy1 - wf_y0) * 0.5 - 1.5)

                p.setPen(QPen(QColor("#f24957"), 1.2))
                p.setBrush(QColor("#3a161e"))
                p.drawRoundedRect(QRectF(rx0, cy0, rec_w, cy1 - cy0), 3.0, 3.0)
                p.fillRect(
                    QRectF(rx0 + 1.0, cy0 + 1.0, max(0.0, rec_w - 2.0), self.CLIP_HDR_H - 1.0),
                    QColor("#6e1e2a"),
                )
                if rec_w > 36.0:
                    p.setFont(clip_font)
                    p.setPen(QColor("#ffd6da"))
                    p.drawText(
                        QRectF(max(rx0 + 5.0, float(self.HEADER_W) + 5.0), cy0 + 0.5, 80.0, self.CLIP_HDR_H),
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                        "● REC",
                    )
                if n_peaks >= 2:
                    n_pts = max(2, min(n_peaks, int(rec_w / 1.5)))
                    inner_w = max(1.0, rec_w - 2.0)
                    top_pts = []
                    bot_pts = []
                    for idx_p in range(n_pts):
                        frac_p = idx_p / float(max(1, n_pts - 1))
                        p_idx = int(frac_p * (n_peaks - 1))
                        amp = max(1.0, min(1.0, peaks[p_idx]) * max_amp)
                        px = rx0 + 1.0 + frac_p * inner_w
                        top_pts.append(QPointF(px, wf_mid - amp))
                        bot_pts.append(QPointF(px, wf_mid + amp))
                    rec_poly = QPolygonF(top_pts + list(reversed(bot_pts)))
                    p.setPen(QPen(QColor("#ff8a94"), 0.8))
                    p.setBrush(QColor("#ff6b78"))
                    p.drawPolygon(rec_poly)
            p.restore()

        # Lightweight O(1) playhead overlay
        px = self._sec_to_x(self._position) if eff_dur > 0.0 else float(self.HEADER_W)
        self._last_drawn_px = px
        if self.HEADER_W - 1.0 <= px <= w - self.PAD_RIGHT + 2.0:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            glow = QColor("#f24957" if self._rec_active else "#48d18a")
            glow.setAlpha(60)
            p.setPen(QPen(glow, 5.0))
            p.drawLine(QPointF(px, self.RULER_H), QPointF(px, h))

            p.setPen(QPen(QColor("#eef7ff"), 1.8))
            p.drawLine(QPointF(px, self.RULER_H - 1.0), QPointF(px, h))

            cap_col = QColor("#f24957" if self._rec_active else "#48d18a")
            cap = QPolygonF(
                [
                    QPointF(px - 5.5, 3.0),
                    QPointF(px + 5.5, 3.0),
                    QPointF(px, self.RULER_H - 1.0),
                ]
            )
            p.setPen(QPen(QColor("#eef7ff"), 1.0))
            p.setBrush(cap_col)
            p.drawPolygon(cap)


class WaveformTimeline(QFrame):
    """4-track DAW waveform arrange view with cached QPixmap rendering, dynamic zoom, scrubbing & drag-and-drop."""

    fileDroppedOnChannel = Signal(int, str)
    detailRequested = Signal(float, float, int)

    def __init__(self, parent=None, on_seek=None):
        super().__init__(parent)
        self._on_seek = on_seek
        self._last_pos_text = "00:00"
        self._last_dur_text = "00:00"
        self._rec_view_dur = 0.0

        self.setObjectName("timelineFrame")
        self.setStyleSheet(
            """
            QFrame#timelineFrame {
                background-color: #182836;
                border: 1px solid #3c5c78;
                border-radius: 6px;
            }
            QFrame#timelineHeader {
                background-color: #1b2d3e;
                border-bottom: 1px solid #2b455e;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
            }
            QPushButton.zoomBtn {
                background-color: #23384c;
                color: #cfe3f5;
                border: 1px solid #3c5c78;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 700;
                padding: 1px 6px;
            }
            QPushButton.zoomBtn:hover {
                background-color: #2e4b66;
                color: #ffffff;
            }
            QScrollBar:horizontal {
                background: #101b26;
                height: 10px;
                margin: 0px;
                border-bottom-left-radius: 5px;
                border-bottom-right-radius: 5px;
            }
            QScrollBar::handle:horizontal {
                background: #365673;
                min-width: 28px;
                border-radius: 4px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #4d789e;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0px;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        top_bar = QFrame(self)
        top_bar.setObjectName("timelineHeader")
        top_bar.setFixedHeight(28)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(10, 0, 10, 0)
        top_layout.setSpacing(6)

        title_lbl = QLabel("ARRANGE  ·  WAVEFORMS  (CLICK/DRAG TO SCRUB · DROP .WAV ON ANY TRACK)", top_bar)
        title_lbl.setStyleSheet("color: #8eb0cf; font-size: 10px; font-weight: 700; background: transparent;")
        top_layout.addWidget(title_lbl)
        top_layout.addStretch(1)

        zoom_out_btn = QPushButton("−", top_bar)
        zoom_out_btn.setProperty("class", "zoomBtn")
        zoom_out_btn.setFixedWidth(22)
        zoom_out_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        zoom_out_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        zoom_out_btn.setToolTip("Zoom out timeline")
        zoom_out_btn.clicked.connect(lambda: self.canvas.set_zoom(self.canvas.zoom() / 1.35))

        self.zoom_reset_btn = QPushButton("100%", top_bar)
        self.zoom_reset_btn.setProperty("class", "zoomBtn")
        self.zoom_reset_btn.setFixedWidth(52)
        self.zoom_reset_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.zoom_reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.zoom_reset_btn.setToolTip("Reset timeline zoom to 100%")
        self.zoom_reset_btn.clicked.connect(self.reset_zoom)

        zoom_in_btn = QPushButton("+", top_bar)
        zoom_in_btn.setProperty("class", "zoomBtn")
        zoom_in_btn.setFixedWidth(22)
        zoom_in_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        zoom_in_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        zoom_in_btn.setToolTip("Zoom in timeline (or scroll wheel over waveforms)")
        zoom_in_btn.clicked.connect(lambda: self.canvas.set_zoom(self.canvas.zoom() * 1.35))

        top_layout.addWidget(zoom_out_btn)
        top_layout.addWidget(self.zoom_reset_btn)
        top_layout.addWidget(zoom_in_btn)

        sep = QLabel("│", top_bar)
        sep.setStyleSheet("color: #36536e; background: transparent;")
        top_layout.addWidget(sep)

        self.pos_lbl = QLabel("00:00", top_bar)
        self.pos_lbl.setFont(_mono_font(10, bold=True))
        self.pos_lbl.setStyleSheet("color: #7ef2b8; background: transparent;")

        slash_lbl = QLabel("/", top_bar)
        slash_lbl.setFont(_mono_font(10, bold=True))
        slash_lbl.setStyleSheet("color: #5c7b99; background: transparent;")

        self.dur_lbl = QLabel("00:00", top_bar)
        self.dur_lbl.setFont(_mono_font(10, bold=True))
        self.dur_lbl.setStyleSheet("color: #9cb8d1; background: transparent;")

        top_layout.addWidget(self.pos_lbl)
        top_layout.addWidget(slash_lbl)
        top_layout.addWidget(self.dur_lbl)
        layout.addWidget(top_bar)

        self.canvas = _TimelineCanvas(self)
        layout.addWidget(self.canvas, 1)

        self.hscroll = QScrollBar(Qt.Orientation.Horizontal, self)
        self.hscroll.setRange(0, 0)
        self.hscroll.setVisible(False)
        self.hscroll.valueChanged.connect(self._on_scrollbar_moved)
        layout.addWidget(self.hscroll)

        self._updating_scroll = False
        self.canvas.positionScrubbed.connect(self._on_scrub_pos)
        self.canvas.seekRequested.connect(self._on_seek_emit)
        self.canvas.zoomChanged.connect(self._sync_zoom_ui)
        self.canvas.detailRequested.connect(self.detailRequested.emit)
        self.canvas.fileDroppedOnChannel.connect(self.fileDroppedOnChannel.emit)

    def _set_pos_text(self, pos_sec):
        txt = format_timecode(pos_sec)
        if txt != self._last_pos_text:
            self._last_pos_text = txt
            self.pos_lbl.setText(txt)

    def _set_dur_text(self, dur_sec):
        txt = format_timecode(dur_sec)
        if txt != self._last_dur_text:
            self._last_dur_text = txt
            self.dur_lbl.setText(txt)

    def _on_scrub_pos(self, pos_sec):
        self._set_pos_text(pos_sec)

    def _on_seek_emit(self, pos_sec):
        if self._on_seek is not None:
            self._on_seek(pos_sec)

    def _sync_zoom_ui(self, zoom_val):
        self.zoom_reset_btn.setText(f"{int(round(zoom_val * 100))}%")
        self._updating_scroll = True
        if zoom_val <= 1.001:
            self.hscroll.setVisible(False)
            self.hscroll.setRange(0, 0)
        else:
            scale = 10000.0
            page = max(10, int(round(scale / zoom_val)))
            max_val = max(0, int(scale) - page)
            self.hscroll.setVisible(True)
            self.hscroll.setPageStep(page)
            self.hscroll.setRange(0, max_val)
            self.hscroll.setValue(int(round(self.canvas.scroll_frac() * scale)))
        self._updating_scroll = False

    def _on_scrollbar_moved(self, val):
        if self._updating_scroll:
            return
        self.canvas.set_scroll_frac(val / 10000.0)

    def reset_zoom(self):
        self.canvas.set_zoom(1.0)
        self.canvas.set_scroll_frac(0.0)
        self._sync_zoom_ui(1.0)

    # ---- Public API ----

    def set_track_waveform(self, channel_idx, peaks, duration_sec, label=""):
        if 0 <= channel_idx < 4:
            self.canvas._tracks[channel_idx] = (list(peaks), float(duration_sec), label)
            self.canvas._track_details[channel_idx] = None
            self.canvas.invalidate_cache()
            self.canvas._schedule_detail_refresh()

    def set_track_detail(self, channel_idx, detail_bins, start_sec, end_sec):
        """Apply high-resolution zoomed waveform slice for the visible window."""
        if 0 <= channel_idx < 4 and self.canvas._tracks[channel_idx] is not None:
            self.canvas._track_details[channel_idx] = (list(detail_bins), float(start_sec), float(end_sec))
            self.canvas.invalidate_cache()

    def clear_track(self, channel_idx):
        if 0 <= channel_idx < 4:
            self.canvas._tracks[channel_idx] = None
            self.canvas._track_details[channel_idx] = None
            self.canvas.invalidate_cache()

    def set_audible_flags(self, audible_flags):
        new_flags = [bool(f) for f in audible_flags[:4]]
        if new_flags != self.canvas._audible:
            self.canvas._audible = new_flags
            self.canvas.invalidate_cache()

    def set_duration(self, duration_sec):
        self.canvas._duration = max(0.0, float(duration_sec))
        if self.canvas._duration <= 0.0 and self.canvas.zoom() > 1.001:
            self.reset_zoom()
        if self.canvas._position > self.canvas._duration and not self.canvas._rec_active:
            self.canvas._position = self.canvas._duration
        self._set_dur_text(self.canvas._duration)
        self.canvas.invalidate_cache()

    def set_position(self, pos_sec):
        if self.canvas._dragging:
            return
        eff = self.canvas._effective_duration()
        self.canvas._position = max(0.0, min(eff, float(pos_sec))) if eff > 0.0 else 0.0
        self._set_pos_text(self.canvas._position)
        self.canvas.ensure_playhead_visible()
        self.canvas._update_playhead_region()

    def update_live_recording(self, active, armed_indices=(), peaks=(), elapsed_sec=0.0):
        was_active = self.canvas._rec_active
        self.canvas._rec_active = bool(active)
        self.canvas._rec_armed = list(armed_indices) if active else []
        self.canvas._rec_peaks = list(peaks) if active else []
        self.canvas._rec_elapsed = float(elapsed_sec) if active else 0.0

        if active:
            self.canvas._position = self.canvas._rec_elapsed
            eff_dur = self.canvas._effective_duration()
            self._set_pos_text(self.canvas._position)
            self._set_dur_text(eff_dur)
            self.canvas.ensure_playhead_visible()
            if not was_active or abs(eff_dur - self._rec_view_dur) > 0.5:
                self._rec_view_dur = eff_dur
                self.canvas.invalidate_cache()
            else:
                self.canvas.update()
        else:
            self._rec_view_dur = 0.0
            self._set_dur_text(self.canvas._duration)
            if was_active:
                self.canvas.invalidate_cache()
