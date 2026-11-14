/// phone_wake.dart — the wake word heard by the PHONE'S OWN speech
/// recogniser (Google's "Speech Recognition & Synthesis" on Android, Apple's
/// on iOS) instead of the bundled sherpa-onnx keyword spotter.
///
/// WHY IT EXISTS. The keyword spotter is private and cheap but it is a
/// small model, and on some phones and in some rooms it misses the phrase
/// more than a person will forgive. The phone's recogniser is a much bigger
/// model that hears well. Her ask, 2026-09-03: let it do the wake word.
///
/// WHAT IT DOES AND DOES NOT DO. It listens for the wake phrase and NOTHING
/// ELSE. The moment the phrase is heard it stops, hands the microphone back,
/// and the app records the command with its OWN capture path exactly as
/// before -- so the raw clip still reaches the hub for the passive voice
/// check and the challenge, and the on-device transcriber still writes the
/// text. The recogniser never hears a command, never sees a challenge, and
/// never has the mic while the app is speaking. Two engines, one
/// microphone, strictly in turns: the recogniser holds it only while the
/// flow is idle (see VoiceFlow._applyMicPolicy).
///
/// WHAT IT COSTS, said plainly, because this app's whole argument is
/// privacy:
///   * The recogniser is not ours. With [onDeviceOnly] the app asks for
///     recognition that stays on the phone; on Android 12+ with the offline
///     language pack installed that request is binding (the on-device
///     recogniser is used and nothing leaves the phone). On an older phone,
///     or without the pack, it is a PREFERENCE the service may not honour,
///     and the room's sound while idle may reach the vendor's servers. The
///     Settings row says so; the default keeps the bundled spotter.
///   * It is a session recogniser, not an always-on one: each session ends
///     after a pause or a timeout and is started again here, with a short
///     gap between. Some phones play the recogniser's tone at each start;
///     that tone is the service's and cannot be silenced from Dart.
///   * It draws more battery than the spotter.
///
/// The `speech_to_text` plugin is the one dependency; it is used through
/// exactly four calls (initialize, listen, cancel, isListening) so that a
/// plugin change is confined to this file.
library;

import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:speech_to_text/speech_recognition_error.dart';
import 'package:speech_to_text/speech_recognition_result.dart';
import 'package:speech_to_text/speech_to_text.dart' as stt;

class PhoneWake {
  PhoneWake({
    required this.phrase,
    required this.onWake,
    required this.onProblem,
    this.onDeviceOnly = true,
    this.localeId,
  });

  /// The wake phrase, e.g. "hey home".
  final String phrase;

  /// Fired ONCE per hit, after the recogniser has been stopped. The caller
  /// owns the microphone from this moment.
  final void Function() onWake;

  /// A problem the person must hear about (no recogniser, permission
  /// refused, offline pack missing). Called at most once per distinct
  /// message while running.
  final void Function(String message) onProblem;

  /// Ask for recognition that stays on the phone. See the library comment
  /// for what that guarantee is worth on which phones.
  final bool onDeviceOnly;

  /// null = the phone's default locale.
  final String? localeId;

  final stt.SpeechToText _stt = stt.SpeechToText();

  /// SpeechToText() is a process-wide SINGLETON, and its initialize() binds
  /// onStatus/onError only the first time it succeeds -- every later call
  /// returns true and keeps the FIRST listeners. A PhoneWake built after
  /// that (a Settings change, the engine switched back, a re-pair) never
  /// heard 'done', so it never restarted after its first session: deaf,
  /// while isRunning said it was listening. The listeners are bound once to
  /// these trampolines, which reach whichever instance started last.
  static PhoneWake? _active;
  static void _statusToActive(String status) => _active?._onStatus(status);
  static void _errorToActive(SpeechRecognitionError e) =>
      _active?._onError(e);

  bool _initialised = false;
  bool _available = false;
  bool _wanted = false;
  Timer? _restart;
  int _failures = 0;
  String? _lastProblem;
  int _generation = 0;

  bool get isRunning => _wanted;

  /// False once the phone has been asked and has no recogniser to offer.
  /// Distinct from a start() that returned false because stop() came
  /// first -- only THIS is a reason to fall back to the bundled spotter.
  bool get available => !_initialised || _available;

  /// A listen() call is in flight to the platform. isListening only turns
  /// true after that call returns, so a restart must not double up on it.
  bool _starting = false;
  Future<void>? _listenInFlight;

  /// Start listening for the phrase. False when the phone has no usable
  /// recogniser (a problem has been reported).
  Future<bool> start() async {
    _wanted = true;
    _active = this;
    final gen = ++_generation;
    if (!_initialised) {
      _initialised = true;
      try {
        _available = await _stt.initialize(
          onStatus: _statusToActive,
          onError: _errorToActive,
          debugLogging: false,
        );
      } catch (e) {
        debugPrint('[phonewake] initialize threw: $e');
        _available = false;
      }
    }
    if (gen != _generation || !_wanted) return false; // stopped meanwhile
    if (!_available) {
      _problem('This phone has no speech recogniser available, so the '
          'phone wake word cannot run. The bundled wake word is still used.');
      _wanted = false;
      return false;
    }
    _listen();
    return true;
  }

  /// Stop listening and give the microphone back. Safe to call twice.
  Future<void> stop() async {
    _wanted = false;
    _generation++;
    _restart?.cancel();
    _restart = null;
    // A listen() still on its way to the platform would otherwise open a
    // session AFTER this returns -- and the caller takes "stop() returned"
    // to mean the recogniser has let go of the microphone.
    final inFlight = _listenInFlight;
    if (inFlight != null) await inFlight;
    if (_stt.isListening) {
      try {
        await _stt.cancel();
      } catch (_) {
        // Already gone; the outcome asked for.
      }
    }
  }

  Future<void> dispose() => stop();

  void _listen() {
    if (!_wanted || _starting || _stt.isListening) return;
    _starting = true;
    final gen = _generation;
    // partialResults: the phrase is acted on the moment it appears in a
    // partial, not after the pause that ends the session. cancelOnError:
    // a broken session is restarted by us, on our own schedule.
    //
    // listen() is async and its failures arrive on the FUTURE (the plugin
    // rethrows a platform error as ListenFailedException there), so they
    // are caught there: a sync try/catch never saw them, and a dead
    // session in idle is a deaf phone.
    _listenInFlight = _stt
        .listen(
          onResult: _onResult,
          // listenFor / pauseFor / localeId live in SpeechListenOptions
          // now; listen()'s own parameters of those names are deprecated.
          listenOptions: stt.SpeechListenOptions(
            listenFor: const Duration(seconds: 50),
            pauseFor: const Duration(seconds: 3),
            localeId: localeId,
            partialResults: true,
            onDevice: onDeviceOnly,
            listenMode: stt.ListenMode.dictation,
            cancelOnError: true,
          ),
        )
        .then((_) {
      _starting = false;
      _listenInFlight = null;
      if (gen != _generation) {
        // stop() or a restart came while this was starting. A newer
        // start() was turned away by the _starting guard, so it is
        // resumed here; a stop() is honoured by closing what just opened.
        if (_wanted) {
          _scheduleRestart();
        } else if (_stt.isListening) {
          unawaited(_stt.cancel().catchError((_) {}));
        }
        return;
      }
      // The platform may decline without an error (started=false): no
      // status arrives, so a session that did not open is retried here.
      if (!_stt.isListening) _scheduleRestart();
    }, onError: (Object e) {
      _starting = false;
      _listenInFlight = null;
      if (gen != _generation) return;
      debugPrint('[phonewake] listen failed: $e');
      _failures++;
      _scheduleRestart();
    });
  }

  void _onStatus(String status) {
    // 'listening' | 'notListening' | 'done' | 'doneNoResult'
    if (status == 'done' || status == 'doneNoResult' ||
        status == 'notListening') {
      _scheduleRestart();
    }
  }

  void _onError(SpeechRecognitionError e) {
    switch (e.errorMsg) {
      case 'error_permission':
      case 'error_insufficient_permissions':
        _problem('Microphone permission was refused for speech recognition.');
        _wanted = false;
        return;
      case 'error_network':
      case 'error_network_timeout':
        // With onDeviceOnly this is the tell that the phone has no offline
        // pack and the service tried the network anyway.
        if (onDeviceOnly) {
          _problem('The phone\'s recogniser wanted the network. Install the '
              'offline speech pack for your language in the phone\'s speech '
              'settings, or switch the wake word back to the bundled one.');
        }
        _failures++;
        break;
      case 'error_busy':
      case 'error_client':
      case 'error_audio_error':
      case 'error_server':
        _failures++;
        break;
      default:
        // error_no_match / error_speech_timeout: a quiet room. Normal.
        break;
    }
    _scheduleRestart();
  }

  void _scheduleRestart() {
    if (!_wanted) return;
    _restart?.cancel();
    final ms = _failures == 0
        ? 150
        : math.min(8000, 500 * (1 << math.min(_failures, 4)));
    _restart = Timer(Duration(milliseconds: ms), () {
      _restart = null;
      _listen();
    });
  }

  void _onResult(SpeechRecognitionResult r) {
    if (!_wanted) return;
    if (r.finalResult) _failures = 0;
    final heard = r.recognizedWords;
    if (heard.isEmpty) return;
    if (!wakeHeard(heard, phrase)) return;
    debugPrint('[phonewake] HIT in "$heard"');
    // Stop FIRST, then hand over: the caller opens the microphone next and
    // the recogniser must not still be holding it.
    unawaited(stop().then((_) => onWake()));
  }

  void _problem(String message) {
    if (_lastProblem == message) return;
    _lastProblem = message;
    onProblem(message);
  }

  // ── The match, pure and testable ─────────────────────────────────────

  static final RegExp _nonWord = RegExp(r"[^a-z0-9' ]+");

  static List<String> _words(String s) => s
      .toLowerCase()
      .replaceAll(_nonWord, ' ')
      .split(' ')
      .where((w) => w.isNotEmpty)
      .toList();

  /// Small spellings the recogniser produces for short wake words. The
  /// generic edit-distance rule below refuses to be lenient on words this
  /// short, so the known ones are listed.
  static const Map<String, Set<String>> _shortVariants = {
    'hey': {'hey', 'hay', 'hi', 'eh'},
    'home': {'home', 'hone', 'hom', 'homme', 'holm'},
    'ok': {'ok', 'okay', 'k'},
    'hello': {'hello', 'hallo', 'hullo', 'hellow', 'halo', 'hell', 'yellow', 'hollow'},
    'voice': {'voice', 'voic', 'voyce', 'boice', 'voices'},
    // The names on offer (app_state.dart `personas`) and what a recogniser
    // writes when it mishears them.
    'alfred': {'alfred', 'alford', 'alfredo', 'alfread', 'alfrid', 'alfrad',
      'olfred', 'elfred', 'offered', 'alfredd', 'alfreds', 'alfrida'},
    'jasper': {'jasper', 'jaspar', 'jesper', 'jaspers', 'gasper', 'casper',
      'jazper', 'jasber', 'jasbar', 'jaspa', 'jasperr', 'jaspher'},
    'nova': {'nova', 'novah', 'noah', 'novo', 'nover', 'novas', 'noba',
      'novia', 'nuva', 'nava', 'nora', 'nove', 'novar'},
    'stella': {'stella', 'stela', 'stellar', 'stellah', 'stelle', 'steller',
      'estella', 'stellas', 'stealer', 'stellaa', 'stiller', 'stela\'s'},
  };

  static bool _sameWord(String a, String b) {
    if (a == b) return true;
    final v = _shortVariants[b];
    if (v != null && v.contains(a)) return true;
    if (a.length < 4 || b.length < 4) return false;
    final tolerance = b.length >= 6 ? 2 : 1;
    return _edits(a, b) <= tolerance;
  }

  static int _edits(String a, String b) {
    final m = a.length, n = b.length;
    var prev = List<int>.generate(n + 1, (j) => j);
    var cur = List<int>.filled(n + 1, 0);
    for (var i = 1; i <= m; i++) {
      cur[0] = i;
      for (var j = 1; j <= n; j++) {
        final cost = a.codeUnitAt(i - 1) == b.codeUnitAt(j - 1) ? 0 : 1;
        cur[j] = math.min(math.min(cur[j - 1] + 1, prev[j] + 1),
            prev[j - 1] + cost);
      }
      final t = prev;
      prev = cur;
      cur = t;
    }
    return prev[n];
  }

  /// Did [heard] contain [phrase]? The phrase's words must appear in
  /// order, adjacent, anywhere in what was heard — so "hey home turn on the
  /// lights" wakes just as "hey home" does. A run-together hearing
  /// ("heyhome") is accepted too.
  /// How many words, the way [wakeHeard] counts them.
  static int wordCount(String text) => _words(text).length;

  static bool wakeHeard(String heard, String phrase) {
    final want = _words(phrase);
    if (want.isEmpty) return false;
    final got = _words(heard);
    if (got.isEmpty) return false;
    for (var i = 0; i + want.length <= got.length; i++) {
      var ok = true;
      for (var k = 0; k < want.length; k++) {
        if (!_sameWord(got[i + k], want[k])) {
          ok = false;
          break;
        }
      }
      if (ok) return true;
    }
    // A LONG PHRASE DOES NOT HAVE TO ARRIVE WHOLE. The recogniser drops or
    // mangles one word of three often enough to matter (a burst read by a
    // small model especially), so two neighbouring words of a three-word
    // phrase, in order, are enough -- and a run-together pair
    // ("homevoice") too. Two-word phrases still need both words.
    if (want.length >= 3) {
      for (var i = 0; i + 1 < got.length; i++) {
        for (var k = 0; k + 1 < want.length; k++) {
          if (_sameWord(got[i], want[k]) && _sameWord(got[i + 1], want[k + 1])) {
            return true;
          }
        }
      }
      for (var k = 0; k + 1 < want.length; k++) {
        final pair = want[k] + want[k + 1];
        for (final w in got) {
          if (w.length >= 6 && _edits(w, pair) <= (pair.length >= 8 ? 2 : 1)) {
            return true;
          }
        }
      }
    }
    // "HELLO <NAME>": THE NAME ALONE IS ENOUGH. Of the two words, "hello"
    // is the one a small model drops or bends, and the name is the
    // distinctive half -- so a hearing of just the name (or of the name
    // split in two, "no va") counts. Only for a two-word phrase that begins
    // "hello" and ends in a name of four letters or more.
    if (want.length == 2 && want[0] == 'hello' && want[1].length >= 4) {
      final name = want[1];
      // Stricter than between two words: alone, a name within one edit
      // (or a listed spelling) counts, so "still" is not "stella".
      bool near(String w) =>
          w == name ||
          (_shortVariants[name]?.contains(w) ?? false) ||
          (w.length >= 4 && _edits(w, name) <= 1);
      for (var i = 0; i < got.length; i++) {
        if (near(got[i])) return true;
        if (i + 1 < got.length &&
            got[i].length >= 2 &&
            got[i + 1].length >= 2 &&
            near(got[i] + got[i + 1])) {
          return true;
        }
      }
    }
    if (want.length > 1) {
      final joined = want.join();
      for (final w in got) {
        if (w.length >= 4 && _edits(w, joined) <= (joined.length >= 6 ? 2 : 1)) {
          return true;
        }
      }
    }
    return false;
  }
}