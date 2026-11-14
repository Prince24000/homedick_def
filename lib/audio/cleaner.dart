/// Offline audio cleaning for ASR. Raw PCM for server verification is
/// untouched -- this produces a cleaned copy for Moonshine only.
///
/// Chain: [neural denoise (GTCRN, if a model was provided)] -> high-pass ->
/// pre-emphasis -> AGC. The A/B harness in voice_flow is the referee for
/// which stages stay: the target is Moonshine's word-error rate, not
/// human-perceived cleanliness -- over-suppression that "sounds perfect"
/// introduces artifacts that transcribe WORSE.
library;

import 'dart:math';
import 'dart:typed_data';

import '../config.dart';
import 'pipeline.dart' show Denoiser;
import 'wav.dart' show pcm16ToFloat32;

class AudioCleaner {
  static Uint8List cleanForTranscription(
    Uint8List pcm16, {
    int sampleRate = AppConfig.sampleRate,
    Denoiser? denoiser,
  }) {
    // Empty/near-empty capture: nothing to clean. Without this guard,
    // _preEmphasis reads x[0] of an empty list (RangeError) and _agcRms
    // computes 0/0 = NaN which .round() then throws on -- a crash wired to
    // fire on the first silent timeout of an enrollment or challenge window.
    if (pcm16.length < 4) return pcm16;

    // 1. PCM16 -> float32 [-1, 1]
    var x = pcm16ToFloat32(pcm16);

    // 2. Neural denoise, when the model shipped. A trained network knows
    //    what speech looks like; the hand-rolled stages below only know
    //    arithmetic. Runs FIRST so the later stages see denoised signal.
    if (denoiser != null) {
      x = denoiser.run(x);
      if (x.isEmpty) return pcm16; // model misbehaved -- fail open to raw
      // A NaN/Infinity from the network is the same failure: fail open.
      for (var i = 0; i < x.length; i++) {
        if (!x[i].isFinite) return pcm16;
      }
    }

    // 3. High-pass @ 80 Hz -- kills DC offset and low rumble
    final hp = _highPass(x, sampleRate, cutoffHz: 80);

    // 4. Pre-emphasis -- boosts consonants (t, k, l) that base models miss
    final pre = _preEmphasis(hp, coef: 0.97);

    // 5. Safety clamp before AGC (pre-emphasis can overshoot transients)
    for (var i = 0; i < pre.length; i++) {
      pre[i] = pre[i].clamp(-1.0, 1.0);
    }

    // 6. AGC -- normalize RMS to -20 dBFS. No noise gate; gates click.
    final norm = _agcRms(pre, targetDb: -20);

    // 7. float32 -> PCM16
    return _float32ToPcm16(norm);
  }

  static Uint8List _float32ToPcm16(Float32List f) {
    final out = Uint8List(f.length * 2);
    final view = ByteData.sublistView(out);
    for (var i = 0; i < f.length; i++) {
      // .round() THROWS on NaN/Infinity. A sample the maths lost becomes
      // silence, never an exception in the capture path.
      var v = f[i];
      if (!v.isFinite) v = 0.0;
      var s = (v * 32768.0).round();
      if (s > 32767) s = 32767;
      if (s < -32768) s = -32768;
      view.setInt16(i * 2, s, Endian.little);
    }
    return out;
  }

  static Float32List _highPass(Float32List x, int fs, {int cutoffHz = 80}) {
    final y = Float32List(x.length);
    final c = tan(pi * cutoffHz / fs);
    final a1 = (1.0 - c) / (1.0 + c);
    final b0 = 1.0 / (1.0 + c);
    var yPrev = 0.0;
    var xPrev = 0.0;
    for (var i = 0; i < x.length; i++) {
      y[i] = a1 * yPrev + b0 * (x[i] - xPrev);
      yPrev = y[i];
      xPrev = x[i];
    }
    return y;
  }

  static Float32List _preEmphasis(Float32List x, {double coef = 0.97}) {
    if (x.isEmpty) return x;
    final y = Float32List(x.length);
    y[0] = x[0];
    for (var i = 1; i < x.length; i++) {
      y[i] = x[i] - coef * x[i - 1];
    }
    return y;
  }

  static Float32List _agcRms(Float32List x, {double targetDb = -20}) {
    if (x.isEmpty) return x;
    final target = pow(10, targetDb / 20.0) as double;
    var sumSq = 0.0;
    for (var i = 0; i < x.length; i++) {
      // One NaN or Infinity anywhere poisons the RMS and, through the gain,
      // every sample; the signal is returned as-is instead.
      if (!x[i].isFinite) return Float32List.fromList(x);
      sumSq += x[i] * x[i];
    }
    final rms = sqrt(sumSq / x.length);
    // NaN guard matters: NaN < 1e-10 is FALSE, so a bare threshold check
    // lets NaN sail through and detonate in float->pcm rounding.
    if (!rms.isFinite || rms < 1e-10) return Float32List.fromList(x);
    final gain = target / rms;
    final clamped = gain > 31.62 ? 31.62 : gain; // +30 dB max boost
    final y = Float32List(x.length);
    for (var i = 0; i < x.length; i++) {
      y[i] = (x[i] * clamped).clamp(-1.0, 1.0);
    }
    return y;
  }
}
