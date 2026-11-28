# Chorus Effect (DSP Processor + Tkinter Popup Window)
import ctypes
import math
import threading
import tkinter as tk

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


class ChorusWindow:
    """Non-blocking Toplevel popup to control a channel's ChorusProcessor."""

    BG_DARK = "#173c42"
    BG_HEADER = "#112e33"
    BG_WELL = "#0d2326"
    ACCENT = "#6be0eb"

    def __init__(self, parent=None, processor=None, channel_num=None, on_close=None):
        self.processor = processor if processor is not None else ChorusProcessor()
        self.on_close_callback = on_close
        self._standalone = parent is None

        if self._standalone:
            self.window = tk.Tk()
        else:
            self.window = tk.Toplevel(parent)
            self.window.transient(parent)

        title = f"Chorus - Channel {channel_num}" if channel_num is not None else "Chorus"
        self.window.title(title)
        self.window.geometry("320x320")
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

        rate, depth_ms, base_delay_ms, mix = self.processor.get_params()

        self.rate_label_var = tk.StringVar(value=f"Rate: {rate:.2f} Hz")
        self.depth_label_var = tk.StringVar(value=f"Depth: {depth_ms:.1f} ms")
        self.delay_label_var = tk.StringVar(value=f"Base Delay: {base_delay_ms:.1f} ms")
        self.mix_label_var = tk.StringVar(value=f"Wet/Dry Mix: {int(round(mix * 100))}%")

        container = tk.Frame(self.window, bg=self.BG_DARK, padx=16, pady=12)
        container.pack(fill=tk.BOTH, expand=True)

        tk.Label(container, textvariable=self.rate_label_var, bg=self.BG_DARK, fg="#f0f6fc",
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        self.rate_slider = tk.Scale(
            container, from_=0.1, to=5.0, resolution=0.05, orient=tk.HORIZONTAL,
            length=275, showvalue=False, troughcolor=self.BG_WELL, bg=self.BG_DARK,
            activebackground=self.ACCENT, fg="white", highlightthickness=0, bd=0,
            command=self._on_rate_change
        )
        self.rate_slider.set(rate)
        self.rate_slider.pack(fill=tk.X, pady=(2, 6))

        tk.Label(container, textvariable=self.depth_label_var, bg=self.BG_DARK, fg="#f0f6fc",
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        self.depth_slider = tk.Scale(
            container, from_=0.5, to=15.0, resolution=0.5, orient=tk.HORIZONTAL,
            length=275, showvalue=False, troughcolor=self.BG_WELL, bg=self.BG_DARK,
            activebackground=self.ACCENT, fg="white", highlightthickness=0, bd=0,
            command=self._on_depth_change
        )
        self.depth_slider.set(depth_ms)
        self.depth_slider.pack(fill=tk.X, pady=(2, 6))

        tk.Label(container, textvariable=self.delay_label_var, bg=self.BG_DARK, fg="#f0f6fc",
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        self.delay_slider = tk.Scale(
            container, from_=5.0, to=35.0, resolution=0.5, orient=tk.HORIZONTAL,
            length=275, showvalue=False, troughcolor=self.BG_WELL, bg=self.BG_DARK,
            activebackground=self.ACCENT, fg="white", highlightthickness=0, bd=0,
            command=self._on_delay_change
        )
        self.delay_slider.set(base_delay_ms)
        self.delay_slider.pack(fill=tk.X, pady=(2, 6))

        tk.Label(container, textvariable=self.mix_label_var, bg=self.BG_DARK, fg="#f0f6fc",
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        self.mix_slider = tk.Scale(
            container, from_=0.0, to=1.0, resolution=0.01, orient=tk.HORIZONTAL,
            length=275, showvalue=False, troughcolor=self.BG_WELL, bg=self.BG_DARK,
            activebackground=self.ACCENT, fg="white", highlightthickness=0, bd=0,
            command=self._on_mix_change
        )
        self.mix_slider.set(mix)
        self.mix_slider.pack(fill=tk.X, pady=(2, 12))

        btn_frame = tk.Frame(container, bg=self.BG_DARK)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self._make_btn(btn_frame, "Reset", self._reset_defaults, "#23545c", "#2d6c75").pack(side=tk.LEFT)
        self._make_btn(btn_frame, "Close", self.close, "#1c747d", "#24929e").pack(side=tk.RIGHT)

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

    def _on_rate_change(self, value):
        val = float(value)
        self.processor.set_params(rate=val)
        self.rate_label_var.set(f"Rate: {val:.2f} Hz")

    def _on_depth_change(self, value):
        val = float(value)
        self.processor.set_params(depth_ms=val)
        self.depth_label_var.set(f"Depth: {val:.1f} ms")

    def _on_delay_change(self, value):
        val = float(value)
        self.processor.set_params(base_delay_ms=val)
        self.delay_label_var.set(f"Base Delay: {val:.1f} ms")

    def _on_mix_change(self, value):
        val = float(value)
        self.processor.set_params(mix=val)
        self.mix_label_var.set(f"Wet/Dry Mix: {int(round(val * 100))}%")

    def _reset_defaults(self):
        self.processor.reset_defaults()
        rate, depth_ms, base_delay_ms, mix = self.processor.get_params()
        self.rate_slider.set(rate)
        self.depth_slider.set(depth_ms)
        self.delay_slider.set(base_delay_ms)
        self.mix_slider.set(mix)

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
chorus = ChorusWindow


def Main():
    ChorusWindow()
    return 0


if __name__ == "__main__":
    Main()
