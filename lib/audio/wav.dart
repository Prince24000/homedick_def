/// PCM16 -> WAV -> base64, matching the server's decoder contract exactly
/// ("expected 16-bit PCM WAV", decoded by audio_io to 16 kHz mono float32).
/// The bytes sent for speaker verification are the SAME bytes Moonshine
/// transcribed -- one capture, two consumers, no resample in between.
library;

import 'dart:convert';
import 'dart:typed_data';

import '../config.dart';

class WavClip {
  WavClip(this.b64, this.seconds);
  final String b64;
  final double seconds;
}

/// [pcm] is little-endian signed 16-bit mono at AppConfig.sampleRate.
WavClip encodeWavB64(Uint8List pcm, {int sampleRate = AppConfig.sampleRate}) {
  const channels = 1, bits = 16;
  final byteRate = sampleRate * channels * (bits ~/ 8);
  const blockAlign = channels * (bits ~/ 8);
  final header = BytesBuilder();

  void tag(String s) => header.add(ascii.encode(s));
  void u32(int v) =>
      header.add(Uint8List(4)..buffer.asByteData().setUint32(0, v, Endian.little));
  void u16(int v) =>
      header.add(Uint8List(2)..buffer.asByteData().setUint16(0, v, Endian.little));

  tag('RIFF');
  u32(36 + pcm.length);
  tag('WAVE');
  tag('fmt ');
  u32(16); // PCM fmt chunk size
  u16(1); //  PCM
  u16(channels);
  u32(sampleRate);
  u32(byteRate);
  u16(blockAlign);
  u16(bits);
  tag('data');
  u32(pcm.length);

  final bytes = BytesBuilder()
    ..add(header.toBytes())
    ..add(pcm);
  return WavClip(
    base64Encode(bytes.toBytes()),
    pcm.length / byteRate,
  );
}

/// PCM16 bytes -> float32 samples in [-1, 1] for the on-device models.
Float32List pcm16ToFloat32(Uint8List pcm) {
  final view = ByteData.sublistView(pcm);
  final out = Float32List(pcm.length ~/ 2);
  for (var i = 0; i < out.length; i++) {
    out[i] = view.getInt16(i * 2, Endian.little) / 32768.0;
  }
  return out;
}
