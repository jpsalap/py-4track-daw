"""Mini-DAW: 4-Channel Python Audio Mixer, Multitrack Recorder & Waveform Arrange View (PySide6)."""

import array
import ctypes
import math
import os
import random
import re
import sys
import threading
import time
import wave
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor

try:
    import dsp_accel as _dsp
except Exception:
    _dsp = None

from PySide6.QtCore import QEvent, QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

try:
    import pyaudio

    _PYAUDIO_IMPORT_ERROR = None
except Exception as _exc:
    pyaudio = None
    _PYAUDIO_IMPORT_ERROR = _exc

from Chorus import ChorusProcessor, ChorusWindow
from Compressor import CompressorProcessor, CompressorWindow
from Delay import DelayProcessor, DelayWindow
from Tremolo import TremoloProcessor, TremoloWindow
from ui_widgets import (
    MAX_FADER_DB,
    MIN_FADER_DB,
    ChannelFileWell,
    ChannelMeter,
    FaderBadge,
    FaderWithScale,
    InsertSlotWidget,
    PanSlider,
    PeakReadout,
    TogglePadButton,
    TransportButton,
    WaveformTimeline,
    format_peak_db,
    format_timecode,
)

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.join(os.path.expanduser("~"), "Music", "Mini-DAW")
    os.makedirs(BASE_DIR, exist_ok=True)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))


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


def sanitize_filename(name):
    """Strip directory traversal and invalid filename characters from user-typed recording/mix names."""
    cleaned = os.path.basename(str(name).strip())
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", cleaned)
    return cleaned.strip(" .")


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


def extract_waveform_peaks(filepath, num_bins=2400, start_sec=None, end_sec=None):
    """
    Fast stdlib WAV envelope extractor for pro DAW waveform display.
    Supports both full-file overview extraction and high-resolution zoomed window extraction.
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

        if start_sec is None:
            win_start_f = 0
        else:
            win_start_f = max(0, min(nframes - 1, int(math.floor(float(start_sec) * fr))))

        if end_sec is None:
            win_end_f = nframes
        else:
            win_end_f = max(win_start_f + 1, min(nframes, int(math.ceil(float(end_sec) * fr))))

        span_frames = win_end_f - win_start_f
        if span_frames <= 0:
            return [], duration_sec

        bins = min(int(num_bins), span_frames)
        frames_per_bin = span_frames / float(bins)
        # Read every sample when zoomed in; cap per-bin read for fast full-file overview
        sample_cap = 256 if start_sec is None else 512
        envelope = [(0.0, 0.0, 0.0)] * bins

        if frames_per_bin <= sample_cap:
            # Contiguous read: seek once and decode chunks sequentially (very fast when zoomed in!)
            wf.setpos(win_start_f)
            prev_f = win_start_f
            for b in range(bins):
                end_f = win_start_f + int((b + 1) * frames_per_bin)
                count = max(1, end_f - prev_f)
                prev_f += count
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
        else:
            for b in range(bins):
                start_f = win_start_f + int(b * frames_per_bin)
                end_f = win_start_f + int((b + 1) * frames_per_bin)
                count = max(1, min(sample_cap, end_f - start_f))
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
    out_bit_depth=16,
    dither_tpdf=False,
    dither_rng=None,
):
    """
    Process per-channel mono float chunks through enabled effects, polarity inversion,
    mute/solo logic, volume faders, constant-power stereo panning, master gain, and sum
    to 16-bit stereo PCM bytes.
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
        is_audible = (not snap.mute) and (not any_solo or snap.solo)
        if not is_audible or snap.volume <= 0.0:
            continue

        gain = -snap.volume if snap.polarity_invert else snap.volume
        abs_gain = abs(gain)

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

    m_gain = float(master_gain)
    bit_depth = int(out_bit_depth) if int(out_bit_depth) in (16, 24, 32) else 16
    use_dither = bool(dither_tpdf) and (bit_depth < 32)
    sampwidth = bit_depth // 8
    want_m_vu = out_master_levels is not None and len(out_master_levels) >= 2
    want_m_pk = out_master_peaks is not None and len(out_master_peaks) >= 2

    if use_c:
        out_u8 = _dsp.alloc_u8(max_len * 2 * sampwidth)
        rng_ptr = ctypes.byref(dither_rng) if (use_dither and dither_rng is not None) else None
        if want_m_vu or want_m_pk:
            vl_c = ctypes.c_double(0.0)
            vr_c = ctypes.c_double(0.0)
            pl_c = ctypes.c_double(0.0)
            pr_c = ctypes.c_double(0.0)
            _dsp.LIB.dsp_master_pack_pcm(
                ml_ptr,
                mr_ptr,
                max_len,
                m_gain,
                bit_depth,
                1 if use_dither else 0,
                rng_ptr,
                _dsp.u8_ptr(out_u8),
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
            _dsp.LIB.dsp_master_pack_pcm(
                ml_ptr,
                mr_ptr,
                max_len,
                m_gain,
                bit_depth,
                1 if use_dither else 0,
                rng_ptr,
                _dsp.u8_ptr(out_u8),
                0,
                None,
                None,
                None,
                None,
            )
        return out_u8.tobytes()

    if bit_depth == 24:
        scale, q_min, q_max = 8388608.0, -8388608, 8388607
        out_24 = bytearray(max_len * 6)
    elif bit_depth == 32:
        scale, q_min, q_max = 2147483648.0, -2147483648, 2147483647
        stereo32 = array.array("i", [0] * (max_len * 2))
    else:
        scale, q_min, q_max = 32768.0, -32768, 32767
        stereo16 = array.array("h", [0] * (max_len * 2))

    rand = random.random
    l_sq = 0.0
    r_sq = 0.0
    l_pk = 0.0
    r_pk = 0.0
    off24 = 0

    for j in range(max_len):
        lv = master_left[j] * m_gain
        rv = master_right[j] * m_gain
        if want_m_vu or want_m_pk:
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

        sl = lv * scale
        sr = rv * scale
        if use_dither:
            sl += rand() - rand()
            sr += rand() - rand()
        ql = int(round(sl))
        qr = int(round(sr))
        if ql > q_max:
            ql = q_max
        elif ql < q_min:
            ql = q_min
        if qr > q_max:
            qr = q_max
        elif qr < q_min:
            qr = q_min

        if bit_depth == 16:
            stereo16[j * 2] = ql
            stereo16[j * 2 + 1] = qr
        elif bit_depth == 24:
            ul = ql & 0xFFFFFF
            ur = qr & 0xFFFFFF
            out_24[off24] = ul & 0xFF
            out_24[off24 + 1] = (ul >> 8) & 0xFF
            out_24[off24 + 2] = (ul >> 16) & 0xFF
            out_24[off24 + 3] = ur & 0xFF
            out_24[off24 + 4] = (ur >> 8) & 0xFF
            out_24[off24 + 5] = (ur >> 16) & 0xFF
            off24 += 6
        else:
            stereo32[j * 2] = ql
            stereo32[j * 2 + 1] = qr

    if want_m_vu:
        out_master_levels[0] = math.sqrt(l_sq * inv_len) * sqrt2
        out_master_levels[1] = math.sqrt(r_sq * inv_len) * sqrt2
    if want_m_pk:
        out_master_peaks[0] = l_pk
        out_master_peaks[1] = r_pk

    if bit_depth == 24:
        return bytes(out_24)
    if bit_depth == 32:
        return stereo32.tobytes()
    return stereo16.tobytes()


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


class _UiBridge(QObject):
    """Thread-safe queued Qt signal dispatcher replacing Tkinter's root.after(0, ...)."""

    invoke = Signal(object, tuple, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active = True
        self.invoke.connect(self._execute, Qt.ConnectionType.QueuedConnection)

    def shutdown(self):
        self._active = False

    def post(self, fn, *args, **kwargs):
        if self._active:
            self.invoke.emit(fn, args, kwargs)

    def _execute(self, fn, args, kwargs):
        if self._active:
            fn(*args, **kwargs)


class _MainWindow(QMainWindow):
    """QMainWindow wrapper handling Spacebar transport shortcut, focus release, and clean shutdown."""

    def __init__(self, controller):
        super().__init__()
        self._controller = controller
        self._close_handled = False

    def keyPressEvent(self, event):
        if (
            event.key() == Qt.Key.Key_Space
            and not event.isAutoRepeat()
            and not self._controller.nameBox.hasFocus()
        ):
            self._controller._on_space_key()
            event.accept()
            return
        super().keyPressEvent(event)

    def eventFilter(self, watched, event):
        etype = event.type()
        if etype == QEvent.Type.MouseButtonPress:
            name_box = getattr(self._controller, "nameBox", None)
            if name_box is not None and name_box.hasFocus() and watched is not name_box:
                name_box.clearFocus()
        elif etype == QEvent.Type.KeyPress:
            name_box = getattr(self._controller, "nameBox", None)
            if watched is name_box and event.key() == Qt.Key.Key_Escape:
                name_box.clearFocus()
                return True
        return super().eventFilter(watched, event)

    def closeEvent(self, event):
        if not self._close_handled:
            self._close_handled = True
            self._controller._shutdown_resources()
        super().closeEvent(event)


class Window:
    """4-Channel Audio Mixer PySide6 GUI & Multithreaded Audio Engine."""

    THEME = {
        "bg_root": "#1a2836",
        "bg_strip": "#263e54",
        "bg_master": "#22384c",
        "bg_header": "#1b2f40",
        "bg_well": "#14222f",
        "border": "#3f5f7d",
        "divider": "#38546e",
        "text_primary": "#f0f6fc",
        "text_muted": "#9cb8d1",
        "unity_gold": "#ffd166",
        "boost_coral": "#fc8181",
        "status_fg": "#7ef2b8",
    }

    TRACK_ACCENTS = ["#4da3ff", "#48d18a", "#f5c542", "#a375ff"]

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
        self.bounce_bitdepth = 16
        self.bounce_dither = True
        self.channels = 2
        self.rate = 44100

        # Ensure a QApplication exists (use Fusion style for fast, uniform dark controls on macOS)
        self._app = QApplication.instance()
        self._owns_app = False
        if self._app is None:
            self._app = QApplication(sys.argv)
            self._app.setStyle("Fusion")
            self._owns_app = True

        # Thread synchronization
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._active_thread = None
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
        self._rec_wave_elapsed = 0.0
        self._rec_wave_active = False

        # Blink timer tick counter for armed Rec buttons (~2 Hz flash at 60 FPS)
        self._blink_tick = 0

        # 4 independent channel states
        self.channel_states = [ChannelState(i + 1) for i in range(4)]

        # Open effect popup windows keyed by (channel_index_0_based, effect_name)
        self._effect_windows = {}

        # Build Qt6 main window
        self.root = _MainWindow(self)
        self.root.setWindowTitle("Mini-DAW")
        self.root.setMinimumSize(1080, 740)
        self.root.resize(1180, 820)
        self._apply_global_stylesheet()

        self._ui_bridge = _UiBridge(self.root)

        # Central layout with resizable vertical splitter (Arrange View / Mixer Console)
        central = QWidget(self.root)
        central.setObjectName("centralWidget")
        self.root.setCentralWidget(central)
        outer_layout = QVBoxLayout(central)
        outer_layout.setContentsMargins(12, 10, 12, 12)
        outer_layout.setSpacing(0)

        self.splitter = QSplitter(Qt.Orientation.Vertical, central)
        self.splitter.setHandleWidth(8)
        outer_layout.addWidget(self.splitter, 1)

        # Top: 4-Track Waveform Arrange View
        self._zoom_detail_gen = 0
        self.timeline = WaveformTimeline(self.splitter, on_seek=self.seek_to_seconds)
        self.timeline.fileDroppedOnChannel.connect(self.load_channel_file_path)
        self.timeline.detailRequested.connect(self._load_zoomed_waveforms_async)
        self.splitter.addWidget(self.timeline)

        # Native menu bar (built after timeline exists)
        self._build_menu_bar()

        # Bottom: Mixer Console Board (Ch 1-4 + Master Bus + Control & Export)
        mixer_board = QWidget(self.splitter)
        mixer_board.setObjectName("mixerBoard")
        self._mixer_layout = QHBoxLayout(mixer_board)
        self._mixer_layout.setContentsMargins(0, 4, 0, 0)
        self._mixer_layout.setSpacing(8)
        self.splitter.addWidget(mixer_board)

        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setSizes([230, 560])

        # Per-channel & master widget references
        self._ch_widgets = []
        self._channel_meters = []
        self._channel_peak_readouts = []
        self._rec_pad_modes = []
        self._master_meters = []
        self._master_peak_readout = None

        self._set_play_lit = lambda _lit: None
        self._set_pause_lit = lambda _lit: None
        self._set_rec_lit = lambda _lit: None
        self._set_rec_mode = lambda _mode: None

        self._build_channel_strips(mixer_board)
        self._build_master_bus_strip(mixer_board)
        self._build_master_panel(mixer_board)

        self._last_bounce_color = "#48d18a"

        # 30 FPS UI timer (33 ms) — smooth VU ballistics with minimal Python GIL overhead
        self._meter_timer = QTimer(self.root)
        self._meter_timer.setInterval(33)
        self._meter_timer.timeout.connect(self._poll_meters)
        self._meter_timer.start()

        if start_mainloop:
            self.root.show()
            if self._owns_app:
                self._app.exec()

    # ---------- Styling & Menu ----------

    def _apply_global_stylesheet(self):
        T = self.THEME
        self.root.setStyleSheet(
            f"""
            QMainWindow, QWidget#centralWidget, QWidget#mixerBoard, QSplitter {{
                background-color: {T['bg_root']};
                color: {T['text_primary']};
                font-family: ".AppleSystemUIFont", "Helvetica Neue", Helvetica, Arial;
            }}
            QSplitter::handle {{
                background-color: transparent;
            }}
            QSplitter::handle:hover {{
                background-color: #2f4b66;
                border-radius: 3px;
            }}
            QFrame.channelStrip {{
                background-color: {T['bg_strip']};
                border: 1px solid {T['border']};
                border-radius: 6px;
            }}
            QFrame.masterStrip {{
                background-color: {T['bg_master']};
                border: 1px solid #4f7496;
                border-radius: 6px;
            }}
            QFrame.stripHeader {{
                background-color: {T['bg_header']};
                border-bottom: 1px solid #2e4963;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
            }}
            QLabel.stripTitle {{
                background: transparent;
                color: {T['text_primary']};
                font-size: 12px;
                font-weight: 700;
                letter-spacing: 0.6px;
            }}
            QLabel.sectionLbl {{
                background: transparent;
                color: {T['text_muted']};
                font-size: 9px;
                font-weight: 700;
                letter-spacing: 0.7px;
            }}
            QFrame.hDivider {{
                background-color: {T['divider']};
                max-height: 1px;
                min-height: 1px;
                border: none;
            }}
            QLineEdit#nameBox {{
                background-color: {T['bg_well']};
                color: {T['text_primary']};
                border: 1px solid #45698a;
                border-radius: 5px;
                padding: 6px 8px;
                font-size: 12px;
            }}
            QLineEdit#nameBox:focus {{
                border-color: #63b3ed;
            }}
            QPushButton#exportBtn {{
                background-color: #3b7a47;
                color: #ffffff;
                border: 1px solid #529c60;
                border-radius: 5px;
                padding: 9px 12px;
                font-size: 12px;
                font-weight: 700;
            }}
            QPushButton#exportBtn:hover {{
                background-color: #4a9458;
            }}
            QPushButton#exportBtn:pressed {{
                background-color: #2d6037;
            }}
            QPushButton#unityBtn {{
                background-color: #1c3144;
                color: #ffd166;
                border: 1px solid #3c5c78;
                border-radius: 4px;
                padding: 5px 8px;
                font-size: 10px;
                font-weight: 700;
            }}
            QPushButton#unityBtn:hover {{
                background-color: #2a4763;
            }}
            QProgressBar#bounceBar {{
                background-color: {T['bg_well']};
                border: 1px solid #38546e;
                border-radius: 4px;
                max-height: 12px;
                min-height: 12px;
                text-align: center;
            }}
            QProgressBar#bounceBar::chunk {{
                background-color: #48d18a;
                border-radius: 3px;
            }}
            """
        )

    def _build_menu_bar(self):
        menubar = self.root.menuBar()

        file_menu = menubar.addMenu("&File")
        for idx in range(4):
            act = QAction(f"Load WAV on Channel {idx + 1}...", self.root)
            act.setShortcut(QKeySequence(f"Ctrl+{idx + 1}"))
            act.triggered.connect(lambda _checked=False, c=idx: self.open_channel_file(c))
            file_menu.addAction(act)

        file_menu.addSeparator()
        bounce_act = QAction("Bounce Mix (.wav)...", self.root)
        bounce_act.setShortcut(QKeySequence("Ctrl+E"))
        bounce_act.triggered.connect(self.bounce)
        file_menu.addAction(bounce_act)

        file_menu.addSeparator()
        exit_act = QAction("Exit", self.root)
        exit_act.setShortcut(QKeySequence.StandardKey.Quit)
        exit_act.triggered.connect(self.on_close)
        file_menu.addAction(exit_act)

        transport_menu = menubar.addMenu("&Transport")
        play_act = QAction("Play / Pause", self.root)
        play_act.triggered.connect(self._on_space_key)
        transport_menu.addAction(play_act)

        stop_act = QAction("Stop", self.root)
        stop_act.setShortcut(QKeySequence(Qt.Key.Key_Escape))
        stop_act.triggered.connect(self.stop)
        transport_menu.addAction(stop_act)

        rec_act = QAction("Record / Stop Recording", self.root)
        rec_act.setShortcut(QKeySequence("Ctrl+R"))
        rec_act.triggered.connect(self.press_rec)
        transport_menu.addAction(rec_act)

        view_menu = menubar.addMenu("&View")
        zoom_in_act = QAction("Zoom In Timeline", self.root)
        zoom_in_act.setShortcuts([QKeySequence.StandardKey.ZoomIn, QKeySequence("Ctrl+=")])
        zoom_in_act.triggered.connect(lambda: self.timeline.canvas.set_zoom(self.timeline.canvas.zoom() * 1.4))
        view_menu.addAction(zoom_in_act)

        zoom_out_act = QAction("Zoom Out Timeline", self.root)
        zoom_out_act.setShortcuts([QKeySequence.StandardKey.ZoomOut, QKeySequence("Ctrl+-")])
        zoom_out_act.triggered.connect(lambda: self.timeline.canvas.set_zoom(self.timeline.canvas.zoom() / 1.4))
        view_menu.addAction(zoom_out_act)

        zoom_reset_act = QAction("Reset Timeline Zoom", self.root)
        zoom_reset_act.setShortcut(QKeySequence("Ctrl+0"))
        zoom_reset_act.triggered.connect(self.timeline.reset_zoom)
        view_menu.addAction(zoom_reset_act)

    # ---------- Console Construction ----------

    def _build_channel_strips(self, parent):
        T = self.THEME
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

        for idx in range(4):
            ch_num = idx + 1
            accent = self.TRACK_ACCENTS[idx]

            strip = QFrame(parent)
            strip.setProperty("class", "channelStrip")
            strip.setMinimumWidth(164)
            strip.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            strip_layout = QVBoxLayout(strip)
            strip_layout.setContentsMargins(0, 0, 0, 8)
            strip_layout.setSpacing(0)

            # Header bar with subtle track-color top accent
            header = QFrame(strip)
            header.setObjectName("chHeader")
            header.setFixedHeight(32)
            header.setStyleSheet(
                f"QFrame#chHeader {{ background-color: {T['bg_header']}; border-top: 3px solid {accent}; "
                "border-bottom: 1px solid #2e4963; border-top-left-radius: 5px; border-top-right-radius: 5px; }"
            )
            hdr_layout = QHBoxLayout(header)
            hdr_layout.setContentsMargins(8, 0, 8, 0)
            hdr_lbl = QLabel(f"CHANNEL {ch_num}", header)
            hdr_lbl.setProperty("class", "stripTitle")
            hdr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hdr_layout.addWidget(hdr_lbl)
            strip_layout.addWidget(header)

            body = QWidget(strip)
            body.setStyleSheet("background: transparent;")
            body_layout = QVBoxLayout(body)
            body_layout.setContentsMargins(8, 7, 8, 0)
            body_layout.setSpacing(6)
            strip_layout.addWidget(body, 1)

            # File well (click or drag & drop WAV)
            file_well = ChannelFileWell(body)
            file_well.loadClicked.connect(lambda c=idx: self.open_channel_file(c))
            file_well.clearClicked.connect(lambda c=idx: self.clear_channel_file(c))
            file_well.fileDropped.connect(lambda path, c=idx: self.load_channel_file_path(c, path))
            body_layout.addWidget(file_well)

            # Inserts section
            ins_lbl = QLabel("INSERTS", body)
            ins_lbl.setProperty("class", "sectionLbl")
            body_layout.addWidget(ins_lbl)

            fx_slots = {}
            ins_box = QVBoxLayout()
            ins_box.setSpacing(4)
            for fx_key, fx_label, active_bg, led_color, inactive_bg, hover_bg, _ in self.EFFECT_SPECS:
                slot = InsertSlotWidget(
                    fx_label=fx_label,
                    active_bg=active_bg,
                    led_color=led_color,
                    inactive_bg=inactive_bg,
                    hover_bg=hover_bg,
                    parent=body,
                )
                slot.toggled.connect(lambda en, c=idx, k=fx_key: self._on_effect_toggle(c, k, en))
                slot.editClicked.connect(lambda c=idx, k=fx_key: self.open_effect_window(c, k))
                ins_box.addWidget(slot)
                fx_slots[fx_key] = slot
            body_layout.addLayout(ins_box)

            # Divider
            div = QFrame(body)
            div.setProperty("class", "hDivider")
            body_layout.addWidget(div)

            # Pan control
            pan_lbl = QLabel("Pan: C", body)
            pan_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pan_lbl.setStyleSheet("background: transparent; color: #f0f6fc; font-size: 11px;")
            body_layout.addWidget(pan_lbl)

            pan_row = QHBoxLayout()
            pan_row.setContentsMargins(0, 0, 0, 0)
            pan_row.setSpacing(4)
            l_lbl = QLabel("L", body)
            l_lbl.setProperty("class", "sectionLbl")
            r_lbl = QLabel("R", body)
            r_lbl.setProperty("class", "sectionLbl")
            pan_slider = PanSlider(body)
            pan_slider.panChanged.connect(lambda val, c=idx, lbl=pan_lbl: self._on_pan_change(c, val, lbl))
            pan_row.addWidget(l_lbl)
            pan_row.addWidget(pan_slider, 1)
            pan_row.addWidget(r_lbl)
            body_layout.addLayout(pan_row)

            # Dual readouts: [Peak dBFS] [Fader dB]
            readout_row = QHBoxLayout()
            readout_row.setContentsMargins(0, 0, 0, 0)
            readout_row.setSpacing(4)
            peak_readout = PeakReadout(body)
            self._channel_peak_readouts.append(peak_readout)

            vol_badge = FaderBadge("0.0 dB", body)

            readout_row.addWidget(peak_readout, 1)
            readout_row.addWidget(vol_badge, 1)
            body_layout.addLayout(readout_row)

            # Meter + Custom DAW Fader + Painted dB Scale
            fader_row = QHBoxLayout()
            fader_row.setContentsMargins(0, 2, 0, 2)
            fader_row.setSpacing(6)
            fader_row.addStretch(1)

            meter = ChannelMeter(body, width=16, height=150, segments=22)
            self._channel_meters.append(meter)
            fader_with_scale = FaderWithScale(ch_ticks, accent_color=accent, parent=body)
            fader_with_scale.fader.dbChanged.connect(
                lambda db, c=idx, bdg=vol_badge: self._on_vol_change(c, db, bdg)
            )

            fader_row.addWidget(meter)
            fader_row.addWidget(fader_with_scale)
            fader_row.addStretch(1)
            body_layout.addLayout(fader_row, 1)

            # Channel toggle pads: Solo / Mute / Phase / Rec
            ctrl_grid = QGridLayout()
            ctrl_grid.setContentsMargins(0, 2, 0, 0)
            ctrl_grid.setHorizontalSpacing(4)
            ctrl_grid.setVerticalSpacing(4)
            ctrl_grid.setColumnStretch(0, 1)
            ctrl_grid.setColumnStretch(1, 1)

            solo_pad = TogglePadButton(
                "S  Solo", "#f5c542", "#332c19", "#473d22", "#fae08c", "#7a662b",
                "#dba316", "#1c1400", "#fff1ad", parent=body,
            )
            solo_pad.toggled.connect(lambda en, c=idx: self.channel_states[c].set_solo(en))

            mute_pad = TogglePadButton(
                "M  Mute", "#f27a30", "#382319", "#4d2f21", "#ffa873", "#804a2c",
                "#d95516", "#ffffff", "#ffc6a3", parent=body,
            )
            mute_pad.toggled.connect(lambda en, c=idx: self.channel_states[c].set_mute(en))

            pol_pad = TogglePadButton(
                "Ø  Phase", "#a375ff", "#2b2142", "#3b2d5c", "#d1b3ff", "#614994",
                "#7242d6", "#ffffff", "#dec9ff", parent=body,
            )
            pol_pad.toggled.connect(lambda en, c=idx: self.channel_states[c].set_polarity_invert(en))

            rec_pad = TogglePadButton(
                "●  Rec", "#f24957", "#3a1c22", "#52252e", "#ff8c96", "#853743",
                "#cc2431", "#ffffff", "#ffb8bf", parent=body,
            )
            rec_pad.toggled.connect(lambda en, c=idx: self.channel_states[c].set_rec_armed(en))

            ctrl_grid.addWidget(solo_pad, 0, 0)
            ctrl_grid.addWidget(mute_pad, 0, 1)
            ctrl_grid.addWidget(pol_pad, 1, 0)
            ctrl_grid.addWidget(rec_pad, 1, 1)
            body_layout.addLayout(ctrl_grid)

            self._rec_pad_modes.append(rec_pad.set_flash_mode)
            self._ch_widgets.append(
                {
                    "file_well": file_well,
                    "fx_slots": fx_slots,
                    "pan_slider": pan_slider,
                    "vol_fader": fader_with_scale.fader,
                    "solo_pad": solo_pad,
                    "mute_pad": mute_pad,
                    "pol_pad": pol_pad,
                    "rec_pad": rec_pad,
                }
            )
            self._mixer_layout.addWidget(strip, 1)

    def _build_master_bus_strip(self, parent):
        T = self.THEME
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

        strip = QFrame(parent)
        strip.setProperty("class", "masterStrip")
        strip.setMinimumWidth(148)
        strip.setMaximumWidth(178)
        strip_layout = QVBoxLayout(strip)
        strip_layout.setContentsMargins(0, 0, 0, 8)
        strip_layout.setSpacing(0)

        header = QFrame(strip)
        header.setObjectName("masterHeader")
        header.setFixedHeight(32)
        header.setStyleSheet(
            f"QFrame#masterHeader {{ background-color: {T['bg_header']}; border-top: 3px solid {T['unity_gold']}; "
            "border-bottom: 1px solid #2e4963; border-top-left-radius: 5px; border-top-right-radius: 5px; }"
        )
        hdr_layout = QHBoxLayout(header)
        hdr_layout.setContentsMargins(8, 0, 8, 0)
        hdr_lbl = QLabel("MASTER BUS", header)
        hdr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hdr_lbl.setStyleSheet(
            f"background: transparent; border: none; color: {T['unity_gold']}; "
            "font-size: 12px; font-weight: 700; letter-spacing: 0.6px;"
        )
        hdr_layout.addWidget(hdr_lbl)
        strip_layout.addWidget(header)

        body = QWidget(strip)
        body.setStyleSheet("background: transparent;")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(8, 8, 8, 0)
        body_layout.setSpacing(7)
        strip_layout.addWidget(body, 1)

        bus_badge = QLabel("SUM · CH 1–4", body)
        bus_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bus_badge.setStyleSheet(
            f"background-color: {T['bg_well']}; color: #8fc5f5; border: 1px solid #38546e; "
            "border-radius: 4px; padding: 5px; font-size: 10px; font-weight: 700;"
        )
        body_layout.addWidget(bus_badge)

        pf_lbl = QLabel("PEAK  /  FADER", body)
        pf_lbl.setProperty("class", "sectionLbl")
        pf_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body_layout.addWidget(pf_lbl)

        readout_row = QHBoxLayout()
        readout_row.setSpacing(4)
        self._master_peak_readout = PeakReadout(body)
        self.master_vol_badge = FaderBadge("0.0 dB", body)
        readout_row.addWidget(self._master_peak_readout, 1)
        readout_row.addWidget(self.master_vol_badge, 1)
        body_layout.addLayout(readout_row)

        # Stereo L/R VU meters + Master Fader + dB ticks
        bus_row = QHBoxLayout()
        bus_row.setContentsMargins(0, 4, 0, 4)
        bus_row.setSpacing(8)
        bus_row.addStretch(1)

        meters_col = QVBoxLayout()
        meters_col.setSpacing(3)
        lr_pair = QHBoxLayout()
        lr_pair.setSpacing(3)
        meter_l = ChannelMeter(body, width=16, height=230, segments=36)
        meter_r = ChannelMeter(body, width=16, height=230, segments=36)
        lr_pair.addWidget(meter_l)
        lr_pair.addWidget(meter_r)
        self._master_meters = [meter_l, meter_r]
        meters_col.addLayout(lr_pair, 1)

        lr_labels = QHBoxLayout()
        lbl_l = QLabel("L", body)
        lbl_l.setProperty("class", "sectionLbl")
        lbl_l.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_r = QLabel("R", body)
        lbl_r.setProperty("class", "sectionLbl")
        lbl_r.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lr_labels.addWidget(lbl_l)
        lr_labels.addWidget(lbl_r)
        meters_col.addLayout(lr_labels)

        self._master_fader_widget = FaderWithScale(
            master_ticks, accent_color=T["unity_gold"], is_master=True, parent=body
        )
        self.master_scale = self._master_fader_widget.fader
        self.master_scale.dbChanged.connect(self._on_master_vol_change)

        fader_col = QVBoxLayout()
        fader_col.setSpacing(3)
        fader_col.addWidget(self._master_fader_widget, 1)
        spacer_lbl = QLabel(" ", body)
        spacer_lbl.setProperty("class", "sectionLbl")
        fader_col.addWidget(spacer_lbl)

        bus_row.addLayout(meters_col)
        bus_row.addLayout(fader_col)
        bus_row.addStretch(1)
        body_layout.addLayout(bus_row, 1)

        unity_btn = QPushButton("0.0 dB Unity", body)
        unity_btn.setObjectName("unityBtn")
        unity_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        unity_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        unity_btn.clicked.connect(lambda: self.master_scale.set_db(0.0))
        body_layout.addWidget(unity_btn)

        self._mixer_layout.addWidget(strip)

    def _build_master_panel(self, parent):
        T = self.THEME

        panel = QFrame(parent)
        panel.setProperty("class", "channelStrip")
        panel.setMinimumWidth(248)
        panel.setMaximumWidth(285)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 10)
        panel_layout.setSpacing(0)

        header = QFrame(panel)
        header.setObjectName("ctrlHeader")
        header.setFixedHeight(32)
        header.setStyleSheet(
            f"QFrame#ctrlHeader {{ background-color: {T['bg_header']}; border-top: 3px solid #5aa9f5; "
            "border-bottom: 1px solid #2e4963; border-top-left-radius: 5px; border-top-right-radius: 5px; }"
        )
        hdr_layout = QHBoxLayout(header)
        hdr_layout.setContentsMargins(8, 0, 8, 0)
        hdr_lbl = QLabel("CONTROL & EXPORT", header)
        hdr_lbl.setProperty("class", "stripTitle")
        hdr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hdr_layout.addWidget(hdr_lbl)
        panel_layout.addWidget(header)

        body = QWidget(panel)
        body.setStyleSheet("background: transparent;")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(12, 10, 12, 0)
        body_layout.setSpacing(8)
        panel_layout.addWidget(body, 1)

        # Transport bay
        t_lbl = QLabel("TRANSPORT", body)
        t_lbl.setProperty("class", "sectionLbl")
        body_layout.addWidget(t_lbl)

        transport_bay = QFrame(body)
        transport_bay.setStyleSheet(
            f"background-color: {T['bg_well']}; border: 1px solid #38546e; border-radius: 6px;"
        )
        tb_layout = QHBoxLayout(transport_bay)
        tb_layout.setContentsMargins(8, 10, 8, 10)
        tb_layout.setSpacing(6)

        self.stop_button = TransportButton(
            kind="stop", caption="STOP", icon_color="#d5e4f2", hover_icon_color="#ffffff", parent=transport_bay
        )
        self.stop_button.clicked.connect(self.stop)

        self.play_button = TransportButton(
            kind="play",
            caption="PLAY",
            icon_color="#48d18a",
            hover_icon_color="#7ef2b8",
            lit_bg="#246e4c",
            lit_border="#68d391",
            parent=transport_bay,
        )
        self.play_button.clicked.connect(self.press_play)
        self._set_play_lit = self.play_button.set_lit

        self.pause_button = TransportButton(
            kind="pause",
            caption="PAUSE",
            icon_color="#f5c542",
            hover_icon_color="#ffe28a",
            lit_bg="#b88414",
            lit_border="#ffe28a",
            parent=transport_bay,
        )
        self.pause_button.clicked.connect(self.press_pause)
        self._set_pause_lit = self.pause_button.set_lit

        self.rec_button = TransportButton(
            kind="rec",
            caption="REC",
            icon_color="#f24957",
            hover_icon_color="#ff7a85",
            lit_bg="#b82530",
            lit_border="#ff858f",
            parent=transport_bay,
        )
        self.rec_button.clicked.connect(self.press_rec)
        self._set_rec_lit = self.rec_button.set_lit
        self._set_rec_mode = self.rec_button.set_mode

        tb_layout.addStretch(1)
        tb_layout.addWidget(self.stop_button)
        tb_layout.addWidget(self.play_button)
        tb_layout.addWidget(self.pause_button)
        tb_layout.addWidget(self.rec_button)
        tb_layout.addStretch(1)
        body_layout.addWidget(transport_bay)

        # Status screen
        s_lbl = QLabel("STATUS", body)
        s_lbl.setProperty("class", "sectionLbl")
        body_layout.addWidget(s_lbl)

        self.status_label = QLabel("Ready", body)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(44)
        self.status_label.setStyleSheet(
            f"background-color: {T['bg_well']}; color: {T['status_fg']}; "
            "border: 1px solid #38546e; border-radius: 5px; padding: 8px; "
            "font-size: 12px; font-weight: 700;"
        )
        body_layout.addWidget(self.status_label)

        div = QFrame(body)
        div.setProperty("class", "hDivider")
        body_layout.addWidget(div)

        # Mix / Recording Name & Export
        n_lbl = QLabel("MIX / RECORDING NAME", body)
        n_lbl.setProperty("class", "sectionLbl")
        body_layout.addWidget(n_lbl)

        self.nameBox = QLineEdit(body)
        self.nameBox.setObjectName("nameBox")
        self.nameBox.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.nameBox.setPlaceholderText("Optional filename (e.g. final_mix)")
        self.nameBox.returnPressed.connect(self.nameBox.clearFocus)
        self.nameBox.installEventFilter(self.root)
        self._app.installEventFilter(self.root)
        body_layout.addWidget(self.nameBox)

        # Export bit depth & TPDF dither selector
        fmt_lbl = QLabel("BOUNCE FORMAT", body)
        fmt_lbl.setProperty("class", "sectionLbl")
        body_layout.addWidget(fmt_lbl)

        fmt_box = QFrame(body)
        fmt_box.setStyleSheet(
            f"background-color: {T['bg_well']}; border: 1px solid #38546e; border-radius: 5px;"
        )
        fmt_layout = QVBoxLayout(fmt_box)
        fmt_layout.setContentsMargins(6, 6, 6, 6)
        fmt_layout.setSpacing(5)

        depth_row = QHBoxLayout()
        depth_row.setContentsMargins(0, 0, 0, 0)
        depth_row.setSpacing(4)
        self._depth_btns = {}

        def _refresh_export_fmt():
            bd = self.bounce_bitdepth
            for d, btn in self._depth_btns.items():
                if d == bd:
                    btn.setStyleSheet(
                        "background-color: #245994; color: #ffffff; border: 1px solid #63b3ed; "
                        "border-radius: 4px; padding: 4px 2px; font-size: 10px; font-weight: 700;"
                    )
                else:
                    btn.setStyleSheet(
                        "background-color: #1d3042; color: #9cb8d1; border: 1px solid #33506b; "
                        "border-radius: 4px; padding: 4px 2px; font-size: 10px; font-weight: 700;"
                    )
            if bd >= 32:
                self._dither_btn.setEnabled(False)
                self._dither_btn.setText("○  TPDF Dither (N/A at 32-bit)")
                self._dither_btn.setStyleSheet(
                    "background-color: #182735; color: #637e96; border: 1px solid #2b4257; "
                    "border-radius: 4px; padding: 4px 6px; font-size: 10px; font-weight: 700;"
                )
            elif self.bounce_dither:
                self._dither_btn.setEnabled(True)
                self._dither_btn.setText("●  TPDF Dither (1-LSB)")
                self._dither_btn.setStyleSheet(
                    "background-color: #1f4d3b; color: #8cf5c0; border: 1px solid #48d18a; "
                    "border-radius: 4px; padding: 4px 6px; font-size: 10px; font-weight: 700;"
                )
            else:
                self._dither_btn.setEnabled(True)
                self._dither_btn.setText("○  TPDF Dither (Off · Round)")
                self._dither_btn.setStyleSheet(
                    "background-color: #1d3042; color: #9cb8d1; border: 1px solid #33506b; "
                    "border-radius: 4px; padding: 4px 6px; font-size: 10px; font-weight: 700;"
                )

        def _select_bitdepth(depth):
            self.bounce_bitdepth = depth
            if depth == 16:
                self.bounce_dither = True
            elif depth >= 24:
                self.bounce_dither = False
            _refresh_export_fmt()

        def _toggle_dither():
            if self.bounce_bitdepth >= 32:
                return
            self.bounce_dither = not self.bounce_dither
            _refresh_export_fmt()

        for depth in (16, 24, 32):
            btn = QPushButton(f"{depth}-bit", fmt_box)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _checked=False, d=depth: _select_bitdepth(d))
            depth_row.addWidget(btn, 1)
            self._depth_btns[depth] = btn
        fmt_layout.addLayout(depth_row)

        self._dither_btn = QPushButton("●  TPDF Dither (1-LSB)", fmt_box)
        self._dither_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._dither_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dither_btn.setToolTip(
            "1-LSB Triangular PDF dither eliminates quantization distortion when reducing bit depth"
        )
        self._dither_btn.clicked.connect(_toggle_dither)
        fmt_layout.addWidget(self._dither_btn)
        _refresh_export_fmt()
        body_layout.addWidget(fmt_box)

        self.export_button = QPushButton("Bounce Mix (.wav)", body)
        self.export_button.setObjectName("exportBtn")
        self.export_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_button.clicked.connect(self.bounce)
        body_layout.addWidget(self.export_button)

        self._bounce_prog_lbl = QLabel("Bounce: ready", body)
        self._bounce_prog_lbl.setStyleSheet("background: transparent; color: #9cb8d1; font-size: 10px; font-weight: 700;")
        body_layout.addWidget(self._bounce_prog_lbl)

        self._bounce_bar = QProgressBar(body)
        self._bounce_bar.setObjectName("bounceBar")
        self._bounce_bar.setRange(0, 1000)
        self._bounce_bar.setValue(0)
        self._bounce_bar.setTextVisible(False)
        body_layout.addWidget(self._bounce_bar)

        body_layout.addStretch(1)

        # Quick Guide
        tips_box = QFrame(body)
        tips_box.setStyleSheet(
            f"background-color: {T['bg_header']}; border: 1px solid #38546e; border-radius: 6px;"
        )
        tips_layout = QVBoxLayout(tips_box)
        tips_layout.setContentsMargins(10, 8, 10, 8)
        tips_layout.setSpacing(4)

        tips_hdr = QLabel("QUICK GUIDE", tips_box)
        tips_hdr.setProperty("class", "sectionLbl")
        tips_layout.addWidget(tips_hdr)

        tips_text = QLabel(
            "• Drag & drop .wav onto any track or slot\n"
            "• Scroll wheel over waveforms to zoom\n"
            "• Click / drag top view to scrub playhead\n"
            "• Spacebar toggles Play / Pause\n"
            "• Double-click fader / pan to reset\n"
            "• Arm ● Rec (flashes), click REC",
            tips_box,
        )
        tips_text.setWordWrap(True)
        tips_text.setStyleSheet("background: transparent; color: #c9dced; font-size: 11px; line-height: 1.3;")
        tips_layout.addWidget(tips_text)
        body_layout.addWidget(tips_box)

        self._mixer_layout.addWidget(panel)

    # ---------- Thread-safe UI helper ----------

    def _ui_call(self, fn, *args, **kwargs):
        if self._closing:
            return
        self._ui_bridge.post(fn, *args, **kwargs)

    def _set_status(self, text):
        self._ui_call(self.status_label.setText, text)

    def _show_info(self, title, message):
        QMessageBox.information(self.root, title, message)

    def _show_error(self, title, message):
        QMessageBox.critical(self.root, title, message)

    def _set_meter_peaks(
        self, ch_levels, master_levels=None, ch_true_peaks=None, master_true_peaks=None
    ):
        with self._meter_lock:
            for i in range(min(4, len(ch_levels))):
                self._channel_peaks[i] = ch_levels[i]
            if master_levels is not None and len(master_levels) >= 2:
                self._master_peaks[0] = master_levels[0]
                self._master_peaks[1] = master_levels[1]
            if ch_true_peaks is not None:
                for i in range(min(4, len(ch_true_peaks))):
                    if ch_true_peaks[i] > self._channel_true_peaks[i]:
                        self._channel_true_peaks[i] = ch_true_peaks[i]
            if master_true_peaks is not None and len(master_true_peaks) >= 2:
                if master_true_peaks[0] > self._master_true_peaks[0]:
                    self._master_true_peaks[0] = master_true_peaks[0]
                if master_true_peaks[1] > self._master_true_peaks[1]:
                    self._master_true_peaks[1] = master_true_peaks[1]

    def _reset_meter_peaks(self):
        with self._meter_lock:
            self._channel_peaks = [0.0, 0.0, 0.0, 0.0]
            self._master_peaks = [0.0, 0.0]
            self._channel_true_peaks = [0.0, 0.0, 0.0, 0.0]
            self._master_true_peaks = [0.0, 0.0]

    def _get_master_volume(self):
        with self._master_lock:
            return self._master_volume

    def _on_master_vol_change(self, db):
        gain = db_to_linear(db)
        with self._master_lock:
            self._master_volume = gain
        self.master_vol_badge.set_db_display(format_fader_db(db), db)

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
        self.timeline.set_position(clamped)
        if not is_playing and dur > 0.0:
            self._set_status(f"Playhead: {format_timecode(clamped)}")

    def _poll_meters(self):
        """60 FPS main-thread loop: VU meters, playhead sync, and armed Rec LED flashing."""
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
            self._channel_true_peaks = [0.0, 0.0, 0.0, 0.0]
            self._master_true_peaks = [0.0, 0.0]
            if not is_playing and not is_recording:
                self._channel_peaks = [0.0, 0.0, 0.0, 0.0]
                self._master_peaks = [0.0, 0.0]

        now = time.monotonic()
        for idx, meter in enumerate(self._channel_meters):
            meter.update_level(ch_peaks[idx])
        for idx, readout in enumerate(self._channel_peak_readouts):
            readout.update_peak(ch_true[idx], now=now)
        for idx, meter in enumerate(self._master_meters):
            meter.update_level(m_peaks[idx])
        if self._master_peak_readout is not None:
            self._master_peak_readout.update_peak(max(m_true[0], m_true[1]), now=now)

        snaps_aud = [ch.snapshot() for ch in self.channel_states]
        any_s = any(s.solo for s in snaps_aud)
        self.timeline.set_audible_flags([(not s.mute) and (not any_s or s.solo) for s in snaps_aud])

        if is_recording:
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
            self.timeline.set_position(pos_sec)

        # Synchronized ~2 Hz flash for armed Rec buttons (30 FPS -> toggle every 14 ticks)
        self._blink_tick += 1
        blink_hi = (self._blink_tick // 14) % 2 == 0
        any_armed = False
        for idx, snap in enumerate(snaps_aud):
            armed = snap.rec_armed
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

    def _on_space_key(self):
        """Spacebar shortcut: Play/Pause/Resume, or Stop when recording (ignored while typing or bouncing)."""
        if self.nameBox.hasFocus():
            return
        with self._state_lock:
            is_playing = self.playing
            is_recording = self.recording
            is_bouncing = self.bouncing
        if is_bouncing:
            return
        if is_recording:
            self.stop()
        elif is_playing:
            self.press_pause()
        else:
            self.press_play()

    def _set_bounce_progress(self, fraction, label=None, bar_color="#48d18a"):
        frac = max(0.0, min(1.0, float(fraction)))
        self._bounce_bar.setValue(int(round(frac * 1000)))
        if bar_color != self._last_bounce_color:
            self._last_bounce_color = bar_color
            self._bounce_bar.setStyleSheet(
                f"""
                QProgressBar#bounceBar {{
                    background-color: {self.THEME['bg_well']};
                    border: 1px solid #38546e;
                    border-radius: 4px;
                    max-height: 12px;
                    min-height: 12px;
                }}
                QProgressBar#bounceBar::chunk {{
                    background-color: {bar_color};
                    border-radius: 3px;
                }}
                """
            )
        if label is not None:
            self._bounce_prog_lbl.setText(label)

    # ---------- Slider & Effect Popup Callbacks ----------

    def _on_vol_change(self, channel_idx, db, badge=None):
        val = float(db)
        self.channel_states[channel_idx].set_volume_db(val)
        if badge is not None:
            badge.set_db_display(format_fader_db(val), val)

    def _on_pan_change(self, channel_idx, value, label_widget):
        val = float(value)
        self.channel_states[channel_idx].set_pan(val)
        if abs(val - 0.5) < 0.02:
            label_widget.setText("Pan: C")
        elif val < 0.5:
            pct = int(round((0.5 - val) * 200))
            label_widget.setText(f"Pan: {pct}% L")
        else:
            pct = int(round((val - 0.5) * 200))
            label_widget.setText(f"Pan: {pct}% R")

    def _on_effect_toggle(self, channel_idx, effect_key, enabled):
        self.channel_states[channel_idx].set_effect_enabled(effect_key, bool(enabled))
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
        self._position_effect_window(popup)
        self._effect_windows[key] = popup

    def _position_effect_window(self, popup):
        """Tile newly opened effect popups side-by-side instead of stacking on top of each other."""
        try:
            screen = self.root.screen() or QApplication.primaryScreen()
            avail = screen.availableGeometry() if screen is not None else self.root.frameGeometry()
            root_geo = self.root.frameGeometry()

            pw = max(popup.width(), popup.sizeHint().width(), 340)
            ph = max(popup.height(), popup.sizeHint().height(), 240)
            gap = 12

            occupied = [
                p.frameGeometry()
                for p in self._effect_windows.values()
                if p is not popup and p.is_open()
            ]

            start_x = max(avail.left() + gap, root_geo.left() + 24)
            start_y = max(avail.top() + gap, root_geo.top() + 72)

            # Search left-to-right, top-to-bottom for the first non-overlapping slot
            y = start_y
            placed = False
            while y + ph <= avail.bottom() - gap:
                x = start_x
                while x + pw <= avail.right() - gap:
                    cand = QRect(x, y, pw, ph)
                    if not any(cand.adjusted(-6, -6, 6, 6).intersects(r) for r in occupied):
                        popup.move(x, y)
                        placed = True
                        break
                    x += pw + gap
                if placed:
                    break
                y += ph + gap

            if not placed:
                offset = (len(occupied) % 6) * 28
                cx = min(avail.right() - pw - gap, start_x + offset)
                cy = min(avail.bottom() - ph - gap, start_y + offset)
                popup.move(max(avail.left() + gap, cx), max(avail.top() + gap, cy))
        except Exception:
            pass

    def _close_effect_window(self, channel_idx, effect_key):
        key = (channel_idx, effect_key)
        popup = self._effect_windows.pop(key, None)
        if popup is not None and popup.is_open():
            popup.close()

    # ---------- File Loading ----------

    def open_channel_file(self, channel_idx):
        filepath, _ = QFileDialog.getOpenFileName(
            self.root,
            f"Load WAV for Channel {channel_idx + 1}",
            BASE_DIR,
            "WAV files (*.wav);;All files (*.*)",
        )
        if not filepath:
            return
        self.load_channel_file_path(channel_idx, filepath)

    def load_channel_file_path(self, channel_idx, filepath):
        """Load a WAV file path directly into a channel (used by file dialog and drag-and-drop)."""
        if self._is_busy():
            self._show_info(
                "Mixer Busy",
                "Please stop playback, recording, or bounce before loading a new file.",
            )
            return

        try:
            with wave.open(filepath, "rb") as wf:
                channels = wf.getnchannels()
                samplefreq = wf.getframerate()
                bitdepth = wf.getsampwidth() * 8
                length = int(wf.getnframes() / max(1, samplefreq))
        except Exception as exc:
            self._show_error("Error loading WAV", f"Could not open file:\n{exc}")
            return

        self.channel_states[channel_idx].set_filepath(filepath)
        well = self._ch_widgets[channel_idx]["file_well"]
        well.set_file_text(os.path.basename(filepath))
        well.file_btn.setToolTip(filepath)
        well.set_meta_text(format_audio_meta(samplefreq, bitdepth, channels))

        mins, secs = divmod(length, 60)
        print(
            f"Channel {channel_idx + 1} audio loaded\n"
            f"  File route: {filepath}\n"
            f"  Number of channels: {channels}\n"
            f"  Sample rate: {samplefreq} Hz\n"
            f"  Bit Depth: {bitdepth}-bit\n"
            f"  Duration: {mins}:{secs:02d}"
        )
        self._update_session_duration()
        self._load_channel_waveform_async(channel_idx, filepath)
        self._set_status(f"Loaded Ch {channel_idx + 1}: {os.path.basename(filepath)}")

    def _load_channel_waveform_async(self, channel_idx, filepath):
        """Extract high-resolution waveform envelope on a background thread and paint it on the Arrange view."""

        def _worker():
            try:
                peaks, dur = extract_waveform_peaks(filepath, num_bins=2400)
            except Exception as exc:
                print(f"Waveform extraction skipped: {exc}")
                return
            if self.channel_states[channel_idx].get_filepath() != filepath:
                return
            self._ui_call(
                self.timeline.set_track_waveform,
                channel_idx,
                peaks,
                dur,
                os.path.basename(filepath),
            )

        threading.Thread(target=_worker, name=f"WaveformCh{channel_idx + 1}", daemon=True).start()

    def _load_zoomed_waveforms_async(self, start_sec, end_sec, num_bins):
        """Extract sample-accurate zoomed waveform detail for the currently visible timeline window."""
        self._zoom_detail_gen += 1
        gen = self._zoom_detail_gen
        paths = [ch.get_filepath() for ch in self.channel_states]
        if not any(paths):
            return

        def _worker():
            for ch_idx, path in enumerate(paths):
                if self._closing or gen != self._zoom_detail_gen:
                    return
                if not path:
                    continue
                try:
                    with wave.open(path, "rb") as wf:
                        track_dur = wf.getnframes() / float(max(1, wf.getframerate()))
                    vis_s = max(0.0, float(start_sec))
                    vis_e = min(track_dur, float(end_sec))
                    if vis_e <= vis_s:
                        continue
                    bins, _ = extract_waveform_peaks(
                        path,
                        num_bins=max(200, int(num_bins)),
                        start_sec=vis_s,
                        end_sec=vis_e,
                    )
                except Exception:
                    continue
                if self._closing or gen != self._zoom_detail_gen:
                    return
                if self.channel_states[ch_idx].get_filepath() != path:
                    continue
                self._ui_call(self.timeline.set_track_detail, ch_idx, bins, vis_s, vis_e)

        threading.Thread(target=_worker, name=f"WaveformZoom{gen}", daemon=True).start()

    def clear_channel_file(self, channel_idx):
        """Clear the loaded WAV file from a channel without needing to load another."""
        if self._is_busy():
            self._show_info(
                "Mixer Busy",
                "Please stop playback, recording, or bounce before clearing a channel.",
            )
            return

        if not self.channel_states[channel_idx].get_filepath():
            return

        self.channel_states[channel_idx].set_filepath(None)
        self.channel_states[channel_idx].reset_effects()
        self._channel_meters[channel_idx].reset()
        self._channel_peak_readouts[channel_idx].clear()
        well = self._ch_widgets[channel_idx]["file_well"]
        well.set_file_text("(click or drop .wav)")
        well.file_btn.setToolTip("Click to browse for a .wav file, or drag & drop one here")
        well.set_meta_text("—")
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
            self._show_error(
                "PyAudio Not Installed",
                f"Cannot start {action_name} because 'pyaudio' could not be loaded in this Python environment.{detail}\n\n"
                "Install PortAudio and PyAudio, e.g.:\n"
                "  brew install portaudio\n"
                "  pip install pyaudio\n\n"
                "(Note: Offline Bounce still works without PyAudio.)",
            )
            return False
        return True

    def press_play(self):
        if not self._require_pyaudio("playback"):
            return

        with self._state_lock:
            if self.playing:
                if self.paused:
                    self.paused = False
                    self._pause_event.set()
                    self._ui_call(self._set_pause_lit, False)
                    self._ui_call(self._set_play_lit, True)
                return
            if self.recording or self.bouncing:
                self._show_info(
                    "Mixer Busy",
                    "Please stop recording or wait for bounce to finish before playing.",
                )
                return

        loaded = [ch.get_filepath() for ch in self.channel_states if ch.get_filepath()]
        if not loaded:
            self._show_info(
                "No Audio Loaded",
                "There's no audio file opened to play. Please load at least one .wav file.",
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
                with self._seek_lock:
                    req_seek = self._pending_seek_sec
                    self._pending_seek_sec = None
                if req_seek is not None:
                    _apply_seek(req_seek)

                if not self._pause_event.is_set():
                    elapsed = int(frames_played / float(session_rate))
                    self._set_status(f"Paused {format_timecode(elapsed)}")
                    self._reset_meter_peaks()
                    try:
                        stream.stop_stream()
                    except Exception:
                        pass
                    while not self._pause_event.is_set() and not self._stop_event.is_set():
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
                    if _is_pcm_silent(pcm_bytes, 4):
                        print("Audio ended")
                        break

                if pcm_bytes:
                    stream.write(pcm_bytes)
                    frames_played += len(pcm_bytes) // 4
                    with self._seek_lock:
                        if self._pending_seek_sec is None and not self._stop_event.is_set():
                            self._playhead_sec = frames_played / float(session_rate)

                now = time.monotonic()
                if now - last_ui_update >= 0.25 and not self._stop_event.is_set():
                    elapsed = int(frames_played / float(session_rate))
                    self._set_status(f"Playing {format_timecode(elapsed)}")
                    last_ui_update = now

            if self._stop_event.is_set():
                with self._seek_lock:
                    self._playhead_sec = 0.0
                    self._pending_seek_sec = None
                self._ui_call(self.timeline.set_position, 0.0)
                self._set_status("Stopped")
            else:
                self._set_status("Playback finished")
                with self._seek_lock:
                    self._playhead_sec = 0.0
                self._ui_call(self.timeline.set_position, 0.0)

        except Exception as exc:
            print(f"Playback error: {exc}")
            self._set_status("Playback error")
            self._ui_call(self._show_error, "Playback Error", f"An error occurred during playback:\n{exc}")
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
            self._pause_event.set()
            self._ui_call(self._set_play_lit, False)
            self._ui_call(self._set_pause_lit, False)

    # ---------- Recording (Multithreaded, with Overdub Support) ----------

    def press_rec(self):
        if not self._require_pyaudio("recording"):
            return

        with self._state_lock:
            if self.recording:
                self._stop_event.set()
                self._set_status("Stopping recording...")
                return
            if self.playing or self.bouncing:
                self._show_info(
                    "Mixer Busy",
                    "Please stop playback or wait for bounce to finish before recording.",
                )
                return

        armed_indices = [idx for idx, ch in enumerate(self.channel_states) if ch.snapshot().rec_armed]
        if not armed_indices:
            self._show_info(
                "No Channel Armed",
                "Please arm at least one channel by clicking its '● Rec' pad before recording.",
            )
            return

        base_name = sanitize_filename(self.nameBox.text())
        if not base_name:
            base_name = f"recording_{time.strftime('%Y%m%d_%H%M%S')}"
        elif base_name.lower().endswith(".wav"):
            base_name = base_name[:-4] or f"recording_{time.strftime('%Y%m%d_%H%M%S')}"

        with self._state_lock:
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

            has_overdub = False
            for idx, ch in enumerate(self.channel_states):
                if idx in armed_indices:
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
            chunk_levels = [0.0, 0.0, 0.0, 0.0]
            master_levels = [0.0, 0.0]
            chunk_true_peaks = [0.0, 0.0, 0.0, 0.0]
            master_true_peaks = [0.0, 0.0]

            with self._rec_wave_lock:
                self._rec_wave_armed = list(armed_indices)
                self._rec_wave_peaks = []
                self._rec_wave_elapsed = 0.0
                self._rec_wave_active = True

            while not self._stop_event.is_set():
                raw_in = in_stream.read(self.buffer, exception_on_overflow=False)
                recorded_frames.append(raw_in)
                frames_recorded += len(raw_in) // 2

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
                    if any_data:
                        snapshots = [ch.snapshot() for ch in self.channel_states]
                        pcm_bytes = mix_stereo_chunk(
                            chunks,
                            snapshots,
                            session_rate,
                            out_levels=chunk_levels,
                            master_gain=self._get_master_volume(),
                            out_master_levels=master_levels,
                            out_peaks=chunk_true_peaks,
                            out_master_peaks=master_true_peaks,
                        )
                        if pcm_bytes:
                            out_stream.write(pcm_bytes)

                for idx in armed_indices:
                    chunk_levels[idx] = in_vu
                    chunk_true_peaks[idx] = in_pk
                self._set_meter_peaks(
                    chunk_levels, master_levels, chunk_true_peaks, master_true_peaks
                )

                with self._rec_wave_lock:
                    self._rec_wave_peaks.append(in_pk)
                    if len(self._rec_wave_peaks) > 1200:
                        self._rec_wave_peaks = [
                            max(self._rec_wave_peaks[k], self._rec_wave_peaks[k + 1])
                            for k in range(0, len(self._rec_wave_peaks) - 1, 2)
                        ]
                    self._rec_wave_elapsed = frames_recorded / float(session_rate)

                now = time.monotonic()
                if now - last_ui_update >= 0.25:
                    elapsed = int(frames_recorded / float(session_rate))
                    self._set_status(f"Recording {format_timecode(elapsed)}")
                    last_ui_update = now

            raw_all = b"".join(recorded_frames)
            saved_files = []
            for idx in armed_indices:
                filename = f"{base_name}_ch{idx + 1}.wav"
                out_path = os.path.join(BASE_DIR, filename)
                with wave.open(out_path, "wb") as wf_out:
                    wf_out.setnchannels(1)
                    wf_out.setsampwidth(2)
                    wf_out.setframerate(session_rate)
                    wf_out.writeframes(raw_all)
                self.channel_states[idx].set_filepath(out_path)
                well = self._ch_widgets[idx]["file_well"]
                self._ui_call(well.set_file_text, filename)
                self._ui_call(well.file_btn.setToolTip, out_path)
                self._ui_call(well.set_meta_text, format_audio_meta(session_rate, 16, 1))
                saved_files.append(filename)
                print(f"Saved recording to {out_path}")

            with self._rec_wave_lock:
                self._rec_wave_active = False
            self._ui_call(self.timeline.update_live_recording, False)
            self._update_session_duration()
            for idx in armed_indices:
                path = self.channel_states[idx].get_filepath()
                if path:
                    self._load_channel_waveform_async(idx, path)
            self._set_status(f"Saved: {', '.join(saved_files)}")

        except Exception as exc:
            print(f"Recording error: {exc}")
            self._set_status("Recording error")
            self._ui_call(self._show_error, "Recording Error", f"An error occurred while recording:\n{exc}")
        finally:
            with self._rec_wave_lock:
                self._rec_wave_active = False
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
            self._pause_event.set()
            self._reset_meter_peaks()
            self._ui_call(self._set_pause_lit, False)
            print("Stopped")
            self._set_status("Stopping...")
        with self._seek_lock:
            self._playhead_sec = 0.0
            self._pending_seek_sec = None
        self._ui_call(self.timeline.set_position, 0.0)

    # ---------- Offline Bounce / Export (Multithreaded) ----------

    def bounce(self):
        loaded = [ch.get_filepath() for ch in self.channel_states if ch.get_filepath()]
        if not loaded:
            self._show_info("No Audio Loaded", "Load at least one .wav file before bouncing.")
            return

        if self._is_busy():
            self._show_info("Mixer Busy", "Please stop playback/recording before bouncing.")
            return

        entry_name = sanitize_filename(self.nameBox.text())
        if entry_name:
            if not entry_name.lower().endswith(".wav"):
                entry_name += ".wav"
            out_path = os.path.join(BASE_DIR, entry_name)
        else:
            out_path, _ = QFileDialog.getSaveFileName(
                self.root,
                "Save Bounced Mix",
                os.path.join(BASE_DIR, "bounced_mix.wav"),
                "WAV files (*.wav)",
            )
            if not out_path:
                return
            if not out_path.lower().endswith(".wav"):
                out_path += ".wav"

        busy_now = False
        with self._state_lock:
            if self.playing or self.recording or self.bouncing:
                busy_now = True
            else:
                self.bouncing = True
                self._stop_event.clear()

        if busy_now:
            self._show_info("Mixer Busy", "Please stop playback/recording before bouncing.")
            return

        self._set_status("Bouncing... 0%")
        self._ui_call(self._set_bounce_progress, 0.0, "Bouncing... 0%", "#48d18a")
        t = threading.Thread(
            target=self._bounce_worker,
            args=(out_path, self.bounce_bitdepth, self.bounce_dither),
            name="MixerBounceThread",
            daemon=True,
        )
        self._active_thread = t
        t.start()

    def _bounce_worker(self, out_path, bounce_bit_depth=None, bounce_dither=None):
        wav_handles = [None] * 4
        readers = [None] * 4
        use_c = _dsp is not None and _dsp.AVAILABLE
        chunk_size = 65536 if use_c else 4096
        tail_step = 4096
        bit_depth = int(bounce_bit_depth if bounce_bit_depth is not None else getattr(self, "bounce_bitdepth", self.bitdepth))
        if bit_depth not in (16, 24, 32):
            bit_depth = 16
        sampwidth = bit_depth // 8
        use_dither = bool(bounce_dither if bounce_dither is not None else getattr(self, "bounce_dither", bit_depth == 16)) and (bit_depth < 32)
        dither_rng = ctypes.c_uint64((time.time_ns() ^ 0xA0761D6478BD642F) & 0xFFFFFFFFFFFFFFFF or 1)

        try:
            dither_tag = "TPDF Dither" if use_dither else "No Dither"
            print(f"Bouncing {bit_depth}-bit ({dither_tag}) audio to {out_path}...")
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
            tail_peaks = [0.0, 0.0]
            frame_bytes = 2 * sampwidth

            cancelled = False
            fx_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="BounceFX") if use_c else None
            try:
                with wave.open(out_path, "wb") as bounce_wf:
                    bounce_wf.setnchannels(2)
                    bounce_wf.setsampwidth(sampwidth)
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
                            out_master_peaks=tail_peaks if in_tail else None,
                            fx_executor=fx_pool,
                            out_bit_depth=bit_depth,
                            dither_tpdf=use_dither,
                            dither_rng=dither_rng,
                        )
                        if in_tail and tail_frames_emitted > min_tail_frames and pcm_bytes:
                            if max(tail_peaks[0], tail_peaks[1]) < 1.2e-4:
                                break
                        if pcm_bytes:
                            bounce_wf.writeframes(pcm_bytes)
                            frames_written += len(pcm_bytes) // frame_bytes

                            now = time.monotonic()
                            frac = min(0.99, frames_written / float(total_frames))
                            pct = int(frac * 100)
                            if (pct - last_prog_pct >= 2 or now - last_prog_time >= 0.05) and pct != last_prog_pct:
                                last_prog_pct = pct
                                last_prog_time = now
                                self._set_status(f"Bouncing... {pct}%")
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
                self._ui_call(self._set_bounce_progress, 0.0, "Bounce cancelled", "#f5c542")
                self._set_status("Bounce cancelled")
            else:
                meta_txt = f"{format_audio_meta(int(session_rate), bit_depth, 2)} · {dither_tag}"
                print(f"Bounce completed successfully ({meta_txt})")
                self._ui_call(self._set_bounce_progress, 1.0, f"Bounce complete ({bit_depth}-bit)", "#7ef2b8")
                self._set_status(f"Bounced ({bit_depth}-bit): {os.path.basename(out_path)}")
                self._ui_call(
                    self._show_info,
                    "Bounce Complete",
                    f"Mix successfully exported to:\n{out_path}\n\nFormat: {meta_txt}",
                )

        except Exception as exc:
            print(f"Bounce error: {exc}")
            self._ui_call(self._set_bounce_progress, 0.0, "Bounce failed", "#fc8181")
            self._set_status("Bounce error")
            self._ui_call(self._show_error, "Bounce Error", f"Could not export mix:\n{exc}")
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

    def _shutdown_resources(self):
        if self._closing:
            return
        self._closing = True
        self._app.removeEventFilter(self.root)
        self._meter_timer.stop()
        self._ui_bridge.shutdown()
        self._stop_event.set()
        self._pause_event.set()

        for popup in list(self._effect_windows.values()):
            try:
                popup.close()
            except Exception:
                pass
        self._effect_windows.clear()

        t = self._active_thread
        if t is not None and t.is_alive():
            t.join(timeout=1.5)

    def on_close(self):
        self._shutdown_resources()
        self.root.close()


def Main():
    Window()
    return 0


if __name__ == "__main__":
    Main()
