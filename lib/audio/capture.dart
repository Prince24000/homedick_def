/// One microphone stream, opened once, at the one format everything eats:
/// 16 kHz mono PCM16. The wake-word spotter drinks from it continuously;
/// when the flow opens an active window it collects the SAME stream into an
/// utterance buffer -- so what the wake word heard, what Moonshine
/// transcribes, and what the server verifies are one recording.
library;

import 'dart:async';
import 'dart:typed_data';

import 'package:flutter/foundation.dart' show debugPrint;
import 'package:record/record.dart';

import '../config.dart';

class MicCapture {
  final _rec = AudioRecorder();
  StreamSubscription<Uint8List>? _sub;
  final _chunks = StreamController<Uint8List>.broadcast();

  DateTime _lastChunk = DateTime.now();
  Timer? _watchdog;
  bool _restarting = false;

  /// IS THE MICROPHONE SUPPOSED TO BE OPEN RIGHT NOW?
  ///
  /// The watchdog below used to have no way to ask this, and that was the
  /// whole bug (field report 2026-08-28: "the green light still shows at the
  /// top right even though i leave the app, to the point i cant even use my
  /// mic for voice notes in whatsapp, and only goes if i clear the cache").
  ///
  /// The watchdog exists because Android can kill a capture stream silently.
  /// But "no chunks arriving" has TWO causes and it could only see one:
  ///
  ///   * the stream died and nobody said so          → reopen, correct
  ///   * something else took the mic, on purpose      → reopening FIGHTS it
  ///
  /// The second is WhatsApp recording a voice note, a phone call, the
  /// assistant — and every ten seconds this snatched the microphone back.
  /// Hence a green indicator that never went out and a mic no other app
  /// could hold. Nothing released it because nothing ever called stop():
  /// the mic opened when the pipeline connected and lived until the process
  /// was killed.
  ///
  /// This flag is the missing question. `stop()` clears it, so the watchdog
  /// stands down instead of contesting. On a hub whose whole argument is
  /// privacy, an always-hot mic that other apps cannot take is not a small
  /// bug.
  bool _wanted = false;

  /// Grows each time a reopen does not stick, so a mic we genuinely cannot
  /// have is asked for less and less often instead of once every 10s
  /// forever. Reset by a chunk actually arriving.
  int _failures = 0;

  bool get isRunning => _wanted;

  /// Continuous PCM16 chunks while running.
  Stream<Uint8List> get chunks => _chunks.stream;

  Future<bool> start() async {
    if (!await _rec.hasPermission()) return false;
    _wanted = true;
    _failures = 0;
    final ok = await _openStream();
    if (!ok) {
      _wanted = false;
      return false;
    }
    // SELF-HEALING (the "mic stopped listening until app restart" report):
    // Android can silently kill a capture stream — audio-focus loss to a
    // call/assistant, doze — and `record` does not always error, it just
    // stops delivering. Nothing downstream can tell "quiet room" from "dead
    // stream" except the total absence of chunks: even silence produces a
    // steady chunk flow.
    //
    // THE BACKSTOP, NOT THE SIGNAL. onDone/onError in _openStream are the
    // real ones — a stream that ends says so. This fires only for a stream
    // that dies without saying so, which does happen.
    //
    // Two guards make it safe. It runs only while _wanted, so a mic given up
    // on purpose is never resurrected. And it waits 60s before believing
    // timing alone: at 10s it was firing inside the length of an ordinary
    // voice note and taking the microphone back mid-sentence.
    _watchdog?.cancel();
    _watchdog = Timer.periodic(const Duration(seconds: 15), (_) async {
      if (_restarting) return;
      if (!_wanted) return; // stopped on purpose; do not resurrect
      if (DateTime.now().difference(_lastChunk).inSeconds >= 60) {
        // Back off further rather than hammer: 60s, 120s... capped. A mic
        // held by a long call should not be pulled at repeatedly for the
        // length of the call.
        final due = 60 * (1 << (_failures.clamp(0, 3)));
        if (DateTime.now().difference(_lastChunk).inSeconds < due) return;
        _restarting = true;
        try {
          debugPrint('[mic] no chunks for ${due}s — reopening (try '
              '${_failures + 1})');
          await _sub?.cancel();
          _sub = null;
          try {
            await _rec.stop();
          } catch (_) {}
          if (!_wanted) return; // stopped while we were awaiting
          final ok = await _openStream();
          _failures = ok ? _failures : _failures + 1;
        } finally {
          _restarting = false;
        }
      }
    });
    return true;
  }

  Future<bool> _openStream() async {
    try {
      // ASSUMED, NOT CONFIRMED: `record` exposes no way to read the rate the
      // platform actually opened, and the WAV header (wav.dart) states
      // 16 kHz unconditionally -- a device that substitutes another rate
      // would be decoded pitch-shifted on the hub.
      final stream = await _rec.startStream(const RecordConfig(
        encoder: AudioEncoder.pcm16bits,
        sampleRate: AppConfig.sampleRate,
        numChannels: 1,
        // Keep the OS processing minimal: aggressive noise suppression has
        // been known to eat wake words on budget devices.
        autoGain: false,
        echoCancel: false,
        noiseSuppress: false,
      ));
      // stop() ran while the platform was opening the stream (the app
      // backgrounded mid-start, a reopen racing a release): its rec.stop()
      // may have landed first, and listening now would leave a microphone
      // open that nothing wants -- the green indicator that never goes out.
      if (!_wanted) {
        try {
          await _rec.stop();
        } catch (_) {}
        return false;
      }
      _lastChunk = DateTime.now();
      _sub = stream.listen(
        (c) {
          _lastChunk = DateTime.now();
          _failures = 0; // audio is flowing again; forget the backoff
          _chunks.add(c);
        },
        // A STREAM THAT ENDS SAYS SO. This is the reliable death signal and
        // the one to act on; the timing watchdog in start() is a guess
        // dressed as a diagnosis, kept only for streams that die quietly.
        //
        // It matters because of what SILENCE means on Android. When another
        // app takes priority on the microphone, the loser is not stopped: it
        // keeps receiving buffers, filled with zeros. So "no audio" and "no
        // chunks" are different things, and only the second one is a dead
        // stream. Reopening on the first is what fought WhatsApp for the
        // mic every ten seconds.
        //
        // Being handed silence is CORRECT and must be left alone: the
        // foreground app should win, we go deaf for the length of their
        // voice note, and real audio resumes on its own when they finish.
        // Nothing to do, nothing to reclaim, no tug of war.
        onDone: () {
          if (!_wanted) return;
          debugPrint('[mic] stream ended');
          _reopenSoon();
        },
        onError: (Object e) {
          if (!_wanted) return;
          debugPrint('[mic] stream error: $e');
          _reopenSoon();
        },
        cancelOnError: true,
      );
      return true;
    } catch (e) {
      debugPrint('[mic] could not open stream: $e');
      return false;
    }
  }

  /// Reopen after a stream genuinely ended, on the same backoff the watchdog
  /// uses. Not immediate: an end caused by another app taking the mic would
  /// otherwise become the same fight by a different route.
  Future<void> _reopenSoon() async {
    if (_restarting || !_wanted) return;
    _restarting = true;
    try {
      final wait = Duration(seconds: 2 * (1 << _failures.clamp(0, 4)));
      await Future<void>.delayed(wait);
      if (!_wanted) return;
      await _sub?.cancel();
      _sub = null;
      try {
        await _rec.stop();
      } catch (_) {}
      if (!_wanted) return;
      final ok = await _openStream();
      // NOT reset to 0 on a successful open — matching the watchdog. Opening
      // is not hearing: the stream can come back and be silent again
      // immediately, and zeroing the count here would drop the backoff to
      // 2s and reintroduce the hammering. Only a chunk actually arriving
      // clears it (see the listener above).
      _failures = ok ? _failures : _failures + 1;
    } finally {
      _restarting = false;
    }
  }

  /// Release the microphone and MEAN it.
  ///
  /// `_wanted = false` first, before any await: the watchdog is a periodic
  /// timer and could fire between the cancel and the stop, reopening the
  /// stream we are in the middle of closing. Clearing the flag first makes
  /// that firing a no-op.
  Future<void> stop() async {
    _wanted = false;
    _failures = 0;
    _watchdog?.cancel();
    _watchdog = null;
    await _sub?.cancel();
    _sub = null;
    try {
      await _rec.stop();
    } catch (_) {
      // Already stopped, or the platform lost it. Either way the mic is not
      // ours any more, which is the outcome asked for.
    }
  }

  Future<void> dispose() async {
    await stop();
    await _chunks.close();
    _rec.dispose();
  }
}

/// Collects chunks from an existing stream into one utterance, bounded by
/// [AppConfig.maxUtterance] -- the ceiling exists because the server rejects
/// oversized audio and because an unbounded buffer on a 3 GB phone is a leak
/// with a schedule.
class UtteranceCollector {
  /// Chunks arriving before [ignoreUntil] are dropped: the tail of the
  /// phone's own voice, still in the mic buffers when a window opens
  /// straight after it spoke.
  UtteranceCollector(Stream<Uint8List> source, {DateTime? ignoreUntil}) {
    _sub = source.listen((c) {
      if (ignoreUntil != null && DateTime.now().isBefore(ignoreUntil)) return;
      if (_bytes.length < _maxBytes) _bytes.add(c);
    });
  }

  static final int _maxBytes =
      AppConfig.maxUtterance.inSeconds * AppConfig.sampleRate * 2;

  final _bytes = BytesBuilder();
  StreamSubscription? _sub;

  /// Bytes captured so far, WITHOUT consuming them. Lets a caller decide
  /// whether a window is worth sending before committing to finish() —
  /// an empty enrollment window must not be sent as a recording the
  /// server can only reject.
  int get length => _bytes.length;

  /// Audio that arrived BEFORE this window opened but belongs to it: the
  /// words said while the wake phrase was still being read. Placed first,
  /// so the clip is in the order it was spoken.
  void seed(Uint8List pcm) {
    if (_bytes.length < _maxBytes) _bytes.add(pcm);
  }

  Uint8List finish() {
    _sub?.cancel();
    _sub = null;
    return _bytes.takeBytes();
  }

  void abort() {
    _sub?.cancel();
    _sub = null;
    _bytes.clear();
  }
}