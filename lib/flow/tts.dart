/// The app's only real output device. Everything the browser client showed
/// on screen, this speaks -- and speaking must be AWAITABLE, because the
/// flow records right after talking and a TTS voice leaking into its own
/// listening window would transcribe itself.
library;

import 'dart:async';

import 'package:flutter/foundation.dart' show debugPrint, ValueNotifier;
import 'package:flutter_tts/flutter_tts.dart';

class Voice {
  Voice() {
    _tts
      ..setSpeechRate(0.5)
      ..awaitSpeakCompletion(true)
      // How long the engine took to START a sentence, in the log: a slow
      // number here is the engine (or a network voice), not the app.
      // EACH WORD AS IT IS SAID (the orb's waves go out on these, her
      // design 2026-10-11: "the waves follow how it speaks"). Android's
      // engines report a word's start; none reports loudness.
      ..setProgressHandler((String text, int start, int end, String word) {
        lastWordLength = word.length;
        wordStarts.value++;
      })
      ..setStartHandler(() {
        final at = _askedAt;
        if (at != null) {
          debugPrint('[tts] started ${DateTime.now().difference(at).inMilliseconds} ms after asked');
          _askedAt = null;
        }
      });
    _ready = _preferOfflineVoice();
    unawaited(_ready.then((_) => warm(force: true)));
    _keepWarm = Timer.periodic(const Duration(seconds: 30), (_) {
      if (DateTime.now().difference(_touched) > const Duration(seconds: 25)) {
        unawaited(warm());
      }
    });
  }

  final _tts = FlutterTts();

  /// Counts the words the engine has begun saying; [lastWordLength] is the
  /// newest one's length. One voice per app, so these are shared.
  static final ValueNotifier<int> wordStarts = ValueNotifier<int>(0);
  static int lastWordLength = 0;

  /// Does this voice synthesise on a SERVER? The engine's own flag when it
  /// gives one; otherwise the name -- Google's voices end "-network" or
  /// "-local". A network voice is a round trip per sentence, which is the lag
  /// of "it does not start saying the screen at once", and flutter_tts does
  /// not report the flag on every version, so the name has to say it.
  static bool _needsNetwork(Map v) {
    final dynamic flag = v['network_required'] ?? v['networkRequired'];
    if (flag != null) {
      return flag == true || '$flag' == '1' || '$flag' == 'true';
    }
    final name = '${v['name'] ?? ''}'.toLowerCase();
    return name.contains('network') && !name.contains('local');
  }

  DateTime? _askedAt;

  Future<void> _ready = Future.value();

  // ── Warm-up ──────────────────────────────────────────────────────────
  //
  // The engine is slow to speak the first sentence after it has been idle
  // (field report 2026-10-02: changing screens, the screen's words came late
  // everywhere). A silent word -- volume 0 -- keeps it awake: once at start,
  // whenever a listening window opens (an answer is about to be spoken), and
  // every half minute of quiet. A real sentence always wins: say() stops a
  // warm-up first and puts the volume back.
  bool _warming = false;
  int _warmGen = 0;
  DateTime _touched = DateTime.now();
  Timer? _keepWarm;

  Future<void> warm({bool force = false}) async {
    if (muted || _warming || _cancel != null) return;
    if (!force &&
        DateTime.now().difference(_touched) < const Duration(seconds: 20)) {
      return;
    }
    _warming = true;
    final gen = ++_warmGen;
    try {
      await _ready;
      if (gen != _warmGen) return;
      await _tts.setVolume(0.0);
      if (gen != _warmGen) return;
      await _tts.speak('ok').timeout(const Duration(seconds: 4));
    } catch (_) {
      // A warm-up that fails is just not warm.
    } finally {
      try {
        await _tts.setVolume(1.0);
      } catch (_) {}
      _warming = false;
      _touched = DateTime.now();
    }
  }

  void dispose() {
    _keepWarm?.cancel();
    _keepWarm = null;
  }

  /// Everything the hub says goes through the platform's TTS engine, and a
  /// Google TTS "network" voice synthesises on Google's servers. Where the
  /// engine reports which voices need the network, pick one that does not.
  /// Where it reports nothing (flutter_tts exposes no such key on every
  /// version), on-device synthesis cannot be guaranteed through flutter_tts
  /// at all -- nothing is changed and the default voice stands. Never throws.
  Future<void> _preferOfflineVoice() async {
    try {
      final dynamic voices = await _tts.getVoices;
      if (voices is! List) return;
      final dynamic current = await _tts.getDefaultVoice;
      final locale = current is Map ? '${current['locale'] ?? ''}' : '';
      final lang = (locale.isEmpty ? 'en' : locale)
          .toLowerCase()
          .replaceAll('_', '-');
      final language = lang.split('-').first;
      Map<String, String>? pick;
      var sawFlag = false;
      for (final dynamic v in voices) {
        if (v is! Map) continue;
        sawFlag = true;
        final needsNet = _needsNetwork(v);
        final vLocale =
            '${v['locale'] ?? ''}'.toLowerCase().replaceAll('_', '-');
        if (needsNet || !vLocale.startsWith(language)) continue;
        pick = {'name': '${v['name'] ?? ''}', 'locale': '${v['locale'] ?? ''}'};
        if (vLocale == lang) break; // exact locale beats a language match
      }
      if (!sawFlag || pick == null) return;
      await _tts.setVoice(pick);
      debugPrint('[tts] offline voice: ${pick['name']} (${pick['locale']})');
    } catch (_) {
      // TTS setup must never take the flow down; the default voice stands.
    }
  }

  /// When true, say() returns immediately without speaking. The FLOW is
  /// unaffected on purpose: capture windows still open after the (skipped)
  /// prompt, so a muted phone answers questions by reading the screen.
  bool muted = false;

  /// Speak and return only when playback has finished — BOUNDED.
  ///
  /// FIX (mic dead until restart, cause #1): some Android TTS engines drop
  /// the completion callback (notably after stop() or overlapping
  /// utterances), and an unresolved speak() future froze whatever flow was
  /// awaiting it in `speaking` forever — where the wake word is ignored.
  /// The budget scales with text length; on timeout the flow simply moves
  /// on mid-sentence, which is always better than a dead microphone.
  /// The sentence being said (or last said): the flow's "stop" watch
  /// ignores the phone's own words heard back through the microphone.
  String saying = '';

  Future<void> say(String text) async {
    if (muted || text.trim().isEmpty) return;
    saying = text;
    final asked = DateTime.now();
    // The first sentence never starts in the middle of the voice being set.
    await _ready.timeout(const Duration(seconds: 2), onTimeout: () {});
    final waited = DateTime.now().difference(asked).inMilliseconds;
    if (waited > 50) debugPrint('[tts] waited $waited ms for the voice to be set');
    if (_warming) {
      _warmGen++; // the warm-up gives way, and must not mute what follows
      try {
        await _tts.stop();
        await _tts.setVolume(1.0);
        // A speak() sent the instant after stop() is dropped by some
        // engines: the sentence is "shown, never said". Let it settle.
        await Future.delayed(const Duration(milliseconds: 150));
      } catch (_) {}
    }
    final words = text.split(RegExp(r'\s+')).length;
    // ROOM FOR A LIST. The cap was 25 s, and an option list -- the
    // question, every numbered name, then the
    // how-to-answer tail -- runs past forty words, which is right at the
    // cap at a normal speaking rate. When the budget ran out the flow
    // moved on and OPENED ITS LISTENING WINDOW while the engine was still
    // reading the names: the window heard the phone's own voice,
    // transcribed it, matched nothing, re-read the list, and heard itself
    // again (2026-09-11, "say an option doesn't work"). Sixty seconds is
    // the ceiling now, and the engine is SILENCED on the way out, so a
    // window can never open under a voice that is still talking.
    // FROM THE ASKING, NOT FROM THE FIRST WORD -- so it has to cover a slow
    // start as well. On a slow phone the engine can take seconds to begin
    // (see the "[tts] started ... ms after asked" line); at half a second a
    // word the old budget then ran out IN THE MIDDLE of an ordinary
    // sentence and the stop below cut it off ("it speaks, then stops
    // talking mid-sentence", 2026-10-06). The budget below is still an end
    // to a sentence whose "done" never comes.
    // TWO SECONDS A WORD (it was one). A board's name is four "words" and
    // eleven said aloud -- "ESP32-S3-DevKitC-1" is E, S, P, thirty-two, S,
    // three, dev, kit, C, one -- so three of them in a sentence ran past
    // the budget, the stop below cut the third off after "ESP", the pause
    // came, and the next sentence began with the one after ("it tries to
    // say the ESP, then pauses, then starts in number five", 2026-10-06).
    // The X does not wait for this: it ends the sentence at once.
    final budget = Duration(seconds: (8 + words * 2).clamp(8, 120));
    _askedAt = DateTime.now();
    // STOP MEANS STOP, WHATEVER THE ENGINE DOES (2026-09-11). The dock's X
    // ends the sentence, and the flow behind it is waiting on this future:
    // if an engine drops its completion callback on stop() -- several
    // Android ones do, which is the whole reason the budget exists -- the
    // caller would sit here until the budget ran out, and the X would look
    // like it had done nothing for up to a minute. Racing an explicit
    // cancel against the engine makes the return immediate either way.
    //
    // A NEW SENTENCE SUPERSEDES THE OLD ONE. The engine flushes the
    // previous utterance and its completion never arrives, so the earlier
    // caller is released here -- otherwise it sat out its whole budget and
    // then its timeout stop() cut off whatever was being said by then.
    final previous = _cancel;
    if (previous != null && !previous.isCompleted) previous.complete();
    final cancel = Completer<void>();
    _cancel = cancel;
    try {
      final began = DateTime.now();
      await Future.any([
        _tts.speak(text).timeout(budget),
        cancel.future,
      ]);
      // SOME ENGINES SAY "DONE" BEFORE THEY ARE -- notably the first
      // sentence after the voice was changed. The flow opens its
      // microphone the moment this returns, and then hears the sentence
      // itself. Nobody says a word in under a tenth of a second: a return
      // quicker than that per word is waited out (not after a stop()).
      // A FIFTH OF A SECOND A WORD (it was a tenth, which only caught an
      // engine that returned at once). One that says "done" part-way
      // through is as bad: the next sentence is sent while this one is
      // still in the air, the engine drops what is left of this one for
      // it, and what the person hears is an item missing after a pause
      // ("some options were skipped", 2026-10-06). The fastest pace the
      // app sets is about a quarter of a second a word, so an honest
      // engine never waits here.
      final least = Duration(milliseconds: (words * 200).clamp(0, 6000));
      final spent = DateTime.now().difference(began);
      if (!cancel.isCompleted && spent < least) {
        debugPrint('[tts] engine returned after ${spent.inMilliseconds} ms '
            'for $words word(s) -- waiting it out');
        await Future.any([Future<void>.delayed(least - spent), cancel.future]);
      }
    } catch (_) {
      // TimeoutException or a platform error — never let TTS pin the flow,
      // and never let it keep talking into the window that opens next.
      // Only while this is still the current sentence: a later one owns
      // the engine now.
      if (identical(_cancel, cancel)) {
        try {
          await _tts.stop();
        } catch (_) {}
      }
    } finally {
      if (identical(_cancel, cancel)) _cancel = null;
      lastSpokeEnd = DateTime.now();
      debugPrint('[tts] said $words word(s) in '
          '${lastSpokeEnd!.difference(asked).inMilliseconds} ms in all');
      _touched = lastSpokeEnd!;
    }
  }

  /// When the phone last stopped talking (finished, cut off or timed out),
  /// or null if it never has. A capture window opened right after uses it
  /// to skip the tail of its own voice still in the mic buffers.
  DateTime? lastSpokeEnd;

  /// Completed by stop() to end the awaited say() at once. Null when
  /// nothing is being spoken.
  Completer<void>? _cancel;

  /// End whatever is being said, now. Safe to call when nothing is.
  Future<void> stop() async {
    final cancel = _cancel;
    _cancel = null;
    if (cancel != null && !cancel.isCompleted) {
      cancel.complete();
      lastSpokeEnd = DateTime.now();
    }
    try {
      await _tts.stop();
    } catch (_) {
      // Nothing was speaking, or the platform lost it. Either way it is
      // quiet now, which is what was asked for.
    }
  }
}