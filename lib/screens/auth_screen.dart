/// Log in or sign up, right after pairing.
///
/// This screen owns no logic. The hub drives the flow -- who is asked what,
/// how many PIN attempts, what happens next -- and this renders whichever
/// question is pending and answers through the SAME sendChoiceResponse /
/// sendPromptReply every other screen uses. The generic choice screen could
/// show these questions too (it did, for one evening); but "who are you?"
/// is the first thing a person sees after pairing, and it deserves a
/// welcome, not a settings quiz.
///
/// Styling per the owner's spec: centered; Log in as an OUTLINED button,
/// Sign up FILLED, both in the theme's accent (deep teal in light, ivory in
/// dark); the PIN arrives as a centered obscured field on the same screen.
library;

import 'package:flutter/material.dart';

import '../app_state.dart';
import '../widgets/pin_boxes.dart';

class AuthScreen extends StatefulWidget {
  const AuthScreen({super.key, required this.appState});

  final AppState appState;

  @override
  State<AuthScreen> createState() => _AuthScreenState();
}

class _AuthScreenState extends State<AuthScreen> {
  // The free-typed field and its Unlock button are gone: the PIN arrives
  // through PinBoxes (six cells, number pad, auto-submit).
  bool _revealPin = false;

  /// The question the eye was toggled on. A revealed PIN must not stay
  /// revealed for the next PIN question (a retry after a wrong one).
  int? _lastSerial;

  @override
  Widget build(BuildContext context) {
    final s = widget.appState;
    return ListenableBuilder(
      listenable: s,
      builder: (context, _) {
        final choice = s.pendingChoice;
        final prompt = s.pendingPrompt;
        if (s.questionSerial != _lastSerial) {
          _lastSerial = s.questionSerial;
          _revealPin = false;
        }

        Widget body;
        if (choice != null && choice.text == 'Who is holding this phone?') {
          body = _fork(s);
        } else if (choice != null && choice.text == 'Who are you?') {
          body = _whoAreYou(s, choice);
        } else if (prompt != null) {
          body = _pin(s, prompt.text);
        } else {
          // Between questions: the hub is checking a PIN or finishing the
          // bind. _releaseHeldQuestion pops this route the moment the flow
          // moves on, so this state is seconds long.
          body = Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              SizedBox(
                width: 22,
                height: 22,
                child:
                    CircularProgressIndicator(strokeWidth: 2, color: s.accent),
              ),
              const SizedBox(height: 16),
              Text(s.status,
                  textAlign: TextAlign.center,
                  style: TextStyle(color: s.inkDim, fontSize: 13)),
            ],
          );
        }

        return PopScope(
          // System back pops this route WITHOUT sendCancel -- tell the hub,
          // exactly as ChoiceScreen does, or it waits on the login answer
          // for the whole prompt window with nothing on screen.
          onPopInvokedWithResult: (didPop, _) {
            if (didPop) s.onQuestionRouteDismissed();
          },
          child: Scaffold(
          backgroundColor: s.bg,
          body: SafeArea(
            child: Center(
              // SCROLLS: "Who are you?" is one button per person, and a
              // household longer than the screen overflowed it.
              child: SingleChildScrollView(
                child: ConstrainedBox(
                  constraints: const BoxConstraints(maxWidth: 360),
                  child: Padding(
                    padding: const EdgeInsets.all(28),
                    child: body,
                  ),
                ),
              ),
            ),
          ),
          ),
        );
      },
    );
  }

  /// Log in / I'm new -- the fork.
  Widget _fork(AppState s) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text('Welcome',
            textAlign: TextAlign.center,
            style: TextStyle(
                color: s.ink, fontSize: 26, fontWeight: FontWeight.w600)),
        const SizedBox(height: 8),
        Text('This phone is paired. Who is holding it?',
            textAlign: TextAlign.center,
            style: TextStyle(color: s.inkDim, fontSize: 14)),
        const SizedBox(height: 36),
        OutlinedButton(
          style: OutlinedButton.styleFrom(
            side: BorderSide(color: s.accent, width: 1.4),
            foregroundColor: s.accent,
            padding: const EdgeInsets.symmetric(vertical: 16),
            shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(10)),
          ),
          onPressed: () => s.sendChoiceResponse(['login']),
          child: const Text('Log in',
              style: TextStyle(fontSize: 16, fontWeight: FontWeight.w600)),
        ),
        const SizedBox(height: 14),
        FilledButton(
          style: FilledButton.styleFrom(
            backgroundColor: s.accent,
            foregroundColor: s.accentFg,
            padding: const EdgeInsets.symmetric(vertical: 16),
            shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(10)),
          ),
          onPressed: () => s.sendChoiceResponse(['signup']),
          child: const Text("I'm new",
              style: TextStyle(fontSize: 16, fontWeight: FontWeight.w600)),
        ),
        const SizedBox(height: 20),
        Text(
          'Log in: your PIN proves it\'s you -- your voice already works. '
          'New: record a few phrases and set a PIN.',
          textAlign: TextAlign.center,
          style: TextStyle(color: s.inkFaint, fontSize: 12),
        ),
        // A PIN verdict that ended the last login round ("Too many wrong
        // PINs…", "PIN entry is locked…") stays visible when the fork is
        // shown again -- ONLY warnings, so ordinary boot chatter never
        // gatecrashes the welcome.
        if (_isPinWarning((s.lastFlowMessage ?? '').split('\n').last))
          _hubNote(s),
      ],
    );
  }

  /// Several people live here: whose phone is this?
  Widget _whoAreYou(AppState s, ChoiceFrame choice) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text('Who are you?',
            textAlign: TextAlign.center,
            style: TextStyle(
                color: s.ink, fontSize: 22, fontWeight: FontWeight.w600)),
        const SizedBox(height: 28),
        for (final option in choice.options) ...[
          OutlinedButton(
            style: OutlinedButton.styleFrom(
              side: BorderSide(color: s.accent, width: 1.2),
              foregroundColor: s.ink,
              padding: const EdgeInsets.symmetric(vertical: 14),
              shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(10)),
            ),
            onPressed: () => s.sendChoiceResponse([option.value]),
            child: Text(option.label ?? option.value,
                style: const TextStyle(fontSize: 15)),
          ),
          const SizedBox(height: 10),
        ],
      ],
    );
  }

  /// True when the hub's line is a PIN complaint that deserves amber, not
  /// dim grey: "Incorrect PIN.", "PIN entry is locked…", "A PIN is 4 to 6
  /// digits…", "Too many wrong PINs…".
  static bool _isPinWarning(String line) =>
      line.contains('Incorrect PIN') ||
      line.contains('locked') ||
      line.contains('digits') ||
      line.contains('Too many wrong');

  /// The hub's own words for the last thing that went wrong, rendered WITH
  /// the question. The hub already said "Incorrect PIN -- try again." on a
  /// wrong entry, but this screen ignored the flow banner every other
  /// question screen shows -- so the field simply reappeared, blank and
  /// unexplained, which reads as a glitch rather than a verdict.
  /// _flowLines clears when a reply is sent, so this is empty on the first
  /// ask and carries exactly the verdict on every retry.
  Widget _hubNote(AppState s) {
    final note = (s.lastFlowMessage ?? '').split('\n').last.trim();
    if (note.isEmpty) return const SizedBox.shrink();
    final warn = _isPinWarning(note);
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

  /// The PIN, as SIX BOXES (the owner's spec, 2026-08-26): number keyboard
  /// only, auto-submits on the sixth digit — no Unlock button to reach for.
  /// The eye toggles dots to digits for anyone unsure of a fat finger.
  Widget _pin(AppState s, String promptText) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text('Enter your PIN',
            textAlign: TextAlign.center,
            style: TextStyle(
                color: s.ink, fontSize: 22, fontWeight: FontWeight.w600)),
        _hubNote(s),
        const SizedBox(height: 28),
        PinBoxes(
          key: ValueKey(s.questionSerial),
          onComplete: (pin) => s.sendPromptReply(pin),
          fillColor: s.surface,
          borderColor: s.border,
          focusColor: s.accent,
          textColor: s.ink,
          revealed: _revealPin,
        ),
        const SizedBox(height: 14),
        Center(
          child: TextButton.icon(
            onPressed: () => setState(() => _revealPin = !_revealPin),
            icon: Icon(
                _revealPin
                    ? Icons.visibility_off_outlined
                    : Icons.visibility_outlined,
                size: 16,
                color: s.inkDim),
            label: Text(_revealPin ? 'Hide digits' : 'Show digits',
                style: TextStyle(color: s.inkDim, fontSize: 12.5)),
          ),
        ),
      ],
    );
  }
}