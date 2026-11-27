/*
 * Mini-DAW Native C DSP Accelerator
 * Compiled to libminidaw_dsp.dylib and loaded via Python ctypes with automatic
 * pure-Python fallback. Uses IEEE-754 double precision to match Python DSP math.
 */
#include <math.h>
#include <stddef.h>
#include <stdint.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define SQRT2_VAL 1.4142135623730951

/* Decode 8/16/24/32-bit little-endian PCM WAV bytes to mono double [-1.0, 1.0]. */
size_t dsp_decode_pcm_to_mono_f64(
    const uint8_t *raw,
    size_t nbytes,
    int sampwidth,
    int nchannels,
    double *out
) {
    if (!raw || !out || nbytes == 0 || nchannels <= 0) {
        return 0;
    }

    size_t frame_bytes = (size_t)sampwidth * (size_t)nchannels;
    if (frame_bytes == 0) {
        return 0;
    }
    size_t n_frames = nbytes / frame_bytes;

    if (sampwidth == 2) {
        const double scale = 1.0 / 32768.0;
        const int16_t *pcm = (const int16_t *)raw;
        if (nchannels == 1) {
            for (size_t i = 0; i < n_frames; ++i) {
                out[i] = (double)pcm[i] * scale;
            }
        } else if (nchannels == 2) {
            const double inv_ch = scale * 0.5;
            for (size_t i = 0; i < n_frames; ++i) {
                size_t j = i * 2;
                out[i] = (double)((int32_t)pcm[j] + (int32_t)pcm[j + 1]) * inv_ch;
            }
        } else {
            const double inv_ch = scale / (double)nchannels;
            for (size_t f = 0; f < n_frames; ++f) {
                size_t base = f * (size_t)nchannels;
                int64_t total = 0;
                for (int c = 0; c < nchannels; ++c) {
                    total += (int64_t)pcm[base + (size_t)c];
                }
                out[f] = (double)total * inv_ch;
            }
        }
        return n_frames;
    }

    if (sampwidth == 1) {
        const double scale = 1.0 / 128.0;
        if (nchannels == 1) {
            for (size_t i = 0; i < n_frames; ++i) {
                out[i] = (double)((int)raw[i] - 128) * scale;
            }
        } else {
            const double inv_ch = scale / (double)nchannels;
            for (size_t f = 0; f < n_frames; ++f) {
                size_t base = f * (size_t)nchannels;
                int64_t total = 0;
                for (int c = 0; c < nchannels; ++c) {
                    total += (int64_t)((int)raw[base + (size_t)c] - 128);
                }
                out[f] = (double)total * inv_ch;
            }
        }
        return n_frames;
    }

    if (sampwidth == 3) {
        const double inv_ch = (1.0 / 8388608.0) / (double)nchannels;
        for (size_t f = 0; f < n_frames; ++f) {
            size_t base = f * frame_bytes;
            int64_t total = 0;
            for (int c = 0; c < nchannels; ++c) {
                size_t off = base + (size_t)c * 3;
                int32_t val = (int32_t)raw[off]
                            | ((int32_t)raw[off + 1] << 8)
                            | ((int32_t)raw[off + 2] << 16);
                if (val & 0x800000) {
                    val -= 0x1000000;
                }
                total += (int64_t)val;
            }
            out[f] = (double)total * inv_ch;
        }
        return n_frames;
    }

    if (sampwidth == 4) {
        const double scale = 1.0 / 2147483648.0;
        const int32_t *pcm = (const int32_t *)raw;
        if (nchannels == 1) {
            for (size_t i = 0; i < n_frames; ++i) {
                out[i] = (double)pcm[i] * scale;
            }
        } else {
            const double inv_ch = scale / (double)nchannels;
            for (size_t f = 0; f < n_frames; ++f) {
                size_t base = f * (size_t)nchannels;
                int64_t total = 0;
                for (int c = 0; c < nchannels; ++c) {
                    total += (int64_t)pcm[base + (size_t)c];
                }
                out[f] = (double)total * inv_ch;
            }
        }
        return n_frames;
    }

    return 0;
}

/* Phase-accurate streaming linear resampler. */
size_t dsp_resample_linear_f64(
    const double *buf,
    size_t avail,
    double pos,
    double step,
    size_t max_out,
    double *out,
    double *out_pos
) {
    if (!buf || !out || avail == 0 || max_out == 0) {
        if (out_pos) {
            *out_pos = pos;
        }
        return 0;
    }

    size_t produced = 0;
    size_t last = avail - 1;
    for (size_t k = 0; k < max_out; ++k) {
        size_t i0 = (size_t)pos;
        if (i0 >= avail) {
            break;
        }
        if (i0 < last) {
            double frac = pos - (double)i0;
            out[produced++] = buf[i0] * (1.0 - frac) + buf[i0 + 1] * frac;
        } else {
            out[produced++] = buf[last];
        }
        pos += step;
    }

    if (out_pos) {
        *out_pos = pos;
    }
    return produced;
}

/* Peak dynamic range compressor with attack/release envelope and makeup gain. */
void dsp_compressor_process(
    const double *in,
    double *out,
    size_t n,
    double sample_rate,
    double threshold_db,
    double ratio,
    double attack_ms,
    double release_ms,
    double makeup_db,
    double *env_io
) {
    if (!in || !out || !env_io || n == 0) {
        return;
    }

    double att_samples = attack_ms * 0.001 * sample_rate;
    double rel_samples = release_ms * 0.001 * sample_rate;
    if (att_samples < 1.0) att_samples = 1.0;
    if (rel_samples < 1.0) rel_samples = 1.0;

    double attack_c = exp(-1.0 / att_samples);
    double release_c = exp(-1.0 / rel_samples);
    double one_minus_att = 1.0 - attack_c;
    double one_minus_rel = 1.0 - release_c;

    double thresh_lin = pow(10.0, threshold_db / 20.0);
    double inv_thresh = 1.0 / (thresh_lin > 1e-9 ? thresh_lin : 1e-9);
    double slope = (1.0 / ratio) - 1.0;
    double makeup_lin = pow(10.0, makeup_db / 20.0);
    double env = *env_io;

    for (size_t i = 0; i < n; ++i) {
        double x = in[i];
        double ax = x >= 0.0 ? x : -x;
        if (ax > env) {
            env = attack_c * env + one_minus_att * ax;
        } else {
            env = release_c * env + one_minus_rel * ax;
        }

        double gain = makeup_lin;
        if (env > thresh_lin && slope < 0.0) {
            gain = pow(env * inv_thresh, slope) * makeup_lin;
        }
        out[i] = x * gain;
    }

    *env_io = env;
}

/* Modulated delay-line chorus with fractional linear interpolation. */
void dsp_chorus_process(
    const double *in,
    double *out,
    size_t n,
    double sample_rate,
    double rate,
    double depth_ms,
    double base_delay_ms,
    double mix,
    double *buf,
    size_t buf_len,
    size_t *write_idx_io,
    double *phase_io
) {
    if (!in || !out || !buf || !write_idx_io || !phase_io || n == 0 || buf_len < 3) {
        return;
    }

    size_t write_idx = *write_idx_io;
    double phase = *phase_io;
    const double two_pi = 2.0 * M_PI;
    const double phase_inc = two_pi * rate / sample_rate;
    const double ms_to_samples = sample_rate * 0.001;
    const double base_samples = base_delay_ms * ms_to_samples;
    const double depth_samples = depth_ms * ms_to_samples;
    const double max_d = (double)(buf_len - 2);
    const double dry = 1.0 - mix;
    const double buf_len_d = (double)buf_len;

    for (size_t i = 0; i < n; ++i) {
        double x = in[i];
        buf[write_idx] = x;

        double d = base_samples + depth_samples * sin(phase);
        if (d < 1.0) {
            d = 1.0;
        } else if (d > max_d) {
            d = max_d;
        }

        double read_pos = (double)write_idx - d;
        if (read_pos < 0.0) {
            read_pos += buf_len_d;
        }

        size_t idx0 = (size_t)read_pos;
        double frac;
        if (idx0 >= buf_len) {
            idx0 = 0;
            frac = 0.0;
        } else {
            frac = read_pos - (double)idx0;
        }
        size_t idx1 = idx0 + 1;
        if (idx1 >= buf_len) {
            idx1 = 0;
        }

        double wet = buf[idx0] * (1.0 - frac) + buf[idx1] * frac;
        out[i] = x * dry + wet * mix;

        phase += phase_inc;
        if (phase >= two_pi) {
            phase -= two_pi;
        }

        write_idx += 1;
        if (write_idx >= buf_len) {
            write_idx = 0;
        }
    }

    *write_idx_io = write_idx;
    *phase_io = phase;
}

/* Continuous-phase LFO tremolo. */
void dsp_tremolo_process(
    const double *in,
    double *out,
    size_t n,
    double sample_rate,
    double rate,
    double depth,
    double *phase_io
) {
    if (!in || !out || !phase_io || n == 0) {
        return;
    }

    const double two_pi = 2.0 * M_PI;
    const double phase_inc = two_pi * rate / sample_rate;
    double phase = *phase_io;

    if (depth <= 0.0) {
        for (size_t i = 0; i < n; ++i) {
            out[i] = in[i];
        }
        *phase_io = fmod(phase + phase_inc * (double)n, two_pi);
        return;
    }

    const double base = 1.0 - depth;
    const double half_depth = 0.5 * depth;

    for (size_t i = 0; i < n; ++i) {
        double gain = base + half_depth * (1.0 + sin(phase));
        out[i] = in[i] * gain;
        phase += phase_inc;
        if (phase >= two_pi) {
            phase -= two_pi;
        }
    }

    *phase_io = phase;
}

/* Feedback ring-buffer delay. */
void dsp_delay_process(
    const double *in,
    double *out,
    size_t n,
    double sample_rate,
    double delay_ms,
    double feedback,
    double mix,
    double *buf,
    size_t buf_len,
    size_t *write_idx_io
) {
    if (!in || !out || !buf || !write_idx_io || n == 0 || buf_len < 2) {
        return;
    }

    size_t write_idx = *write_idx_io;
    long delay_samples_l = (long)(delay_ms * sample_rate * 0.001);
    if (delay_samples_l < 1) {
        delay_samples_l = 1;
    } else if ((size_t)delay_samples_l >= buf_len) {
        delay_samples_l = (long)(buf_len - 1);
    }
    size_t delay_samples = (size_t)delay_samples_l;
    const double dry = 1.0 - mix;

    for (size_t i = 0; i < n; ++i) {
        double x = in[i];
        size_t read_idx = (write_idx >= delay_samples)
            ? (write_idx - delay_samples)
            : (write_idx + buf_len - delay_samples);

        double delayed = buf[read_idx];
        double fb_val = x + delayed * feedback;
        if (fb_val > 1.5) {
            fb_val = 1.5;
        } else if (fb_val < -1.5) {
            fb_val = -1.5;
        }
        buf[write_idx] = fb_val;
        out[i] = x * dry + delayed * mix;

        write_idx += 1;
        if (write_idx >= buf_len) {
            write_idx = 0;
        }
    }

    *write_idx_io = write_idx;
}

/* Accumulate a mono channel into stereo master buffers + optional VU/peak metering. */
void dsp_mix_channel_accum(
    const double *samples,
    size_t n,
    double l_gain,
    double r_gain,
    double abs_gain,
    double *master_left,
    double *master_right,
    int compute_meters,
    double *out_vu,
    double *out_peak
) {
    if (!samples || !master_left || !master_right || n == 0) {
        return;
    }

    if (compute_meters) {
        double sum_sq = 0.0;
        double ch_peak = 0.0;
        for (size_t j = 0; j < n; ++j) {
            double s = samples[j];
            sum_sq += s * s;
            double a = s >= 0.0 ? s : -s;
            if (a > ch_peak) {
                ch_peak = a;
            }
            master_left[j] += s * l_gain;
            master_right[j] += s * r_gain;
        }
        if (out_vu) {
            *out_vu = sqrt(sum_sq / (double)n) * SQRT2_VAL * abs_gain;
        }
        if (out_peak) {
            *out_peak = ch_peak * abs_gain;
        }
    } else {
        for (size_t j = 0; j < n; ++j) {
            double s = samples[j];
            master_left[j] += s * l_gain;
            master_right[j] += s * r_gain;
        }
    }
}

/* Apply master gain, meter L/R VU & pre-clip peaks, clip [-1, 1], pack int16 stereo PCM. */
void dsp_master_pack_i16(
    const double *master_left,
    const double *master_right,
    size_t n,
    double master_gain,
    int16_t *stereo_out,
    int compute_meters,
    double *out_vu_l,
    double *out_vu_r,
    double *out_pk_l,
    double *out_pk_r
) {
    if (!master_left || !master_right || !stereo_out || n == 0) {
        return;
    }

    if (compute_meters) {
        double l_sq = 0.0;
        double r_sq = 0.0;
        double l_pk = 0.0;
        double r_pk = 0.0;
        for (size_t j = 0; j < n; ++j) {
            double lv = master_left[j] * master_gain;
            double rv = master_right[j] * master_gain;
            l_sq += lv * lv;
            r_sq += rv * rv;
            double al = lv >= 0.0 ? lv : -lv;
            double ar = rv >= 0.0 ? rv : -rv;
            if (al > l_pk) l_pk = al;
            if (ar > r_pk) r_pk = ar;

            if (lv > 1.0) lv = 1.0;
            else if (lv < -1.0) lv = -1.0;
            if (rv > 1.0) rv = 1.0;
            else if (rv < -1.0) rv = -1.0;

            stereo_out[j * 2] = (int16_t)(lv * 32767.0);
            stereo_out[j * 2 + 1] = (int16_t)(rv * 32767.0);
        }
        double inv_n = 1.0 / (double)n;
        if (out_vu_l) *out_vu_l = sqrt(l_sq * inv_n) * SQRT2_VAL;
        if (out_vu_r) *out_vu_r = sqrt(r_sq * inv_n) * SQRT2_VAL;
        if (out_pk_l) *out_pk_l = l_pk;
        if (out_pk_r) *out_pk_r = r_pk;
    } else {
        for (size_t j = 0; j < n; ++j) {
            double lv = master_left[j] * master_gain;
            if (lv > 1.0) lv = 1.0;
            else if (lv < -1.0) lv = -1.0;
            double rv = master_right[j] * master_gain;
            if (rv > 1.0) rv = 1.0;
            else if (rv < -1.0) rv = -1.0;

            stereo_out[j * 2] = (int16_t)(lv * 32767.0);
            stereo_out[j * 2 + 1] = (int16_t)(rv * 32767.0);
        }
    }
}

/* Fast xorshift64* uniform [0.0, 1.0) generator for uncorrelated stereo TPDF dither. */
static inline double _rng_uniform(uint64_t *state) {
    uint64_t x = *state;
    if (x == 0) {
        x = 0x9E3779B97F4A7C15ULL;
    }
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    *state = x;
    uint64_t r = (x * 0x2545F4914F6CDD1DULL) >> 11;
    return (double)r * (1.0 / 9007199254740992.0);
}

/*
 * Apply master gain, optionally meter L/R VU & pre-clip true peaks, optionally apply
 * 1-LSB triangular PDF (TPDF) dither, round, clamp, and pack little-endian signed
 * stereo PCM at 16-bit (2B), 24-bit (3B), or 32-bit (4B).
 */
void dsp_master_pack_pcm(
    const double *master_left,
    const double *master_right,
    size_t n,
    double master_gain,
    int bit_depth,
    int dither_tpdf,
    uint64_t *rng_state,
    uint8_t *out_bytes,
    int compute_meters,
    double *out_vu_l,
    double *out_vu_r,
    double *out_pk_l,
    double *out_pk_r
) {
    if (!master_left || !master_right || !out_bytes || n == 0) {
        return;
    }
    if (bit_depth != 16 && bit_depth != 24 && bit_depth != 32) {
        bit_depth = 16;
    }

    uint64_t local_rng = (rng_state && *rng_state) ? *rng_state : 0xA0761D6478BD642FULL;
    int use_dither = (dither_tpdf != 0) && (bit_depth < 32);

    double scale;
    int64_t q_min;
    int64_t q_max;
    if (bit_depth == 24) {
        scale = 8388608.0;
        q_min = -8388608LL;
        q_max = 8388607LL;
    } else if (bit_depth == 32) {
        scale = 2147483648.0;
        q_min = -2147483648LL;
        q_max = 2147483647LL;
    } else {
        scale = 32768.0;
        q_min = -32768LL;
        q_max = 32767LL;
    }

    double l_sq = 0.0;
    double r_sq = 0.0;
    double l_pk = 0.0;
    double r_pk = 0.0;

    int16_t *out16 = (int16_t *)out_bytes;
    int32_t *out32 = (int32_t *)out_bytes;
    size_t off24 = 0;

    for (size_t j = 0; j < n; ++j) {
        double lv = master_left[j] * master_gain;
        double rv = master_right[j] * master_gain;

        if (compute_meters) {
            l_sq += lv * lv;
            r_sq += rv * rv;
            double al = lv >= 0.0 ? lv : -lv;
            double ar = rv >= 0.0 ? rv : -rv;
            if (al > l_pk) l_pk = al;
            if (ar > r_pk) r_pk = ar;
        }

        if (lv > 1.0) lv = 1.0;
        else if (lv < -1.0) lv = -1.0;
        if (rv > 1.0) rv = 1.0;
        else if (rv < -1.0) rv = -1.0;

        double sl = lv * scale;
        double sr = rv * scale;
        if (use_dither) {
            sl += _rng_uniform(&local_rng) - _rng_uniform(&local_rng);
            sr += _rng_uniform(&local_rng) - _rng_uniform(&local_rng);
        }

        int64_t ql = (int64_t)llround(sl);
        int64_t qr = (int64_t)llround(sr);
        if (ql > q_max) ql = q_max;
        else if (ql < q_min) ql = q_min;
        if (qr > q_max) qr = q_max;
        else if (qr < q_min) qr = q_min;

        if (bit_depth == 16) {
            out16[j * 2] = (int16_t)ql;
            out16[j * 2 + 1] = (int16_t)qr;
        } else if (bit_depth == 24) {
            uint32_t ul = (uint32_t)(int32_t)ql;
            uint32_t ur = (uint32_t)(int32_t)qr;
            out_bytes[off24++] = (uint8_t)(ul & 0xFFu);
            out_bytes[off24++] = (uint8_t)((ul >> 8) & 0xFFu);
            out_bytes[off24++] = (uint8_t)((ul >> 16) & 0xFFu);
            out_bytes[off24++] = (uint8_t)(ur & 0xFFu);
            out_bytes[off24++] = (uint8_t)((ur >> 8) & 0xFFu);
            out_bytes[off24++] = (uint8_t)((ur >> 16) & 0xFFu);
        } else {
            out32[j * 2] = (int32_t)ql;
            out32[j * 2 + 1] = (int32_t)qr;
        }
    }

    if (rng_state) {
        *rng_state = local_rng;
    }

    if (compute_meters) {
        double inv_n = 1.0 / (double)n;
        if (out_vu_l) *out_vu_l = sqrt(l_sq * inv_n) * SQRT2_VAL;
        if (out_vu_r) *out_vu_r = sqrt(r_sq * inv_n) * SQRT2_VAL;
        if (out_pk_l) *out_pk_l = l_pk;
        if (out_pk_r) *out_pk_r = r_pk;
    }
}

/* Return 1 if all int16 samples have abs(v) < threshold, else 0. */
int dsp_is_silent_i16(const int16_t *pcm, size_t n_samples, int threshold) {
    if (!pcm || n_samples == 0) {
        return 1;
    }
    for (size_t i = 0; i < n_samples; ++i) {
        int v = (int)pcm[i];
        if (v < 0) v = -v;
        if (v >= threshold) {
            return 0;
        }
    }
    return 1;
}

/* Decode 32-bit little-endian IEEE-754 float WAV bytes to mono double. */
size_t dsp_decode_f32_to_mono_f64(
    const uint8_t *raw,
    size_t nbytes,
    int nchannels,
    double *out
) {
    if (!raw || !out || nbytes == 0 || nchannels <= 0) {
        return 0;
    }
    size_t frame_bytes = (size_t)4 * (size_t)nchannels;
    size_t n_frames = nbytes / frame_bytes;
    const float *fp = (const float *)raw;
    if (nchannels == 1) {
        for (size_t i = 0; i < n_frames; ++i) {
            double v = (double)fp[i];
            out[i] = isfinite(v) ? v : 0.0;
        }
    } else if (nchannels == 2) {
        for (size_t i = 0; i < n_frames; ++i) {
            size_t j = i * 2;
            double v0 = (double)fp[j];
            double v1 = (double)fp[j + 1];
            if (!isfinite(v0)) v0 = 0.0;
            if (!isfinite(v1)) v1 = 0.0;
            out[i] = (v0 + v1) * 0.5;
        }
    } else {
        double inv_ch = 1.0 / (double)nchannels;
        for (size_t f = 0; f < n_frames; ++f) {
            size_t base = f * (size_t)nchannels;
            double total = 0.0;
            for (int c = 0; c < nchannels; ++c) {
                double v = (double)fp[base + (size_t)c];
                if (isfinite(v)) {
                    total += v;
                }
            }
            out[f] = total * inv_ch;
        }
    }
    return n_frames;
}

/* Decode 64-bit little-endian IEEE-754 double WAV bytes to mono double. */
size_t dsp_decode_f64_to_mono_f64(
    const uint8_t *raw,
    size_t nbytes,
    int nchannels,
    double *out
) {
    if (!raw || !out || nbytes == 0 || nchannels <= 0) {
        return 0;
    }
    size_t frame_bytes = (size_t)8 * (size_t)nchannels;
    size_t n_frames = nbytes / frame_bytes;
    const double *dp = (const double *)raw;
    if (nchannels == 1) {
        for (size_t i = 0; i < n_frames; ++i) {
            double v = dp[i];
            out[i] = isfinite(v) ? v : 0.0;
        }
    } else {
        double inv_ch = 1.0 / (double)nchannels;
        for (size_t f = 0; f < n_frames; ++f) {
            size_t base = f * (size_t)nchannels;
            double total = 0.0;
            for (int c = 0; c < nchannels; ++c) {
                double v = dp[base + (size_t)c];
                if (isfinite(v)) {
                    total += v;
                }
            }
            out[f] = total * inv_ch;
        }
    }
    return n_frames;
}

/* Stereo-linked zero-latency peak brickwall master limiter with pre-gain & smooth release. */
void dsp_master_limiter_inplace(
    double *left,
    double *right,
    size_t n,
    double sample_rate,
    double pre_gain,
    double ceiling_db,
    double release_ms,
    double *env_io
) {
    if (!left || !right || !env_io || n == 0) {
        return;
    }
    double sr = sample_rate > 1.0 ? sample_rate : 1.0;
    double rel_samples = release_ms * 0.001 * sr;
    if (rel_samples < 1.0) rel_samples = 1.0;
    double release_c = exp(-1.0 / rel_samples);
    double one_minus_rel = 1.0 - release_c;
    double ceiling_lin = pow(10.0, ceiling_db / 20.0);
    double env = *env_io;

    for (size_t i = 0; i < n; ++i) {
        double lv = left[i] * pre_gain;
        double rv = right[i] * pre_gain;
        if (!isfinite(lv)) lv = 0.0;
        if (!isfinite(rv)) rv = 0.0;
        double al = lv >= 0.0 ? lv : -lv;
        double ar = rv >= 0.0 ? rv : -rv;
        double pk = al >= ar ? al : ar;
        if (pk > env) {
            env = pk;
        } else {
            env = release_c * env + one_minus_rel * pk;
        }
        if (env > ceiling_lin && env > 1e-12) {
            double g = ceiling_lin / env;
            left[i] = lv * g;
            right[i] = rv * g;
        } else {
            left[i] = lv;
            right[i] = rv;
        }
    }
    *env_io = env;
}

/*
 * Apply master gain, optionally meter L/R VU & true peaks, optionally clamp to
 * [-1.0, 1.0] (1 for live paFloat32, 0 for unclipped 32-bit float WAV bounce),
 * and pack interleaved stereo IEEE-754 float32 samples.
 */
void dsp_master_pack_f32(
    const double *master_left,
    const double *master_right,
    size_t n,
    double master_gain,
    int clamp_output,
    float *out_f32,
    int compute_meters,
    double *out_vu_l,
    double *out_vu_r,
    double *out_pk_l,
    double *out_pk_r
) {
    if (!master_left || !master_right || !out_f32 || n == 0) {
        return;
    }

    double l_sq = 0.0;
    double r_sq = 0.0;
    double l_pk = 0.0;
    double r_pk = 0.0;

    for (size_t j = 0; j < n; ++j) {
        double lv = master_left[j] * master_gain;
        double rv = master_right[j] * master_gain;
        if (!isfinite(lv)) lv = 0.0;
        if (!isfinite(rv)) rv = 0.0;

        if (compute_meters) {
            l_sq += lv * lv;
            r_sq += rv * rv;
            double al = lv >= 0.0 ? lv : -lv;
            double ar = rv >= 0.0 ? rv : -rv;
            if (al > l_pk) l_pk = al;
            if (ar > r_pk) r_pk = ar;
        }

        if (clamp_output) {
            if (lv > 1.0) lv = 1.0;
            else if (lv < -1.0) lv = -1.0;
            if (rv > 1.0) rv = 1.0;
            else if (rv < -1.0) rv = -1.0;
        }

        out_f32[j * 2] = (float)lv;
        out_f32[j * 2 + 1] = (float)rv;
    }

    if (compute_meters) {
        double inv_n = 1.0 / (double)n;
        if (out_vu_l) *out_vu_l = sqrt(l_sq * inv_n) * SQRT2_VAL;
        if (out_vu_r) *out_vu_r = sqrt(r_sq * inv_n) * SQRT2_VAL;
        if (out_pk_l) *out_pk_l = l_pk;
        if (out_pk_r) *out_pk_r = r_pk;
    }
}

/* Convert mono IEEE-754 float32 samples into 24-bit signed little-endian PCM bytes. */
void dsp_pack_mono_f32_to_i24(
    const float *in_f32,
    size_t n,
    uint8_t *out_bytes
) {
    if (!in_f32 || !out_bytes || n == 0) {
        return;
    }
    const double scale = 8388608.0;
    size_t off = 0;
    for (size_t i = 0; i < n; ++i) {
        double v = (double)in_f32[i];
        if (!isfinite(v)) v = 0.0;
        if (v > 1.0) v = 1.0;
        else if (v < -1.0) v = -1.0;
        int64_t q = (int64_t)llrint(v * scale);
        if (q > 8388607LL) q = 8388607LL;
        else if (q < -8388608LL) q = -8388608LL;
        uint32_t u = (uint32_t)(int32_t)q;
        out_bytes[off++] = (uint8_t)(u & 0xFFu);
        out_bytes[off++] = (uint8_t)((u >> 8) & 0xFFu);
        out_bytes[off++] = (uint8_t)((u >> 16) & 0xFFu);
    }
}
