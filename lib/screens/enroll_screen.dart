import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:homevoice/flow/voice_flow.dart';
import '../app_state.dart';
import '../widgets/orb.dart';
import '../widgets/pin_boxes.dart';

/// THE REGISTRATION CEREMONY — one room, one ritual (the owner's spec,
/// 2026-08-27: "the screen should just be the orb and the sentence to say
/// for registration then pin and secret words... just the orb and the
/// mode").
///
/// No app bar, no drawer, no dock, no feed. The whole registration flows
/// through THIS screen without ever leaving it:
///
///   * a question from the hub (your name, then later the PIN) renders
///     HERE, centred — AppState holds prompts instead of pushing the
///     generic Question screen while this room is up;
///   * the phrases: the orb, the sentence to say above it, a quiet
///     "phrase 2 of 5" — nothing else;
///   * the PIN: the six boxes appear where the field was, number pad
///     only, submits itself on the last digit;
///   * the recovery words arrive as the keepsafe card AppState already
///     owns, over this same room;
///   * done → the hub switches the view and the room dismisses itself.
///
/// And the hub's law underneath makes the minimalism safe: registration
/// is ONE TRANSACTION — stop anywhere and nothing is saved, so this room
/// never has to explain a half-finished state. Cancel really means
/// "nothing happened".
class EnrollScreen extends StatefulWidget {
  final AppState appState;
  const EnrollScreen({super.key, required this.appState});

  @override
  State<EnrollScreen> createState() => _EnrollScreenState();
}

class _EnrollScreenState extends State<EnrollScreen> {
  final _ctl = TextEditingController();
  bool _reveal = false;

  /// The hub's serial for the question on screen. Keyed on the SERIAL, as
  /// every other question screen is: a re-asked question with the same
  /// wording (a refused name, a wrong PIN) is still a new question.
  int? _lastSerial;

  @override
  void dispose() {
    _ctl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: widget.appState,
      builder: (context, _) => _build(context),
    );
  }

  Widget _build(BuildContext context) {
    final s = widget.appState;
    final record = s.pendingRecord;
    final prompt = s.pendingPrompt;

    // Fresh question, fresh field.
    if (s.questionSerial != _lastSerial) {
      _lastSerial = s.questionSerial;
      _ctl.clear();
      _reveal = false;
    }

    final Widget body;
    if (record != null) {
      body = _phrase(s, record);
    } else if (prompt != null) {
      final isPin =
          prompt.secret && prompt.text.toUpperCase().contains('PIN');
      body = isPin
          ? _pin(s, prompt.text)
          : _question(s, prompt.text, secret: prompt.secret);
    } else {
      body = _between(s);
    }

    return AnnotatedRegion<SystemUiOverlayStyle>(
      value: s.isLight
          ? SystemUiOverlayStyle.dark.copyWith(
              statusBarColor: Colors.transparent,
              systemNavigationBarColor: s.bg,
              systemNavigationBarIconBrightness: Brightness.dark,
            )
          : SystemUiOverlayStyle.light.copyWith(
              statusBarColor: Colors.transparent,
              systemNavigationBarColor: s.bg,
              systemNavigationBarIconBrightness: Brightness.light,
            ),
      // NO AppBar — the ritual owns the whole screen. Backing out is the
      // ceremony's cancel (PopScope tells the hub; the transaction law
      // erases anything partial), so system-back is safe and honest.
      child: PopScope(
        onPopInvokedWithResult: (didPop, _) {
          if (didPop) s.onQuestionRouteDismissed();
        },
        child: Scaffold(
          backgroundColor: s.bg,
          body: SafeArea(
            child: Stack(
              children: [
                Center(
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(maxWidth: 380),
                    child: Padding(
                      padding: const EdgeInsets.fromLTRB(28, 12, 28, 24),
                      child: body,
                    ),
                  ),
                ),
                // "just the orb and the mode (dark/light)" — the owner's
                // spec named exactly two things this room may carry, and
                // the first build shipped only one of them (field test
                // 2026-08-27). The mode switch, quiet, top-right.
                Positioned(
                  top: 4,
                  right: 8,
                  child: IconButton(
                    tooltip: s.isLight ? 'Dark mode' : 'Light mode',
                    icon: Icon(
                      s.isLight
                          ? Icons.dark_mode_outlined
                          : Icons.light_mode_outlined,
                      size: 20,
                      color: s.inkDim,
                    ),
                    onPressed: s.cycleTheme,
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  // ── the phrase steps: the sentence, the orb, nothing else ──────────

  Widget _phrase(AppState s, RecordFrame record) {
    final phrase = record.phrase;
    final index = record.index;
    final total = record.total;

    final flow = s.flowState;
    final isActive =
        flow == FlowState.capturing || flow == FlowState.speaking;
    final listening = flow == FlowState.capturing;

    return Column(
      mainAxisAlignment: MainAxisAlignment.center,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (total > 0) ...[
          Text('phrase $index of $total',
              textAlign: TextAlign.center,
              style: TextStyle(
                  fontSize: 11,
                  letterSpacing: 1.1,
                  fontWeight: FontWeight.w600,
                  color: s.inkFaint)),
          const SizedBox(height: 10),
          _pips(s, index, total),
          const SizedBox(height: 30),
        ],
        // THE SENTENCE — bare, large, the one instruction on screen. The
        // old card ("SAY THIS" in a border) is gone: in an empty room the
        // words need no frame.
        Text(
          phrase.isEmpty ? 'Say the phrase you hear' : '“$phrase”',
          textAlign: TextAlign.center,
          style: TextStyle(
              color: s.ink,
              fontSize: 24,
              fontWeight: FontWeight.w700,
              height: 1.4),
        ),
        const SizedBox(height: 44),
        Center(
          child: Orb(
            appState: s,
            phrase: null,
            isActive: isActive,
            isSpeaking: flow == FlowState.speaking,
            isThinking: s.isStarting,
            showCancel: true,
            // The CEREMONY's cancel, not the eject seat: the hub rolls back
            // and re-asks the name question in this same room.
            onCancel: () => s.sendCeremonyCancel(),
            // dockTap, NOT triggerCapture: this tap must open an
            // ENROLLMENT capture so the phrase leaves as a `recording`
            // frame — triggerCapture opens a COMMAND window.
            onTap: () => s.dockTap(),
          ),
        ),
        const SizedBox(height: 26),
        Text(
          listening
              ? 'recording — tap again when done'
              : (s.awaitingCapture ? 'tap the circle, then say it' : s.status),
          textAlign: TextAlign.center,
          style: TextStyle(
              color: (listening || s.awaitingCapture) ? s.accent : s.inkDim,
              fontSize: 14,
              fontWeight: FontWeight.w600),
        ),
      ],
    );
  }

  Widget _pips(AppState s, int index, int total) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: List.generate(total, (i) {
        // Theme accent throughout — the off-palette green is retired
        // (owner's colour law, 2026-08-26). Done and current pips share
        // the accent; only width tells them apart, which is enough.
        final done = i + 1 < index;
        final current = i + 1 == index;
        return AnimatedContainer(
          duration: const Duration(milliseconds: 200),
          margin: const EdgeInsets.symmetric(horizontal: 3),
          width: current ? 34 : 24,
          height: 5,
          decoration: BoxDecoration(
            color: (done || current)
                ? s.accent.withValues(alpha: done ? 0.55 : 1.0)
                : s.border,
            borderRadius: BorderRadius.circular(3),
          ),
        );
      }),
    );
  }

  // ── a question from the hub, in the same room ──────────────────────

  Widget _question(AppState s, String text, {bool secret = false}) {
    return Column(
      mainAxisAlignment: MainAxisAlignment.center,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          _cleanQuestion(text),
          textAlign: TextAlign.center,
          style: TextStyle(
              color: s.ink,
              fontSize: 22,
              fontWeight: FontWeight.w600,
              height: 1.35),
        ),
        _hubNote(s),
        const SizedBox(height: 30),
        TextField(
          controller: _ctl,
          autofocus: true,
          autocorrect: false,
          enableSuggestions: !secret,
          enableIMEPersonalizedLearning: false,
          // THE HUB SAYS WHAT IS SECRET, here as on the Question screen: a
          // masked question that is not the PIN still must not be drawn in
          // the clear just because it was asked in this room.
          obscureText: secret && !_reveal,
          textAlign: TextAlign.center,
          style: TextStyle(color: s.ink, fontSize: 17),
          decoration: InputDecoration(
            suffixIcon: secret
                ? IconButton(
                    tooltip: _reveal ? 'Hide' : 'Show',
                    icon: Icon(
                        _reveal
                            ? Icons.visibility_off_outlined
                            : Icons.visibility_outlined,
                        color: s.inkDim),
                    onPressed: () => setState(() => _reveal = !_reveal),
                  )
                : null,
            filled: true,
            fillColor: s.surface,
            border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(10),
                borderSide: BorderSide(color: s.border)),
            enabledBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(10),
                borderSide: BorderSide(color: s.border)),
            focusedBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(10),
                borderSide: BorderSide(color: s.accent, width: 1.4)),
          ),
          onSubmitted: (_) => _sendTyped(s),
        ),
        const SizedBox(height: 16),
        FilledButton(
          style: FilledButton.styleFrom(
            backgroundColor: s.accent,
            foregroundColor: s.accentFg,
            padding: const EdgeInsets.symmetric(vertical: 14),
            shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(10)),
          ),
          onPressed: () => _sendTyped(s),
          child: const Text('Continue',
              style: TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
        ),
        const SizedBox(height: 8),
        _cancelText(s),
      ],
    );
  }

  // ── the PIN, six boxes, same room ──────────────────────────────────

  Widget _pin(AppState s, String text) {
    return Column(
      mainAxisAlignment: MainAxisAlignment.center,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text('Set your PIN',
            textAlign: TextAlign.center,
            style: TextStyle(
                color: s.ink, fontSize: 22, fontWeight: FontWeight.w600)),
        const SizedBox(height: 6),
        Text('Six digits. The fallback when your voice cannot be verified.',
            textAlign: TextAlign.center,
            style: TextStyle(color: s.inkDim, fontSize: 12.5)),
        _hubNote(s),
        const SizedBox(height: 26),
        PinBoxes(
          key: ValueKey(s.questionSerial),
          onComplete: (pin) => s.sendPromptReply(pin),
          fillColor: s.surface,
          borderColor: s.border,
          focusColor: s.accent,
          textColor: s.ink,
          revealed: _reveal,
        ),
        const SizedBox(height: 12),
        Center(
          child: TextButton.icon(
            onPressed: () => setState(() => _reveal = !_reveal),
            icon: Icon(
                _reveal
                    ? Icons.visibility_off_outlined
                    : Icons.visibility_outlined,
                size: 16,
                color: s.inkDim),
            label: Text(_reveal ? 'Hide digits' : 'Show digits',
                style: TextStyle(color: s.inkDim, fontSize: 12.5)),
          ),
        ),
        const SizedBox(height: 4),
        _cancelText(s),
      ],
    );
  }

  // ── between steps: the hub is working ──────────────────────────────

  Widget _between(AppState s) {
    return Column(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        SizedBox(
          width: 24,
          height: 24,
          child: CircularProgressIndicator(strokeWidth: 2.5, color: s.accent),
        ),
        const SizedBox(height: 18),
        Text(s.status,
            textAlign: TextAlign.center,
            style: TextStyle(color: s.inkDim, fontSize: 13.5)),
      ],
    );
  }

  // ── shared bits ────────────────────────────────────────────────────

  /// The hub's last flow line — "Incorrect...", "must be 1-64
  /// characters..." — shown WITH the question, amber when it is a
  /// complaint. Same idea as auth_screen's note.
  Widget _hubNote(AppState s) {
    final note = (s.lastFlowMessage ?? '').split('\n').last.trim();
    if (note.isEmpty) return const SizedBox.shrink();
    final warn = note.contains('Incorrect') ||
        note.contains('must be') ||
        note.contains('try again') ||
        note.contains('Try again') ||
        note.contains('digits') ||
        note.contains('cannot');
    return Padding(
      padding: const EdgeInsets.only(top: 12),
      child: Text(
        note,
        textAlign: TextAlign.center,
        style: TextStyle(
          color: warn ? const Color(0xFFD29922) : s.inkDim,
          fontSize: 13,
          fontWeight: warn ? FontWeight.w600 : FontWeight.w400,
        ),
      ),
    );
  }

  Widget _cancelText(AppState s) {
    return Center(
      child: TextButton(
        // sendCeremonyCancel, NOT sendCancel: registration's cancel rolls
        // the ceremony back and starts it over AT THE NAME QUESTION — in
        // this room — instead of ejecting to home (field test 2026-08-27).
        onPressed: () => s.sendCeremonyCancel(),
        // cancelInk = the DISCONNECT button's red (her rule, corrected
        // 2026-08-27 evening: "every cancel must have it") — faint grey
        // read as furniture, and a control that decides the whole
        // ceremony must read as one.
        child: Text('Cancel — nothing will be saved',
            style: TextStyle(
                color: s.cancelInk,
                fontSize: 12.5,
                fontWeight: FontWeight.w500)),
      ),
    );
  }

  void _sendTyped(AppState s) {
    final text = _ctl.text.trim();
    if (text.isEmpty) return;
    _ctl.clear();
    s.sendPromptReply(text);
  }

  /// The wire prompt carries terminal punctuation ("  Name to enroll: ")
  /// — the room asks it like a person. "Name to enroll" is the hub's
  /// wording since 2026-09-14 (register_person); only the older "User ID
  /// to enroll" was matched, so the current hub's question showed raw.
  String _cleanQuestion(String text) {
    var t = text.trim();
    if (t.endsWith(':')) t = t.substring(0, t.length - 1).trim();
    if (t == 'Name to enroll' || t == 'User ID to enroll') {
      return 'What should the house call you?';
    }
    return t;
  }
}