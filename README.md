# 4-Channel Python Audio Mixer & Multitrack Recorder

A desktop 4-track multitrack recorder, real-time DSP mixer, and DAW-style arrange workstation built in Python with **Tkinter** and **PyAudio**. Originally written in 2019 as an incomplete prototype, the codebase was re-architected in 2026 into a thread-safe audio engine with independent per-channel effects, synchronized overdub recording, 30 FPS VU/dBFS metering, and a dual-layer waveform timeline.

---

## Architecture Evolution: 2019 Prototype vs. 2026 Overhaul

| Subsystem | 2019 Baseline (`main`) | 2026 Architecture (`feat/antigravity-refactor`) |
| :--- | :--- | :--- |
| **DSP Effects** | Incomplete, copy-pasted stubs with broken parameter popups | Independent, thread-safe per-channel DSP processors (`Compressor`, `Chorus`, `Tremolo`, `Delay`) with non-blocking dark `Toplevel` editors |
| **Signal Routing** | Partial mono/stereo handling, reversed pan axis, and copy-paste pan slider bugs (`set_pan2..4`) | Full per-channel insert chain, polarity invert (`Ø`), Mute/Solo logic, dB faders, constant-power stereo panning, and Master Bus summing |
| **Concurrency** | Blocking transport calls and unsafe shared state across UI and audio callbacks | Lock-protected `ChannelState` snapshots, `threading.Event` cancellation, background cancellable offline `bounce()`, and main-thread `queue.Queue` UI marshaling |
| **Audio I/O** | Legacy Python `<3.9` `array.tostring()` calls and rigid WAV format assumptions | Python 3.9+ `array.tobytes()`, 8/16/24/32-bit mono/multi-channel WAV decoding, phase-accurate `StreamingWavReader` resampling, and automatic device sample-rate fallback |
| **DSP Acceleration** | Pure-Python sample loops only | Optional 33 KB native C DSP library (`dsp_accel.c` → `libminidaw_dsp.dylib`) via stdlib `ctypes` + 4-core parallel offline FX, with automatic pure-Python fallback |
| **Transport & Overdub** | Unreliable stop/playback state and incomplete recording | Glitch-free Pause/Resume (`stream.stop_stream()` / `start_stream()`), Spacebar shortcut, and multithreaded `_rec_worker` overdubbing with delay tails |
| **UI & Visualization** | Legacy PNG buttons, basic sliders, and no visual feedback | Vector-drawn `Canvas` transport pads, DAW insert racks, 30 FPS VU + peak-hold dBFS meters, expanded Master Bus strip, and a zoomable (`1.0×`–`32×`) 4-track Peak + RMS Arrange View |

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

* **`window.py`** — Main DAW console UI, PyAudio stream lifecycle, thread-safe `ChannelState` management, phase-accurate `StreamingWavReader`, multithreaded overdub recorder (`_rec_worker`), parallel offline bounce engine, 30 FPS meter ballistics, and zoomable `WaveformTimeline` canvas.
* **`dsp_accel.c` / `dsp_accel.py`** — Optional native `-O3` C DSP shared library (`libminidaw_dsp.dylib`, 33 KB) loaded via stdlib `ctypes` with zero-copy `array.array('d')` buffers for WAV decoding, linear resampling, all 4 effects, stereo summing/metering, and int16 PCM packing. Falls back automatically to pure Python if the compiled library is absent.
* **`Compressor.py`** — Dynamic range processor featuring configurable threshold, ratio, attack/release envelope ballistics, and makeup gain.
* **`Chorus.py`** — Modulated delay-line effect using fractional-sample linear interpolation with rate, depth, base delay, and dry/wet controls.
* **`Tremolo.py`** — Amplitude modulation processor driven by a continuous-phase LFO to prevent phase discontinuities across buffer boundaries.
* **`Delay.py`** — Ring-buffer echo processor supporting adjustable delay time, feedback regeneration, dry/wet mix, and natural tail ring-out after playback stops.

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

To prevent audio dropouts and Tkinter main-thread freezes on macOS:

* **Atomic State Snapshots:** UI controls mutate channel parameters behind a `threading.Lock`, allowing the audio callback and offline renderer to read consistent `ChannelState` snapshots without blocking.
* **Non-Blocking Transport:** `stop()` signals a `threading.Event` for immediate cancellation of playback, recording, or offline bounce, while `press_pause()` suspends and resumes the PyAudio stream cleanly in place without losing the playhead timestamp.
* **Synchronized Overdub Recording:** `_rec_worker` captures audio on armed channels while playing back non-armed tracks in sync (masking armed channels' Solo state so backing tracks stay audible), automatically saving non-colliding takes and reloading the recorded WAV file into the track slot upon completion.
* **Main-Thread UI Marshaling:** Background threads (playback, recording, offline `bounce()`, and waveform peak extraction) enqueue UI updates into a thread-safe `queue.Queue` drained on the Tkinter main thread inside the 30 FPS `_poll_meters` loop, with clean resource release bound to `WM_DELETE_WINDOW`.

---

## DAW Console, Metering & Arrange View

* **4-Track Waveform Arrange View (`WaveformTimeline`):**
  * Computes 2,400-bin dual-layer **Peak + RMS** waveforms in the background with min/max/RMS bin aggregation, clip title headers, and Mute/Solo region dimming.
  * Supports **horizontal zoom (`1.0×`–`32×`) and panning** via toolbar buttons (`−`, readout, `+`, `Fit`), cursor-anchored mouse wheel/trackpad zoom, `Shift`+scroll pan, a slim overview scrollbar, keyboard shortcuts (`+`/`-`/`0`), tenths-of-a-second (`MM:SS.t`) zoomed ruler ticks, and automatic playhead follow.
  * Draws live crimson waveforms in real time during recording with uniform-stride peak compaction so long takes stay aligned with the time ruler.
  * Renders a time ruler with an $O(1)$ moving playhead supporting click-and-drag scrubbing while stopped, playing, or paused.
* **30 FPS VU & True-Peak Metering:**
  * Every channel strip and the expanded 332 px / 42-segment Master Bus L/R meter feature ~300 ms rise/fall VU ballistics, peak-hold indicators, and numeric dBFS readouts with inter-tick peak latching, 2.5 s auto-clear, and red digital clip alerts (`>= 0.0 dBFS`).
* **Console Ergonomics:**
  * Unified DAW insert slots with LED bypass toggles and `⚙` parameter popup buttons.
  * Color-coded channel switches (`S Solo`, `M Mute`, `Ø Phase`, `● Rec` with synchronized arming flash and solid-red record state).
  * Logarithmic dB faders (`+6.0 dB` to `-∞ dB`, `0.0 dB` unity default, double-click reset) and per-channel format badges (`sample rate · bit depth · Mono/Stereo`).

---

## Repository Structure

```text
.
├── window.py            # Main DAW console, transport, audio engine, metering & arrange view
├── dsp_accel.c          # Native C DSP kernels (decode, resample, 4 FX, mix/meter, PCM pack)
├── dsp_accel.py         # Zero-copy ctypes loader for libminidaw_dsp with pure-Python fallback
├── libminidaw_dsp.dylib # Precompiled macOS x86_64 C DSP shared library (33 KB)
├── Compressor.py        # Per-channel dynamic range compressor & parameter popup
├── Chorus.py            # Per-channel interpolated delay-line chorus & parameter popup
├── Tremolo.py           # Per-channel continuous-phase LFO tremolo & parameter popup
├── Delay.py             # Per-channel ring-buffer delay with tail ring-out & parameter popup
└── Mini-DAW.spec        # PyInstaller specification for standalone macOS Mini-DAW.app bundle
```

---

## Getting Started

### Prerequisites

* **Python 3.9+** (uses `array.tobytes()` and modern `wave` sample-width handling)
* **Tkinter** (included with standard macOS/Windows Python installers)
* **PyAudio** (requires PortAudio on macOS)
* *(Optional)* **Clang / GCC** if rebuilding the native C DSP accelerator on a different OS or CPU architecture (otherwise Mini-DAW runs automatically using its pure-Python fallback)

### Installation & Run

```bash
# Install PortAudio on macOS (if not already installed)
brew install portaudio

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install PyAudio
pip install pyaudio

# (Optional) Rebuild the native C DSP accelerator for your CPU/OS
clang -O3 -shared -fPIC -o libminidaw_dsp.dylib dsp_accel.c -lm

# Launch the mixer
python3 window.py
```