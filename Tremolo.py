# Tremolo Effect (DSP Processor + Tkinter Popup Window)
import math
import threading
import tkinter as tk
from tkinter import ttk


class TremoloProcessor:
    """Thread-safe real-time LFO tremolo audio processor."""

    DEFAULT_RATE = 4.0     # Hz
    DEFAULT_DEPTH = 0.6    # 0.0 to 1.0

    def __init__(self, rate=DEFAULT_RATE, depth=DEFAULT_DEPTH):
        self._lock = threading.Lock()
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
        self._phase = 0.0

    def clone_for_offline(self):
        rate, depth = self.get_params()
        return TremoloProcessor(rate=rate, depth=depth)

    def process(self, samples, sample_rate):
        """Apply tremolo to a list of mono float samples in [-1.0, 1.0]."""
        rate, depth = self.get_params()
        n = len(samples)
        if n == 0:
            return samples

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


class TremoloWindow:
    """Non-blocking Toplevel popup to control a channel's TremoloProcessor."""

    BG_DARK = "#1c332d"
    BG_HEADER = "#152722"
    BG_WELL = "#12211d"
    ACCENT = "#48d18a"

    def __init__(self, parent=None, processor=None, channel_num=None, on_close=None):
        self.processor = processor if processor is not None else TremoloProcessor()
        self.on_close_callback = on_close
        self._standalone = parent is None

        if self._standalone:
            self.window = tk.Tk()
        else:
            self.window = tk.Toplevel(parent)
            self.window.transient(parent)

        title = f"Tremolo - Channel {channel_num}" if channel_num is not None else "Tremolo"
        self.window.title(title)
        self.window.geometry("320x220")
        self.window.resizable(False, False)
        self.window.configure(bg=self.BG_DARK)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        # Header bar
        header = tk.Frame(self.window, bg=self.BG_HEADER, height=30)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(
            header, text=title.upper(), bg=self.BG_HEADER, fg=self.ACCENT,
            font=("Helvetica", 10, "bold")
        ).pack(expand=True)

        rate, depth = self.processor.get_params()

        self.rate_label_var = tk.StringVar(value=f"Rate: {rate:.2f} Hz")
        self.depth_label_var = tk.StringVar(value=f"Depth: {int(depth * 100)}%")

        container = tk.Frame(self.window, bg=self.BG_DARK, padx=16, pady=12)
        container.pack(fill=tk.BOTH, expand=True)

        tk.Label(container, textvariable=self.rate_label_var, bg=self.BG_DARK, fg="#f0f6fc",
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        self.rate_slider = tk.Scale(
            container, from_=0.5, to=15.0, resolution=0.1, orient=tk.HORIZONTAL,
            length=275, showvalue=False, troughcolor=self.BG_WELL, bg=self.BG_DARK,
            activebackground=self.ACCENT, fg="white", highlightthickness=0, bd=0,
            command=self._on_rate_change
        )
        self.rate_slider.set(rate)
        self.rate_slider.pack(fill=tk.X, pady=(2, 10))

        tk.Label(container, textvariable=self.depth_label_var, bg=self.BG_DARK, fg="#f0f6fc",
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        self.depth_slider = tk.Scale(
            container, from_=0.0, to=1.0, resolution=0.01, orient=tk.HORIZONTAL,
            length=275, showvalue=False, troughcolor=self.BG_WELL, bg=self.BG_DARK,
            activebackground=self.ACCENT, fg="white", highlightthickness=0, bd=0,
            command=self._on_depth_change
        )
        self.depth_slider.set(depth)
        self.depth_slider.pack(fill=tk.X, pady=(2, 14))

        btn_frame = tk.Frame(container, bg=self.BG_DARK)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self._make_btn(btn_frame, "Reset", self._reset_defaults, "#2a4d43", "#376659").pack(side=tk.LEFT)
        self._make_btn(btn_frame, "Close", self.close, "#246e4c", "#2e8a60").pack(side=tk.RIGHT)

        if self._standalone:
            self.window.mainloop()

    def _make_btn(self, parent, text, command, bg, hover_bg):
        lbl = tk.Label(
            parent, text=text, bg=bg, fg="#ffffff", font=("Helvetica", 9, "bold"),
            padx=14, pady=5, cursor="hand2"
        )
        lbl.bind("<Enter>", lambda _e: lbl.config(bg=hover_bg))
        lbl.bind("<Leave>", lambda _e: lbl.config(bg=bg))
        lbl.bind("<ButtonRelease-1>", lambda _e: command())
        return lbl

    def _on_rate_change(self, value):
        val = float(value)
        self.processor.set_params(rate=val)
        self.rate_label_var.set(f"Rate: {val:.2f} Hz")

    def _on_depth_change(self, value):
        val = float(value)
        self.processor.set_params(depth=val)
        self.depth_label_var.set(f"Depth: {int(round(val * 100))}%")

    def _reset_defaults(self):
        self.processor.reset_defaults()
        rate, depth = self.processor.get_params()
        self.rate_slider.set(rate)
        self.depth_slider.set(depth)

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
tremolo = TremoloWindow


def Main():
    TremoloWindow()
    return 0


if __name__ == "__main__":
    Main()
