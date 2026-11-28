"""
Stereo-linked zero-latency Master Bus Safety Limiter for Mini-DAW.
Operates in IEEE-754 float64 with optional native C acceleration and
bit-exact pure-Python fallback.
"""
import ctypes
import math
import threading

try:
    import dsp_accel as _dsp
except Exception:
    _dsp = None


class MasterLimiterProcessor:
    """Thread-safe stereo-linked peak brickwall limiter for the Master Bus."""

    DEFAULT_CEILING_DB = -0.3
    DEFAULT_RELEASE_MS = 60.0

    def __init__(self, ceiling_db=DEFAULT_CEILING_DB, release_ms=DEFAULT_RELEASE_MS):
        self._lock = threading.Lock()
        self._proc_lock = threading.Lock()
        self._ceiling_db = float(ceiling_db)
        self._release_ms = float(release_ms)
        self._env = 0.0

    def set_params(self, ceiling_db=None, release_ms=None):
        with self._lock:
            if ceiling_db is not None:
                self._ceiling_db = max(-6.0, min(0.0, float(ceiling_db)))
            if release_ms is not None:
                self._release_ms = max(10.0, min(500.0, float(release_ms)))

    def get_params(self):
        with self._lock:
            return self._ceiling_db, self._release_ms

    def reset(self):
        with self._proc_lock:
            self._env = 0.0

    def clone_for_offline(self):
        ceiling_db, release_ms = self.get_params()
        return MasterLimiterProcessor(ceiling_db=ceiling_db, release_ms=release_ms)

    def process_stereo_inplace(self, left, right, n, sample_rate, pre_gain=1.0):
        """Scale by pre_gain and limit stereo float64 buffers in place."""
        if n <= 0:
            return
        gain = float(pre_gain)
        with self._proc_lock:
            ceiling_db, release_ms = self.get_params()
            if _dsp is not None and _dsp.AVAILABLE and getattr(_dsp, "HAS_LIMITER", False):
                l_arr = _dsp.as_f64_array(left)
                r_arr = _dsp.as_f64_array(right)
                env_c = ctypes.c_double(self._env)
                _dsp.LIB.dsp_master_limiter_inplace(
                    _dsp.f64_ptr(l_arr),
                    _dsp.f64_ptr(r_arr),
                    n,
                    float(sample_rate),
                    gain,
                    ceiling_db,
                    release_ms,
                    ctypes.byref(env_c),
                )
                self._env = env_c.value
                if l_arr is not left:
                    left[:n] = l_arr[:n]
                if r_arr is not right:
                    right[:n] = r_arr[:n]
                return

            sr = max(1.0, float(sample_rate))
            rel_samples = max(1.0, release_ms * 0.001 * sr)
            release_c = math.exp(-1.0 / rel_samples)
            one_minus_rel = 1.0 - release_c
            ceiling_lin = 10.0 ** (ceiling_db / 20.0)
            env = self._env

            if _dsp is not None and _dsp.USE_NUMPY:
                np = _dsp.NP
                lv = np.asarray(left[:n], dtype=np.float64) * gain
                rv = np.asarray(right[:n], dtype=np.float64) * gain
                lv = np.where(np.isfinite(lv), lv, 0.0)
                rv = np.where(np.isfinite(rv), rv, 0.0)
                pk_list = np.maximum(np.abs(lv), np.abs(rv)).tolist()
                env_list = [0.0] * n
                for i in range(n):
                    pk = pk_list[i]
                    if pk > env:
                        env = pk
                    else:
                        env = release_c * env + one_minus_rel * pk
                    env_list[i] = env
                self._env = env

                env_arr = np.asarray(env_list, dtype=np.float64)
                above = (env_arr > ceiling_lin) & (env_arr > 1e-12)
                if np.any(above):
                    g = np.ones(n, dtype=np.float64)
                    g[above] = ceiling_lin / env_arr[above]
                    lv = lv * g
                    rv = rv * g
                if isinstance(left, np.ndarray):
                    left[:n] = lv
                else:
                    left[:n] = lv.tolist()
                if isinstance(right, np.ndarray):
                    right[:n] = rv
                else:
                    right[:n] = rv.tolist()
                return

            for i in range(n):
                lv = left[i] * gain
                rv = right[i] * gain
                if not math.isfinite(lv):
                    lv = 0.0
                if not math.isfinite(rv):
                    rv = 0.0
                al = lv if lv >= 0.0 else -lv
                ar = rv if rv >= 0.0 else -rv
                pk = al if al >= ar else ar
                if pk > env:
                    env = pk
                else:
                    env = release_c * env + one_minus_rel * pk

                if env > ceiling_lin and env > 1e-12:
                    g = ceiling_lin / env
                    left[i] = lv * g
                    right[i] = rv * g
                else:
                    left[i] = lv
                    right[i] = rv

            self._env = env
