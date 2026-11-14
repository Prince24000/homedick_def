import 'dart:math' show max;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../app_state.dart';

class ChoiceScreen extends StatefulWidget {
  final AppState appState;
  const ChoiceScreen({super.key, required this.appState});

  @override
  State<ChoiceScreen> createState() => _ChoiceScreenState();
}

class _ChoiceScreenState extends State<ChoiceScreen> {
  final Set<String> _selected = {};
  bool _didSchedulePop = false;

  // In-list search (2026-08-26): a long list — HA entities, devices to
  // remove — gets a filter box, same word-AND matching as the pin map's
  // device sheet. Short lists stay clean; the box only appears past
  // _searchThreshold options.
  static const int _searchThreshold = 8;
  final _searchCtl = TextEditingController();
  String _query = '';

  /// How many of the hub's latest lines the Working view shows.
  static const int _collapsedLines = 2;

  // Fresh question, fresh state: a new choice must not inherit the last
  // one's ticks or its filter text. (Stale ticks were harmless on the wire
  // — the hub drops values it never offered — but a checkmark from the
  // previous question is still a lie on screen.) Keyed on the hub's
  // question SERIAL, not the wording: two identical questions in a row
  // are still two questions.
  int? _lastSerial;

  /// The hub's "show me the whole list again" row on a narrowed question
  /// (WebSocketIO._CHOICE_NONE -- the leading space is part of it). Not an
  /// answer: sent beside real picks, the hub throws the picks away and
  /// re-asks. So it is never ticked into a multi-select, never swept up by
  /// "All of them", and a tap on it is sent at once, alone.
  static const String _noneValue = ' __none__';

  @override
  void initState() {
    super.initState();
    widget.appState.interceptTranscript = true;
    widget.appState.addListener(_onAppState);
  }

  @override
  void dispose() {
    widget.appState.interceptTranscript = false;
    widget.appState.removeListener(_onAppState);
    _searchCtl.dispose();
    super.dispose();
  }

  void _onAppState() {
    if (!mounted) return;
    // A SPOKEN answer is not merged here any more: the words go to the hub
    // (AppState.sendChoiceHeard) with the ticks reported in _toggle riding
    // beside them, and the hub sends back the answer or a narrower
    // question.
    //
    // Rebuild on every notification: a cleared pendingChoice (result/error
    // arrived) must reach the pop-guard in build(), and the heard-line
    // updates as the matcher reports.
    setState(() {});
    // Voice MATCHING lives in VoiceFlow now — it is the only place a choice
    // transcript actually arrives. The matcher that used to live here ran
    // off AppState.lastTranscript, which only ever carries COMMAND
    // transcripts, so it never fired: dead code pretending to be the
    // fuzzy matching. Anything that did land here is dropped so it cannot
    // leak into the next question.
    if (widget.appState.lastTranscript != null) {
      widget.appState.clearTranscript();
    }
  }

  // Matching a SPOKEN answer is not done on this phone at all: the words
  // go to the hub (nlu/choice_match.py) and an answer comes back. Keeping
  // a second, divergent matcher here is how "say the one you want" came
  // to behave differently from what the voice actually did.
  //
  // The filter below is a different job -- it narrows a long list as you
  // TYPE, where every keystroke must redraw instantly and there is no
  // guessing involved.

  /// Word-AND filter over label, value and sub — "kitchen lam" finds
  /// "Kitchen lamp". Empty query means everything.
  List<ChoiceOption> _visible(ChoiceFrame choice) {
    final q = _query.trim().toLowerCase();
    if (q.isEmpty) return choice.options;
    final words = q.split(RegExp(r'\s+')).where((w) => w.isNotEmpty);
    return choice.options.where((o) {
      final hay =
          '${o.label ?? ''} ${o.value} ${o.sub ?? ''}'.toLowerCase();
      return words.every(hay.contains);
    }).toList();
  }

  // ── Build ──────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    final s = widget.appState;
    final choice = s.pendingChoice;
    if (choice == null) {
      // MID-FLOW HOLD, same rule as PromptScreen: while the flow is still
      // running, show the working spinner here instead of flashing home
      // between a picked option and the next question. A prompt arriving
      // during the hold REPLACES this route (see _handlePrompt). A spoken
      // answer holds it too, whatever flow asked: the hub is matching the
      // words, and a "did you mean…?" lands in this same route.
      if ((s.menuBusy || s.choiceAwaitingHub) && s.isReady) {
        _didSchedulePop = false;
        return _buildWorking(context, s);
      }
      if (!_didSchedulePop) {
        _didSchedulePop = true;
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (!mounted) return;
          final route = ModalRoute.of(context);
          // Same double-pop guard as PromptScreen: this route stays mounted
          // through its exit animation after sendCancel/sendChoiceResponse
          // popped it, and an unconditional pop() here removed the HOME
          // route — the blank black screen. Mid-exit routes aren't current.
          if (route != null &&
              route.isCurrent &&
              widget.appState.pendingChoice == null) {
            Navigator.of(context).pop();
          } else {
            _didSchedulePop = false;
          }
        });
      }
      return const SizedBox.shrink();
    }

    // A NEW question resets the ticks and the filter, before anything is
    // drawn from them.
    if (_lastSerial != s.questionSerial) {
      _lastSerial = s.questionSerial;
      _selected.clear();
      s.noteChoiceTicks(const <String>[]);
      _query = '';
      _searchCtl.clear();
    }

    final visible = _visible(choice);
    final filtering = _query.trim().isNotEmpty;
    final showSearch = choice.options.length > _searchThreshold;
    // "All of them" hides while a filter is active — "all" of a filtered
    // view is ambiguous, and the full row returns the moment the box
    // clears.
    final showAllRow =
        choice.allowAll && !filtering && choice.options.length > 1;

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
      child: PopScope(
        // System back pops this route WITHOUT sendCancel — tell the server,
        // or it waits on a choice_response forever and everything after is
        // refused "busy".
        onPopInvokedWithResult: (didPop, _) {
          if (didPop) s.onQuestionRouteDismissed();
        },
        child: Scaffold(
        backgroundColor: s.bg,
        appBar: AppBar(
          backgroundColor: s.bg,
          elevation: 0,
          leading: IconButton(
            // BACK STEPS OUT, X BAILS OUT (the navigation law,
            // 2026-08-26). This arrow used to call sendCancel() — the
            // eject seat — so "back" cancelled the whole flow AND jumped
            // home. A pop is enough: PopScope tells the hub, _backCancel
            // keeps the ending quiet, and the person lands on the LAST
            // screen. The Cancel button below remains the loud exit.
            icon: Icon(Icons.arrow_back, color: s.ink),
            onPressed: () => Navigator.of(context).maybePop(),
          ),
          // NO TITLE: the question sits in the grey box below.
        ),
        body: SafeArea(
          child: Padding(
            // Bottom 72: the voice dock floats over this screen at ~14px
            // from the edge, and with a full option list the Done/Cancel
            // buttons sit exactly under it. Reserve the dock's lane.
            padding: const EdgeInsets.fromLTRB(16, 16, 16, 72),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                // ONLY THE QUESTION, IN THE GREY BOX (her design,
                // 2026-10-11): "I heard 'lump ar bob'. Did you mean bulb or
                // lamp?" -- smaller, regular weight, italic. The hub's
                // running lines and the separate "heard" box are gone from
                // here; the question already says what was heard.
                Container(
                  margin: const EdgeInsets.only(bottom: 16),
                  padding: const EdgeInsets.fromLTRB(16, 14, 16, 14),
                  decoration: BoxDecoration(
                    color: s.surface,
                    border: Border.all(color: s.border),
                    borderRadius: BorderRadius.circular(14),
                  ),
                  child: Text(
                    choice.text,
                    style: TextStyle(
                        color: s.ink.withValues(alpha: 0.85),
                        fontSize: 14,
                        fontStyle: FontStyle.italic,
                        height: 1.45),
                  ),
                ),

                if (showSearch) ...[
                  TextField(
                    controller: _searchCtl,
                    autocorrect: false,
                    enableIMEPersonalizedLearning: false,
                    onChanged: (v) => setState(() => _query = v),
                    style: TextStyle(color: s.ink, fontSize: 14),
                    decoration: InputDecoration(
                      isDense: true,
                      hintText: 'Search…',
                      hintStyle: TextStyle(color: s.inkFaint),
                      prefixIcon:
                          Icon(Icons.search, size: 18, color: s.inkDim),
                      suffixIcon: _query.isEmpty
                          ? null
                          : IconButton(
                              icon: Icon(Icons.close,
                                  size: 16, color: s.inkDim),
                              onPressed: () => setState(() {
                                _query = '';
                                _searchCtl.clear();
                              }),
                            ),
                      filled: true,
                      fillColor: s.surface,
                      contentPadding: const EdgeInsets.symmetric(
                          horizontal: 12, vertical: 10),
                      border: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(8),
                          borderSide: BorderSide(color: s.border)),
                      enabledBorder: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(8),
                          borderSide: BorderSide(color: s.border)),
                      focusedBorder: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(8),
                          borderSide: BorderSide(color: s.accent)),
                    ),
                  ),
                  const SizedBox(height: 10),
                ],

                Expanded(
                  child: visible.isEmpty
                      ? Center(
                          child: Text(
                            'Nothing matches "${_query.trim()}"',
                            style:
                                TextStyle(color: s.inkFaint, fontSize: 13),
                          ),
                        )
                      : ListView.builder(
                          itemCount: visible.length + (showAllRow ? 1 : 0),
                          itemBuilder: (context, index) {
                            if (showAllRow && index == visible.length) {
                              return _buildOption(
                                  '__all__', 'All of them', null, true);
                            }
                            final opt = visible[index];
                            // FIX: coerce nulls so _buildOption gets
                            // non-null Strings
                            return _buildOption(
                              opt.value,
                              opt.label ?? opt.value,
                              opt.sub,
                              false,
                            );
                          },
                        ),
                ),
                const SizedBox(height: 12),
                if (choice.multi)
                  FilledButton(
                    style: FilledButton.styleFrom(
                      backgroundColor: s.accent,
                      foregroundColor: s.accentFg,
                      padding: const EdgeInsets.symmetric(vertical: 14),
                      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                    ),
                    onPressed: _selected.isEmpty ? null : () => s.sendChoiceResponse([..._selected]),
                    child: const Text('Done', style: TextStyle(fontWeight: FontWeight.w700, fontSize: 15)),
                  ),
                const SizedBox(height: 8),
                OutlinedButton(
                  // The DISCONNECT dress: every cancel wears the
                  // disconnect button's red/burgundy (her rule, 2026-08-27).
                  style: s.cancelButtonStyle(),
                  onPressed: () => s.sendCancel(),
                  // WHAT CANCEL COSTS, in the hub's own words when it has
                  // any. On a destructive question "Cancel" alone says
                  // nothing; "Cancel -- leave the chip as 'kitchen'" names
                  // what survives, which is half the decision.
                  child: Text(
                    choice.cancelLabel.isEmpty ? 'Cancel' : choice.cancelLabel,
                    textAlign: TextAlign.center,
                    style: const TextStyle(
                        fontWeight: FontWeight.w600, fontSize: 15),
                  ),
                ),
              ],
            ),
          ),
        ),
        ),
      ),
    );
  }

  Widget _buildOption(String value, String label, String? sub, bool isAll) {
    final s = widget.appState;
    final choice = s.pendingChoice!;
    final isSelected = isAll
        ? _allValues(choice).every(_selected.contains)
        : _selected.contains(value);

    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: OutlinedButton(
        style: OutlinedButton.styleFrom(
          // THE THEME'S OWN ACCENT for a selected row (deep teal light /
          // ivory dark) — the hardcoded green pair (#12331f/#3FB950) was
          // the off-theme colour the owner retired on 2026-08-26.
          backgroundColor: isSelected
              ? s.accent.withValues(alpha: 0.10)
              : Colors.transparent,
          foregroundColor: s.ink,
          side: BorderSide(
              color: isSelected ? s.accent : s.border,
              width: isSelected ? 1.4 : 1),
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 14),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
        ),
        onPressed: () => _toggle(value, isAll),
        child: Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(label, style: TextStyle(fontSize: 15, fontWeight: FontWeight.w500, color: s.ink)),
                  if (sub != null)
                    Text(sub, style: TextStyle(fontSize: 12, color: s.inkFaint)),
                ],
              ),
            ),
            if (isSelected)
              Icon(Icons.check, color: s.accent, size: 20),
          ],
        ),
      ),
    );
  }

  /// Between-questions view, identical in spirit to PromptScreen's: the
  /// flow is grinding and the next question hasn't arrived — spinner and
  /// progress banner on THIS screen, no bounce to home.
  Widget _buildWorking(BuildContext context, AppState s) {
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
              // Same law as the Question screen: back steps AWAY — the
              // work carries on and re-summons its screen at the next
              // question; only X/Cancel aborts.
              icon: Icon(Icons.arrow_back, color: s.ink),
              onPressed: () => Navigator.of(context).maybePop(),
            ),
            title: Text('Working…',
                style: TextStyle(
                    color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
          ),
          body: SafeArea(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(16, 16, 16, 72),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  // The last couple of lines only. This is a WAITING panel,
                  // not a log: the Working screen keeps the full account,
                  // and eight lines here just pushes the spinner off.
                  if (s.flowLines.isNotEmpty)
                    Container(
                      margin: const EdgeInsets.only(bottom: 14),
                      padding: const EdgeInsets.fromLTRB(12, 10, 12, 10),
                      decoration: BoxDecoration(
                        color: s.surface,
                        border: Border(
                            left: BorderSide(color: s.inkFaint, width: 3)),
                      ),
                      child: Text(
                        s.flowLines
                            .sublist(max(0,
                                s.flowLines.length - _collapsedLines))
                            .join('\n'),
                        style: TextStyle(
                            color: s.inkDim, fontSize: 13, height: 1.45),
                      ),
                    ),
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
                      style: TextStyle(color: s.inkDim, fontSize: 13.5),
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

  /// Every real answer on the list -- the "none of those" row excluded.
  static List<String> _allValues(ChoiceFrame choice) => [
        for (final o in choice.options)
          if (o.value != _noneValue) o.value
      ];

  void _toggle(String value, bool isAll) {
    final choice = widget.appState.pendingChoice!;
    if (!isAll && value == _noneValue) {
      widget.appState.sendChoiceResponse([value]);
      return;
    }
    setState(() {
      if (isAll) {
        final all = _allValues(choice);
        if (all.every(_selected.contains)) {
          _selected.clear();
        } else {
          _selected.addAll(all);
        }
      } else if (choice.multi) {
        if (_selected.contains(value)) {
          _selected.remove(value);
        } else {
          _selected.add(value);
        }
      } else {
        widget.appState.sendChoiceResponse([value]);
      }
    });
    // The ticks travel with a SPOKEN answer ("and the porch"), so the
    // app has to know them, not only this screen.
    if (choice.multi) widget.appState.noteChoiceTicks(_selected);
  }
}