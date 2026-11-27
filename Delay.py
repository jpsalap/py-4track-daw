# Delay Effect (DSP Processor + Tkinter Popup Window)
import threading
import tkinter as tk


class DelayProcessor:
    """Thread-safe real-time feedback delay line processor."""

    DEFAULT_DELAY_MS = 300.0   # ms
    DEFAULT_FEEDBACK = 0.40    # 0.0 to 0.85
    DEFAULT_MIX = 0.40         # 0.0 (dry) to 1.0 (wet)
    MAX_DELAY_SEC = 1.1

    def __init__(self, delay_ms=DEFAULT_DELAY_MS, feedback=DEFAULT_FEEDBACK, mix=DEFAULT_MIX):
        self._lock = threading.Lock()
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
        if self._buf_len > 0:
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
            self._buffer = [0.0] * self._buf_len
            self._write_idx = 0

    def process(self, samples, sample_rate):
        """Apply feedback delay to a list of mono float samples in [-1.0, 1.0]."""
        n = len(samples)
        if n == 0:
            return samples

        self._ensure_buffer(sample_rate)
        delay_ms, feedback, mix = self.get_params()

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


class DelayWindow:
    """Non-blocking Toplevel popup to control a channel's DelayProcessor."""

    BG_DARK = "#1b304a"
    BG_HEADER = "#142438"
    BG_WELL = "#101e2e"
    ACCENT = "#63b3ed"

    def __init__(self, parent=None, processor=None, channel_num=None, on_close=None):
        self.processor = processor if processor is not None else DelayProcessor()
        self.on_close_callback = on_close
        self._standalone = parent is None

        if self._standalone:
            self.window = tk.Tk()
        else:
            self.window = tk.Toplevel(parent)
            self.window.transient(parent)

        title = f"Delay - Channel {channel_num}" if channel_num is not None else "Delay"
        self.window.title(title)
        self.window.geometry("320x270")
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

        delay_ms, feedback, mix = self.processor.get_params()

        self.time_label_var = tk.StringVar(value=f"Delay Time: {int(round(delay_ms))} ms")
        self.fb_label_var = tk.StringVar(value=f"Feedback: {int(round(feedback * 100))}%")
        self.mix_label_var = tk.StringVar(value=f"Wet/Dry Mix: {int(round(mix * 100))}%")

        container = tk.Frame(self.window, bg=self.BG_DARK, padx=16, pady=12)
        container.pack(fill=tk.BOTH, expand=True)

        tk.Label(container, textvariable=self.time_label_var, bg=self.BG_DARK, fg="#f0f6fc",
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        self.time_slider = tk.Scale(
            container, from_=10, to=1000, resolution=5, orient=tk.HORIZONTAL,
            length=275, showvalue=False, troughcolor=self.BG_WELL, bg=self.BG_DARK,
            activebackground=self.ACCENT, fg="white", highlightthickness=0, bd=0,
            command=self._on_time_change
        )
        self.time_slider.set(delay_ms)
        self.time_slider.pack(fill=tk.X, pady=(2, 8))

        tk.Label(container, textvariable=self.fb_label_var, bg=self.BG_DARK, fg="#f0f6fc",
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        self.fb_slider = tk.Scale(
            container, from_=0.0, to=0.85, resolution=0.01, orient=tk.HORIZONTAL,
            length=275, showvalue=False, troughcolor=self.BG_WELL, bg=self.BG_DARK,
            activebackground=self.ACCENT, fg="white", highlightthickness=0, bd=0,
            command=self._on_fb_change
        )
        self.fb_slider.set(feedback)
        self.fb_slider.pack(fill=tk.X, pady=(2, 8))

        tk.Label(container, textvariable=self.mix_label_var, bg=self.BG_DARK, fg="#f0f6fc",
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        self.mix_slider = tk.Scale(
            container, from_=0.0, to=1.0, resolution=0.01, orient=tk.HORIZONTAL,
            length=275, showvalue=False, troughcolor=self.BG_WELL, bg=self.BG_DARK,
            activebackground=self.ACCENT, fg="white", highlightthickness=0, bd=0,
            command=self._on_mix_change
        )
        self.mix_slider.set(mix)
        self.mix_slider.pack(fill=tk.X, pady=(2, 14))

        btn_frame = tk.Frame(container, bg=self.BG_DARK)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self._make_btn(btn_frame, "Reset", self._reset_defaults, "#2a4669", "#375c8a").pack(side=tk.LEFT)
        self._make_btn(btn_frame, "Close", self.close, "#245994", "#2e6eb5").pack(side=tk.RIGHT)

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

    def _on_time_change(self, value):
        val = float(value)
        self.processor.set_params(delay_ms=val)
        self.time_label_var.set(f"Delay Time: {int(round(val))} ms")

    def _on_fb_change(self, value):
        val = float(value)
        self.processor.set_params(feedback=val)
        self.fb_label_var.set(f"Feedback: {int(round(val * 100))}%")

    def _on_mix_change(self, value):
        val = float(value)
        self.processor.set_params(mix=val)
        self.mix_label_var.set(f"Wet/Dry Mix: {int(round(val * 100))}%")

    def _reset_defaults(self):
        self.processor.reset_defaults()
        delay_ms, feedback, mix = self.processor.get_params()
        self.time_slider.set(delay_ms)
        self.fb_slider.set(feedback)
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
delay = DelayWindow


def Main():
    DelayWindow()
    return 0


if __name__ == "__main__":
    Main()
