"""
Optional native C DSP accelerator loader (libminidaw_dsp.dylib / .so / .dll).
Provides zero-copy ctypes bindings over array.array('d') / array.array('h')
with graceful fallback to None when the shared library is unavailable.
"""
import array
import ctypes
import os
import sys

_c_double_p = ctypes.POINTER(ctypes.c_double)
_c_int16_p = ctypes.POINTER(ctypes.c_int16)
_c_uint8_p = ctypes.POINTER(ctypes.c_uint8)
_c_uint64_p = ctypes.POINTER(ctypes.c_uint64)
_c_size_p = ctypes.POINTER(ctypes.c_size_t)


def _candidate_lib_paths():
    if sys.platform == "darwin":
        names = ("libminidaw_dsp.dylib",)
    elif sys.platform == "win32":
        names = ("minidaw_dsp.dll", "libminidaw_dsp.dll")
    else:
        names = ("libminidaw_dsp.so",)

    dirs = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs.append(meipass)
        exe_dir = os.path.dirname(sys.executable)
        dirs.append(exe_dir)
        dirs.append(os.path.normpath(os.path.join(exe_dir, "..", "Frameworks")))
        dirs.append(os.path.normpath(os.path.join(exe_dir, "..", "Resources")))
    dirs.append(os.path.dirname(os.path.abspath(__file__)))

    paths = []
    for d in dirs:
        for name in names:
            paths.append(os.path.join(d, name))
    return paths


def _load_library():
    for path in _candidate_lib_paths():
        if not os.path.exists(path):
            continue
        try:
            lib = ctypes.CDLL(path)

            lib.dsp_decode_pcm_to_mono_f64.argtypes = [
                ctypes.c_char_p,
                ctypes.c_size_t,
                ctypes.c_int,
                ctypes.c_int,
                _c_double_p,
            ]
            lib.dsp_decode_pcm_to_mono_f64.restype = ctypes.c_size_t

            lib.dsp_resample_linear_f64.argtypes = [
                _c_double_p,
                ctypes.c_size_t,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_size_t,
                _c_double_p,
                _c_double_p,
            ]
            lib.dsp_resample_linear_f64.restype = ctypes.c_size_t

            lib.dsp_compressor_process.argtypes = [
                _c_double_p,
                _c_double_p,
                ctypes.c_size_t,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_double,
                _c_double_p,
            ]
            lib.dsp_compressor_process.restype = None

            lib.dsp_chorus_process.argtypes = [
                _c_double_p,
                _c_double_p,
                ctypes.c_size_t,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_double,
                _c_double_p,
                ctypes.c_size_t,
                _c_size_p,
                _c_double_p,
            ]
            lib.dsp_chorus_process.restype = None

            lib.dsp_tremolo_process.argtypes = [
                _c_double_p,
                _c_double_p,
                ctypes.c_size_t,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_double,
                _c_double_p,
            ]
            lib.dsp_tremolo_process.restype = None

            lib.dsp_delay_process.argtypes = [
                _c_double_p,
                _c_double_p,
                ctypes.c_size_t,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_double,
                _c_double_p,
                ctypes.c_size_t,
                _c_size_p,
            ]
            lib.dsp_delay_process.restype = None

            lib.dsp_mix_channel_accum.argtypes = [
                _c_double_p,
                ctypes.c_size_t,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_double,
                _c_double_p,
                _c_double_p,
                ctypes.c_int,
                _c_double_p,
                _c_double_p,
            ]
            lib.dsp_mix_channel_accum.restype = None

            lib.dsp_master_pack_i16.argtypes = [
                _c_double_p,
                _c_double_p,
                ctypes.c_size_t,
                ctypes.c_double,
                _c_int16_p,
                ctypes.c_int,
                _c_double_p,
                _c_double_p,
                _c_double_p,
                _c_double_p,
            ]
            lib.dsp_master_pack_i16.restype = None

            lib.dsp_master_pack_pcm.argtypes = [
                _c_double_p,
                _c_double_p,
                ctypes.c_size_t,
                ctypes.c_double,
                ctypes.c_int,
                ctypes.c_int,
                _c_uint64_p,
                _c_uint8_p,
                ctypes.c_int,
                _c_double_p,
                _c_double_p,
                _c_double_p,
                _c_double_p,
            ]
            lib.dsp_master_pack_pcm.restype = None

            lib.dsp_is_silent_i16.argtypes = [
                ctypes.c_char_p,
                ctypes.c_size_t,
                ctypes.c_int,
            ]
            lib.dsp_is_silent_i16.restype = ctypes.c_int

            return lib
        except (OSError, AttributeError):
            continue
    return None


LIB = _load_library()
AVAILABLE = LIB is not None


def as_f64_array(samples):
    """Return samples as array.array('d') without copying if already one."""
    if isinstance(samples, array.array) and samples.typecode == "d":
        return samples
    return array.array("d", samples)


def f64_ptr(arr):
    """Return ctypes POINTER(c_double) to an array.array('d') buffer."""
    return ctypes.cast(arr.buffer_info()[0], _c_double_p)


def i16_ptr(arr):
    """Return ctypes POINTER(c_int16) to an array.array('h') buffer."""
    return ctypes.cast(arr.buffer_info()[0], _c_int16_p)


def alloc_f64(n):
    """Fast zero-initialized array.array('d') of length n."""
    out = array.array("d")
    out.frombytes(bytes(n * 8))
    return out


def alloc_i16(n):
    """Fast zero-initialized array.array('h') of length n."""
    out = array.array("h")
    out.frombytes(bytes(n * 2))
    return out


def alloc_u8(n):
    """Fast zero-initialized array.array('B') of length n."""
    out = array.array("B")
    out.frombytes(bytes(n))
    return out


def u8_ptr(arr):
    """Return ctypes POINTER(c_uint8) to an array.array('B') buffer."""
    return ctypes.cast(arr.buffer_info()[0], _c_uint8_p)
