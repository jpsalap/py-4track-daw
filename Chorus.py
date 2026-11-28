# Chorus Effect (DSP Processor + PySide6 Parameter Dialog)
import ctypes
import math
import threading

from fx_dialog_base import EffectDialogBase

try:
    import dsp_accel as _dsp
except Exception:
    _dsp = None


class ChorusProcessor:
    """Thread-safe real-time modulated delay-line chorus processor."""

    DEFAULT_RATE = 1.2          # Hz
    DEFAULT_DEPTH_MS = 5.0      # ms
    DEFAULT_BASE_DELAY_MS = 15.0  # ms
    DEFAULT_MIX = 0.50          # 0.0 (dry) to 1.0 (wet)
    MAX_DELAY_SEC = 0.1         # 100 ms ring buffer

    def __init__(
        self,
        rate=DEFAULT_RATE,
        depth_ms=DEFAULT_DEPTH_MS,
        base_delay_ms=DEFAULT_BASE_DELAY_MS,
        mix=DEFAULT_MIX,
    ):
        self._lock = threading.Lock()
        self._proc_lock = threading.Lock()
        self._rate = float(rate)
        self._depth_ms = float(depth_ms)
        self._base_delay_ms = float(base_delay_ms)
        self._mix = float(mix)
        self._phase = 0.0
        self._buffer = []
        self._buf_len = 0
        self._write_idx = 0
        self._sample_rate = 0

    def set_params(self, rate=None, depth_ms=None, base_delay_ms=None, mix=None):
        with self._lock:
            if rate is not None:
                self._rate = max(0.1, min(5.0, float(rate)))
            if depth_ms is not None:
                self._depth_ms = max(0.5, min(15.0, float(depth_ms)))
            if base_delay_ms is not None:
                self._base_delay_ms = max(5.0, min(35.0, float(base_delay_ms)))
            if mix is not None:
                self._mix = max(0.0, min(1.0, float(mix)))

    def get_params(self):
        with self._lock:
            return self._rate, self._depth_ms, self._base_delay_ms, self._mix

    def reset_defaults(self):
        self.set_params(
            rate=self.DEFAULT_RATE,
            depth_ms=self.DEFAULT_DEPTH_MS,
            base_delay_ms=self.DEFAULT_BASE_DELAY_MS,
            mix=self.DEFAULT_MIX,
        )

    def reset(self):
        with self._proc_lock:
            self._phase = 0.0
            if self._buf_len > 0:
                if _dsp is not None and _dsp.AVAILABLE:
                    self._buffer = _dsp.alloc_f64(self._buf_len)
                elif _dsp is not None and _dsp.USE_NUMPY:
                    self._buffer = _dsp.alloc_np_f64(self._buf_len)
                else:
                    self._buffer = [0.0] * self._buf_len
            self._write_idx = 0

    def clone_for_offline(self):
        rate, depth_ms, base_delay_ms, mix = self.get_params()
        return ChorusProcessor(
            rate=rate, depth_ms=depth_ms, base_delay_ms=base_delay_ms, mix=mix
        )

    def _ensure_buffer(self, sample_rate):
        sr = int(sample_rate)
        if sr != self._sample_rate or self._buf_len == 0:
            self._sample_rate = sr
            self._buf_len = int(sr * self.MAX_DELAY_SEC) + 4
            if _dsp is not None and _dsp.AVAILABLE:
                self._buffer = _dsp.alloc_f64(self._buf_len)
            elif _dsp is not None and _dsp.USE_NUMPY:
                self._buffer = _dsp.alloc_np_f64(self._buf_len)
            else:
                self._buffer = [0.0] * self._buf_len
            self._write_idx = 0

    def process(self, samples, sample_rate):
        """Apply modulated chorus to mono float samples in [-1.0, 1.0]."""
        with self._proc_lock:
            return self._process_unlocked(samples, sample_rate)

    def _process_unlocked(self, samples, sample_rate):
        n = len(samples)
        if n == 0:
            return samples

        self._ensure_buffer(sample_rate)
        rate, depth_ms, base_delay_ms, mix = self.get_params()

        if _dsp is not None and _dsp.AVAILABLE:
            in_arr = _dsp.as_f64_array(samples)
            out_arr = _dsp.alloc_f64(n)
            buf_arr = _dsp.as_f64_array(self._buffer)
            self._buffer = buf_arr
            w_idx = ctypes.c_size_t(self._write_idx)
            phase_c = ctypes.c_double(self._phase)
            _dsp.LIB.dsp_chorus_process(
                _dsp.f64_ptr(in_arr),
                _dsp.f64_ptr(out_arr),
                n,
                float(sample_rate),
                rate,
                depth_ms,
                base_delay_ms,
                mix,
                _dsp.f64_ptr(buf_arr),
                self._buf_len,
                ctypes.byref(w_idx),
                ctypes.byref(phase_c),
            )
            self._write_idx = w_idx.value
            self._phase = phase_c.value
            return out_arr

        if _dsp is not None and _dsp.USE_NUMPY:
            np = _dsp.NP
            x = np.asarray(samples, dtype=np.float64)
            buf_len = self._buf_len
            hist = self._buffer
            if not isinstance(hist, np.ndarray) or len(hist) != buf_len:
                hist = np.zeros(buf_len, dtype=np.float64)

            ext = np.concatenate((hist, x))
            two_pi = 2.0 * math.pi
            phase_inc = two_pi * rate / float(sample_rate)
            phases = self._phase + phase_inc * np.arange(n, dtype=np.float64)

            ms_to_samples = sample_rate * 0.001
            base_samples = base_delay_ms * ms_to_samples
            depth_samples = depth_ms * ms_to_samples
            max_d = float(buf_len - 2)

            d = np.clip(base_samples + depth_samples * np.sin(phases), 1.0, max_d)
            read_pos = (np.arange(n, dtype=np.float64) + buf_len) - d
            idx0 = read_pos.astype(np.intp)
            frac = read_pos - idx0
            wet = ext[idx0] * (1.0 - frac) + ext[idx0 + 1] * frac
            out = x * (1.0 - mix) + wet * mix

            self._buffer = ext[-buf_len:].copy()
            self._phase = float((self._phase + phase_inc * n) % two_pi)
            return out

        buf = self._buffer
        buf_len = self._buf_len
        write_idx = self._write_idx
        phase = self._phase
        two_pi = 2.0 * math.pi
        phase_inc = two_pi * rate / float(sample_rate)

        ms_to_samples = sample_rate * 0.001
        base_samples = base_delay_ms * ms_to_samples
        depth_samples = depth_ms * ms_to_samples
        max_d = float(buf_len - 2)
        dry = 1.0 - mix
        sin = math.sin
        out = [0.0] * n

        for i in range(n):
            x = samples[i]
            buf[write_idx] = x

            d = base_samples + depth_samples * sin(phase)
            if d < 1.0:
                d = 1.0
            elif d > max_d:
                d = max_d

            read_pos = write_idx - d
            if read_pos < 0.0:
                read_pos += buf_len

            idx0 = int(read_pos)
            if idx0 >= buf_len:
                idx0 = 0
                frac = 0.0
            else:
                frac = read_pos - idx0
            idx1 = idx0 + 1
            if idx1 >= buf_len:
                idx1 = 0

            wet = buf[idx0] * (1.0 - frac) + buf[idx1] * frac
            out[i] = x * dry + wet * mix

            phase += phase_inc
            if phase >= two_pi:
                phase -= two_pi

            write_idx += 1
            if write_idx >= buf_len:
                write_idx = 0

        self._phase = phase
        self._write_idx = write_idx
        return out


class ChorusWindow(EffectDialogBase):
    """Non-blocking PySide6 dialog to control a channel's ChorusProcessor."""

    BG_DARK = "#173c42"
    BG_HEADER = "#112e33"
    BG_WELL = "#0d2326"
    ACCENT = "#6be0eb"

    def __init__(self, parent=None, processor=None, channel_num=None, on_close=None):
        self.processor = processor if processor is not None else ChorusProcessor()
        title = f"Chorus - Channel {channel_num}" if channel_num is not None else "Chorus"

        super().__init__(
            title=title,
            bg_dark=self.BG_DARK,
            bg_header=self.BG_HEADER,
            bg_well=self.BG_WELL,
            accent=self.ACCENT,
            btn_reset_bg="#23545c",
            btn_reset_hover="#2d6c75",
            btn_close_bg="#1c747d",
            btn_close_hover="#24929e",
            parent=parent,
            on_close=on_close,
            width=340,
        )

        rate, depth_ms, base_delay_ms, mix = self.processor.get_params()

        self.rate_slider, _ = self.add_param_slider(
            "Rate",
            0.1,
            5.0,
            0.05,
            rate,
            ChorusProcessor.DEFAULT_RATE,
            lambda v: f"{v:.2f} Hz",
            lambda v: self.processor.set_params(rate=v),
        )
        self.depth_slider, _ = self.add_param_slider(
            "Depth",
            0.5,
            15.0,
            0.5,
            depth_ms,
            ChorusProcessor.DEFAULT_DEPTH_MS,
            lambda v: f"{v:.1f} ms",
            lambda v: self.processor.set_params(depth_ms=v),
        )
        self.delay_slider, _ = self.add_param_slider(
            "Base Delay",
            5.0,
            35.0,
            0.5,
            base_delay_ms,
            ChorusProcessor.DEFAULT_BASE_DELAY_MS,
            lambda v: f"{v:.1f} ms",
            lambda v: self.processor.set_params(base_delay_ms=v),
        )
        self.mix_slider, _ = self.add_param_slider(
            "Wet/Dry Mix",
            0.0,
            1.0,
            0.01,
            mix,
            ChorusProcessor.DEFAULT_MIX,
            lambda v: f"{int(round(v * 100))}%",
            lambda v: self.processor.set_params(mix=v),
        )

        self.reset_btn.clicked.connect(self._reset_defaults)
        self.finalize_and_show()

    def _reset_defaults(self):
        self.processor.reset_defaults()
        rate, depth_ms, base_delay_ms, mix = self.processor.get_params()
        self.rate_slider.set_value_float(rate)
        self.depth_slider.set_value_float(depth_ms)
        self.delay_slider.set_value_float(base_delay_ms)
        self.mix_slider.set_value_float(mix)


# Backward-compatible alias
chorus = ChorusWindow


def Main():
    ChorusWindow()
    return 0


if __name__ == "__main__":
    Main()
