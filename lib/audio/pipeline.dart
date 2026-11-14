/// The offline speech stack, isolated in one file on purpose: sherpa-onnx's
/// Dart constructor surface has shifted between versions, so when a pinned
/// upgrade breaks a name, the fix lives here and nowhere else. Cross-check
/// against dart-api-examples/ of the INSTALLED sherpa_onnx version --
/// keyword-spotter-from-file and vad-with-non-streaming-asr (run-moonshine)
/// are the two relevant examples.
library;


import 'package:sherpa_onnx/sherpa_onnx.dart' as sherpa;
import 'package:flutter/foundation.dart';

import '../config.dart';
import 'wav.dart';
bool _sherpaInitDone = false;
void _ensureSherpaInit() {
  if (_sherpaInitDone) return;
  sherpa.initBindings();
  _sherpaInitDone = true;
}

/// Wake-word detection over the continuous mic stream. A zipformer KWS
/// model (a few MB, cheap enough to run all day) watching for
/// [AppConfig.wakePhrase].
class WakeWordSpotter {
  WakeWordSpotter({required String modelDir, required String keywordsFile}) {
    _ensureSherpaInit();
    final config = sherpa.KeywordSpotterConfig(
      model: sherpa.OnlineModelConfig(
        transducer: sherpa.OnlineTransducerModelConfig(
          encoder: '$modelDir/encoder.onnx',
          decoder: '$modelDir/decoder.onnx',
          joiner: '$modelDir/joiner.onnx',
        ),
        tokens: '$modelDir/tokens.txt',
        numThreads: 1,
        provider: 'cpu',
      ),
      keywordsFile: keywordsFile,
      // Sensitivity. Defaults were too tight in practice: detection was
      // intermittent even with a strong signal (peaks in the thousands).
      // keywordsScore boosts the keyword path during decoding (higher =
      // easier to trigger); keywordsThreshold is the trigger cutoff (lower
      // = easier). Raise score / lower threshold if it still misses; if it
      // starts firing on unrelated speech, walk them back.
      // NOTE: if the build complains these names don't exist, check the
      // KeywordSpotterConfig constructor of the INSTALLED sherpa_onnx --
      // this API has moved between versions.
      keywordsScore: 2.0,
      keywordsThreshold: 0.15,
      numTrailingBlanks: 1,
    );
    _spotter = sherpa.KeywordSpotter(config);
    _stream = _spotter.createStream();
  }

  late final sherpa.KeywordSpotter _spotter;
  late final sherpa.OnlineStream _stream;

  /// Feed PCM16 chunks; returns true on a wake-word hit.
  bool accept(Uint8List pcm) {
    _stream.acceptWaveform(
        samples: pcm16ToFloat32(pcm), sampleRate: AppConfig.sampleRate);
    var hit = false;
    while (_spotter.isReady(_stream)) {
      _spotter.decode(_stream);                 // decode FIRST,
      final r = _spotter.getResult(_stream);    // then read -- canonical order
      if (r.keyword.isNotEmpty) {
        debugPrint('[kws] HIT: ${r.keyword}');
        hit = true;
        _spotter.reset(_stream);
      }
    }
    return hit;
  }

  void dispose() {
    _stream.free();
    _spotter.free();
  }
}

/// Silero VAD wrapper used for ENDPOINTING inside an active window: the
/// window closes after [AppConfig.endpointSilence] of non-speech, so the
/// user talks, stops, and the pipeline moves on without a button.
class Endpointer {
  Endpointer({required String modelPath}) {
    _ensureSherpaInit();
    _vad = sherpa.VoiceActivityDetector(
      config: sherpa.VadModelConfig(
        sileroVad: sherpa.SileroVadModelConfig(
          model: modelPath,
          minSilenceDuration:
              AppConfig.endpointSilence.inMilliseconds / 1000.0,
          minSpeechDuration: 0.15,
        ),
        sampleRate: AppConfig.sampleRate,
        numThreads: 1,
      ),
      bufferSizeInSeconds: AppConfig.activeWindow.inSeconds.toDouble() + 2,
    );
  }

  late final sherpa.VoiceActivityDetector _vad;
  bool _sawSpeech = false;

  /// Speech has been heard since the last reset (it may still be going).
  bool get heardSpeech => _sawSpeech;

  /// Feed a chunk; returns true when speech has been heard AND has now
  /// ended -- the signal to close the window and transcribe.
  bool acceptAndCheckDone(Uint8List pcm) {
    _vad.acceptWaveform(pcm16ToFloat32(pcm));
    if (_vad.isDetected()) _sawSpeech = true;
    // Silero flips isDetected() back to false once trailing silence passes
    // minSilenceDuration; combined with _sawSpeech that is the endpoint.
    return _sawSpeech && !_vad.isDetected();
  }

  void reset() {
    _sawSpeech = false;
    _vad.clear();
  }

  void dispose() => _vad.free();
}

/// Neural denoiser (GTCRN) for the TRANSCRIPT copy only. Sub-megabyte
/// model, 16 kHz native, runs per-utterance -- cheap enough for this phone.
/// The raw capture for server-side speaker verification NEVER passes
/// through it: verification models key on exactly the spectral detail a
/// denoiser rewrites.
///
/// As everywhere in this file: if the build rejects these constructor
/// names, diff against the dart-api-examples of the INSTALLED sherpa_onnx
/// (speech-enhancement-gtcrn example) -- this API is young and has moved.
class Denoiser {
  Denoiser({required String modelPath}) {
    _ensureSherpaInit();
    _d = sherpa.OfflineSpeechDenoiser(
      sherpa.OfflineSpeechDenoiserConfig(
        model: sherpa.OfflineSpeechDenoiserModelConfig(
          gtcrn:
              sherpa.OfflineSpeechDenoiserGtcrnModelConfig(model: modelPath),
          numThreads: 1,
          provider: 'cpu',
        ),
      ),
    );
  }

  late final sherpa.OfflineSpeechDenoiser _d;

  Float32List run(Float32List samples) {
    final out = _d.run(samples: samples, sampleRate: AppConfig.sampleRate);
    return out.samples;
  }

  void dispose() => _d.free();
}

/// Moonshine transcription of one finished utterance. Non-streaming by
/// design: wake word -> window -> whole-clip decode is exactly the flow
/// Moonshine v1-style models serve best, and it keeps the phone's CPU idle
/// except in the second after you stop talking.
class Transcriber {
  Transcriber({required String modelDir, int numThreads = 2}) {
    _ensureSherpaInit();
    final config = sherpa.OfflineRecognizerConfig(
      model: sherpa.OfflineModelConfig(
        moonshine: sherpa.OfflineMoonshineModelConfig(
          preprocessor: '$modelDir/preprocess.onnx',
          encoder: '$modelDir/encode.int8.onnx',
          uncachedDecoder: '$modelDir/uncached_decode.int8.onnx',
          cachedDecoder: '$modelDir/cached_decode.int8.onnx',
        ),
        tokens: '$modelDir/tokens.txt',
        numThreads: numThreads, // 2 of the little cores; leave the UI alive
        provider: 'cpu',
        debug: false,
      ),
    );
    _rec = sherpa.OfflineRecognizer(config);
  }

  late final sherpa.OfflineRecognizer _rec;

  String transcribe(Uint8List pcm) {
    // Under 0.1 s is no word at all, and an empty clip (a window closed by
    // a second tap at once) must never reach the native decoder.
    if (pcm.length < AppConfig.sampleRate ~/ 5) return '';
    final stream = _rec.createStream();
    try {
      stream.acceptWaveform(
          samples: pcm16ToFloat32(pcm), sampleRate: AppConfig.sampleRate);
      _rec.decode(stream);
      return _rec.getResult(stream).text.trim();
    } finally {
      stream.free();
    }
  }

  void dispose() => _rec.free();
}
