# Compressor Effect (DSP Processor + Tkinter Popup Window)
import ctypes
import math
import threading
import tkinter as tk

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
        self._env = 0.0

    def clone_for_offline(self):
        t, r, a, rel, m = self.get_params()
        return CompressorProcessor(
            threshold_db=t, ratio=r, attack_ms=a, release_ms=rel, makeup_db=m
        )

    def process(self, samples, sample_rate):
        """Apply dynamic compression to mono float samples in [-1.0, 1.0]."""
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


class CompressorWindow:
    """Non-blocking Toplevel popup to control a channel's CompressorProcessor."""

    BG_DARK = "#451e24"
    BG_HEADER = "#33151a"
    BG_WELL = "#260f13"
    ACCENT = "#fc8181"

    def __init__(self, parent=None, processor=None, channel_num=None, on_close=None):
        self.processor = processor if processor is not None else CompressorProcessor()
        self.on_close_callback = on_close
        self._standalone = parent is None

        if self._standalone:
            self.window = tk.Tk()
        else:
            self.window = tk.Toplevel(parent)
            self.window.transient(parent)

        title = f"Compressor - Channel {channel_num}" if channel_num is not None else "Compressor"
        self.window.title(title)
        self.window.geometry("330x370")
        self.window.resizable(False, False)
        self.window.configure(bg=self.BG_DARK)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        header = tk.Frame(self.window, bg=self.BG_HEADER, height=30)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(
            header, text=title.upper(), bg=self.BG_HEADER, fg=self.ACCENT,
            font=("Helvetica", 10, "bold")
        ).pack(expand=True)

        thresh, ratio, attack, release, makeup = self.processor.get_params()

        self.thresh_label_var = tk.StringVar(value=f"Threshold: {thresh:.1f} dB")
        self.ratio_label_var = tk.StringVar(value=f"Ratio: {ratio:.1f}:1")
        self.attack_label_var = tk.StringVar(value=f"Attack: {attack:.0f} ms")
        self.release_label_var = tk.StringVar(value=f"Release: {release:.0f} ms")
        self.makeup_label_var = tk.StringVar(value=f"Makeup Gain: +{makeup:.1f} dB")

        container = tk.Frame(self.window, bg=self.BG_DARK, padx=16, pady=12)
        container.pack(fill=tk.BOTH, expand=True)

        tk.Label(container, textvariable=self.thresh_label_var, bg=self.BG_DARK, fg="#f0f6fc",
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        self.thresh_slider = tk.Scale(
            container, from_=-40.0, to=0.0, resolution=0.5, orient=tk.HORIZONTAL,
            length=285, showvalue=False, troughcolor=self.BG_WELL, bg=self.BG_DARK,
            activebackground=self.ACCENT, fg="white", highlightthickness=0, bd=0,
            command=self._on_thresh_change
        )
        self.thresh_slider.set(thresh)
        self.thresh_slider.pack(fill=tk.X, pady=(1, 6))

        tk.Label(container, textvariable=self.ratio_label_var, bg=self.BG_DARK, fg="#f0f6fc",
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        self.ratio_slider = tk.Scale(
            container, from_=1.0, to=20.0, resolution=0.5, orient=tk.HORIZONTAL,
            length=285, showvalue=False, troughcolor=self.BG_WELL, bg=self.BG_DARK,
            activebackground=self.ACCENT, fg="white", highlightthickness=0, bd=0,
            command=self._on_ratio_change
        )
        self.ratio_slider.set(ratio)
        self.ratio_slider.pack(fill=tk.X, pady=(1, 6))

        tk.Label(container, textvariable=self.attack_label_var, bg=self.BG_DARK, fg="#f0f6fc",
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        self.attack_slider = tk.Scale(
            container, from_=1.0, to=100.0, resolution=1.0, orient=tk.HORIZONTAL,
            length=285, showvalue=False, troughcolor=self.BG_WELL, bg=self.BG_DARK,
            activebackground=self.ACCENT, fg="white", highlightthickness=0, bd=0,
            command=self._on_attack_change
        )
        self.attack_slider.set(attack)
        self.attack_slider.pack(fill=tk.X, pady=(1, 6))

        tk.Label(container, textvariable=self.release_label_var, bg=self.BG_DARK, fg="#f0f6fc",
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        self.release_slider = tk.Scale(
            container, from_=10.0, to=500.0, resolution=5.0, orient=tk.HORIZONTAL,
            length=285, showvalue=False, troughcolor=self.BG_WELL, bg=self.BG_DARK,
            activebackground=self.ACCENT, fg="white", highlightthickness=0, bd=0,
            command=self._on_release_change
        )
        self.release_slider.set(release)
        self.release_slider.pack(fill=tk.X, pady=(1, 6))

        tk.Label(container, textvariable=self.makeup_label_var, bg=self.BG_DARK, fg="#f0f6fc",
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        self.makeup_slider = tk.Scale(
            container, from_=0.0, to=24.0, resolution=0.5, orient=tk.HORIZONTAL,
            length=285, showvalue=False, troughcolor=self.BG_WELL, bg=self.BG_DARK,
            activebackground=self.ACCENT, fg="white", highlightthickness=0, bd=0,
            command=self._on_makeup_change
        )
        self.makeup_slider.set(makeup)
        self.makeup_slider.pack(fill=tk.X, pady=(1, 12))

        btn_frame = tk.Frame(container, bg=self.BG_DARK)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self._make_btn(btn_frame, "Reset", self._reset_defaults, "#662d36", "#803944").pack(side=tk.LEFT)
        self._make_btn(btn_frame, "Close", self.close, "#9b2c2c", "#bd3737").pack(side=tk.RIGHT)

        if self._standalone:
            self.window.mainloop()

    def _make_btn(self, parent, text, command, bg, hover_bg):
        lbl = tk.Label(
            parent, text=text, bg=bg, fg="#ffffff", font=("Helvetica", 9, "bold"),
            padx=14, pady=5, cursor="hand2"
        )
        lbl.bind("<Enter>", lambda _e: lbl.config(bg=hover_bg))
        lbl.bind("<Leave>", lambda _e: lbl.config(bg=bg))

        def on_release(event):
            try:
                x = event.x_root - lbl.winfo_rootx()
                y = event.y_root - lbl.winfo_rooty()
                inside = 0 <= x <= lbl.winfo_width() and 0 <= y <= lbl.winfo_height()
            except Exception:
                inside = True
            if inside:
                command()

        lbl.bind("<ButtonRelease-1>", on_release)
        return lbl

    def _on_thresh_change(self, value):
        val = float(value)
        self.processor.set_params(threshold_db=val)
        self.thresh_label_var.set(f"Threshold: {val:.1f} dB")

    def _on_ratio_change(self, value):
        val = float(value)
        self.processor.set_params(ratio=val)
        self.ratio_label_var.set(f"Ratio: {val:.1f}:1")

    def _on_attack_change(self, value):
        val = float(value)
        self.processor.set_params(attack_ms=val)
        self.attack_label_var.set(f"Attack: {val:.0f} ms")

    def _on_release_change(self, value):
        val = float(value)
        self.processor.set_params(release_ms=val)
        self.release_label_var.set(f"Release: {val:.0f} ms")

    def _on_makeup_change(self, value):
        val = float(value)
        self.processor.set_params(makeup_db=val)
        self.makeup_label_var.set(f"Makeup Gain: +{val:.1f} dB")

    def _reset_defaults(self):
        self.processor.reset_defaults()
        thresh, ratio, attack, release, makeup = self.processor.get_params()
        self.thresh_slider.set(thresh)
        self.ratio_slider.set(ratio)
        self.attack_slider.set(attack)
        self.release_slider.set(release)
        self.makeup_slider.set(makeup)

    def focus(self):
        if self.is_open():
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()

    def is_open(self):
        try:
            return bool(self.window.winfo_exists())
        except tk.TclError:
            return False

    def close(self):
        if self.on_close_callback is not None:
            cb = self.on_close_callback
            self.on_close_callback = None
            cb()
        if self.is_open():
            self.window.destroy()


# Backward-compatible alias
compressor = CompressorWindow


def Main():
    CompressorWindow()
    return 0


if __name__ == "__main__":
    Main()
