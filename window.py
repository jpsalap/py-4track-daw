import array
import ctypes
import math
import os
import queue
import re
import sys
import threading
import time
import wave
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import font, messagebox
from tkinter.filedialog import askopenfilename, asksaveasfilename

try:
    import dsp_accel as _dsp
except Exception:
    _dsp = None

try:
    import pyaudio
    _PYAUDIO_IMPORT_ERROR = None
except Exception as _exc:
    pyaudio = None
    _PYAUDIO_IMPORT_ERROR = _exc

from Delay import DelayProcessor, DelayWindow
from Tremolo import TremoloProcessor, TremoloWindow
from Chorus import ChorusProcessor, ChorusWindow
from Compressor import CompressorProcessor, CompressorWindow


if getattr(sys, "frozen", False):
    BASE_DIR = os.path.join(os.path.expanduser("~"), "Music", "Mini-DAW")
    os.makedirs(BASE_DIR, exist_ok=True)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MIN_FADER_DB = -60.0
MAX_FADER_DB = 6.0


def db_to_linear(db):
    """Convert a DAW fader value in dB to linear amplitude gain (-60 dB = -inf / silence)."""
    val = float(db)
    if val <= MIN_FADER_DB:
        return 0.0
    return 10.0 ** (val / 20.0)


def format_fader_db(db):
    """Format a fader dB value for display (e.g. '+3.0 dB', '0.0 dB', '-12.5 dB', '-∞ dB')."""
    val = float(db)
    if val <= MIN_FADER_DB:
        return "-∞ dB"
    if abs(val) < 0.05:
        return "0.0 dB"
    if val > 0.0:
        return f"+{val:.1f} dB"
    return f"{val:.1f} dB"


def format_audio_meta(sample_rate, bit_depth, channels):
    """Format WAV metadata concisely (e.g. '44.1 kHz · 16-bit · Stereo')."""
    khz = sample_rate / 1000.0
    sr_txt = f"{int(khz)} kHz" if abs(khz - round(khz)) < 1e-3 else f"{khz:.1f} kHz"
    ch_txt = "Mono" if channels == 1 else ("Stereo" if channels == 2 else f"{channels}ch")
    return f"{sr_txt} · {bit_depth}-bit · {ch_txt}"

ChannelSnapshot = namedtuple(
    "ChannelSnapshot",
    [
        "index",
        "filepath",
        "volume",
        "pan",
        "mute",
        "solo",
        "polarity_invert",
        "rec_armed",
        "delay_enabled",
        "tremolo_enabled",
        "chorus_enabled",
        "compressor_enabled",
        "delay",
        "tremolo",
        "chorus",
        "compressor",
    ],
)


def _is_pcm_silent(pcm_bytes, threshold=4):
    """Return True if all 16-bit PCM samples in pcm_bytes have abs(v) < threshold."""
    if not pcm_bytes:
        return True
    if _dsp is not None and _dsp.AVAILABLE:
        return bool(_dsp.LIB.dsp_is_silent_i16(pcm_bytes, len(pcm_bytes) // 2, int(threshold)))
    check = array.array("h")
    check.frombytes(pcm_bytes)
    return max(abs(v) for v in check) < threshold


def decode_pcm_to_mono_float(raw_bytes, sampwidth, nchannels):
    """Decode PCM WAV bytes (8/16/24/32-bit, mono or multi-channel) to mono floats in [-1.0, 1.0]."""
    if not raw_bytes:
        return []

    if _dsp is not None and _dsp.AVAILABLE and sampwidth in (1, 2, 3, 4) and nchannels > 0:
        frame_bytes = sampwidth * nchannels
        n_frames = len(raw_bytes) // frame_bytes
        if n_frames == 0:
            return []
        out = _dsp.alloc_f64(n_frames)
        _dsp.LIB.dsp_decode_pcm_to_mono_f64(
            raw_bytes, len(raw_bytes), sampwidth, nchannels, _dsp.f64_ptr(out)
        )
        return out

    if sampwidth == 2:
        pcm = array.array("h")
        pcm.frombytes(raw_bytes)
        scale = 1.0 / 32768.0
        if nchannels == 1:
            return [s * scale for s in pcm]
        inv_ch = scale / float(nchannels)
        n_frames = len(pcm) // nchannels
        if nchannels == 2:
            return [(pcm[i] + pcm[i + 1]) * inv_ch for i in range(0, n_frames * 2, 2)]
        out = [0.0] * n_frames
        for f in range(n_frames):
            base = f * nchannels
            total = 0
            for c in range(nchannels):
                total += pcm[base + c]
            out[f] = total * inv_ch
        return out

    if sampwidth == 1:
        # 8-bit WAV is unsigned [0..255]
        scale = 1.0 / 128.0
        if nchannels == 1:
            return [(b - 128) * scale for b in raw_bytes]
        inv_ch = scale / float(nchannels)
        n_frames = len(raw_bytes) // nchannels
        out = [0.0] * n_frames
        for f in range(n_frames):
            base = f * nchannels
            total = 0
            for c in range(nchannels):
                total += raw_bytes[base + c] - 128
            out[f] = total * inv_ch
        return out

    if sampwidth == 3:
        # 24-bit signed little-endian PCM
        scale = 1.0 / 8388608.0
        frame_bytes = 3 * nchannels
        n_frames = len(raw_bytes) // frame_bytes
        inv_ch = scale / float(nchannels)
        out = [0.0] * n_frames
        for f in range(n_frames):
            total = 0
            base = f * frame_bytes
            for c in range(nchannels):
                off = base + c * 3
                val = raw_bytes[off] | (raw_bytes[off + 1] << 8) | (raw_bytes[off + 2] << 16)
                if val & 0x800000:
                    val -= 0x1000000
                total += val
            out[f] = total * inv_ch
        return out

    if sampwidth == 4:
        pcm = array.array("i")
        pcm.frombytes(raw_bytes)
        scale = 1.0 / 2147483648.0
        if nchannels == 1:
            return [s * scale for s in pcm]
        inv_ch = scale / float(nchannels)
        n_frames = len(pcm) // nchannels
        out = [0.0] * n_frames
        for f in range(n_frames):
            base = f * nchannels
            total = 0
            for c in range(nchannels):
                total += pcm[base + c]
            out[f] = total * inv_ch
        return out

    raise ValueError(f"Unsupported WAV sample width: {sampwidth} bytes")


def resample_linear(samples, target_len):
    """Linearly resample a list of floats to target_len frames."""
    src_len = len(samples)
    if src_len == 0 or target_len <= 0:
        return []
    if src_len == target_len:
        return samples
    if src_len == 1:
        return [samples[0]] * target_len

    ratio = (src_len - 1) / float(max(1, target_len - 1))
    out = [0.0] * target_len
    last = src_len - 1
    for i in range(target_len):
        pos = i * ratio
        idx = int(pos)
        if idx >= last:
            out[i] = samples[last]
        else:
            frac = pos - idx
            out[i] = samples[idx] * (1.0 - frac) + samples[idx + 1] * frac
    return out


class StreamingWavReader:
    """Phase-accurate streaming WAV reader with continuous linear resampling."""

    def __init__(self, wf, dst_rate):
        self.wf = wf
        self.nch = wf.getnchannels()
        self.sw = wf.getsampwidth()
        self.src_rate = max(1, wf.getframerate())
        self.dst_rate = max(1, int(dst_rate))
        self.step = self.src_rate / float(self.dst_rate)
        self._use_c = _dsp is not None and _dsp.AVAILABLE
        self._buf = array.array("d") if self._use_c else []
        self._pos = 0.0
        self._eof = False

    def read(self, out_frames):
        """Read and resample up to out_frames mono floats at dst_rate with zero drift."""
        if out_frames <= 0:
            return []
        if self.src_rate == self.dst_rate:
            raw = self.wf.readframes(out_frames)
            return decode_pcm_to_mono_float(raw, self.sw, self.nch) if raw else []

        needed = int(math.floor(self._pos + (out_frames - 1) * self.step)) + 2
        while not self._eof and len(self._buf) < needed:
            to_read = max(256, needed - len(self._buf))
            raw = self.wf.readframes(to_read)
            if not raw:
                self._eof = True
                break
            self._buf.extend(decode_pcm_to_mono_float(raw, self.sw, self.nch))

        buf = self._buf
        avail = len(buf)
        if avail == 0:
            return []

        if self._use_c:
            out = _dsp.alloc_f64(out_frames)
            out_pos = ctypes.c_double(self._pos)
            produced = _dsp.LIB.dsp_resample_linear_f64(
                _dsp.f64_ptr(buf),
                avail,
                self._pos,
                self.step,
                out_frames,
                _dsp.f64_ptr(out),
                ctypes.byref(out_pos),
            )
            pos = out_pos.value
            if produced < out_frames:
                out = out[:produced]
        else:
            pos = self._pos
            step = self.step
            out = []
            append = out.append
            last = avail - 1

            for _ in range(out_frames):
                i0 = int(pos)
                if i0 >= avail:
                    break
                if i0 < last:
                    frac = pos - i0
                    append(buf[i0] * (1.0 - frac) + buf[i0 + 1] * frac)
                else:
                    append(buf[last])
                pos += step

        drop = int(pos)
        if drop > 0:
            if drop >= avail:
                self._buf = array.array("d") if self._use_c else []
                self._pos = 0.0 if self._eof else (pos - avail)
            else:
                del self._buf[:drop]
                self._pos = pos - drop
        else:
            self._pos = pos
        return out

    def seek(self, target_sec):
        """Seek to target_sec preserving fractional sample alignment."""
        exact_frame = max(0.0, float(target_sec) * self.src_rate)
        nframes = self.wf.getnframes()
        empty = array.array("d") if self._use_c else []
        if exact_frame >= nframes:
            self.wf.setpos(nframes)
            self._buf = empty
            self._pos = 0.0
            self._eof = True
        else:
            base_frame = int(math.floor(exact_frame))
            self.wf.setpos(base_frame)
            self._buf = empty
            self._pos = exact_frame - base_frame
            self._eof = False

    def close(self):
        self.wf.close()


_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def sanitize_filename_stem(raw_name):
    """Sanitize user-entered filename stem; returns empty string if nothing valid remains."""
    name = (raw_name or "").strip()
    if name.lower().endswith(".wav"):
        name = name[:-4].strip()
    name = _INVALID_FILENAME_RE.sub("_", name)
    name = name.strip(" .")
    if name in ("", ".", ".."):
        return ""
    return name


def unique_recording_paths(base_dir, stem, armed_indices):
    """Return non-colliding (filename, full_path) pairs for all armed channels in a take."""
    suffix = ""
    counter = 2
    while True:
        candidates = [
            (f"{stem}{suffix}_ch{idx + 1}.wav", os.path.join(base_dir, f"{stem}{suffix}_ch{idx + 1}.wav"))
            for idx in armed_indices
        ]
        if not any(os.path.exists(p) for _, p in candidates):
            return candidates
        suffix = f"_{counter}"
        counter += 1


def format_timecode(seconds):
    """Format seconds as MM:SS."""
    sec_int = max(0, int(seconds))
    mins, secs = divmod(sec_int, 60)
    return f"{mins:02d}:{secs:02d}"


def format_ruler_time(seconds, show_tenths=False):
    """Format ruler tick label as MM:SS or MM:SS.t when zoomed in."""
    sec = max(0.0, float(seconds))
    if not show_tenths:
        return format_timecode(sec)
    mins = int(sec // 60)
    rem = sec - mins * 60
    return f"{mins:02d}:{rem:04.1f}"


def format_peak_db(linear_peak):
    """Format a linear sample peak as dBFS (e.g. '-11.3 dB', '+0.1 dB', '-∞ dB')."""
    val = float(linear_peak)
    if val <= 1e-4:
        return "-∞ dB"
    db = 20.0 * math.log10(val)
    if db <= -60.0:
        return "-∞ dB"
    if db >= 0.0:
        return f"+{db:.1f} dB" if db >= 0.05 else "0.0 dB"
    return f"{db:.1f} dB"


def extract_waveform_peaks(filepath, num_bins=2400):
    """
    Fast stdlib WAV envelope extractor for pro DAW waveform display.
    Returns (bins_list, duration_sec) where each bin is (min_val, max_val, rms_val) in [-1.0, 1.0].
    """
    with wave.open(filepath, "rb") as wf:
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        fr = max(1, wf.getframerate())
        nframes = wf.getnframes()
        duration_sec = nframes / float(fr)
        if nframes <= 0:
            return [], 0.0

        bins = min(int(num_bins), nframes)
        frames_per_bin = nframes / float(bins)
        sample_cap = 256
        envelope = [(0.0, 0.0, 0.0)] * bins

        for b in range(bins):
            start_f = int(b * frames_per_bin)
            end_f = int((b + 1) * frames_per_bin)
            count = max(1, min(sample_cap, end_f - start_f))
            if frames_per_bin > sample_cap:
                wf.setpos(start_f)
            raw = wf.readframes(count)
            if not raw:
                break
            mono = decode_pcm_to_mono_float(raw, sw, nch)
            if mono:
                lo = 0.0
                hi = 0.0
                sum_sq = 0.0
                for s in mono:
                    if s < lo:
                        lo = s
                    if s > hi:
                        hi = s
                    sum_sq += s * s
                rms = math.sqrt(sum_sq / float(len(mono)))
                envelope[b] = (max(-1.0, lo), min(1.0, hi), min(1.0, rms))

        return envelope, duration_sec


def mix_stereo_chunk(
    channel_chunks,
    snapshots,
    sample_rate,
    offline_processors=None,
    out_levels=None,
    master_gain=1.0,
    out_master_levels=None,
    out_peaks=None,
    out_master_peaks=None,
    fx_executor=None,
):
    """
    Process per-channel mono float chunks through enabled effects, polarity inversion,
    mute/solo logic, volume faders, constant-power stereo panning, master gain, and sum
    to 16-bit stereo PCM bytes.

    - out_levels (optional list of 4 floats): per-channel post-fader VU-calibrated RMS.
    - out_peaks (optional list of 4 floats): per-channel post-fader true sample peak.
    - out_master_levels (optional list of 2 floats): post-master-fader [L, R] VU-calibrated RMS.
    - out_master_peaks (optional list of 2 floats): pre-clip post-master-fader [L, R] true sample peak.
    """
    if out_levels is not None:
        for i in range(len(out_levels)):
            out_levels[i] = 0.0
    if out_peaks is not None:
        for i in range(len(out_peaks)):
            out_peaks[i] = 0.0
    if out_master_levels is not None:
        for i in range(len(out_master_levels)):
            out_master_levels[i] = 0.0
    if out_master_peaks is not None:
        for i in range(len(out_master_peaks)):
            out_master_peaks[i] = 0.0

    max_len = 0
    for chunk in channel_chunks:
        if len(chunk) > max_len:
            max_len = len(chunk)

    if max_len == 0:
        return b""

    any_solo = any(snap.solo for snap in snapshots)
    use_c = _dsp is not None and _dsp.AVAILABLE
    master_left = _dsp.alloc_f64(max_len) if use_c else [0.0] * max_len
    master_right = _dsp.alloc_f64(max_len) if use_c else [0.0] * max_len
    if use_c:
        ml_ptr = _dsp.f64_ptr(master_left)
        mr_ptr = _dsp.f64_ptr(master_right)
    half_pi = math.pi * 0.5
    sqrt2 = 1.4142135623730951
    inv_len = 1.0 / float(max_len)

    def _process_channel_fx(i, snap, samples):
        if offline_processors is not None:
            comp_proc, chorus_proc, trem_proc, delay_proc = offline_processors[i]
        else:
            comp_proc = snap.compressor
            chorus_proc = snap.chorus
            trem_proc = snap.tremolo
            delay_proc = snap.delay

        # Signal chain: Compressor -> Chorus -> Tremolo -> Delay
        if snap.compressor_enabled:
            samples = comp_proc.process(samples, sample_rate)
        if snap.chorus_enabled:
            samples = chorus_proc.process(samples, sample_rate)
        if snap.tremolo_enabled:
            samples = trem_proc.process(samples, sample_rate)
        if snap.delay_enabled:
            samples = delay_proc.process(samples, sample_rate)
        return samples

    prepared = []
    for i, snap in enumerate(snapshots):
        chunk = channel_chunks[i]
        if not chunk:
            # If channel had a loaded file that already ended, only keep feeding zeros if Delay/Chorus is active
            if snap.filepath and (snap.delay_enabled or snap.chorus_enabled):
                samples = _dsp.alloc_f64(max_len) if use_c else [0.0] * max_len
            else:
                continue
        elif len(chunk) < max_len:
            pad_len = max_len - len(chunk)
            if use_c:
                samples = _dsp.as_f64_array(chunk) + _dsp.alloc_f64(pad_len)
            else:
                samples = list(chunk) + [0.0] * pad_len
        else:
            samples = chunk
        prepared.append((i, snap, samples))

    # When offline-bouncing with the GIL-releasing C accelerator, run independent channel FX in parallel
    has_parallel_fx = (
        fx_executor is not None
        and use_c
        and sum(
            1
            for _, s, _ in prepared
            if s.compressor_enabled or s.chorus_enabled or s.tremolo_enabled or s.delay_enabled
        )
        >= 2
    )
    if has_parallel_fx:
        futures = [
            (i, snap, fx_executor.submit(_process_channel_fx, i, snap, samples))
            for i, snap, samples in prepared
        ]
        processed = [(i, snap, fut.result()) for i, snap, fut in futures]
    else:
        processed = [
            (i, snap, _process_channel_fx(i, snap, samples))
            for i, snap, samples in prepared
        ]

    for i, snap, samples in processed:
        # Standard mixer Mute / Solo logic
        is_audible = (not snap.mute) and (not any_solo or snap.solo)
        if not is_audible or snap.volume <= 0.0:
            continue

        # Polarity inversion + channel fader gain
        gain = -snap.volume if snap.polarity_invert else snap.volume
        abs_gain = abs(gain)

        # Constant-power panning: 0.0 = Full Left, 0.5 = Center, 1.0 = Full Right
        angle = snap.pan * half_pi
        l_gain = gain * math.cos(angle)
        r_gain = gain * math.sin(angle)

        want_vu = out_levels is not None and i < len(out_levels)
        want_pk = out_peaks is not None and i < len(out_peaks)

        if use_c:
            s_arr = _dsp.as_f64_array(samples)
            if want_vu or want_pk:
                vu_c = ctypes.c_double(0.0)
                pk_c = ctypes.c_double(0.0)
                _dsp.LIB.dsp_mix_channel_accum(
                    _dsp.f64_ptr(s_arr),
                    max_len,
                    l_gain,
                    r_gain,
                    abs_gain,
                    ml_ptr,
                    mr_ptr,
                    1,
                    ctypes.byref(vu_c),
                    ctypes.byref(pk_c),
                )
                if want_vu:
                    out_levels[i] = vu_c.value
                if want_pk:
                    out_peaks[i] = pk_c.value
            else:
                _dsp.LIB.dsp_mix_channel_accum(
                    _dsp.f64_ptr(s_arr),
                    max_len,
                    l_gain,
                    r_gain,
                    abs_gain,
                    ml_ptr,
                    mr_ptr,
                    0,
                    None,
                    None,
                )
        elif want_vu or want_pk:
            sum_sq = 0.0
            ch_peak = 0.0
            for j in range(max_len):
                s = samples[j]
                sum_sq += s * s
                a = s if s >= 0.0 else -s
                if a > ch_peak:
                    ch_peak = a
                master_left[j] += s * l_gain
                master_right[j] += s * r_gain
            if want_vu:
                out_levels[i] = math.sqrt(sum_sq * inv_len) * sqrt2 * abs_gain
            if want_pk:
                out_peaks[i] = ch_peak * abs_gain
        else:
            for j in range(max_len):
                s = samples[j]
                master_left[j] += s * l_gain
                master_right[j] += s * r_gain

    # Apply master fader, measure stereo master bus VU + pre-clip peaks, clip, and pack 16-bit PCM
    m_gain = float(master_gain)
    want_m_vu = out_master_levels is not None and len(out_master_levels) >= 2
    want_m_pk = out_master_peaks is not None and len(out_master_peaks) >= 2

    if use_c:
        stereo = _dsp.alloc_i16(max_len * 2)
        if want_m_vu or want_m_pk:
            vl_c = ctypes.c_double(0.0)
            vr_c = ctypes.c_double(0.0)
            pl_c = ctypes.c_double(0.0)
            pr_c = ctypes.c_double(0.0)
            _dsp.LIB.dsp_master_pack_i16(
                ml_ptr,
                mr_ptr,
                max_len,
                m_gain,
                _dsp.i16_ptr(stereo),
                1,
                ctypes.byref(vl_c),
                ctypes.byref(vr_c),
                ctypes.byref(pl_c),
                ctypes.byref(pr_c),
            )
            if want_m_vu:
                out_master_levels[0] = vl_c.value
                out_master_levels[1] = vr_c.value
            if want_m_pk:
                out_master_peaks[0] = pl_c.value
                out_master_peaks[1] = pr_c.value
        else:
            _dsp.LIB.dsp_master_pack_i16(
                ml_ptr,
                mr_ptr,
                max_len,
                m_gain,
                _dsp.i16_ptr(stereo),
                0,
                None,
                None,
                None,
                None,
            )
        return stereo.tobytes()

    stereo = array.array("h", [0] * (max_len * 2))

    if want_m_vu or want_m_pk:
        l_sq = 0.0
        r_sq = 0.0
        l_pk = 0.0
        r_pk = 0.0
        for j in range(max_len):
            lv = master_left[j] * m_gain
            rv = master_right[j] * m_gain
            l_sq += lv * lv
            r_sq += rv * rv
            al = lv if lv >= 0.0 else -lv
            ar = rv if rv >= 0.0 else -rv
            if al > l_pk:
                l_pk = al
            if ar > r_pk:
                r_pk = ar
            if lv > 1.0:
                lv = 1.0
            elif lv < -1.0:
                lv = -1.0
            if rv > 1.0:
                rv = 1.0
            elif rv < -1.0:
                rv = -1.0
            stereo[j * 2] = int(lv * 32767.0)
            stereo[j * 2 + 1] = int(rv * 32767.0)
        if want_m_vu:
            out_master_levels[0] = math.sqrt(l_sq * inv_len) * sqrt2
            out_master_levels[1] = math.sqrt(r_sq * inv_len) * sqrt2
        if want_m_pk:
            out_master_peaks[0] = l_pk
            out_master_peaks[1] = r_pk
    else:
        for j in range(max_len):
            lv = master_left[j] * m_gain
            if lv > 1.0:
                lv = 1.0
            elif lv < -1.0:
                lv = -1.0
            rv = master_right[j] * m_gain
            if rv > 1.0:
                rv = 1.0
            elif rv < -1.0:
                rv = -1.0
            stereo[j * 2] = int(lv * 32767.0)
            stereo[j * 2 + 1] = int(rv * 32767.0)

    return stereo.tobytes()


class ChannelState:
    """Thread-safe state container for a single mixer channel."""

    def __init__(self, index):
        self.index = index
        self._lock = threading.Lock()
        self._filepath = None
        self._volume = 1.0  # 0.0 dB unity gain
        self._pan = 0.5
        self._mute = False
        self._solo = False
        self._polarity_invert = False
        self._rec_armed = False
        self._delay_enabled = False
        self._tremolo_enabled = False
        self._chorus_enabled = False
        self._compressor_enabled = False

        # Independent DSP processors per channel
        self.delay = DelayProcessor()
        self.tremolo = TremoloProcessor()
        self.chorus = ChorusProcessor()
        self.compressor = CompressorProcessor()

    def set_filepath(self, path):
        with self._lock:
            self._filepath = path

    def get_filepath(self):
        with self._lock:
            return self._filepath

    def set_volume(self, value):
        with self._lock:
            self._volume = max(0.0, min(4.0, float(value)))

    def set_volume_db(self, db):
        self.set_volume(db_to_linear(db))

    def set_pan(self, value):
        with self._lock:
            self._pan = max(0.0, min(1.0, float(value)))

    def set_mute(self, enabled):
        with self._lock:
            self._mute = bool(enabled)

    def set_solo(self, enabled):
        with self._lock:
            self._solo = bool(enabled)

    def set_polarity_invert(self, enabled):
        with self._lock:
            self._polarity_invert = bool(enabled)

    def set_rec_armed(self, enabled):
        with self._lock:
            self._rec_armed = bool(enabled)

    def set_effect_enabled(self, effect_name, enabled):
        with self._lock:
            setattr(self, f"_{effect_name}_enabled", bool(enabled))

    def get_processor(self, effect_name):
        return getattr(self, effect_name)

    def reset_effects(self):
        self.delay.reset()
        self.tremolo.reset()
        self.chorus.reset()
        self.compressor.reset()

    def clone_processors_for_offline(self):
        return (
            self.compressor.clone_for_offline(),
            self.chorus.clone_for_offline(),
            self.tremolo.clone_for_offline(),
            self.delay.clone_for_offline(),
        )

    def snapshot(self):
        with self._lock:
            return ChannelSnapshot(
                index=self.index,
                filepath=self._filepath,
                volume=self._volume,
                pan=self._pan,
                mute=self._mute,
                solo=self._solo,
                polarity_invert=self._polarity_invert,
                rec_armed=self._rec_armed,
                delay_enabled=self._delay_enabled,
                tremolo_enabled=self._tremolo_enabled,
                chorus_enabled=self._chorus_enabled,
                compressor_enabled=self._compressor_enabled,
                delay=self.delay,
                tremolo=self.tremolo,
                chorus=self.chorus,
                compressor=self.compressor,
            )


class ChannelMeter:
    """Segmented vertical LED dBFS level meter with peak-hold indicator."""

    def __init__(self, parent, width=16, height=154, segments=22):
        self.width = width
        self.height = height
        self.segments = segments

        self.canvas = tk.Canvas(
            parent,
            width=width,
            height=height,
            bg="#101c28",
            bd=0,
            highlightthickness=1,
            highlightbackground="#36536e",
        )

        self._seg_ids = []
        self._lit_colors = []
        self._dim_colors = []
        self._seg_lit = [False] * segments

        pad_x = 2
        pad_y = 3
        usable_h = height - 2 * pad_y
        step = usable_h / float(segments)
        gap = 1.5

        for idx in range(segments):
            # idx 0 is bottom (-60 dB), idx segments-1 is top (0 dB / Clip)
            y_bottom = height - pad_y - idx * step
            y_top = y_bottom - step + gap
            frac = (idx + 1) / float(segments)
            if frac >= 0.88:
                lit_c, dim_c = "#f24957", "#361920"
            elif frac >= 0.70:
                lit_c, dim_c = "#f5c542", "#332d18"
            else:
                lit_c, dim_c = "#2ecc71", "#163026"

            rect_id = self.canvas.create_rectangle(
                pad_x, y_top, width - pad_x, y_bottom, fill=dim_c, outline=""
            )
            self._seg_ids.append(rect_id)
            self._lit_colors.append(lit_c)
            self._dim_colors.append(dim_c)

        self._peak_seg = -1
        self._displayed_level = 0.0
        self._peak_hold = 0.0
        self._hold_ticks = 0

    @staticmethod
    def linear_to_norm(linear_peak):
        """Convert linear amplitude peak to normalized [0.0, 1.0] on a -60..0 dBFS scale."""
        if linear_peak <= 1e-4:
            return 0.0
        db = 20.0 * math.log10(linear_peak)
        if db <= -60.0:
            return 0.0
        if db >= 0.0:
            return 1.0
        return (db + 60.0) / 60.0

    def update_level(self, linear_level):
        """Update LED segments with smooth ~300 ms VU-style ballistics and peak hold."""
        target = self.linear_to_norm(linear_level)

        # Smooth VU ballistics (~300 ms rise & fall at 30 FPS for easy readability)
        if target >= self._displayed_level:
            self._displayed_level += 0.24 * (target - self._displayed_level)
        else:
            self._displayed_level += 0.18 * (target - self._displayed_level)

        if self._displayed_level < 0.008 and target == 0.0:
            self._displayed_level = 0.0

        # Subtle slow peak-hold indicator (~0.8s hold at 30 FPS)
        if target >= self._peak_hold:
            self._peak_hold = target
            self._hold_ticks = 24
        elif self._hold_ticks > 0:
            self._hold_ticks -= 1
        else:
            self._peak_hold *= 0.92
            if self._peak_hold < 0.01:
                self._peak_hold = 0.0

        lit_count = int(round(self._displayed_level * self.segments))
        if lit_count > self.segments:
            lit_count = self.segments

        new_peak_seg = (
            min(self.segments - 1, int(round(self._peak_hold * self.segments)) - 1)
            if self._peak_hold > 0.02
            else -1
        )

        # Redraw only segments whose visual state changed
        for i in range(self.segments):
            should_be_lit = i < lit_count
            is_peak_marker = i == new_peak_seg
            was_peak_marker = i == self._peak_seg

            if should_be_lit != self._seg_lit[i] or is_peak_marker != was_peak_marker:
                if is_peak_marker and not should_be_lit:
                    # Peak-hold floating LED
                    color = "#ff7a85" if i >= int(self.segments * 0.88) else "#eef7ff"
                elif should_be_lit:
                    color = self._lit_colors[i]
                else:
                    color = self._dim_colors[i]
                self.canvas.itemconfig(self._seg_ids[i], fill=color)
                self._seg_lit[i] = should_be_lit

        self._peak_seg = new_peak_seg

    def reset(self):
        self._displayed_level = 0.0
        self._peak_hold = 0.0
        self._hold_ticks = 0
        self.update_level(0.0)


class PeakReadout:
    """Numeric dBFS peak indicator with ~2.5s hold/auto-clear and red digital clip alert."""

    HOLD_SECONDS = 2.5

    def __init__(self, parent, width=8):
        self._held_peak = 0.0
        self._hold_until = 0.0
        self._last_text = "-∞ dB"
        self._last_state = "idle"

        self.var = tk.StringVar(value="-∞ dB")
        self.label = tk.Label(
            parent,
            textvariable=self.var,
            bg="#12202d",
            fg="#6886a3",
            font=("Helvetica", 8, "bold"),
            width=width,
            pady=2,
            cursor="hand2",
            highlightthickness=1,
            highlightbackground="#2c465e",
        )
        # Clicking clears peak / clip alert immediately
        self.label.bind("<Button-1>", lambda _e: self.clear())

    def update(self, linear_peak, now=None):
        """Feed latest true sample peak; holds highest peak for 2.5s then auto-clears."""
        if now is None:
            now = time.monotonic()

        pk = float(linear_peak)
        if pk >= self._held_peak and pk > 1e-4:
            self._held_peak = pk
            self._hold_until = now + self.HOLD_SECONDS
        elif now >= self._hold_until:
            # Hold window expired -> clear back to current live peak (or -∞ if silent)
            if pk > 1e-4:
                self._held_peak = pk
                self._hold_until = now + self.HOLD_SECONDS
            else:
                self._held_peak = 0.0

        self._render()

    def clear(self):
        self._held_peak = 0.0
        self._hold_until = 0.0
        self._render()

    def _render(self):
        pk = self._held_peak
        txt = format_peak_db(pk)
        if pk <= 1e-4 or txt == "-∞ dB":
            state = "idle"
            bg, fg, bd = "#12202d", "#6886a3", "#2c465e"
        elif pk >= 0.999:
            state = "clip"
            bg, fg, bd = "#b82530", "#ffffff", "#ff858f"
        else:
            db = 20.0 * math.log10(pk)
            if db >= -3.0:
                state = "hot"
                bg, fg, bd = "#12202d", "#ffd166", "#8a6f28"
            else:
                state = "normal"
                bg, fg, bd = "#12202d", "#9be7c4", "#345470"

        if txt != self._last_text:
            self.var.set(txt)
            self._last_text = txt
        if state != self._last_state:
            self.label.config(bg=bg, fg=fg, highlightbackground=bd)
            self._last_state = state


class WaveformTimeline:
    """
    Pro Tools / Logic Pro-style 4-track waveform arrange view with dual-layer filled
    Peak + RMS polygons, clip title bars, muted region dimming, time ruler, horizontal
    zoom & pan scrollbar, smooth moving playhead, live recording waveform, and scrubbing.
    """

    # (accent_stripe, clip_bg, header_bg, peak_fill, rms_fill)
    TRACK_THEMES = [
        ("#4da3ff", "#152a3f", "#1d3b59", "#3478bd", "#6eb8ff"),  # Ch 1: Sky Blue
        ("#48d18a", "#142f26", "#1b4436", "#2c8f5e", "#6ee3a5"),  # Ch 2: Emerald
        ("#f5c542", "#2f2916", "#453b1c", "#b58d26", "#ffe07d"),  # Ch 3: Amber Gold
        ("#a375ff", "#251c3a", "#352654", "#7049bf", "#bfa0ff"),  # Ch 4: Violet
    ]
    DIM_THEME = ("#586e82", "#17222d", "#1e2c3a", "#364b5e", "#4e677d")
    HEADER_W = 56
    RULER_H = 18
    LANE_H = 40
    CLIP_HDR_H = 11
    PAD_RIGHT = 8
    SCROLL_H = 10
    MIN_ZOOM = 1.0
    MAX_ZOOM = 32.0

    def __init__(self, parent, on_seek, bg="#0e1822"):
        self.on_seek = on_seek
        self._bg = bg
        self._duration = 0.0
        self._view_duration = 0.0
        self._position = 0.0
        self._dragging = False

        # Horizontal zoom & viewport pan state
        self._zoom = 1.0
        self._view_start = 0.0
        self._scroll_dragging = False
        self._scroll_drag_offset = 0.0

        # Per-channel static loaded waveforms: (envelope_list, track_duration_sec, filename)
        self._tracks = [None, None, None, None]
        self._audible = [True, True, True, True]

        # Live recording overlay state
        self._rec_armed = []
        self._rec_peaks = []
        self._rec_elapsed = 0.0
        self._rec_active = False

        total_canvas_h = self.RULER_H + self.LANE_H * 4

        self.frame = tk.Frame(
            parent,
            bg="#182836",
            highlightthickness=1,
            highlightbackground="#3c5c78",
        )

        # Compact header bar with zoom controls and timecode display
        top_bar = tk.Frame(self.frame, bg="#1b2d3e", height=24, padx=10)
        top_bar.pack(fill=tk.X)
        top_bar.pack_propagate(False)

        tk.Label(
            top_bar,
            text="ARRANGE  ·  WAVEFORMS  (DRAG TO SCRUB · SCROLL / ± TO ZOOM)",
            bg="#1b2d3e",
            fg="#8eb0cf",
            font=("Helvetica", 8, "bold"),
        ).pack(side=tk.LEFT)

        self.dur_var = tk.StringVar(value="00:00")
        tk.Label(
            top_bar,
            textvariable=self.dur_var,
            bg="#1b2d3e",
            fg="#9cb8d1",
            font=("Helvetica", 9, "bold"),
        ).pack(side=tk.RIGHT)

        tk.Label(
            top_bar,
            text=" / ",
            bg="#1b2d3e",
            fg="#5c7b99",
            font=("Helvetica", 9, "bold"),
        ).pack(side=tk.RIGHT)

        self.pos_var = tk.StringVar(value="00:00")
        tk.Label(
            top_bar,
            textvariable=self.pos_var,
            bg="#1b2d3e",
            fg="#7ef2b8",
            font=("Helvetica", 9, "bold"),
        ).pack(side=tk.RIGHT)

        # Compact studio zoom controls: [ - ] [ 1.0x ] [ + ] [ Fit ]
        zoom_bar = tk.Frame(top_bar, bg="#1b2d3e")
        zoom_bar.pack(side=tk.RIGHT, padx=(0, 14))

        self._make_zoom_btn(zoom_bar, "Fit", self.reset_zoom, padx=6).pack(
            side=tk.RIGHT, padx=(3, 0)
        )
        self._make_zoom_btn(zoom_bar, "+", self.zoom_in, padx=6).pack(
            side=tk.RIGHT, padx=(2, 0)
        )

        self.zoom_var = tk.StringVar(value="1.0×")
        zoom_lbl = tk.Label(
            zoom_bar,
            textvariable=self.zoom_var,
            bg="#12202d",
            fg="#9bd2ff",
            font=("Helvetica", 8, "bold"),
            width=5,
            cursor="hand2",
            highlightthickness=1,
            highlightbackground="#33506b",
        )
        zoom_lbl.pack(side=tk.RIGHT, padx=(2, 0))
        zoom_lbl.bind("<Button-1>", lambda _e: self.reset_zoom())

        self._make_zoom_btn(zoom_bar, "−", self.zoom_out, padx=6).pack(side=tk.RIGHT)

        tk.Label(
            zoom_bar,
            text="ZOOM",
            bg="#1b2d3e",
            fg="#6e8fad",
            font=("Helvetica", 7, "bold"),
        ).pack(side=tk.RIGHT, padx=(0, 5))

        # Main 4-track waveform canvas
        self.canvas = tk.Canvas(
            self.frame,
            height=total_canvas_h,
            bg=bg,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        self.canvas.pack(fill=tk.X, expand=True)

        # Slim horizontal overview / pan scrollbar below lanes
        self.scroll_canvas = tk.Canvas(
            self.frame,
            height=self.SCROLL_H,
            bg="#12202d",
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        self.scroll_canvas.pack(fill=tk.X)
        self._scroll_thumb = self.scroll_canvas.create_rectangle(
            self.HEADER_W,
            2,
            1080,
            self.SCROLL_H - 2,
            fill="#22364a",
            outline="",
        )

        # Persistent playhead items (only these 2 move at 30 FPS!)
        self._playhead_line = self.canvas.create_line(
            self.HEADER_W,
            self.RULER_H - 2,
            self.HEADER_W,
            total_canvas_h,
            fill="#eef7ff",
            width=2,
            tags=("playhead",),
        )
        self._playhead_cap = self.canvas.create_polygon(
            self.HEADER_W - 5,
            3,
            self.HEADER_W + 5,
            3,
            self.HEADER_W,
            self.RULER_H - 1,
            fill="#48d18a",
            outline="#eef7ff",
            tags=("playhead",),
        )

        self.canvas.bind("<Configure>", lambda _e: self.redraw_waveforms())
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_shift_wheel)
        self.canvas.bind("<Button-4>", self._on_wheel_up)
        self.canvas.bind("<Button-5>", self._on_wheel_down)

        self.scroll_canvas.bind("<Configure>", lambda _e: self._draw_scrollbar())
        self.scroll_canvas.bind("<ButtonPress-1>", self._on_scroll_press)
        self.scroll_canvas.bind("<B1-Motion>", self._on_scroll_drag)
        self.scroll_canvas.bind("<ButtonRelease-1>", self._on_scroll_release)

        self.redraw_waveforms()

    @staticmethod
    def _make_zoom_btn(parent, text, command, padx=5):
        base_bg = "#23384c"
        hover_bg = "#304f6b"
        active_bg = "#162636"
        lbl = tk.Label(
            parent,
            text=text,
            bg=base_bg,
            fg="#dcecf9",
            font=("Helvetica", 8, "bold"),
            padx=padx,
            pady=0,
            cursor="hand2",
            highlightthickness=1,
            highlightbackground="#3c5c78",
        )
        lbl.bind("<Enter>", lambda _e: lbl.config(bg=hover_bg, fg="#ffffff"))
        lbl.bind("<Leave>", lambda _e: lbl.config(bg=base_bg, fg="#dcecf9"))
        lbl.bind("<ButtonPress-1>", lambda _e: lbl.config(bg=active_bg))

        def on_release(event):
            try:
                x = event.x_root - lbl.winfo_rootx()
                y = event.y_root - lbl.winfo_rooty()
                inside = 0 <= x <= lbl.winfo_width() and 0 <= y <= lbl.winfo_height()
            except Exception:
                inside = True
            lbl.config(bg=hover_bg if inside else base_bg, fg="#ffffff" if inside else "#dcecf9")
            if inside:
                command()

        lbl.bind("<ButtonRelease-1>", on_release)
        return lbl

    def _lane_span(self):
        w = self.canvas.winfo_width()
        if w <= self.HEADER_W + 40:
            w = 1090
        return w, max(1.0, float(w - self.HEADER_W - self.PAD_RIGHT))

    def _effective_duration(self):
        if self._rec_active:
            return max(self._duration, self._rec_elapsed, 8.0)
        return self._duration

    def _visible_window(self):
        eff_dur = self._effective_duration()
        if eff_dur <= 0.0:
            self._view_start = 0.0
            return 0.0, 0.0, 0.0, 0.0
        vis_dur = eff_dur / self._zoom
        max_start = max(0.0, eff_dur - vis_dur)
        if self._view_start < 0.0:
            self._view_start = 0.0
        elif self._view_start > max_start:
            self._view_start = max_start
        return self._view_start, self._view_start + vis_dur, vis_dur, eff_dur

    def _sec_to_x(self, sec):
        _, span = self._lane_span()
        v_start, _, vis_dur, _ = self._visible_window()
        if vis_dur <= 0.0:
            return float(self.HEADER_W)
        return self.HEADER_W + ((float(sec) - v_start) / vis_dur) * span

    def _x_to_sec(self, x):
        _, span = self._lane_span()
        v_start, _, vis_dur, eff_dur = self._visible_window()
        if eff_dur <= 0.0 or vis_dur <= 0.0:
            return 0.0
        frac = max(0.0, min(1.0, (x - self.HEADER_W) / span))
        return max(0.0, min(eff_dur, v_start + frac * vis_dur))

    # ---------- Zoom & Pan API ----------

    def get_zoom(self):
        return self._zoom

    def set_zoom(self, new_zoom, anchor_sec=None, anchor_frac=None):
        """Set horizontal zoom factor [1.0..32.0], keeping anchor_sec pinned at anchor_frac."""
        clamped = max(self.MIN_ZOOM, min(self.MAX_ZOOM, float(new_zoom)))
        if abs(clamped - 1.0) < 0.02:
            clamped = 1.0

        v_start, _, vis_dur, eff_dur = self._visible_window()
        if eff_dur > 0.0:
            if anchor_sec is None:
                anchor_sec = self._position if (v_start <= self._position <= v_start + vis_dur) else (v_start + vis_dur * 0.5)
            if anchor_frac is None:
                anchor_frac = ((anchor_sec - v_start) / vis_dur) if vis_dur > 0.0 else 0.5
            anchor_frac = max(0.0, min(1.0, float(anchor_frac)))
            new_vis = eff_dur / clamped
            new_start = anchor_sec - anchor_frac * new_vis
            self._zoom = clamped
            self._view_start = max(0.0, min(max(0.0, eff_dur - new_vis), new_start))
        else:
            self._zoom = clamped
            self._view_start = 0.0

        self.zoom_var.set(f"{self._zoom:.1f}×" if self._zoom < 10.0 else f"{int(round(self._zoom))}×")
        self.redraw_waveforms()

    def zoom_in(self, factor=1.5):
        self.set_zoom(self._zoom * factor)

    def zoom_out(self, factor=1.5):
        self.set_zoom(self._zoom / factor)

    def reset_zoom(self):
        self._zoom = 1.0
        self._view_start = 0.0
        self.zoom_var.set("1.0×")
        self.redraw_waveforms()

    def zoom_at_x(self, factor, x):
        _, span = self._lane_span()
        frac = max(0.0, min(1.0, (x - self.HEADER_W) / span))
        anchor_sec = self._x_to_sec(x)
        self.set_zoom(self._zoom * factor, anchor_sec=anchor_sec, anchor_frac=frac)

    def pan_by_fraction(self, view_frac):
        """Pan viewport horizontally by a fraction of the current visible duration."""
        if self._zoom <= 1.0:
            return
        v_start, _, vis_dur, eff_dur = self._visible_window()
        if eff_dur <= 0.0:
            return
        max_start = max(0.0, eff_dur - vis_dur)
        new_start = max(0.0, min(max_start, v_start + float(view_frac) * vis_dur))
        if abs(new_start - self._view_start) > 1e-6:
            self._view_start = new_start
            self.redraw_waveforms()

    def _on_mouse_wheel(self, event):
        if self._effective_duration() <= 0.0:
            return
        delta = getattr(event, "delta", 0)
        if delta == 0:
            return
        step = 1 if delta > 0 else -1
        if event.state & 0x0001:  # Shift held -> horizontal pan
            self.pan_by_fraction(-step * 0.14)
        else:
            self.zoom_at_x(1.25 if step > 0 else (1.0 / 1.25), event.x)

    def _on_shift_wheel(self, event):
        if self._effective_duration() <= 0.0:
            return
        delta = getattr(event, "delta", 0)
        if delta != 0:
            step = 1 if delta > 0 else -1
            self.pan_by_fraction(-step * 0.14)

    def _on_wheel_up(self, event):
        if self._effective_duration() <= 0.0:
            return
        if event.state & 0x0001:
            self.pan_by_fraction(-0.14)
        else:
            self.zoom_at_x(1.25, event.x)

    def _on_wheel_down(self, event):
        if self._effective_duration() <= 0.0:
            return
        if event.state & 0x0001:
            self.pan_by_fraction(0.14)
        else:
            self.zoom_at_x(1.0 / 1.25, event.x)

    # ---------- Horizontal Overview Scrollbar ----------

    def _thumb_bounds(self):
        _, span = self._lane_span()
        v_start, _, vis_dur, eff_dur = self._visible_window()
        if eff_dur <= 0.0 or self._zoom <= 1.0:
            return float(self.HEADER_W), float(self.HEADER_W) + span
        thumb_w = max(24.0, (vis_dur / eff_dur) * span)
        max_start = max(1e-9, eff_dur - vis_dur)
        avail_travel = max(0.0, span - thumb_w)
        x0 = self.HEADER_W + (v_start / max_start) * avail_travel
        return x0, x0 + thumb_w

    def _draw_scrollbar(self):
        try:
            x0, x1 = self._thumb_bounds()
            fill = "#5489bd" if self._scroll_dragging else ("#3d6891" if self._zoom > 1.0 else "#22364a")
            self.scroll_canvas.coords(self._scroll_thumb, x0, 2, x1, self.SCROLL_H - 2)
            self.scroll_canvas.itemconfig(self._scroll_thumb, fill=fill)
        except Exception:
            pass

    def _on_scroll_press(self, event):
        if self._zoom <= 1.0 or self._effective_duration() <= 0.0:
            return
        x0, x1 = self._thumb_bounds()
        self._scroll_dragging = True
        if x0 <= event.x <= x1:
            self._scroll_drag_offset = event.x - x0
        else:
            self._scroll_drag_offset = (x1 - x0) * 0.5
            self._apply_scroll_x(event.x)
        self._draw_scrollbar()

    def _on_scroll_drag(self, event):
        if not self._scroll_dragging:
            return
        self._apply_scroll_x(event.x)

    def _on_scroll_release(self, _event):
        if self._scroll_dragging:
            self._scroll_dragging = False
            self._draw_scrollbar()

    def _apply_scroll_x(self, x):
        _, span = self._lane_span()
        _, _, vis_dur, eff_dur = self._visible_window()
        if eff_dur <= 0.0 or self._zoom <= 1.0:
            return
        x0, x1 = self._thumb_bounds()
        thumb_w = x1 - x0
        avail_travel = max(1.0, span - thumb_w)
        desired_x0 = max(float(self.HEADER_W), min(self.HEADER_W + avail_travel, x - self._scroll_drag_offset))
        frac = (desired_x0 - self.HEADER_W) / avail_travel
        self._view_start = frac * max(0.0, eff_dur - vis_dur)
        self.redraw_waveforms()

    def _on_press(self, event):
        if self._rec_active or self._duration <= 0.0 or event.x < self.HEADER_W:
            return
        self._dragging = True
        self._position = self._x_to_sec(event.x)
        self._move_playhead_only()

    def _on_drag(self, event):
        if not self._dragging or self._duration <= 0.0:
            return
        self._position = self._x_to_sec(event.x)
        self._move_playhead_only()

    def _on_release(self, event):
        if not self._dragging:
            return
        self._dragging = False
        if self._duration > 0.0:
            self._position = self._x_to_sec(event.x)
            self._move_playhead_only()
            self.on_seek(self._position)

    def set_track_waveform(self, channel_idx, peaks, duration_sec, label=""):
        if 0 <= channel_idx < 4:
            self._tracks[channel_idx] = (list(peaks), float(duration_sec), label)
            self.redraw_waveforms()

    def clear_track(self, channel_idx):
        if 0 <= channel_idx < 4:
            self._tracks[channel_idx] = None
            self.redraw_waveforms()

    def set_audible_flags(self, audible_flags):
        """Dim muted or non-soloed tracks in the Arrange view."""
        new_flags = [bool(f) for f in audible_flags[:4]]
        if new_flags != self._audible:
            self._audible = new_flags
            self.redraw_waveforms()

    def set_duration(self, duration_sec):
        self._duration = max(0.0, float(duration_sec))
        if self._duration <= 0.0:
            self._zoom = 1.0
            self._view_start = 0.0
            self.zoom_var.set("1.0×")
        if self._position > self._duration and not self._rec_active:
            self._position = self._duration
        self.dur_var.set(format_timecode(self._duration))
        self.redraw_waveforms()

    def set_position(self, pos_sec, follow=False):
        if self._dragging:
            return
        eff = self._effective_duration()
        self._position = max(0.0, min(eff, float(pos_sec))) if eff > 0.0 else 0.0
        if follow and self._zoom > 1.0 and eff > 0.0:
            v_start, v_end, vis_dur, _ = self._visible_window()
            if self._position < v_start or self._position > v_end - 0.05 * vis_dur:
                max_start = max(0.0, eff - vis_dur)
                self._view_start = max(0.0, min(max_start, self._position - 0.15 * vis_dur))
                self.redraw_waveforms()
                return
        self._move_playhead_only()

    def update_live_recording(self, active, armed_indices=(), peaks=(), elapsed_sec=0.0):
        """Draw live growing crimson waveform on armed channel lane(s) during recording."""
        self._rec_active = bool(active)
        self._rec_armed = list(armed_indices) if active else []
        self._rec_peaks = list(peaks) if active else []
        self._rec_elapsed = float(elapsed_sec) if active else 0.0

        if active:
            self._position = self._rec_elapsed
            v_start, v_end, vis_dur, eff_dur = self._visible_window()
            self.dur_var.set(format_timecode(eff_dur))
            need_redraw = abs(eff_dur - self._view_duration) > 0.5
            if self._zoom > 1.0 and (self._position < v_start or self._position > v_end - 0.05 * vis_dur):
                max_start = max(0.0, eff_dur - vis_dur)
                self._view_start = max(0.0, min(max_start, self._position - 0.80 * vis_dur))
                need_redraw = True
            if need_redraw:
                self.redraw_waveforms()
            else:
                self._draw_live_rec_overlay()
                self._move_playhead_only()
        else:
            self.dur_var.set(format_timecode(self._duration))
            self.redraw_waveforms()

    def redraw_waveforms(self):
        """Redraw ruler, 4 track lanes, and loaded waveforms for current zoom/pan viewport."""
        try:
            self.canvas.delete("static")
            self.canvas.delete("rec_live")

            w, span = self._lane_span()
            total_h = self.RULER_H + self.LANE_H * 4
            v_start, v_end, vis_dur, eff_dur = self._visible_window()
            self._view_duration = eff_dur

            # Time ruler background
            self.canvas.create_rectangle(
                0, 0, w, self.RULER_H, fill="#162534", outline="", tags=("static",)
            )
            self.canvas.create_line(
                0, self.RULER_H - 1, w, self.RULER_H - 1, fill="#2e4963", tags=("static",)
            )

            # Vertical time grid ticks in ruler + subtle guides across lanes
            num_ticks = 8
            if eff_dur > 0.0 and vis_dur > 0.0:
                show_tenths = vis_dur < 16.0
                for t_idx in range(num_ticks + 1):
                    frac = t_idx / float(num_ticks)
                    gx = self.HEADER_W + frac * span
                    tick_sec = v_start + frac * vis_dur
                    self.canvas.create_line(
                        gx, self.RULER_H, gx, total_h, fill="#162635", tags=("static",)
                    )
                    if t_idx < num_ticks:
                        self.canvas.create_text(
                            gx + 3,
                            self.RULER_H // 2,
                            text=format_ruler_time(tick_sec, show_tenths=show_tenths),
                            fill="#6e8fad",
                            font=("Helvetica", 7),
                            anchor="w",
                            tags=("static",),
                        )

            # Draw 4 horizontal track lanes
            for ch in range(4):
                y0 = self.RULER_H + ch * self.LANE_H
                y1 = y0 + self.LANE_H
                mid_y = (y0 + y1) * 0.5
                is_aud = self._audible[ch]
                stripe_col, clip_bg, hdr_bg, peak_col, rms_col = (
                    self.TRACK_THEMES[ch] if is_aud else self.DIM_THEME
                )
                base_stripe = self.TRACK_THEMES[ch][0]

                # Lane alternating dark background
                lane_bg = "#0f1a25" if ch % 2 == 0 else "#121f2d"
                self.canvas.create_rectangle(
                    self.HEADER_W, y0, w, y1, fill=lane_bg, outline="", tags=("static",)
                )
                # Track header box on the left
                self.canvas.create_rectangle(
                    0, y0, self.HEADER_W, y1, fill="#192a3b", outline="#263d54", tags=("static",)
                )
                self.canvas.create_rectangle(
                    0, y0 + 1, 4, y1 - 1, fill=base_stripe if is_aud else "#4a5e70", outline="", tags=("static",)
                )
                self.canvas.create_text(
                    30,
                    mid_y,
                    text=f"CH {ch + 1}",
                    fill="#d4e5f5" if is_aud else "#6f869c",
                    font=("Helvetica", 8, "bold"),
                    tags=("static",),
                )
                # Subtle empty-lane center line & bottom divider
                self.canvas.create_line(
                    self.HEADER_W, mid_y, w - self.PAD_RIGHT, mid_y, fill="#172737", tags=("static",)
                )
                self.canvas.create_line(
                    0, y1 - 1, w, y1 - 1, fill="#22364a", tags=("static",)
                )

                # Draw Pro DAW audio clip region clipped to visible viewport [v_start, v_end]
                track = self._tracks[ch]
                if track is not None and eff_dur > 0.0 and vis_dur > 0.0:
                    envelope, track_dur, clip_label = track
                    if envelope and track_dur > 0.0:
                        clip_t0 = max(0.0, v_start)
                        clip_t1 = min(track_dur, v_end)
                        if clip_t1 <= clip_t0:
                            continue

                        x_start = self.HEADER_W + ((clip_t0 - v_start) / vis_dur) * span
                        x_end = self.HEADER_W + ((clip_t1 - v_start) / vis_dur) * span
                        clip_w = max(2.0, x_end - x_start)
                        x_end = x_start + clip_w

                        cy0 = y0 + 2
                        cy1 = y1 - 2
                        wf_y0 = cy0 + self.CLIP_HDR_H
                        wf_mid = (wf_y0 + cy1) * 0.5
                        max_amp = max(2.0, (cy1 - wf_y0) * 0.5 - 1.5)

                        # Clip body background
                        self.canvas.create_rectangle(
                            x_start, cy0, x_end, cy1,
                            fill=clip_bg, outline=stripe_col, width=1, tags=("static",),
                        )
                        # Top clip title bar (Logic / Pro Tools region header)
                        self.canvas.create_rectangle(
                            x_start + 1, cy0 + 1, x_end - 1, wf_y0,
                            fill=hdr_bg, outline="", tags=("static",),
                        )
                        if clip_label and clip_w > 34:
                            max_chars = max(4, int((clip_w - 10) // 6))
                            disp = clip_label if len(clip_label) <= max_chars else clip_label[: max_chars - 2] + ".."
                            self.canvas.create_text(
                                x_start + 5,
                                (cy0 + wf_y0) * 0.5,
                                text=disp,
                                fill="#e6f2fc" if is_aud else "#8aa0b5",
                                font=("Helvetica", 7, "bold"),
                                anchor="w",
                                tags=("static",),
                            )

                        # Center zero-crossing reference line inside waveform body
                        self.canvas.create_line(
                            x_start + 1, wf_mid, x_end - 1, wf_mid, fill="#243c54", tags=("static",)
                        )

                        # Build continuous filled Peak & RMS polygons (~1.5px per vertex)
                        n_bins = len(envelope)
                        b_start_f = (clip_t0 / track_dur) * (n_bins - 1)
                        b_end_f = (clip_t1 / track_dur) * (n_bins - 1)
                        vis_bins = max(1.0, b_end_f - b_start_f)
                        n_pts = max(2, min(int(clip_w / 1.5), max(2, int(math.ceil(vis_bins)) + 1)))
                        inner_w = max(1.0, clip_w - 2.0)

                        pk_top = []
                        pk_bot = []
                        rms_top = []
                        rms_bot = []

                        for p in range(n_pts):
                            frac_p = p / float(max(1, n_pts - 1))
                            b_pos = b_start_f + frac_p * (b_end_f - b_start_f)
                            if vis_bins > n_pts:
                                # Aggregate min/max across covered bins so transients never vanish
                                next_frac = (p + 1) / float(max(1, n_pts - 1))
                                bi0 = max(0, min(n_bins - 1, int(b_pos)))
                                bi1 = max(bi0 + 1, min(n_bins, int(b_start_f + next_frac * (b_end_f - b_start_f)) + 1))
                                lo_v = 0.0
                                hi_v = 0.0
                                rms_v = 0.0
                                for bi in range(bi0, bi1):
                                    it = envelope[bi]
                                    if isinstance(it, tuple):
                                        if it[0] < lo_v:
                                            lo_v = it[0]
                                        if it[1] > hi_v:
                                            hi_v = it[1]
                                        if it[2] > rms_v:
                                            rms_v = it[2]
                                    else:
                                        v = float(it)
                                        if -v < lo_v:
                                            lo_v = -v
                                        if v > hi_v:
                                            hi_v = v
                                        if v * 0.65 > rms_v:
                                            rms_v = v * 0.65
                            else:
                                bi = max(0, min(n_bins - 1, int(round(b_pos))))
                                item = envelope[bi]
                                if isinstance(item, tuple):
                                    lo_v, hi_v, rms_v = item
                                else:
                                    v = float(item)
                                    lo_v, hi_v, rms_v = -v, v, v * 0.65

                            px = x_start + 1.0 + frac_p * inner_w
                            y_hi = wf_mid - max(0.0, hi_v) * max_amp
                            y_lo = wf_mid - min(0.0, lo_v) * max_amp
                            if y_lo - y_hi < 1.2:
                                y_hi = wf_mid - 0.6
                                y_lo = wf_mid + 0.6
                            pk_top.extend((px, y_hi))
                            pk_bot.append((px, y_lo))

                            r_amp = max(0.5, rms_v * max_amp)
                            rms_top.extend((px, wf_mid - r_amp))
                            rms_bot.append((px, wf_mid + r_amp))

                        for px, py in reversed(pk_bot):
                            pk_top.extend((px, py))
                        for px, py in reversed(rms_bot):
                            rms_top.extend((px, py))

                        # Layer 1: Outer true min/max peak silhouette
                        self.canvas.create_polygon(
                            *pk_top, fill=peak_col, outline=peak_col, tags=("static",)
                        )
                        # Layer 2: Brighter inner RMS energy core
                        self.canvas.create_polygon(
                            *rms_top, fill=rms_col, outline="", tags=("static",)
                        )
                        # Crisp zero-line overlay
                        self.canvas.create_line(
                            x_start + 1, wf_mid, x_end - 1, wf_mid, fill=clip_bg, tags=("static",)
                        )

            if self._rec_active:
                self._draw_live_rec_overlay()

            self._draw_scrollbar()
            self._move_playhead_only()
            self.canvas.tag_raise("playhead")
        except Exception:
            pass

    def _draw_live_rec_overlay(self):
        try:
            self.canvas.delete("rec_live")
            if not self._rec_active or not self._rec_armed:
                return
            _, span = self._lane_span()
            v_start, v_end, vis_dur, eff_dur = self._visible_window()
            if eff_dur <= 0.0 or vis_dur <= 0.0:
                return

            rec_dur = max(0.0, self._rec_elapsed)
            clip_t0 = max(0.0, v_start)
            clip_t1 = min(rec_dur, v_end)
            if clip_t1 <= clip_t0 and rec_dur > 0.0:
                return

            x_start = self.HEADER_W + ((clip_t0 - v_start) / vis_dur) * span
            x_end = self.HEADER_W + ((clip_t1 - v_start) / vis_dur) * span
            clip_w = max(6.0, x_end - x_start)
            x_end = min(self.HEADER_W + span, x_start + clip_w)
            peaks = self._rec_peaks
            n_peaks = len(peaks)

            for ch in self._rec_armed:
                if not (0 <= ch < 4):
                    continue
                y0 = self.RULER_H + ch * self.LANE_H
                y1 = y0 + self.LANE_H
                cy0 = y0 + 2
                cy1 = y1 - 2
                wf_y0 = cy0 + self.CLIP_HDR_H
                wf_mid = (wf_y0 + cy1) * 0.5
                max_amp = max(2.0, (cy1 - wf_y0) * 0.5 - 1.5)

                self.canvas.create_rectangle(
                    x_start, cy0, x_end, cy1,
                    fill="#3a161e", outline="#f24957", width=1, tags=("rec_live",),
                )
                self.canvas.create_rectangle(
                    x_start + 1, cy0 + 1, x_end - 1, wf_y0,
                    fill="#6e1e2a", outline="", tags=("rec_live",),
                )
                if clip_w > 34:
                    self.canvas.create_text(
                        x_start + 5,
                        (cy0 + wf_y0) * 0.5,
                        text="● REC",
                        fill="#ffd6da",
                        font=("Helvetica", 7, "bold"),
                        anchor="w",
                        tags=("rec_live",),
                    )
                if n_peaks >= 2 and rec_dur > 0.0:
                    p_start_f = (clip_t0 / rec_dur) * (n_peaks - 1)
                    p_end_f = (clip_t1 / rec_dur) * (n_peaks - 1)
                    n_pts = max(2, min(n_peaks, int(clip_w / 1.5)))
                    inner_w = max(1.0, (x_end - x_start) - 2.0)
                    poly_top = []
                    poly_bot = []
                    for p in range(n_pts):
                        frac_p = p / float(max(1, n_pts - 1))
                        p_idx = max(0, min(n_peaks - 1, int(round(p_start_f + frac_p * (p_end_f - p_start_f)))))
                        amp = max(1.0, min(1.0, peaks[p_idx]) * max_amp)
                        px = x_start + 1.0 + frac_p * inner_w
                        poly_top.extend((px, wf_mid - amp))
                        poly_bot.append((px, wf_mid + amp))
                    for px, py in reversed(poly_bot):
                        poly_top.extend((px, py))
                    self.canvas.create_polygon(
                        *poly_top, fill="#ff6b78", outline="#ff8a94", tags=("rec_live",)
                    )

            self.canvas.tag_raise("playhead")
        except Exception:
            pass

    def _move_playhead_only(self):
        """Lightweight O(1) playhead movement called at 30 FPS without redrawing waveforms."""
        self.pos_var.set(format_timecode(self._position))
        try:
            _, span = self._lane_span()
            total_h = self.RULER_H + self.LANE_H * 4
            v_start, v_end, vis_dur, eff_dur = self._visible_window()
            if eff_dur > 0.0 and vis_dur > 0.0:
                if self._position < v_start - 1e-6 or self._position > v_end + 1e-6:
                    self.canvas.itemconfig(self._playhead_line, state="hidden")
                    self.canvas.itemconfig(self._playhead_cap, state="hidden")
                    return
                frac = max(0.0, min(1.0, (self._position - v_start) / vis_dur))
            else:
                frac = 0.0
            cx = self.HEADER_W + frac * span
            cap_col = "#f24957" if self._rec_active else "#48d18a"
            self.canvas.itemconfig(self._playhead_line, state="normal")
            self.canvas.itemconfig(self._playhead_cap, state="normal", fill=cap_col)
            self.canvas.coords(self._playhead_line, cx, self.RULER_H - 2, cx, total_h)
            self.canvas.coords(
                self._playhead_cap, cx - 5, 3, cx + 5, 3, cx, self.RULER_H - 1
            )
        except Exception:
            pass


class Window:
    """4-Channel Audio Mixer GUI & Multithreaded Audio Engine."""

    THEME = {
        "bg_root": "#1d2d3d",
        "bg_strip": "#2b455c",
        "bg_header": "#203548",
        "bg_well": "#182836",
        "border": "#426382",
        "divider": "#3b5975",
        "text_primary": "#f0f6fc",
        "text_muted": "#9cb8d1",
        "unity_gold": "#ffd166",
        "boost_coral": "#fc8181",
        "status_fg": "#7ef2b8",
    }

    # (key, label, active_bg, led_color, inactive_bg, hover_bg, popup_cls)
    # Ordered top-to-bottom to match the DSP signal flow: Compressor -> Chorus -> Tremolo -> Delay
    EFFECT_SPECS = [
        ("compressor", "Compressor", "#9b2c2c", "#fc8181", "#38242c", "#4c303b", CompressorWindow),
        ("chorus", "Chorus", "#1c747d", "#6be0eb", "#1c3742", "#244957", ChorusWindow),
        ("tremolo", "Tremolo", "#246e4c", "#68d391", "#1e3736", "#274947", TremoloWindow),
        ("delay", "Delay", "#245994", "#63b3ed", "#20354a", "#2a4561", DelayWindow),
    ]

    def __init__(self, start_mainloop=True):
        self.buffer = 1024
        self.bitdepth = 16
        self.channels = 2
        self.rate = 44100

        # Thread synchronization
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # Set = running, Cleared = paused
        self._active_thread = None
        self._main_thread_id = threading.get_ident()
        self._ui_queue = queue.Queue()
        self.playing = False
        self.paused = False
        self.recording = False
        self.bouncing = False
        self._closing = False

        # Live post-fader VU RMS levels AND true sample peaks (4 channels + stereo master L/R)
        self._meter_lock = threading.Lock()
        self._channel_peaks = [0.0, 0.0, 0.0, 0.0]
        self._master_peaks = [0.0, 0.0]
        self._channel_true_peaks = [0.0, 0.0, 0.0, 0.0]
        self._master_true_peaks = [0.0, 0.0]

        # Master bus volume (linear gain, 1.0 = 0.0 dB unity)
        self._master_lock = threading.Lock()
        self._master_volume = 1.0

        # Session playhead, scrubbing & live recording waveform state
        self._seek_lock = threading.Lock()
        self._session_duration_sec = 0.0
        self._playhead_sec = 0.0
        self._pending_seek_sec = None
        self._rec_wave_lock = threading.Lock()
        self._rec_wave_armed = []
        self._rec_wave_peaks = []
        self._rec_wave_stride = 1
        self._rec_wave_accum = 0.0
        self._rec_wave_count = 0
        self._rec_wave_elapsed = 0.0
        self._rec_wave_active = False

        # Blink timer tick counter for armed Rec buttons (~2 Hz flash)
        self._blink_tick = 0

        # 4 independent channel states
        self.channel_states = [ChannelState(i + 1) for i in range(4)]

        # Open effect popup windows keyed by (channel_index_0_based, effect_name)
        self._effect_windows = {}

        # Build main Tkinter window
        self.root = tk.Tk()
        self.root.geometry("1120x788")
        self.root.title("Mini-DAW")
        self.root.resizable(False, False)
        self.root.configure(bg=self.THEME["bg_root"])
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<space>", self._on_space_key)
        self.root.bind("<Button-1>", self._on_root_click, add="+")
        for key in ("<plus>", "<equal>", "<KP_Add>"):
            self.root.bind(key, lambda _e: self._on_zoom_key("in"))
        for key in ("<minus>", "<KP_Subtract>"):
            self.root.bind(key, lambda _e: self._on_zoom_key("out"))
        for key in ("<Key-0>", "<KP_0>"):
            self.root.bind(key, lambda _e: self._on_zoom_key("reset"))

        self.font_header = font.Font(family="Helvetica", weight="bold", size=11)
        self.font_section = font.Font(family="Helvetica", weight="bold", size=8)
        self.font_small = font.Font(family="Helvetica", size=9)
        self.font_readout = font.Font(family="Helvetica", weight="bold", size=9)

        # Menu bar
        self.menu = tk.Menu(self.root)
        self.root.config(menu=self.menu)
        self.file_menu = tk.Menu(self.menu, tearoff=0)
        self.menu.add_cascade(label="File", menu=self.file_menu)
        self.file_menu.add_command(label="Exit", command=self.on_close)

        # Transport button live-LED callbacks (populated in _build_master_panel)
        self._set_play_lit = lambda _lit: None
        self._set_pause_lit = lambda _lit: None
        self._set_rec_lit = lambda _lit: None
        self._set_rec_mode = lambda _mode: None

        # Harmonious grid container:
        #   Row 0: Full-width 4-Track Waveform Arrange View + Time Ruler & Playhead
        #   Row 1: 4 Channel strips (cols 0..3) + Master Bus strip (col 4) + Control/Export (col 5)
        self.board = tk.Frame(self.root, bg=self.THEME["bg_root"], padx=14, pady=10)
        self.board.pack(fill=tk.BOTH, expand=True)
        self.board.rowconfigure(0, weight=0)
        self.board.rowconfigure(1, weight=1)
        for col in range(4):
            self.board.columnconfigure(col, weight=1, uniform="channels")
        self.board.columnconfigure(4, minsize=142)
        self.board.columnconfigure(5, minsize=248)

        # Top 4-track waveform timeline & playhead scrubber
        self.timeline = WaveformTimeline(self.board, on_seek=self.seek_to_seconds)
        self.timeline.frame.grid(row=0, column=0, columnspan=6, sticky="ew", pady=(0, 8))

        # Per-channel Tkinter widget/variable references
        self._ch_widgets = []
        self._channel_meters = []
        self._channel_peak_readouts = []
        self._rec_pad_modes = []
        self._master_meters = []
        self._master_peak_readout = None
        self._build_channel_strips()
        self._build_master_bus_strip()
        self._build_master_panel()

        if start_mainloop:
            self.root.after(33, self._poll_meters)
            self.root.mainloop()

    @staticmethod
    def _pointer_in_widget(widget, event):
        """Return True if the mouse event coordinates fall inside widget's screen bounds."""
        try:
            x = event.x_root - widget.winfo_rootx()
            y = event.y_root - widget.winfo_rooty()
            return 0 <= x <= widget.winfo_width() and 0 <= y <= widget.winfo_height()
        except Exception:
            return True

    @staticmethod
    def _reset_scale(scale, value):
        """Reset a tk.Scale on double-click and suppress default class click handling."""
        scale.set(value)
        return "break"

    def _make_flat_button(
        self,
        parent,
        text,
        command,
        bg="#3d7a47",
        hover_bg="#4c9458",
        active_bg="#2f6138",
        fg="#ffffff",
        font_spec=("Helvetica", 10, "bold"),
        pady=7,
    ):
        """Flat studio button that renders consistently on macOS without Aqua chrome."""
        btn = tk.Label(
            parent,
            text=text,
            bg=bg,
            fg=fg,
            font=font_spec,
            pady=pady,
            cursor="hand2",
            bd=0,
            highlightthickness=1,
            highlightbackground="#203548",
        )

        def on_enter(_e):
            btn.config(bg=hover_bg)

        def on_leave(_e):
            btn.config(bg=bg)

        def on_press(_e):
            btn.config(bg=active_bg)

        def on_release(event):
            if self._pointer_in_widget(btn, event):
                btn.config(bg=hover_bg)
                command()
            else:
                btn.config(bg=bg)

        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)
        btn.bind("<ButtonPress-1>", on_press)
        btn.bind("<ButtonRelease-1>", on_release)
        return btn

    def _make_transport_button(
        self,
        parent,
        kind,
        caption,
        command,
        icon_color,
        hover_icon_color,
        lit_bg=None,
        lit_border=None,
        lit_icon_color="#ffffff",
        width=48,
    ):
        """Crisp vector-drawn studio transport pad (Stop / Play / Pause / Rec) with active glow."""
        base_bg = "#23384c"
        hover_bg = "#2e4b66"
        active_bg = "#162636"
        base_border = "#426382"
        hover_border = "#638db3"

        pad = tk.Frame(
            parent,
            width=width,
            height=46,
            bg=base_bg,
            bd=0,
            highlightthickness=1,
            highlightbackground=base_border,
            cursor="hand2",
        )
        pad.pack_propagate(False)

        canvas = tk.Canvas(
            pad,
            width=24,
            height=18,
            bg=base_bg,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        canvas.pack(pady=(5, 1))

        if kind == "stop":
            shape_ids = (canvas.create_rectangle(6, 3, 18, 15, fill=icon_color, outline=""),)
        elif kind == "play":
            shape_ids = (canvas.create_polygon(7, 2, 7, 16, 19, 9, fill=icon_color, outline=""),)
        elif kind == "pause":
            shape_ids = (
                canvas.create_rectangle(6, 3, 10, 15, fill=icon_color, outline=""),
                canvas.create_rectangle(14, 3, 18, 15, fill=icon_color, outline=""),
            )
        else:  # "rec"
            shape_ids = (canvas.create_oval(6, 3, 18, 15, fill=icon_color, outline=""),)

        cap_lbl = tk.Label(
            pad,
            text=caption,
            bg=base_bg,
            fg="#b9d2e8",
            font=("Helvetica", 7, "bold"),
            cursor="hand2",
        )
        cap_lbl.pack()

        state = {"hover": False, "pressed": False, "mode": "off"}

        def refresh():
            mode = state["mode"]
            if state["pressed"]:
                bg_c = active_bg
                bd_c = hover_border
                ic_c = lit_icon_color if mode in ("solid", "blink_hi") else hover_icon_color
                tx_c = "#ffffff"
            elif mode == "solid" and lit_bg:
                bg_c = lit_bg
                bd_c = lit_border or hover_border
                ic_c = lit_icon_color
                tx_c = "#ffffff"
            elif mode == "blink_hi" and lit_bg:
                bg_c = lit_bg
                bd_c = lit_border or hover_border
                ic_c = lit_icon_color
                tx_c = "#ffffff"
            elif mode == "blink_lo":
                bg_c = "#3d1c22"
                bd_c = "#9e3843"
                ic_c = icon_color
                tx_c = "#ff9ea6"
            elif state["hover"]:
                bg_c = hover_bg
                bd_c = hover_border
                ic_c = hover_icon_color
                tx_c = "#f0f6fc"
            else:
                bg_c = base_bg
                bd_c = base_border
                ic_c = icon_color
                tx_c = "#b9d2e8"

            pad.config(bg=bg_c, highlightbackground=bd_c)
            canvas.config(bg=bg_c)
            for sid in shape_ids:
                canvas.itemconfig(sid, fill=ic_c)
            cap_lbl.config(bg=bg_c, fg=tx_c)

        def set_mode(mode):
            if state["mode"] != mode:
                state["mode"] = mode
                refresh()

        def set_lit(is_lit):
            set_mode("solid" if is_lit else "off")

        set_lit.set_mode = set_mode

        def on_enter(_event):
            state["hover"] = True
            refresh()

        def on_leave(event):
            if not self._pointer_in_widget(pad, event):
                state["hover"] = False
                state["pressed"] = False
                refresh()

        def on_press(_event):
            state["pressed"] = True
            refresh()

        def on_release(event):
            was_inside = self._pointer_in_widget(pad, event)
            state["pressed"] = False
            state["hover"] = was_inside
            refresh()
            if was_inside:
                command()

        for widget in (pad, canvas, cap_lbl):
            widget.bind("<Enter>", on_enter)
            widget.bind("<Leave>", on_leave)
            widget.bind("<ButtonPress-1>", on_press)
            widget.bind("<ButtonRelease-1>", on_release)

        return pad, set_lit

    def _make_compact_clear_button(self, parent, command):
        """Compact icon-only ✕ clear button designed to sit inside the top file bar."""
        base_bg = "#261d24"
        hover_bg = "#7a2a36"
        active_bg = "#1c1218"
        base_border = "#4a333b"
        hover_border = "#ff858f"

        lbl = tk.Label(
            parent,
            text="✕",
            width=2,
            bg=base_bg,
            fg="#b87c85",
            font=("Helvetica", 8, "bold"),
            cursor="hand2",
            highlightthickness=1,
            highlightbackground=base_border,
        )

        def on_enter(_e):
            lbl.config(bg=hover_bg, fg="#ffffff", highlightbackground=hover_border)

        def on_leave(_e):
            lbl.config(bg=base_bg, fg="#b87c85", highlightbackground=base_border)

        def on_press(_e):
            lbl.config(bg=active_bg, fg="#ffffff")

        def on_release(e):
            if self._pointer_in_widget(lbl, e):
                lbl.config(bg=hover_bg, fg="#ffffff", highlightbackground=hover_border)
                command()
            else:
                lbl.config(bg=base_bg, fg="#b87c85", highlightbackground=base_border)

        lbl.bind("<Enter>", on_enter)
        lbl.bind("<Leave>", on_leave)
        lbl.bind("<ButtonPress-1>", on_press)
        lbl.bind("<ButtonRelease-1>", on_release)
        return lbl

    def _make_effect_slot(
        self,
        parent,
        channel_idx,
        fx_key,
        fx_label,
        active_bg,
        led_color,
        inactive_bg,
        hover_bg,
        var,
    ):
        """Sleek DAW insert slot: clickable LED+name bypass toggle + integrated ⚙ edit button."""
        slot = tk.Frame(
            parent,
            bg=inactive_bg,
            height=26,
            highlightthickness=1,
            highlightbackground="#38546e",
        )
        slot.pack(fill=tk.X, padx=8, pady=2)
        slot.pack_propagate(False)

        toggle_area = tk.Frame(slot, bg=inactive_bg, cursor="hand2")
        toggle_area.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        led = tk.Label(
            toggle_area,
            text="●",
            bg=inactive_bg,
            fg="#4c6780",
            font=("Helvetica", 9),
            padx=5,
            cursor="hand2",
        )
        led.pack(side=tk.LEFT)

        name_lbl = tk.Label(
            toggle_area,
            text=fx_label,
            bg=inactive_bg,
            fg="#c9dced",
            font=("Helvetica", 9, "bold"),
            anchor="w",
            cursor="hand2",
        )
        name_lbl.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        sep = tk.Frame(slot, bg="#38546e", width=1)
        sep.pack(side=tk.LEFT, fill=tk.Y, pady=3)

        edit_lbl = tk.Label(
            slot,
            text="⚙",
            bg=inactive_bg,
            fg="#b8d0e6",
            font=("Helvetica", 11),
            width=3,
            anchor="center",
            cursor="hand2",
        )
        edit_lbl.pack(side=tk.RIGHT, fill=tk.Y)

        hover_state = {"toggle": False, "edit": False}

        def refresh_visuals():
            is_on = bool(var.get())
            base_bg = active_bg if is_on else inactive_bg
            t_bg = active_bg if (is_on and not hover_state["toggle"]) else (hover_bg if not is_on and hover_state["toggle"] else base_bg)
            e_bg = hover_bg if hover_state["edit"] else base_bg

            slot.config(
                bg=base_bg,
                highlightbackground=led_color if is_on else "#38546e",
            )
            toggle_area.config(bg=t_bg)
            led.config(bg=t_bg, fg=led_color if is_on else "#4c6780")
            name_lbl.config(bg=t_bg, fg="#ffffff" if is_on else "#bfd4e8")
            sep.config(bg=led_color if is_on else "#38546e")
            edit_lbl.config(
                bg=e_bg,
                fg="#ffffff" if (is_on or hover_state["edit"]) else "#9cb8d1",
            )

        def on_toggle_click(_e):
            var.set(not bool(var.get()))
            refresh_visuals()
            self._on_effect_toggle(channel_idx, fx_key, var)

        def on_toggle_enter(_e):
            hover_state["toggle"] = True
            refresh_visuals()

        def on_toggle_leave(e):
            if not self._pointer_in_widget(toggle_area, e):
                hover_state["toggle"] = False
                refresh_visuals()

        def on_edit_click(_e):
            self.open_effect_window(channel_idx, fx_key)

        def on_edit_enter(_e):
            hover_state["edit"] = True
            refresh_visuals()

        def on_edit_leave(_e):
            hover_state["edit"] = False
            refresh_visuals()

        for w in (toggle_area, led, name_lbl):
            w.bind("<Button-1>", on_toggle_click)
            w.bind("<Enter>", on_toggle_enter)
            w.bind("<Leave>", on_toggle_leave)

        edit_lbl.bind("<Button-1>", on_edit_click)
        edit_lbl.bind("<Enter>", on_edit_enter)
        edit_lbl.bind("<Leave>", on_edit_leave)

        refresh_visuals()
        return refresh_visuals

    def _make_toggle_pad(
        self,
        parent,
        text,
        var,
        accent_color,
        off_bg,
        off_hover_bg,
        off_fg,
        off_border,
        on_bg,
        on_fg,
        on_border,
        on_change,
    ):
        """Distinct color-coded DAW toggle pad with accent stripe (Solo / Mute / Ø Phase / Rec)."""
        pad = tk.Frame(
            parent,
            bg=off_bg,
            height=26,
            cursor="hand2",
            highlightthickness=1,
            highlightbackground=off_border,
        )
        pad.pack_propagate(False)

        stripe = tk.Frame(pad, bg=accent_color, width=4, cursor="hand2")
        stripe.pack(side=tk.LEFT, fill=tk.Y)

        lbl = tk.Label(
            pad,
            text=text,
            bg=off_bg,
            fg=off_fg,
            font=("Helvetica", 9, "bold"),
            anchor="center",
            cursor="hand2",
        )
        lbl.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        hover = {"in": False, "flash_mode": None}

        def refresh():
            is_on = bool(var.get())
            fmode = hover["flash_mode"]
            if is_on and fmode == "blink_lo":
                # Dim armed flash phase (clearly armed, alternating with bright solid)
                pad.config(bg="#59222a", highlightbackground=accent_color)
                stripe.config(bg=accent_color)
                lbl.config(bg="#59222a", fg="#ffb3ba")
            elif is_on:
                pad.config(bg=on_bg, highlightbackground=on_border)
                stripe.config(bg=on_border)
                lbl.config(bg=on_bg, fg=on_fg)
            else:
                bg_now = off_hover_bg if hover["in"] else off_bg
                pad.config(
                    bg=bg_now,
                    highlightbackground=accent_color if hover["in"] else off_border,
                )
                stripe.config(bg=accent_color)
                lbl.config(bg=bg_now, fg="#ffffff" if hover["in"] else off_fg)

        def set_flash_mode(mode):
            if hover["flash_mode"] != mode:
                hover["flash_mode"] = mode
                refresh()

        def on_click(_e):
            var.set(not bool(var.get()))
            if not var.get():
                hover["flash_mode"] = None
            refresh()
            on_change(bool(var.get()))

        def on_enter(_e):
            hover["in"] = True
            refresh()

        def on_leave(e):
            if not self._pointer_in_widget(pad, e):
                hover["in"] = False
                refresh()

        for w in (pad, stripe, lbl):
            w.bind("<Button-1>", on_click)
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)

        refresh()
        pad.set_flash_mode = set_flash_mode
        return pad

    def _make_fader_ticks_canvas(
        self,
        parent,
        bg,
        fader_len,
        pad_frac,
        major_ticks,
        minor_step_db=3.0,
        width=32,
    ):
        """Crisp studio fader dB ruler canvas with major labels and minor hash marks."""
        T = self.THEME
        cv = tk.Canvas(
            parent,
            width=width,
            height=fader_len,
            bg=bg,
            bd=0,
            highlightthickness=0,
        )
        db_span = MAX_FADER_DB - MIN_FADER_DB
        top_y = fader_len * pad_frac
        span_y = fader_len * (1.0 - 2.0 * pad_frac)
        major_dbs = {db for db, _ in major_ticks}

        # Minor graduation dashes
        db = float(MAX_FADER_DB)
        while db >= float(MIN_FADER_DB) - 1e-6:
            if not any(abs(db - m) < 0.05 for m in major_dbs):
                y = top_y + ((MAX_FADER_DB - db) / db_span) * span_y
                cv.create_line(0, y, 3, y, fill="#527494", width=1)
            db -= float(minor_step_db)

        # Major ticks + labels (non-overlapping guard prioritizing unity and endpoints)
        min_gap = 10.5
        drawn_ys = []
        priority = [t for t in major_ticks if t[0] in (0.0, MAX_FADER_DB, MIN_FADER_DB)]
        others = [t for t in major_ticks if t[0] not in (0.0, MAX_FADER_DB, MIN_FADER_DB)]
        show_lbl = set()
        for tick_db, _ in priority + others:
            y = top_y + ((MAX_FADER_DB - tick_db) / db_span) * span_y
            if all(abs(y - py) >= min_gap for py in drawn_ys):
                drawn_ys.append(y)
                show_lbl.add(tick_db)

        for tick_db, tick_txt in major_ticks:
            y = top_y + ((MAX_FADER_DB - tick_db) / db_span) * span_y
            is_unity = tick_db == 0.0
            has_lbl = tick_db in show_lbl
            col = T["unity_gold"] if is_unity else (T["text_muted"] if has_lbl else "#5c7e9e")
            cv.create_line(0, y, 5 if has_lbl else 3, y, fill=col, width=1.5 if is_unity else 1)
            if has_lbl:
                cv.create_text(
                    7,
                    y,
                    text=tick_txt,
                    fill=col,
                    font=("Helvetica", 7, "bold" if is_unity else "normal"),
                    anchor="w",
                )
        return cv

    def _build_channel_strips(self):
        T = self.THEME

        for idx in range(4):
            ch_num = idx + 1

            frame = tk.Frame(
                self.board,
                bg=T["bg_strip"],
                highlightthickness=1,
                highlightbackground=T["border"],
            )
            frame.grid(row=1, column=idx, sticky="nsew", padx=(0, 8))

            # Channel top header bar
            header = tk.Frame(frame, bg=T["bg_header"], height=30)
            header.pack(fill=tk.X)
            header.pack_propagate(False)
            tk.Label(
                header,
                text=f"CHANNEL {ch_num}",
                bg=T["bg_header"],
                fg=T["text_primary"],
                font=self.font_header,
            ).pack(expand=True)

            # Top file well: clickable filename bar + compact ✕ clear button + format metadata
            file_well = tk.Frame(
                frame,
                bg=T["bg_well"],
                padx=5,
                pady=4,
                highlightthickness=1,
                highlightbackground="#38546e",
            )
            file_well.pack(pady=(6, 4), padx=8, fill=tk.X)

            file_top = tk.Frame(file_well, bg=T["bg_well"])
            file_top.pack(fill=tk.X)

            clear_icon = self._make_compact_clear_button(
                file_top,
                command=lambda c=idx: self.clear_channel_file(c),
            )
            clear_icon.pack(side=tk.RIGHT, padx=(3, 0))

            file_var = tk.StringVar(value="(click to load)")
            file_lbl = tk.Label(
                file_top,
                textvariable=file_var,
                bg=T["bg_well"],
                fg="#dcecf9",
                font=("Helvetica", 8, "bold"),
                anchor="center",
                padx=4,
                pady=2,
                cursor="hand2",
            )
            file_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
            file_lbl.bind("<Button-1>", lambda _e, c=idx: self.open_channel_file(c))
            file_lbl.bind("<Enter>", lambda _e, l=file_lbl: l.config(bg="#243a4f", fg="#ffffff"))
            file_lbl.bind("<Leave>", lambda _e, l=file_lbl: l.config(bg=T["bg_well"], fg="#dcecf9"))

            meta_var = tk.StringVar(value="—")
            meta_lbl = tk.Label(
                file_well,
                textvariable=meta_var,
                bg=T["bg_well"],
                fg="#7cc0f7",
                font=("Helvetica", 8),
                anchor="center",
                cursor="hand2",
            )
            meta_lbl.pack(fill=tk.X, pady=(2, 0))
            meta_lbl.bind("<Button-1>", lambda _e, c=idx: self.open_channel_file(c))

            # Insert Rack section
            tk.Label(
                frame,
                text="INSERTS",
                bg=T["bg_strip"],
                fg=T["text_muted"],
                font=self.font_section,
                anchor="w",
            ).pack(fill=tk.X, padx=10, pady=(2, 2))

            fx_vars = {}
            for fx_key, fx_label, active_bg, led_color, inactive_bg, hover_bg, _ in self.EFFECT_SPECS:
                var = tk.BooleanVar(value=False)
                fx_vars[fx_key] = var
                self._make_effect_slot(
                    frame,
                    idx,
                    fx_key,
                    fx_label,
                    active_bg,
                    led_color,
                    inactive_bg,
                    hover_bg,
                    var,
                )

            # Subtle divider
            tk.Frame(frame, bg=T["divider"], height=1).pack(fill=tk.X, padx=10, pady=(8, 6))

            # Pan control (0.0 = Left, 0.5 = Center, 1.0 = Right)
            pan_label_var = tk.StringVar(value="Pan: C")
            tk.Label(
                frame,
                textvariable=pan_label_var,
                bg=T["bg_strip"],
                fg=T["text_primary"],
                font=self.font_small,
            ).pack()

            pan_row = tk.Frame(frame, bg=T["bg_strip"])
            pan_row.pack(fill=tk.X, padx=8, pady=(2, 6))

            tk.Label(
                pan_row, text="L", bg=T["bg_strip"], fg=T["text_muted"], font=self.font_section
            ).pack(side=tk.LEFT, padx=(0, 3))

            pan_scale = tk.Scale(
                pan_row,
                from_=0.0,
                to=1.0,
                resolution=0.01,
                showvalue=False,
                orient=tk.HORIZONTAL,
                length=116,
                bg=T["bg_strip"],
                troughcolor=T["bg_well"],
                activebackground="#4a749c",
                highlightthickness=0,
                bd=0,
                sliderlength=16,
                command=lambda val, c=idx, lbl=pan_label_var: self._on_pan_change(c, val, lbl),
            )
            pan_scale.set(0.5)
            pan_scale.bind("<Double-Button-1>", lambda _e, s=pan_scale: self._reset_scale(s, 0.5))
            pan_scale.pack(side=tk.LEFT, fill=tk.X, expand=True)

            tk.Label(
                pan_row, text="R", bg=T["bg_strip"], fg=T["text_muted"], font=self.font_section
            ).pack(side=tk.RIGHT, padx=(3, 0))

            # Dual readouts above meter & fader: [Peak dBFS] [Fader dB]
            readout_row = tk.Frame(frame, bg=T["bg_strip"])
            readout_row.pack(fill=tk.X, padx=8, pady=(2, 5))

            peak_readout = PeakReadout(readout_row, width=8)
            peak_readout.label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3))
            self._channel_peak_readouts.append(peak_readout)

            vol_label_var = tk.StringVar(value="0.0 dB")
            vol_badge = tk.Label(
                readout_row,
                textvariable=vol_label_var,
                bg=T["bg_well"],
                fg=T["unity_gold"],
                font=("Helvetica", 8, "bold"),
                width=8,
                pady=2,
                highlightthickness=1,
                highlightbackground="#2c465e",
            )
            vol_badge.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(3, 0))

            fader_row = tk.Frame(frame, bg=T["bg_strip"])
            fader_row.pack(pady=(0, 8))

            fader_len = 148

            # Real-time vertical VU LED meter next to the volume fader
            meter = ChannelMeter(fader_row, width=16, height=fader_len, segments=22)
            meter.canvas.pack(side=tk.LEFT, padx=(6, 6))
            self._channel_meters.append(meter)

            vol_scale = tk.Scale(
                fader_row,
                from_=MAX_FADER_DB,
                to=MIN_FADER_DB,
                resolution=0.5,
                showvalue=False,
                orient=tk.VERTICAL,
                length=fader_len,
                bg=T["bg_strip"],
                troughcolor=T["bg_well"],
                activebackground="#4a749c",
                highlightthickness=0,
                bd=0,
                sliderlength=22,
                command=lambda val, c=idx, lbl=vol_label_var, bdg=vol_badge: self._on_vol_change(
                    c, val, lbl, bdg
                ),
            )
            vol_scale.set(0.0)
            vol_scale.bind("<Double-Button-1>", lambda _e, s=vol_scale: self._reset_scale(s, 0.0))
            vol_scale.pack(side=tk.LEFT, padx=(0, 2))

            ch_ticks = (
                (6.0, "+6"),
                (0.0, "0"),
                (-6.0, "-6"),
                (-12.0, "-12"),
                (-18.0, "-18"),
                (-24.0, "-24"),
                (-30.0, "-30"),
                (-36.0, "-36"),
                (-42.0, "-42"),
                (-48.0, "-48"),
                (-54.0, "-54"),
                (MIN_FADER_DB, "-∞"),
            )
            ticks_cv = self._make_fader_ticks_canvas(
                fader_row,
                bg=T["bg_strip"],
                fader_len=fader_len,
                pad_frac=0.08,
                major_ticks=ch_ticks,
                minor_step_db=3.0,
            )
            ticks_cv.pack(side=tk.LEFT)

            # Channel control toggle pads pinned cleanly at bottom
            ctrl_grid = tk.Frame(frame, bg=T["bg_strip"])
            ctrl_grid.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 8))

            solo_var = tk.BooleanVar(value=False)
            mute_var = tk.BooleanVar(value=False)
            pol_var = tk.BooleanVar(value=False)
            rec_var = tk.BooleanVar(value=False)

            solo_pad = self._make_toggle_pad(
                ctrl_grid,
                text="S  Solo",
                var=solo_var,
                accent_color="#f5c542",
                off_bg="#332c19",
                off_hover_bg="#473d22",
                off_fg="#fae08c",
                off_border="#7a662b",
                on_bg="#dba316",
                on_fg="#1c1400",
                on_border="#fff1ad",
                on_change=lambda en, c=idx: self.channel_states[c].set_solo(en),
            )
            mute_pad = self._make_toggle_pad(
                ctrl_grid,
                text="M  Mute",
                var=mute_var,
                accent_color="#f27a30",
                off_bg="#382319",
                off_hover_bg="#4d2f21",
                off_fg="#ffa873",
                off_border="#804a2c",
                on_bg="#d95516",
                on_fg="#ffffff",
                on_border="#ffc6a3",
                on_change=lambda en, c=idx: self.channel_states[c].set_mute(en),
            )
            pol_pad = self._make_toggle_pad(
                ctrl_grid,
                text="Ø  Phase",
                var=pol_var,
                accent_color="#a375ff",
                off_bg="#2b2142",
                off_hover_bg="#3b2d5c",
                off_fg="#d1b3ff",
                off_border="#614994",
                on_bg="#7242d6",
                on_fg="#ffffff",
                on_border="#dec9ff",
                on_change=lambda en, c=idx: self.channel_states[c].set_polarity_invert(en),
            )
            rec_pad = self._make_toggle_pad(
                ctrl_grid,
                text="●  Rec",
                var=rec_var,
                accent_color="#f24957",
                off_bg="#3a1c22",
                off_hover_bg="#52252e",
                off_fg="#ff8c96",
                off_border="#853743",
                on_bg="#cc2431",
                on_fg="#ffffff",
                on_border="#ffb8bf",
                on_change=lambda en, c=idx: self.channel_states[c].set_rec_armed(en),
            )

            solo_pad.grid(row=0, column=0, padx=2, pady=2, sticky="ew")
            mute_pad.grid(row=0, column=1, padx=2, pady=2, sticky="ew")
            pol_pad.grid(row=1, column=0, padx=2, pady=2, sticky="ew")
            rec_pad.grid(row=1, column=1, padx=2, pady=2, sticky="ew")
            ctrl_grid.columnconfigure(0, weight=1, uniform="ctrl")
            ctrl_grid.columnconfigure(1, weight=1, uniform="ctrl")

            self._rec_pad_modes.append(rec_pad.set_flash_mode)

            self._ch_widgets.append(
                {
                    "file_var": file_var,
                    "meta_var": meta_var,
                    "fx_vars": fx_vars,
                    "pan_scale": pan_scale,
                    "vol_scale": vol_scale,
                    "solo_var": solo_var,
                    "mute_var": mute_var,
                    "pol_var": pol_var,
                    "rec_var": rec_var,
                }
            )

    def _build_master_bus_strip(self):
        """Dedicated Master Bus fader & Stereo L/R VU meter strip (sum of Channels 1–4)."""
        T = self.THEME

        strip = tk.Frame(
            self.board,
            bg="#263e54",
            highlightthickness=1,
            highlightbackground="#4f7496",
        )
        strip.grid(row=1, column=4, sticky="nsew", padx=(2, 10))

        # Matching top header bar
        header = tk.Frame(strip, bg="#1b2f40", height=30)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(
            header,
            text="MASTER BUS",
            bg="#1b2f40",
            fg=T["unity_gold"],
            font=self.font_header,
        ).pack(expand=True)

        # Bus label badge
        tk.Label(
            strip,
            text="SUM · CH 1–4",
            bg=T["bg_well"],
            fg="#8fc5f5",
            font=("Helvetica", 8, "bold"),
            pady=5,
            highlightthickness=1,
            highlightbackground="#38546e",
        ).pack(fill=tk.X, padx=8, pady=(8, 12))

        # Peak dBFS clip indicator + Master Fader dB readout
        tk.Label(
            strip,
            text="PEAK  /  FADER",
            bg="#263e54",
            fg=T["text_muted"],
            font=self.font_section,
        ).pack(pady=(2, 4))

        master_readout_row = tk.Frame(strip, bg="#263e54")
        master_readout_row.pack(fill=tk.X, padx=8, pady=(0, 8))

        self._master_peak_readout = PeakReadout(master_readout_row, width=7)
        self._master_peak_readout.label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3))

        self.master_vol_var = tk.StringVar(value="0.0 dB")
        self.master_vol_badge = tk.Label(
            master_readout_row,
            textvariable=self.master_vol_var,
            bg=T["bg_well"],
            fg=T["unity_gold"],
            font=("Helvetica", 8, "bold"),
            width=7,
            pady=2,
            highlightthickness=1,
            highlightbackground="#2c465e",
        )
        self.master_vol_badge.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(3, 0))

        # Stereo L/R VU meters + Master Fader + dB ticks (sized to fill Master Bus strip)
        fader_len = 332
        bus_row = tk.Frame(strip, bg="#263e54")
        bus_row.pack(expand=True, pady=(2, 4))

        meters_col = tk.Frame(bus_row, bg="#263e54")
        meters_col.pack(side=tk.LEFT, padx=(4, 8))

        lr_pair = tk.Frame(meters_col, bg="#263e54")
        lr_pair.pack()

        meter_l = ChannelMeter(lr_pair, width=16, height=fader_len, segments=42)
        meter_r = ChannelMeter(lr_pair, width=16, height=fader_len, segments=42)
        meter_l.canvas.pack(side=tk.LEFT, padx=(0, 3))
        meter_r.canvas.pack(side=tk.LEFT)
        self._master_meters = [meter_l, meter_r]

        lr_labels = tk.Frame(meters_col, bg="#263e54")
        lr_labels.pack(fill=tk.X, pady=(3, 0))
        tk.Label(
            lr_labels, text="L", bg="#263e54", fg=T["text_muted"], font=("Helvetica", 8, "bold")
        ).pack(side=tk.LEFT, padx=(5, 0))
        tk.Label(
            lr_labels, text="R", bg="#263e54", fg=T["text_muted"], font=("Helvetica", 8, "bold")
        ).pack(side=tk.RIGHT, padx=(0, 5))

        fader_col = tk.Frame(bus_row, bg="#263e54")
        fader_col.pack(side=tk.LEFT, anchor="n")

        self.master_scale = tk.Scale(
            fader_col,
            from_=MAX_FADER_DB,
            to=MIN_FADER_DB,
            resolution=0.5,
            showvalue=False,
            orient=tk.VERTICAL,
            length=fader_len,
            bg="#263e54",
            troughcolor=T["bg_well"],
            activebackground="#ffd166",
            highlightthickness=0,
            bd=0,
            sliderlength=26,
            command=self._on_master_vol_change,
        )
        self.master_scale.set(0.0)
        self.master_scale.bind("<Double-Button-1>", lambda _e: self._reset_scale(self.master_scale, 0.0))
        self.master_scale.pack(side=tk.LEFT, padx=(0, 2))

        master_ticks = (
            (6.0, "+6"),
            (3.0, "+3"),
            (0.0, "0"),
            (-3.0, "-3"),
            (-6.0, "-6"),
            (-9.0, "-9"),
            (-12.0, "-12"),
            (-15.0, "-15"),
            (-18.0, "-18"),
            (-24.0, "-24"),
            (-30.0, "-30"),
            (-36.0, "-36"),
            (-42.0, "-42"),
            (-48.0, "-48"),
            (-54.0, "-54"),
            (MIN_FADER_DB, "-∞"),
        )
        ticks_cv = self._make_fader_ticks_canvas(
            fader_col,
            bg="#263e54",
            fader_len=fader_len,
            pad_frac=0.04,
            major_ticks=master_ticks,
            minor_step_db=1.5,
        )
        ticks_cv.pack(side=tk.LEFT)

        # Unity reset shortcut button at bottom of Master Bus strip
        reset_btn = self._make_flat_button(
            strip,
            text="0.0 dB Unity",
            command=lambda: self.master_scale.set(0.0),
            bg="#1e3447",
            hover_bg="#2b4863",
            active_bg="#162636",
            fg="#ffd166",
            font_spec=("Helvetica", 8, "bold"),
            pady=5,
        )
        reset_btn.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 10))

    def _build_master_panel(self):
        T = self.THEME

        panel = tk.Frame(
            self.board,
            bg=T["bg_strip"],
            highlightthickness=1,
            highlightbackground=T["border"],
        )
        panel.grid(row=1, column=5, sticky="nsew", padx=(0, 0))

        # Matching top header bar
        header = tk.Frame(panel, bg=T["bg_header"], height=30)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(
            header,
            text="CONTROL & EXPORT",
            bg=T["bg_header"],
            fg=T["text_primary"],
            font=self.font_header,
        ).pack(expand=True)

        # Transport bay
        tk.Label(
            panel,
            text="TRANSPORT",
            bg=T["bg_strip"],
            fg=T["text_muted"],
            font=self.font_section,
            anchor="w",
        ).pack(fill=tk.X, padx=12, pady=(12, 4))

        transport_bay = tk.Frame(
            panel,
            bg=T["bg_well"],
            padx=8,
            pady=10,
            highlightthickness=1,
            highlightbackground="#38546e",
        )
        transport_bay.pack(fill=tk.X, padx=12)

        transport_inner = tk.Frame(transport_bay, bg=T["bg_well"])
        transport_inner.pack(anchor="center")

        self.stop_button, _ = self._make_transport_button(
            transport_inner,
            kind="stop",
            caption="STOP",
            command=self.stop,
            icon_color="#d5e4f2",
            hover_icon_color="#ffffff",
            width=46,
        )
        self.play_button, self._set_play_lit = self._make_transport_button(
            transport_inner,
            kind="play",
            caption="PLAY",
            command=self.press_play,
            icon_color="#48d18a",
            hover_icon_color="#7ef2b8",
            lit_bg="#246e4c",
            lit_border="#68d391",
            lit_icon_color="#ffffff",
            width=46,
        )
        self.pause_button, self._set_pause_lit = self._make_transport_button(
            transport_inner,
            kind="pause",
            caption="PAUSE",
            command=self.press_pause,
            icon_color="#f5c542",
            hover_icon_color="#ffe28a",
            lit_bg="#b88414",
            lit_border="#ffe28a",
            lit_icon_color="#ffffff",
            width=46,
        )
        self.rec_button, self._set_rec_lit = self._make_transport_button(
            transport_inner,
            kind="rec",
            caption="REC",
            command=self.press_rec,
            icon_color="#f24957",
            hover_icon_color="#ff7a85",
            lit_bg="#b82530",
            lit_border="#ff858f",
            lit_icon_color="#ffffff",
            width=46,
        )
        self._set_rec_mode = self._set_rec_lit.set_mode

        self.stop_button.grid(row=0, column=0, padx=3)
        self.play_button.grid(row=0, column=1, padx=3)
        self.pause_button.grid(row=0, column=2, padx=3)
        self.rec_button.grid(row=0, column=3, padx=3)

        # Status screen
        tk.Label(
            panel,
            text="STATUS",
            bg=T["bg_strip"],
            fg=T["text_muted"],
            font=self.font_section,
            anchor="w",
        ).pack(fill=tk.X, padx=12, pady=(14, 4))

        self.status_var = tk.StringVar(value="Ready")
        tk.Label(
            panel,
            textvariable=self.status_var,
            bg=T["bg_well"],
            fg=T["status_fg"],
            font=("Helvetica", 10, "bold"),
            wraplength=192,
            justify=tk.CENTER,
            padx=8,
            pady=12,
            highlightthickness=1,
            highlightbackground="#38546e",
        ).pack(padx=12, fill=tk.X)

        # Divider
        tk.Frame(panel, bg=T["divider"], height=1).pack(fill=tk.X, padx=12, pady=14)

        # Bounce / Export section
        tk.Label(
            panel,
            text="MIX / RECORDING NAME",
            bg=T["bg_strip"],
            fg=T["text_muted"],
            font=self.font_section,
            anchor="w",
        ).pack(fill=tk.X, padx=12, pady=(0, 4))

        self.nameEntry = tk.StringVar()
        self.nameBox = tk.Entry(
            panel,
            textvariable=self.nameEntry,
            bg=T["bg_well"],
            fg=T["text_primary"],
            insertbackground=T["text_primary"],
            font=("Helvetica", 10),
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground="#45698a",
            highlightcolor="#63b3ed",
        )
        self.nameBox.pack(padx=12, pady=(0, 10), ipady=5, fill=tk.X)
        self.nameBox.bind("<Return>", lambda _e: self.root.focus_set())
        self.nameBox.bind("<Escape>", lambda _e: self.root.focus_set())

        self.export_button = self._make_flat_button(
            panel,
            text="Bounce Mix (.wav)",
            command=self.bounce,
            bg="#3b7a47",
            hover_bg="#4a9458",
            active_bg="#2d6037",
            pady=8,
        )
        self.export_button.pack(padx=12, fill=tk.X)

        # Bounce progress bar & readout
        self._bounce_prog_var = tk.StringVar(value="Bounce: ready")
        tk.Label(
            panel,
            textvariable=self._bounce_prog_var,
            bg=T["bg_strip"],
            fg=T["text_muted"],
            font=("Helvetica", 8, "bold"),
            anchor="w",
        ).pack(fill=tk.X, padx=12, pady=(8, 2))

        self._bounce_canvas = tk.Canvas(
            panel,
            height=12,
            bg=T["bg_well"],
            bd=0,
            highlightthickness=1,
            highlightbackground="#38546e",
        )
        self._bounce_canvas.pack(fill=tk.X, padx=12, pady=(0, 8))
        self._bounce_bar_id = self._bounce_canvas.create_rectangle(
            0, 0, 0, 12, fill="#48d18a", outline=""
        )

        # Quick guide anchored cleanly at the bottom
        tips_box = tk.Frame(
            panel,
            bg=T["bg_header"],
            padx=10,
            pady=8,
            highlightthickness=1,
            highlightbackground="#38546e",
        )
        tips_box.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(0, 10))

        tk.Label(
            tips_box,
            text="QUICK GUIDE",
            bg=T["bg_header"],
            fg=T["text_muted"],
            font=self.font_section,
            anchor="w",
        ).pack(fill=tk.X, pady=(0, 4))

        tips = (
            "• Click / drag top bar to scrub\n"
            "• Scroll / ± keys to zoom waveform\n"
            "• Spacebar toggles Play / Pause\n"
            "• Double-click fader / pan to reset\n"
            "• Arm ● Rec (flashes), click REC"
        )
        tk.Label(
            tips_box,
            text=tips,
            bg=T["bg_header"],
            fg="#c9dced",
            font=("Helvetica", 9),
            justify=tk.LEFT,
            anchor="w",
        ).pack(fill=tk.X)

    # ---------- Thread-safe UI helper ----------

    def _ui_call(self, fn, *args, **kwargs):
        if self._closing:
            return
        if threading.get_ident() == self._main_thread_id:
            try:
                fn(*args, **kwargs)
            except tk.TclError:
                pass
        else:
            self._ui_queue.put((fn, args, kwargs))

    def _drain_ui_queue(self):
        while not self._closing:
            try:
                fn, args, kwargs = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args, **kwargs)
            except tk.TclError:
                pass

    def _set_status(self, text):
        self._ui_call(self.status_var.set, text)

    def _set_meter_peaks(
        self, ch_levels, master_levels=None, ch_true_peaks=None, master_true_peaks=None
    ):
        if hasattr(self, "_meter_lock"):
            with self._meter_lock:
                for i in range(min(4, len(ch_levels))):
                    self._channel_peaks[i] = ch_levels[i]
                if master_levels is not None and len(master_levels) >= 2:
                    self._master_peaks[0] = master_levels[0]
                    self._master_peaks[1] = master_levels[1]
                # Latch highest true peak between GUI ticks so transient clips are never missed
                if ch_true_peaks is not None and hasattr(self, "_channel_true_peaks"):
                    for i in range(min(4, len(ch_true_peaks))):
                        if ch_true_peaks[i] > self._channel_true_peaks[i]:
                            self._channel_true_peaks[i] = ch_true_peaks[i]
                if (
                    master_true_peaks is not None
                    and len(master_true_peaks) >= 2
                    and hasattr(self, "_master_true_peaks")
                ):
                    if master_true_peaks[0] > self._master_true_peaks[0]:
                        self._master_true_peaks[0] = master_true_peaks[0]
                    if master_true_peaks[1] > self._master_true_peaks[1]:
                        self._master_true_peaks[1] = master_true_peaks[1]

    def _reset_meter_peaks(self):
        if hasattr(self, "_meter_lock"):
            with self._meter_lock:
                self._channel_peaks = [0.0, 0.0, 0.0, 0.0]
                self._master_peaks = [0.0, 0.0]
                if hasattr(self, "_channel_true_peaks"):
                    self._channel_true_peaks = [0.0, 0.0, 0.0, 0.0]
                if hasattr(self, "_master_true_peaks"):
                    self._master_true_peaks = [0.0, 0.0]

    def _get_master_volume(self):
        if hasattr(self, "_master_lock"):
            with self._master_lock:
                return self._master_volume
        return 1.0

    def _on_master_vol_change(self, value):
        db = float(value)
        gain = db_to_linear(db)
        with self._master_lock:
            self._master_volume = gain
        self.master_vol_var.set(format_fader_db(db))
        if db <= MIN_FADER_DB:
            self.master_vol_badge.config(fg=self.THEME["text_muted"])
        elif db > 0.05:
            self.master_vol_badge.config(fg=self.THEME["boost_coral"])
        elif abs(db) <= 0.05:
            self.master_vol_badge.config(fg=self.THEME["unity_gold"])
        else:
            self.master_vol_badge.config(fg=self.THEME["text_primary"])

    def _update_session_duration(self):
        """Recompute total session duration (max length across loaded channels) and update playhead bar."""
        max_dur = 0.0
        for ch in self.channel_states:
            path = ch.get_filepath()
            if not path:
                continue
            try:
                with wave.open(path, "rb") as wf:
                    sr = max(1, wf.getframerate())
                    dur = wf.getnframes() / float(sr)
                    if dur > max_dur:
                        max_dur = dur
            except Exception:
                pass
        with self._seek_lock:
            self._session_duration_sec = max_dur
            if self._playhead_sec > max_dur:
                self._playhead_sec = 0.0
            cur_pos = self._playhead_sec
        if hasattr(self, "timeline"):
            self._ui_call(self.timeline.set_duration, max_dur)
            self._ui_call(self.timeline.set_position, cur_pos)

    def seek_to_seconds(self, target_sec):
        """Scrub / seek playhead to target_sec (works stopped, playing, or paused)."""
        with self._seek_lock:
            dur = self._session_duration_sec
            clamped = max(0.0, min(dur, float(target_sec))) if dur > 0.0 else 0.0
            self._playhead_sec = clamped
            with self._state_lock:
                is_playing = self.playing
            if is_playing:
                self._pending_seek_sec = clamped
        if hasattr(self, "timeline"):
            self.timeline.set_position(clamped)
        if not is_playing and dur > 0.0:
            self._set_status(f"Playhead: {format_timecode(clamped)}")

    def _poll_meters(self):
        """30 FPS main-thread loop: UI queue drain, VU meters, playhead sync, and Rec LED flash."""
        if self._closing:
            return

        self._drain_ui_queue()
        if self._closing:
            return

        with self._state_lock:
            is_playing = self.playing
            is_recording = self.recording

        with self._meter_lock:
            ch_peaks = list(self._channel_peaks)
            m_peaks = list(self._master_peaks)
            ch_true = list(self._channel_true_peaks)
            m_true = list(self._master_true_peaks)
            # Clear latched true peaks after reading so next tick gets fresh peaks
            self._channel_true_peaks = [0.0, 0.0, 0.0, 0.0]
            self._master_true_peaks = [0.0, 0.0]
            if not is_playing and not is_recording:
                self._channel_peaks = [0.0, 0.0, 0.0, 0.0]
                self._master_peaks = [0.0, 0.0]

        now = time.monotonic()
        for idx, meter in enumerate(self._channel_meters):
            meter.update_level(ch_peaks[idx])
        for idx, readout in enumerate(self._channel_peak_readouts):
            readout.update(ch_true[idx], now=now)
        for idx, meter in enumerate(self._master_meters):
            meter.update_level(m_peaks[idx])
        if self._master_peak_readout is not None:
            self._master_peak_readout.update(max(m_true[0], m_true[1]), now=now)

        # Sync playhead indicator, muted clip dimming & live recording waveform overlay
        if hasattr(self, "timeline"):
            snaps_aud = [ch.snapshot() for ch in self.channel_states]
            any_s = any(s.solo for s in snaps_aud)
            self.timeline.set_audible_flags(
                [(not s.mute) and (not any_s or s.solo) for s in snaps_aud]
            )
            if is_recording and hasattr(self, "_rec_wave_lock"):
                with self._rec_wave_lock:
                    r_act = self._rec_wave_active
                    r_armed = list(self._rec_wave_armed)
                    r_peaks = list(self._rec_wave_peaks)
                    r_elapsed = self._rec_wave_elapsed
                if r_act:
                    self.timeline.update_live_recording(True, r_armed, r_peaks, r_elapsed)
            elif is_playing:
                with self._seek_lock:
                    pos_sec = self._playhead_sec
                self.timeline.set_position(pos_sec, follow=True)

        # Synchronized ~2 Hz flash for armed Rec buttons (solid red while actively recording)
        self._blink_tick += 1
        blink_hi = (self._blink_tick // 14) % 2 == 0
        any_armed = False
        for idx, ch in enumerate(self.channel_states):
            armed = ch.snapshot().rec_armed
            if armed:
                any_armed = True
            if idx < len(self._rec_pad_modes):
                if armed and is_recording:
                    self._rec_pad_modes[idx]("solid")
                elif armed:
                    self._rec_pad_modes[idx]("blink_hi" if blink_hi else "blink_lo")
                else:
                    self._rec_pad_modes[idx](None)

        if is_recording:
            self._set_rec_mode("solid")
        elif any_armed:
            self._set_rec_mode("blink_hi" if blink_hi else "blink_lo")
        else:
            self._set_rec_mode("off")

        try:
            self.root.after(33, self._poll_meters)
        except tk.TclError:
            pass

    def _on_root_click(self, event):
        """Release focus from nameBox when clicking elsewhere so Spacebar shortcut keeps working."""
        if hasattr(self, "nameBox") and event.widget is not self.nameBox:
            try:
                if self.root.focus_get() == self.nameBox:
                    self.root.focus_set()
            except Exception:
                pass

    def _on_zoom_key(self, action):
        """Keyboard zoom shortcuts (+ / - / 0), ignored while typing in Name box."""
        try:
            if self.root.focus_get() == self.nameBox:
                return
        except Exception:
            pass
        if not hasattr(self, "timeline"):
            return
        if action == "in":
            self.timeline.zoom_in()
        elif action == "out":
            self.timeline.zoom_out()
        elif action == "reset":
            self.timeline.reset_zoom()

    def _on_space_key(self, _event):
        """Spacebar shortcut to Play / Pause / Resume (ignored while typing in Name box)."""
        try:
            if self.root.focus_get() == self.nameBox:
                return
        except Exception:
            pass
        with self._state_lock:
            is_playing = self.playing
            is_busy_other = self.recording or self.bouncing
        if is_busy_other:
            return
        if is_playing:
            self.press_pause()
        else:
            self.press_play()

    def _set_bounce_progress(self, fraction, label=None, bar_color="#48d18a"):
        """Update the Bounce progress bar (must run on the Tkinter main thread)."""
        frac = max(0.0, min(1.0, float(fraction)))
        try:
            width = self._bounce_canvas.winfo_width()
            if width <= 1:
                width = 204
            self._bounce_canvas.coords(self._bounce_bar_id, 0, 0, int(round(width * frac)), 12)
            self._bounce_canvas.itemconfig(self._bounce_bar_id, fill=bar_color)
            if label is not None:
                self._bounce_prog_var.set(label)
        except Exception:
            pass

    # ---------- Slider & Effect Popup Callbacks ----------

    def _on_vol_change(self, channel_idx, value, label_var, badge=None):
        db = float(value)
        self.channel_states[channel_idx].set_volume_db(db)
        label_var.set(format_fader_db(db))
        if badge is not None:
            if db <= MIN_FADER_DB:
                badge.config(fg=self.THEME["text_muted"])
            elif db > 0.05:
                badge.config(fg=self.THEME["boost_coral"])
            elif abs(db) <= 0.05:
                badge.config(fg=self.THEME["unity_gold"])
            else:
                badge.config(fg=self.THEME["text_primary"])

    def _on_pan_change(self, channel_idx, value, label_var):
        val = float(value)
        self.channel_states[channel_idx].set_pan(val)
        if abs(val - 0.5) < 0.02:
            label_var.set("Pan: C")
        elif val < 0.5:
            pct = int(round((0.5 - val) * 200))
            label_var.set(f"Pan: {pct}% L")
        else:
            pct = int(round((val - 0.5) * 200))
            label_var.set(f"Pan: {pct}% R")

    def _on_effect_toggle(self, channel_idx, effect_key, var):
        enabled = bool(var.get())
        self.channel_states[channel_idx].set_effect_enabled(effect_key, enabled)
        if enabled:
            self.open_effect_window(channel_idx, effect_key)
        else:
            self._close_effect_window(channel_idx, effect_key)

    def open_effect_window(self, channel_idx, effect_key):
        key = (channel_idx, effect_key)
        existing = self._effect_windows.get(key)
        if existing is not None and existing.is_open():
            existing.focus()
            return

        win_cls = None
        for spec in self.EFFECT_SPECS:
            if spec[0] == effect_key:
                win_cls = spec[-1]
                break
        if win_cls is None:
            return

        processor = self.channel_states[channel_idx].get_processor(effect_key)

        def on_popup_close():
            self._effect_windows.pop(key, None)

        popup = win_cls(
            parent=self.root,
            processor=processor,
            channel_num=channel_idx + 1,
            on_close=on_popup_close,
        )
        self._effect_windows[key] = popup

    def _close_effect_window(self, channel_idx, effect_key):
        key = (channel_idx, effect_key)
        popup = self._effect_windows.pop(key, None)
        if popup is not None and popup.is_open():
            popup.close()

    # ---------- File Loading ----------

    def open_channel_file(self, channel_idx):
        if self._is_busy():
            messagebox.showinfo(
                "Mixer Busy",
                "Please stop playback, recording, or bounce before loading a channel.",
                parent=self.root,
            )
            return

        filepath = askopenfilename(
            parent=self.root,
            title=f"Load WAV for Channel {channel_idx + 1}",
            filetypes=(("WAV files", "*.wav"), ("All files", "*.*")),
        )
        if not filepath:
            return

        try:
            with wave.open(filepath, "rb") as wf:
                channels = wf.getnchannels()
                samplefreq = wf.getframerate()
                sampwidth = wf.getsampwidth()
                bitdepth = sampwidth * 8
                if channels <= 0 or samplefreq <= 0:
                    raise ValueError(f"Invalid WAV header (channels={channels}, rate={samplefreq})")
                if sampwidth not in (1, 2, 3, 4):
                    raise ValueError(f"Unsupported WAV bit depth: {bitdepth}-bit")
                length = int(wf.getnframes() / samplefreq)
        except Exception as exc:
            messagebox.showerror("Error loading WAV", f"Could not open file:\n{exc}", parent=self.root)
            return

        self.channel_states[channel_idx].set_filepath(filepath)
        short_name = os.path.basename(filepath)
        if len(short_name) > 17:
            short_name = short_name[:14] + "..."
        self._ch_widgets[channel_idx]["file_var"].set(short_name)
        self._ch_widgets[channel_idx]["meta_var"].set(
            format_audio_meta(samplefreq, bitdepth, channels)
        )

        mins, secs = divmod(length, 60)
        print(
            f"Channel {channel_idx + 1} audio loaded\n"
            f"  File route: {filepath}\n"
            f"  Number of channels: {channels}\n"
            f"  Sample rate: {samplefreq} Hz\n"
            f"  Bit Depth: {bitdepth}-bit\n"
            f"  Duration: {mins}:{secs:02d}"
        )
        if hasattr(self, "timeline"):
            self.timeline.clear_track(channel_idx)
        self._update_session_duration()
        self._load_channel_waveform_async(channel_idx, filepath)
        self._set_status(f"Loaded Ch {channel_idx + 1}: {os.path.basename(filepath)}")

    def _load_channel_waveform_async(self, channel_idx, filepath):
        """Extract waveform envelope on a background thread and paint it on the Arrange view."""
        def _worker():
            try:
                peaks, dur = extract_waveform_peaks(filepath, num_bins=2400)
            except Exception as exc:
                print(f"Waveform extraction skipped: {exc}")
                return
            # Only apply if channel still points to the same file
            if self.channel_states[channel_idx].get_filepath() != filepath:
                return
            if hasattr(self, "timeline"):
                self._ui_call(
                    self.timeline.set_track_waveform,
                    channel_idx,
                    peaks,
                    dur,
                    os.path.basename(filepath),
                )

        threading.Thread(target=_worker, name=f"WaveformCh{channel_idx + 1}", daemon=True).start()

    def clear_channel_file(self, channel_idx):
        """Clear the loaded WAV file from a channel without needing to load another."""
        if self._is_busy():
            messagebox.showinfo(
                "Mixer Busy",
                "Please stop playback, recording, or bounce before clearing a channel.",
                parent=self.root,
            )
            return

        if not self.channel_states[channel_idx].get_filepath():
            return

        self.channel_states[channel_idx].set_filepath(None)
        self.channel_states[channel_idx].reset_effects()
        self._ch_widgets[channel_idx]["file_var"].set("(click to load)")
        self._ch_widgets[channel_idx]["meta_var"].set("—")
        if hasattr(self, "timeline"):
            self.timeline.clear_track(channel_idx)
        self._update_session_duration()
        self._set_status(f"Cleared Channel {channel_idx + 1}")

    # ---------- Playback (Multithreaded) ----------

    def _is_busy(self):
        with self._state_lock:
            return self.playing or self.recording or self.bouncing

    def _require_pyaudio(self, action_name):
        if pyaudio is None:
            detail = f"\n\nImport error: {_PYAUDIO_IMPORT_ERROR}" if _PYAUDIO_IMPORT_ERROR else ""
            messagebox.showerror(
                "PyAudio Not Installed",
                f"Cannot start {action_name} because 'pyaudio' could not be loaded in this Python environment.{detail}\n\n"
                "Install PortAudio and PyAudio, e.g.:\n"
                "  brew install portaudio\n"
                "  pip install pyaudio\n\n"
                "(Note: Offline Bounce still works without PyAudio.)",
                parent=self.root,
            )
            return False
        return True

    @staticmethod
    def _audible_delay_indices(snapshots, master_gain=1.0):
        """Return indices of loaded, audible channels that have Delay enabled."""
        if float(master_gain) <= 0.0:
            return []
        any_solo = any(s.solo for s in snapshots)
        return [
            idx
            for idx, s in enumerate(snapshots)
            if s.filepath
            and s.delay_enabled
            and (not s.mute)
            and (not any_solo or s.solo)
            and s.volume > 0.0
        ]

    def press_play(self):
        if not self._require_pyaudio("playback"):
            return

        resumed = False
        busy = False
        already_playing = False
        with self._state_lock:
            if self.playing:
                already_playing = True
                if self.paused:
                    # Resume seamlessly from paused position
                    self.paused = False
                    self._pause_event.set()
                    resumed = True
            elif self.recording or self.bouncing:
                busy = True

        if already_playing:
            if resumed:
                self._ui_call(self._set_pause_lit, False)
                self._ui_call(self._set_play_lit, True)
            return
        if busy:
            messagebox.showinfo(
                "Mixer Busy",
                "Please stop recording or wait for bounce to finish before playing.",
                parent=self.root,
            )
            return

        loaded = [ch.get_filepath() for ch in self.channel_states if ch.get_filepath()]
        if not loaded:
            messagebox.showinfo(
                "No Audio Loaded",
                "There's no audio file opened to play. Please load at least one .wav file.",
                parent=self.root,
            )
            return

        with self._state_lock:
            if self.playing or self.recording or self.bouncing:
                return
            self.playing = True
            self.paused = False
            self._stop_event.clear()
            self._pause_event.set()

        for ch in self.channel_states:
            ch.reset_effects()

        self._set_status("Playing...")
        self._ui_call(self._set_pause_lit, False)
        self._ui_call(self._set_play_lit, True)
        t = threading.Thread(target=self._play_worker, name="MixerPlayThread", daemon=True)
        self._active_thread = t
        t.start()

    def press_pause(self):
        """Toggle pause / resume during playback without losing track position."""
        with self._state_lock:
            if not self.playing:
                return
            if self.paused:
                self.paused = False
                self._pause_event.set()
                resuming = True
            else:
                self.paused = True
                self._pause_event.clear()
                resuming = False

        if resuming:
            self._ui_call(self._set_pause_lit, False)
            self._ui_call(self._set_play_lit, True)
        else:
            self._reset_meter_peaks()
            self._ui_call(self._set_play_lit, False)
            self._ui_call(self._set_pause_lit, True)

    def _play_worker(self):
        pa = None
        stream = None
        wav_handles = [None] * 4
        readers = [None] * 4

        try:
            print("Now playing...")
            session_rate = None

            for idx, ch in enumerate(self.channel_states):
                path = ch.get_filepath()
                if path:
                    wf = wave.open(path, "rb")
                    wav_handles[idx] = wf
                    fr = wf.getframerate()
                    if session_rate is None and fr > 0:
                        session_rate = fr

            if session_rate is None:
                session_rate = self.rate

            pa = pyaudio.PyAudio()
            try:
                stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=2,
                    rate=session_rate,
                    output=True,
                    frames_per_buffer=self.buffer,
                )
            except OSError:
                dev_rate = int(pa.get_default_output_device_info().get("defaultSampleRate", 44100))
                session_rate = dev_rate
                stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=2,
                    rate=session_rate,
                    output=True,
                    frames_per_buffer=self.buffer,
                )

            for idx, wf in enumerate(wav_handles):
                if wf is not None:
                    readers[idx] = StreamingWavReader(wf, session_rate)

            def _apply_seek(target_sec):
                nonlocal frames_played, in_tail, tail_frames_remaining, tail_frames_emitted
                for rdr in readers:
                    if rdr is not None:
                        rdr.seek(target_sec)
                for ch_s in self.channel_states:
                    ch_s.reset_effects()
                frames_played = int(round(target_sec * session_rate))
                in_tail = False
                tail_frames_remaining = 0
                tail_frames_emitted = 0

            frames_played = 0
            last_ui_update = 0.0
            tail_frames_remaining = 0
            tail_frames_emitted = 0
            min_tail_frames = 0
            in_tail = False
            chunk_levels = [0.0, 0.0, 0.0, 0.0]
            master_levels = [0.0, 0.0]
            chunk_true_peaks = [0.0, 0.0, 0.0, 0.0]
            master_true_peaks = [0.0, 0.0]

            # Start playback from current scrubbed playhead position (if > 0 and < duration)
            if hasattr(self, "_seek_lock"):
                with self._seek_lock:
                    start_sec = self._playhead_sec
                    dur_sec = self._session_duration_sec
                    self._pending_seek_sec = None
                if start_sec > 0.01 and (dur_sec <= 0.0 or start_sec < dur_sec - 0.05):
                    _apply_seek(start_sec)
                else:
                    with self._seek_lock:
                        self._playhead_sec = 0.0

            while not self._stop_event.is_set():
                # Check for live scrub / seek request
                if hasattr(self, "_seek_lock"):
                    with self._seek_lock:
                        req_seek = self._pending_seek_sec
                        self._pending_seek_sec = None
                    if req_seek is not None:
                        _apply_seek(req_seek)

                # Handle Pause / Resume cleanly at chunk boundary with zero CPU spin
                if hasattr(self, "_pause_event") and not self._pause_event.is_set():
                    elapsed = int(frames_played / float(session_rate))
                    self._set_status(f"Paused {format_timecode(elapsed)}")
                    self._reset_meter_peaks()
                    try:
                        stream.stop_stream()
                    except Exception:
                        pass
                    while not self._pause_event.is_set() and not self._stop_event.is_set():
                        # Allow scrubbing while paused so resume starts at the new spot
                        if hasattr(self, "_seek_lock"):
                            with self._seek_lock:
                                req_seek = self._pending_seek_sec
                                self._pending_seek_sec = None
                            if req_seek is not None:
                                _apply_seek(req_seek)
                                self._set_status(f"Paused {format_timecode(req_seek)}")
                        self._pause_event.wait(timeout=0.05)
                    if self._stop_event.is_set():
                        break
                    try:
                        stream.start_stream()
                    except Exception:
                        pass
                    elapsed = int(frames_played / float(session_rate))
                    self._set_status(f"Playing {format_timecode(elapsed)}")
                    last_ui_update = time.monotonic()

                chunks = [[] for _ in range(4)]
                any_data = False

                for idx, rdr in enumerate(readers):
                    if rdr is None:
                        continue
                    mono = rdr.read(self.buffer)
                    if not mono:
                        continue
                    any_data = True
                    chunks[idx] = mono

                snapshots = [ch.snapshot() for ch in self.channel_states]
                master_gain = self._get_master_volume()

                if not any_data:
                    if not in_tail:
                        delay_indices = self._audible_delay_indices(snapshots, master_gain)
                        if not delay_indices:
                            print("Audio ended")
                            break
                        in_tail = True
                        max_delay_ms = max(
                            snapshots[i].delay.get_params()[0] for i in delay_indices
                        )
                        min_tail_frames = int(max_delay_ms * session_rate / 1000.0) + self.buffer
                        tail_frames_remaining = max(int(session_rate * 2.5), min_tail_frames * 2)
                    if tail_frames_remaining <= 0:
                        print("Audio ended")
                        break
                    tail_chunk_len = min(self.buffer, tail_frames_remaining)
                    tail_frames_remaining -= tail_chunk_len
                    tail_frames_emitted += tail_chunk_len
                    delay_indices = self._audible_delay_indices(snapshots, master_gain)
                    if not delay_indices:
                        print("Audio ended")
                        break
                    for idx in delay_indices:
                        chunks[idx] = [0.0] * tail_chunk_len

                pcm_bytes = mix_stereo_chunk(
                    chunks,
                    snapshots,
                    session_rate,
                    out_levels=chunk_levels,
                    master_gain=master_gain,
                    out_master_levels=master_levels,
                    out_peaks=chunk_true_peaks,
                    out_master_peaks=master_true_peaks,
                )
                self._set_meter_peaks(
                    chunk_levels, master_levels, chunk_true_peaks, master_true_peaks
                )

                if in_tail and tail_frames_emitted > min_tail_frames and pcm_bytes:
                    # Stop tail early once past one full delay cycle and virtually silent
                    if _is_pcm_silent(pcm_bytes, 4):
                        print("Audio ended")
                        break

                if pcm_bytes:
                    stream.write(pcm_bytes)
                    frames_played += len(pcm_bytes) // 4  # 2 channels * 2 bytes
                    if hasattr(self, "_seek_lock"):
                        with self._seek_lock:
                            if self._pending_seek_sec is None:
                                self._playhead_sec = frames_played / float(session_rate)

                now = time.monotonic()
                if now - last_ui_update >= 0.25:
                    elapsed = int(frames_played / float(session_rate))
                    self._set_status(f"Playing {format_timecode(elapsed)}")
                    last_ui_update = now

            if self._stop_event.is_set():
                self._set_status("Stopped")
            else:
                self._set_status("Playback finished")
                if hasattr(self, "_seek_lock"):
                    with self._seek_lock:
                        self._playhead_sec = 0.0
                if hasattr(self, "timeline"):
                    self._ui_call(self.timeline.set_position, 0.0)

        except Exception as exc:
            print(f"Playback error: {exc}")
            self._set_status("Playback error")
            self._ui_call(
                messagebox.showerror, "Playback Error", f"An error occurred during playback:\n{exc}", parent=self.root
            )
        finally:
            self._reset_meter_peaks()
            for wf in wav_handles:
                if wf is not None:
                    try:
                        wf.close()
                    except Exception:
                        pass
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            if pa is not None:
                try:
                    pa.terminate()
                except Exception:
                    pass
            with self._state_lock:
                self.playing = False
                self.paused = False
            if hasattr(self, "_pause_event"):
                self._pause_event.set()
            if hasattr(self, "_set_play_lit"):
                self._ui_call(self._set_play_lit, False)
            if hasattr(self, "_set_pause_lit"):
                self._ui_call(self._set_pause_lit, False)

    # ---------- Recording (Multithreaded, with Overdub Support) ----------

    def press_rec(self):
        if not self._require_pyaudio("recording"):
            return

        stopping = False
        busy = False
        with self._state_lock:
            if self.recording:
                # Clicking Rec again stops recording
                self._stop_event.set()
                stopping = True
            elif self.playing or self.bouncing:
                busy = True

        if stopping:
            self._set_status("Stopping recording...")
            return
        if busy:
            messagebox.showinfo(
                "Mixer Busy",
                "Please stop playback or wait for bounce to finish before recording.",
                parent=self.root,
            )
            return

        armed_indices = [
            idx for idx, ch in enumerate(self.channel_states) if ch.snapshot().rec_armed
        ]
        if not armed_indices:
            messagebox.showinfo(
                "No Channel Armed",
                "Please arm at least one channel by enabling its '● Rec' button before recording.",
                parent=self.root,
            )
            return

        raw_name = self.nameEntry.get().strip()
        if not raw_name:
            base_name = f"recording_{time.strftime('%Y%m%d_%H%M%S')}"
        else:
            base_name = sanitize_filename_stem(raw_name)
            if not base_name:
                messagebox.showerror(
                    "Invalid Recording Name",
                    "Please enter a valid filename (avoid slashes or special path characters).",
                    parent=self.root,
                )
                return

        with self._state_lock:
            if self.playing or self.recording or self.bouncing:
                return
            self.recording = True
            self._stop_event.clear()

        for ch in self.channel_states:
            ch.reset_effects()

        self._set_status("Recording 00:00")
        self._ui_call(self._set_rec_lit, True)
        t = threading.Thread(
            target=self._rec_worker,
            args=(armed_indices, base_name),
            name="MixerRecThread",
            daemon=True,
        )
        self._active_thread = t
        t.start()

    def _rec_worker(self, armed_indices, base_name):
        pa = None
        in_stream = None
        out_stream = None
        wav_handles = [None] * 4
        readers = [None] * 4
        recorded_frames = []
        armed_set = set(armed_indices)

        try:
            print(f"Recording started on channel(s): {[i + 1 for i in armed_indices]}")
            session_rate = self.rate
            pa = pyaudio.PyAudio()
            try:
                in_stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=session_rate,
                    input=True,
                    frames_per_buffer=self.buffer,
                )
            except OSError:
                dev_rate = int(pa.get_default_input_device_info().get("defaultSampleRate", 44100))
                session_rate = dev_rate
                in_stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=session_rate,
                    input=True,
                    frames_per_buffer=self.buffer,
                )

            # Open non-armed loaded channels for synchronized overdub playback
            has_overdub = False
            for idx, ch in enumerate(self.channel_states):
                if idx in armed_set:
                    continue
                path = ch.get_filepath()
                if path:
                    wf = wave.open(path, "rb")
                    wav_handles[idx] = wf
                    readers[idx] = StreamingWavReader(wf, session_rate)
                    has_overdub = True

            if has_overdub:
                try:
                    out_stream = pa.open(
                        format=pyaudio.paInt16,
                        channels=2,
                        rate=session_rate,
                        output=True,
                        frames_per_buffer=self.buffer,
                    )
                except Exception as exc:
                    print(f"Overdub monitor unavailable, recording without playback: {exc}")
                    out_stream = None

            frames_recorded = 0
            last_ui_update = 0.0
            overdub_in_tail = False
            overdub_tail_rem = 0
            overdub_tail_emitted = 0
            overdub_min_tail = 0
            chunk_levels = [0.0, 0.0, 0.0, 0.0]
            master_levels = [0.0, 0.0]
            chunk_true_peaks = [0.0, 0.0, 0.0, 0.0]
            master_true_peaks = [0.0, 0.0]

            if hasattr(self, "_rec_wave_lock"):
                with self._rec_wave_lock:
                    self._rec_wave_armed = list(armed_indices)
                    self._rec_wave_peaks = []
                    self._rec_wave_stride = 1
                    self._rec_wave_accum = 0.0
                    self._rec_wave_count = 0
                    self._rec_wave_elapsed = 0.0
                    self._rec_wave_active = True

            while not self._stop_event.is_set():
                raw_in = in_stream.read(self.buffer, exception_on_overflow=False)
                recorded_frames.append(raw_in)
                frames_recorded += len(raw_in) // 2

                # Compute live VU RMS + true sample peak for armed microphone input channels
                in_pcm = array.array("h")
                in_pcm.frombytes(raw_in)
                if in_pcm:
                    in_sq = 0
                    in_max = 0
                    for v in in_pcm:
                        in_sq += v * v
                        av = v if v >= 0 else -v
                        if av > in_max:
                            in_max = av
                    in_vu = math.sqrt(in_sq / float(len(in_pcm))) * (1.4142135623730951 / 32768.0)
                    in_pk = in_max / 32768.0
                else:
                    in_vu = 0.0
                    in_pk = 0.0

                for i in range(4):
                    chunk_levels[i] = 0.0
                    chunk_true_peaks[i] = 0.0
                master_levels[0] = 0.0
                master_levels[1] = 0.0
                master_true_peaks[0] = 0.0
                master_true_peaks[1] = 0.0

                # Play back non-armed tracks in sync if available
                if out_stream is not None:
                    chunks = [[] for _ in range(4)]
                    any_data = False
                    for idx, rdr in enumerate(readers):
                        if rdr is None:
                            continue
                        mono = rdr.read(self.buffer)
                        if not mono:
                            continue
                        any_data = True
                        chunks[idx] = mono

                    raw_snaps = [ch.snapshot() for ch in self.channel_states]
                    # Mask armed channels so Solo on an armed track cannot silence backing tracks
                    snapshots = [
                        s._replace(solo=False, mute=True, filepath=None)
                        if idx in armed_set
                        else s
                        for idx, s in enumerate(raw_snaps)
                    ]
                    master_gain = self._get_master_volume()

                    close_overdub = False
                    if not any_data:
                        if not overdub_in_tail:
                            delay_indices = self._audible_delay_indices(snapshots, master_gain)
                            if not delay_indices:
                                close_overdub = True
                            else:
                                overdub_in_tail = True
                                max_delay_ms = max(
                                    snapshots[i].delay.get_params()[0] for i in delay_indices
                                )
                                overdub_min_tail = (
                                    int(max_delay_ms * session_rate / 1000.0) + self.buffer
                                )
                                overdub_tail_rem = max(
                                    int(session_rate * 2.5), overdub_min_tail * 2
                                )
                        if not close_overdub:
                            if overdub_tail_rem <= 0:
                                close_overdub = True
                            else:
                                delay_indices = self._audible_delay_indices(snapshots, master_gain)
                                if not delay_indices:
                                    close_overdub = True
                                else:
                                    tail_len = min(self.buffer, overdub_tail_rem)
                                    overdub_tail_rem -= tail_len
                                    overdub_tail_emitted += tail_len
                                    for idx in delay_indices:
                                        chunks[idx] = [0.0] * tail_len

                    if not close_overdub:
                        pcm_bytes = mix_stereo_chunk(
                            chunks,
                            snapshots,
                            session_rate,
                            out_levels=chunk_levels,
                            master_gain=master_gain,
                            out_master_levels=master_levels,
                            out_peaks=chunk_true_peaks,
                            out_master_peaks=master_true_peaks,
                        )
                        if overdub_in_tail and overdub_tail_emitted > overdub_min_tail and pcm_bytes:
                            if _is_pcm_silent(pcm_bytes, 4):
                                close_overdub = True
                        if not close_overdub and pcm_bytes:
                            out_stream.write(pcm_bytes)

                    if close_overdub:
                        try:
                            out_stream.stop_stream()
                            out_stream.close()
                        except Exception:
                            pass
                        out_stream = None

                for idx in armed_indices:
                    chunk_levels[idx] = in_vu
                    chunk_true_peaks[idx] = in_pk
                self._set_meter_peaks(
                    chunk_levels, master_levels, chunk_true_peaks, master_true_peaks
                )

                if hasattr(self, "_rec_wave_lock"):
                    with self._rec_wave_lock:
                        # Uniform stride accumulation keeps live waveform linearly aligned with ruler
                        if in_pk > self._rec_wave_accum:
                            self._rec_wave_accum = in_pk
                        self._rec_wave_count += 1
                        if self._rec_wave_count >= self._rec_wave_stride:
                            self._rec_wave_peaks.append(self._rec_wave_accum)
                            self._rec_wave_accum = 0.0
                            self._rec_wave_count = 0
                            if len(self._rec_wave_peaks) >= 1200:
                                n_p = len(self._rec_wave_peaks)
                                compacted = [
                                    max(self._rec_wave_peaks[k], self._rec_wave_peaks[k + 1])
                                    for k in range(0, n_p - 1, 2)
                                ]
                                if n_p % 2 == 1:
                                    self._rec_wave_accum = self._rec_wave_peaks[-1]
                                    self._rec_wave_count = self._rec_wave_stride
                                self._rec_wave_peaks = compacted
                                self._rec_wave_stride *= 2
                        self._rec_wave_elapsed = frames_recorded / float(session_rate)

                now = time.monotonic()
                if now - last_ui_update >= 0.25:
                    elapsed = int(frames_recorded / float(session_rate))
                    mins, secs = divmod(elapsed, 60)
                    self._set_status(f"Recording {mins:02d}:{secs:02d}")
                    last_ui_update = now

            # Save recorded WAV file(s) and load them into armed channels
            raw_all = b"".join(recorded_frames)
            if not raw_all:
                self._set_status("Recording cancelled")
            else:
                saved_files = []
                out_targets = unique_recording_paths(BASE_DIR, base_name, armed_indices)
                for idx, (filename, out_path) in zip(armed_indices, out_targets):
                    with wave.open(out_path, "wb") as wf_out:
                        wf_out.setnchannels(1)
                        wf_out.setsampwidth(2)
                        wf_out.setframerate(session_rate)
                        wf_out.writeframes(raw_all)
                    self.channel_states[idx].set_filepath(out_path)
                    short_name = filename if len(filename) <= 17 else filename[:14] + "..."
                    self._ui_call(self._ch_widgets[idx]["file_var"].set, short_name)
                    if "meta_var" in self._ch_widgets[idx]:
                        self._ui_call(
                            self._ch_widgets[idx]["meta_var"].set,
                            format_audio_meta(session_rate, 16, 1),
                        )
                    saved_files.append(filename)
                    print(f"Saved recording to {out_path}")

                if hasattr(self, "_rec_wave_lock"):
                    with self._rec_wave_lock:
                        self._rec_wave_active = False
                if hasattr(self, "timeline"):
                    self._ui_call(self.timeline.update_live_recording, False)
                if hasattr(self, "_seek_lock"):
                    self._update_session_duration()
                for idx in armed_indices:
                    path = self.channel_states[idx].get_filepath()
                    if path:
                        self._load_channel_waveform_async(idx, path)
                self._set_status(f"Saved: {', '.join(saved_files)}")

        except Exception as exc:
            print(f"Recording error: {exc}")
            self._set_status("Recording error")
            self._ui_call(
                messagebox.showerror, "Recording Error", f"An error occurred while recording:\n{exc}", parent=self.root
            )
        finally:
            if hasattr(self, "_rec_wave_lock"):
                with self._rec_wave_lock:
                    self._rec_wave_active = False
            if hasattr(self, "timeline"):
                self._ui_call(self.timeline.update_live_recording, False)
            self._reset_meter_peaks()
            for wf in wav_handles:
                if wf is not None:
                    try:
                        wf.close()
                    except Exception:
                        pass
            if in_stream is not None:
                try:
                    in_stream.stop_stream()
                    in_stream.close()
                except Exception:
                    pass
            if out_stream is not None:
                try:
                    out_stream.stop_stream()
                    out_stream.close()
                except Exception:
                    pass
            if pa is not None:
                try:
                    pa.terminate()
                except Exception:
                    pass
            with self._state_lock:
                self.recording = False
            if hasattr(self, "_set_rec_lit"):
                self._ui_call(self._set_rec_lit, False)

    # Backward-compatible alias
    rec = press_rec

    # ---------- Stop ----------

    def stop(self):
        with self._state_lock:
            active = self.playing or self.recording or self.bouncing
            self.paused = False
        if active:
            self._stop_event.set()
            if hasattr(self, "_pause_event"):
                self._pause_event.set()
            self._reset_meter_peaks()
            if hasattr(self, "_set_pause_lit"):
                self._ui_call(self._set_pause_lit, False)
            print("Stopped")
            self._set_status("Stopping...")
        # Rewind playhead to 00:00 on Stop
        if hasattr(self, "_seek_lock"):
            with self._seek_lock:
                self._playhead_sec = 0.0
                self._pending_seek_sec = None
        if hasattr(self, "timeline"):
            self._ui_call(self.timeline.set_position, 0.0)

    # ---------- Offline Bounce / Export (Multithreaded) ----------

    def bounce(self):
        loaded = [ch.get_filepath() for ch in self.channel_states if ch.get_filepath()]
        if not loaded:
            messagebox.showinfo(
                "No Audio Loaded",
                "Load at least one .wav file before bouncing.",
                parent=self.root,
            )
            return

        if self._is_busy():
            messagebox.showinfo(
                "Mixer Busy",
                "Please stop playback/recording before bouncing.",
                parent=self.root,
            )
            return

        raw_name = self.nameEntry.get().strip()
        if raw_name:
            stem = sanitize_filename_stem(raw_name)
            if not stem:
                messagebox.showerror(
                    "Invalid Mix Name",
                    "Please enter a valid filename (avoid slashes or special path characters).",
                    parent=self.root,
                )
                return
            out_path = os.path.join(BASE_DIR, f"{stem}.wav")
            if os.path.exists(out_path):
                overwrite = messagebox.askyesno(
                    "Overwrite File?",
                    f"'{os.path.basename(out_path)}' already exists.\nDo you want to overwrite it?",
                    parent=self.root,
                )
                if not overwrite:
                    return
        else:
            out_path = asksaveasfilename(
                parent=self.root,
                title="Save Bounced Mix",
                initialdir=BASE_DIR,
                defaultextension=".wav",
                filetypes=(("WAV files", "*.wav"),),
            )
            if not out_path:
                return

        busy_now = False
        with self._state_lock:
            if self.playing or self.recording or self.bouncing:
                busy_now = True
            else:
                self.bouncing = True
                self._stop_event.clear()

        if busy_now:
            messagebox.showinfo(
                "Mixer Busy",
                "Please stop playback/recording before bouncing.",
                parent=self.root,
            )
            return

        self._set_status("Bouncing... 0%")
        if hasattr(self, "_bounce_canvas"):
            self._ui_call(self._set_bounce_progress, 0.0, "Bouncing... 0%", "#48d18a")
        t = threading.Thread(
            target=self._bounce_worker,
            args=(out_path,),
            name="MixerBounceThread",
            daemon=True,
        )
        self._active_thread = t
        t.start()

    def _bounce_worker(self, out_path):
        wav_handles = [None] * 4
        readers = [None] * 4
        use_c = _dsp is not None and _dsp.AVAILABLE
        chunk_size = 65536 if use_c else 4096
        tail_step = 4096
        cancelled = False

        try:
            print(f"Bouncing audio to {out_path}...")
            session_rate = None
            snapshots = [ch.snapshot() for ch in self.channel_states]
            offline_procs = [ch.clone_processors_for_offline() for ch in self.channel_states]
            frozen_master_gain = self._get_master_volume()

            for idx, snap in enumerate(snapshots):
                if snap.filepath:
                    wf = wave.open(snap.filepath, "rb")
                    wav_handles[idx] = wf
                    fr = wf.getframerate()
                    if session_rate is None and fr > 0:
                        session_rate = fr

            if session_rate is None:
                session_rate = self.rate

            for idx, wf in enumerate(wav_handles):
                if wf is not None:
                    readers[idx] = StreamingWavReader(wf, session_rate)

            # Estimate total output frames for progress bar accuracy
            total_frames = 0
            for idx, wf in enumerate(wav_handles):
                if wf is None:
                    continue
                fr = max(1, wf.getframerate())
                scaled = int(round(wf.getnframes() * (session_rate / float(fr))))
                if scaled > total_frames:
                    total_frames = scaled
            delay_indices_init = self._audible_delay_indices(snapshots, frozen_master_gain)
            if delay_indices_init:
                est_delay_ms = max(
                    offline_procs[i][3].get_params()[0] for i in delay_indices_init
                )
                total_frames += int(est_delay_ms * session_rate / 1000.0)
            total_frames = max(1, total_frames)

            frames_written = 0
            last_prog_time = 0.0
            last_prog_pct = -1
            tail_frames_remaining = 0
            tail_frames_emitted = 0
            min_tail_frames = 0
            in_tail = False

            fx_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="BounceFX") if use_c else None
            try:
                with wave.open(out_path, "wb") as bounce_wf:
                    bounce_wf.setnchannels(2)
                    bounce_wf.setsampwidth(self.bitdepth // 8)
                    bounce_wf.setframerate(int(session_rate))

                    while True:
                        if self._stop_event.is_set():
                            cancelled = True
                            break

                        chunks = [[] for _ in range(4)]
                        any_data = False

                        for idx, rdr in enumerate(readers):
                            if rdr is None:
                                continue
                            mono = rdr.read(chunk_size)
                            if not mono:
                                continue
                            any_data = True
                            chunks[idx] = mono

                        if not any_data:
                            if not in_tail:
                                if not delay_indices_init:
                                    break
                                in_tail = True
                                max_delay_ms = max(
                                    offline_procs[i][3].get_params()[0] for i in delay_indices_init
                                )
                                min_tail_frames = int(max_delay_ms * session_rate / 1000.0) + tail_step
                                tail_frames_remaining = max(int(session_rate * 2.5), min_tail_frames * 2)
                            if tail_frames_remaining <= 0:
                                break
                            tail_chunk_len = min(tail_step, tail_frames_remaining)
                            tail_frames_remaining -= tail_chunk_len
                            tail_frames_emitted += tail_chunk_len
                            zero_chunk = _dsp.alloc_f64(tail_chunk_len) if use_c else [0.0] * tail_chunk_len
                            for idx in delay_indices_init:
                                chunks[idx] = zero_chunk

                        pcm_bytes = mix_stereo_chunk(
                            chunks,
                            snapshots,
                            session_rate,
                            offline_processors=offline_procs,
                            master_gain=frozen_master_gain,
                            fx_executor=fx_pool,
                        )
                        if in_tail and tail_frames_emitted > min_tail_frames and pcm_bytes:
                            if _is_pcm_silent(pcm_bytes, 4):
                                break
                        if pcm_bytes:
                            bounce_wf.writeframes(pcm_bytes)
                            frames_written += len(pcm_bytes) // 4

                            now = time.monotonic()
                            frac = min(0.99, frames_written / float(total_frames))
                            pct = int(frac * 100)
                            if (pct - last_prog_pct >= 2 or now - last_prog_time >= 0.05) and pct != last_prog_pct:
                                last_prog_pct = pct
                                last_prog_time = now
                                self._set_status(f"Bouncing... {pct}%")
                                if hasattr(self, "_bounce_canvas"):
                                    self._ui_call(
                                        self._set_bounce_progress,
                                        frac,
                                        f"Bouncing... {pct}%",
                                        "#48d18a",
                                    )
            finally:
                if fx_pool is not None:
                    fx_pool.shutdown(wait=True)

            if cancelled:
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except OSError:
                    pass
                print("Bounce cancelled")
                if hasattr(self, "_bounce_canvas"):
                    self._ui_call(self._set_bounce_progress, 0.0, "Bounce cancelled", "#f5c542")
                self._set_status("Bounce cancelled")
            else:
                print("Bounce completed successfully")
                if hasattr(self, "_bounce_canvas"):
                    self._ui_call(self._set_bounce_progress, 1.0, "Bounce complete (100%)", "#7ef2b8")
                self._set_status(f"Bounced: {os.path.basename(out_path)}")
                self._ui_call(
                    messagebox.showinfo,
                    "Bounce Complete",
                    f"Mix successfully exported to:\n{out_path}",
                    parent=self.root,
                )

        except Exception as exc:
            print(f"Bounce error: {exc}")
            if hasattr(self, "_bounce_canvas"):
                self._ui_call(self._set_bounce_progress, 0.0, "Bounce failed", "#fc8181")
            self._set_status("Bounce error")
            self._ui_call(
                messagebox.showerror, "Bounce Error", f"Could not export mix:\n{exc}", parent=self.root
            )
        finally:
            self._reset_meter_peaks()
            for wf in wav_handles:
                if wf is not None:
                    try:
                        wf.close()
                    except Exception:
                        pass
            with self._state_lock:
                self.bouncing = False

    # ---------- Graceful Shutdown ----------

    def on_close(self):
        self._closing = True
        self._stop_event.set()
        if hasattr(self, "_pause_event"):
            self._pause_event.set()

        # Close all effect popups cleanly
        for popup in list(self._effect_windows.values()):
            try:
                popup.close()
            except Exception:
                pass
        self._effect_windows.clear()

        # Wait briefly for any background audio thread to clean up PyAudio/WAV handles
        t = self._active_thread
        if t is not None and t.is_alive():
            t.join(timeout=1.5)

        try:
            self.root.destroy()
        except tk.TclError:
            pass


def Main():
    Window()
    return 0


if __name__ == "__main__":
    Main()
