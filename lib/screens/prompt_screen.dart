import 'dart:math' show max;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../app_state.dart';
import '../widgets/busy_card.dart';
import '../widgets/pin_boxes.dart';

/// The Question screen, in three shapes chosen from the question itself:
///
///  * RITUAL — the answer is fully covered by buttons (Yes/No, On/Off…):
///    question + buttons, nothing else. No text box, no Reply.
///  * WIZARD — a multi-field update flow: current value + a text box
///    (empty = keep) + big Next / Back / Cancel, with Next becoming Finish
///    on the last field.
///  * FREE — anything genuinely typed (PIN, names, values): text box +
///    Submit/Next.
class PromptScreen extends StatefulWidget {
  final AppState appState;
  const PromptScreen({super.key, required this.appState});

  @override
  State<PromptScreen> createState() => _PromptScreenState();
}

enum _Kind { ritual, wizard, free }

class _PromptScreenState extends State<PromptScreen> {
  final _ctl = TextEditingController();

  /// Whether a masked field is currently being shown. Per-screen, and reset
  /// whenever the question changes so one revealed password does not leave
  /// the next one exposed.
  bool _reveal = false;
  bool _didSchedulePop = false;

  /// The hub's serial for the question this screen's state belongs to.
  /// Keyed on the SERIAL, not the text: a question re-asked with the same
  /// wording (a refused password) used to keep the last answer in the box.
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

  // THE AI REVIEW CARD IS GONE (2026-09-23). It answered a "Use both?"
  // prompt (meta.ai_review) that no hub flow asks any more: the AI's
  // proposal is drawn beside the person's own on the registration screen,
  // and the one "Use it? [Y/n]" left (scene descriptions) is a plain
  // yes/no. The card, its inline editors and the scripted edit chain it
  // drove were unreachable.
  _Kind _classify(AppState s, String text) {
    final lower = text.toLowerCase();
    final meta = s.pendingPrompt?.meta;
    if ((meta != null && meta['wizard'] is Map) ||
        lower.contains('enter=keep')) {
      return _Kind.wizard;
    }
    if (quickReplies(text).isNotEmpty) return _Kind.ritual;
    return _Kind.free;
  }

  Widget _build(BuildContext context) {
    final s = widget.appState;
    final prompt = s.pendingPrompt;

    if (prompt == null) {
      // MID-FLOW HOLD: the flow is still running (AI generating, HA being
      // read) — stay HERE with a spinner and the progress banner instead of
      // bouncing to home and back for every gap between questions.
      if (s.menuBusy && s.isReady) {
        _didSchedulePop = false;
        return _buildWorking(context, s);
      }
      if (!_didSchedulePop) {
        _didSchedulePop = true;
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (!mounted) return;
          final route = ModalRoute.of(context);
          // isCurrent is the DOUBLE-POP guard: a route mid-exit is not
          // current; only a live, on-top, questionless route may
          // self-destruct — popping otherwise removed the HOME route.
          if (route != null &&
              route.isCurrent &&
              widget.appState.pendingPrompt == null) {
            Navigator.of(context).pop();
          } else {
            _didSchedulePop = false;
          }
        });
      }
      return const SizedBox.shrink();
    }
    _didSchedulePop = false;

    // One-time per question: reset local state so nothing leaks between
    // questions.
    if (_lastSerial != s.questionSerial) {
      _lastSerial = s.questionSerial;
      // A new question starts hidden again -- revealing one password must not
      // leave the next one on screen.
      _reveal = false;
      _ctl.clear();
    }

    final kind = _classify(s, prompt.text);

    return AnnotatedRegion<SystemUiOverlayStyle>(
      value: _overlayStyle(s),
      child: PopScope(
        // THE GESTURE DOES WHAT THE BUTTON DOES (2026-09-12).
        //
        // On a wizard step past the first, this screen already draws a
        // Back button that sends `prev` and steps the hub back a field.
        // The system gesture cancelled the whole flow instead — two
        // gestures that read as the same intention doing opposite things,
        // the same disagreement as the Cancel button that sent nothing.
        //
        // So when the hub has said this is a wizard step (`meta.wizard`,
        // attached only by flows that can actually rewind), the gesture is
        // INTERCEPTED rather than allowed: the route stays, `prev` goes
        // up, and the previous field swaps into this same screen — which
        // is exactly what pressing the button does. Everywhere else the
        // pop happens as before and the hub is told the flow is over,
        // because a straight-line flow has no previous step to return to.
        canPop: !s.canStepBack,
        onPopInvokedWithResult: (didPop, _) {
          if (didPop) {
            s.onQuestionRouteDismissed();
          } else {
            // Not a second path: literally the button's own call.
            s.sendPromptReply('prev');
          }
        },
        child: Scaffold(
          backgroundColor: s.bg,
          appBar: AppBar(
            backgroundColor: s.bg,
            elevation: 0,
            leading: IconButton(
              // BACK STEPS OUT, X BAILS OUT. This arrow used to call
              // sendCancel() -- the eject seat -- so "back" cancelled the
              // whole flow AND jumped home ("still wrong in the
              // navigation", field test 2026-08-26). A pop is enough:
              // PopScope tells the hub, _backCancel keeps the ending
              // quiet, and the person lands on the LAST screen.
              icon: Icon(Icons.arrow_back, color: s.ink),
              onPressed: () => Navigator.of(context).maybePop(),
            ),
            title: Text('Question',
                style: TextStyle(
                    color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
          ),
          body: SafeArea(
            child: SingleChildScrollView(
              // Bottom 72 clears the floating voice dock — a long question
              // with buttons scrolled its last control under the pill.
              padding: const EdgeInsets.fromLTRB(16, 16, 16, 72),
              child: switch (kind) {
                _Kind.ritual => _buildRitual(s, prompt.text),
                _Kind.wizard => _buildWizard(s, prompt),
                _Kind.free => _buildFree(s, prompt.text),
              },
            ),
          ),
        ),
      ),
    );
  }

  SystemUiOverlayStyle _overlayStyle(AppState s) => s.isLight
      ? SystemUiOverlayStyle.dark.copyWith(
          statusBarColor: Colors.transparent,
          systemNavigationBarColor: s.bg,
          systemNavigationBarIconBrightness: Brightness.dark,
        )
      : SystemUiOverlayStyle.light.copyWith(
          statusBarColor: Colors.transparent,
          systemNavigationBarColor: s.bg,
          systemNavigationBarIconBrightness: Brightness.light,
        );

  /// Is the hub's chatter expanded on this screen?
  ///
  /// THE SAME WALL THE CHOOSE SCREEN HAD. lastFlowMessage is the join of up
  /// to EIGHT flow lines, and rendering all of them above the question is
  /// what pushed the answers off an 844px screen. It matters at least as
  /// much here: this is the screen that holds the describe fields and the
  /// PIN boxes, and a field you cannot see is a field you cannot fill.
  bool _showAllLines = false;
  static const int _collapsedLines = 2;

  Widget _banner(AppState s) {
    final lines = s.flowLines;
    if (lines.isEmpty) return const SizedBox.shrink();
    final shown = _showAllLines
        ? lines
        : lines.sublist(max(0, lines.length - _collapsedLines));
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Container(
          padding: const EdgeInsets.fromLTRB(12, 10, 12, 10),
          decoration: BoxDecoration(
            color: s.surface,
            border: Border(left: BorderSide(color: s.inkFaint, width: 3)),
          ),
          child: Text(
            shown.join('\n'),
            style: TextStyle(color: s.inkDim, fontSize: 13, height: 1.45),
          ),
        ),
        if (lines.length > _collapsedLines)
          TextButton.icon(
            onPressed: () => setState(() => _showAllLines = !_showAllLines),
            icon: Icon(
                _showAllLines
                    ? Icons.keyboard_arrow_up
                    : Icons.keyboard_arrow_down,
                size: 17,
                color: s.accent),
            label: Text(
              _showAllLines ? 'Show less' : 'Show all ${lines.length} lines',
              style: TextStyle(color: s.accent, fontSize: 12.5),
            ),
            style: TextButton.styleFrom(
              padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 0),
              minimumSize: const Size(0, 30),
              tapTargetSize: MaterialTapTargetSize.shrinkWrap,
            ),
          ),
        const SizedBox(height: 12),
      ],
    );
  }

  Widget _question(AppState s, String text) => Padding(
        padding: const EdgeInsets.only(bottom: 16),
        child: Text(text,
            style: TextStyle(color: s.ink, fontSize: 16, height: 1.4)),
      );

  Widget _bigFilled(AppState s, String label, VoidCallback? onTap) =>
      FilledButton(
        style: FilledButton.styleFrom(
          backgroundColor: s.accent,
          foregroundColor: s.accentFg,
          disabledBackgroundColor: s.accent.withValues(alpha: 0.4),
          padding: const EdgeInsets.symmetric(vertical: 14),
          shape:
              RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        ),
        onPressed: onTap,
        child: Text(label,
            style:
                const TextStyle(fontWeight: FontWeight.w700, fontSize: 15)),
      );

  Widget _bigOutlined(AppState s, String label, VoidCallback onTap,
          {bool accented = false}) =>
      OutlinedButton(
        style: OutlinedButton.styleFrom(
          foregroundColor: accented ? s.accent : s.inkDim,
          side: BorderSide(
              color: accented ? s.accent : s.border,
              width: accented ? 1.2 : 1),
          padding: const EdgeInsets.symmetric(vertical: 14),
          shape:
              RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        ),
        onPressed: onTap,
        child: Text(label,
            style:
                const TextStyle(fontWeight: FontWeight.w600, fontSize: 15)),
      );

  Widget _cancelButton(AppState s) => OutlinedButton(
        // The DISCONNECT dress, not the grey outline: every cancel wears
        // the disconnect button's red/burgundy (her rule, 2026-08-27).
        style: s.cancelButtonStyle(),
        onPressed: () => widget.appState.sendCancel(),
        child: const Text('Cancel',
            style: TextStyle(fontWeight: FontWeight.w600, fontSize: 15)),
      );

  // ── RITUAL: buttons ARE the answer ─────────────────────────────────

  Widget _buildRitual(AppState s, String rawText) {
    final chips = quickReplies(rawText);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _banner(s),
        _question(s, displayText(rawText)),
        Wrap(
          spacing: 10,
          runSpacing: 10,
          children: [
            for (final chip in chips)
              OutlinedButton(
                style: OutlinedButton.styleFrom(
                  foregroundColor: s.accent,
                  side: BorderSide(color: s.accent, width: 1.2),
                  padding: const EdgeInsets.symmetric(
                      horizontal: 26, vertical: 13),
                  shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(24)),
                ),
                onPressed: () => s.sendPromptReply(chip.value),
                child: Text(chip.key,
                    style: const TextStyle(
                        fontSize: 15, fontWeight: FontWeight.w600)),
              ),
          ],
        ),
        const SizedBox(height: 24),
        _cancelButton(s),
      ],
    );
  }

  // ── WIZARD: field walker with Next / Back / Cancel ─────────────────

  Widget _buildWizard(AppState s, PromptFrame prompt) {
    // Greedy label + non-bracket current value, so a label that itself
    // carries a bracket ("Security-critical? [y/n]") parses correctly:
    // label = everything up to the LAST [current] before "(Enter=keep".
    final m = RegExp(r'^(.*)\s*\[([^\]]*)\]\s*\(enter=keep',
            caseSensitive: false)
        .firstMatch(prompt.text.trim());
    final rawLabel = (m?.group(1) ?? displayText(prompt.text)).trim();
    final current = (m?.group(2) ?? '').trim();

    // A wizard field whose value is itself a two-way choice gets buttons,
    // not a text box.
    final lower = rawLabel.toLowerCase();
    final List<MapEntry<String, String>> valueChips =
        lower.contains('[y/n]')
            ? const [MapEntry('Yes', 'y'), MapEntry('No', 'n')]
            : lower.contains('on or off')
                ? const [MapEntry('On', 'on'), MapEntry('Off', 'off')]
                : const [];
    final label = displayText(rawLabel);

    final wizard = prompt.meta?['wizard'];
    final isLast = (wizard is Map && wizard['last'] == true) ||
        label.toLowerCase().contains('current value');
    final index = (wizard is Map) ? wizard['index'] : null;
    final total = (wizard is Map) ? wizard['total'] : null;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _banner(s),
        if (index != null && total != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 6),
            child: Text('Step $index of $total',
                style: TextStyle(
                    color: s.inkFaint,
                    fontSize: 12,
                    fontWeight: FontWeight.w600)),
          ),
        Text(label,
            style: TextStyle(
                color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
        if (current.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Text('Current: $current',
                style: TextStyle(color: s.inkDim, fontSize: 13.5)),
          ),
        const SizedBox(height: 16),
        if (valueChips.isNotEmpty)
          Wrap(
            spacing: 10,
            runSpacing: 10,
            children: [
              for (final chip in valueChips)
                OutlinedButton(
                  style: OutlinedButton.styleFrom(
                    foregroundColor: s.accent,
                    side: BorderSide(color: s.accent, width: 1.2),
                    padding: const EdgeInsets.symmetric(
                        horizontal: 26, vertical: 13),
                    shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(24)),
                  ),
                  onPressed: () => s.sendPromptReply(chip.value),
                  child: Text(chip.key,
                      style: const TextStyle(
                          fontSize: 15, fontWeight: FontWeight.w600)),
                ),
            ],
          )
        else
          TextField(
            controller: _ctl,
            autocorrect: false,
            enableIMEPersonalizedLearning: false,
            style: TextStyle(color: s.ink),
            decoration: InputDecoration(
              labelText: 'New value — leave empty to keep',
              labelStyle: TextStyle(color: s.inkDim, fontSize: 13.5),
              filled: true,
              fillColor: s.surface,
              border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(8),
                  borderSide: BorderSide(color: s.border)),
              focusedBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(8),
                  borderSide: BorderSide(color: s.accent)),
            ),
            onSubmitted: (_) => s.sendPromptReply(_ctl.text.trim()),
          ),
        const SizedBox(height: 18),
        _bigFilled(
            s,
            isLast
                ? 'Finish'
                : (valueChips.isNotEmpty ? 'Next (keep current)' : 'Next'),
            () => s.sendPromptReply(
                valueChips.isNotEmpty ? '' : _ctl.text.trim())),
        // No Back on the FIRST step: the hub answers `prev` there by asking
        // the same field again (see AppState.canStepBack).
        if (index is! int || index > 1) ...[
          const SizedBox(height: 8),
          _bigOutlined(s, 'Back', () => s.sendPromptReply('prev'),
              accented: true),
        ],
        const SizedBox(height: 8),
        _cancelButton(s),
      ],
    );
  }

  // ── FREE: genuinely typed answers ──────────────────────────────────

  Widget _buildFree(AppState s, String rawText) {
    // The SERVER says whether this is a secret. It used to be guessed here
    // with `rawText.contains('pin')`, which masked GPIO pin numbers ("Data
    // pin", "GPIO pin") while leaving the ESPHome API key in plain view --
    // wrong in both directions from one heuristic.
    final isSecret = s.pendingPrompt?.secret == true;
    // THE SECURITY PIN GETS SIX BOXES (the owner's spec, 2026-08-26):
    // secret AND the question names the PIN -> the six-cell number-pad
    // entry that submits itself on the last digit. Other secrets (Wi-Fi
    // password, API key) keep the free field below -- those are not six
    // digits and need a text keyboard. GPIO "pin" questions are not
    // secret, so they fall through to the plain numeric field unharmed.
    final isPinEntry = isSecret && rawText.toUpperCase().contains('PIN');
    if (isPinEntry) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          _banner(s),
          _question(s, rawText),
          const SizedBox(height: 6),
          PinBoxes(
            // One box set per QUESTION: a re-asked PIN (wrong, or re-sent
            // after a timeout) must not inherit half-typed digits.
            key: ValueKey(s.questionSerial),
            onComplete: (pin) => s.sendPromptReply(pin),
            fillColor: s.surface,
            borderColor: s.border,
            focusColor: s.accent,
            textColor: s.ink,
            revealed: _reveal,
          ),
          const SizedBox(height: 10),
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
          const SizedBox(height: 10),
          _cancelButton(s),
        ],
      );
    }
    // A NUMBER pad for a GPIO "pin" question only. A secret that reaches
    // here is NOT the PIN (that took the boxes above) -- a Wi-Fi password,
    // an API key -- and those need letters; the number pad used to be
    // forced on every secret, so a password could only be typed by
    // revealing it. Suggestions and autocorrect stay off whether or not
    // the eye is open: nothing typed here is prose.
    final numeric = !isSecret && rawText.toLowerCase().contains('pin');
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _banner(s),
        _question(s, rawText),
        TextField(
          controller: _ctl,
          obscureText: isSecret && !_reveal,
          autocorrect: false,
          enableSuggestions: !isSecret,
          enableIMEPersonalizedLearning: false,
          keyboardType:
              numeric ? TextInputType.number : TextInputType.text,
          style: TextStyle(color: s.ink),
          decoration: InputDecoration(
            labelText: 'Answer',
            labelStyle: TextStyle(color: s.inkDim),
            filled: true,
            fillColor: s.surface,
            // The eye. Masking without a way to look is how a mistyped
            // Wi-Fi password becomes a board that silently never joins.
            suffixIcon: isSecret
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
            border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(8),
                borderSide: BorderSide(color: s.border)),
            focusedBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(8),
                borderSide: BorderSide(color: s.accent)),
          ),
          onSubmitted: (_) => _sendTyped(),
        ),
        const SizedBox(height: 16),
        _bigFilled(s, isSecret ? 'Submit' : 'Next', _sendTyped),
        const SizedBox(height: 8),
        _cancelButton(s),
      ],
    );
  }

  void _sendTyped() {
    final text = _ctl.text.trim();
    // AN EMPTY ANSWER IS AN ANSWER to an ordinary box: the hub asks plenty
    // of questions whose empty reply means skip or keep ("(optional)",
    // "Enter to skip", "Enter keeps: ...", "[device_id]", "or nothing to
    // finish"), and refusing it here left Cancel -- which abandons the
    // whole flow -- as the only way past them. A question that needs a
    // value re-asks on its own (get_input). Only a SECRET is held back:
    // an empty secret is read as a cancel by the hub.
    if (text.isEmpty && widget.appState.pendingPrompt?.secret == true) {
      return;
    }
    widget.appState.sendPromptReply(text);
    // An answered secret must not sit in the box through the hold.
    _ctl.clear();
  }

  // ── Working (between questions) ────────────────────────────────────

  Widget _buildWorking(BuildContext context, AppState s) {
    // WHEN THE HUB SENT A BUSY CARD, THE CARD IS THE SCREEN. The first
    // live flash rendered as a popup floating over a scroll of esptool
    // chatter -- two layers saying the same thing, one of them in hex.
    // Now the loader stands alone: no banner, no status line, no logs.
    // (main.dart suppresses its overlay on this route for the same
    // reason -- one card, one place.)
    final busy = s.busyText != null;
    return AnnotatedRegion<SystemUiOverlayStyle>(
      value: _overlayStyle(s),
      child: PopScope(
        onPopInvokedWithResult: (didPop, _) {
          if (didPop) s.onQuestionRouteDismissed();
        },
        child: Scaffold(
          backgroundColor: s.bg,
          appBar: AppBar(
            backgroundColor: s.bg,
            elevation: 0,
            leading: IconButton(
              // Same law as the Question screen: back steps AWAY (the
              // work carries on and re-summons its screen at the next
              // question); only X aborts.
              icon: Icon(Icons.arrow_back, color: s.ink),
              onPressed: () => Navigator.of(context).maybePop(),
            ),
            title: Text('Working…',
                style: TextStyle(
                    color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
          ),
          body: SafeArea(
            child: busy
                // BARE, not boxed: this is a screen, not a popup. The
                // content sits directly on the background -- a bordered
                // box centered on a page of its own would just be the
                // popup wearing a different hat.
                ? Center(
                    child: Padding(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 36, vertical: 16),
                      child: BusyCard(appState: s, boxed: false),
                    ),
                  )
                : Padding(
                    padding: const EdgeInsets.fromLTRB(16, 16, 16, 72),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        _banner(s),
                        const Spacer(),
                        Center(
                          child: SizedBox(
                            width: 28,
                            height: 28,
                            child: CircularProgressIndicator(
                                strokeWidth: 3, color: s.accent),
                          ),
                        ),
                        const SizedBox(height: 14),
                        Center(
                          child: Text(
                            s.status,
                            textAlign: TextAlign.center,
                            style:
                                TextStyle(color: s.inkDim, fontSize: 13.5),
                          ),
                        ),
                        const Spacer(),
                      ],
                    ),
                  ),
          ),
        ),
      ),
    );
  }

  // ── Text helpers (shared patterns with VoiceFlow._stripRitual) ─────

  /// Quick-reply buttons derived from the QUESTION's own text. Server-side
  /// wording is the contract.
  static List<MapEntry<String, String>> quickReplies(String text) {
    final t = text.toLowerCase();
    // (No wizard chips here: a prompt carrying "enter=keep" -- the only
    // one that says 'prev' -- is classified as a wizard before this runs.)
    if (t.contains('on or off') || t.contains('[on/off]')) {
      return const [MapEntry('On', 'on'), MapEntry('Off', 'off')];
    }
    if (t.contains('[y/n') || t.contains('[y =') || t.contains('[y/')) {
      final chips = <MapEntry<String, String>>[
        const MapEntry('Yes', 'y'),
        const MapEntry('No', 'n'),
      ];
      if (t.contains('e = edit') || t.contains('/e]') || t.contains('/ e ')) {
        chips.add(const MapEntry('Edit', 'e'));
      }
      if (t.contains('prev')) {
        chips.add(const MapEntry('Previous', 'prev'));
      }
      return chips;
    }
    return const [];
  }

  /// The question with its typed-answer ritual stripped. A current-value
  /// bracket like "Room [kitchen]" survives — only brackets carrying
  /// alternatives (a '/' or '=') are removed.
  static String displayText(String text) {
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
    return t;
  }
}