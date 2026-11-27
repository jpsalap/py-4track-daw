"""
Universal stdlib-only WAV reader and writer for Mini-DAW.
Supports integer PCM (8/16/24/32-bit, format tag 1) as well as IEEE-754
floating-point WAV (32-bit & 64-bit, format tag 3) and WAVE_FORMAT_EXTENSIBLE (0xFFFE).
Exposes a drop-in wave.Wave_read / wave.Wave_write compatible interface.
"""
import struct
import wave

WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

_GUID_TAIL = b"\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"


class WavReader:
    """Drop-in replacement for wave.Wave_read supporting integer PCM and IEEE Float WAVs."""

    def __init__(self, filepath):
        self._fp = open(filepath, "rb")
        try:
            self._parse_headers()
        except Exception:
            self._fp.close()
            raise

    def _parse_headers(self):
        fp = self._fp
        riff = fp.read(12)
        if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
            raise wave.Error("file does not start with RIFF/WAVE id")

        fmt_parsed = False
        data_found = False
        self._nchannels = 0
        self._framerate = 0
        self._sampwidth = 0
        self._sample_format = "pcm"
        self._data_offset = 0
        self._data_size = 0

        fp.seek(0, 2)
        file_size = fp.tell()
        fp.seek(12)

        while fp.tell() + 8 <= file_size:
            hdr = fp.read(8)
            if len(hdr) < 8:
                break
            chunk_id, chunk_size = struct.unpack("<4sI", hdr)
            chunk_start = fp.tell()

            if chunk_id == b"fmt ":
                if chunk_size < 16:
                    raise wave.Error("invalid fmt chunk size")
                fmt_bytes = fp.read(chunk_size)
                if len(fmt_bytes) < 16:
                    raise wave.Error("truncated fmt chunk")
                audio_fmt, nch, fr, _byte_rate, _block_align, bits = struct.unpack_from(
                    "<HHIIHH", fmt_bytes, 0
                )
                if audio_fmt == WAVE_FORMAT_EXTENSIBLE:
                    if len(fmt_bytes) < 40:
                        raise wave.Error("truncated WAVE_FORMAT_EXTENSIBLE fmt chunk")
                    sub_tag = struct.unpack_from("<I", fmt_bytes, 24)[0]
                    sub_tail = fmt_bytes[28:40]
                    if sub_tail != _GUID_TAIL:
                        raise wave.Error("unsupported WAVE_FORMAT_EXTENSIBLE SubFormat GUID")
                    audio_fmt = sub_tag

                if audio_fmt == WAVE_FORMAT_PCM:
                    self._sample_format = "pcm"
                    if bits not in (8, 16, 24, 32):
                        raise ValueError(f"Unsupported PCM WAV bit depth: {bits}-bit")
                elif audio_fmt == WAVE_FORMAT_IEEE_FLOAT:
                    self._sample_format = "float"
                    if bits not in (32, 64):
                        raise ValueError(f"Unsupported IEEE Float WAV bit depth: {bits}-bit")
                else:
                    raise wave.Error(f"unsupported WAV format tag: {audio_fmt}")

                if nch <= 0 or fr <= 0:
                    raise ValueError(f"Invalid WAV header (channels={nch}, rate={fr})")

                self._nchannels = int(nch)
                self._framerate = int(fr)
                self._sampwidth = int(bits // 8)
                fmt_parsed = True

            elif chunk_id == b"data":
                self._data_offset = chunk_start
                avail_bytes = max(0, file_size - chunk_start)
                self._data_size = min(chunk_size, avail_bytes)
                data_found = True
                if fmt_parsed:
                    break

            next_pos = chunk_start + chunk_size + (chunk_size & 1)
            if next_pos <= chunk_start or next_pos > file_size:
                break
            fp.seek(next_pos)

        if not fmt_parsed:
            raise wave.Error("fmt chunk not found")
        if not data_found:
            raise wave.Error("data chunk not found")

        self._frame_bytes = self._nchannels * self._sampwidth
        if self._frame_bytes <= 0:
            raise wave.Error("invalid frame size")
        self._nframes = self._data_size // self._frame_bytes
        self._pos = 0
        self._fp.seek(self._data_offset)

    def getnchannels(self):
        return self._nchannels

    def getsampwidth(self):
        return self._sampwidth

    def getframerate(self):
        return self._framerate

    def getnframes(self):
        return self._nframes

    def getsampleformat(self):
        return self._sample_format

    def tell(self):
        return self._pos

    def setpos(self, pos):
        pos_i = int(pos)
        if pos_i < 0 or pos_i > self._nframes:
            raise wave.Error("position not in range")
        self._pos = pos_i
        self._fp.seek(self._data_offset + self._pos * self._frame_bytes)

    def readframes(self, nframes):
        if nframes <= 0 or self._pos >= self._nframes:
            return b""
        remaining = self._nframes - self._pos
        to_read = min(int(nframes), remaining)
        raw = self._fp.read(to_read * self._frame_bytes)
        actual_frames = len(raw) // self._frame_bytes
        self._pos += actual_frames
        usable = actual_frames * self._frame_bytes
        return raw[:usable] if len(raw) != usable else raw

    def close(self):
        if self._fp is not None:
            try:
                self._fp.close()
            finally:
                self._fp = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class _Float32WavWriter:
    """Spec-compliant 32-bit IEEE-754 floating-point WAV writer (format tag 3 + fact chunk)."""

    def __init__(self, filepath, channels, sample_rate):
        self._channels = int(channels)
        self._sample_rate = int(sample_rate)
        self._sampwidth = 4
        self._frame_bytes = self._channels * self._sampwidth
        self._bytes_written = 0
        self._fp = open(filepath, "wb")
        try:
            self._write_header(0, 0)
        except Exception:
            self._fp.close()
            self._fp = None
            raise

    def _write_header(self, data_size, nframes):
        byte_rate = self._sample_rate * self._frame_bytes
        block_align = self._frame_bytes
        # 4 ("WAVE") + 26 (fmt chunk) + 12 (fact chunk) + 8 (data header) + data_size (+ pad)
        pad = data_size & 1
        riff_size = 4 + 26 + 12 + 8 + data_size + pad
        header = struct.pack(
            "<4sI4s4sIHHIIHHH4sII4sI",
            b"RIFF",
            riff_size,
            b"WAVE",
            b"fmt ",
            18,
            WAVE_FORMAT_IEEE_FLOAT,
            self._channels,
            self._sample_rate,
            byte_rate,
            block_align,
            32,
            0,
            b"fact",
            4,
            nframes,
            b"data",
            data_size,
        )
        self._fp.seek(0)
        self._fp.write(header)

    def writeframes(self, raw_bytes):
        if not raw_bytes:
            return
        self._fp.write(raw_bytes)
        self._bytes_written += len(raw_bytes)

    def close(self):
        if self._fp is not None:
            try:
                if self._bytes_written & 1:
                    self._fp.write(b"\x00")
                nframes = self._bytes_written // self._frame_bytes
                self._write_header(self._bytes_written, nframes)
                self._fp.flush()
            finally:
                self._fp.close()
                self._fp = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def open_wav_read(filepath):
    """Open a WAV file for reading (integer PCM 8/16/24/32-bit or IEEE Float 32/64-bit)."""
    return WavReader(filepath)


def open_wav_write(filepath, channels, sample_rate, bit_depth=16, sample_format="pcm"):
    """Open a WAV file for writing (integer PCM 16/24/32-bit or IEEE Float 32-bit)."""
    if sample_format == "float":
        return _Float32WavWriter(filepath, channels, sample_rate)
    wf = wave.open(filepath, "wb")
    wf.setnchannels(int(channels))
    wf.setsampwidth(int(bit_depth) // 8)
    wf.setframerate(int(sample_rate))
    return wf
