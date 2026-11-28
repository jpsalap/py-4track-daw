# Delay Effect (DSP Processor + PySide6 Parameter Dialog)
import ctypes
import threading

from fx_dialog_base import EffectDialogBase

try:
    import dsp_accel as _dsp
except Exception:
    _dsp = None


class DelayProcessor:
    """Thread-safe real-time feedback delay line processor."""

    DEFAULT_DELAY_MS = 300.0   # ms
    DEFAULT_FEEDBACK = 0.40    # 0.0 to 0.85
    DEFAULT_MIX = 0.40         # 0.0 (dry) to 1.0 (wet)
    MAX_DELAY_SEC = 1.1

    def __init__(self, delay_ms=DEFAULT_DELAY_MS, feedback=DEFAULT_FEEDBACK, mix=DEFAULT_MIX):
        self._lock = threading.Lock()
        self._proc_lock = threading.Lock()
        self._delay_ms = float(delay_ms)
        self._feedback = float(feedback)
        self._mix = float(mix)
        self._buffer = []
        self._buf_len = 0
        self._write_idx = 0
        self._sample_rate = 0

    def set_params(self, delay_ms=None, feedback=None, mix=None):
        with self._lock:
            if delay_ms is not None:
                self._delay_ms = max(10.0, min(1000.0, float(delay_ms)))
            if feedback is not None:
                self._feedback = max(0.0, min(0.85, float(feedback)))
            if mix is not None:
                self._mix = max(0.0, min(1.0, float(mix)))

    def get_params(self):
        with self._lock:
            return self._delay_ms, self._feedback, self._mix

    def reset_defaults(self):
        self.set_params(
            delay_ms=self.DEFAULT_DELAY_MS,
            feedback=self.DEFAULT_FEEDBACK,
            mix=self.DEFAULT_MIX,
        )

    def reset(self):
        with self._proc_lock:
            if self._buf_len > 0:
                if _dsp is not None and _dsp.AVAILABLE:
                    self._buffer = _dsp.alloc_f64(self._buf_len)
                elif _dsp is not None and _dsp.USE_NUMPY:
                    self._buffer = _dsp.alloc_np_f64(self._buf_len)
                else:
                    self._buffer = [0.0] * self._buf_len
            self._write_idx = 0

    def clone_for_offline(self):
        delay_ms, feedback, mix = self.get_params()
        return DelayProcessor(delay_ms=delay_ms, feedback=feedback, mix=mix)

    def _ensure_buffer(self, sample_rate):
        sr = int(sample_rate)
        if sr != self._sample_rate or self._buf_len == 0:
            self._sample_rate = sr
            self._buf_len = int(sr * self.MAX_DELAY_SEC) + 2
            if _dsp is not None and _dsp.AVAILABLE:
                self._buffer = _dsp.alloc_f64(self._buf_len)
            elif _dsp is not None and _dsp.USE_NUMPY:
                self._buffer = _dsp.alloc_np_f64(self._buf_len)
            else:
                self._buffer = [0.0] * self._buf_len
            self._write_idx = 0

    def process(self, samples, sample_rate):
        """Apply feedback delay to mono float samples in [-1.0, 1.0]."""
        with self._proc_lock:
            return self._process_unlocked(samples, sample_rate)

    def _process_unlocked(self, samples, sample_rate):
        n = len(samples)
        if n == 0:
            return samples

        self._ensure_buffer(sample_rate)
        delay_ms, feedback, mix = self.get_params()

        if _dsp is not None and _dsp.AVAILABLE:
            in_arr = _dsp.as_f64_array(samples)
            out_arr = _dsp.alloc_f64(n)
            buf_arr = _dsp.as_f64_array(self._buffer)
            self._buffer = buf_arr
            w_idx = ctypes.c_size_t(self._write_idx)
            _dsp.LIB.dsp_delay_process(
                _dsp.f64_ptr(in_arr),
                _dsp.f64_ptr(out_arr),
                n,
                float(sample_rate),
                delay_ms,
                feedback,
                mix,
                _dsp.f64_ptr(buf_arr),
                self._buf_len,
                ctypes.byref(w_idx),
            )
            self._write_idx = w_idx.value
            return out_arr

        if _dsp is not None and _dsp.USE_NUMPY:
            np = _dsp.NP
            x = np.asarray(samples, dtype=np.float64)
            buf = self._buffer
            buf_len = self._buf_len
            if not isinstance(buf, np.ndarray) or len(buf) != buf_len:
                buf = np.zeros(buf_len, dtype=np.float64)
                self._buffer = buf

            delay_samples = int(delay_ms * sample_rate * 0.001)
            if delay_samples < 1:
                delay_samples = 1
            elif delay_samples >= buf_len:
                delay_samples = buf_len - 1

            dry = 1.0 - mix
            out = np.empty(n, dtype=np.float64)
            write_idx = self._write_idx
            pos = 0

            while pos < n:
                k = n - pos
                if k > delay_samples:
                    k = delay_samples

                read_idx = write_idx - delay_samples
                if read_idx < 0:
                    read_idx += buf_len

                if read_idx + k <= buf_len:
                    delayed = buf[read_idx : read_idx + k].copy()
                else:
                    first = buf_len - read_idx
                    delayed = np.concatenate((buf[read_idx:], buf[: k - first]))

                x_blk = x[pos : pos + k]
                fb_val = np.clip(x_blk + delayed * feedback, -1.5, 1.5)

                if write_idx + k <= buf_len:
                    buf[write_idx : write_idx + k] = fb_val
                else:
                    first = buf_len - write_idx
                    buf[write_idx:] = fb_val[:first]
                    buf[: k - first] = fb_val[first:]

                out[pos : pos + k] = x_blk * dry + delayed * mix

                write_idx += k
                if write_idx >= buf_len:
                    write_idx -= buf_len
                pos += k

            self._write_idx = write_idx
            return out

        buf = self._buffer
        buf_len = self._buf_len
        write_idx = self._write_idx
        delay_samples = int(delay_ms * sample_rate * 0.001)
        if delay_samples < 1:
            delay_samples = 1
        elif delay_samples >= buf_len:
            delay_samples = buf_len - 1

        dry = 1.0 - mix
        out = [0.0] * n

        for i in range(n):
            x = samples[i]
            read_idx = write_idx - delay_samples
            if read_idx < 0:
                read_idx += buf_len

            delayed = buf[read_idx]
            fb_val = x + delayed * feedback
            if fb_val > 1.5:
                fb_val = 1.5
            elif fb_val < -1.5:
                fb_val = -1.5
            buf[write_idx] = fb_val

            out[i] = x * dry + delayed * mix

            write_idx += 1
            if write_idx >= buf_len:
                write_idx = 0

        self._write_idx = write_idx
        return out


class DelayWindow(EffectDialogBase):
    """Non-blocking PySide6 dialog to control a channel's DelayProcessor."""

    BG_DARK = "#1b304a"
    BG_HEADER = "#142438"
    BG_WELL = "#101e2e"
    ACCENT = "#63b3ed"

    def __init__(self, parent=None, processor=None, channel_num=None, on_close=None):
        self.processor = processor if processor is not None else DelayProcessor()
        title = f"Delay - Channel {channel_num}" if channel_num is not None else "Delay"

        super().__init__(
            title=title,
            bg_dark=self.BG_DARK,
            bg_header=self.BG_HEADER,
            bg_well=self.BG_WELL,
            accent=self.ACCENT,
            btn_reset_bg="#2a4669",
            btn_reset_hover="#375c8a",
            btn_close_bg="#245994",
            btn_close_hover="#2e6eb5",
            parent=parent,
            on_close=on_close,
            width=340,
        )

        delay_ms, feedback, mix = self.processor.get_params()

        self.time_slider, _ = self.add_param_slider(
            "Delay Time",
            10.0,
            1000.0,
            5.0,
            delay_ms,
            DelayProcessor.DEFAULT_DELAY_MS,
            lambda v: f"{int(round(v))} ms",
            lambda v: self.processor.set_params(delay_ms=v),
        )
        self.fb_slider, _ = self.add_param_slider(
            "Feedback",
            0.0,
            0.85,
            0.01,
            feedback,
            DelayProcessor.DEFAULT_FEEDBACK,
            lambda v: f"{int(round(v * 100))}%",
            lambda v: self.processor.set_params(feedback=v),
        )
        self.mix_slider, _ = self.add_param_slider(
            "Wet/Dry Mix",
            0.0,
            1.0,
            0.01,
            mix,
            DelayProcessor.DEFAULT_MIX,
            lambda v: f"{int(round(v * 100))}%",
            lambda v: self.processor.set_params(mix=v),
        )

        self.reset_btn.clicked.connect(self._reset_defaults)
        self.finalize_and_show()

    def _reset_defaults(self):
        self.processor.reset_defaults()
        delay_ms, feedback, mix = self.processor.get_params()
        self.time_slider.set_value_float(delay_ms)
        self.fb_slider.set_value_float(feedback)
        self.mix_slider.set_value_float(mix)


# Backward-compatible alias
delay = DelayWindow


def Main():
    DelayWindow()
    return 0


if __name__ == "__main__":
    Main()
