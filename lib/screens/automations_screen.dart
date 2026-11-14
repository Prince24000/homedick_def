/// AUTOMATIONS — the rules the house follows, and the blocks that build
/// them. ONE screen (her merge, 2026-08-27), not two.
///
/// The list and the builder were separate screens for about an hour, and
/// that was one screen too many: they are the same subject seen from two
/// distances. So this screen has two faces and swaps between them —
/// there is no route change, no push, nothing to go "back" through.
///
///   * the LIST: every rule as one plain sentence, its switch, its menu,
///     and the journal of what the house has actually done — including
///     what it refused, which is the half that matters when a rule
///     misbehaves.
///   * the BLOCKS, whenever the hub hands over a rule to build (her
///     Tinkercad design: "you select the device and then do if else
///     statements"):
///
///         WHEN   [ doorway — kitchen ▾ ] [ sees motion ▾ ]
///         IF     [ time is between ▾ ] [ 19:00 ] and [ 06:00 ]
///         THEN   [ run scene ▾ ] [ night light ▾ ]
///         ELSE AFTER [ 5 ] min quiet  [ set ▾ ] [ hall lamp ▾ ] [ off ▾ ]
///
///     Stacked, not dragged: the order is fixed, so a rule cannot be
///     arranged into a shape the hub would refuse. Beneath the blocks a
///     live plain-English sentence rebuilds as they change — that
///     sentence is what she approves and what the rule is described by.
///
/// NOTHING IS COMMITTED WHILE BUILDING. The whole rule travels in one
/// frame at Save, so the WHEN can be changed after the THEN, freely. That
/// is the difference between a builder and an interrogation.
///
/// The hub remains the authority: every law is applied there, and a
/// refusal comes back as an amber note with the blocks kept.
library;

import 'package:flutter/cupertino.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../app_state.dart';
import '../widgets/voice_ui.dart';
import '../widgets/owner_bits.dart';
import '../widgets/device_controls.dart';

class AutomationsScreen extends StatelessWidget {
  final AppState appState;
  const AutomationsScreen({super.key, required this.appState});

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: appState,
      builder: (context, _) => _build(context),
    );
  }

  Widget _build(BuildContext context) {
    final s = appState;
    final rules = s.automationRecords;
    // WHICH FACE. A rule in flight means the hub handed one over to
    // build; otherwise this is the list. One screen, two faces.
    final building = s.ruleBuilder != null;
    final editing = building && s.ruleBuilder!.rule?['id'] != null;

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
      // BACK STEPS OUT OF THE BLOCKS, not out of Automations. While a rule
      // is in hand the hub is waiting on it, so a bare pop would strand
      // it -- the gesture cancels the rule instead and lands on the list,
      // which is where "back" from the blocks should go anyway. With no
      // rule in hand it is an ordinary screen and pops normally.
      child: PopScope(
        canPop: !building,
        onPopInvokedWithResult: (didPop, _) {
          if (!didPop && building) s.sendRuleBuilder(null);
        },
        child: Scaffold(
        backgroundColor: s.bg,
        appBar: AppBar(
          backgroundColor: s.bg,
          elevation: 0,
          // NO BACK ARROW WHILE BUILDING: leaving would strand the hub
          // waiting on a rule. Cancel is the way out, and it is on the
          // save bar where the decision is being made.
          automaticallyImplyLeading: false,
          leading: building
              ? null
              : IconButton(
                  icon: Icon(Icons.arrow_back, color: s.ink),
                  onPressed: () => Navigator.pop(context),
                ),
          title: Text(
              building
                  ? (editing ? 'Edit automation' : 'New automation')
                  : 'Automations',
              style: TextStyle(
                  color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
          actions: [
            // The "Home Assistant things" filter is GONE with the hub's
            // `hide_ha` action: the WHEN list is boards only now, so there
            // is nothing to hide.
            if (!building)
              IconButton(
                tooltip: 'Refresh',
                icon: Icon(Icons.refresh, color: s.inkDim),
                onPressed: () => s.requestAutomationList(),
              ),
          ],
        ),
        // The FAB belongs to the LIST. Building has its own save bar, and
        // a floating "New" over a half-built rule is an invitation to
        // lose it.
        floatingActionButton: building
            ? null
            : FloatingActionButton.extended(
                backgroundColor: s.accent,
                foregroundColor: s.accentFg,
                // STAYS HERE. The hub answers with the builder frame and
                // this screen simply changes face.
                onPressed: () => s.sendMenuAction('add_automation'),
                icon: const Icon(Icons.add, size: 20),
                label: const Text('New',
                    style: TextStyle(fontWeight: FontWeight.w700)),
              ),
        body: SafeArea(
          child: building
              ? _RuleBlocks(appState: s, key: ValueKey(s.ruleBuilder))
              : rules.isEmpty
              ? Center(
                  child: Padding(
                    padding: const EdgeInsets.all(32),
                    child: Text(
                      'No automations yet.\n\nOne is a trigger and what it '
                      'should do — "when the doorway sees motion after dark, '
                      'run night light".\n\nNew builds one; tap any rule '
                      'afterwards to change it.',
                      textAlign: TextAlign.center,
                      style: TextStyle(
                          color: s.inkDim, fontSize: 14, height: 1.5),
                    ),
                  ),
                )
              : VoiceUiScope(
                  state: s,
                  builder: (ctx, scroll) => ListView(
                  controller: scroll,
                  padding: const EdgeInsets.fromLTRB(16, 8, 16, 90),
                  children: [
                    for (final r in rules) _ruleCard(context, s, r),
                    if (s.automationJournal.isNotEmpty) _journal(s),
                  ],
                ),
                ),
        ),
        ),
      ),
    );
  }

  Widget _ruleCard(BuildContext context, AppState s, Map<String, dynamic> r) {
    final enabled = r['enabled'] != false;
    final fired = (r['fired_count'] as num?)?.toInt() ?? 0;
    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      decoration: BoxDecoration(
        color: s.surface,
        border: Border.all(color: enabled ? s.border : s.border),
        borderRadius: BorderRadius.circular(12),
      ),
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        // TAP = EDIT. The hub asks "update which one?" and the app answers
        // it silently with this id, so the blocks open straight away.
        onTap: () => s.editAutomation((r['id'] ?? '').toString()),
        // LONG PRESS IS DETAILS (her design, 2026-10-11): who made and
        // changed it, and when it last ran, in a panel -- not on the card.
        onLongPress: () => _details(context, s, r),
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(children: [
                Icon(enabled ? Icons.bolt : Icons.pause_circle_outline,
                    size: 19, color: enabled ? s.accent : s.inkFaint),
                const SizedBox(width: 10),
                Expanded(
                  child: Text((r['name'] ?? '').toString(),
                      style: TextStyle(
                          fontSize: 15.5,
                          fontWeight: FontWeight.w600,
                          color: enabled ? s.ink : s.inkDim)),
                ),
                OwnerTag(s, (r['chip'] ?? '').toString()),
                // THE TWO SMALL CHANGES, ON the thing they change. Stop
                // that for now, or get rid of it — neither is worth a
                // question flow, and both used to be one.
                Switch(
                  value: enabled,
                  // activeThumbColor, not activeColor: renamed after
                  // Flutter 3.31.0-2.0.pre, and the old name only warns
                  // until it stops working.
                  activeThumbColor: s.accent,
                  onChanged: (_) =>
                      s.pauseAutomation((r['id'] ?? '').toString()),
                ),
                _menu(context, s, r),
              ]),
              const SizedBox(height: 6),
              Text((r['sentence'] ?? '').toString(),
                  style: TextStyle(
                      fontSize: 12.5, color: s.inkDim, height: 1.4)),
              const SizedBox(height: 8),
              Row(children: [
                if (r['has_else'] == true) _chip(s, 'ELSE'),
                if (r['has_revert'] == true) _chip(s, 'reverts when quiet'),
                const Spacer(),
                Text(fired == 0 ? 'never fired' : 'fired $fired×',
                    style: TextStyle(fontSize: 11, color: s.inkFaint)),
              ]),
            ],
          ),
        ),
      ),
    );
  }

  void _details(BuildContext context, AppState s, Map<String, dynamic> r) {
    final fired = (r['fired_count'] as num?)?.toInt() ?? 0;
    final last = dayOf(r['last_fired'], time: true);
    final who = whoLines(s, r);
    showDetailsPanel(
      context,
      s,
      title: (r['name'] ?? '').toString(),
      tag: (r['chip'] ?? '').toString(),
      subtitle: (r['sentence'] ?? '').toString(),
      body: [
        if (who.isNotEmpty) ...[
          Divider(color: s.border, height: 1),
          detailsHeading(s, 'WHO'),
          ...who,
        ],
        detailsHeading(s, 'WHEN IT RAN'),
        Text(
            fired == 0
                ? 'Never yet'
                : [
                    if (last.isNotEmpty) 'Last ran $last',
                    fired == 1 ? 'once in all' : '$fired times in all',
                  ].join(' · '),
            style: TextStyle(fontSize: 14, color: s.ink)),
      ],
    );
  }

  /// Remove, behind a confirmation. Deleting a habit is not dangerous —
  /// the devices are untouched — but it is not undoable either, and the
  /// rule is right there to be misread.
  Widget _menu(BuildContext context, AppState s, Map<String, dynamic> r) {
    return PopupMenuButton<String>(
      icon: Icon(Icons.more_vert, size: 18, color: s.inkFaint),
      color: s.surface,
      onSelected: (v) async {
        if (v != 'remove') return;
        final yes = await showDialog<bool>(
          context: context,
          builder: (dctx) => AlertDialog(
            backgroundColor: s.surface,
            title: Text('Remove ${r['name']}?',
                style: TextStyle(color: s.ink, fontSize: 17)),
            content: Text(
                'The habit goes. Every device it touched is left exactly '
                'as it is.',
                style: TextStyle(color: s.inkDim, fontSize: 13.5)),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(dctx, false),
                child: Text('Cancel',
                    style: TextStyle(color: s.cancelInk)),
              ),
              TextButton(
                onPressed: () => Navigator.pop(dctx, true),
                child: Text('Remove',
                    style: TextStyle(
                        color: s.cancelInk, fontWeight: FontWeight.w700)),
              ),
            ],
          ),
        );
        if (yes != true) return;
        s.removeAutomation((r['id'] ?? '').toString());
      },
      itemBuilder: (_) => [
        PopupMenuItem(
          value: 'remove',
          child: Row(children: [
            Icon(Icons.delete_outline, size: 16, color: s.cancelInk),
            const SizedBox(width: 8),
            Text('Remove', style: TextStyle(color: s.ink, fontSize: 14)),
          ]),
        ),
      ],
    );
  }

  Widget _chip(AppState s, String text) => Container(
        margin: const EdgeInsets.only(right: 6),
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
        decoration: BoxDecoration(
          color: s.accent.withValues(alpha: 0.10),
          borderRadius: BorderRadius.circular(20),
        ),
        child: Text(text,
            style: TextStyle(
                fontSize: 10.5,
                color: s.accent,
                fontWeight: FontWeight.w600)),
      );

  /// What the house has actually DONE, most recent first — including what
  /// it refused, which is the half that matters when a rule misbehaves.
  Widget _journal(AppState s) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(4, 12, 4, 8),
          child: Row(children: [
            Icon(Icons.history, size: 14, color: s.inkFaint),
            const SizedBox(width: 6),
            Text('RECENTLY',
                style: TextStyle(
                    fontSize: 11,
                    letterSpacing: 0.09,
                    fontWeight: FontWeight.w600,
                    color: s.inkFaint)),
            const SizedBox(width: 8),
            Expanded(child: Divider(color: s.border, height: 1)),
          ]),
        ),
        for (final j in s.automationJournal.take(12))
          Padding(
            padding: const EdgeInsets.only(bottom: 6, left: 4),
            child: RichText(
              text: TextSpan(children: [
                TextSpan(
                    text: '${j['name']}: ',
                    style: TextStyle(
                        fontSize: 12,
                        color: s.inkDim,
                        fontWeight: FontWeight.w600)),
                TextSpan(
                    text: (j['text'] ?? '').toString(),
                    style: TextStyle(
                        fontSize: 12, color: s.inkFaint, height: 1.4)),
              ]),
            ),
          ),
      ],
    );
  }
}


/// The blocks. Private to this file: the builder is not a place you go,
/// it is what this screen looks like when there is a rule in your hands.
class _RuleBlocks extends StatefulWidget {
  final AppState appState;
  const _RuleBlocks({super.key, required this.appState});

  @override
  State<_RuleBlocks> createState() => _RuleBlocksState();
}

class _RuleBlocksState extends State<_RuleBlocks> {
  final _nameCtl = TextEditingController();
  final _sayCtl = TextEditingController();

  // WHEN
  String? _whenDevice;
  Map<String, dynamic>? _whenOption;
  final _whenNumber = TextEditingController();
  final _whenMinutes = TextEditingController(text: '5');
  final _whenAt = TextEditingController(text: '07:00');
  final _whenTag = TextEditingController();

  /// Take the option just chosen and fill in what the hub already knows.
  ///
  /// ONE CASE, and it earns its place: a card UID is sixteen hex
  /// characters nobody memorises, and until now the only place one was
  /// ever printed was the hub's log — so "a specific card" meant reading
  /// a log file and typing it back. The hub sends `last`, the UID that
  /// reader saw most recently, so the flow is: tap the card, open the
  /// builder, pick the option, and it is already there. Only when the
  /// field is EMPTY, so it can never overwrite what somebody typed.
  void _seed(Map<String, dynamic>? option) {
    if (option?['needs'] == 'tag' && _whenTag.text.trim().isEmpty) {
      _whenTag.text = (option?['last'] ?? '').toString();
    }
  }

  /// A TIME IS NOT A DEVICE — but the WHEN block starts from one dropdown,
  /// and that dropdown is where a person looks for "at seven". So the time
  /// trigger rides in the device slot under a sentinel no device id can
  /// collide with: ids are `<room>.<name>`, and neither half holds an '@'.
  ///
  /// The hub has understood this trigger all along (`TRIGGER_KINDS`
  /// includes "time", validate_rule checks the HH:MM, the tick fires it
  /// once a day). Only the app had no way in, because the block builder
  /// replaces the console flow that offered it. Her ruling, 2026-09-01.
  static const _kTime = '@time';

  /// The single option that sentinel offers, shaped EXACTLY like the ones
  /// the hub sends. Nothing downstream needs to know it is special: the
  /// second dropdown lists it, `needs` opens its field, and `trigger` is
  /// the rule the hub already validates and fires.
  static const Map<String, dynamic> _timeOption = {
    'label': 'at a time of day',
    'needs': 'time',
    'trigger': {'kind': 'time'},
  };

  // IF
  /// The "only if"s, every one of which must hold. The first may be "no
  /// limit"; the names below are the first one's (the voice flow uses them).
  final List<_Limit> _ifs = [_Limit()];
  String get _ifKind => _ifs.first.kind;
  set _ifKind(String v) => _ifs.first.kind = v;
  TextEditingController get _ifAfter => _ifs.first.after;
  TextEditingController get _ifBefore => _ifs.first.before;
  set _ifDevice(String? v) => _ifs.first.device = v;
  set _ifEquals(bool v) => _ifs.first.equals = v;

  // THEN / ELSE rows
  final List<_ActionRow> _then = [_ActionRow()];
  final List<_ActionRow> _else = [];
  final List<_ActionRow> _revert = [];
  final _revertMinutes = TextEditingController(text: '5');

  /// LOOP: THEN starts again from the top after its last step, until the
  /// automation is switched off (Arduino's loop()). Needs a Wait row.
  bool _loop = false;

  // IF I SWITCH IT MYSELF -- who wins when a person commands the device a
  // rule drives. default = the house setting (a short window, time rules
  // exempt); window = the person wins for [_ovValue] [_ovUnit] after their
  // last command; always = the automation always wins. (2026-10-01)
  String _ovMode = 'default';
  final _ovValue = TextEditingController(text: '10');
  String _ovUnit = 'minutes';

  bool _loaded = false;

  /// The trigger of the rule being edited, for the one field the blocks
  /// cannot show: a change trigger's optional `by`. Without it an edit
  /// silently dropped the rule's own movement threshold.
  Map<String, dynamic>? _loadedTrigger;

  @override
  void initState() {
    super.initState();
    // The dock's tap fills the BLOCKS in by voice. Registered here rather
    // than on the screen because the builder is a face of the automations
    // screen, not a route: it appears and goes without the navigator ever
    // hearing about it (2026-09-11).
    widget.appState.screenVoiceRequest = _startVoice;
  }

  @override
  void dispose() {
    final st = widget.appState;
    if (st.screenVoiceRequest == _startVoice) st.screenVoiceRequest = null;
    st.disarmScreenChoice();
    for (final c in [_nameCtl, _whenNumber, _whenMinutes, _whenAt, _whenTag,
                     _revertMinutes, _ovValue]) {
      c.dispose();
    }
    _sayCtl.dispose();
    for (final l in _ifs) {
      l.dispose();
    }
    for (final r in [..._then, ..._else, ..._revert]) {
      r.dispose();
    }
    super.dispose();
  }

  // ── loading an existing rule back INTO blocks ──────────────────────

  void _loadFrom(RuleBuilderFrame f) {
    _loaded = true;
    _sayCtl.text = f.heard;
    final rule = f.rule;
    if (rule == null) {
      _whenDevice = f.devices.keys.isEmpty ? null : f.devices.keys.first;
      _whenOption = _optionsFor(f, _whenDevice).isEmpty
          ? null
          : _optionsFor(f, _whenDevice).first;
      return;
    }
    _nameCtl.text = (rule['name'] ?? '').toString();
    final trigger = (rule['trigger'] as Map?)?.cast<String, dynamic>() ?? {};
    _loadedTrigger = trigger;
    // A TIME RULE NAMES NO DEVICE, so reopening one has to land on the
    // sentinel rather than on a blank dropdown that loses the rule.
    if (trigger['kind'] == 'time') {
      _whenDevice = _kTime;
      _whenOption = _timeOption;
      final at = (trigger['at'] ?? '').toString();
      if (at.isNotEmpty) _whenAt.text = at;
      _loadRest(rule);
      return;
    }
    _whenDevice = trigger['device']?.toString();
    for (final o in _optionsFor(f, _whenDevice)) {
      final t = (o['trigger'] as Map?)?.cast<String, dynamic>() ?? {};
      if (t['kind'] != trigger['kind']) continue;
      // The same device can offer several shapes of one kind (turns on /
      // turns off): match the value and the reading key too, so reopening
      // a rule lands on the block it was actually built from.
      if (t.containsKey('value') && t['value'] != trigger['value']) continue;
      if (o.containsKey('key') && o['key'] != trigger['key']) continue;
      // A threshold's EDGE: both options carry the same kind and key, so
      // matching on those alone always landed on "rises above", and a
      // saved "falls below" rule flipped on its next Save. The hub names
      // the side in `edge`.
      if (t['kind'] == 'threshold') {
        final edge = _edgeOf(o);
        if (trigger['below'] != null && edge != 'below') continue;
        if (trigger['above'] != null && edge != 'above') continue;
      }
      _whenOption = o;
      break;
    }
    _whenOption ??= _optionsFor(f, _whenDevice).isEmpty
        ? null
        : _optionsFor(f, _whenDevice).first;
    if (trigger['above'] != null) _whenNumber.text = '${trigger['above']}';
    if (trigger['below'] != null) _whenNumber.text = '${trigger['below']}';
    // A card rule carries its UID in `value`, which the option match above
    // already used to land on the right block; this puts it back in the
    // field so reopening the rule shows the card it answers to.
    if (_whenOption?['needs'] == 'tag') {
      _whenTag.text = (trigger['value'] ?? '').toString();
    }
    if (trigger['for_s'] != null) {
      _whenMinutes.text =
          '${((trigger['for_s'] as num).toDouble() / 60).round()}';
    }
    _loadRest(rule);
  }

  /// Everything BELOW the WHEN block. Split out so the time trigger, which
  /// has no device to look up and so leaves early, still restores the IF,
  /// the THEN, the ELSE and the revert exactly as any other rule does.
  void _loadRest(Map<String, dynamic> rule) {
    final limits = [
      rule['condition'],
      ...((rule['conditions'] as List?) ?? const []),
    ];
    for (final l in _ifs) {
      l.dispose();
    }
    _ifs
      ..clear()
      ..addAll([
        for (final c in limits)
          if (c is Map) _Limit.fromJson(c.cast<String, dynamic>()),
      ]);
    if (_ifs.isEmpty) _ifs.add(_Limit());

    final ov = (rule['override'] as Map?)?.cast<String, dynamic>();
    _ovMode = 'default';
    if (ov != null) {
      final mode = ov['mode']?.toString() ?? 'default';
      if (mode == 'always') {
        _ovMode = 'always';
      } else if (mode == 'window') {
        _ovMode = 'window';
        final v = ov['value'];
        if (v is num) {
          _ovValue.text = v == v.roundToDouble() ? '${v.toInt()}' : '$v';
        }
        final u = ov['unit']?.toString() ?? 'minutes';
        _ovUnit =
            const ['seconds', 'minutes', 'hours', 'days'].contains(u)
                ? u
                : 'minutes';
      }
    }

    for (final r in [..._then, ..._else, ..._revert]) {
      r.dispose();
    }
    _then
      ..clear()
      // The hub's actions_of: a NON-EMPTY `actions` wins, else `action`.
      ..addAll(_rowsFrom(
          rule['actions'] is List && (rule['actions'] as List).isNotEmpty
              ? rule['actions']
              : rule['action']));
    if (_then.isEmpty) _then.add(_ActionRow());
    _loop = rule['loop'] == true;
    _else
      ..clear()
      ..addAll(_rowsFrom(rule['else_actions']));
    final rev = (rule['revert'] as Map?)?.cast<String, dynamic>();
    if (rev != null) {
      _revert
        ..clear()
        ..addAll(_rowsFrom(rev['actions']));
      final after = (rev['after_s'] as num?)?.toDouble() ?? 300;
      _revertMinutes.text = '${(after / 60).round()}';
    }
  }

  /// Which side a threshold option's number goes on: the hub's `edge`
  /// ("above" / "below"), read off the label only for a hub too old to
  /// send it.
  static String _edgeOf(Map<String, dynamic> option) {
    final edge = option['edge']?.toString();
    if (edge == 'above' || edge == 'below') return edge!;
    return (option['label'] ?? '').toString().contains('rises above')
        ? 'above'
        : 'below';
  }

  List<_ActionRow> _rowsFrom(dynamic raw) {
    final list = raw is List ? raw : (raw is Map ? [raw] : const []);
    return [
      for (final a in list)
        if (a is Map) _ActionRow.fromJson(a.cast<String, dynamic>())
    ];
  }

  List<Map<String, dynamic>> _optionsFor(RuleBuilderFrame f, String? did) {
    if (did == _kTime) return const [_timeOption];
    final d = (f.devices[did] as Map?)?.cast<String, dynamic>();
    return [
      for (final o in (d?['options'] as List? ?? const []))
        if (o is Map) o.cast<String, dynamic>()
    ];
  }


  // ══ BUILDING A RULE BY VOICE ═══════════════════════════════════════
  //
  // A form is a flat list of fields and "say the field, then say the
  // value" reaches all of it. A rule is not: it is four little sentences
  // whose slots DEPEND on each other. `_whenOption` means nothing until
  // `_whenDevice` is picked, because each device carries its own
  // `options[]`; an option's extra slot exists only when it `needs` a
  // number, minutes, a time or a tag; an action's second slot is a scene
  // or a device or a line of text depending on its kind.
  //
  // So the voice walks each block as a short SCRIPT instead of offering a
  // field list, and every step of every script is one of the two things
  // already built: a local choice (a fixed set -- match it) or a local
  // dictation (a value -- take the words). No new primitives; the whole
  // of this section is a state machine over those two.
  //
  // Nothing arms itself, here as everywhere: the dock's tap starts it,
  // and `screenVoiceRequest` is registered by the BLOCKS -- not by the
  // screen -- because the builder is a face of the automations screen
  // rather than a route of its own, so it appears and goes without the
  // navigator ever hearing about it.

  /// The frame the scripts read their vocabularies from, refreshed on
  /// every build so a re-armed list is always what is on screen.
  RuleBuilderFrame? _voiceFrame;

  void _startVoice() {
    final f = _voiceFrame;
    if (f == null) return;
    _armBlocks(widget.appState, f, speak: true);
  }

  /// Level one: which block. Plus the name, save, and the one control
  /// that earns its place on a screen made of four dropdown rows --
  /// "read it back", which speaks the live plain-English sentence the
  /// blocks already compose. That sentence is what a rule IS; being able
  /// to ask for it at any point is most of what voice is for here.
  void _armBlocks(AppState s, RuleBuilderFrame f,
      {bool speak = false, String say = ''}) {
    final lead = say.isEmpty
        ? 'Say a block to fill in: when, if, then, else. '
            'Or say name, read it back, or save.'
        : '$say Say another block, or say save.';
    s.armScreenChoice(
      prompt: lead,
      speakPrompt: speak || say.isNotEmpty,
      repeatLine: 'Say when, if, then, else, name, read it back, or save.',
      options: const [
        {'value': 'when', 'label': 'when'},
        {'value': 'if', 'label': 'if'},
        {'value': 'then', 'label': 'then'},
        {'value': 'else', 'label': 'else'},
        {'value': 'name', 'label': 'name'},
        {'value': 'read', 'label': 'read it back'},
        {'value': 'save', 'label': 'save'},
      ],
      onCancel: () => s.sendRuleBuilder(null),
      onPick: (value, heard) {
        if (!mounted) return;
        final f2 = _voiceFrame ?? f;
        switch (value) {
          case 'when':
            _voiceWhenDevice(s, f2);
            return;
          case 'if':
            _voiceIfKind(s, f2);
            return;
          case 'then':
            _voiceActionRows(s, f2, _then, 'then');
            return;
          case 'else':
            _voiceActionRows(s, f2, _else, 'else');
            return;
          case 'name':
            s.armScreenDictation(
              prompt: 'What is this rule called?',
              onCancel: () => _armBlocks(s, f2),
              onText: (t) {
                if (!mounted) return;
                setState(() => _nameCtl.text = t);
                _armBlocks(s, f2, say: '$t.');
              },
            );
            return;
          case 'read':
            // The sentence under the blocks, said out loud. Re-armed
            // after it, because "read it back" is nearly always followed
            // by changing the thing you just heard.
            _armBlocks(s, f2, say: '${_sentence(f2)}.');
            return;
          case 'save':
            if (_compose() == null) {
              _armBlocks(s, f2,
                  say: 'Not ready yet — the WHEN block needs filling in, '
                      'and THEN needs at least one action.');
            } else if (_nameCtl.text.trim().isEmpty) {
              _armBlocks(s, f2, say: 'It still needs a name.');
            } else {
              s.sendRuleBuilder(_compose());
            }
            return;
        }
      },
    );
  }

  // ── WHEN: device, then that device's own option, then its extra slot ──

  void _voiceWhenDevice(AppState s, RuleBuilderFrame f) {
    final options = <Map<String, dynamic>>[
      {'value': _kTime, 'label': 'time of day'},
      for (final e in f.devices.entries)
        {
          'value': e.key,
          'label': ((e.value as Map?)?['name'] ?? e.key).toString(),
        },
    ];
    s.armScreenChoice(
      prompt: 'When what? Say a device, or say time of day.',
      speakPrompt: true,
      repeatLine: 'Say a device, or say time of day.',
      options: options,
      onCancel: () => _armBlocks(s, f),
      onPick: (value, heard) {
        if (!mounted) return;
        setState(() {
          _whenDevice = value;
          _whenOption = _optionsFor(f, value).isEmpty
              ? null
              : _optionsFor(f, value).first;
          _seed(_whenOption);
        });
        _voiceWhenOption(s, f);
      },
    );
  }

  void _voiceWhenOption(AppState s, RuleBuilderFrame f) {
    final opts = _optionsFor(f, _whenDevice);
    if (opts.isEmpty) {
      _armBlocks(s, f, say: 'That one has nothing it can be triggered by.');
      return;
    }
    if (opts.length == 1) {
      setState(() {
        _whenOption = opts.first;
        _seed(opts.first);
      });
      _voiceWhenExtra(s, f, opts.first);
      return;
    }
    s.armScreenChoice(
      prompt: 'When it does what? Say one of: '
          '${opts.map((o) => o['label']).join(', ')}.',
      speakPrompt: true,
      repeatLine: 'Say one of: ${opts.map((o) => o['label']).join(', ')}.',
      options: [
        for (final o in opts)
          {'value': (o['label'] ?? '').toString(), 'label': (o['label'] ?? '').toString()}
      ],
      onCancel: () => _armBlocks(s, f),
      onPick: (value, heard) {
        if (!mounted) return;
        final picked = opts.firstWhere(
            (o) => (o['label'] ?? '').toString() == value,
            orElse: () => opts.first);
        setState(() {
          _whenOption = picked;
          _seed(picked);
        });
        _voiceWhenExtra(s, f, picked);
      },
    );
  }

  /// The slot an option asks for, when it asks for one: a number, a
  /// number of minutes, a time of day, or a card's tag. A tag is the one
  /// that is NOT dictated -- sixteen hex characters is not something a
  /// mouth says, and the hub already seeds the last card this reader saw.
  void _voiceWhenExtra(
      AppState s, RuleBuilderFrame f, Map<String, dynamic> option) {
    switch (option['needs']?.toString()) {
      case 'number':
        _voiceNumber(s, f, 'What number?', _whenNumber);
        return;
      case 'minutes':
        _voiceNumber(s, f, 'How many minutes?', _whenMinutes);
        return;
      case 'time':
        _voiceTime(s, f, 'At what time?', _whenAt);
        return;
      case 'tag':
        _armBlocks(s, f,
            say: _whenTag.text.trim().isEmpty
                ? 'That one needs a card. Tap the card on the reader, or '
                    'type its number.'
                : 'Using the last card seen.');
        return;
      default:
        _armBlocks(s, f, say: '${_sentence(f)}.');
    }
  }

  // ── IF ────────────────────────────────────────────────────────────

  void _voiceIfKind(AppState s, RuleBuilderFrame f) {
    s.armScreenChoice(
      prompt: 'Only when? Say any time, between two times, '
          'or when a device is on or off.',
      speakPrompt: true,
      repeatLine: 'Say any time, between two times, or '
          'when a device is on or off.',
      options: const [
        {'value': 'none', 'label': 'any time'},
        {'value': 'time_range', 'label': 'between two times'},
        {'value': 'device', 'label': 'when a device is on or off'},
      ],
      onCancel: () => _armBlocks(s, f),
      onPick: (value, heard) {
        if (!mounted) return;
        setState(() => _ifKind = value);
        if (value == 'time_range') {
          _voiceTime(s, f, 'After what time?', _ifAfter,
              then: () => _voiceTime(s, f, 'And before what time?', _ifBefore));
        } else if (value == 'device') {
          _voiceIfDevice(s, f);
        } else {
          _armBlocks(s, f, say: 'Any time.');
        }
      },
    );
  }

  void _voiceIfDevice(AppState s, RuleBuilderFrame f) {
    final targets = <Map<String, dynamic>>[
      for (final t in f.targets)
        if (t is Map)
          {
            'value': (t['value'] ?? '').toString(),
            'label': (t['label'] ?? t['value'] ?? '').toString(),
          }
    ];
    if (targets.isEmpty) {
      _armBlocks(s, f, say: 'There is nothing to check.');
      return;
    }
    s.armScreenChoice(
      prompt: 'Which device?',
      speakPrompt: true,
      repeatLine: 'Which device? Say one of them by name.',
      options: targets,
      onCancel: () => _armBlocks(s, f),
      onPick: (value, heard) {
        if (!mounted) return;
        setState(() => _ifDevice = value);
        s.armScreenChoice(
          prompt: 'Is it on, or off?',
          speakPrompt: true,
          repeatLine: 'Say on, or off.',
          options: const [
            {'value': 'on', 'label': 'on'},
            {'value': 'off', 'label': 'off'},
          ],
          onCancel: () => _armBlocks(s, f),
          onPick: (v2, h2) {
            if (!mounted) return;
            setState(() => _ifEquals = v2 == 'on');
            _armBlocks(s, f, say: '${_sentence(f)}.');
          },
        );
      },
    );
  }

  // ── THEN / ELSE: a list of action rows ─────────────────────────────

  /// With one row, go straight into it. With several, ask which -- and
  /// always offer another, because "and also turn on the hall light" is
  /// the commonest second thought there is.
  void _voiceActionRows(
      AppState s, RuleBuilderFrame f, List<_ActionRow> rows, String which) {
    if (rows.isEmpty) {
      setState(() => rows.add(_ActionRow()));
      _voiceActionKind(s, f, rows, rows.length - 1, which);
      return;
    }
    if (rows.length == 1) {
      _voiceActionKind(s, f, rows, 0, which);
      return;
    }
    s.armScreenChoice(
      prompt: 'Which $which action? Say a number, or say add another.',
      speakPrompt: true,
      repeatLine: 'Say a number from one to ${rows.length}, '
          'or say add another.',
      options: [
        for (var i = 0; i < rows.length; i++)
          {
            'value': '$i',
            'label': rows[i].say(f).isEmpty
                ? 'action ${i + 1}'
                : rows[i].say(f),
          },
        {'value': 'add', 'label': 'add another'},
      ],
      onCancel: () => _armBlocks(s, f),
      onPick: (value, heard) {
        if (!mounted) return;
        if (value == 'add') {
          setState(() => rows.add(_ActionRow()));
          _voiceActionKind(s, f, rows, rows.length - 1, which);
          return;
        }
        _voiceActionKind(s, f, rows, int.tryParse(value) ?? 0, which);
      },
    );
  }

  void _voiceActionKind(AppState s, RuleBuilderFrame f,
      List<_ActionRow> rows, int i, String which) {
    if (i < 0 || i >= rows.length) {
      _armBlocks(s, f);
      return;
    }
    final row = rows[i];
    s.armScreenChoice(
      prompt: 'Do what? Say run a scene, set a device, '
          'send a notification, ask me to approve, or show it on a panel.',
      speakPrompt: true,
      repeatLine: 'Say run a scene, set a device, send a notification, '
          'ask me to approve, or show it on a panel.',
      options: const [
        {'value': 'scene', 'label': 'run a scene'},
        {'value': 'device', 'label': 'set a device'},
        {'value': 'notify', 'label': 'send a notification'},
        {'value': 'ask', 'label': 'ask me to approve'},
        {'value': 'display', 'label': 'show it on a panel'},
      ],
      onCancel: () => _armBlocks(s, f),
      onPick: (value, heard) {
        if (!mounted) return;
        setState(() => row.kind = value);
        _voiceActionValue(s, f, rows, i, which);
      },
    );
  }

  void _voiceActionValue(AppState s, RuleBuilderFrame f,
      List<_ActionRow> rows, int i, String which) {
    final row = rows[i];
    void done() => _voiceMoreActions(s, f, rows, which);

    switch (row.kind) {
      case 'scene':
        final scenes = <Map<String, dynamic>>[
          for (final sc in f.scenes)
            {'value': sc.toString(), 'label': sc.toString()}
        ];
        if (scenes.isEmpty) {
          _armBlocks(s, f, say: 'There are no scenes to run yet.');
          return;
        }
        s.armScreenChoice(
          prompt: 'Which scene?',
          speakPrompt: true,
          repeatLine: 'Which scene? Say one of them by name.',
          options: scenes,
          onCancel: () => _armBlocks(s, f),
          onPick: (v, h) {
            if (!mounted) return;
            setState(() => row.scene = v);
            done();
          },
        );
        return;

      case 'notify':
        s.armScreenDictation(
          prompt: 'What should it say?',
          onCancel: () => _armBlocks(s, f),
          onText: (t) {
            if (!mounted) return;
            setState(() => row.text.text = t);
            done();
          },
        );
        return;

      case 'device':
      case 'ask':
        _voicePickTarget(s, f, 'Which device?', (did) {
          setState(() => row.device = did);
          s.armScreenChoice(
            prompt: 'On, or off?',
            speakPrompt: true,
            repeatLine: 'Say on, or off.',
            options: const [
              {'value': 'on', 'label': 'on'},
              {'value': 'off', 'label': 'off'},
            ],
            onCancel: () => _armBlocks(s, f),
            onPick: (v, h) {
              if (!mounted) return;
              setState(() => row.on = v == 'on');
              done();
            },
          );
        });
        return;

      case 'display':
        _voicePickTarget(s, f, 'Which panel?', (did) {
          setState(() => row.device = did);
          s.armScreenDictation(
            prompt: 'What should it show?',
            onCancel: () => _armBlocks(s, f),
            onText: (t) {
              if (!mounted) return;
              setState(() {
                row.text.text = t;
                row.kind = 'display';
                row.display = {
                  'pages': [
                    [
                      {'text': t, 'align': 'left'}
                    ]
                  ],
                  'for_s': 0,
                };
              });
              done();
            },
          );
        });
        return;
    }
    done();
  }

  void _voicePickTarget(AppState s, RuleBuilderFrame f, String prompt,
      void Function(String did) pick) {
    final targets = <Map<String, dynamic>>[
      for (final t in f.targets)
        if (t is Map)
          {
            'value': (t['value'] ?? '').toString(),
            'label': (t['label'] ?? t['value'] ?? '').toString(),
          }
    ];
    if (targets.isEmpty) {
      _armBlocks(s, f, say: 'There is nothing this rule could drive.');
      return;
    }
    s.armScreenChoice(
      prompt: prompt,
      speakPrompt: true,
      repeatLine: '$prompt Say one of them by name.',
      options: targets,
      onCancel: () => _armBlocks(s, f),
      onPick: (v, h) {
        if (!mounted) return;
        pick(v);
      },
    );
  }

  /// After an action is filled in: another one, or back to the blocks.
  void _voiceMoreActions(AppState s, RuleBuilderFrame f,
      List<_ActionRow> rows, String which) {
    s.armScreenChoice(
      prompt: '${_sentence(f)}. Say add another, or say done.',
      speakPrompt: true,
      repeatLine: 'Say add another, or say done.',
      options: const [
        {'value': 'add', 'label': 'add another'},
        {'value': 'done', 'label': 'done'},
      ],
      onCancel: () => _armBlocks(s, f),
      onPick: (v, h) {
        if (!mounted) return;
        if (v == 'add') {
          setState(() => rows.add(_ActionRow()));
          _voiceActionKind(s, f, rows, rows.length - 1, which);
        } else {
          _armBlocks(s, f);
        }
      },
    );
  }

  // ── values ─────────────────────────────────────────────────────────

  void _voiceNumber(AppState s, RuleBuilderFrame f, String prompt,
      TextEditingController box) {
    s.armScreenDictation(
      prompt: prompt,
      onCancel: () => _armBlocks(s, f),
      onText: (t) {
        if (!mounted) return;
        final cleaned = t.replaceAll(RegExp(r'[^0-9.\-]'), '');
        if (double.tryParse(cleaned) == null) {
          _armBlocks(s, f, say: '$t is not a number.');
          return;
        }
        setState(() => box.text = cleaned);
        _armBlocks(s, f, say: '${_sentence(f)}.');
      },
    );
  }

  /// A time slot, spoken. `then` runs instead of returning to the blocks
  /// -- the IF block asks for two times and the second follows the first.
  void _voiceTime(AppState s, RuleBuilderFrame f, String prompt,
      TextEditingController box,
      {VoidCallback? then}) {
    s.armScreenDictation(
      prompt: prompt,
      onCancel: () => _armBlocks(s, f),
      onText: (t) {
        if (!mounted) return;
        final parsed = _spokenTime(t);
        if (parsed == null) {
          // REFUSED, NOT GUESSED. The hub validates HH:MM and would
          // reject the rule; writing a guess into the slot only moves the
          // refusal somewhere less useful. Same rule as the number field.
          _armBlocks(s, f, say: 'I could not read $t as a time.');
          return;
        }
        setState(() => box.text = parsed);
        if (then != null) {
          then();
        } else {
          _armBlocks(s, f, say: '$parsed.');
        }
      },
    );
  }

  /// A SPOKEN TIME AS HH:MM, or null when it is not one.
  ///
  /// Moonshine gives words, and people say a time half a dozen ways:
  /// "seven pm", "7:30", "half past six", "quarter to eight", "nineteen
  /// hundred", "noon", "midnight", "seven o'clock". All of those land
  /// here; anything else is refused out loud rather than written into a
  /// slot the hub will reject.
  ///
  /// Deliberately NOT a general parser. It covers what a person says to a
  /// phone about a lamp, and says so when it cannot.
  static String? _spokenTime(String said) {
    var t = said.toLowerCase().trim();
    t = t.replaceAll(RegExp(r"[.,!?]"), ' ');
    t = t.replaceAll("o'clock", ' ').replaceAll('oclock', ' ');
    t = t.replaceAll(RegExp(r'\s+'), ' ').trim();
    if (t.isEmpty) return null;

    if (RegExp(r'^(at )?noon$|^12 noon$|^midday$').hasMatch(t)) return '12:00';
    if (RegExp(r'^(at )?midnight$').hasMatch(t)) return '00:00';
    t = t.replaceFirst(RegExp(r'^at '), '');

    // pm/am, taken off the end and applied last.
    var pm = false, am = false;
    final ampm = RegExp(r'\b(a m|am|p m|pm|in the morning|in the evening|'
            r'in the afternoon|at night)\b')
        .firstMatch(t);
    if (ampm != null) {
      final w = ampm.group(1)!;
      pm = w.startsWith('p') || w.contains('evening') || w.contains('afternoon') ||
          w.contains('night');
      am = !pm;
      t = t.replaceRange(ampm.start, ampm.end, ' ').trim();
      t = t.replaceAll(RegExp(r'\s+'), ' ');
    }

    int? hour;
    int minute = 0;

    // "half past six", "quarter past six", "quarter to eight", "ten to six"
    final rel = RegExp(r'^(half|quarter|\d{1,2}|' + _numWords.keys.join('|') +
            r') (past|to) (\d{1,2}|' + _numWords.keys.join('|') + r')$')
        .firstMatch(t);
    if (rel != null) {
      final amount = rel.group(1)!;
      final dir = rel.group(2)!;
      final h = _asNumber(rel.group(3)!);
      if (h == null) return null;
      final mins = amount == 'half'
          ? 30
          : amount == 'quarter'
              ? 15
              : _asNumber(amount);
      if (mins == null || mins > 59) return null;
      if (dir == 'past') {
        hour = h;
        minute = mins;
      } else {
        hour = h - 1;
        minute = 60 - mins;
        if (hour < 0) hour = 23;
      }
    } else {
      // "7:30", "0730", "19 30", "nineteen hundred", "seven thirty", "seven"
      final colon = RegExp(r'^(\d{1,2})[:h ](\d{2})$').firstMatch(t);
      final bare4 = RegExp(r'^(\d{2})(\d{2})$').firstMatch(t);
      final hundred = RegExp(r'^(.+) hundred$').firstMatch(t);
      if (colon != null) {
        hour = int.parse(colon.group(1)!);
        minute = int.parse(colon.group(2)!);
      } else if (bare4 != null) {
        hour = int.parse(bare4.group(1)!);
        minute = int.parse(bare4.group(2)!);
      } else if (hundred != null) {
        hour = _asNumber(hundred.group(1)!);
        minute = 0;
      } else {
        // GREEDY: the longest leading phrase that is a number is the
        // HOUR, and whatever is left is the minutes. Counting words
        // instead got "seven forty five" wrong (three words, so it fell
        // through to nothing) and "twenty five o'clock" WRONG-wrong -- it
        // read 20 past 5 and wrote 20:05 for an hour that does not exist.
        // Taking the longest hour first makes that 25, which the range
        // check below then refuses, which is the honest answer.
        final words = t.split(' ');
        for (var take = words.length < 2 ? words.length : 2;
            take >= 1;
            take--) {
          final h = _asNumber(words.take(take).join(' '));
          if (h == null) continue;
          hour = h;
          final rest = words.skip(take).join(' ').trim();
          if (rest.isNotEmpty) minute = _asNumber(rest) ?? -1;
          break;
        }
      }
    }

    if (hour == null || minute < 0 || minute > 59) return null;
    if (pm && hour < 12) hour += 12;
    if (am && hour == 12) hour = 0;
    if (hour > 23) return null;
    return '${hour.toString().padLeft(2, '0')}:'
        '${minute.toString().padLeft(2, '0')}';
  }

  /// The number words a time is said with. Twenty-one to fifty-nine are
  /// handled as two words by _asNumber rather than listed.
  static const Map<String, int> _numWords = {
    'zero': 0, 'oh': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4,
    'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
    'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
    'nineteen': 19, 'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50,
  };

  static int? _asNumber(String word) {
    final w = word.trim();
    if (w.isEmpty) return null;
    final digits = int.tryParse(w);
    if (digits != null) return digits;
    if (_numWords.containsKey(w)) return _numWords[w];
    // "twenty five", "thirty seven"
    final parts = w.split(RegExp(r'[ -]'));
    if (parts.length == 2 &&
        _numWords.containsKey(parts[0]) &&
        _numWords.containsKey(parts[1])) {
      final tens = _numWords[parts[0]]!;
      final units = _numWords[parts[1]]!;
      if (tens >= 20 && tens % 10 == 0 && units < 10) return tens + units;
      // "seven oh five" -- the minutes half of a time said the way a
      // clock reads it.
      if ((parts[0] == 'oh' || parts[0] == 'zero') && units < 10) {
        return units;
      }
    }
    return null;
  }

  // ── composing the rule the hub will receive ────────────────────────

  Map<String, dynamic>? _compose() {
    final option = _whenOption;
    if (option == null) return null;
    final trigger = Map<String, dynamic>.from(
        (option['trigger'] as Map?)?.cast<String, dynamic>() ?? {});
    final needs = option['needs']?.toString();
    if (needs == 'number') {
      final v = double.tryParse(_whenNumber.text.trim());
      if (v == null) return null;
      // The option's `edge` says which side the number goes on.
      if (_edgeOf(option) == 'above') {
        trigger['above'] = v;
      } else {
        trigger['below'] = v;
      }
      trigger['key'] = option['key'];
    } else if (needs == 'minutes') {
      final m = int.tryParse(_whenMinutes.text.trim());
      if (m == null || m <= 0) return null;
      trigger['for_s'] = m * 60;
    } else if (needs == 'time') {
      // HH:MM, and padded here so 7:00 saves as 07:00 -- the hub compares
      // the string against its own clock, and "7:00" would never match.
      final m = RegExp(r'^(\d{1,2}):(\d{2})$').firstMatch(_whenAt.text.trim());
      if (m == null) return null;
      final h = int.parse(m.group(1)!), mi = int.parse(m.group(2)!);
      if (h > 23 || mi > 59) return null;
      trigger['at'] = '${h.toString().padLeft(2, '0')}:'
          '${mi.toString().padLeft(2, '0')}';
    } else if (needs == 'tag') {
      // THE UID IS THE WHOLE TRIGGER, so an empty one is not a rule — it
      // would save as "any card" wearing the label of one card, which is
      // the worst of both. Upper-cased because Tasmota publishes hex in
      // upper case and the hub compares the strings exactly.
      final uid = _whenTag.text.trim().toUpperCase();
      if (uid.isEmpty) return null;
      trigger['value'] = uid;
    }
    final was = _loadedTrigger;
    if (trigger['kind'] == 'change' &&
        was != null &&
        was['kind'] == 'change' &&
        was['by'] != null &&
        was['device'] == trigger['device'] &&
        was['key'] == trigger['key']) {
      trigger['by'] = was['by'];
    }

    final limits = [
      for (final l in _ifs)
        if (l.kind != 'none') l.toJson(),
    ];
    if (limits.any((l) => l == null)) return null;
    final Map<String, dynamic>? condition =
        limits.length == 1 ? limits.first : null;
    final List<Map<String, dynamic>>? conditions =
        limits.length > 1 ? [for (final l in limits) l!] : null;

    if ([..._then, ..._else, ..._revert].any((r) => r.badHold)) return null;
    final then = [for (final r in _then) if (r.toJson() != null) r.toJson()!];
    if (then.isEmpty) return null;
    // A loop with no Wait would run flat out: the hub refuses it too.
    if (_loop && !then.any((a) => a['kind'] == 'wait')) return null;
    final rule = <String, dynamic>{
      'name': _nameCtl.text.trim(),
      'trigger': trigger,
      'condition': condition,
      'conditions': conditions,
      'actions': then,
      if (_loop) 'loop': true,
    };
    final els = [for (final r in _else) if (r.toJson() != null) r.toJson()!];
    if (els.isNotEmpty) rule['else_actions'] = els;
    final rev = [for (final r in _revert) if (r.toJson() != null) r.toJson()!];
    if (rev.isNotEmpty) {
      // The hub refuses after_s <= 0; an empty or zero box used to be
      // saved as a silent 5 (empty) or refused only after Save (zero).
      final m = int.tryParse(_revertMinutes.text.trim());
      if (m == null || m <= 0) return null;
      rule['revert'] = {'after_s': m * 60, 'actions': rev};
    }
    // ALWAYS SENT, null for the house setting: the hub clears a rule's
    // override when it arrives as null, and keeps it when the key is absent
    // (an older build of this app never mentions it).
    if (_ovMode == 'always') {
      rule['override'] = {'mode': 'always'};
    } else if (_ovMode == 'window') {
      final v = double.tryParse(_ovValue.text.trim());
      if (v == null || !v.isFinite || v <= 0) return null;
      var days = 1.0;
      switch (_ovUnit) {
        case 'seconds':
          days = 1.0 / 86400;
          break;
        case 'minutes':
          days = 1.0 / 1440;
          break;
        case 'hours':
          days = 1.0 / 24;
          break;
      }
      if (v * days > 30) return null; // the hub allows at most 30 days
      rule['override'] = {
        'mode': 'window',
        'value': v == v.roundToDouble() ? v.toInt() : v,
        'unit': _ovUnit,
      };
    } else {
      rule['override'] = null;
    }
    return rule;
  }

  /// The live sentence. Deliberately built HERE from the same parts the
  /// hub words its own way — the two agreeing is what makes the preview
  /// trustworthy, and the hub's version is what gets stored.
  String _sentence(RuleBuilderFrame f) {
    final option = _whenOption;
    if (option == null) return 'Pick something to start it.';
    // A TIME NAMES NO DEVICE. The hub words this one "at 07:00" (see
    // describe() in automations.py) and the preview says the same, which
    // is the whole point of building the sentence here.
    // Each of these says exactly what the hub's own describe() says for the
    // same rule (automations.py), which is the whole point of building the
    // sentence here: the preview and the list must not disagree.
    var head = 'When ${_label(f, _whenDevice)} ${option['label']}';
    if (option['needs'] == 'time') {
      head = 'At ${_whenAt.text.trim()}';
    } else if (option['needs'] == 'tag') {
      head = 'When ${_label(f, _whenDevice)} reads '
          '${_whenTag.text.trim().toUpperCase()}';
    }
    if (option['needs'] == 'number') {
      head += ' ${_whenNumber.text.trim()}';
    } else if (option['needs'] == 'minutes') {
      head += ' ${_whenMinutes.text.trim()} minutes';
    }
    var s = head;
    final saidIfs = [
      for (final l in _ifs)
        if (l.kind != 'none') l.say((d) => _targetLabel(f, d)),
    ].where((x) => x.isNotEmpty).toList();
    s += saidIfs.isEmpty ? ',' : ', and ${saidIfs.join(' and ')},';
    final then = _then.map((r) => r.say(f)).where((x) => x.isNotEmpty);
    s += ' ${then.isEmpty ? 'do nothing' : then.join(' and ')}';
    if (_loop) s += ', and repeat';
    final els = _else.map((r) => r.say(f)).where((x) => x.isNotEmpty);
    if (els.isNotEmpty) s += ' — otherwise ${els.join(' and ')}';
    final rev = _revert.map((r) => r.say(f)).where((x) => x.isNotEmpty);
    if (rev.isNotEmpty) {
      s += ' — and ${rev.join(' and ')} once it has been quiet for '
          '${_revertMinutes.text.trim()} minutes';
    }
    return '$s.';
  }

  String _label(RuleBuilderFrame f, String? did) {
    final d = (f.devices[did] as Map?)?.cast<String, dynamic>();
    return (d?['name'] ?? did ?? '?').toString();
  }

  String _targetLabel(RuleBuilderFrame f, String? did) {
    for (final t in f.targets) {
      if (t is Map && t['value'] == did) return (t['label'] ?? did).toString();
    }
    return did ?? '?';
  }

  // ── build ──────────────────────────────────────────────────────────

  /// The BLOCKS ONLY. No Scaffold, no AppBar, no system-UI styling: this
  /// is a body now, hosted by the Automations screen, because the list
  /// and the builder are one place (her merge, 2026-08-27) rather than
  /// two screens that happen to be about the same thing.
  @override
  Widget build(BuildContext context) {
    final s = widget.appState;
    final f = s.ruleBuilder;
    if (f == null) {
      return Center(
        child: Text('This automation is no longer open.',
            style: TextStyle(color: s.inkDim, fontSize: 14)),
      );
    }
    if (!_loaded) _loadFrom(f);
    // What the voice scripts read their vocabularies from. Refreshed on
    // every build so a re-armed list is always what is on screen; a plain
    // assignment, so it is safe to do here (nothing notifies).
    _voiceFrame = f;
    return Column(
      children: [
        Expanded(
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 4, 16, 16),
            children: [
              _sentenceBox(s, f),
              const SizedBox(height: 14),
              if (f.tried.isNotEmpty) ...[
                _triedCard(s, f),
                const SizedBox(height: 12),
              ],
              _nameField(s),
              const SizedBox(height: 14),
              _whenBlock(s, f),
              const SizedBox(height: 10),
              _ifBlock(s, f),
              const SizedBox(height: 10),
              _actionBlock(
                  s, f, 'THEN', _then, Icons.play_arrow_rounded, s.accent,
                  footer: _loopSwitch(s)),
              const SizedBox(height: 10),
              _elseBlocks(s, f),
              const SizedBox(height: 10),
              _overrideBlock(s),
              const SizedBox(height: 20),
              _sentenceCard(s, f),
            ],
          ),
        ),
        _saveBar(s, f),
      ],
    );
  }

  Widget _nameField(AppState s) => TextField(
        controller: _nameCtl,
        style: TextStyle(color: s.ink, fontSize: 16),
        onChanged: (_) => setState(() {}),
        decoration: InputDecoration(
          labelText: 'What would you call this habit?',
          hintText: 'e.g. kitchen at night',
          labelStyle: TextStyle(color: s.inkDim),
          hintStyle: TextStyle(color: s.inkFaint),
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
      );

  /// One block-card: a coloured spine, its word, and its contents.
  Widget _block(AppState s, String word, IconData icon, Color tint,
      List<Widget> children) {
    return Container(
      decoration: BoxDecoration(
        color: s.surface,
        border: Border.all(color: s.border),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Container(
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
            decoration: BoxDecoration(
              color: tint.withValues(alpha: 0.12),
              borderRadius:
                  const BorderRadius.vertical(top: Radius.circular(11)),
            ),
            child: Row(children: [
              Icon(icon, size: 15, color: tint),
              const SizedBox(width: 8),
              Text(word,
                  style: TextStyle(
                      color: tint,
                      fontSize: 11.5,
                      letterSpacing: 1.0,
                      fontWeight: FontWeight.w700)),
            ]),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 12, 12, 12),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: children,
            ),
          ),
        ],
      ),
    );
  }

  /// IF I SWITCH IT MYSELF: who wins when a person commands a device this
  /// rule drives. The time is seconds, minutes, hours or days.
  Widget _overrideBlock(AppState s) {
    final hint = switch (_ovMode) {
      'always' =>
        'The rule acts even if you switched the device a moment ago.',
      'window' =>
        'After you switch a device yourself, this rule leaves it alone for '
            'this long (counted from your last command).',
      _ =>
        'You win for a short while (a minute, unless the hub is set '
            'otherwise). A time-of-day rule ignores it.',
    };
    return _block(s, 'IF I SWITCH IT MYSELF', Icons.touch_app, s.inkDim, [
      _dropdown<String>(
        s,
        value: _ovMode,
        hint: 'who wins?',
        items: const [
          DropdownMenuItem(value: 'default', child: Text('The house setting')),
          DropdownMenuItem(
              value: 'window', child: Text('I win for a time I choose')),
          DropdownMenuItem(
              value: 'always', child: Text('The automation always wins')),
        ],
        onChanged: (v) => setState(() => _ovMode = v ?? 'default'),
      ),
      if (_ovMode == 'window') ...[
        const SizedBox(height: 10),
        Row(children: [
          SizedBox(width: 90, child: _numberField(s, _ovValue, 'for')),
          const SizedBox(width: 10),
          Expanded(
            child: _dropdown<String>(
              s,
              value: _ovUnit,
              hint: 'unit',
              items: const [
                DropdownMenuItem(value: 'seconds', child: Text('seconds')),
                DropdownMenuItem(value: 'minutes', child: Text('minutes')),
                DropdownMenuItem(value: 'hours', child: Text('hours')),
                DropdownMenuItem(value: 'days', child: Text('days')),
              ],
              onChanged: (v) => setState(() => _ovUnit = v ?? 'minutes'),
            ),
          ),
        ]),
      ],
      _hint(s, hint),
    ]);
  }

  Widget _whenBlock(AppState s, RuleBuilderFrame f) {
    final options = _optionsFor(f, _whenDevice);
    return _block(s, 'WHEN', Icons.sensors, s.accent, [
      _dropdown<String>(
        s,
        value: _whenDevice,
        hint: 'pick a device',
        items: [
          // FIRST, above the house: a rule that starts from the clock
          // rather than from something happening.
          DropdownMenuItem(
            value: _kTime,
            child: Row(children: [
              Icon(Icons.schedule, size: 13, color: s.inkFaint),
              const SizedBox(width: 6),
              Text('a time of day',
                  style: TextStyle(color: s.ink, fontSize: 14)),
            ]),
          ),
          for (final entry in f.devices.entries)
            DropdownMenuItem(
              value: entry.key,
              child: _deviceRow(s, (entry.value as Map).cast<String, dynamic>()),
            ),
        ],
        onChanged: (v) => setState(() {
          _whenDevice = v;
          final opts = _optionsFor(f, v);
          _whenOption = opts.isEmpty ? null : opts.first;
          _seed(_whenOption);
        }),
      ),
      const SizedBox(height: 8),
      _dropdown<String>(
        s,
        value: _whenOption?['label']?.toString(),
        hint: options.isEmpty ? 'nothing to listen for' : 'what happens',
        items: [
          for (final o in options)
            DropdownMenuItem(
              value: o['label'].toString(),
              child: Text(o['label'].toString(),
                  style: TextStyle(color: s.ink, fontSize: 14)),
            ),
        ],
        onChanged: (v) => setState(() {
          _whenOption = options.firstWhere((o) => o['label'].toString() == v,
              orElse: () => options.first);
          _seed(_whenOption);
        }),
      ),
      if (_whenOption?['needs'] == 'number') ...[
        const SizedBox(height: 8),
        _numberField(s, _whenNumber, _bounds(_whenOption)),
      ],
      if (_whenOption?['needs'] == 'minutes') ...[
        const SizedBox(height: 8),
        _numberField(s, _whenMinutes, 'minutes'),
      ],
      if (_whenOption?['needs'] == 'time') ...[
        const SizedBox(height: 8),
        Align(alignment: Alignment.centerLeft, child: _timeChip(s, _whenAt)),
        _hint(s, 'every day at that minute, once'),
      ],
      if (_whenOption?['needs'] == 'tag') ...[
        const SizedBox(height: 8),
        _textField(s, _whenTag, 'card UID'),
        _hint(
            s,
            (_whenOption?['last'] ?? '').toString().isEmpty
                ? 'tap the card on the reader once, then reopen this — the '
                    'hub offers the UID it saw'
                : 'last card seen: ${_whenOption?['last']}'),
        // A UID IS A NAME, NOT A PROOF (the RFID law). Said here, where
        // somebody is deciding what a card may do, rather than left for
        // them to discover.
        _hint(s, 'a card is a claim, not a key — it can never open a '
            'secure device on its own'),
      ],
      // No "how it is heard" hint: the hub lists only devices it hears
      // over MQTT (`heard` is always "mqtt"), so there is nothing to warn.
    ]);
  }

  String _bounds(Map<String, dynamic>? option) {
    final b = (option?['bounds'] as Map?)?.cast<String, dynamic>() ?? {};
    if (b['range_min'] == null || b['range_max'] == null) return 'value';
    return '${b['range_min']} – ${b['range_max']}';
  }

  Widget _ifBlock(AppState s, RuleBuilderFrame f) {
    return _block(s, 'ONLY IF', Icons.filter_alt_outlined, s.inkDim, [
      for (var i = 0; i < _ifs.length; i++) ...[
        if (i > 0)
          Padding(
            padding: const EdgeInsets.only(top: 6, bottom: 2),
            child: Row(children: [
              Text('AND',
                  style: TextStyle(
                      fontSize: 11,
                      letterSpacing: 0.6,
                      fontWeight: FontWeight.w600,
                      color: s.inkFaint)),
              const SizedBox(width: 8),
              Expanded(child: Divider(color: s.border, height: 1)),
              IconButton(
                tooltip: 'remove this limit',
                icon: Icon(Icons.close, size: 16, color: s.cancelInk),
                onPressed: () {
                  final gone = _ifs[i];
                  setState(() => _ifs.removeAt(i));
                  WidgetsBinding.instance
                      .addPostFrameCallback((_) => gone.dispose());
                },
              ),
            ]),
          ),
        _limitEditor(s, f, i),
      ],
      if (_ifKind != 'none')
        Align(
          alignment: Alignment.centerLeft,
          child: TextButton.icon(
            onPressed: () => setState(() => _ifs.add(_Limit('days'))),
            icon: Icon(Icons.add, size: 16, color: s.inkDim),
            label: Text('add another limit',
                style: TextStyle(color: s.inkDim, fontSize: 12.5)),
          ),
        ),
    ]);
  }

  Widget _radioRow(AppState s, String label, bool on, VoidCallback tap) =>
      InkWell(
        onTap: tap,
        borderRadius: BorderRadius.circular(8),
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 7),
          child: Row(children: [
            Icon(on ? Icons.radio_button_checked : Icons.radio_button_unchecked,
                size: 20, color: on ? s.accent : s.inkFaint),
            const SizedBox(width: 10),
            Expanded(
                child: Text(label,
                    style: TextStyle(color: s.ink, fontSize: 14))),
          ]),
        ),
      );

  Widget _limitEditor(AppState s, RuleBuilderFrame f, int i) {
    final l = _ifs[i];
    final kinds = <(String, String)>[
      if (i == 0) ('none', 'No limit'),
      ('time_range', 'Between two times'),
      ('days', 'On certain days'),
      ('device', 'While a device is on or off'),
      ('reading', 'While a reading is above or below'),
    ];
    final readable = [
      for (final t in f.targets)
        if (t is Map && ((t['readings'] as List?) ?? const []).isNotEmpty) t,
    ];
    final keys = [
      for (final t in readable)
        if (t['value'] == l.device)
          for (final k in (t['readings'] as List)) k.toString(),
    ];
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      for (final (k, label) in kinds)
        _radioRow(s, label, l.kind == k, () => setState(() => l.kind = k)),
      if (l.kind == 'time_range')
        Padding(
          padding: const EdgeInsets.only(left: 30, top: 4, bottom: 4),
          child: Row(children: [
            _timeChip(s, l.after),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 10),
              child: Text('to', style: TextStyle(color: s.inkDim)),
            ),
            _timeChip(s, l.before),
          ]),
        ),
      if (l.kind == 'days')
        Padding(
          padding: const EdgeInsets.only(left: 30, top: 4),
          child: Wrap(spacing: 6, runSpacing: 6, children: [
            for (var d = 0; d < 7; d++)
              FilterChip(
                label: Text(_Limit.shortDays[d]),
                selected: l.days.contains(d),
                onSelected: (on) => setState(
                    () => on ? l.days.add(d) : l.days.remove(d)),
              ),
            ActionChip(
              label: const Text('Weekdays'),
              onPressed: () => setState(() => l.days
                ..clear()
                ..addAll(const [0, 1, 2, 3, 4])),
            ),
            ActionChip(
              label: const Text('Weekend'),
              onPressed: () => setState(() => l.days
                ..clear()
                ..addAll(const [5, 6])),
            ),
          ]),
        ),
      if (l.kind == 'device') ...[
        const SizedBox(height: 6),
        _dropdown<String>(
          s,
          value: l.device,
          hint: 'which device',
          items: [
            for (final t in f.targets)
              if (t is Map)
                DropdownMenuItem(
                  value: t['value'].toString(),
                  child: Text(t['label'].toString(),
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(color: s.ink, fontSize: 14)),
                ),
          ],
          onChanged: (v) => setState(() => l.device = v),
        ),
        const SizedBox(height: 6),
        Wrap(spacing: 8, children: [
          ChoiceChip(
              label: const Text('is on'),
              selected: l.equals,
              onSelected: (_) => setState(() => l.equals = true)),
          ChoiceChip(
              label: const Text('is off'),
              selected: !l.equals,
              onSelected: (_) => setState(() => l.equals = false)),
        ]),
      ],
      if (l.kind == 'reading') ...[
        const SizedBox(height: 6),
        _dropdown<String>(
          s,
          value: readable.any((t) => t['value'] == l.device) ? l.device : null,
          hint: readable.isEmpty ? 'nothing reports a reading yet' : 'which device',
          items: [
            for (final t in readable)
              DropdownMenuItem(
                value: t['value'].toString(),
                child: Text(t['label'].toString(),
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(color: s.ink, fontSize: 14)),
              ),
          ],
          onChanged: (v) => setState(() {
            l.device = v;
            l.key = null;
          }),
        ),
        if (keys.isNotEmpty) ...[
          const SizedBox(height: 6),
          _dropdown<String>(
            s,
            value: keys.contains(l.key) ? l.key : null,
            hint: 'which reading',
            items: [
              for (final k in keys)
                DropdownMenuItem(
                  value: k,
                  child: Text(k.replaceAll('_', ' '),
                      style: TextStyle(color: s.ink, fontSize: 14)),
                ),
            ],
            onChanged: (v) => setState(() => l.key = v),
          ),
        ],
        const SizedBox(height: 6),
        Row(children: [
          ChoiceChip(
              label: const Text('above'),
              selected: l.edge == 'above',
              onSelected: (_) => setState(() => l.edge = 'above')),
          const SizedBox(width: 8),
          ChoiceChip(
              label: const Text('below'),
              selected: l.edge == 'below',
              onSelected: (_) => setState(() => l.edge = 'below')),
          const SizedBox(width: 10),
          Expanded(child: _numberField(s, l.number, 'number')),
        ]),
      ],
    ]);
  }

  /// A time of day, picked on a clock face (never typed as HH:MM).
  Widget _timeChip(AppState s, TextEditingController c) => OutlinedButton(
        onPressed: () async {
          final parts = c.text.trim().split(':');
          final start = TimeOfDay(
              hour: (int.tryParse(parts.first) ?? 7).clamp(0, 23),
              minute: parts.length > 1
                  ? (int.tryParse(parts[1]) ?? 0).clamp(0, 59)
                  : 0);
          final t = await showTimePicker(
            context: context,
            initialTime: start,
            builder: (ctx, child) => MediaQuery(
              data: MediaQuery.of(ctx).copyWith(alwaysUse24HourFormat: true),
              child: child!,
            ),
          );
          if (t == null || !mounted) return;
          setState(() => c.text = '${t.hour.toString().padLeft(2, '0')}:'
              '${t.minute.toString().padLeft(2, '0')}');
        },
        style: OutlinedButton.styleFrom(
          foregroundColor: s.ink,
          side: BorderSide(color: s.accent),
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
          shape:
              RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
        ),
        child: Text(c.text.trim().isEmpty ? 'pick a time' : c.text.trim(),
            style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600)),
      );

  /// Type (or dictate) the whole automation in one sentence; the hub fills
  /// the boxes and says which ones to check.
  Widget _sentenceBox(AppState s, RuleBuilderFrame f) {
    final names = {'when': 'WHEN', 'only if': 'ONLY IF', 'do': 'DO'};
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      TextField(
        controller: _sayCtl,
        minLines: 1,
        maxLines: 4,
        textInputAction: TextInputAction.done,
        onSubmitted: (t) => s.fillRuleFromSentence(t),
        onChanged: (_) => setState(() {}),
        style: TextStyle(color: s.ink, fontSize: 14.5, height: 1.35),
        decoration: InputDecoration(
          hintText: 'Say or type it, e.g. When the motion sensor turns on, '
              'turn on the hall light',
          hintStyle: TextStyle(color: s.inkFaint, fontSize: 13.5),
          suffixIcon: IconButton(
            tooltip: 'Fill the boxes',
            icon: Icon(Icons.auto_fix_high, color: s.accent),
            onPressed: _sayCtl.text.trim().isEmpty
                ? null
                : () => s.fillRuleFromSentence(_sayCtl.text),
          ),
          enabledBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(12),
              borderSide: BorderSide(color: s.accent, width: 1.2)),
          focusedBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(12),
              borderSide: BorderSide(color: s.accent, width: 1.6)),
        ),
      ),
      Padding(
        padding: const EdgeInsets.only(top: 6, left: 2),
        child: Text(
          f.heard.isEmpty
              ? 'The boxes below fill themselves in. Or skip this and tap them.'
              : f.unsure.isEmpty
                  ? 'Filled in from your sentence. Check the boxes, then save.'
                  : 'Check ${f.unsure.map((u) => names[u] ?? u).join(' and ')}: '
                      'I could not tell from the sentence. Tap to fix.',
          style: TextStyle(
              color: f.unsure.isEmpty ? s.inkFaint : const Color(0xFFD29922),
              fontSize: 11.5,
              height: 1.35),
        ),
      ),
    ]);
  }

  /// What "Try it now" did, at the top of the blocks.
  Widget _triedCard(AppState s, RuleBuilderFrame f) => Container(
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: s.accent.withValues(alpha: 0.08),
          border: Border.all(color: s.border),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Icon(Icons.play_circle_outline, size: 18, color: s.inkDim),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
                'Tried it (not saved): ${f.tried.join('; ')}',
                style: TextStyle(color: s.ink, fontSize: 13, height: 1.35)),
          ),
        ]),
      );

  Widget _actionBlock(AppState s, RuleBuilderFrame f, String word,
      List<_ActionRow> rows, IconData icon, Color tint,
      {Widget? header, Widget? footer}) {
    return _block(s, word, icon, tint, [
      if (header != null) header,
      for (var i = 0; i < rows.length; i++) ...[
        if (i > 0) const SizedBox(height: 10),
        _actionRow(s, f, rows, i),
      ],
      const SizedBox(height: 8),
      Align(
        alignment: Alignment.centerLeft,
        child: TextButton.icon(
          onPressed: () => setState(() => rows.add(_ActionRow())),
          icon: Icon(Icons.add, size: 16, color: tint),
          label: Text('add another',
              style: TextStyle(color: tint, fontSize: 12.5)),
        ),
      ),
      if (footer != null) footer,
    ]);
  }

  /// The LOOP switch under THEN, and its hint when there is no Wait.
  Widget _loopSwitch(AppState s) {
    final noWait = _loop && !_then.any((r) => r.kind == 'wait');
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Row(children: [
        Expanded(
          child: Text(
              'Loop — THEN runs again and again until this automation '
              'is switched off',
              style: TextStyle(color: s.inkDim, fontSize: 12.5)),
        ),
        Switch(
          value: _loop,
          activeThumbColor: s.accent,
          onChanged: (v) => setState(() => _loop = v),
        ),
      ]),
      if (noWait)
        _hint(s, "add a Wait so it doesn't run flat out", warn: true),
    ]);
  }

  /// The time wheel for a Wait row (the same sheet as a display's page
  /// time): hours, minutes, seconds; at least one second.
  Future<double?> _pickWait(AppState s, double start) async {
    var picked = Duration(seconds: start.round());
    final ok = await showModalBottomSheet<bool>(
      context: context,
      backgroundColor: s.surface,
      builder: (ctx) => SafeArea(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          SizedBox(
            height: 216,
            child: CupertinoTheme(
              data: CupertinoThemeData(
                  brightness: s.isLight ? Brightness.light : Brightness.dark),
              child: CupertinoTimerPicker(
                mode: CupertinoTimerPickerMode.hms,
                initialTimerDuration: picked,
                onTimerDurationChanged: (d) => picked = d,
              ),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
            child: Row(children: [
              Expanded(
                child: TextButton(
                  onPressed: () => Navigator.pop(ctx, false),
                  child: Text('Cancel', style: TextStyle(color: s.cancelInk)),
                ),
              ),
              Expanded(
                child: FilledButton(
                  style: FilledButton.styleFrom(
                      backgroundColor: s.accent, foregroundColor: s.accentFg),
                  onPressed: () => Navigator.pop(ctx, true),
                  child: const Text('Set'),
                ),
              ),
            ]),
          ),
        ]),
      ),
    );
    if (ok != true) return null;
    final secs = picked.inSeconds.toDouble();
    return secs < 1 ? 1.0 : secs;
  }

  Widget _actionRow(AppState s, RuleBuilderFrame f, List<_ActionRow> rows,
      int i) {
    final row = rows[i];
    final target = _targetFor(f, row.device);
    final secure = target?['secure'] == true;
    final what =
        const {'device', 'press', 'display'}.contains(row.kind) ? 'device' : row.kind;
    DropdownMenuItem<String> item(String v, String label) => DropdownMenuItem(
        value: v,
        child: Text(label, style: TextStyle(color: s.ink, fontSize: 14)));
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(children: [
          Expanded(
            child: _dropdown<String>(
              s,
              value: what,
              hint: 'do what',
              items: [
                item('device', 'use a device'),
                item('scene', 'run a scene'),
                item('notify', 'send a notification'),
                item('ask', 'ask me to approve'),
                // A pause between steps (not in the quiet-revert).
                if (!identical(rows, _revert)) item('wait', 'wait a while'),
              ],
              onChanged: (v) => setState(() {
                row.kind = v ?? 'device';
                if (row.kind == 'device') {
                  row.fitTo(_useKind(_targetFor(f, row.device)));
                }
              }),
            ),
          ),
          // THEN keeps at least one row; ELSE or revert can be emptied.
          if (rows.length > 1 || !identical(rows, _then))
            IconButton(
              tooltip: 'remove',
              icon: Icon(Icons.close, size: 16, color: s.cancelInk),
              onPressed: () {
                final gone = rows[i];
                setState(() => rows.removeAt(i));
                WidgetsBinding.instance
                    .addPostFrameCallback((_) => gone.dispose());
              },
            ),
        ]),
        const SizedBox(height: 8),
        if (row.kind == 'scene')
          _dropdown<String>(
            s,
            value: row.scene,
            hint: 'which scene',
            items: [
              for (final sc in f.scenes)
                if (sc is Map)
                  DropdownMenuItem(
                    value: sc['value'].toString(),
                    child: Text(sc['label'].toString(),
                        style: TextStyle(color: s.ink, fontSize: 14)),
                  ),
            ],
            onChanged: (v) => setState(() => row.scene = v),
          )
        else if (row.kind == 'notify')
          _textField(s, row.text, 'what should it say')
        else if (row.kind == 'wait')
          Align(
            alignment: Alignment.centerLeft,
            child: ActionChip(
              avatar: Icon(Icons.hourglass_bottom, size: 16, color: s.inkDim),
              label: Text('Wait ${_ActionRow.waitChip(row.waitS)}'),
              onPressed: () async {
                final t = await _pickWait(s, row.waitS);
                if (t != null && mounted) setState(() => row.waitS = t);
              },
            ),
          )
        else ...[
          _dropdown<String>(
            s,
            value: row.device,
            hint: 'which device',
            items: [
              for (final t in f.targets)
                if (t is Map &&
                    (row.kind == 'ask' ||
                        !const {'reading', 'none'}
                            .contains(_useKind(t.cast<String, dynamic>()))))
                  DropdownMenuItem(
                    value: t['value'].toString(),
                    child: Row(children: [
                      if (t['secure'] == true) ...[
                        const Icon(Icons.shield_outlined,
                            size: 13, color: Color(0xFFD29922)),
                        const SizedBox(width: 6),
                      ],
                      Flexible(
                        child: Text(t['label'].toString(),
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(color: s.ink, fontSize: 14)),
                      ),
                    ]),
                  ),
            ],
            onChanged: (v) => setState(() {
              row.device = v;
              row.reset();
              if (row.kind != 'ask') row.fitTo(_useKind(_targetFor(f, v)));
            }),
          ),
          if (row.device != null) ...[
            const SizedBox(height: 10),
            if (row.kind == 'ask')
              _onOffChips(s, row, flip: false)
            else
              _controlsFor(s, row, target),
          ],
          if (secure && row.kind == 'device' && _whenOption?['card'] == true)
            _hint(
                s,
                'a secure device: it opens only for a card whose owner '
                'allowed it in their Profile (My card opens); other cards '
                'are refused.',
                warn: true)
          else if (secure && row.kind != 'ask')
            _hint(
                s,
                'this is a secure device — an automation may make it safer, '
                'never less. Use “ask me to approve” for the other direction.',
                warn: true),
          if (secure && row.kind == 'ask')
            _hint(s,
                'the hub will ask you, and your PIN answers — nothing moves '
                'on its own'),
        ],
      ],
    );
  }

  String _useKind(Map<String, dynamic>? target) =>
      ((target?['use'] as Map?)?['kind'] ?? '').toString();

  static const _palette = <List<int>>[
    [255, 214, 170], [255, 160, 40], [255, 40, 30], [255, 60, 160],
    [150, 70, 255], [40, 110, 255], [0, 200, 220], [40, 200, 90],
  ];

  Widget _chip(String label, bool on, VoidCallback tap) => ChoiceChip(
        label: Text(label),
        selected: on,
        onSelected: (_) => setState(tap),
      );

  Widget _onOffChips(AppState s, _ActionRow row,
          {bool flip = true, String onWord = 'On', String offWord = 'Off'}) =>
      Wrap(spacing: 8, runSpacing: 6, children: [
        _chip(onWord, row.on && !row.flip && !row.levelOnly, () {
          row.on = true;
          row.levelOnly = false;
        }),
        _chip(offWord, !row.on && !row.flip && !row.levelOnly, () {
          row.on = false;
          row.levelOnly = false;
        }),
        if (flip)
          _chip('Flip it', row.flip, () {
            row.flip = true;
            row.levelOnly = false;
          }),
      ]);

  Widget _levelSlider(AppState s, _ActionRow row, String label,
      {double min = 0, double max = 100, double? start}) {
    final value = (row.level ?? start ?? max).clamp(min, max).toDouble();
    row.level ??= value;
    return Row(children: [
      SizedBox(
        width: 84,
        child: Text('$label ${value.round()}',
            style: TextStyle(color: s.inkDim, fontSize: 12.5)),
      ),
      Expanded(
        child: Slider(
          value: value,
          min: min,
          max: max,
          activeColor: s.accent,
          onChanged: (v) => setState(() => row.level = v),
        ),
      ),
    ]);
  }

  /// The controls that fit the device -- the same as its page in Devices.
  Widget _controlsFor(
      AppState s, _ActionRow row, Map<String, dynamic>? target) {
    final use = ((target?['use'] as Map?) ?? const {}).cast<String, dynamic>();
    final kind = (use['kind'] ?? '').toString();
    final min = (use['min'] as num?)?.toDouble() ?? 0;
    final max = (use['max'] as num?)?.toDouble() ?? 100;
    switch (kind) {
      case 'display':
        final d = ((use['display'] as Map?) ?? const {}).cast<String, dynamic>();
        return DisplayWriter(
          key: ValueKey('writer-${row.device}-${identityHashCode(row)}'),
          s: s,
          deviceId: row.device!,
          rows: (d['rows'] as num?)?.toInt() ?? 2,
          cols: (d['cols'] as num?)?.toInt() ?? 16,
          initial: row.display,
          onChanged: (a) => setState(() => row.display = a),
        );
      case 'ir':
        final buttons = [
          for (final b in (use['buttons'] as List? ?? const [])) b.toString()
        ];
        if (buttons.isEmpty) {
          return _hint(s, 'no buttons learned for it yet');
        }
        return Wrap(spacing: 8, runSpacing: 6, children: [
          for (final b in buttons)
            _chip(b.replaceAll('_', ' '), row.button == b,
                () => row.button = b),
        ]);
      case 'press':
        return _hint(s, 'it is pressed when this runs');
      case 'dimmer':
        return Column(children: [
          _onOffChips(s, row),
          if (row.on && !row.flip)
            _levelSlider(s, row, 'Brightness', min: min, max: max),
        ]);
      case 'colour':
        return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          _onOffChips(s, row),
          if (row.on && !row.flip) ...[
            const SizedBox(height: 8),
            Wrap(spacing: 8, runSpacing: 8, children: [
              for (final c in _palette)
                GestureDetector(
                  onTap: () => setState(() => row.colour = c),
                  child: Container(
                    width: 30,
                    height: 30,
                    decoration: BoxDecoration(
                      color: Color.fromARGB(255, c[0], c[1], c[2]),
                      shape: BoxShape.circle,
                      border: Border.all(
                          color: row.colour != null &&
                                  row.colour![0] == c[0] &&
                                  row.colour![1] == c[1] &&
                                  row.colour![2] == c[2]
                              ? s.ink
                              : s.border,
                          width: 2),
                    ),
                  ),
                ),
            ]),
            _levelSlider(s, row, 'Brightness'),
          ],
        ]);
      case 'cover':
        return Column(children: [
          Wrap(spacing: 8, runSpacing: 6, children: [
            _chip('Open', row.on && !row.levelOnly && !row.flip, () {
              row.on = true;
              row.levelOnly = false;
            }),
            _chip('Close', !row.on && !row.levelOnly && !row.flip, () {
              row.on = false;
              row.levelOnly = false;
            }),
            _chip('Position', row.levelOnly, () {
              row.flip = false;
              row.levelOnly = true;
            }),
          ]),
          if (row.levelOnly) _levelSlider(s, row, 'Open %', start: 50),
        ]);
      case 'servo':
        row.levelOnly = true;
        return _levelSlider(s, row, 'Angle', min: min, max: max,
            start: (min + max) / 2);
      case 'switch':
        return _onOffChips(s, row);
      default:
        // An older hub (no `use`): on, off, flip, as before.
        return _onOffChips(s, row);
    }
  }

  Map<String, dynamic>? _targetFor(RuleBuilderFrame f, String? did) {
    for (final t in f.targets) {
      if (t is Map && t['value'] == did) return t.cast<String, dynamic>();
    }
    return null;
  }

  Widget _elseBlocks(AppState s, RuleBuilderFrame f) {
    return Column(children: [
      // Each ELSE shape is offered while it is absent, so a rule can carry
      // both (the hub allows ELSE and the revert together).
      if (_else.isEmpty || _revert.isEmpty)
        Row(children: [
          if (_else.isEmpty)
          Expanded(
            child: OutlinedButton.icon(
              style: OutlinedButton.styleFrom(
                foregroundColor: s.inkDim,
                side: BorderSide(color: s.border),
                padding: const EdgeInsets.symmetric(vertical: 12),
                shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(10)),
              ),
              onPressed: _ifKind == 'none'
                  ? null
                  : () => setState(() => _else.add(_ActionRow())),
              icon: const Icon(Icons.alt_route, size: 15),
              label: const Text('ELSE', style: TextStyle(fontSize: 12.5)),
            ),
          ),
          if (_else.isEmpty && _revert.isEmpty) const SizedBox(width: 10),
          if (_revert.isEmpty)
          Expanded(
            child: OutlinedButton.icon(
              style: OutlinedButton.styleFrom(
                foregroundColor: s.inkDim,
                side: BorderSide(color: s.border),
                padding: const EdgeInsets.symmetric(vertical: 12),
                shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(10)),
              ),
              onPressed: () => setState(() => _revert.add(_ActionRow())),
              icon: const Icon(Icons.history, size: 15),
              label: const Text('ELSE AFTER quiet',
                  style: TextStyle(fontSize: 12.5)),
            ),
          ),
        ]),
      if (_else.isEmpty && _ifKind == 'none')
        _hint(s, 'an ELSE needs an ONLY IF above'),
      if (_else.isEmpty || _revert.isEmpty) const SizedBox(height: 10),
      if (_else.isNotEmpty) ...[
        _actionBlock(s, f, 'ELSE', _else, Icons.alt_route, s.inkDim,
            header: _hint(s, 'when the ONLY IF above does not hold')),
        const SizedBox(height: 10),
      ],
      if (_revert.isNotEmpty)
        _actionBlock(s, f, 'ELSE AFTER QUIET', _revert, Icons.history,
            s.inkDim,
            header: Padding(
              padding: const EdgeInsets.only(bottom: 10),
              child: Row(children: [
                SizedBox(
                    width: 64,
                    child: _numberField(s, _revertMinutes, 'min')),
                const SizedBox(width: 10),
                Expanded(
                  child: Text('minutes of quiet, then:',
                      style: TextStyle(color: s.inkDim, fontSize: 12.5)),
                ),
              ]),
            )),
    ]);
  }

  Widget _sentenceCard(AppState s, RuleBuilderFrame f) {
    // The hub's own complaint, if it refused the last attempt — shown
    // WITH the blocks kept, so nothing is retyped.
    final note = (s.lastFlowMessage ?? '').trim();
    return Column(children: [
      Container(
        width: double.infinity,
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: s.accent.withValues(alpha: 0.08),
          border: Border.all(color: s.accent.withValues(alpha: 0.35)),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Text(_sentence(f),
            style: TextStyle(
                color: s.ink, fontSize: 14.5, height: 1.45,
                fontStyle: FontStyle.italic)),
      ),
      if (note.isNotEmpty && !s.menuBusy) ...[
        const SizedBox(height: 10),
        Text(note,
            textAlign: TextAlign.center,
            style: const TextStyle(
                color: Color(0xFFD29922),
                fontSize: 13,
                fontWeight: FontWeight.w600)),
      ],
    ]);
  }

  Widget _saveBar(AppState s, RuleBuilderFrame f) {
    final ready = _compose() != null && _nameCtl.text.trim().isNotEmpty;
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
        child: Row(children: [
          Expanded(
            child: OutlinedButton(
              style: s.cancelButtonStyle(),
              onPressed: () => s.sendRuleBuilder(null),
              child: const Text('Cancel',
                  style:
                      TextStyle(fontWeight: FontWeight.w600, fontSize: 15)),
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: OutlinedButton(
              style: OutlinedButton.styleFrom(
                foregroundColor: s.ink,
                side: BorderSide(color: s.accent, width: 1.5),
                padding: const EdgeInsets.symmetric(vertical: 14),
                shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(12)),
              ),
              onPressed: _compose() != null
                  ? () => s.tryRuleBuilder({
                        ..._compose()!,
                        if (f.rule?['id'] != null) 'id': f.rule!['id'],
                        'name': _nameCtl.text.trim().isEmpty
                            ? 'This automation'
                            : _nameCtl.text.trim(),
                      })
                  : null,
              child: const Text('Try it now',
                  maxLines: 1,
                  style: TextStyle(fontWeight: FontWeight.w600, fontSize: 14)),
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            flex: 2,
            child: FilledButton(
              style: FilledButton.styleFrom(
                backgroundColor: s.accent,
                foregroundColor: s.accentFg,
                disabledBackgroundColor: s.border,
                disabledForegroundColor: s.inkFaint,
                padding: const EdgeInsets.symmetric(vertical: 14),
                shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(12)),
              ),
              onPressed: ready ? () => s.sendRuleBuilder(_compose()) : null,
              child: Text(ready ? 'Save' : 'Finish the blocks',
                  style: const TextStyle(
                      fontWeight: FontWeight.w700, fontSize: 15)),
            ),
          ),
        ]),
      ),
    );
  }

  // ── small parts ────────────────────────────────────────────────────

  Widget _deviceRow(AppState s, Map<String, dynamic> d) {
    return Row(children: [
      Flexible(
        child: Text(
          [d['name'], if ((d['room'] ?? '').toString().isNotEmpty) d['room']]
              .join(' — '),
          overflow: TextOverflow.ellipsis,
          style: TextStyle(color: s.ink, fontSize: 14),
        ),
      ),
    ]);
  }

  Widget _dropdown<T>(AppState s,
      {required T? value,
      required String hint,
      required List<DropdownMenuItem<T>> items,
      required ValueChanged<T?> onChanged}) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12),
      decoration: BoxDecoration(
        color: s.bg,
        border: Border.all(color: s.border),
        borderRadius: BorderRadius.circular(9),
      ),
      child: DropdownButtonHideUnderline(
        child: DropdownButton<T>(
          // A value no item carries (a device no longer heard, a deleted
          // scene, a row past a smaller panel) trips DropdownButton's
          // exactly-one-item assert; show the hint instead.
          value: items.where((i) => i.value == value).length == 1
              ? value
              : null,
          isExpanded: true,
          dropdownColor: s.surface,
          iconEnabledColor: s.inkDim,
          hint: Text(hint,
              style: TextStyle(color: s.inkFaint, fontSize: 14)),
          items: items,
          onChanged: items.isEmpty ? null : onChanged,
        ),
      ),
    );
  }

  Widget _textField(AppState s, TextEditingController c, String label) =>
      TextField(
        controller: c,
        autocorrect: false,
        enableIMEPersonalizedLearning: false,
        style: TextStyle(color: s.ink, fontSize: 14),
        onChanged: (_) => setState(() {}),
        decoration: _decoration(s, label),
      );

  Widget _numberField(AppState s, TextEditingController c, String label) =>
      TextField(
        controller: c,
        keyboardType: const TextInputType.numberWithOptions(decimal: true),
        style: TextStyle(color: s.ink, fontSize: 14),
        onChanged: (_) => setState(() {}),
        decoration: _decoration(s, label),
      );

  InputDecoration _decoration(AppState s, String label) => InputDecoration(
        isDense: true,
        labelText: label,
        labelStyle: TextStyle(color: s.inkDim, fontSize: 12.5),
        filled: true,
        fillColor: s.bg,
        contentPadding:
            const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
        border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(9),
            borderSide: BorderSide(color: s.border)),
        enabledBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(9),
            borderSide: BorderSide(color: s.border)),
        focusedBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(9),
            borderSide: BorderSide(color: s.accent, width: 1.3)),
      );

  Widget _hint(AppState s, String text, {bool warn = false}) => Padding(
        padding: const EdgeInsets.only(top: 8),
        child: Text(text,
            style: TextStyle(
                color: warn ? const Color(0xFFD29922) : s.inkFaint,
                fontSize: 11.5,
                height: 1.35,
                fontWeight: warn ? FontWeight.w600 : FontWeight.w400)),
      );
}

/// One THEN/ELSE row's state, and how it becomes an action.
class _ActionRow {
  /// device | press | display | scene | notify | ask | wait
  String kind = 'device';
  String? scene;
  String? device;

  /// A Wait row's pause, in seconds (Arduino's delay()).
  double waitS = 5;

  /// The direction a `device`/`ask` row sets. Setting it clears [flip].
  bool _on = true;
  bool get on => _on;
  set on(bool v) {
    _on = v;
    flip = false;
  }

  /// `set: {"on": "toggle"}`, the hub's flip. A STRING on the wire, never
  /// coerced to a bool (that saved a flip back as "off"). Device actions only.
  bool flip = false;

  /// Brightness (with on), a curtain's position or a servo's angle.
  double? level;

  /// Only a level is set: a curtain's position, a servo's angle.
  bool levelOnly = false;

  /// A colour light's colour, [r, g, b].
  List<int>? colour;

  /// An IR appliance's learned button.
  String? button;

  /// A display's message: pages, page_s, clock_between, for_s (or the
  /// older text/row/for_s, kept as it was until it is edited).
  Map<String, dynamic>? display;

  final TextEditingController text = TextEditingController();

  /// Kept for the save check; the display writer always has a valid time.
  bool get badHold => false;

  _ActionRow();

  void reset() {
    _on = true;
    flip = false;
    level = null;
    levelOnly = false;
    colour = null;
    button = null;
    display = null;
  }

  /// The row's kind from what the device is (its `use`).
  void fitTo(String useKind) {
    if (useKind == 'display') {
      kind = 'display';
    } else if (useKind == 'ir' || useKind == 'press') {
      kind = 'press';
    } else {
      kind = 'device';
      if (useKind == 'servo') levelOnly = true;
    }
  }

  factory _ActionRow.fromJson(Map<String, dynamic> a) {
    final r = _ActionRow();
    r.kind = (a['kind'] ?? 'device').toString();
    r.scene = a['scene']?.toString();
    r.device = a['device']?.toString();
    final set = (a['set'] as Map?)?.cast<String, dynamic>() ?? const {};
    r.on = set['on'] == true;
    r.flip = set['on'] == 'toggle';
    r.level = (set['level'] as num?)?.toDouble();
    r.levelOnly = !set.containsKey('on') && set.containsKey('level');
    final c = set['colour'];
    if (c is List && c.length == 3) {
      r.colour = [for (final v in c) (v as num).toInt()];
    }
    r.button = a['button']?.toString();
    if (r.kind == 'notify') r.text.text = (a['text'] ?? '').toString();
    if (r.kind == 'wait') r.waitS = (a['s'] as num?)?.toDouble() ?? 5;
    if (r.kind == 'display') {
      r.display = Map<String, dynamic>.from(a)
        ..remove('kind')
        ..remove('device');
    }
    return r;
  }

  void dispose() {
    text.dispose();
  }

  bool get _displayHasWords {
    final d = display;
    if (d == null) return false;
    if ((d['text'] ?? '').toString().trim().isNotEmpty) return true;
    for (final p in (d['pages'] as List? ?? const [])) {
      for (final l in (p is List ? p : const [])) {
        if (l is Map && (l['text'] ?? '').toString().trim().isNotEmpty) {
          return true;
        }
      }
    }
    return false;
  }

  /// "5 s", "5 min", "1 h 30 min": the Wait chip.
  static String waitChip(double secs) {
    final whole = secs.floor();
    final h = whole ~/ 3600, m = (whole % 3600) ~/ 60;
    final rest = secs - h * 3600 - m * 60;
    final sec = rest == rest.roundToDouble()
        ? '${rest.toInt()}'
        : rest.toStringAsFixed(1);
    return [
      if (h > 0) '$h h',
      if (m > 0) '$m min',
      if (rest > 0 || (h == 0 && m == 0)) '$sec s',
    ].join(' ');
  }

  /// "5 seconds", "2 minutes", "90 seconds": as the hub says it.
  static String waitWords(double secs) {
    for (final u in const [
      [86400, 'day'],
      [3600, 'hour'],
      [60, 'minute']
    ]) {
      final size = u[0] as int;
      if (secs >= size && secs % size == 0) {
        final n = secs ~/ size;
        return '$n ${u[1]}${n != 1 ? 's' : ''}';
      }
    }
    final n = secs == secs.roundToDouble()
        ? '${secs.toInt()}'
        : secs.toStringAsFixed(1);
    return '$n second${secs != 1 ? 's' : ''}';
  }

  Map<String, dynamic>? toJson() {
    switch (kind) {
      case 'wait':
        return {'kind': 'wait', 's': waitS};
      case 'scene':
        return scene == null ? null : {'kind': 'scene', 'scene': scene};
      case 'notify':
        return text.text.trim().isEmpty
            ? null
            : {'kind': 'notify', 'text': text.text.trim()};
      case 'ask':
        return device == null
            ? null
            : {'kind': 'ask', 'device': device, 'set': {'on': on}};
      case 'press':
        return device == null
            ? null
            : {'kind': 'press', 'device': device,
               if (button != null) 'button': button};
      case 'display':
        if (device == null || !_displayHasWords) return null;
        return {'kind': 'display', 'device': device, ...display!};
      case 'device':
        if (device == null) return null;
        final Map<String, dynamic> set;
        if (flip) {
          set = {'on': 'toggle'};
        } else if (levelOnly) {
          set = {'level': (level ?? 0).round()};
        } else if (!on) {
          set = {'on': false};
        } else {
          set = {
            'on': true,
            if (level != null) 'level': level!.round(),
            if (colour != null) 'colour': colour,
          };
        }
        return {'kind': 'device', 'device': device, 'set': set};
    }
    return null;
  }

  String say(RuleBuilderFrame f) {
    String label(String? did) {
      for (final t in f.targets) {
        if (t is Map && t['value'] == did) return (t['label'] ?? did).toString();
      }
      return did ?? '?';
    }

    switch (kind) {
      case 'wait':
        return 'wait ${waitWords(waitS)}';
      case 'scene':
        return scene == null ? '' : "run '$scene'";
      case 'notify':
        return text.text.trim().isEmpty ? '' : 'send a notification';
      case 'ask':
        return device == null ? '' : 'ask me to approve ${label(device)}';
      case 'press':
        if (device == null) return '';
        return button == null
            ? 'press ${label(device)}'
            : 'press ${button!.replaceAll('_', ' ')} on ${label(device)}';
      case 'display':
        if (device == null || !_displayHasWords) return '';
        final pages = (display?['pages'] as List?)?.length ?? 1;
        final hold = display?['for_s'];
        final what = pages > 1
            ? 'show $pages pages on ${label(device)}'
            : 'show a message on ${label(device)}';
        if (display?.containsKey('for_s') == true && hold == null) {
          return '$what, sharing it with the clock';
        }
        if (hold == null || hold == 0) return '$what and leave it up';
        return '$what for ${((hold as num) / 60).round()} minutes';
      case 'device':
        if (device == null) return '';
        if (flip) return 'flip ${label(device)}';
        if (levelOnly) return 'set ${label(device)} to ${(level ?? 0).round()}';
        if (!on) return 'turn ${label(device)} off';
        return 'turn ${label(device)} on'
            '${level != null ? ' at ${level!.round()}%' : ''}'
            '${colour != null ? ' in the chosen colour' : ''}';
    }
    return '';
  }
}

/// One "only if": none | time_range | days | device | reading.
class _Limit {
  String kind;
  final TextEditingController after = TextEditingController(text: '19:00');
  final TextEditingController before = TextEditingController(text: '06:00');
  String? device;
  bool equals = true;
  String? key;
  String edge = 'above';
  final TextEditingController number = TextEditingController();
  final Set<int> days = {0, 1, 2, 3, 4};

  static const shortDays = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  static const _dayNames = [
    'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday',
    'Sunday'
  ];

  _Limit([this.kind = 'none']);

  factory _Limit.fromJson(Map<String, dynamic> c) {
    final l = _Limit((c['kind'] ?? 'none').toString());
    if (c['after'] != null) l.after.text = c['after'].toString();
    if (c['before'] != null) l.before.text = c['before'].toString();
    l.device = c['device']?.toString();
    l.equals = c['equals'] == true;
    l.key = c['key']?.toString();
    if (c['below'] != null) {
      l.edge = 'below';
      l.number.text = '${c['below']}';
    } else if (c['above'] != null) {
      l.number.text = '${c['above']}';
    }
    final d = c['days'];
    if (d is List) {
      l.days
        ..clear()
        ..addAll([for (final x in d) if (x is num) x.toInt()]);
    }
    return l;
  }

  Map<String, dynamic>? toJson() {
    switch (kind) {
      case 'time_range':
        return {
          'kind': 'time_range',
          'after': after.text.trim(),
          'before': before.text.trim(),
        };
      case 'days':
        return days.isEmpty
            ? null
            : {'kind': 'days', 'days': days.toList()..sort()};
      case 'device':
        return device == null
            ? null
            : {'kind': 'device', 'device': device, 'key': 'on',
               'equals': equals};
      case 'reading':
        final v = double.tryParse(number.text.trim());
        if (device == null || key == null || v == null) return null;
        return {'kind': 'reading', 'device': device, 'key': key, edge: v};
    }
    return null;
  }

  String say(String Function(String?) label) {
    switch (kind) {
      case 'time_range':
        return 'it is between ${after.text.trim()} and ${before.text.trim()}';
      case 'days':
        final list = days.toList()..sort();
        if (list.length == 7) return 'it is any day';
        if (list.length == 5 && !list.contains(5) && !list.contains(6)) {
          return 'it is a weekday';
        }
        if (list.length == 2 && list.contains(5) && list.contains(6)) {
          return 'it is the weekend';
        }
        return 'it is ${list.map((d) => _dayNames[d]).join(' or ')}';
      case 'device':
        return device == null
            ? ''
            : '${label(device)} is ${equals ? 'on' : 'off'}';
      case 'reading':
        if (device == null || key == null) return '';
        return '${label(device)} ${key!.replaceAll('_', ' ')} is $edge '
            '${number.text.trim()}';
    }
    return '';
  }

  void dispose() {
    after.dispose();
    before.dispose();
    number.dispose();
  }
}
