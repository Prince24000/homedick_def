/// The conversation engine. Two loops meet here:
///
///  * The LOCAL loop: mic -> wake word -> active window -> VAD endpoint ->
///    Moonshine -> `command` frame with BOTH the transcript and the audio
///    (the server verifies the speaker against the very clip that was
///    transcribed).
///
///  * The SERVER'S loop: after a command, the server may ask things --
///    challenge_required (repeat a phrase), prompt (a question), choice
///    (options), record_required (enrollment phrases). Each is voiced by
///    TTS and answered by another capture. The server owns the dialogue;
///    this file just gives it a mouth and ears.
///
/// One rule keeps the whole thing honest: THE SERVER'S QUESTION DEFINES THE
/// CAPTURE'S DESTINATION. Audio recorded under a challenge goes out as
/// challenge_response; under enrollment as recording; under a prompt it is
/// transcribed into prompt_reply; with no question outstanding it is a new
/// command.
///
/// Second rule, from the denoiser work: THE SERVER GETS RAW, MOONSHINE GETS
/// CLEANED. Verification keys on exactly the spectral detail cleaning
/// rewrites, so the clip that leaves the phone is always the untouched
/// capture; the cleaned copy exists only long enough to be transcribed.
library;

import 'dart:async';
import 'dart:convert' show base64Decode;
import 'dart:io' show Directory, File;
import 'dart:math' as math;
import 'dart:typed_data' show BytesBuilder;

import 'package:flutter/foundation.dart';

import '../audio/capture.dart';
import '../audio/cleaner.dart';
import '../audio/pipeline.dart';
import '../audio/wav.dart';
import '../config.dart';
import '../protocol.dart';
import '../socket_client.dart';
import 'tts.dart';
import 'phone_wake.dart';

enum FlowState { idleListening, capturing, thinking, speaking }

/// What the next finished capture answers.
/// `localText` is DICTATION FOR A SCREEN: transcribe, hand the words back,
/// send nothing. Every other role ends in a frame on the socket; this one
/// ends in a text field on the phone (2026-09-11, the voice-fillable form).
enum _CaptureRole {
  command,
  challenge,
  enrollment,
  prompt,
  choiceReply,
  localText,
}

/// Which roles feed Moonshine (and therefore pay for cleaning). Challenge
/// and enrollment audio goes to the server raw and is never transcribed.
const _transcribingRoles = {
  _CaptureRole.command,
  _CaptureRole.prompt,
  _CaptureRole.choiceReply,
  _CaptureRole.localText,
};

class VoiceFlow {
  VoiceFlow({
    required this.socket,
    required this.mic,
    required this.wake,
    required this.endpointer,
    required this.transcriber,
    required this.voice,
    required this.onStatus,
    this.denoiser,
    this.debugDir,
    this.onTranscript,
    this.onChoiceSpoken,
    this.onChoiceHeard,
    this.onChoiceCancel,
    this.onMuteSpoken,
    PhoneWake? phoneWake,
    this.wakeText,
    this.restText,
    this.uiContext,
  })  : _phoneWake = phoneWake;

  /// The wake phrase the word-reading engines listen for (it follows the
  /// assistant's name). Null: the fixed default in AppConfig.
  final String Function()? wakeText;
  String get _wakeText => wakeText?.call() ?? AppConfig.wakePhraseText;

  /// What the status says at rest: the assistant's name, nothing more (her
  /// design, 2026-10-11: "just Nova is okay"). Null: the wake phrase.
  final String Function()? restText;

  /// The screen context to attach to a command frame, or null for none.
  /// Read at send time, so it describes the screen the words were said on.
  final Map<String, dynamic>? Function()? uiContext;

  final SocketClient socket;
  final MicCapture mic;
  final WakeWordSpotter wake;
  final Endpointer endpointer;
  final Transcriber transcriber;
  final Voice voice;
  final void Function(String) onStatus;
  final bool Function(String)? onTranscript;

  /// A spoken answer to a SERVER list, handed to the app to send as words
  /// (AppState.sendChoiceHeard): the app owns the question, so it is the
  /// one that marks it answered and adds any rows already ticked. Returns
  /// true when sent, false when it could not be, null when the question
  /// was already gone. One utterance is one complete answer -- the hub
  /// matches it and either acts or asks "did you mean…?".
  final bool? Function(String said)? onChoiceSpoken;

  /// What the matcher HEARD and what it made of it, for the selection
  /// screen's feedback line. The screen used to run its own fuzzy match on
  /// AppState.lastTranscript — which never arrives during a choice (that
  /// path only carries COMMAND transcripts), so its matcher was dead code
  /// and voice picking had no visible feedback at all. One matcher, here,
  /// reporting outward.
  final void Function(String heard, List<String> matchedLabels)? onChoiceHeard;

  /// A spoken "cancel" on a selection. Routed out to the app so it takes
  /// the SAME path as the on-screen Cancel button (server told, screen
  /// popped, pending state cleared) instead of a second, divergent one.
  final void Function()? onChoiceCancel;

  /// Optional GTCRN stage for the transcript copy (see cleaner.dart).
  final Denoiser? denoiser;

  /// THE PHONE'S OWN RECOGNISER AS THE WAKE WORD, or null for the bundled
  /// spotter. When set, the two engines take the microphone IN TURNS:
  /// the recogniser holds it while the flow is idle, the app's capture
  /// holds it the rest of the time (see _applyMicPolicy). Never both.
  PhoneWake? _phoneWake;
  PhoneWake? get phoneWake => _phoneWake;

  /// Swap wake engines on a running flow (a Settings change). The old
  /// engine is stopped before the new one starts; the microphone ends up
  /// with whichever the current state calls for.
  Future<void> setPhoneWake(PhoneWake? next) async {
    final old = _phoneWake;
    if (identical(old, next)) return;
    _phoneWake = next;
    if (old != null) await old.stop();
    await _applyMicPolicy();
  }

  /// Leaving the app with the wake word OFF: no engine may hold the mic.
  /// Cleared by resumeMic. Only meaningful with a phone wake engine; with
  /// the bundled spotter AppState stops the mic itself, as it always has.
  bool _micSuspended = false;
  Future<void> suspendMic() async {
    _micSuspended = true;
    await _applyMicPolicy();
  }

  Future<void> resumeMic() async {
    _micSuspended = false;
    await _applyMicPolicy();
  }

  bool _started = false;
  Future<void> _policyChain = Future.value();

  /// WHO HOLDS THE MICROPHONE. Serialised, and the state is re-read when
  /// the step actually runs: idle→capturing→thinking can happen faster
  /// than one stop/start round trip, and acting on a stale reading would
  /// hand the mic to the wrong engine.
  Future<void> _applyMicPolicy() {
    _policyChain = _policyChain.then((_) => _applyMicPolicyNow()).catchError((e) {
      debugPrint('[flow] mic policy failed: $e');
    });
    return _policyChain;
  }

  Future<void> _applyMicPolicyNow() async {
    if (!_started) return;
    final pw = _phoneWake;
    if (pw == null) {
      // Bundled spotter: the mic is AppState's to open and close. The one
      // thing to undo is a recogniser that was running before a swap --
      // handled in setPhoneWake -- and a mic a swap left closed.
      if (!_micSuspended && !mic.isRunning) await mic.start();
      return;
    }
    if (_micSuspended) {
      if (pw.isRunning) await pw.stop();
      if (!_started) return;
      if (mic.isRunning) await mic.stop();
      return;
    }
    // Every await below is a chance for stop() or a swap to have happened;
    // the state is re-read after each and the step gives up if the world
    // moved on (a step that finishes against a torn-down pipeline opens a
    // microphone nobody owns).
    if (state == FlowState.idleListening) {
      if (mic.isRunning) await mic.stop();
      if (!_started || !identical(_phoneWake, pw)) return;
      if (!pw.isRunning) {
        await pw.start();
        if (!_started || !identical(_phoneWake, pw)) return;
        if (!pw.available && !mic.isRunning) {
          // No recogniser on this phone (not merely "stopped meanwhile"):
          // fall back to the bundled spotter rather than sit deaf.
          // onProblem has already said why.
          _phoneWake = null;
          await mic.start();
        }
      }
    } else {
      if (pw.isRunning) await pw.stop();
      if (!_started) return;
      if (!mic.isRunning) await mic.start();
    }
  }

  /// Take the microphone for a capture window, IN TURN with the policy
  /// steps: a step queued by the transition that just ended cannot slip in
  /// between the recogniser stopping and the window opening. True when the
  /// mic is ours. With the bundled spotter the mic is already open and this
  /// is a no-op that answers true.
  Future<bool> _acquireMic() {
    final done = Completer<bool>();
    _policyChain = _policyChain.then((_) async {
      // Suspended = backgrounded with the wake word OFF: no engine may hold
      // the mic. A window a hub frame opened meanwhile (a choice read out
      // in the background) used to take it anyway.
      if (!_started || _micSuspended) {
        done.complete(false);
        return;
      }
      final pw = _phoneWake;
      if (pw == null) {
        done.complete(true);
        return;
      }
      if (pw.isRunning) await pw.stop();
      final ok = mic.isRunning || await mic.start();
      done.complete(ok);
    }).catchError((e) {
      debugPrint('[flow] mic acquire failed: $e');
      if (!done.isCompleted) done.complete(false);
    });
    return done.future;
  }

  void _onStateForPolicy() {
    if (state != FlowState.thinking) _disarmCancelWatch();
    if (state == FlowState.speaking) {
      _armStopWatch();
    } else {
      _disarmStopWatch();
    }
    if (state == FlowState.idleListening) _gateReset();
    if (_phoneWake != null) _applyMicPolicy();
  }

  // ══ TALKING OVER A COMMAND (2026-09-30, rebuilt 2026-10-09) ══════════
  //
  // From the moment a spoken command's window closes until its answer
  // arrives, the microphone is watched. A spoken "cancel" used to lose
  // every race -- by the time the phone had heard the pause after it and
  // read it, the lamp was on -- so now:
  //
  //   * the instant SPEECH STARTS, the hub is told to HOLD: it touches no
  //     device until it hears what that was (or for at most a few seconds);
  //   * longer than a few seconds is talk in the room, not words for the
  //     hub: the hold is let go and nothing is read;
  //   * what was said is read here and sent as an `aside`, with its clip.
  //     The HUB decides what it was -- a cancel, "and the fan", "no, the
  //     fan", or nothing -- and checks the clip is this phone's person's
  //     voice first, cancel included (the television must not be able to
  //     stop or add anything).
  //
  // Leaving `thinking` for any reason -- a question, a result, the phone
  // speaking -- ends the watch, so it never overlaps a capture window.
  bool _cancelWatch = false;

  /// The LAST ~4 s heard, not the first (see the master copy's note).
  final List<Uint8List> _cancelChunks = [];
  static const int _cancelKeepChunks = 50;
  int _cancelEpoch = 0;

  /// Speech heard over the command, not yet read.
  bool _asideHeard = false;
  /// ...and it went on too long to be for the hub (_asideMaxBytes).
  bool _asideLong = false;
  int _asideBytes = 0;
  static const int _asideMaxBytes = 3 * 32000; // three seconds of speech
  /// The hub has been (or must be, once the command is out) told to hold.
  bool _holdWanted = false;

  void _armCancelWatch() {
    if (_freed) return;
    // Already watching since the window closed: keep what is being heard.
    if (_cancelWatch) return;
    _cancelWatch = true;
    _cancelChunks.clear();
    _cancelEpoch++;
    _asideHeard = _asideLong = false;
    _asideBytes = 0;
    endpointer.reset();
  }

  void _disarmCancelWatch() {
    if (!_cancelWatch) return;
    // A hold asked for and never answered (a question arrived, or the
    // result, before the words were read): let the hub go on now rather
    // than wait out the limit (her log, 2026-10-09).
    if (_holdWanted && _commandAnswer) socket.send({'type': 'release'});
    _cancelWatch = false;
    _cancelChunks.clear();
    _cancelEpoch++;
    _asideHeard = _asideLong = _holdWanted = false;
  }

  void _holdHub() {
    _holdWanted = true;
    if (_commandAnswer) socket.send({'type': 'hold'});
  }

  void _releaseHub() {
    if (!_holdWanted) return;
    _holdWanted = false;
    if (_commandAnswer) socket.send({'type': 'release'});
  }

  void _feedCancelWatch(Uint8List chunk) {
    if (!_cancelWatch || _freed) return;
    _cancelChunks.add(chunk);
    if (_cancelChunks.length > _cancelKeepChunks) _cancelChunks.removeAt(0);
    final done = endpointer.acceptAndCheckDone(chunk);
    if (endpointer.heardSpeech && !_asideHeard) {
      _asideHeard = true;
      _asideBytes = 0;
      _holdHub();
    }
    if (_asideHeard) {
      _asideBytes += chunk.length;
      if (!done && !_asideLong && _asideBytes > _asideMaxBytes) {
        _asideLong = true; // talk in the room: let the command go on
        debugPrint('[flow] talk over the command -- too long to be for the hub');
        _releaseHub();
      }
    }
    if (!done) return;
    final bb = BytesBuilder();
    for (final c in _cancelChunks) {
      bb.add(c);
    }
    _cancelChunks.clear();
    final pcm = bb.takeBytes();
    endpointer.reset();
    final long = _asideLong;
    _asideHeard = _asideLong = false;
    if (long) return;
    debugPrint('[flow] said over the command, '
        '${(pcm.length / 32000).toStringAsFixed(2)} s');
    // Under ~0.3 s is a cough or a click, not a word.
    if (pcm.length < 9600) {
      _releaseHub();
      return;
    }
    _checkAsideClip(pcm, _cancelEpoch);
  }

  Future<void> _checkAsideClip(Uint8List whole, int epoch) async {
    try {
      final raw = _trimToSpeech(whole); // the words, not the room around them
      final clean = AudioCleaner.cleanForTranscription(raw, denoiser: denoiser);
      final said = _hear(clean, raw).trim();
      // The answer came meanwhile: the command is over, and so is this.
      if (!_cancelWatch || epoch != _cancelEpoch) return;
      if (said.isEmpty || !_commandAnswer) {
        _releaseHub();
        return;
      }
      _holdWanted = false; // the aside answers the hold
      debugPrint('[flow] said over the command: "$said" -- to the hub');
      socket.send({
        'type': 'aside',
        'text': said,
        'audio': encodeWavB64(raw).b64,
        'audio_format': 'wav',
      });
      onStatus('"$said"');
    } catch (e) {
      debugPrint('[flow] said over the command: $e');
      _releaseHub();
    }
  }

  // ══ "STOP" WHILE THE PHONE IS TALKING (2026-10-09) ═══════════════════
  //
  // While the phone speaks, a SHORT burst (a word or two) is read, and a
  // stop or cancel ends the sentence. Never sent to the hub: it only
  // silences the phone. Words the phone itself is saying are ignored -- it
  // hears its own voice. HONEST LIMIT: the phone's voice and yours arrive
  // as one sound unless the phone cancels its own echo, so a "stop" said
  // over a sentence may be heard only in the pause after it.
  bool _stopWatch = false;
  final List<Uint8List> _stopChunks = [];
  static const int _stopMaxBytes = 48000; // a word or two: 1.5 s

  void _armStopWatch() {
    if (_freed || _stopWatch) return;
    _stopWatch = true;
    _stopChunks.clear();
    endpointer.reset();
  }

  void _disarmStopWatch() {
    _stopWatch = false;
    _stopChunks.clear();
  }

  void _feedStopWatch(Uint8List chunk) {
    if (!_stopWatch || _freed) return;
    _stopChunks.add(chunk);
    if (_stopChunks.length > 25) _stopChunks.removeAt(0);
    if (!endpointer.acceptAndCheckDone(chunk)) return;
    final bb = BytesBuilder();
    for (final c in _stopChunks) {
      bb.add(c);
    }
    _stopChunks.clear();
    endpointer.reset();
    final pcm = _trimToSpeech(bb.takeBytes());
    if (pcm.length < 9600 || pcm.length > _stopMaxBytes) return;
    _checkStopClip(pcm);
  }

  Future<void> _checkStopClip(Uint8List raw) async {
    try {
      final clean = AudioCleaner.cleanForTranscription(raw, denoiser: denoiser);
      final said = _hear(clean, raw).trim();
      if (!_stopWatch || said.isEmpty || !_meansCancel(said)) return;
      // Its own words, heard back: not a stop.
      final own = _norm(voice.saying).split(' ').toSet();
      if (_norm(said).split(' ').every(own.contains)) return;
      debugPrint('[flow] "$said" while speaking -- stopping the voice');
      onStatus('stopped');
      await voice.stop();
    } catch (e) {
      debugPrint('[flow] stop watch: $e');
    }
  }

  /// ONE TRANSCRIPTION AT A TIME. The cancel watch transcribes while the
  /// hub works; a question arriving can open a window whose own
  /// transcription must not run on the same native model at the same
  /// instant. Every transcription in this class goes through here.

  // ══ THE WAKE WORD: ONE MICROPHONE, READ ONLY WHEN SOMEONE SPEAKS ═══
  //
  // The app's own microphone stays open, a voice-activity detector watches
  // it, and ONLY a short burst of speech -- the length of a wake phrase --
  // is turned into words by Moonshine and checked for the phrase. No
  // recogniser is started and stopped all day, and nothing is "spotted":
  // the burst is read like any other clip.
  //
  //   * long speech (a conversation, the television) and faint sounds are
  //     never read at all, so the cost while the room talks is a
  //     voice-activity check and nothing else.
  //   * the words said while the phrase was still being read are not lost:
  //     they seed the command window (see _openWindow).
  //
  // HONEST COST: the read takes about a second and the phrase is only
  // recognised after the pause that ends it, so the window opens about two
  // seconds after the phrase. Say it, wait for "listening...".
  final List<Uint8List> _gateChunks = [];
  final List<Uint8List> _gateTail = [];
  int _gateBuffered = 0;
  int _gateBytes = 0;
  int _gateSpeechFrom = -1;
  bool _gateBusy = false;
  DateTime _gateLastDecode = DateTime.fromMillisecondsSinceEpoch(0);
  Uint8List? _gateSeed;
  DateTime? _gateSeedAt;

  void _gateReset() {
    _gateChunks.clear();
    _gateTail.clear();
    _gateBuffered = 0;
    _gateBytes = 0;
    _gateSpeechFrom = -1;
    _gateBusy = false;
    if (!_freed) endpointer.reset();
  }

  void _feedGatedWake(Uint8List chunk) {
    if (_freed) return;
    if (_gateBusy) {
      // Still reading the phrase: keep what is said meanwhile.
      if (_gateTail.length < 60) _gateTail.add(chunk);
      return;
    }
    _gateChunks.add(chunk);
    _gateBuffered += chunk.length;
    while (_gateBuffered > 160000 && _gateChunks.isNotEmpty) {
      _gateBuffered -= _gateChunks.removeAt(0).length;
    }
    final before = _gateBytes;
    _gateBytes += chunk.length;
    final done = endpointer.acceptAndCheckDone(chunk);
    if (_gateSpeechFrom < 0 && endpointer.heardSpeech) _gateSpeechFrom = before;
    if (!done) return;

    // The speech, and the pause that ended it (AppConfig.endpointSilence).
    final segment = _gateBytes - (_gateSpeechFrom < 0 ? _gateBytes : _gateSpeechFrom);
    final tail = AppConfig.sampleRate *
        2 *
        AppConfig.endpointSilence.inMilliseconds ~/
        1000;
    final speech = segment - tail;
    final bb = BytesBuilder();
    for (final c in _gateChunks) {
      bb.add(c);
    }
    final all = bb.takeBytes();
    _gateChunks.clear();
    _gateBuffered = 0;
    _gateBytes = 0;
    _gateSpeechFrom = -1;
    endpointer.reset();

    // A wake phrase is a short burst: 0.3 s to 3 s of speech.
    if (speech < 9600 || speech > 96000) return;
    final now = DateTime.now();
    if (now.difference(_gateLastDecode) < const Duration(milliseconds: 1200)) {
      return;
    }
    // Not the phone's own voice still in the air.
    final spoke = voice.lastSpokeEnd;
    if (spoke != null &&
        now.difference(spoke) < const Duration(milliseconds: 1500)) {
      return;
    }
    // The burst plus a little lead-in.
    final take = (segment + 12800).clamp(0, all.length).toInt();
    final pcm = Uint8List.sublistView(all, all.length - take);
    if (_peakOf(pcm) < 1500) return; // far away or faint: not for us
    _gateBusy = true;
    _gateTail.clear();
    unawaited(_gateDecode(pcm));
  }

  Future<void> _gateDecode(Uint8List pcm) async {
    try {
      final clean = AudioCleaner.cleanForTranscription(pcm, denoiser: denoiser);
      final heard = transcriber.transcribe(clean).trim();
      _gateLastDecode = DateTime.now();
      if (_freed || state != FlowState.idleListening) return;
      if (heard.isNotEmpty &&
          PhoneWake.wakeHeard(heard, AppConfig.wakePhraseText)) {
        debugPrint('[gate] HIT in "$heard"');
        final bb = BytesBuilder();
        for (final c in _gateTail) {
          bb.add(c);
        }
        _gateSeed = bb.takeBytes();
        _gateSeedAt = DateTime.now();
        _gateTail.clear();
        _gateBusy = false;
        // THE WAKE WORD ALONE ("Nova", and a breath): answered "Yes?" so
        // the person knows they were heard (her ask, 2026-10-11). Said in
        // one breath with a command ("Nova, turn on the light"), nothing
        // is said over them -- the command is already on its way.
        final extra = PhoneWake.wordCount(heard) - PhoneWake.wordCount(_wakeText);
        final alone = extra <= 1 && _peakOf(_gateSeed!) < 1500;
        unawaited(_onWake(answer: alone));
        return;
      }
      debugPrint('[gate] not the phrase: "$heard"');
    } catch (e) {
      debugPrint('[gate] $e');
    } finally {
      if (_gateBusy) {
        _gateBusy = false;
        _gateTail.clear();
      }
    }
  }

  /// THE WORD WITHOUT THE WAITING. A window opens when the question is
  /// asked and closes when the person has stopped, so a one-word answer
  /// ("cancel", "back", "kitchen") arrives inside several seconds of a
  /// quiet room -- field log 2026-10-01: 7 to 9 s clips, loud enough
  /// (peak 3300-4000), every one read as nothing, while a 2-3 s clip of
  /// the same kind read fine. A recogniser built for utterances loses a
  /// short word in a long clip, and the loudness levelling that follows
  /// raises the quiet room as much as the word. So before the clip is
  /// cleaned and read, it is cut down to where the voice is, with about
  /// half a second of room either side (a word needs its ends). Clips
  /// that are already tight are left alone, and so is everything that
  /// goes to the hub: this changes only what the speech reader is shown.
  static Uint8List _trimToSpeech(Uint8List pcm) {
    const frame = 320; // 20 ms at 16 kHz
    final n = pcm.length ~/ 2 ~/ frame;
    if (n < 60) return pcm; // under 1.2 s: nothing worth cutting
    final bd = ByteData.sublistView(pcm);
    final rms = List<double>.filled(n, 0);
    for (var f = 0; f < n; f++) {
      var sum = 0.0;
      final base = f * frame * 2;
      for (var i = 0; i < frame; i++) {
        final v = bd.getInt16(base + i * 2, Endian.little);
        sum += v * v;
      }
      rms[f] = math.sqrt(sum / frame);
    }
    final sorted = [...rms]..sort();
    final floor = sorted[(n * 0.25).floor()];
    final top = sorted[(n * 0.98).floor().clamp(0, n - 1)];
    if (top < 250) return pcm; // nothing loud enough to find
    final threshold = math.max(floor * 3.0, top * 0.15);
    // Runs of loud frames, joined across pauses under 0.6 s.
    final starts = <int>[];
    final ends = <int>[];
    final energy = <double>[];
    var last = -1000;
    for (var f = 0; f < n; f++) {
      if (rms[f] <= threshold) continue;
      if (f - last > 30) {
        starts.add(f);
        ends.add(f);
        energy.add(0);
      }
      ends[ends.length - 1] = f;
      energy[energy.length - 1] += rms[f];
      last = f;
    }
    if (starts.isEmpty) return pcm;
    final best = energy.reduce(math.max);
    var first = -1, lastF = -1;
    for (var i = 0; i < starts.length; i++) {
      if (energy[i] < best * 0.25) continue; // a door, a cough
      if (first < 0) first = starts[i];
      lastF = ends[i];
    }
    var from = math.max(0, first - 25); // 0.5 s before
    var to = math.min(n, lastF + 1 + 25); // 0.5 s after
    if (to - from < 50) {
      // A word needs a second of room at least.
      final grow = (50 - (to - from) + 1) ~/ 2;
      from = math.max(0, from - grow);
      to = math.min(n, to + grow);
    }
    if (from + (n - to) < 60) return pcm; // under 1.2 s to gain: leave it
    debugPrint('[flow] trimmed ${(n * frame / 16000).toStringAsFixed(2)} s '
        'to ${((to - from) * frame / 16000).toStringAsFixed(2)} s');
    return Uint8List.sublistView(pcm, from * frame * 2, to * frame * 2);
  }

  /// Loudest sample (sampled): a clip with a voice in it, or a quiet room?
  static int _peakOf(Uint8List pcm) {
    final bd = ByteData.sublistView(pcm);
    var peak = 0;
    for (var i = 0; i + 1 < pcm.length; i += 6) {
      final v = bd.getInt16(i, Endian.little).abs();
      if (v > peak) peak = v;
    }
    return peak;
  }

  /// EVERY transcription goes through here. When the first look finds
  /// nothing in a clip that clearly held a voice, the untouched recording
  /// is tried, then that recording with a second of quiet either side.
  /// Field log 2026-09-30: "back", "go back" and "cancel", said loudly,
  /// came back EMPTY while longer words were fine. Reached only by an
  /// empty answer, so nothing that already worked runs any differently.
  String _hear(Uint8List clean, Uint8List raw) {
    final t0 = DateTime.now();
    var text = transcriber.transcribe(clean);
    if (text.isEmpty &&
        raw.length >= 6400 &&
        raw.length <= 16000 * 2 * 8 &&
        _peakOf(raw) >= 2500) {
      text = transcriber.transcribe(raw);
      if (text.isEmpty) {
        final pad = Uint8List(16000 * 2); // 1 s of silence
        final padded = BytesBuilder()
          ..add(pad)
          ..add(raw)
          ..add(pad);
        text = transcriber.transcribe(padded.takeBytes());
      }
      if (text.isNotEmpty) debugPrint('[flow] second look found "$text"');
    }
    debugPrint('[flow] heard "$text" -- '
        '${(raw.length / 32000).toStringAsFixed(2)} s of audio, '
        'state=$state role=$_role, '
        '${DateTime.now().difference(t0).inMilliseconds} ms');
    return text;
  }

  /// When set, COMMAND captures are saved as raw_/clean_ WAV pairs under
  /// this directory -- the A/B harness that settles cleaning arguments with
  /// evidence. AppState passes it only in debug builds (kDebugMode); a
  /// release build gets null. Challenge, enrolment and prompt audio is
  /// never written even when it is set -- see _saveDebugPair.
  final String? debugDir;

  /// Flow state, OBSERVABLE. It was a bare field, so the orb and any dock
  /// only repainted when onStatus() happened to fire alongside a transition
  /// — a coincidence, not a contract. Every existing `state` read/write
  /// keeps working through the getter/setter pair.
  final ValueNotifier<FlowState> stateListenable =
      ValueNotifier(FlowState.idleListening);
  FlowState get state => stateListenable.value;
  set state(FlowState v) => stateListenable.value = v;

  _CaptureRole _role = _CaptureRole.command;

  /// When a phrase window (challenge, enrolment) last heard its speech end,
  /// while it waits [_phraseGrace] for the next word; null otherwise.
  DateTime? _phrasePause;
  static const Duration _phraseGrace = Duration(milliseconds: 1200);

  /// The words the hub asked to hear (a voice check or an enrolment
  /// phrase), so a spoken "cancel" is never mistaken for one of them.
  String _askedPhrase = '';

  /// The outstanding choice, in its true wire shape: the human HEARS labels,
  /// the server RECEIVES values. Verified against the browser client's
  /// buildChoice(): options are {label, value, sub?} objects, plus `multi`
  /// and `allow_all` flags (an "All of them" path when both are set).
  List<Map<String, dynamic>> _choiceOptions = const [];
  bool _choiceMulti = false;
  bool _choiceAllowAll = false;

  StreamSubscription<Uint8List>? _micSub;
  StreamSubscription<ServerFrame>? _frameSub;
  UtteranceCollector? _collector;
  Timer? _windowTimer;

  /// Watchdog: `thinking` is the one state with no local exit — it waits on
  /// a server frame. If that frame never comes (any path the fixes above
  /// missed), this returns the mic to idle instead of dying until restart.
  Timer? _thinkWatchdog;
  void _armThinkWatchdog({bool watchCancel = true}) {
    if (watchCancel) _armCancelWatch();
    _thinkWatchdog?.cancel();
    _thinkWatchdog = Timer(const Duration(seconds: 60), () {
      if (state == FlowState.thinking) {
        state = FlowState.idleListening;
        onStatus('no reply from the house — listening again');
      }
    });
  }

  /// Output lines produced by OUR command (query readings, action
  /// outcomes), collected while `thinking` and SPOKEN with the result — so
  /// what was shown is also said, and "Done" becomes the actual values.
  final List<String> _resultLines = [];
  /// A command of ours is out and its answer has not ended yet.
  bool _commandAnswer = false;

  /// ONE FRAME AT A TIME. A stream listener with an async body is NOT
  /// serialised -- the next frame ran while the previous handler was still
  /// awaiting its TTS, and the `output` handler then restored a stale state
  /// on top of a capture window a later frame had opened. Every frame now
  /// waits for the one before it; a handler that throws ends its own turn
  /// and never blocks the chain.
  Future<void> _frameChain = Future.value();

  /// Frames that OUTRANK a sentence still being read.
  ///
  /// Because the chain is strictly serial, a frame that arrives while the
  /// voice is mid-line waits for that line to finish -- correct for one
  /// narration line after another (chopping those would leave you hearing
  /// the first half of every sentence and the whole of none), and wrong
  /// for these four. A question needs answering and is about to be read
  /// out itself; a result ends the flow the line belonged to. Waiting
  /// means the question sits silent on screen while the phone finishes
  /// telling you something that has just been superseded.
  ///
  /// `output` is deliberately absent. `busy` too: a job's own card says
  /// what it is doing, and the line being read is usually the reason the
  /// job started.
  static const _interruptingFrames = {
    'prompt', 'choice', 'record_required', 'challenge_required', 'result',
  };

  /// True while the `output` handler is reading a line (see _enqueueFrame).
  bool _speakingOutput = false;

  void _enqueueFrame(ServerFrame f) {
    // BARGE-IN BY THE HUB, decided HERE rather than in the handler: this
    // runs the moment the frame lands, while the chain is still stuck on
    // the utterance. Stopping the voice ends the awaited say(), the
    // handler finishes normally (its epoch is untouched, so it restores
    // state and opens nothing it would not have opened), and this frame
    // is reached at once. Nothing is skipped -- only shortened.
    // Except a `result` over an OUTPUT line: the hub writes its warning
    // and ends the flow in the same breath, so the result always arrived
    // mid-line and cut the one thing a menu flow still says out loud to
    // its first word (an ok menu result then says nothing at all).
    if (state == FlowState.speaking &&
        _interruptingFrames.contains(f.type) &&
        !(f.type == 'result' && _speakingOutput)) {
      voice.stop();
    }
    _frameChain = _frameChain.then((_) => _onFrame(f)).catchError((e) {
      debugPrint('[flow] frame ${f.type} failed: $e');
    });
  }

  Future<void> start() async {
    _frameSub = socket.frames.listen(_enqueueFrame);
    _micSub = mic.chunks.listen(_onAudio);
    _started = true;
    stateListenable.addListener(_onStateForPolicy);
    await _applyMicPolicy();
    onStatus(_listeningStatus);
  }

  String get _listeningStatus => restText?.call() ?? _wakeText;

  Future<void> stop() async {
    _started = false;
    _starveTimer?.cancel();
    stateListenable.removeListener(_onStateForPolicy);
    await _micSub?.cancel();
    await _frameSub?.cancel();
    _thinkWatchdog?.cancel();
    _disarmCancelWatch();
    _closeWindow(abort: true);
    // A policy step still in flight would otherwise resume after this and
    // start the recogniser on a pipeline that is being torn down.
    await _policyChain;
    await _phoneWake?.stop();
    // STOP IS TERMINAL: AppState discards this flow and builds a new one
    // (a re-pair, a retried boot), each with its own spotter, VAD and
    // Moonshine. Nothing freed the old native models, so every rebuild
    // leaked the whole speech stack's memory. Freed once, here; `_freed`
    // turns away the handlers still finishing an await.
    if (!_freed) {
      _freed = true;
      unawaited(voice.stop());
      wake.dispose();
      endpointer.dispose();
      transcriber.dispose();
      denoiser?.dispose();
    }
  }

  /// Set by stop() once the native models are freed. See _openWindow.
  bool _freed = false;

  // ── Local loop ─────────────────────────────────────────────────────────

  static int _dbgChunks = 0;

  void _onAudio(Uint8List chunk) {
    if (++_dbgChunks % 100 == 0) {
      // Peak amplitude of this chunk: ~0 means the stream is silent even
      // though the mic is "open" (privacy layer, or another app holding it);
      // normal speech at arm's length peaks in the thousands; near 32000
      // means clipping, which the model also misses.
      final bd = ByteData.sublistView(chunk);
      var peak = 0;
      for (var i = 0; i + 1 < chunk.length; i += 2) {
        final v = bd.getInt16(i, Endian.little).abs();
        if (v > peak) peak = v;
      }
      debugPrint('[kws] fed $_dbgChunks chunks, peak=$peak, state=$state');
    }
    switch (state) {
      case FlowState.idleListening:
        // The wake phrase is heard from a short burst of speech (see _feedGatedWake).
        _feedGatedWake(chunk);
        break; // FIX: was falling through to capturing
      case FlowState.capturing:
        // The phone's own last words, still draining: see _echoTail.
        final hold = _ignoreAudioUntil;
        if (hold != null && DateTime.now().isBefore(hold)) break;
        // COUNTED AND MEASURED, so a window that hears nothing can say so
        // (see _watchForSilence). Every fourth sample is plenty for "is
        // there any signal at all" and keeps this off the hot path.
        _windowChunks++;
        if (_windowPeak < 3000) {
          final bd = ByteData.sublistView(chunk);
          for (var i = 0; i + 1 < chunk.length; i += 8) {
            final v = bd.getInt16(i, Endian.little).abs();
            if (v > _windowPeak) _windowPeak = v;
          }
        }
        final ended = endpointer.acceptAndCheckDone(chunk);
        // A PHRASE IS READ WORD BY WORD (her log, 2026-10-10: the
        // challenge cut her off -- 2 s of audio, one word of three heard,
        // and the rest caught as something "said over the command"). Words
        // a person is reading off the screen come with pauses longer than
        // a command's, so the challenge and enrolment windows wait
        // [_phraseGrace] more after the usual end of speech; talking again
        // in that time carries on the same recording.
        final phrase = _role == _CaptureRole.challenge ||
            _role == _CaptureRole.enrollment;
        if (phrase && _phrasePause != null) {
          if (endpointer.heardSpeech) {
            _phrasePause = null; // went on reading
          } else if (DateTime.now().difference(_phrasePause!) >= _phraseGrace) {
            _phrasePause = null;
            _finishCapture();
            break;
          }
        }
        if (ended) {
          if (phrase) {
            _phrasePause = DateTime.now();
            endpointer.reset();
            break;
          }
          _finishCapture();
        }
        break; // FIX: was falling through to thinking/speaking
      case FlowState.thinking:
        // Only the cancel watch listens here (see _armCancelWatch): the
        // wake word still cannot fire, and nothing heard is a command.
        _feedCancelWatch(chunk);
        break;
      case FlowState.speaking:
        // The wake word cannot fire off the app's own voice, and a reply
        // cannot be recorded over it. Only a short "stop" is listened for.
        _feedStopWatch(chunk);
        break;
    }
  }

  /// Manual trigger: does exactly what a wake-word HIT does. Exists so the
  /// downstream pipeline (capture -> Moonshine -> command frame -> server
  /// -> spoken reply) can be exercised and debugged INDEPENDENTLY of wake
  /// word detection. A gate failure and a pipeline failure look identical
  /// from the outside; this separates them.
  Future<void> triggerCapture() => _onWake();

  /// Fire a server MENU action (from the Manage drawer). These map onto the
  /// server's menu frames -- list/add/remove devices and scenes, enrollment,
  /// PIN, IR learning. PIN-gated actions are enforced SERVER-side; the drawer
  /// only labels them. Fire-and-forget: the server drives any follow-up
  /// (prompt/choice/record) back through the normal frame loop, so this needs
  /// no local state.
  void sendMenuAction(String action) {
    if (!socket.send(menuFrame(action))) {
      onStatus('not connected — cannot send that yet');
    }
  }

  /// True while a server QUESTION (or a screen's own list) is waiting on a
  /// capture from this flow. A tapped answer goes through AppState, which
  /// calls abortCapture and so clears this -- a late voice window then
  /// stops re-arming instead of sending a second answer.
  bool _awaitingAnswer = false;

  /// Bumped on every abort. Question handlers snapshot it before their
  /// awaited TTS and compare after: a cancel that lands mid-sentence must
  /// not have the handler open a capture window when the speech returns —
  /// that zombie window was most of why cancel felt laggy.
  int _epoch = 0;

  /// Close any open capture window WITHOUT sending anything. Called by the
  /// UI layer when a tapped/typed answer (or a cancel) supersedes the voice
  /// path — the screen sends its own frame, so the voice window must not be
  /// allowed to finish later and send a duplicate.
  void abortCapture() {
    _epoch++;
    // Cut the voice off mid-sentence: cancel must FEEL like cancel, not
    // "wait for the question to finish being read".
    voice.stop();
    _awaitingAnswer = false;
    _closeWindow(abort: true);
  }

  /// Read a screen out, and leave the mic alone.
  ///
  /// Shared by the form, the registration form and the automation builder,
  /// and by the dock's "read it to me again". It speaks from whatever
  /// state it is in and restores that state, so a re-read never opens,
  /// closes or steals a capture window.
  Future<void> _sayScreen(String text) async {
    if (text.trim().isEmpty) return;
    _screenSpeech = text;
    // A screen arriving while `thinking` IS the hub's reply: it now waits
    // on the screen, so the flow comes to rest instead of restoring
    // `thinking` until the watchdog gave up a minute later.
    if (state == FlowState.thinking) _thinkWatchdog?.cancel();
    final resume = (state == FlowState.speaking || state == FlowState.thinking)
        ? FlowState.idleListening
        : state;
    final role = _role;
    final epoch = _epoch;
    state = FlowState.speaking;
    await voice.say(text);
    if (state == FlowState.speaking && epoch == _epoch) {
      if (resume == FlowState.capturing) {
        // The window was open under the sentence: its collector heard the
        // phone and its timer may have lapsed meanwhile. A fresh one.
        _openWindow(role);
        return;
      }
      state = resume;
      onStatus('answer on screen, or tap the mic to hear it again');
    }
  }

  /// Read the screen out again (the dock's tap on a form). Nothing to
  /// say means nothing happens -- never a stale line from a screen that
  /// has since closed, which is what `result` clears.
  Future<void> sayScreenAgain() => _sayScreen(_screenSpeech);

  /// What the LAST form/registration/builder frame should say when asked
  /// again. Kept so the dock can re-read a screen the person is standing
  /// on minutes later, without the hub re-sending anything.
  String _screenSpeech = '';

  /// A form, spoken: its title, its note, then the fields BY NAME, in the
  /// order they are drawn ("question plus field names", her choice
  /// 2026-09-11) -- so a form can be filled in from across the room
  /// without reading it. A required field says so; a secret one is named
  /// and NEVER read back, whatever it already holds.
  String _formSpeech(Map<String, dynamic> raw) {
    // The hub's form frame carries its title as `text` (WebSocketIO.form),
    // which is where the form screen reads it too.
    final title = (raw['text'] as String? ?? raw['title'] as String? ?? '')
        .trim();
    final note = _undecorate((raw['note'] as String? ?? '').trim());
    final fields = <String>[];
    for (final f in (raw['fields'] as List? ?? const [])) {
      if (f is! Map) continue;
      final label = ((f['label'] ?? f['name'] ?? '') as Object).toString().trim();
      if (label.isEmpty) continue;
      fields.add(f['required'] == true ? '$label, required' : label);
    }
    final parts = <String>[
      if (title.isNotEmpty) title,
      if (note.isNotEmpty) note,
      if (fields.isNotEmpty)
        'It asks for: ${_spokenList(fields)}.'
      else
        'Nothing to fill in.',
      'Fill it in on screen; tap the mic to hear it again.',
    ];
    return parts.join('. ').replaceAll('.. ', '. ');
  }

  /// The registration form, spoken. Its fields are the screen's own rather
  /// than the frame's, so they are named here: the four everybody sees,
  /// plus the range only when the chosen kind actually carries one.
  String _registrationSpeech(Map<String, dynamic> raw) {
    final title = (raw['title'] as String? ?? 'Set up this device').trim();
    final values = raw['values'];
    final v = values is Map ? values : const {};
    final name = (v['name'] ?? '').toString().trim();
    final kind = (v['kind'] ?? '').toString().trim().toLowerCase();
    final positional = v['positional'] == true;
    final hasRange = kind == 'level' ||
        kind == 'reading' ||
        (kind == 'open' && positional);
    final fields = <String>[
      'room',
      'name',
      'what it does',
      'how it behaves',
      if (hasRange) 'its smallest and largest value, and the step',
    ];
    final parts = <String>[
      name.isEmpty ? title : '$title: $name',
      'It asks for: ${_spokenList(fields)}.',
      if (raw['ai_available'] == true)
        'The AI can fill in how it behaves, if you want it to.',
      'Fill it in on screen; tap the mic to hear it again.',
    ];
    return parts.join('. ');
  }

  /// "a, b and c" — a list a mouth can read, rather than commas to the end.
  static String _spokenList(List<String> items) {
    if (items.isEmpty) return '';
    if (items.length == 1) return items.first;
    return '${items.sublist(0, items.length - 1).join(', ')} '
        'and ${items.last}';
  }

  /// STOP TALKING, AND ONLY THAT (2026-09-11: "why is the dock not having
  /// an x to stop speaking").
  ///
  /// Deliberately NOT abortCapture. That one bumps `_epoch`, which is how
  /// a cancel tells every awaiting handler "the thing you were reading is
  /// gone, do not open a window when you come back" -- right for a cancel,
  /// wrong here. Silence is not cancellation: a list you have stopped
  /// listening to is a list you are ready to answer.
  ///
  /// So this only ends the utterance. `voice.say()` returns as if the
  /// sentence had finished, the epoch is untouched, and the handler
  /// carries on into exactly what it would have done next -- for a choice,
  /// opening the capture window. Say "stop", get the mic.
  void hush() {
    voice.stop();
  }

  /// Set by AppState: true for a prompt the app is answering silently from
  /// the AI-review form's script — such a question is neither shown nor
  /// spoken, so this flow must skip it entirely.
  bool Function(String text)? isAutoAnswered;

  /// Set by AppState: true (once) for a `choice` frame the app answered
  /// without showing it -- the automation a tap already chose, an armed
  /// cancel. Such a list is not read out and gets no window.
  bool Function(Map<String, dynamic> frame)? isChoiceAnsweredSilently;

  /// Set by AppState: true while a MENU action is running (add device,
  /// update scene…). Those flows are worked through on screen, so a picker
  /// inside one must NOT open the mic by itself — the same tap-to-talk rule
  /// enrollment and challenges already follow. A picker raised by a spoken
  /// COMMAND still listens, because there the person's hands are nowhere
  /// near the phone.
  bool Function()? isMenuFlow;

  /// Set by AppState: called the moment a voice-captured recording (an
  /// enrollment phrase or a challenge repeat) has been SENT. This flow
  /// sends those frames itself — straight onto the socket — so AppState's
  /// pendingRecord mirror never saw the send and stayed stale. The stale
  /// record then outranked the next question in the ceremony room: after
  /// the last phrase, the PIN prompt arrived and the room kept showing the
  /// phrase (field test 2026-08-27, "no pin screen to write the pin").
  void Function()? onRecordingSent;

  /// Set by AppState: called the moment a voice-captured PROMPT answer has
  /// been sent. Same reason as onRecordingSent -- this flow sends the
  /// prompt_reply itself, so without the hook AppState's pendingPrompt
  /// stayed set, the answered question stayed on screen, and a later
  /// cancel picked the prompt branch and sent the wrong reply type for
  /// the choice or form the hub had moved on to.
  void Function()? onPromptSent;

  /// The `user_id` that rode on the last auth_ok -- empty for an unbound
  /// phone. Only then is "no voice is enrolled yet" a true thing to say
  /// when a view:enroll arrives; an enrolled person adding a second voice
  /// gets the same frame and must not be told to start over.
  String _boundUser = '';

  /// Console dressing stripped from a line bound for TTS: banner runs
  /// ("=" * 60, "--- SET PIN ---"), box rules, arrows. A speech engine
  /// reads a 60-character banner as "equals equals equals…" for minutes —
  /// the field-tested "tts keeps saying ====== continuously" (2026-08-27),
  /// heard all through registration. What survives is what is worth
  /// SAYING; empty means the line was decoration through and through.
  static String _undecorate(String text) {
    return text
        .replaceAll(RegExp(r'[=\-_*#~•·│┌┐└┘├┤─]{2,}'), ' ')
        .replaceFirst(RegExp(r'^\s*->\s*'), '')
        .trim();
  }

  /// Same ritual-stripping patterns as PromptScreen.displayText: brackets
  /// carrying y/n-style alternatives and the "(Enter=keep, 'prev' …)" hint
  /// are rendered as buttons, so neither should be read aloud. A
  /// current-value bracket like "Room [kitchen]" survives.
  String _stripRitual(String text) {
    var t = text.trim();
    t = t.replaceFirst(
        RegExp(r'\s*\[y[^\]]*[/=][^\]]*\]\s*:?\s*$', caseSensitive: false),
        '');
    t = t.replaceFirst(
        RegExp(r'\s*\[on ?/ ?off\]\s*:?\s*$', caseSensitive: false), '');
    t = t.replaceFirst(
        RegExp(r"\s*\(enter=keep[^)]*\)\s*:?\s*$", caseSensitive: false), '');
    t = t.trimRight();
    if (t.endsWith(':')) t = t.substring(0, t.length - 1).trimRight();
    return t.trim();
  }

  /// A second tap while recording: stop and send NOW, instead of waiting
  /// for the silence detector. Only for a capture that is answering a
  /// question — a command capture keeps its own endpointing.
  void finishNow() {
    if (state != FlowState.capturing) return;
    _finishCapture();
  }

  /// Dock tap-to-talk: open a capture window for the question the server is
  /// waiting on (the UI layer knows which one from its pending state), or
  /// fall through to an ordinary command capture. Only from true idle — an
  /// open window, TTS mid-sentence, or a command in flight is never stomped.
  Future<void> reopenFor(String kind) async {
    if (state != FlowState.idleListening) return;
    // With the phone wake engine the mic is CLOSED while idle. Take it
    // before the window opens: for a challenge or an enrolment the first
    // syllables are the voiceprint, and a window that opened deaf would
    // time out as "nothing recorded" with no word about why.
    if (_phoneWake != null) {
      if (!await _acquireMic()) {
        onStatus('the microphone is busy — try again');
        await _applyMicPolicy();
        return;
      }
      if (state != FlowState.idleListening) return;
    }
    _CaptureRole role;
    switch (kind) {
      case 'challenge':
        role = _CaptureRole.challenge;
        break;
      case 'record':
        role = _CaptureRole.enrollment;
        break;
      case 'prompt':
        role = _CaptureRole.prompt;
        break;
      case 'choice':
        role = _CaptureRole.choiceReply;
        break;
      default:
        role = _CaptureRole.command;
    }
    _awaitingAnswer = role != _CaptureRole.command;
    _openWindow(role);
  }

  /// What the wake word alone is answered with.
  static const _wakeAnswer = 'Yes?';

  Future<void> _onWake({bool answer = false}) async {
    // FIX: the manual trigger (orb tap) used to fire unconditionally. Tapped
    // while a challenge/enrollment/prompt window was open, it re-opened the
    // window as a COMMAND — so the challenge answer went out as a command
    // frame and the server's question timed out. Only wake from true idle.
    if (state != FlowState.idleListening) return;
    if (_phoneWake != null) {
      // The recogniser heard the phrase (or the orb was tapped). Take the
      // microphone back BEFORE the window opens, so the collector's first
      // chunks are the person's first words.
      if (!await _acquireMic()) {
        onStatus('the microphone is busy — try again');
        await _applyMicPolicy(); // idle again: hand it back to the recogniser
        return;
      }
      if (state != FlowState.idleListening) return; // something else woke
    }
    // AUTHENTICATING IS ALREADY CONNECTED: the socket is open and the
    // handshake is a moment from auth_ok. connect() here tore that socket
    // down and started the whole sweep again.
    if (socket.state != ConnState.ready &&
        socket.state != ConnState.authenticating) {
      state = FlowState.speaking;
      final url = await socket.connect();
      if (url == null) {
        await voice.say('I could not reach the house on any known address.');
        state = FlowState.idleListening;
        return;
      }
      // auth_ok arrives via the frame stream; open the window optimistically
      // -- a failed auth closes the socket and the send below reports it.
    }
    if (answer && state == FlowState.idleListening) {
      // "Yes?" -- then listen. Spoken BEFORE the window opens, so the
      // window never hears the phone's own voice (and the echo tail
      // _openWindow keeps after any speech covers the room's reflection).
      // The trailing silence the wake word left is no command: dropped.
      final epoch = _epoch;
      state = FlowState.speaking;
      _gateSeed = null;
      _gateSeedAt = null;
      onStatus(_wakeAnswer);
      try {
        await voice.say(_wakeAnswer);
      } catch (_) {}
      // Something else took the floor meanwhile (a frame from the hub, a
      // stop): it owns the flow now, and this wake gives way.
      if (_freed || epoch != _epoch || state != FlowState.speaking) return;
      state = FlowState.idleListening;
    }
    _openWindow(_CaptureRole.command);
  }

  /// TAKE THE MICROPHONE, WHOEVER OPENED THIS WINDOW (2026-09-11).
  ///
  /// THE BUG THIS FIXES, and it is the one behind "it's not hearing at all
  /// when options are laid": of eighteen places that open a capture
  /// window, exactly TWO took the microphone first -- `reopenFor` and
  /// `_onWake`, which are the dock's tap and the orb's. Every automatic
  /// window -- the one a `choice` frame opens after reading the options,
  /// every keep-alive re-arm, every screen that arms its own list -- just
  /// opened a collector and hoped.
  ///
  /// With the phone's own recogniser as the wake engine that hope is
  /// misplaced: the recogniser HOLDS the microphone while the flow is
  /// idle, and handing it over is `_applyMicPolicy`, which is queued,
  /// fire-and-forget, and reports a failure to `debugPrint` and nobody
  /// else. So a tap worked and a list of options did not, which is
  /// precisely what she reported -- and the window then re-armed itself
  /// forever, silently, because a window that hears nothing looks exactly
  /// like a room that is quiet.
  ///
  /// `_acquireMic` is the call that actually waits for the swap and says
  /// whether it got the microphone. Every window gets it now. Safe to
  /// start after the collector subscribes: `mic.chunks` is a broadcast
  /// stream, so a subscriber that arrives before the microphone does
  /// still receives every chunk once it starts.
  void _ensureMicForWindow() {
    if (_phoneWake == null) return;   // bundled spotter: the mic is ours
    _acquireMic().then((ok) {
      if (!ok && state == FlowState.capturing) {
        _micStarved = true;
        onStatus('the microphone is busy — tap the mic to try again');
      }
    });
  }

  /// Chunks and loudness seen since this window opened, and whether it is
  /// getting nothing. A capture that hears nothing is indistinguishable
  /// from a quiet room unless somebody counts -- which is how a dead
  /// microphone stayed invisible through three rounds of looking at it.
  int _windowChunks = 0;
  int _windowPeak = 0;
  bool _micStarved = false;
  Timer? _starveTimer;

  /// True when the window that is open right now is receiving no usable
  /// audio. The dock says so rather than sitting on "listening…".
  bool get micStarved => _micStarved;

  void _watchForSilence() {
    _windowChunks = 0;
    _windowPeak = 0;
    _micStarved = false;
    _starveTimer?.cancel();
    // Long enough that a person drawing breath is not called a fault,
    // short enough to be told before the window times out at eight.
    _starveTimer = Timer(const Duration(milliseconds: 1500), () {
      if (state != FlowState.capturing) return;
      if (_windowChunks == 0) {
        // Nothing at all: the stream is not running, or another app has
        // it. This is the dead-microphone case and it has a fix.
        _micStarved = true;
        onStatus('no audio from the microphone — '
            'another app may be holding it');
      } else if (_windowPeak < 120) {
        // Chunks arriving, all of them near zero. On Android the loser of
        // a microphone contest is handed BUFFERS OF SILENCE rather than
        // being stopped, so this is a different fault from the one above
        // and deserves different words.
        _micStarved = true;
        onStatus('the microphone is on but silent — '
            'check the app has permission, or that nothing else is '
            'recording');
      }
    });
  }

  /// How long after the phone stops talking a window keeps its ears shut.
  /// Chunks reach Dart late, so a window opened the instant TTS ended is
  /// first handed the end of its own sentence -- and the choice tail ends
  /// "...or cancel to stop", which transcribed alone is a cancel.
  static const Duration _echoTail = Duration(milliseconds: 300);
  DateTime? _ignoreAudioUntil;

  void _openWindow(_CaptureRole role) {
    // After stop() the endpointer's native model is freed; a handler that
    // was mid-sentence when the pipeline stopped must not reach it.
    if (_freed) return;
    _disarmCancelWatch(); // the window owns the endpointer from here
    _disarmStopWatch();
    _role = role;
    _phrasePause = null;
    endpointer.reset();
    final spokeEnd = voice.lastSpokeEnd;
    final now = DateTime.now();
    final sinceVoice = spokeEnd == null ? null : now.difference(spokeEnd);
    _ignoreAudioUntil = (sinceVoice != null && sinceVoice < _echoTail)
        ? now.add(_echoTail - sinceVoice)
        : null;
    // A re-armed window (choice keep-alive) replaces a live collector —
    // abort it first or its mic subscription leaks, one per re-arm.
    _collector?.abort();
    _collector =
        UtteranceCollector(mic.chunks, ignoreUntil: _ignoreAudioUntil);
    // Words said while the wake phrase was still being read.
    final seed = _gateSeed;
    final seedAt = _gateSeedAt;
    _gateSeed = null;
    _gateSeedAt = null;
    if (seed != null &&
        seed.isNotEmpty &&
        role == _CaptureRole.command &&
        seedAt != null &&
        now.difference(seedAt) < const Duration(seconds: 4)) {
      _collector!.seed(seed);
    }
    state = FlowState.capturing;
    _ensureMicForWindow();
    _watchForSilence();
    onStatus('listening…');
    _windowTimer?.cancel();
    _windowTimer = Timer(AppConfig.activeWindow, () {
      if (state != FlowState.capturing) return;
      // STILL TALKING AT THE BELL: what was said so far is the answer. A
      // long command (or a word list read slowly) used to be thrown away
      // whole as "heard nothing" because the endpointer had not yet heard
      // the pause after it.
      if (endpointer.heardSpeech &&
          (_role == _CaptureRole.command ||
              _role == _CaptureRole.choiceReply ||
              _role == _CaptureRole.localText)) {
        _finishCapture();
        return;
      }
      switch (_role) {
        case _CaptureRole.command:
          _closeWindow(abort: true);
          onStatus('heard nothing — back to sleep');
          break;
        case _CaptureRole.prompt:
          // FIX (auto-send while typing): a TYPED answer is live on screen
          // for these. "Silence must still answer" fired an (often empty)
          // reply when this window expired — which is how the PIN field
          // submitted itself 8 seconds in. Close quietly; the question
          // stays live for typing/tapping, or a dock-mic tap to speak.
          // The server's own reply timeout still bounds abandonment.
          _closeWindow(abort: true);
          onStatus('answer on screen, or tap the mic to speak');
          break;
        case _CaptureRole.choiceReply:
          // SELECTION IS DIFFERENT: the person is browsing a list, and
          // "quiet close" here is why the mic silently died seconds into
          // picking devices. While the choice is still unanswered the
          // window RE-ARMS itself — listening ends only when an answer is
          // sent, or X / Cancel aborts (both clear _awaitingAnswer via
          // abortCapture), or the server ends the choice (result clears
          // the options). No auto-submit risk: a choice has no typing
          // field and an empty transcript never answers.
          if (_awaitingAnswer && _choiceOptions.isNotEmpty) {
            _openWindow(_CaptureRole.choiceReply);
            return;
          }
          _closeWindow(abort: true);
          onStatus('answer on screen, or tap the mic to speak');
          break;
        case _CaptureRole.localText:
          // A field waiting for a value behaves like a selection, not like
          // a prompt: the person is looking at the form, deciding. Silence
          // RE-ARMS while the field is still open, so the mic does not
          // quietly die between "Room?" and the person answering it. It
          // ends when the value arrives, when the screen disarms, or on a
          // spoken cancel -- never by writing an empty value.
          if (_localTextSink != null) {
            _openWindow(_CaptureRole.localText);
            return;
          }
          _closeWindow(abort: true);
          onStatus('type it, or tap the mic to say it');
          break;
        case _CaptureRole.challenge:
        case _CaptureRole.enrollment:
          // These windows are opened BY A TAP now, so an empty one means
          // the person tapped and did not speak — sending that as a
          // recording only earns a rejection and a re-prompt. Close it and
          // wait for another tap; the question stays outstanding.
          // "Empty" is judged by the endpointer, not by bytes: eight
          // seconds of a quiet room is 256 KB, and that silence went up as
          // the voiceprint and failed the check.
          if ((!endpointer.heardSpeech && _phrasePause == null) ||
              (_collector?.length ?? 0) < 9600) {
            // < 0.3s at 16 kHz mono 16-bit
            _closeWindow(abort: true);
            _awaitingAnswer = true; // the question is still open
            onStatus('nothing recorded — tap the mic and say it');
            break;
          }
          _finishCapture();
          break;
      }
    });
  }

  void _closeWindow({required bool abort}) {
    _windowTimer?.cancel();
    _windowTimer = null;
    _starveTimer?.cancel();
    _starveTimer = null;
    _micStarved = false;
    if (abort) {
      _collector?.abort();
      _collector = null;
      state = FlowState.idleListening;
    }
  }

  /// Notify upstream that a transcript is ready. Returns true if upstream
  /// consumed it (intercepted or handled itself), in which case VoiceFlow
  /// must NOT send its own duplicate frame.
  bool _onTranscriptReady(String text) {
    final consumed = onTranscript?.call(text) ?? false;
    return consumed;
  }

  /// About this many words are spoken before the house stops to listen
  /// (see _readAloud). A line is never split, so a longer line is one chunk.
  static const int _chunkWords = 30;

  /// How long the house listens between two chunks of a long reading, once
  /// its own voice has died away (see _echoTail). Long enough to start a
  /// sentence in, short enough that a reading is not a series of pauses.
  static const Duration _listenGapLength = Duration(milliseconds: 700);

  /// Lines, grouped into chunks of about [_chunkWords] words, each chunk one
  /// spoken sentence. EVERY line is in some chunk: nothing is left "on
  /// screen" -- a person who asked a question is owed the whole answer.
  static List<String> _chunksOf(List<String> lines) {
    final chunks = <String>[];
    var cur = <String>[];
    var words = 0;
    for (final line in lines) {
      final n = line.split(RegExp(r'\s+')).where((w) => w.isNotEmpty).length;
      if (cur.isNotEmpty && words + n > _chunkWords) {
        chunks.add(cur.join('. '));
        cur = <String>[];
        words = 0;
      }
      cur.add(line);
      words += n;
    }
    if (cur.isNotEmpty) chunks.add(cur.join('. '));
    return chunks;
  }

  /// Say [chunks] one after another, listening for [_listenGapLength] after
  /// each but the last. Returns true when reading stopped early: the person
  /// began speaking in a gap (the window the gap opened is already
  /// capturing it, and the usual handling takes it from there), or the flow
  /// was cancelled or taken over. The caller then does nothing more. False
  /// means every chunk was said, and the caller carries on as it always did.
  Future<bool> _readAloud(List<String> chunks,
      {required _CaptureRole gapRole}) async {
    final epoch = _epoch;
    for (var i = 0; i < chunks.length; i++) {
      state = FlowState.speaking;
      await voice.say(chunks[i]);
      // Cancelled, freed, or another path took the flow while this was
      // speaking (a tap on the mic opens its own window): not ours to touch.
      if (epoch != _epoch || _freed || state != FlowState.speaking) return true;
      if (i == chunks.length - 1) break;
      if (await _listenGap(gapRole)) return true;
    }
    return false;
  }

  /// One listening gap: open the window the answer would use, and give it
  /// the gap to hear a voice. Speech began -> true, the window stays open
  /// and takes the whole sentence. Silence -> the window is closed again
  /// and false is returned.
  Future<bool> _listenGap(_CaptureRole role) async {
    _openWindow(role);
    final end = DateTime.now().add(_echoTail + _listenGapLength);
    while (DateTime.now().isBefore(end)) {
      await Future<void>.delayed(const Duration(milliseconds: 40));
      // Something else took the flow (a tap, a cancel, a frame).
      if (_freed || state != FlowState.capturing) return true;
      if (endpointer.heardSpeech) return true;
    }
    _closeWindow(abort: true);
    return false;
  }

  /// [_speakThenIdle] for a long answer: all of it, in chunks.
  Future<void> _speakAllThenIdle(List<String> chunks) async {
    final interrupted =
        await _readAloud(chunks, gapRole: _CaptureRole.command);
    if (interrupted) return;
    state = FlowState.idleListening;
    onStatus(_listeningStatus);
  }

  /// Close the window and act on what it heard. Called unawaited (from
  /// the endpointer, the window timer and a second tap), so a throw in
  /// cleaning or transcription used to leave the flow in `thinking` with
  /// no watchdog armed -- deaf until restart. Guarded here, and the role
  /// is read ONCE: a window re-armed across an await must not change what
  /// this capture answers.
  Future<void> _finishCapture() async {
    final role = _role;
    try {
      await _finishCaptureAs(role);
    } catch (e) {
      debugPrint('[flow] capture failed: $e');
      if (state == FlowState.thinking) {
        // Nothing usable was sent: the question, if any, is still open.
        if (role == _CaptureRole.challenge ||
            role == _CaptureRole.enrollment ||
            role == _CaptureRole.prompt) {
          _awaitingAnswer = true;
        }
        state = FlowState.idleListening;
        onStatus('that recording could not be processed — try again');
      }
    }
  }

  /// "CANCEL" SAID INSTEAD OF THE WORDS ENDS THE WHOLE CHECK. A voice
  /// check or an enrolment phrase is sent up as audio and never read here,
  /// so a person who says "cancel" in that window used to have it taken for
  /// a failed attempt. A clip that is only a short word (under two seconds
  /// of voice once the quiet is cut away -- a phrase read aloud is longer)
  /// is read first, and if it means cancel the window's question is
  /// cancelled by the same path as the X button. Not when the asked words
  /// themselves include what was heard. True: handled, send nothing.
  Future<bool> _cancelSaidInstead(Uint8List rawPcm) async {
    final span = _trimToSpeech(rawPcm);
    if (span.length < 3200 || span.length > 16000 * 2 * 2) return false;
    try {
      final clean = AudioCleaner.cleanForTranscription(span, denoiser: denoiser);
      final said = _hear(clean, span).trim();
      if (_freed || said.isEmpty || !_meansCancel(said)) return false;
      final asked = _norm(_askedPhrase).split(' ');
      if (_norm(said).split(' ').any(asked.contains)) return false;
      debugPrint('[flow] "$said" said instead of the words -- cancelling');
      onChoiceHeard?.call(said, const ['cancelling']);
      onStatus('cancelling…');
      _awaitingAnswer = false;
      onChoiceCancel?.call();
      return true;
    } catch (e) {
      debugPrint('[flow] cancel check: $e');
      return false;
    }
  }

  Future<void> _finishCaptureAs(_CaptureRole role) async {
    _windowTimer?.cancel();
    // The spoken answer is now committing; a card tap after this is a no-op.
    _awaitingAnswer = false;
    final rawPcm = _collector?.finish() ?? Uint8List(0);
    _collector = null;
    debugPrint('[flow] window closed: role=$role, '
        '${(rawPcm.length / 32000).toStringAsFixed(2)} s captured, '
        'chunks=$_windowChunks, peak>=$_windowPeak');
    state = FlowState.thinking;
    // Listen over it from now, while the words are still being read: a
    // "cancel" said here used to go unheard (2026-10-09).
    if (role == _CaptureRole.command) _armCancelWatch();
    onStatus('working…');

    // A second tap straight after the first (finishNow) closes a window
    // that holds nothing. A voiceprint cannot be taken from that; sending
    // it only earns a failed check. The question stays open for a retry.
    if ((role == _CaptureRole.challenge || role == _CaptureRole.enrollment) &&
        rawPcm.length < 9600) {
      _awaitingAnswer = true;
      state = FlowState.idleListening;
      onStatus('nothing recorded — tap the mic and say it');
      return;
    }

    // The clip that leaves the phone: ALWAYS the raw capture.
    final clip = encodeWavB64(rawPcm);

    // Size check BEFORE any cleaning: an oversized clip is rejected either
    // way, so it should not pay for processing it will never use.
    if (clip.b64.length > AppConfig.maxAudioB64) {
      if (role == _CaptureRole.challenge ||
          role == _CaptureRole.enrollment) {
        // The hub's question is still open (nothing was sent); a fresh
        // tap records again. Saying "shorter command" here was wrong.
        _awaitingAnswer = true;
        await _speakThenIdle('That was too long. Tap the mic and say it again.');
        return;
      }
      await _speakThenIdle('That was too long. Try a shorter command.');
      return;
    }

    // Cleaned copy exists only for roles that transcribe. cleaner.dart
    // guards the empty-capture case (silent question-window timeouts reach
    // here with zero bytes).
    final bool reads = _transcribingRoles.contains(role);
    final Uint8List readPcm = reads ? _trimToSpeech(rawPcm) : rawPcm;
    final Uint8List cleanPcm = reads
        ? AudioCleaner.cleanForTranscription(readPcm, denoiser: denoiser)
        : rawPcm;

    await _saveDebugPair(rawPcm, cleanPcm, role);
    if (_freed) return; // stopped meanwhile: the transcriber is gone

    switch (role) {
      case _CaptureRole.command:
        final text = _hear(cleanPcm, readPcm);
        if (text.isEmpty) {
          await _speakThenIdle("I didn't catch that.");
          return;
        }
        // QUIET / SPEAK UP, ANYWHERE (2026-09-11). Answered by the PHONE,
        // never sent on: the hub has no idea this handset has a speaker,
        // and "be quiet" travelling to it as a house command would be
        // resolved against the devices -- which is how a lamp called
        // "quiet corner" gets switched. Caught here, so the words work
        // from the wake word on any screen, not only over a list.
        if (_hushPhrase.hasMatch(_norm(text)) ||
            _unhushPhrase.hasMatch(_norm(text))) {
          final wantMuted = _hushPhrase.hasMatch(_norm(text));
          onMuteSpoken?.call(wantMuted);
          // Said AFTER the flag is set, so unmuting is heard and muting
          // is not -- the acknowledgement obeys the instruction. Through
          // `speaking`, so the wake word does not hear it from idle.
          if (!wantMuted) {
            await _speakThenIdle('Talking again.');
          } else {
            state = FlowState.idleListening;
            onStatus('quiet');
          }
          break;
        }
        onStatus('"$text"');
        // FIX: wire _onTranscriptReady so AppState can intercept without
        // causing a duplicate command frame.
        if (_onTranscriptReady(text)) {
          state = FlowState.idleListening;
          return;
        }
        _resultLines.clear(); // fresh command, fresh spoken summary
        _commandAnswer = true;
        if (!socket.send(commandFrame(text,
            audioB64: clip.b64, ui: uiContext?.call()))) {
          await _speakThenIdle(
              'The connection dropped. Say the wake word to retry.');
          break;
        }
        // Speech was already heard over it: the hub holds before any
        // device until that is read.
        if (_holdWanted) socket.send({'type': 'hold'});
        // stay in `thinking`; the server's frames drive us from here —
        // bounded by the watchdog so a lost reply can't kill the mic.
        _armThinkWatchdog();
        break;

      case _CaptureRole.challenge:
        if (await _cancelSaidInstead(rawPcm)) return;
        if (_freed) return;
        // Verification wants the RAW voice, not a transcript -- send as-is.
        if (!socket.send(challengeResponseFrame(clip.b64))) {
          await _speakThenIdle('The connection dropped during the voice check.');
          break;
        }
        onRecordingSent?.call();
        _armThinkWatchdog();
        break;

      case _CaptureRole.enrollment:
        if (await _cancelSaidInstead(rawPcm)) return;
        if (_freed) return;
        if (!socket.send(recordingFrame(clip.b64))) {
          await _speakThenIdle('The connection dropped during enrollment.');
          break;
        }
        // Tell AppState the phrase is away — its pendingRecord mirror must
        // fall back to "the hub is checking", or the ceremony room keeps
        // showing a phrase the hub already has.
        onRecordingSent?.call();
        _armThinkWatchdog();
        break;

      case _CaptureRole.prompt:
        final value = _hear(cleanPcm, readPcm).trim();
        if (value.isEmpty) {
          // FIX: an empty transcript used to be sent as the answer —
          // exactly how a noise-endpointed capture answered the PIN with
          // "". The question stays live for typing or another mic tap.
          _awaitingAnswer = true;
          state = FlowState.idleListening;
          onStatus("didn't catch that — type it, or tap the mic again");
          break;
        }
        // A SPOKEN CANCEL IS A CANCEL, sent the way the screen's Cancel
        // sends it (the `cancelled` flag, the word riding along for an
        // older hub) -- never as the answer, where a caller that forgot to
        // check would store "cancel" as a name or try it as the PIN.
        final cancelled = _meansCancel(value);
        if (!socket.send(cancelled
            ? {'type': 'prompt_reply', 'cancelled': true, 'value': 'cancel'}
            : promptReply(value))) {
          await _speakThenIdle('The connection dropped.');
          break;
        }
        // The answer is away: AppState drops its pendingPrompt mirror, and
        // the watchdog bounds the wait exactly as it does for a command.
        onPromptSent?.call();
        _armThinkWatchdog();
        break;

      case _CaptureRole.localText:
        // A VALUE FOR A FIELD. Nothing is matched and nothing is sent: the
        // words go to the screen that asked for them.
        final sink = _localTextSink;
        if (sink == null) {
          state = FlowState.idleListening;
          onStatus(_listeningStatus);
          break;
        }
        if (rawPcm.length < 9600) {
          // Under 0.3 s -- a twitch, not speech. Keep the field open.
          _awaitingAnswer = true;
          _openWindow(_CaptureRole.localText);
          break;
        }
        final dictated = _hear(cleanPcm, readPcm).trim();
        if (dictated.isEmpty) {
          // NEVER write "" into the box. An empty transcript is the
          // thing that answered a PIN prompt with nothing; here it just
          // means say it again.
          _awaitingAnswer = true;
          _openWindow(_CaptureRole.localText);
          onStatus("didn't catch that — say it again, or type it");
          break;
        }
        if (_hushPhrase.hasMatch(_norm(dictated)) ||
            _unhushPhrase.hasMatch(_norm(dictated))) {
          onMuteSpoken?.call(_hushPhrase.hasMatch(_norm(dictated)));
          _awaitingAnswer = true;
          _openWindow(_CaptureRole.localText); // the field is still open
          break;
        }
        if (_meansCancel(dictated)) {
          final cancel = _localCancel;
          _localTextSink = null;
          _localCancel = null;
          _awaitingAnswer = false;
          _closeWindow(abort: true);
          state = FlowState.idleListening;
          onStatus(_listeningStatus);
          cancel?.call();
          break;
        }
        _localTextSink = null;
        _awaitingAnswer = false;
        _closeWindow(abort: true);
        onChoiceHeard?.call(dictated, const []);
        // The screen decides what happens next -- usually re-arming its
        // field list, which is why this hands over BEFORE going idle.
        sink(dictated);
        if (state != FlowState.speaking && state != FlowState.capturing) {
          state = FlowState.idleListening;
          onStatus(_listeningStatus);
        }
        break;

      case _CaptureRole.choiceReply:
        // ONE UTTERANCE, ONE ANSWER. Silence is the terminator: whatever
        // was said goes up in a single frame, immediately, and the hub
        // decides which option it named.
        //
        // The previous shape asked for a spoken "done" to submit a
        // multi-select, and accumulated in the meantime. That is why it
        // "listened but never sent": the answer sat in the screen's
        // selection waiting for a keyword nobody says. There is no keyword
        // now, no accumulation, and no second turn to get wrong.
        //
        // The choice may also have ended while this capture was in flight
        // (server timeout, a tap answered it). `result` clears the options,
        // and that is the bound on the keep-listening loop below.
        if (_choiceOptions.isEmpty) {
          state = FlowState.idleListening;
          onStatus(_listeningStatus);
          break;
        }
        // Nothing was SAID yet — a VAD twitch on room noise, or the window
        // simply timing out while the list is being read. Re-open and keep
        // listening; silence before speech ends nothing. (Too short to be
        // speech is skipped without paying for a transcription that can
        // only come back empty.)
        if (rawPcm.length < 9600) {
          // < 0.3s at 16 kHz mono 16-bit
          _awaitingAnswer = true;
          _openWindow(_CaptureRole.choiceReply);
          break;
        }
        final said = _hear(cleanPcm, readPcm).toLowerCase().trim();
        if (said.isEmpty) {
          _awaitingAnswer = true;
          _openWindow(_CaptureRole.choiceReply);
          onStatus('listening — say a name, or tap one');
          break;
        }

        // ── Spoken controls, before any matching ──────────────────────
        // QUIET / SPEAK UP: the phone stops reading, the question stays.
        // First, because a person who has said "be quiet" must not then
        // hear the list re-read at them for not matching an option.
        if (_hushPhrase.hasMatch(_norm(said)) ||
            _unhushPhrase.hasMatch(_norm(said))) {
          final wantMuted = _hushPhrase.hasMatch(_norm(said));
          onChoiceHeard?.call(said, [wantMuted ? 'quiet' : 'speaking again']);
          onMuteSpoken?.call(wantMuted);
          _awaitingAnswer = true;   // the question is untouched
          _openWindow(_CaptureRole.choiceReply);
          onStatus(wantMuted
              ? 'quiet — the options are still on screen'
              : 'listening — say a name, or tap one');
          break;
        }
        // REPEAT: read the list again. Costs nothing server-side, and it is
        // the answer to "I missed one" — which used to mean tapping.
        if (_repeatPhrase.hasMatch(_norm(said))) {
          onChoiceHeard?.call(said, const ['reading the options again']);
          _missStreak = 0; // an explicit re-read is not a failed attempt
          final repEpoch = _epoch;
          state = FlowState.speaking;
          var repStopped = false;
          if (_localRepeatLine.isNotEmpty) {
            await voice.say(_localRepeatLine);
          } else {
            repStopped = await _readOptions('');
          }
          if (repEpoch != _epoch || repStopped) break; // cancelled or answered
          _awaitingAnswer = true;
          _openWindow(_CaptureRole.choiceReply);
          break;
        }
        // CANCEL: goes to the SERVER, through the same path the on-screen
        // Cancel button uses — the question is closed, the screen pops, and
        // the shell lock is released. Saying it and tapping it must not
        // mean two different things.
        if (_meansCancel(said) && !_noneIsAnAnswer(said)) {
          _cancelBySpeech(said);
          break;
        }

        // "All of them" is not read here any more. It went to the hub as
        // a SELECTION of every row, which is how "all except the fan"
        // switched the fan on too; the hub reads "all", "both" and "all
        // except …" from the words, against what it offered.

        // ── A SERVER LIST IS MATCHED BY THE HUB ───────────────────────
        //
        // "the orb should have already told u what works... replicate that
        // for voice dock" (2026-09-11), and she is right about why. A
        // command spoken at the orb travels to the hub as WORDS and is
        // resolved there, against the lexicon, the embeddings and the
        // resolver. A spoken answer to a LIST was resolved HERE, by a
        // matcher written from scratch in the one part of this system with
        // no test runner -- and one that scored "bob" 0.000 against "bulb"
        // because bigrams collapse on short words, which is most of the
        // names in this house.
        //
        // So this stops matching. The words go up as `heard`, the hub's
        // nlu/choice_match decides, and when it is not sure the HUB asks
        // "did you mean bulb?" -- as an ordinary choice frame, which this
        // already knows how to read out and answer. One matcher, on the
        // side that can be tested.
        //
        // A LOCAL list (the pin map, a form, the rule builder) has no hub
        // question behind it, so it cannot travel as a `choice_response`.
        // It asks the same matcher a different way -- askHubToMatch, one
        // frame out and one back -- rather than keeping a second matcher
        // here for it. See the local branch below.
        if (_localPick == null && _localTextSink == null) {
          _lastHeard = said;
          onChoiceHeard?.call(said, const ['sent to the hub']);
          _awaitingAnswer = false;
          _closeWindow(abort: true);
          final spoken = onChoiceSpoken;
          final sent =
              spoken != null ? spoken(said) : socket.send(choiceHeard(said));
          // Answered (or already gone): the hub's next frame rules now, and
          // the old list must not re-open a window.
          if (sent != false) _choiceOptions = const [];
          if (sent == null) {
            // Answered by a tap while this was being transcribed.
            state = FlowState.idleListening;
            onStatus(_listeningStatus);
            break;
          }
          if (!sent) {
            await _speakThenIdle('The connection dropped.');
            break;
          }
          state = FlowState.thinking;
          onStatus('working…');
          _armThinkWatchdog();
          break;
        }

        // ── A SCREEN'S OWN LIST, MATCHED BY THE HUB ───────────────────
        //
        // The last matching the phone did. It went the same way as the
        // server lists and for the same reason: one implementation, on the
        // side that can be tested. The screen still OWNS the question --
        // the answer is a callback, not a frame -- but what was said is
        // decided by nlu/choice_match like everything else.
        // A narrowed round asks about THOSE FEW, not the whole list --
        // and answers "yes"/"no" here, because neither names an option and
        // the matcher quite rightly finds nothing in them.
        final confirming = _narrowed.isNotEmpty;
        final pool = confirming
            ? [for (final i in _narrowed) _choiceOptions[i]]
            : _choiceOptions;
        if (confirming) {
          if (_nonePhrase.hasMatch(_plainAnswer(said))) {
            _narrowed = const [];
            onChoiceHeard?.call(said, const ['none of those']);
            final noEpoch = _epoch;
            state = FlowState.speaking;
            final noStopped = await _readOptions('The choices are:');
            if (noEpoch != _epoch || noStopped) break;
            _awaitingAnswer = true;
            _openWindow(_CaptureRole.choiceReply);
            break;
          }
          if (pool.length == 1 && _yesPhrase.hasMatch(_plainAnswer(said))) {
            _narrowed = const [];
            onChoiceHeard?.call(
                said, [(pool.first['label'] ?? '').toString()]);
            _submitChoice([(pool.first['value'] ?? '').toString()]);
            break;
          }
        }
        // A SCREEN THAT READS SOME ANSWERS ITSELF (the pin map's pin
        // numbers). Asked first, because a fuzzy match cannot be allowed
        // near them: "pin 26" scored 0.91 against "pin 2" when 26 was
        // taken, and a wrong pin is a wiring mistake. What the screen
        // takes never reaches the hub; everything else still does.
        final resolve = _localResolve;
        final screenAct =
            (_localPick != null && resolve != null) ? resolve(said) : null;
        if (screenAct != null) {
          _narrowed = const [];
          _lastHeard = said;
          _missStreak = 0;
          onChoiceHeard?.call(said, const ['on screen']);
          _awaitingAnswer = false;
          _closeWindow(abort: true); // idle: the act may speak or re-arm
          _choiceOptions = const [];
          _localPick = null;
          _localCancel = null;
          _localResolve = null;
          _localRepeatLine = '';
          onStatus(_listeningStatus);
          screenAct();
          break;
        }
        final matchEpoch = _epoch;
        final ask = await askHubToMatch(said, pool,
            multi: _choiceMulti,
            allowAll: _choiceAllowAll,
            confirming: confirming,
            cancelCheck: true);
        // Cancelled, or answered by a tap, while the hub was matching:
        // the late spoken pick must not answer a second time.
        if (matchEpoch != _epoch) break;
        if (_localPick == null && _localTextSink == null) {
          // The screen let go while the hub was answering.
          state = FlowState.idleListening;
          onStatus(_listeningStatus);
          break;
        }
        // NOTHING ON THE LIST WAS NAMED, BUT IT MEANT STOP: "no no, I
        // didn't say that" is judged by the hub by meaning (the cancel
        // check rides on the same match request). An answer always wins:
        // this is only reached when no option matched.
        if (ask.values.isEmpty && ask.cancel && !_noneIsAnAnswer(said)) {
          _cancelBySpeech(said);
          break;
        }
        if (ask.values.isNotEmpty) {
          _narrowed = const [];
          _lastHeard = said;
          onChoiceHeard?.call(said, [
            for (final o in _choiceOptions)
              if (ask.values.contains((o['value'] ?? '').toString()))
                (o['label'] ?? o['value']).toString()
          ]);
          _missStreak = 0;
          _submitChoice(ask.values);
          break;
        }
        if (ask.near.isNotEmpty && !confirming) {
          // Not sure enough to act, sure enough to ask -- the same two
          // bars the hub applies to its own lists.
          _narrowed = [
            for (var i = 0; i < _choiceOptions.length; i++)
              if (ask.near.any((o) =>
                  (o['value'] ?? '').toString() ==
                  (_choiceOptions[i]['value'] ?? '').toString()))
                i
          ];
          _missStreak++;
          final names = [
            for (final i in _narrowed) _speakable(_choiceLabels()[i])
          ];
          onChoiceHeard?.call(said, ['did you mean ${names.join(', ')}?']);
          final nearEpoch = _epoch;
          state = FlowState.speaking;
          await voice.say(names.length == 1
              ? 'I heard "$said". Did you mean ${names.first}? '
                  'Say yes, or say no.'
              : 'I heard "$said". Did you mean ${_spokenList(names)}? '
                  'Say one of them, or say none.');
          if (nearEpoch != _epoch) break;
          _awaitingAnswer = true;
          _openWindow(_CaptureRole.choiceReply);
          break;
        }
        // Nothing, or the narrowed round failed too: read the list again.
        _narrowed = const [];
        onChoiceHeard?.call(said, const []);
        _missStreak++;
        final missEpoch = _epoch;
        state = FlowState.speaking;
        var missStopped = false;
        if (_missStreak <= 1) {
          missStopped =
              await _readOptions("I didn't catch a name. The choices are:");
        } else {
          await voice.say('Say a name or its number, repeat to hear them '
              'again, or cancel to stop.');
        }
        if (missEpoch != _epoch || missStopped) break;
        _awaitingAnswer = true;
        _openWindow(_CaptureRole.choiceReply);
        break;
    }
  }

  /// Hand a spoken pick to the SCREEN that owns the list, and close the
  /// voice path for it. Only a screen's own list gets here: a server list
  /// is answered with its words (onChoiceSpoken), never with values the
  /// phone picked.
  void _submitChoice(List<String> values) {
    if (values.isEmpty) return;
    _narrowed = const [];
    _awaitingAnswer = false;
    _closeWindow(abort: true);
    _choiceOptions = const [];

    // A LIST THE SCREEN OWNS ends here, not at the server. Nothing is sent
    // and nothing is waited for, so the mic goes straight back to idle --
    // `thinking` would sit on a reply that is never coming and hold the
    // mic until the watchdog gave up on it. The screen re-arms with its
    // new vocabulary when it is ready.
    final pick = _localPick;
    if (pick != null) {
      _localPick = null;
      _localCancel = null;
      final heard = _lastHeard;
      _localRepeatLine = '';
      pick(values.first, heard);
      // A SCREEN MAY RE-ARM INSIDE ITS OWN onPick -- the pin map does it
      // post-frame, a form does it at once -- and armLocalChoice opens
      // its window synchronously when there is nothing to speak first.
      // Resetting the state here would then pull it out from under a
      // collector that is already running (found while wiring the
      // voice-fillable form, 2026-09-11). Only a flow still sitting where
      // this left it is put back to idle.
      if (state != FlowState.speaking && state != FlowState.capturing) {
        state = FlowState.idleListening;
        onStatus(_listeningStatus);
      }
      return;
    }
    // The screen let go of its list in the meantime: nothing to hand to.
    state = FlowState.idleListening;
    onStatus(_listeningStatus);
  }

  /// The A/B harness: when debugDir is set, save what the server verifies
  /// (raw) next to what Moonshine heard (clean), timestamp-paired. Listening
  /// to the pairs -- and diffing their transcripts -- is how cleaning-chain
  /// arguments get settled by evidence instead of theory.
  ///
  /// COMMAND CAPTURES ONLY. A challenge clip is the voiceprint the hub
  /// verifies against, an enrolment phrase is the voiceprint itself, and a
  /// prompt answer may be a spoken PIN -- none of those belong in a WAV on
  /// external storage, whatever the build. Bounded to the newest
  /// [_debugPairsKept] pairs so the directory cannot grow forever.
  static const int _debugPairsKept = 20;

  Future<void> _saveDebugPair(
      Uint8List rawPcm, Uint8List cleanPcm, _CaptureRole role) async {
    if (debugDir == null || rawPcm.isEmpty) return;
    if (role != _CaptureRole.command) return;
    try {
      final dir = Directory(debugDir!)..createSync(recursive: true);
      final stamp = DateTime.now().millisecondsSinceEpoch;
      // Prune BEFORE writing: oldest raw_ files (and their clean_ twins)
      // beyond the cap go, newest stay.
      final raws = dir
          .listSync()
          .whereType<File>()
          .where((f) => f.path.split('/').last.startsWith('raw_'))
          .toList()
        ..sort((a, b) => a.path.compareTo(b.path));
      while (raws.length >= _debugPairsKept) {
        final old = raws.removeAt(0);
        final twin = File(old.path.replaceFirst('/raw_', '/clean_'));
        try {
          old.deleteSync();
          if (twin.existsSync()) twin.deleteSync();
        } catch (_) {}
      }
      await File('${dir.path}/raw_$stamp.wav')
          .writeAsBytes(base64Decode(encodeWavB64(rawPcm).b64), flush: true);
      if (!identical(cleanPcm, rawPcm)) {
        await File('${dir.path}/clean_$stamp.wav').writeAsBytes(
            base64Decode(encodeWavB64(cleanPcm).b64),
            flush: true);
      }
      debugPrint('[debug] saved capture pair $stamp');
    } catch (e) {
      debugPrint('[debug] capture save failed: $e'); // never break the flow
    }
  }

  List<String> _choiceLabels() => [
        for (final o in _choiceOptions)
          (o['label'] ?? o['value'] ?? '').toString()
      ];

  /// A label as a MOUTH says it. "bulb_2" was read as "bulb underscore
  /// two" (her report, 2026-09-11): the label is the device's stored name,
  /// which keeps whatever was typed on the pin map, and a speech engine
  /// reads punctuation. Underscores, dots and dashes become spaces for the
  /// spoken form only; the screen still shows the name as written, and the
  /// matcher already folds punctuation to spaces on both sides (_norm), so
  /// "bulb two" lands on "bulb_2" exactly as before.
  static String _speakable(String label) => label
      .replaceAll(RegExp(r'[_.\-]+'), ' ')
      .replaceAll(RegExp(r'\s+'), ' ')
      .trim();

  /// The options as SPOKEN, numbered so "number two" is always available
  /// even when a name transcribes badly -- ALL of them, in chunks (see
  /// _readAloud), opened by [lead] and closed by the controls tail.
  List<String> _optionChunks(String lead) {
    final labels = _choiceLabels();
    final items = [
      for (var i = 0; i < labels.length; i++)
        '${i + 1}, ${_speakable(labels[i])}'
    ];
    final chunks = _chunksOf(items);
    if (chunks.isEmpty) chunks.add('');
    chunks[0] = lead.isEmpty ? chunks[0] : '$lead ${chunks[0]}';
    chunks[chunks.length - 1] = '${chunks.last}.$_choiceTail';
    return chunks;
  }

  /// Read the options and listen between chunks. True = the person began
  /// answering mid-list, so the caller must not open its own window.
  Future<bool> _readOptions(String lead) {
    _awaitingAnswer = true; // a gap's window is an answer to THIS question
    return _readAloud(_optionChunks(lead), gapRole: _CaptureRole.choiceReply);
  }

  /// Consecutive unmatched utterances in the current selection, so the
  /// full list is re-read once and then nagging stops.
  int _missStreak = 0;

  /// How to answer, in one place — the announcement, a re-read and a miss
  /// retry must not describe the controls three different ways.
  String get _choiceTail {
    final base = _choiceMulti
        ? (_choiceAllowAll && _choiceOptions.length > 1
            ? ' Say the ones you want, or say all of them.'
            : ' Say the ones you want.')
        : ' Say the name, or its number.';
    return '$base Say repeat to hear them again, or cancel to stop.';
  }

  /// Spoken CONTROLS, matched on the WHOLE utterance only. Anchoring both
  /// ends is what keeps them from stealing selections: bare "cancel" stops
  /// the question, while "the cancel switch" is still just a device name.
  static final RegExp _repeatPhrase = RegExp(
      r'^(repeat|repeat that|again|say (that|them|it) again|'
      r'read (them|it|the options|the list) again|'
      r'what are (my|the) (options|choices)|the options|options)$');
  static final RegExp _cancelPhrase = RegExp(
      r'^(cancel|stop|quit|exit|never ?mind|forget it|go back|none|'
      r'none of (them|these)|cancel that|cancel it|cancel this|stop it|'
      r'stop that|stop this|no stop|forget that|scratch that|abort)$');

  /// QUIETEN THE PHONE, BY VOICE (her ask, 2026-09-11).
  ///
  /// Matched on the WHOLE utterance, exactly like repeat and cancel, and
  /// for the same reason: anchoring both ends is what stops a control word
  /// stealing a selection. "Be quiet" silences the phone; "the quiet lamp"
  /// is still a device.
  ///
  /// NOT a cancel. The question stays on screen, the list stays listed,
  /// the window re-opens -- this turns off the SPEAKING, which is the one
  /// thing you could previously only reach from the home screen. It sets
  /// the same persistent flag the home screen's speaker button sets, so it
  /// survives a restart until it is spoken (or tapped) off again.
  static final RegExp _hushPhrase = RegExp(
      r'^(be quiet|quiet|shut up|stop talking|stop speaking|'
      r'stop reading|silence|mute|mute yourself|mute the voice|'
      r'no more talking|dont speak|do not speak)$');
  static final RegExp _unhushPhrase = RegExp(
      r'^(speak up|unmute|unmute yourself|talk to me|you can talk|'
      r'start talking|speak again|say it again out loud|voice on)$');

  /// Given by AppState: turn the persistent speaking mute on or off, from
  /// a spoken control. A constructor parameter like onChoiceHeard beside
  /// it, not an assigned field -- it is wanted from the first frame.
  final void Function(bool muted)? onMuteSpoken;

  /// Padding around a cancel that carries no intent of its own.
  static const _cancelFillers = {
    'please', 'just', 'ok', 'okay', 'um', 'uh', 'er', 'ah', 'yeah', 'yep',
    'well', 'so', 'now', 'it', 'this', 'that', 'all', 'right', 'then',
    'lets', 'let', 'i', 'wanna', 'want', 'to', 'the',
  };

  /// A SPOKEN CANCEL on a list, whichever layer heard it: the same path
  /// as the on-screen Cancel -- the question is closed, the screen pops,
  /// the hub is told.
  void _cancelBySpeech(String said) {
    onChoiceHeard?.call(said, const ['cancelling']);
    _narrowed = const [];
    _awaitingAnswer = false;
    _closeWindow(abort: true);
    _choiceOptions = const [];
    // A local list has no server question to close: the screen's own
    // Cancel is the whole ending, and going to `thinking` here would
    // wait on a frame nobody is sending.
    final localCancel = _localCancel;
    if (_localPick != null) {
      _localPick = null;
      _localCancel = null;
      localCancel?.call();
      state = FlowState.idleListening;
      onStatus(_listeningStatus);
      return;
    }
    state = FlowState.thinking;
    onStatus('cancelling…');
    _armThinkWatchdog(watchCancel: false);
    onChoiceCancel?.call();
  }

  /// Does this utterance mean "stop, I'm out"?
  ///
  /// THE ANCHORED REGEX ALONE WAS NOT ENOUGH (field test 2026-08-29:
  /// "cancel didnt catch"). _norm already strips punctuation, so "cancel."
  /// was fine — but ^(cancel|...)\$ demands the WHOLE utterance be the word,
  /// so every natural way of saying it missed:
  ///
  ///     "cancel please"  ·  "just cancel"  ·  "okay cancel"  ·  "um cancel"
  ///
  /// The hub carries the twin of this in hv_common.means_cancel, which now
  /// guards its forty prompt sites for exactly the same reason. Both had the
  /// same root cause: exact matching against transcribed speech.
  ///
  /// NEGATION IS CHECKED FIRST, and that is why this is a function rather
  /// than a looser regex. "don't cancel" and "not that one" both contain a
  /// cancel word and mean its opposite; stripping fillers without this guard
  /// would turn a refusal to cancel INTO a cancel — worse than the bug being
  /// fixed, because it throws away work the person just said they wanted to
  /// keep. Same reasoning as the hub's "'don't undo that' must NOT undo".
  static bool _meansCancel(String said) {
    final low = _norm(said);
    if (low.isEmpty) return false;
    if (_cancelPhrase.hasMatch(low)) return true;

    final words = low.split(' ');
    // "do not" always leaves a bare "not" after normalisation, so one set
    // covers every spelling of the negation.
    final negated = words.contains('dont') ||
        words.contains('not') ||
        words.contains('never');
    // "never mind" is itself a cancel and must survive the 'never' guard.
    if (negated &&
        !low.startsWith('never mind') &&
        !low.startsWith('nevermind')) {
      return false;
    }

    final core = words.where((w) => !_cancelFillers.contains(w)).join(' ');
    return core.isNotEmpty && _cancelPhrase.hasMatch(core);
  }

  // ══ THE MATCHING LIVES ON THE HUB ══════════════════════════════════
  //
  // 341 lines of scoring stood here: bigrams, token overlap, a
  // coverage rule, edit distance, three commit thresholds and an ordinal
  // reader. All of it is gone, and none of it is lost -- it is
  // nlu/choice_match.py now, where it can be run ("route every screen
  // choices to the hub because i dont know maybe the code u made for dart
  // is just useless", 2026-09-11).
  //
  // What stays here is what belongs to the PHONE: normalising an
  // utterance for the spoken CONTROLS (cancel, repeat, be quiet, yes,
  // none of those), which are about this handset and never travel. Every
  // question about WHICH OPTION WAS NAMED -- a server list or a screen's
  // own -- goes up as words and comes back as an answer. See
  // askHubToMatch.


  static String _norm(String s) => _synonyms(_spokenNumbers(s
      .toLowerCase()
      .replaceAll(RegExp(r'[^a-z0-9\s]'), ' ')
      .replaceAll(RegExp(r'\s+'), ' ')
      .trim()));

  /// Words this house uses for the same thing, folded to one of them.
  ///
  /// "gpio" is a datasheet word; nobody standing at a board says it (her
  /// call, 2026-08-31). It is safe to fold because this product has no
  /// OTHER pin numbering -- the catalogue, board_wiring and the pin screen
  /// all count in GPIO, and no header numbering exists anywhere for "pin
  /// 14" to mean instead. Applied to labels and utterances alike, so it
  /// cannot make the two sides disagree.
  static const Map<String, String> _sameWord = {'gpio': 'pin'};

  static final RegExp _wordThenDigits = RegExp(r'^([a-z]+)(\d+)$');

  static String _synonyms(String s) {
    if (s.isEmpty) return s;
    return s.split(' ').map((w) {
      final direct = _sameWord[w];
      if (direct != null) return direct;
      // "gpio14" with no gap -- how it is WRITTEN, never how it is said.
      // Split only when the letters are a known synonym, so no device
      // name is ever cut in half, and the ordinal regexes never see it.
      final m = _wordThenDigits.firstMatch(w);
      final head = m == null ? null : _sameWord[m.group(1)!];
      return head == null ? w : '$head ${m!.group(2)}';
    }).join(' ');
  }

  static const Map<String, int> _numWord = {
    'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
    'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
    'nineteen': 19, 'twenty': 20, 'thirty': 30, 'forty': 40,
    'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90,
  };

  /// SPEECH GIVES WORDS, LISTS ARE WRITTEN IN DIGITS. Moonshine returns
  /// "gpio fourteen"; the option reads "GPIO 14". Nothing bridged the two,
  /// so the phrase was never found, the ordinal reader only knew number
  /// words up to ten, and the fuzzy pass had every pin sharing the word
  /// "gpio" -- scores clustered, the solo margin refused to commit, and
  /// the pin screen asked "I didn't catch a name" for ever. It never
  /// picked the WRONG pin; it just never picked one.
  ///
  /// Both sides of every comparison run through _norm, so a label written
  /// in digits and an utterance heard as words become the same string.
  /// "twenty seven" and "twenty-seven" both fold to 27.
  ///
  /// Deliberately NOT joining "gpio 14" into "gpio14": that would break
  /// the `(number|option) 2` rule and quietly kill "number two"
  /// on every choice screen in the app.
  static String _spokenNumbers(String s) {
    if (s.isEmpty) return s;
    final words = s.split(' ');
    final out = <String>[];
    for (var i = 0; i < words.length; i++) {
      final value = _numWord[words[i]];
      if (value != null && value >= 20 && i + 1 < words.length) {
        final unit = _numWord[words[i + 1]];
        if (unit != null && unit >= 1 && unit <= 9) {
          out.add('${value + unit}');
          i++;
          continue;
        }
      }
      out.add(value != null ? '$value' : words[i]);
    }
    return out.join(' ');
  }

  // `_fillers` and `_tokens` stood here -- the filler list and the token
  // splitter the scorer compared with. They went with the matcher: the
  // hub's nlu/choice_match has its own, and nothing on this side compares
  // tokens any more. The spoken CONTROLS use _norm and the whole string,
  // never a token list. (`_cancelFillers`, further up, is a different and
  // still-live thing: the words allowed to surround a bare "cancel".)

  /// The options a "did you mean…" is currently offering. While this is
  /// set, the next utterance is matched against THESE first -- three
  /// candidates instead of nine, which is why a second attempt lands.
  List<int> _narrowed = const [];

  // Kept character-for-character in step with the hub's _YES / _NO
  // (app/shell.py), and read the same way: through _plainAnswer, as the
  // hub reads them through _plain_answer. Yes and no name nothing on any
  // list, so no matcher can see them and each side answers them where it
  // stands -- which only works while the two lists say the same thing.
  // ("do it" was in one and not the other.)
  static final RegExp _yesPhrase = RegExp(
      r'^(yes|yeah|yep|yup|correct|right|that one|thats right|'
      r'that is right|ok|okay|sure|do|do it|please do)$');
  static final RegExp _nonePhrase = RegExp(
      r'^(no|nope|none|neither|none of (them|these|those)|not those|'
      r'not that|wrong|read them again|something else)$');

  /// Padding around a yes or a no: "yes please", "um no", "no thanks".
  /// The same set as the hub's _ANSWER_PADDING.
  static const _answerPadding = {
    'please', 'thanks', 'thank', 'you', 'um', 'uh', 'er', 'erm', 'ah',
    'oh', 'well', 'so', 'just',
  };

  /// A spoken answer as _yesPhrase / _nonePhrase read it: lower case, the
  /// apostrophe closed up ("that's right" -> "thats right"), other
  /// punctuation gone, padding dropped. NOT _norm: that turns "that one"
  /// into "that 1", which no phrase here can match.
  static String _plainAnswer(String said) => said
      .toLowerCase()
      .replaceAll(RegExp(r"['\u2019]"), '')
      .replaceAll(RegExp(r'[^a-z0-9\s]'), ' ')
      .split(RegExp(r'\s+'))
      .where((w) => w.isNotEmpty && !_answerPadding.contains(w))
      .join(' ');

  /// The hub's "show me the whole list again" row on a narrowed question
  /// (WebSocketIO._CHOICE_NONE -- the leading space is part of it).
  static const String _hubNoneRow = ' __none__';

  /// "None" / "neither" / "none of those" ANSWER a list that has a way to
  /// say so -- the hub's narrowed round carries a "None of those" row, the
  /// port picker its own "None of these", and a screen's "did you mean…"
  /// reads its whole list again -- so there they are not a cancel. It was
  /// the cancel phrase that caught them first, and ended the whole flow
  /// when the person had only turned the suggestion down.
  bool _noneIsAnAnswer(String said) {
    if (!_nonePhrase.hasMatch(_plainAnswer(said))) return false;
    if (_narrowed.isNotEmpty) return true;
    return _choiceOptions.any((o) =>
        (o['value'] ?? '').toString() == _hubNoneRow ||
        _norm((o['label'] ?? '').toString()).startsWith('none'));
  }

  // ══ ASKING THE HUB TO MATCH ════════════════════════════════════════
  //
  // "route every screen choices to the hub because i dont know maybe the
  // code u made for dart is just useless" (2026-09-11). A server `choice`
  // already travels up as words; this is the same service for the lists no
  // server question stands behind -- the pin map's pins and parts, a
  // form's fields, the rule builder's blocks. After this the phone owns NO
  // matching: it hears, it sends, it draws.
  //
  // One frame out (`match_request`), one frame back (`match_result`),
  // correlated by id and bounded by a timeout, because a screen waiting
  // for ever on a hub that did not answer is worse than one that decides
  // nothing matched.

  int _matchSeq = 0;

  /// How long a screen waits for the hub to match. Generous for a
  /// round trip over the house's own network, short enough that a silent
  /// hub does not leave a list unanswerable.
  static const Duration _matchTimeout = Duration(seconds: 6);

  /// Ask the hub which of `options` was said. Returns the chosen values
  /// and, when nothing was certain, the two or three worth offering back.
  /// `cancelCheck`: when nothing on the list was named, the hub also says
  /// whether the words meant STOP (`cancel`), judged by meaning -- see
  /// nlu/spoken_cancel.py. With it, an empty list is a valid question:
  /// that is the working screen asking "was that a cancel?".
  Future<({List<String> values, List<Map<String, dynamic>> near, bool cancel})>
      askHubToMatch(
    String heard,
    List<Map<String, dynamic>> options, {
    bool multi = false,
    bool allowAll = false,
    bool confirming = false,
    bool cancelCheck = false,
  }) async {
    const empty =
        (values: <String>[], near: <Map<String, dynamic>>[], cancel: false);
    if (heard.trim().isEmpty || (options.isEmpty && !cancelCheck)) {
      return empty;
    }
    final id = ++_matchSeq;
    final done = Completer<
        ({List<String> values, List<Map<String, dynamic>> near, bool cancel})>();
    late final StreamSubscription<ServerFrame> sub;
    sub = socket.frames.listen((f) {
      if (f.type != 'match_result' || f.raw['id'] != id) return;
      if (done.isCompleted) return;
      done.complete((
        values: [
          for (final v in (f.raw['values'] as List? ?? const []))
            v.toString()
        ],
        near: [
          for (final o in (f.raw['near'] as List? ?? const []))
            if (o is Map<String, dynamic>) o
        ],
        cancel: f.raw['cancel'] == true,
      ));
    });
    try {
      if (!socket.send({
        'type': 'match_request',
        'id': id,
        'heard': heard,
        'multi': multi,
        'allow_all': allowAll,
        'confirming': confirming,
        if (cancelCheck) 'cancel_check': true,
        'options': [
          for (final o in options)
            {
              'value': (o['value'] ?? '').toString(),
              'label': (o['label'] ?? o['value'] ?? '').toString(),
            }
        ],
      })) {
        return empty;
      }
      return await done.future.timeout(_matchTimeout, onTimeout: () {
        onStatus('the hub did not answer — tap one instead');
        return empty;
      });
    } finally {
      await sub.cancel();
    }
  }

  /// Said once, on the first auth_ok of this run.
  bool _greeted = false;

  // ── A choice the SCREEN owns ────────────────────────────────────────
  //
  // The pin map answers itself. It is not a server `choice` frame -- the
  // hub sends one pin_map frame and waits for the finished layout -- so
  // every list on it (the pins, the device sheet, Add, Save) was outside
  // the voice path entirely: the dock rendered there and heard nothing.
  //
  // Rather than a second recogniser and a second matcher, a local choice
  // borrows THIS one whole: same window, same three-pass match, same
  // repeat and cancel controls, same "silence is the terminator" rule.
  // One thing differs -- where the answer goes. A server choice sends a
  // frame and waits in `thinking`; a local one calls back and returns to
  // idle, because no reply is coming and `thinking` would strand the mic
  // until the watchdog fired.
  void Function(String value, String heard)? _localPick;
  VoidCallback? _localCancel;

  /// The screen's own reader, asked before the hub (see armLocalChoice):
  /// an action to run when it understood the words, null to let the hub
  /// match them against the list.
  VoidCallback? Function(String heard)? _localResolve;

  /// What "say it again" should repeat while a local list is armed.
  ///
  /// A CONFIRM ARM HAS TWO OPTIONS, "yes" and "no", and reading those back
  /// answers nothing -- what you want repeated is the CANDIDATE. The
  /// screen supplies that line; empty falls back to reading the list, which
  /// is right for every other local arm.
  String _localRepeatLine = '';

  /// The option values the flow is currently armed with, so re-arming with
  /// an unchanged list is free. See armLocalChoice.
  String _localFingerprint = '';

  bool get hasLocalChoice => _localPick != null;

  /// Hand the flow a list the screen is already drawing.
  ///
  /// `options` are the same maps a server choice sends -- {value, label,
  /// sub?} -- so nothing downstream can tell the two apart. Re-arming
  /// REPLACES the list, which is the point: a pin screen's vocabulary
  /// shrinks with every placement and Save only becomes sayable once
  /// nothing is half done.
  void armLocalChoice({
    required String prompt,
    required List<Map<String, dynamic>> options,
    required void Function(String value, String heard) onPick,
    VoidCallback? onCancel,
    String repeatLine = '',
    bool speakPrompt = false,
    VoidCallback? Function(String heard)? resolve,
  }) {
    if (options.isEmpty) return;

    // SAME LIST, ALREADY ARMED: refresh the closures and stop.
    //
    // THIS IS THE CRASH (her report, 2026-08-31: "voice dock crashed when
    // I tried to say pin"). The pin screen re-arms from a post-frame
    // callback on EVERY build -- which is exactly what makes the
    // vocabulary follow setState -- and without this guard every one of
    // those builds opened a FRESH capture window on top of the last. A
    // pick calls setState, which builds, which re-arms, which opens
    // another window; the audio pipeline churned and took the app with
    // it. Re-arming must be idempotent, because it is called far more
    // often than the list actually changes.
    final fingerprint = [for (final o in options) o['value']].join(' ');
    if (_localPick != null && fingerprint == _localFingerprint) {
      _localPick = onPick; // the closure may close over newer state
      _localCancel = onCancel;
      _localResolve = resolve;
      _localRepeatLine = repeatLine;
      _choiceOptions = options;
      return;
    }

    _localFingerprint = fingerprint;
    _localPick = onPick;
    _localCancel = onCancel;
    _localResolve = resolve;
    _localRepeatLine = repeatLine;
    _choiceOptions = options;
    _choiceMulti = false;
    _choiceAllowAll = false;
    _missStreak = 0;
    if (state != FlowState.idleListening && state != FlowState.speaking) {
      return; // mid-capture or mid-command: the list is stored, not armed
    }
    if (_awaitingAnswer) return; // a window is already outstanding
    () async {
      final epoch = _epoch;
      if (speakPrompt && prompt.isNotEmpty) {
        state = FlowState.speaking;
        await voice.say(prompt);
        if (epoch != _epoch || _localPick == null) return;
      }
      _awaitingAnswer = true;
      _openWindow(_CaptureRole.choiceReply);
    }();
  }

  /// Say one line and go back to whatever was happening. For a screen that
  /// wants to confirm what it just did -- "GPIO14, climate" -- without
  /// owning a question. Never interrupts: a window that is open, a command
  /// in flight or TTS mid-sentence all keep the floor.
  Future<void> speakAside(String text) async {
    if (text.trim().isEmpty) return;
    if (state != FlowState.idleListening) return;
    final epoch = _epoch;
    final rearm = _localPick != null;
    state = FlowState.speaking;
    await voice.say(text);
    if (epoch != _epoch) return;
    // SOMETHING MAY HAVE ARMED WHILE THIS WAS SPEAKING. Same hazard as
    // _submitChoice above: a window opened during the sentence must not
    // be closed by the sentence ending.
    if (state != FlowState.speaking) return;
    state = FlowState.idleListening;
    // A screen that had the mic keeps it: the line it just spoke usually
    // ends with the next word to say.
    if (rearm && _localPick != null && _choiceOptions.isNotEmpty) {
      _awaitingAnswer = true;
      _openWindow(_CaptureRole.choiceReply);
    } else {
      onStatus(_listeningStatus);
    }
  }

  /// The last transcript that reached the matcher, handed back with the
  /// pick so a screen can re-run it. See AppState.matchHeard.
  String _lastHeard = '';

  /// Where a dictated value goes. Set by armLocalDictation, cleared the
  /// moment the words are handed over -- one window, one value.
  void Function(String text)? _localTextSink;

  /// DICTATE ONE VALUE INTO A SCREEN'S FIELD.
  ///
  /// The other half of armLocalChoice, and the piece that makes a form
  /// fillable by voice: that one MATCHES what was said against a list,
  /// this one does not match at all -- it transcribes and hands the words
  /// over. "Kitchen" is not on any list; it is the answer.
  ///
  /// Opened only by a deliberate act (a field named out loud, or a tap),
  /// never on its own. A form carries text fields and a mic that opens
  /// itself behind one is what submitted a PIN eight seconds in; the rule
  /// that came out of that is not "never dictate", it is "never without
  /// being asked".
  ///
  /// The spoken controls still work: "cancel" drops out to the screen's
  /// own cancel, "be quiet" mutes without losing the field, and an empty
  /// or unheard capture re-opens rather than writing "" into the box.
  void armLocalDictation({
    required String prompt,
    required void Function(String text) onText,
    VoidCallback? onCancel,
    bool speakPrompt = true,
  }) {
    // A dictation REPLACES any armed list: the screen has moved from
    // "which field?" to "what goes in it?", and two open windows would
    // race for the same utterance.
    _localPick = null;
    _localFingerprint = '';
    _choiceOptions = const [];
    _localTextSink = onText;
    _localCancel = onCancel;
    _localRepeatLine = prompt;
    if (state != FlowState.idleListening && state != FlowState.speaking) {
      return; // mid-capture or mid-command: stored, not armed
    }
    () async {
      final epoch = _epoch;
      if (speakPrompt && prompt.isNotEmpty) {
        state = FlowState.speaking;
        await voice.say(prompt);
        if (epoch != _epoch || _localTextSink == null) return;
      }
      _awaitingAnswer = true;
      _openWindow(_CaptureRole.localText);
    }();
  }

  /// The screen is done with the mic (popped, or a tap answered it).
  void disarmLocalChoice() {
    if (_localPick == null && _localTextSink == null) return;
    _localPick = null;
    _localTextSink = null;
    _localCancel = null;
    _localResolve = null;
    _narrowed = const [];
    _localRepeatLine = '';
    _localFingerprint = '';
    _choiceOptions = const [];
    _awaitingAnswer = false;
    _closeWindow(abort: true);
    if (state != FlowState.idleListening) {
      state = FlowState.idleListening;
      onStatus(_listeningStatus);
    }
  }

  /// "Good morning, Augusta." The hour is THIS phone's, which is where the
  /// person actually is.
  /// "Hello, Bruce Wayne. Good evening and welcome." -- the sign-in
  /// greeting, spoken here and shown by AppState. Empty for no name.
  static String welcomeLine(String who) {
    if (who.isEmpty || who.length > 60) return '';
    final h = DateTime.now().hour;
    final part = h >= 5 && h < 12
        ? 'morning'
        : h >= 12 && h < 17
            ? 'afternoon'
            : 'evening';
    return 'Hello, $who. Good $part and welcome.';
  }

  static String _greeting(String who) {
    final h = DateTime.now().hour;
    final part = h < 12
        ? 'Good morning'
        : h < 17
            ? 'Good afternoon'
            : 'Good evening';
    if (who.isEmpty) return '$part.';
    return '$part, ${who[0].toUpperCase()}${who.substring(1)}.';
  }

  Future<void> _speakThenIdle(String text) async {
    state = FlowState.speaking;
    await voice.say(text);
    state = FlowState.idleListening;
    onStatus(_listeningStatus);
  }

  // ── Server loop ────────────────────────────────────────────────────────

  Future<void> _onFrame(ServerFrame f) async {
    switch (f.type) {
      case 'auth_ok':
        _boundUser = (f.raw['user_id'] as String? ?? '').trim();
        // Only announce when it interrupts nothing.
        if (state == FlowState.idleListening) {
          onStatus('connected');
          // THE GREETING. auth_ok is the moment the house knows who is
          // holding this phone -- it carries user_id -- so it is the one
          // place a name can be used without asking anyone for it.
          //
          // ONCE PER RUN. auth_ok arrives again on every reconnect, and a
          // house that says good morning each time the Wi-Fi hiccups is a
          // house nobody leaves the volume up for.
          // A NAMELESS "Good evening." is not said: an unbound phone is about
          // to sign in, and the `welcome` frame below greets it by name.
          final who = (f.raw['user_id'] as String? ?? '').trim();
          if (!_greeted && who.isNotEmpty) {
            _greeted = true;
            await _speakThenIdle(_greeting(who));
          }
        }
        break; // FIX: was falling through

      case 'welcome':
        // The hub has just signed this person in (or created them):
        // "Hello, Bruce Wayne. Good evening and welcome." Said AT ONCE and not
        // awaited: frames are handled one after another, so awaiting the
        // speech held back every frame behind it (the result, the screen) for
        // as long as the sentence took -- and waiting for an idle flow
        // before that made it late or never.
        _greeted = true;
        final line = welcomeLine((f.raw['name'] as String? ?? '').trim());
        if (line.isNotEmpty) unawaited(voice.say(line));
        break;

      case 'auth_failed':
        // A FULL HUB IS NOT A REJECTION: the key was accepted and
        // SocketClient's ladder dials again, so saying "rejected" (once per
        // rung) told the person something false.
        if (f.reason == 'too_many_connections') {
          onStatus('the hub is full — trying again shortly');
          break;
        }
        await _speakThenIdle('The house rejected this phone: ${f.describe()}.');
        break; // FIX: was falling through

      case 'challenge_required':
        // The liveness gate: server picked words, we must SAY them. Speak
        // the phrase, then capture -- awaited TTS guarantees the phrase
        // itself is not in the recording.
        state = FlowState.speaking;
        _awaitingAnswer = true;
        final chEpoch = _epoch;
        // "Or say cancel" (her log, 2026-10-10: nothing said cancel could be
        // said here, so it did not look like it was listened for).
        await voice.say('Voice check. Please repeat: ${f.phrase ?? ''}. '
            'Or say cancel.');
        if (chEpoch != _epoch) break; // cancelled while the question was read
        // LISTENS ON ITS OWN (2026-10-01). The phrase has been read, so the
        // window opens by itself: no tap needed, and "cancel" said instead
        // of the words ends the whole check (see _cancelSaidInstead). If
        // nobody speaks the window lapses and the question stays open --
        // "tap the mic and say it" -- exactly as a tap-opened one did.
        _askedPhrase = f.phrase ?? '';
        state = FlowState.idleListening;
        _openWindow(_CaptureRole.challenge);
        break; // FIX: was falling through

      case 'record_required':
        final idx = f.raw['index'], total = f.raw['total'];
        final position = (idx is int && total is int && total > 0)
            ? ' Phrase $idx of $total.'
            : '';
        state = FlowState.speaking;
        _awaitingAnswer = true;
        final recEpoch = _epoch;
        await voice.say('${f.raw['context'] == 'enrollment' ? 'Enrollment.' : ''}'
            '$position Please say: ${f.phrase ?? ''}.');
        if (recEpoch != _epoch) break; // cancelled mid-read
        // LISTENS ON ITS OWN (2026-10-01), like the voice check. It was
        // tap-only after the auto window and the tap once fought over the
        // same gesture: the window lapsed, the flow went idle, and the tap
        // that followed opened a COMMAND capture. That cannot happen now: a
        // lapsed window leaves the question open with its role remembered
        // (_awaitingAnswer), so the tap that follows records the phrase.
        _askedPhrase = f.phrase ?? '';
        state = FlowState.idleListening;
        _openWindow(_CaptureRole.enrollment);
        break; // FIX: was falling through

      case 'prompt':
        if (isAutoAnswered?.call(f.text ?? '') == true) {
          break; // answered silently by the app's form script
        }
        state = FlowState.speaking;
        _awaitingAnswer = true;
        final prEpoch = _epoch;
        // Speak the QUESTION, not the typed-answer ritual — "[Y = yes /
        // n = no ...]" and "(Enter=keep, 'prev' ...)" are buttons on
        // screen now, and reading them aloud is noise.
        final spokenPrompt = _stripRitual(f.text ?? '');
        await voice.say(
            spokenPrompt.isEmpty ? 'The house has a question.' : spokenPrompt);
        if (prEpoch != _epoch) break; // cancelled mid-read
        // FIX (auto-send while typing): NO auto-opened mic window here. A
        // hot mic behind a text field meant any endpointed noise — or the
        // window timeout — transcribed junk/nothing and SUBMITTED it as the
        // answer (the PIN, notably). The question stays live; answering by
        // voice is a deliberate dock-mic tap (reopenFor 'prompt').
        state = FlowState.idleListening;
        onStatus('answer on screen, or tap the mic to speak');
        break;

      case 'choice':
        if (isChoiceAnsweredSilently?.call(f.raw) == true) break;
        _narrowed = const [];   // a new list; no "did you mean" is standing
        // A SERVER QUESTION OUTRANKS A SCREEN'S OWN LIST. One left armed
        // (a screen gone without disarming) would otherwise take the
        // spoken answer: the local branch below calls its pick, and the
        // hub waits for a choice_response that never comes.
        _localPick = null;
        _localTextSink = null;
        _localCancel = null;
        _localFingerprint = '';
        _localRepeatLine = '';
        _choiceOptions = [
          for (final o in (f.raw['options'] as List? ?? const []))
            if (o is Map<String, dynamic>) o
        ];
        _choiceMulti = f.raw['multi'] == true;
        _choiceAllowAll = f.raw['allow_all'] == true;
        state = FlowState.speaking;
        _missStreak = 0;
        // READ THE OPTIONS, numbered. The question text alone tells the
        // person nothing they can answer -- and it used to be worse than
        // that: until 11 Sep the hub asked a device picker with a bare
        // "Pick" and wrote its real question ("Which one(s)?") as an
        // output line, which this phone then collected and recited as the
        // RESULT. The prompt carries the question now, so `f.text` is
        // worth saying; the options are still what makes it answerable.
        // No "say done when finished" — silence ends the utterance and the
        // whole selection is sent at once. The tail also names the two
        // spoken controls, so they are discoverable without a manual.
        final choEpoch = _epoch;
        final choStopped = await _readOptions('${f.text ?? 'Pick one'}.');
        if (choEpoch != _epoch || choStopped) break; // cancelled / answered mid-read
        _awaitingAnswer = true;
        // THE MENU-FLOW EXCLUSION IS GONE (her call, 2026-08-29: "direct
        // the voice dock to just use the main moonshine and then the fuzzy
        // matcher for options selection even for adding device anything").
        //
        // It used to return early here on isMenuFlow, reasoning that a menu
        // flow means "on screen, hands on the phone". That was true of how a
        // menu flow STARTS and false of everything it then asks: adding a
        // device asks which room, which board, which pin, which entity --
        // list after list, every one of them a choice -- and the mic was
        // dead for all of them. The house could be driven by voice right up
        // until you tried to build anything with it.
        //
        // Nothing about a choice is less safe inside a menu flow. The three
        // guards that made auto-listening safe here belong to the CHOICE,
        // not to how it was reached: nothing is committed on a weak score
        // (the hub's matcher asks again instead), an empty transcript
        // never answers, and a silent window closes quietly instead of
        // submitting.
        //
        // The transcript is the hub's own Moonshine and the matching is
        // the hub's too -- no second recogniser, no second word list, and
        // since 11 Sep no second matcher.
        //
        // PROMPTS STAY TAP-TO-TALK, deliberately and separately: those
        // carry a typing field, and a hot mic behind one is exactly what
        // submitted the PIN eight seconds in.
        _openWindow(_CaptureRole.choiceReply);
        break;

      // ── THE THREE SCREENS THAT WERE DRAWN IN SILENCE ────────────────
      //
      // `form`, `registration` and `automation_builder` were handled by
      // AppState (which draws them) and were not in this switch at all, so
      // nothing was ever said about them -- and worse, the dock still
      // rendered, so a tap fell through to a fresh COMMAND capture while
      // the hub sat waiting for a form_response. The hub refused it as
      // busy. The dock looked live on those screens and could not answer
      // them (audit, 2026-09-11).
      //
      // They are SCREENS WITH A RESULT rather than questions with an
      // answer -- the same shape as the pin map -- so what the voice owes
      // them is to say what is on screen and then get out of the way. No
      // capture window is opened: these carry text fields, and a hot mic
      // behind a text field is exactly what submitted a PIN eight seconds
      // in. Tapping the dock re-reads them (see AppState.dockTap).
      case 'form':
        await _sayScreen(_formSpeech(f.raw));
        break;

      case 'registration':
        await _sayScreen(_registrationSpeech(f.raw));
        break;

      case 'file_request': // answered by the system picker, same rule
      case 'pin_map':
        // AppState draws it and the screen arms its own lists. What the
        // flow owes it is to stop `thinking`: the hub is now waiting on
        // the screen, and a flow left thinking refuses every arm (and the
        // wake word) until the watchdog gives up a minute later.
        if (state == FlowState.thinking) {
          _thinkWatchdog?.cancel();
          state = FlowState.idleListening;
          onStatus(_listeningStatus);
        }
        break;

      case 'scene_builder':
        await _sayScreen(
            'A scene is on screen: its name, who it is for, what it is for, '
            'and its devices. Fill it in on screen, then save; '
            'tap the mic to hear this again.');
        break;

      case 'automation_builder':
        await _sayScreen(
            'An automation to build is on screen. Its blocks are: '
            'when, if, then, and else. Fill them in on screen; '
            'tap the mic to hear this again.');
        break;

      case 'output':
        // A MENU FLOW'S NARRATION IS READ, NOT RECITED (2026-09-11: "i will
        // be in adding devices, it's still dictating devices list").
        //
        // A menu action leaves the pipeline in `idleListening` -- nothing
        // was captured, so nothing moved it -- and the test below therefore
        // said yes to EVERY line such a flow writes. Opening Devices made
        // the phone read the entire fixed-width device table out loud, one
        // awaited utterance per row; and because frames are handled one at
        // a time, that queue also held up the next flow's questions. So you
        // could press Add device and stand there listening to the device
        // list, which is the exact report.
        //
        // Those lines are on the SCREEN -- the list screen, the flow lines,
        // the working screen -- and a screen is what they are for. What
        // must still be heard is anything that went WRONG (`warn`, all 177
        // of the hub's refusals and cautions) and the one `important` line
        // in the whole system. Narration goes quiet while a menu flow runs
        // and is unaffected everywhere else, so a spoken command's own
        // answer still speaks exactly as before.
        //
        // NOT THE ANSWER TO A COMMAND WE SENT. A `menuBusy` that is still up
        // (a menu action in flight, or one whose ending was missed) used to
        // silence the reply to a SPOKEN question too -- "what time is it"
        // was shown on screen and never said. While a command of ours is
        // waiting, its lines are its answer.
        final menuNarration = (f.level ?? 'info') == 'info' &&
            (isMenuFlow?.call() ?? false) &&
            !(_commandAnswer && state == FlowState.thinking);
        if (menuNarration) {
          debugPrint('[flow] menu narration kept quiet: ${f.text}');
          break;
        }
        // `important` must be voiced even mid-flow; the rest only when idle,
        // so progress chatter never talks over a capture.
        if (f.level == 'important' || state == FlowState.idleListening) {
          // The SPOKEN path gets the undecorated line. The thinking-state
          // collector below always filtered banners; this path did not, so
          // the first-run "=" * 60 banner reached the speech engine
          // verbatim. A line that is ONLY decoration is neither spoken nor
          // shown — there is nothing in it for a person.
          var spoken = _undecorate(f.text ?? '');
          if (spoken.isEmpty) break;
          // THE RECOVERY WORDS ARE NEVER SAID ALOUD. They are the PIN's
          // reset credential, shown once; the speaker says where they are.
          if ((f.text ?? '').trimLeft().startsWith('RECOVERY PHRASE')) {
            spoken = 'Your recovery words are on the screen. Write them down.';
          }
          final resume = state;
          final resumeRole = _role;
          state = FlowState.speaking;
          final outEpoch = _epoch;
          _speakingOutput = true;
          try {
            await voice.say(spoken);
          } finally {
            _speakingOutput = false;
          }
          // Restore ONLY if nothing moved the flow while we spoke: a
          // window opened by a tap, or a cancel, outranks a finished line.
          if (state == FlowState.speaking && outEpoch == _epoch) {
            if (resume == FlowState.capturing) {
              // Spoken over an open window (an `important` line): that
              // window heard the phone and its timer may have lapsed.
              _openWindow(resumeRole);
            } else {
              state = resume == FlowState.speaking
                  ? FlowState.idleListening
                  : resume;
            }
          }
        } else if (state == FlowState.thinking) {
          // Lines produced BY OUR COMMAND — query readings ("lamp: 50, on")
          // and action outcomes. Collected, then spoken with the result, so
          // what was shown is also SAID. Bracketed diagnostics are skipped —
          // EXCEPT [STATE], which is the pick-query ANSWER wearing a
          // diagnostic's clothes ("did it turn on the lights" answered with
          // nothing was this filter eating exactly those lines).
          var line = (f.text ?? '').trim();
          if (line.startsWith('[STATE]')) {
            line = line.substring(7).trim();
          }
          if (line.isNotEmpty &&
              !line.startsWith('[') &&
              !line.startsWith('(') &&
              !line.startsWith('=') &&
              !line.startsWith('-' * 3)) {
            _resultLines.add(line
                .replaceFirst(RegExp(r'^->\s*'), '')
                .replaceFirst(RegExp(r'^--- .* ---$'), ''));
          }
        } else {
          // Shown, not said: which state swallowed it is what a report needs.
          debugPrint('[flow] output not spoken (state=$state): ${f.text}');
        }
        break; // FIX: was falling through

      case 'view':
        // A view frame is how the hub's login/sign-up ceremony ENDS (it
        // sends no result), so a prompt answered by voice that left us in
        // `thinking` must come back to idle here or the wake word is dead.
        if (state == FlowState.thinking) {
          _thinkWatchdog?.cancel();
          state = FlowState.idleListening;
          onStatus(_listeningStatus);
        }
        if (f.viewName == 'enroll') {
          onStatus('enrollment needed');
          // Only for a phone nobody is behind yet. menu_enroll_user sends
          // this same frame when an enrolled person adds a second voice.
          if (state == FlowState.idleListening && _boundUser.isEmpty) {
            // Through `speaking`, like every other line: said from idle,
            // the wake word and the auto-armed windows heard it.
            await _speakThenIdle('No voice is enrolled yet. '
                'Say the wake word and any command to begin enrollment.');
            onStatus('enrollment needed');
          }
        }
        break;

      case 'result':
        // FIX (same rule as the UI layer): a busy/setup_in_progress refusal
        // answers a CONCURRENT request, not the question currently open —
        // closing the capture window here left the server's real question
        // with no way to be answered by voice.
        // not_bound is the same shape: the hub refused THIS frame and is
        // about to open the login/sign-up choice -- nothing of ours ended.
        if (!f.ok &&
            (f.reason == 'busy' ||
                f.reason == 'server_busy' ||
                f.reason == 'setup_in_progress' ||
                f.reason == 'not_bound')) {
          onStatus(f.describe());
          // FIX (mic dead until restart, cause #3): when it was OUR OWN
          // command that got refused, the flow was sitting in `thinking`
          // and this early break left it there forever — wake word ignored
          // until app restart. Refused = nothing in flight = back to idle.
          if (state == FlowState.thinking) state = FlowState.idleListening;
          break;
        }
        // A COMMAND WINDOW THAT IS OPEN IS NOT WHAT THIS RESULT ENDS. It
        // opened from rest, so the result belongs to something else -- the
        // check_notices the app fires on every connect is the usual one,
        // and it landed while the person was saying the command that woke
        // a sleeping connection, closing the window under them.
        if (state == FlowState.capturing && _role == _CaptureRole.command) {
          _resultLines.clear();
          _screenSpeech = '';
          if (!f.ok && f.reason != 'cancelled') onStatus(f.describe());
          break;
        }
        _commandAnswer = false;
        _thinkWatchdog?.cancel();
        _choiceOptions = const [];
        _narrowed = const [];
        _awaitingAnswer = false;
        // Any other open window answered the question this result ends.
        if (state == FlowState.capturing) _closeWindow(abort: true);
        // The form/registration/builder screen this flow was reading is
        // over, so its re-read goes with it: a dock tap two screens later
        // must never replay a question that has been answered.
        _screenSpeech = '';
        if (f.ok) {
          // Speak WHAT HAPPENED, not just "Done": the collected output
          // lines carry the reading ("ceiling lights: on, 50.2") for both
          // queries and actions.
          //
          // EVERYTHING THE QUESTION ASKED FOR (her rule, 2026-09-25: "it
          // has to say everything the query is asking it for"; again
          // 2026-10-02: nothing is "left on screen"). Every line is spoken,
          // in chunks, with a short listening gap after each: speaking in a
          // gap stops the reading and is taken as what you say next.
          //
          // MENU results say nothing: their outcome is the screen or the
          // card AppState draws, and "Done." after every list, every
          // profile read and the check_notices on each connect was noise.
          final isMenu = (f.reason ?? '').startsWith('menu:') ||
              (f.reason ?? '').startsWith('ui:');
          final chunks = _resultLines.isEmpty
              ? (isMenu ? <String>[] : <String>['Done.'])
              : _chunksOf(_resultLines);
          _resultLines.clear();
          if (chunks.isEmpty) {
            // Same landing as _speakThenIdle, without the sentence.
            state = FlowState.idleListening;
            onStatus(_listeningStatus);
          } else {
            await _speakAllThenIdle(chunks);
          }
        } else if (f.reason == 'not_enrolled') {
          // The handshake, not an error: our command just started
          // enrollment server-side. The record_required frames follow.
          onStatus('starting enrollment');
        } else {
          await _speakThenIdle(f.describe());
        }
        break;

      case 'error':
        await _speakThenIdle(f.describe());
        break;
    }
  }
}