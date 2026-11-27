# 4-Channel Python Audio Mixer & Multitrack Recorder

A desktop 4-track multitrack recorder, real-time DSP mixer, and DAW-style arrange workstation built in Python with **PySide6 (Qt 6)** and **PyAudio**. Originally written in 2019 as a Tkinter prototype, the codebase was re-architected into a thread-safe audio engine and migrated to a modern **Qt 6** interface featuring 60 FPS anti-aliased `QPainter` metering and waveforms, drag-and-drop WAV loading, timeline zoom, and a resizable console layout.

---

## Architecture Evolution

| Subsystem | 2019 Baseline (`main`) | 2026 PySide6 / Qt 6 Architecture |
| :--- | :--- | :--- |
| **GUI Framework** | Legacy Tkinter with PNG buttons & macOS Aqua workarounds | **PySide6 (Qt 6)** with dark-studio QSS, Retina/HiDPI `QPainter` custom widgets, native menu bar, and resizable `QSplitter` layout |
| **DSP Effects** | Incomplete, copy-pasted stubs with broken popups | Independent, thread-safe per-channel DSP processors (`Compressor`, `Chorus`, `Tremolo`, `Delay`) with non-blocking dark `QDialog` editors (`EffectDialogBase`) |
| **Signal Routing** | Partial mono/stereo handling and pan bugs | Full per-channel insert chain, polarity invert (`Ø`), Mute/Solo logic, dB faders, constant-power stereo panning, and Master Bus summing |
| **Concurrency** | Blocking transport calls and unsafe shared state | Lock-protected `ChannelState` snapshots, `threading.Event` cancellation, background offline `bounce()`, and queued Qt `Signal` UI marshaling (`_UiBridge`) |
| **Audio I/O** | Legacy Python `<3.9` calls and rigid WAV assumptions | Python 3.9+ `array.tobytes()`, 8/16/24/32-bit mono/stereo WAV decoding, linear resampling, and automatic device sample-rate fallback |
| **Transport & Overdub** | Unreliable stop/playback state and incomplete recording | Glitch-free Pause/Resume, Spacebar / Cmd shortcuts, synchronized multithreaded `_rec_worker` overdubbing, and live recording waveform overlay |
| **UI & Visualization** | Basic sliders and no visual feedback | Custom-painted DAW faders with dynamic dB scales, bipolar pan sliders, 60 FPS anti-aliased VU + peak-hold dBFS meters, and a zoomable 4-track Peak + RMS Arrange View with drag-and-drop |

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
