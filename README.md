# 4-Channel Python Audio Mixer & Multitrack Recorder

A desktop 4-track multitrack recorder, real-time DSP mixer, and DAW-style arrange workstation built in Python with **Tkinter** and **PyAudio**. Originally written in 2019 as an incomplete prototype, the codebase was re-architected in 2026 into a thread-safe audio engine with independent per-channel effects, synchronized overdub recording, 30 FPS VU/dBFS metering, and a dual-layer waveform timeline.

---

## Architecture Evolution: 2019 Prototype vs. 2026 Overhaul

| Subsystem | 2019 Baseline (`main`) | 2026 Architecture (`feat/antigravity-refactor`) |
| :--- | :--- | :--- |
| **DSP Effects** | Incomplete, copy-pasted stubs with broken parameter popups | Independent, thread-safe per-channel DSP processors (`Compressor`, `Chorus`, `Tremolo`, `Delay`) with non-blocking dark `Toplevel` editors |
| **Signal Routing** | Partial mono/stereo handling, reversed pan axis, and copy-paste pan slider bugs (`set_pan2..4`) | Full per-channel insert chain, polarity invert (`Ø`), Mute/Solo logic, dB faders, constant-power stereo panning, and Master Bus summing |
| **Concurrency** | Blocking transport calls and unsafe shared state across UI and audio callbacks | Lock-protected `ChannelState` snapshots, `threading.Event` cancellation, background offline `bounce()`, and `root.after(0, ...)` UI marshaling |
| **Audio I/O** | Legacy Python `<3.9` `array.tostring()` calls and rigid WAV format assumptions | Python 3.9+ `array.tobytes()`, 8/16/24/32-bit mono/stereo WAV decoding, linear resampling, and automatic device sample-rate fallback |
| **Transport & Overdub** | Unreliable stop/playback state and incomplete recording | Glitch-free Pause/Resume (`stream.stop_stream()` / `start_stream()`), Spacebar shortcut, and multithreaded `_rec_worker` overdubbing |
| **UI & Visualization** | Legacy PNG buttons, basic sliders, and no visual feedback | Vector-drawn `Canvas` transport pads, DAW insert racks, 30 FPS VU + peak-hold dBFS meters, and a 4-track Peak + RMS Arrange View |

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

* **`window.py`** — Main DAW console UI, PyAudio stream lifecycle, thread-safe `ChannelState` management, multithreaded overdub recorder (`_rec_worker`), background offline bounce engine, 30 FPS meter ballistics, and `WaveformTimeline` canvas.
* **`Compressor.py`** — Dynamic range processor featuring configurable threshold, ratio, attack/release envelope ballistics, and makeup gain.
* **`Chorus.py`** — Modulated delay-line effect using fractional-sample linear interpolation with rate, depth, base delay, and dry/wet controls.
* **`Tremolo.py`** — Amplitude modulation processor driven by a continuous-phase LFO to prevent phase discontinuities across buffer boundaries.
* **`Delay.py`** — Ring-buffer echo processor supporting adjustable delay time, feedback regeneration, dry/wet mix, and natural tail ring-out after playback stops.

---

## Concurrency & Real-Time Threading Model

To prevent audio dropouts and Tkinter main-thread freezes on macOS:

* **Atomic State Snapshots:** UI controls mutate channel parameters behind a `threading.Lock`, allowing the audio callback and offline renderer to read consistent `ChannelState` snapshots without blocking.
* **Non-Blocking Transport:** `stop()` signals a `threading.Event` for immediate cancellation, while `press_pause()` suspends and resumes the PyAudio stream cleanly in place without losing the playhead timestamp.
* **Synchronized Overdub Recording:** `_rec_worker` captures audio on armed channels while playing back non-armed tracks in sync, automatically saving and reloading the recorded WAV file into the track slot upon completion.
* **Main-Thread UI Marshaling:** Background threads (recording, offline `bounce()`, and waveform peak extraction) push progress updates and completion callbacks safely to Tkinter via `root.after(0, ...)`, with clean resource release bound to `WM_DELETE_WINDOW`.

---

## DAW Console, Metering & Arrange View

* **4-Track Waveform Arrange View (`WaveformTimeline`):**
  * Computes 600-bin dual-layer **Peak + RMS** waveforms in the background with clip title headers and Mute/Solo region dimming.
  * Draws live crimson waveforms in real time during recording.
  * Renders a time ruler with an $O(1)$ moving playhead supporting click-and-drag scrubbing while stopped, playing, or paused.
* **30 FPS VU & True-Peak Metering:**
  * Every channel strip and the Master Bus L/R meter feature ~300 ms rise/fall VU ballistics, peak-hold indicators, and numeric dBFS readouts with inter-tick peak latching, 2.5 s auto-clear, and red digital clip alerts (`>= 0.0 dBFS`).
* **Console Ergonomics:**
  * Unified DAW insert slots with LED bypass toggles and `⚙` parameter popup buttons.
  * Color-coded channel switches (`S Solo`, `M Mute`, `Ø Phase`, `● Rec` with synchronized arming flash and solid-red record state).
  * Logarithmic dB faders (`+6.0 dB` to `-∞ dB`, `0.0 dB` unity default, double-click reset) and per-channel format badges (`sample rate · bit depth · Mono/Stereo`).

---

## Repository Structure

```text
.
├── window.py        # Main DAW console, transport, audio engine, metering & arrange view
├── Compressor.py    # Per-channel dynamic range compressor & parameter popup
├── Chorus.py        # Per-channel interpolated delay-line chorus & parameter popup
├── Tremolo.py       # Per-channel continuous-phase LFO tremolo & parameter popup
└── Delay.py         # Per-channel ring-buffer delay with tail ring-out & parameter popup
```

---

## Getting Started

### Prerequisites

* **Python 3.9+** (uses `array.tobytes()` and modern `wave` sample-width handling)
* **Tkinter** (included with standard macOS/Windows Python installers)
* **PyAudio** (requires PortAudio on macOS)

### Installation & Run

```bash
# Install PortAudio on macOS (if not already installed)
brew install portaudio

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install PyAudio
pip install pyaudio

# Launch the mixer
python3 window.py
```