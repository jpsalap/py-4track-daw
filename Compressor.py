# Compressor Effect (DSP Processor + PySide6 Parameter Dialog)
import ctypes
import math
import threading

from fx_dialog_base import EffectDialogBase

try:
    import dsp_accel as _dsp
except Exception:
    _dsp = None


class CompressorProcessor:
    """Thread-safe real-time peak dynamic range compressor."""

    DEFAULT_THRESHOLD_DB = -18.0
    DEFAULT_RATIO = 4.0
    DEFAULT_ATTACK_MS = 10.0
    DEFAULT_RELEASE_MS = 100.0
    DEFAULT_MAKEUP_DB = 0.0

    def __init__(
        self,
        threshold_db=DEFAULT_THRESHOLD_DB,
        ratio=DEFAULT_RATIO,
        attack_ms=DEFAULT_ATTACK_MS,
        release_ms=DEFAULT_RELEASE_MS,
        makeup_db=DEFAULT_MAKEUP_DB,
    ):
        self._lock = threading.Lock()
        self._proc_lock = threading.Lock()
        self._threshold_db = float(threshold_db)
        self._ratio = float(ratio)
        self._attack_ms = float(attack_ms)
        self._release_ms = float(release_ms)
        self._makeup_db = float(makeup_db)
        self._env = 0.0

    def set_params(
        self,
        threshold_db=None,
        ratio=None,
        attack_ms=None,
        release_ms=None,
        makeup_db=None,
    ):
        with self._lock:
            if threshold_db is not None:
                self._threshold_db = max(-40.0, min(0.0, float(threshold_db)))
            if ratio is not None:
                self._ratio = max(1.0, min(20.0, float(ratio)))
            if attack_ms is not None:
                self._attack_ms = max(1.0, min(100.0, float(attack_ms)))
            if release_ms is not None:
                self._release_ms = max(10.0, min(500.0, float(release_ms)))
            if makeup_db is not None:
                self._makeup_db = max(0.0, min(24.0, float(makeup_db)))

    def get_params(self):
        with self._lock:
            return (
                self._threshold_db,
                self._ratio,
                self._attack_ms,
                self._release_ms,
                self._makeup_db,
            )

    def reset_defaults(self):
        self.set_params(
            threshold_db=self.DEFAULT_THRESHOLD_DB,
            ratio=self.DEFAULT_RATIO,
            attack_ms=self.DEFAULT_ATTACK_MS,
            release_ms=self.DEFAULT_RELEASE_MS,
            makeup_db=self.DEFAULT_MAKEUP_DB,
        )

    def reset(self):
        with self._proc_lock:
            self._env = 0.0

    def clone_for_offline(self):
        t, r, a, rel, m = self.get_params()
        return CompressorProcessor(
            threshold_db=t, ratio=r, attack_ms=a, release_ms=rel, makeup_db=m
        )

    def process(self, samples, sample_rate):
        """Apply dynamic compression to mono float samples in [-1.0, 1.0]."""
        with self._proc_lock:
            return self._process_unlocked(samples, sample_rate)

    def _process_unlocked(self, samples, sample_rate):
        n = len(samples)
        if n == 0:
            return samples

        threshold_db, ratio, attack_ms, release_ms, makeup_db = self.get_params()

        if _dsp is not None and _dsp.AVAILABLE:
            in_arr = _dsp.as_f64_array(samples)
            out_arr = _dsp.alloc_f64(n)
            env_c = ctypes.c_double(self._env)
            _dsp.LIB.dsp_compressor_process(
                _dsp.f64_ptr(in_arr),
                _dsp.f64_ptr(out_arr),
                n,
                float(sample_rate),
                threshold_db,
                ratio,
                attack_ms,
                release_ms,
                makeup_db,
                ctypes.byref(env_c),
            )
            self._env = env_c.value
            return out_arr

        sr = float(sample_rate)
        attack_c = math.exp(-1.0 / max(1.0, attack_ms * 0.001 * sr))
        release_c = math.exp(-1.0 / max(1.0, release_ms * 0.001 * sr))
        one_minus_att = 1.0 - attack_c
        one_minus_rel = 1.0 - release_c

        thresh_lin = 10.0 ** (threshold_db / 20.0)
        inv_thresh = 1.0 / max(thresh_lin, 1e-9)
        slope = (1.0 / ratio) - 1.0
        makeup_lin = 10.0 ** (makeup_db / 20.0)

        env = self._env
        out = [0.0] * n

        for i in range(n):
            x = samples[i]
            ax = x if x >= 0.0 else -x
            if ax > env:
                env = attack_c * env + one_minus_att * ax
            else:
                env = release_c * env + one_minus_rel * ax

            if env > thresh_lin and slope < 0.0:
                gain = ((env * inv_thresh) ** slope) * makeup_lin
            else:
                gain = makeup_lin

            out[i] = x * gain

        self._env = env
        return out


class CompressorWindow(EffectDialogBase):
    """Non-blocking PySide6 dialog to control a channel's CompressorProcessor."""

    BG_DARK = "#451e24"
    BG_HEADER = "#33151a"
    BG_WELL = "#260f13"
    ACCENT = "#fc8181"

    def __init__(self, parent=None, processor=None, channel_num=None, on_close=None):
        self.processor = processor if processor is not None else CompressorProcessor()
        title = f"Compressor - Channel {channel_num}" if channel_num is not None else "Compressor"

        super().__init__(
            title=title,
            bg_dark=self.BG_DARK,
            bg_header=self.BG_HEADER,
            bg_well=self.BG_WELL,
            accent=self.ACCENT,
            btn_reset_bg="#662d36",
            btn_reset_hover="#803944",
            btn_close_bg="#9b2c2c",
            btn_close_hover="#bd3737",
            parent=parent,
            on_close=on_close,
            width=350,
        )

        thresh, ratio, attack, release, makeup = self.processor.get_params()

        self.thresh_slider, _ = self.add_param_slider(
            "Threshold",
            -40.0,
            0.0,
            0.5,
            thresh,
            CompressorProcessor.DEFAULT_THRESHOLD_DB,
            lambda v: f"{v:.1f} dB",
            lambda v: self.processor.set_params(threshold_db=v),
        )
        self.ratio_slider, _ = self.add_param_slider(
            "Ratio",
            1.0,
            20.0,
            0.5,
            ratio,
            CompressorProcessor.DEFAULT_RATIO,
            lambda v: f"{v:.1f}:1",
            lambda v: self.processor.set_params(ratio=v),
        )
        self.attack_slider, _ = self.add_param_slider(
            "Attack",
            1.0,
            100.0,
            1.0,
            attack,
            CompressorProcessor.DEFAULT_ATTACK_MS,
            lambda v: f"{v:.0f} ms",
            lambda v: self.processor.set_params(attack_ms=v),
        )
        self.release_slider, _ = self.add_param_slider(
            "Release",
            10.0,
            500.0,
            5.0,
            release,
            CompressorProcessor.DEFAULT_RELEASE_MS,
            lambda v: f"{v:.0f} ms",
            lambda v: self.processor.set_params(release_ms=v),
        )
        self.makeup_slider, _ = self.add_param_slider(
            "Makeup Gain",
            0.0,
            24.0,
            0.5,
            makeup,
            CompressorProcessor.DEFAULT_MAKEUP_DB,
            lambda v: f"+{v:.1f} dB",
            lambda v: self.processor.set_params(makeup_db=v),
        )

        self.reset_btn.clicked.connect(self._reset_defaults)
        self.finalize_and_show()

    def _reset_defaults(self):
        self.processor.reset_defaults()
        thresh, ratio, attack, release, makeup = self.processor.get_params()
        self.thresh_slider.set_value_float(thresh)
        self.ratio_slider.set_value_float(ratio)
        self.attack_slider.set_value_float(attack)
        self.release_slider.set_value_float(release)
        self.makeup_slider.set_value_float(makeup)


# Backward-compatible alias
compressor = CompressorWindow


def Main():
    CompressorWindow()
    return 0


if __name__ == "__main__":
    Main()
