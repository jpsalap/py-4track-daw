# 4-Channel Python Audio Mixer & Multitrack Recorder

A desktop 4-track multitrack recorder, real-time DSP mixer, and DAW-style arrange workstation built in Python with **PySide6 (Qt 6)** and **PyAudio**. Originally written in 2019 as a Tkinter prototype (preserved on the Tkinter branch), the codebase was re-architected into a 64-bit floating-point thread-safe audio engine with an optional native C DSP accelerator and migrated to a modern **Qt 6** interface featuring 30 FPS anti-aliased `QPainter` metering, Retina cached waveforms, drag-and-drop WAV loading, dynamic timeline zoom, and switchable **Studio Dark (Qt 6)** and **Classic Blue (v1.3)** themes.

---

## Architecture Evolution

| Subsystem | 2019 Baseline | 2026 PySide6 / Qt 6 Architecture (`v2.0`) |
| :--- | :--- | :--- |
| **GUI Framework** | Legacy Tkinter with PNG buttons & macOS Aqua workarounds | **PySide6 (Qt 6)** with switchable **Studio Dark** & **Classic Blue (v1.3)** themes (`themes.py`), Retina/HiDPI `QPainter` custom widgets, native menu bar, and resizable `QSplitter` layout |
| **DSP Effects** | Incomplete, copy-pasted stubs with broken popups | Independent, thread-safe per-channel DSP processors (`Compressor`, `Chorus`, `Tremolo`, `Delay`) + stereo-linked `MasterLimiterProcessor` (`-0.3 dBFS`), with floating `QDialog` editors (`EffectDialogBase`) |
| **DSP Acceleration** | Pure-Python sample loops only | Optional native `-O3` C DSP library (`dsp_accel.c` → `libminidaw_dsp.dylib`) via stdlib `ctypes` + 4-core parallel offline FX, with automatic bit-exact pure-Python fallback |
| **Audio I/O & Output** | Legacy Python `<3.9` calls and rigid WAV assumptions | 64-bit float internal summing, stdlib universal `wav_io.py` (8/16/24/32-bit PCM & 32/64-bit IEEE Float WAV), `paFloat32` live output with automatic `paInt16` fallback, and selectable `16-bit` / `24-bit` / `32i` / `32f` (unclipped) bounce with 1-LSB TPDF dithering |
| **Concurrency** | Blocking transport calls and unsafe shared state | Lock-protected `ChannelState` snapshots, `threading.Event` cancellation, background offline `bounce()`, and queued Qt `Signal` UI marshaling (`_UiBridge`) |
| **Transport & Overdub** | Unreliable stop/playback state and incomplete recording | Glitch-free Pause/Resume, Spacebar / Cmd shortcuts, synchronized multithreaded `_rec_worker` overdubbing, and live recording waveform overlay |
| **UI & Visualization** | Basic sliders and no visual feedback | Custom-painted hardware & classic DAW faders with dynamic dB scales, bipolar pan sliders, 30 FPS anti-aliased VU + peak-hold dBFS meters, and a zoomable (`1×`–`50×`) 4-track Peak + RMS Arrange View with drag-and-drop and pinch zoom |

---

## Signal Flow & DSP Architecture

Each of the four channels processes audio through a deterministic real-time signal chain shared across **live playback**, **overdub monitoring**, and **offline bounce**:

```text
[WAV Track / Mic Input]
         │
         ▼
   ┌────────────┐
   │ Compressor │  Threshold, Ratio, Attack, Release, Makeup Gain
   └─────┬──────┘
         ▼
   ┌────────────┐
   │   Chorus   │  LFO Rate, Depth, Base Delay, Dry/Wet Mix (Linear Interpolation)
   └─────┬──────┘
         ▼
   ┌────────────┐
   │  Tremolo   │  Continuous-Phase LFO Rate & Modulation Depth
   └─────┬──────┘
         ▼
   ┌────────────┐
   │   Delay    │  Ring-Buffer Delay Time, Feedback, Dry/Wet Mix, Tail Ring-Out
   └─────┬──────┘
         ▼
  [Polarity Invert (Ø)] ──▶ [Mute / Solo] ──▶ [dB Fader (+6.0 to -∞ dB)]
                                                        │
                                                        ▼
                                             [Constant-Power Pan]
                                                        │
                                                        ▼
                       [Master Bus Gain] ──▶ [Hard Clipping] ──▶ [Stereo Output / Bounce WAV]
```

### Module Breakdown

* **`window.py`** — Main `QMainWindow` DAW console, PyAudio stream lifecycle, thread-safe `ChannelState` management, multithreaded overdub recorder (`_rec_worker`), background offline bounce engine, `_UiBridge` queued signal dispatcher, and 60 FPS meter/playhead loop.
* **`ui_widgets.py`** — Custom HiDPI `QPainter` DAW controls: `WaveformTimeline` (zoomable 4-track Peak + RMS arrange view with drag-and-drop), `ChannelMeter` (60 FPS VU LED meter), `PeakReadout` (tabular monospace dBFS peak/clip badge), `DawFader` + `FaderWithScale`, `PanSlider`, `InsertSlotWidget`, `TogglePadButton`, `TransportButton`, and `ChannelFileWell`.
* **`fx_dialog_base.py`** — Shared dark-studio `QDialog` base (`EffectDialogBase`) and `FloatSlider` with double-click default reset for all DSP popups.
* **`Compressor.py`** — Dynamic range processor & PySide6 dialog (threshold, ratio, attack, release, makeup gain).
* **`Chorus.py`** — Modulated delay-line effect using fractional-sample linear interpolation & PySide6 dialog.
* **`Tremolo.py`** — Continuous-phase LFO amplitude modulation processor & PySide6 dialog.
* **`Delay.py`** — Ring-buffer echo processor with tail ring-out & PySide6 dialog.

---

## Offline Bounce Acceleration & DSP Benchmark

To explore how real-time audio engines eliminate Python interpreter overhead while preserving deterministic, bit-exact DSP math, two acceleration approaches were implemented and benchmarked against the pure-Python baseline on the same 4-track mixed-sample-rate workload (3 tracks at 44.1 kHz stereo + 1 track at 48 kHz mono resampled to 44.1 kHz, Python 3.15 on macOS x86_64):

| Workload (4 tracks, mixed 44.1 / 48 kHz) | Pure Python (Tier 3) | NumPy (Tier 2 Fallback) | Native C (Tier 1 Default) |
| :--- | :---: | :---: | :---: |
| **15 s mix, No FX** | `1.155 s` (13.0× RT) | `0.081 s` (185× RT) | **`0.022 s`** (**698× RT**) |
| **15 s mix, Chorus + Tremolo + Delay** (12 FX) | `5.309 s` (3.3× RT) | `0.228 s` (76.7× RT) | **`0.093 s`** (**188× RT**) |
| **15 s mix, ALL 16 FX** (incl. Compressor) | `5.469 s` (3.2× RT) | `0.906 s` (19.2× RT) | **`0.129 s`** (**135× RT**) |
| **3-minute song, Chorus + Tremolo + Delay** (12 FX) | *~64 s est.* | `2.14 s` (85.3× RT) | **`0.79 s`** (**230× RT**) |
| **3-minute song, ALL 16 FX** (incl. Compressor) | *~65 s est.* | `8.57 s` (21.3× RT) | **`1.19 s`** (**153× RT**) |
| **Output Parity vs. Pure Python** | Reference | **`0 LSB`** (bit-exact) | **`0 LSB`** (bit-exact) |
| **Extra Dependencies / `.app` Size Overhead** | None (0 KB) | Optional `numpy` (excluded from `.app`) | **~52 KB** (`.dylib`, stdlib `ctypes`) |

### Architectural Takeaways
* **Why NumPy bottlenecks on the Compressor:** Non-recursive effects (`Tremolo`, `Chorus`) and fixed-lag `Delay` (processed in dependency-free blocks of size `≤ delay_samples`) vectorize cleanly into NumPy C ufuncs (77–185× realtime). However, the dynamic range `Compressor` uses a **sample-by-sample asymmetric attack/release envelope follower** (`if |x[i]| > env: ... else: ...`), whose per-sample state dependency cannot be vectorized in pure NumPy and falls back to a Python loop — dropping a 3-minute 16-FX bounce from `2.14 s` to `8.57 s`.
* **3-Tier Automatic Fallback (`C → NumPy → Pure Python`):** `dsp_accel.py` loads the compiled C shared library (`libminidaw_dsp`) first for full speed (**153–698× realtime**) and GIL-free 4-core offline FX via `ThreadPoolExecutor`. When running from source on an OS/architecture without a compiled binary, it automatically falls back to **NumPy** if installed (excluded from `.app` bundles to avoid 30–50 MB overhead), and finally to **Pure Python** with zero external dependencies. The active tier is shown in the `STATUS` header badge (`DSP: C` / `DSP: NumPy` / `DSP: Python`) and can be overridden via `MINIDAW_DSP_BACKEND=c|numpy|python|auto`.

---

## Concurrency & Real-Time Threading Model

To prevent audio dropouts and GUI freezes on macOS:

* **Atomic State Snapshots:** UI controls mutate channel parameters behind a `threading.Lock`, allowing the audio callback and offline renderer to read consistent `ChannelState` snapshots without blocking.
* **Non-Blocking Transport:** `stop()` signals a `threading.Event` for immediate cancellation, while `press_pause()` suspends and resumes the PyAudio stream cleanly in place without losing the playhead timestamp.
* **Synchronized Overdub Recording:** `_rec_worker` captures audio on armed channels while playing back non-armed tracks in sync, automatically saving and reloading the recorded WAV file into the track slot upon completion.
* **Qt Signal UI Marshaling:** Background threads (recording, offline `bounce()`, and waveform peak extraction) emit updates via `_UiBridge` (`Qt.ConnectionType.QueuedConnection`) safely onto the Qt main thread, with clean resource release wired to `QMainWindow.closeEvent`.

---

## DAW Console, Metering & Arrange View

* **4-Track Waveform Arrange View (`WaveformTimeline`):**
  * Computes 600-bin dual-layer **Peak + RMS** waveforms in the background with clip title headers and Mute/Solo region dimming.
  * Supports **drag-and-drop `.wav`** loading onto any track lane or channel strip file slot.
  * Supports **timeline zoom** (mouse wheel / `+` / `−` / `100%` / `Cmd+0`) with horizontal scrollbar and auto-follow playhead.
  * Draws live crimson waveforms in real time during recording and supports click-and-drag playhead scrubbing while stopped, playing, or paused.
* **60 FPS VU & True-Peak Metering:**
  * Every channel strip and the Master Bus L/R meter feature ~300 ms rise/fall VU ballistics, peak-hold indicators, and tabular monospace dBFS readouts with inter-tick peak latching, 2.5 s auto-clear, click-to-clear, and red digital clip alerts (`>= 0.0 dBFS`).
* **Console Ergonomics:**
  * Unified DAW insert slots with LED bypass toggles and `⚙` parameter dialogs.
  * Color-coded channel switches (`S Solo`, `M Mute`, `Ø Phase`, `● Rec` with synchronized arming flash and solid-red record state).
  * Custom-painted logarithmic dB faders (`+6.0 dB` to `-∞ dB`, `0.0 dB` unity default, double-click reset) with dynamically aligned painted dB tick scales, bipolar center-detented pan sliders, and per-channel format badges.

---

## Repository Structure

```text
.
├── window.py          # Main DAW console, transport, audio engine & offline bounce
├── ui_widgets.py      # Custom PySide6 QPainter widgets (timeline, meters, faders, transport)
├── fx_dialog_base.py  # Shared PySide6 parameter dialog & FloatSlider for DSP effects
├── Compressor.py      # Per-channel dynamic range compressor & PySide6 dialog
├── Chorus.py          # Per-channel interpolated delay-line chorus & PySide6 dialog
├── Tremolo.py         # Per-channel continuous-phase LFO tremolo & PySide6 dialog
├── Delay.py           # Per-channel ring-buffer delay with tail ring-out & PySide6 dialog
├── requirements.txt   # Python dependencies (PySide6-Essentials, pyaudio)
└── Mini-DAW.spec      # PyInstaller macOS .app bundle specification
```

---

## Getting Started

### Prerequisites

* **Python 3.10+** (uses `array.tobytes()` and PySide6)
* **PySide6-Essentials** (`pip install PySide6-Essentials`)
* **PyAudio** (requires PortAudio on macOS; note that offline Bounce works even without PyAudio)

### Installation & Run

```bash
# Install PortAudio on macOS (for live playback & recording)
brew install portaudio

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Launch Mini-DAW
python3 window.py
```
