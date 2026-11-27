# Tremolo Effect (DSP Processor + PySide6 Parameter Dialog)
import ctypes
import math
import threading

from fx_dialog_base import EffectDialogBase

try:
    import dsp_accel as _dsp
except Exception:
    _dsp = None


class TremoloProcessor:
    """Thread-safe real-time LFO tremolo audio processor."""

    DEFAULT_RATE = 4.0     # Hz
    DEFAULT_DEPTH = 0.6    # 0.0 to 1.0

    def __init__(self, rate=DEFAULT_RATE, depth=DEFAULT_DEPTH):
        self._lock = threading.Lock()
        self._proc_lock = threading.Lock()
        self._rate = float(rate)
        self._depth = float(depth)
        self._phase = 0.0

    def set_params(self, rate=None, depth=None):
        with self._lock:
            if rate is not None:
                self._rate = max(0.1, min(20.0, float(rate)))
            if depth is not None:
                self._depth = max(0.0, min(1.0, float(depth)))

    def get_params(self):
        with self._lock:
            return self._rate, self._depth

    def reset_defaults(self):
        self.set_params(rate=self.DEFAULT_RATE, depth=self.DEFAULT_DEPTH)

    def reset(self):
        with self._proc_lock:
            self._phase = 0.0

    def clone_for_offline(self):
        rate, depth = self.get_params()
        return TremoloProcessor(rate=rate, depth=depth)

    def process(self, samples, sample_rate):
        """Apply tremolo to mono float samples in [-1.0, 1.0]."""
        with self._proc_lock:
            return self._process_unlocked(samples, sample_rate)

    def _process_unlocked(self, samples, sample_rate):
        rate, depth = self.get_params()
        n = len(samples)
        if n == 0:
            return samples

        if _dsp is not None and _dsp.AVAILABLE:
            in_arr = _dsp.as_f64_array(samples)
            out_arr = _dsp.alloc_f64(n)
            phase_c = ctypes.c_double(self._phase)
            _dsp.LIB.dsp_tremolo_process(
                _dsp.f64_ptr(in_arr),
                _dsp.f64_ptr(out_arr),
                n,
                float(sample_rate),
                rate,
                depth,
                ctypes.byref(phase_c),
            )
            self._phase = phase_c.value
            return out_arr

        two_pi = 2.0 * math.pi
        phase_inc = two_pi * rate / float(sample_rate)
        phase = self._phase

        if depth <= 0.0:
            self._phase = (phase + phase_inc * n) % two_pi
            return list(samples)

        base = 1.0 - depth
        half_depth = 0.5 * depth
        sin = math.sin
        out = [0.0] * n

        for i in range(n):
            gain = base + half_depth * (1.0 + sin(phase))
            out[i] = samples[i] * gain
            phase += phase_inc
            if phase >= two_pi:
                phase -= two_pi

        self._phase = phase
        return out


class TremoloWindow(EffectDialogBase):
    """Non-blocking PySide6 dialog to control a channel's TremoloProcessor."""

    BG_DARK = "#1c332d"
    BG_HEADER = "#152722"
    BG_WELL = "#12211d"
    ACCENT = "#48d18a"

    def __init__(self, parent=None, processor=None, channel_num=None, on_close=None):
        self.processor = processor if processor is not None else TremoloProcessor()
        title = f"Tremolo - Channel {channel_num}" if channel_num is not None else "Tremolo"

        super().__init__(
            title=title,
            bg_dark=self.BG_DARK,
            bg_header=self.BG_HEADER,
            bg_well=self.BG_WELL,
            accent=self.ACCENT,
            btn_reset_bg="#2a4d43",
            btn_reset_hover="#376659",
            btn_close_bg="#246e4c",
            btn_close_hover="#2e8a60",
            parent=parent,
            on_close=on_close,
            width=340,
        )

        rate, depth = self.processor.get_params()

        self.rate_slider, _ = self.add_param_slider(
            "Rate",
            0.1,
            20.0,
            0.1,
            rate,
            TremoloProcessor.DEFAULT_RATE,
            lambda v: f"{v:.2f} Hz",
            lambda v: self.processor.set_params(rate=v),
        )
        self.depth_slider, _ = self.add_param_slider(
            "Depth",
            0.0,
            1.0,
            0.01,
            depth,
            TremoloProcessor.DEFAULT_DEPTH,
            lambda v: f"{int(round(v * 100))}%",
            lambda v: self.processor.set_params(depth=v),
        )

        self.reset_btn.clicked.connect(self._reset_defaults)
        self.finalize_and_show()

    def _reset_defaults(self):
        self.processor.reset_defaults()
        rate, depth = self.processor.get_params()
        self.rate_slider.set_value_float(rate)
        self.depth_slider.set_value_float(depth)


# Backward-compatible alias
tremolo = TremoloWindow


def Main():
    TremoloWindow()
    return 0


if __name__ == "__main__":
    Main()
