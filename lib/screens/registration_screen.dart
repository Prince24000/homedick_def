/// registration_screen.dart — THE ONE REGISTRATION SCREEN.
///
/// Add device (a board part, a Home Assistant entity, a typed id) and Edit
/// device all land here (her ruling, 2026-09-03: "all these wizard
/// questions in one screen"). Top to bottom, exactly the approved design:
///
///   WHERE AND WHAT   room chips (the house's rooms + New room), name,
///                    what it does -- and, once the AI has answered, its
///                    sentence as a suggestion under that box
///   HOW IT BEHAVES   the five kinds, ALL of them pickable (her ruling,
///                    2026-10-05): the ones this part is meant to be are
///                    outlined in the accent colour, and choosing another
///                    says so in one line. Min / max / step / unit for a
///                    level, for open/closed on a part with a position,
///                    and optionally for a reading -- shown only then.
///                    EACH SETTING APPEARS ONCE. What the AI proposes is a
///                    dashed chip beside the value it differs from: tap it
///                    and the value goes INTO the box, where it can still
///                    be edited. (This replaced the "yours and the AI's,
///                    side by side" table, which showed every value up to
///                    three times.)
///   OPTIONS          security device; its state right now (hidden when
///                    the hub knows already); show on the home screen
///   Save device      the only way out that keeps anything
///
/// The hub owns the truth: every reply goes back as `registration_response`
/// and the hub answers with the same frame again (filled by the AI, or
/// carrying an error line) or ends the flow. Nothing is validated here
/// beyond what makes Save worth tapping.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../app_state.dart';

class RegistrationScreen extends StatefulWidget {
  final AppState appState;
  const RegistrationScreen({super.key, required this.appState});

  @override
  State<RegistrationScreen> createState() => _RegistrationScreenState();
}

class _RegistrationScreenState extends State<RegistrationScreen> {
  final _name = TextEditingController();
  final _does = TextEditingController();
  final _min = TextEditingController();
  final _max = TextEditingController();
  final _step = TextEditingController();
  final _unit = TextEditingController();
  final _level = TextEditingController();
  final _newRoom = TextEditingController();

  String _room = '';
  String? _kind; // onoff | level | open | lock | reading
  bool _secure = false;
  bool _showOnHome = true;
  bool _stateOn = false;
  bool _addingRoom = false;
  bool _generating = false;

  /// The words the AI's sentence was written for. When name or "what it
  /// does" moves away from them the behaviour card says so.
  String _aiFor = '';
  int _builtFor = -1;
  bool _didSchedulePop = false;

  /// WHOSE VALUE EACH FIELD HOLDS -- 'part', 'ha', 'ai' or 'you', by field
  /// (kind, min, max, step, unit). Seeded by the hub, moved to 'you' the
  /// moment a person edits a field, sent back with the values so the hub
  /// keeps it. Drawn as a small tag on the field (2026-09-10).
  final Map<String, String> _prov = {};

  /// WHAT IS IN THE BOX IS WHAT IS SAVED: the description is always the
  /// person's ("you") as far as the hub is told. The AI's sentence gets
  /// there by being put in the box (see _useAiDescription), where it can
  /// be edited like any other words.
  static const String _descriptionSource = 'you';

  /// The AI answer already offered on this screen (its values and the
  /// words it was written for). A NEW answer is offered beside the
  /// person's own values; the same one arriving again (the form re-sent
  /// with an error line) must not undo what they chose since.
  ///
  /// STATIC: a Save the hub refuses ("Min must be below max") comes back
  /// to a NEW copy of this screen -- the old one popped when Save was
  /// tapped -- and a field of that copy would have forgotten the answer
  /// was already seen. Cleared by any form that carries no AI answer, so
  /// it never reaches the next device's.
  static String _proposalSeen = '';

  static const _kinds = <String, String>{
    'onoff': 'On / off',
    'level': 'Level',
    'open': 'Open / closed',
    'lock': 'Locked / unlocked',
    'reading': 'Reading',
  };

  // No controller listeners: _prepare seeds the boxes inside build, and a
  // listener calling setState there is "setState() called during build".
  // Every field's onChanged rebuilds instead, which is all _ready and
  // _stale need.
  @override
  void initState() {
    super.initState();
    // The dock's tap fills THIS screen in by voice while it is up; before
    // 2026-09-11 it fired a house command the hub refused as busy.
    widget.appState.screenVoiceRequest = _startVoice;
  }

  @override
  void dispose() {
    final s = widget.appState;
    if (s.screenVoiceRequest == _startVoice) s.screenVoiceRequest = null;
    s.disarmScreenChoice();
    for (final c in [_name, _does, _min, _max, _step, _unit, _level, _newRoom]) {
      c.dispose();
    }
    super.dispose();
  }

  // ── FILLING THIS IN BY VOICE ────────────────────────────────────────
  //
  // Say the field, then say what goes in it. The field list is an ordinary
  // local choice (same matcher, same "repeat", same "cancel"); a value is
  // a dictation, which matches nothing and takes the words as heard; and
  // "how it behaves" is neither -- it is a second list, of exactly the
  // kinds this part can honour, because a kind is one of five words and
  // matching them beats transcribing them.
  //
  // Nothing arms itself: the window opens on the dock's tap and on nothing
  // else. This screen has a name box and a description box, and a mic that
  // opens itself behind a text field is what answered a PIN prompt with
  // silence.

  /// The frame the voice is naming fields from, kept at build time so a
  /// re-armed list always matches what is on screen.
  RegistrationFrame? _frame;

  void _startVoice() {
    final f = _frame;
    if (f == null) return;
    _armFields(widget.appState, f, speak: true);
  }

  /// The fields the voice offers, in the order the screen draws them.
  List<Map<String, dynamic>> _voiceFields(RegistrationFrame f) {
    final showRange = _kind != null && _hasRange(f); // as the card draws it
    return [
      {'value': 'room', 'label': 'room'},
      {'value': 'name', 'label': 'name'},
      {'value': 'does', 'label': 'what it does'},
      if (_options(f).where((o) => _selectable(f, o.kind)).length > 1)
        {'value': 'kind', 'label': 'how it behaves'},
      if (showRange) ...[
        {'value': 'min', 'label': 'smallest value'},
        {'value': 'max', 'label': 'largest value'},
        {'value': 'step', 'label': 'step'},
        {'value': 'unit', 'label': 'unit'},
      ],
      if (f.aiAvailable) {'value': 'ai', 'label': 'let the AI fill it in'},
      {'value': 'save', 'label': 'save'},
    ];
  }

  /// `say` is spoken BEFORE the list re-arms, and is how a filled field is
  /// confirmed: one utterance ("Room, kitchen. Say another field, or say
  /// save."), then the window. Acknowledging separately would have opened
  /// the mic while the phone was still speaking, and it would have heard
  /// itself -- the same self-hearing that made an over-long option list
  /// unanswerable.
  void _armFields(AppState s, RegistrationFrame f,
      {bool speak = false, String say = ''}) {
    final fields = _voiceFields(f);
    final lead = say.isEmpty
        ? 'Say a field to fill in, or say save.'
        : '$say Say another field, or say save.';
    s.armScreenChoice(
      prompt: lead,
      options: fields,
      speakPrompt: speak || say.isNotEmpty,
      repeatLine: 'Say a field to fill in: '
          '${fields.map((o) => o['label']).join(', ')}.',
      onCancel: () => s.sendCancel(),
      onPick: (value, heard) {
        if (!mounted) return;
        switch (value) {
          case 'save':
            if (_ready && !_generating) {
              _save();
            } else {
              _armFields(s, f,
                  say: 'Not ready yet — it still needs a name, a room and '
                      'how it behaves.');
            }
            return;
          case 'ai':
            _generate();
            return;
          case 'kind':
            _pickKindByVoice(s, f);
            return;
          case 'room':
            _dictate(s, f, 'Room?', (t) => setState(() {
                  // An existing room in whatever case it was heard, so
                  // "Kitchen" lights the kitchen chip instead of adding a
                  // second one (the hub lowercases rooms on save).
                  _room = f.rooms.firstWhere(
                      (r) => r.toLowerCase() == t.toLowerCase(),
                      orElse: () => t);
                  _addingRoom = false;
                }));
            return;
          case 'name':
            _dictate(s, f, 'Name?', (t) => setState(() => _name.text = t));
            return;
          case 'does':
            _dictate(s, f, 'What does it do?', (t) {
              setState(() => _does.text = t);
              _touched('does');
            });
            return;
          case 'min':
            _dictateNumber(s, f, 'Smallest value?', _min, 'min');
            return;
          case 'max':
            _dictateNumber(s, f, 'Largest value?', _max, 'max');
            return;
          case 'step':
            _dictateNumber(s, f, 'Step?', _step, 'step');
            return;
          case 'unit':
            _dictate(s, f, 'Unit?', (t) {
              setState(() => _unit.text = t);
              _touched('unit');
            });
            return;
        }
      },
    );
  }

  /// The transcriber writes sentences ("Front door."), and a room, a name
  /// or a unit is not one: the full stop used to be saved with it, so a
  /// dictated room became "kitchen." beside "kitchen".
  static String _asWord(String heard) =>
      heard.trim().replaceFirst(RegExp(r'[\s.!?,;:]+$'), '');

  void _dictate(AppState s, RegistrationFrame f, String prompt,
      void Function(String) apply) {
    s.armScreenDictation(
      prompt: prompt,
      onCancel: () => _armFields(s, f),
      onText: (heard) {
        if (!mounted) return;
        final text = _asWord(heard);
        if (text.isEmpty) {
          _armFields(s, f, say: 'I did not catch that.');
          return;
        }
        apply(text);
        _armFields(s, f, say: '$text.');
      },
    );
  }

  /// A number dictated as words ("twenty five") or digits. Anything that
  /// is not a number is REFUSED rather than written into a box the form
  /// will then reject -- the range is the one part of this form that has
  /// to parse.
  void _dictateNumber(AppState s, RegistrationFrame f, String prompt,
      TextEditingController box, String field) {
    s.armScreenDictation(
      prompt: prompt,
      onCancel: () => _armFields(s, f),
      onText: (text) {
        if (!mounted) return;
        // EXACTLY ONE NUMBER, read as written. Stripping every other
        // character glued "0 to 100" into 100 and kept the sentence's full
        // stop ("25."); thousands commas ("1,000") are the one thing
        // joined first.
        final joined = text.replaceAllMapped(
            RegExp(r'(\d),(\d{3})\b'), (m) => '${m[1]}${m[2]}');
        final found =
            RegExp(r'-?\d+(?:\.\d+)?').allMatches(joined).toList();
        if (found.length != 1) {
          _armFields(s, f,
              say: found.isEmpty
                  ? '${_asWord(text)} is not a number.'
                  : 'Say one number at a time.');
          return;
        }
        final cleaned = found.single.group(0)!;
        setState(() => box.text = cleaned);
        _touched(field);
        _armFields(s, f, say: '$cleaned.');
      },
    );
  }

  void _pickKindByVoice(AppState s, RegistrationFrame f) {
    final open = _options(f).where((o) => _selectable(f, o.kind)).toList();
    if (open.isEmpty) {
      _armFields(s, f,
          say: 'This part declares how it behaves; it cannot be changed.');
      return;
    }
    s.armScreenChoice(
      prompt: 'How it behaves. Say one of: '
          '${open.map((o) => o.label).join(', ')}.',
      speakPrompt: true,
      repeatLine: 'Say one of: ${open.map((o) => o.label).join(', ')}.',
      options: [
        for (final o in open) {'value': o.kind, 'label': o.label}
      ],
      onCancel: () => _armFields(s, f),
      onPick: (value, heard) {
        if (!mounted) return;
        setState(() {
          // As the chip does: an "on" from the old kind is not this one's.
          if (_kind != value) _stateOn = false;
          _kind = value;
          _prov['kind'] = 'you';
        });
        _armFields(s, f,
            say: '${open.firstWhere((o) => o.kind == value).label}.');
      },
    );
  }

  /// Seed the boxes from a NEW frame only: a rebuild must not wipe what is
  /// being typed. The hub sends the merged values back, so a re-sent form
  /// (an error, or the AI's fill) carries everything typed so far.
  void _prepare(RegistrationFrame f) {
    _frame = f;                       // what the voice names fields from
    final serial = widget.appState.registrationSerial;
    if (_builtFor == serial) return;
    // A NEW form: the old vocabulary named fields that may no longer be
    // on screen, so it goes rather than answering this one.
    // POST-FRAME, NOT NOW: _prepare runs inside build, and disarming
    // moves the flow's state, which notifies AppState, which is
    // "setState() called during build" -- the very trap the comment above
    // this method warns about for controller listeners.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) widget.appState.disarmScreenChoice();
    });
    _builtFor = serial;
    // As they stood before this frame, for what a new AI answer must not
    // take away (see the end of this method).
    final hadOn = _stateOn;
    final hadLevel = _level.text;
    final hadProv = Map<String, String>.from(_prov);
    final v = f.values;
    String str(String k) => (v[k] ?? '').toString();
    _name.text = str('name');
    _does.text = str('does');
    _min.text = str('min');
    _max.text = str('max');
    _step.text = str('step');
    _unit.text = str('unit');
    _room = str('room');
    final k = str('kind').toLowerCase();
    _kind = _kinds.containsKey(k) ? k : null;
    if (_kind != null && !_selectable(f, _kind!)) {
      // A record from before the part behind it changed (an older hub,
      // which refuses the others): the one kind the part allows when
      // there is one, otherwise a fresh choice.
      final open = _options(f).where((o) => o.enabled).toList();
      _kind = open.length == 1 ? open.single.kind : null;
    }
    _secure = v['secure'] == true;
    _showOnHome = v['show_on_home'] != false;
    final st = str('state').toLowerCase();
    _stateOn = const {'on', 'locked', 'open', 'true', 'yes', '1'}.contains(st);
    _level.text =
        (_kind == 'level' && double.tryParse(str('state')) != null) ? str('state') : '';
    // The words the AI actually wrote for, as the hub recorded them -- not
    // whatever is in the boxes now: a form re-sent with an error line
    // carries the edited words, and seeding from those cleared the
    // out-of-date warning while the sentence was still the old one.
    final aiFor = f.proposal?['for'];
    _aiFor = f.aiText.isEmpty
        ? ''
        : aiFor is Map
            ? '${(aiFor['name'] ?? '').toString().trim()}\n'
                '${(aiFor['does'] ?? '').toString().trim()}'
            : '${_name.text.trim()}\n${_does.text.trim()}';
    _prov.clear();
    final rawProv = v['provenance'];
    if (rawProv is Map) {
      for (final e in rawProv.entries) {
        if (e.key is String && e.value is String) {
          _prov[e.key as String] = e.value as String;
        }
      }
    }
    _generating = false;
    _addingRoom = false;
    // A NEW ANSWER FROM THE AI IS A SUGGESTION, NOT A REPLACEMENT. The hub
    // writes the AI's kind, range, step and unit into the values it sends
    // back; where the person had a value of their own it is put back in
    // the box, and the AI's shows beside it as a chip. Where they had
    // none, the AI's stays -- an empty box helps nobody.
    final p = f.proposal;
    final seen = p == null ? '' : '${p['ai']}|${p['for']}';
    if (p != null && seen != _proposalSeen) {
      final mine = (p['mine'] as Map?)?.cast<String, dynamic>() ?? const {};
      String own(String k) => (mine[k] ?? '').toString().trim();
      final ownKind = own('kind').toLowerCase();
      if (_kinds.containsKey(ownKind) && _selectable(f, ownKind)) {
        _kind = ownKind;
        _prov['kind'] = hadProv['kind'] ?? 'you';
        // The hub wiped "right now it is" when the AI's kind differed.
        _stateOn = hadOn;
        _level.text = hadLevel;
      }
      for (final e in {
        'min': _min,
        'max': _max,
        'step': _step,
        'unit': _unit
      }.entries) {
        if (own(e.key).isNotEmpty) {
          e.value.text = own(e.key);
          _prov[e.key] = hadProv[e.key] ?? 'you';
        }
      }
      // NOTHING WRITTEN ABOUT WHAT IT DOES: the AI's sentence goes in the
      // box, or it would be lost -- the description saved is what the box
      // holds, and a bare name is the weakest thing voice matching can be
      // given.
      if (_does.text.trim().isEmpty && f.aiText.isNotEmpty) {
        _does.text = f.aiText;
        _aiFor = '${_name.text.trim()}\n${f.aiText.trim()}';
      }
    }
    _proposalSeen = seen;
  }

  /// The kind chips: the hub's, greyed where this part cannot honour a
  /// kind, or the screen's own five for an older hub.
  List<KindOption> _options(RegistrationFrame f) => f.kindOptions.isNotEmpty
      ? f.kindOptions
      : [
          for (final e in _kinds.entries)
            KindOption(
                kind: e.key,
                label: e.value,
                enabled: !f.kindLocked || _kind == e.key,
                why: f.kindLocked ? 'Declared by the part or by Home Assistant.' : '')
        ];

  /// Is this kind what the part is MEANT to be?
  bool _enabled(RegistrationFrame f, String kind) =>
      _options(f).any((o) => o.kind == kind && o.enabled);

  /// May this kind be picked? Any of them, on a hub that takes any; only
  /// the part's own on an older one, which refuses the rest.
  bool _selectable(RegistrationFrame f, String kind) =>
      f.kindFree ? _kinds.containsKey(kind) : _enabled(f, kind);

  /// Does the chosen kind carry a range on this part? Level always; Open /
  /// closed when the part has a position; Reading optionally.
  bool _hasRange(RegistrationFrame f) =>
      _kind == 'level' ||
      _kind == 'reading' ||
      (_kind == 'open' && f.positionKinds.contains('open'));

  bool _positional(RegistrationFrame f) =>
      _kind == 'open' && f.positionKinds.contains('open');

  void _touched(String field) {
    if (_prov[field] != 'you') setState(() => _prov[field] = 'you');
  }

  Map<String, dynamic> _values() {
    String state;
    switch (_kind) {
      case 'level':
        state = _level.text.trim();
        break;
      case 'lock':
        state = _stateOn ? 'locked' : 'unlocked';
        break;
      case 'open':
        state = _stateOn ? 'open' : 'closed';
        break;
      case 'reading':
        state = '';
        break;
      default:
        state = _stateOn ? 'on' : 'off';
    }
    final f = widget.appState.pendingRegistration;
    return {
      'name': _name.text.trim(),
      'room': _room.trim(),
      'does': _does.text.trim(),
      'kind': _kind ?? '',
      'positional': f != null && _positional(f),
      'min': _min.text.trim(),
      'max': _max.text.trim(),
      'step': _step.text.trim(),
      'unit': _unit.text.trim(),
      'secure': _secure,
      'state': state,
      'show_on_home': _showOnHome,
      'provenance': Map<String, String>.from(_prov),
      'description_source': _descriptionSource,
    };
  }

  bool get _ready {
    if (_name.text.trim().isEmpty || _kind == null || _room.trim().isEmpty) {
      return false;
    }
    final f = widget.appState.pendingRegistration;
    final filled = [_min, _max, _step].where((c) => c.text.trim().isNotEmpty).length;
    if (_kind == 'level' || (f != null && _positional(f))) {
      return filled == 3;
    }
    if (_kind == 'reading') {
      return filled == 0 || filled == 3; // a range is optional, never half
    }
    return true;
  }

  bool get _stale =>
      _aiFor.isNotEmpty &&
      _aiFor != '${_name.text.trim()}\n${_does.text.trim()}';

  void _generate() {
    // Once per answer: the button greys itself, but the spoken "let the AI
    // fill it in" did not, and sent a second generate while the first ran.
    if (_generating) return;
    setState(() => _generating = true);
    widget.appState.sendRegistrationReply('generate', _values());
  }

  void _save() => widget.appState.sendRegistrationReply('save', _values());

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: widget.appState,
      builder: (context, _) => _build(context),
    );
  }

  Widget _build(BuildContext context) {
    final s = widget.appState;
    final f = s.pendingRegistration;
    if (f == null) {
      if (!_didSchedulePop) {
        _didSchedulePop = true;
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (!mounted) return;
          final route = ModalRoute.of(context);
          if (route != null &&
              route.isCurrent &&
              widget.appState.pendingRegistration == null) {
            Navigator.of(context).pop();
          } else {
            _didSchedulePop = false;
          }
        });
      }
      return const SizedBox.shrink();
    }
    _didSchedulePop = false;
    _prepare(f);
    final ready = _ready;
    final rooms = [
      ...f.rooms,
      if (_room.isNotEmpty && !f.rooms.contains(_room)) _room,
    ];

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
              icon: Icon(Icons.arrow_back, color: s.ink),
              onPressed: () => Navigator.of(context).maybePop(),
            ),
            title: Text(f.title,
                style: TextStyle(
                    color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
          ),
          body: SafeArea(
            child: ListView(
              // Room under the last row for the voice dock, as FormScreen.
              padding: const EdgeInsets.fromLTRB(16, 0, 16, 96),
              children: [
                if (f.source.isNotEmpty) _sourceCard(s, f.source),
                if (f.error.isNotEmpty) _errorCard(s, f.error),
                _group(s, 'WHERE AND WHAT'),
                Padding(
                  padding: const EdgeInsets.only(left: 4, bottom: 8),
                  child: Text('Room',
                      style: TextStyle(color: s.inkDim, fontSize: 12)),
                ),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    for (final r in rooms)
                      _chip(s, _title(r), r == _room,
                          () => setState(() {
                                _room = r;
                                _addingRoom = false;
                              })),
                    // The box's words ARE the room while it is open: a name
                    // typed earlier and left in it used to show under the
                    // lit chip while the previous room was what got saved.
                    _chip(s, '+ New room', _addingRoom,
                        () => setState(() {
                              _addingRoom = true;
                              _room = _newRoom.text.trim();
                            })),
                  ],
                ),
                if (_addingRoom) ...[
                  const SizedBox(height: 10),
                  _field(s, _newRoom, 'New room',
                      hint: 'e.g. garage',
                      onChanged: (v) => setState(() => _room = v.trim())),
                ],
                const SizedBox(height: 10),
                _field(s, _name, 'Device name',
                    hint: 'e.g. front door',
                    helper: 'What you will call it when you speak.'),
                const SizedBox(height: 10),
                _field(s, _does, 'What does it do?',
                    hint: 'e.g. the deadbolt on the front door',
                    helper: 'In your words. Voice matching reads this.',
                    lines: 2),
                if (f.aiText.isNotEmpty && _does.text.trim() != f.aiText.trim())
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: _aiChip(s, 'AI: “${f.aiText}”', _useAiDescription,
                        wide: true),
                  ),
                _group(s, 'HOW IT BEHAVES'),
                _behaviourCard(s, f),
                _group(s, 'OPTIONS'),
                _switchRow(s,
                    icon: Icons.lock_outline,
                    title: 'Security device',
                    subtitle:
                        'Voice or PIN is checked every time before it moves. Doors, locks, alarms, cameras.',
                    value: _secure,
                    onChanged: (v) => setState(() => _secure = v)),
                const SizedBox(height: 8),
                if (!f.stateKnown && _kind != null && _kind != 'reading') ...[
                  Padding(
                    padding: const EdgeInsets.fromLTRB(4, 2, 4, 6),
                    child: Text('Right now it is',
                        style: TextStyle(color: s.inkDim, fontSize: 12)),
                  ),
                  _nowControl(s),
                  const SizedBox(height: 8),
                ],
                _switchRow(s,
                    icon: Icons.home_outlined,
                    title: 'Show on the home screen',
                    subtitle: 'A card with its reading or switch.',
                    value: _showOnHome,
                    onChanged: (v) => setState(() => _showOnHome = v)),
                const SizedBox(height: 22),
                SizedBox(
                  height: 50,
                  child: FilledButton.icon(
                    onPressed: ready && !_generating ? _save : null,
                    style: FilledButton.styleFrom(
                      backgroundColor: s.accent,
                      foregroundColor: s.accentFg,
                      disabledBackgroundColor: s.border,
                      disabledForegroundColor: s.inkFaint,
                      shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(12)),
                    ),
                    icon: const Icon(Icons.lock_outline, size: 16),
                    label: Text(f.editing ? 'Save changes' : 'Save device',
                        style: const TextStyle(
                            fontSize: 15, fontWeight: FontWeight.w700)),
                  ),
                ),
                Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: Text(
                    ready
                        ? ''
                        : 'Needs a room, a name and a kind. A level also needs its range and step.',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: s.inkFaint, fontSize: 11),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  // ── pieces ─────────────────────────────────────────────────────────

  static String _title(String r) =>
      r.isEmpty ? r : r[0].toUpperCase() + r.substring(1);

  Widget _group(AppState s, String label) => Padding(
        padding: const EdgeInsets.only(top: 18, bottom: 6, left: 4),
        child: Text(label,
            style: TextStyle(
                color: s.inkFaint,
                fontSize: 11,
                letterSpacing: 0.8,
                fontWeight: FontWeight.w700)),
      );

  Widget _sourceCard(AppState s, String text) => Container(
        margin: const EdgeInsets.only(top: 4),
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        decoration: BoxDecoration(
          color: s.surface,
          border: Border.all(color: s.border),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Row(children: [
          // The hub's words: "On board <slug>" (Edit device) -- it never
          // says "From board", so a board part wore the house icon.
          Icon(
              text.startsWith('On board') || text.startsWith('From board')
                  ? Icons.developer_board
                  : Icons.home_outlined,
              size: 16,
              color: s.inkDim),
          const SizedBox(width: 8),
          Expanded(
              child: Text(text,
                  style: TextStyle(
                      color: s.inkDim, fontSize: 12.5, height: 1.35))),
        ]),
      );

  Widget _errorCard(AppState s, String text) => Container(
        margin: const EdgeInsets.only(top: 10),
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        decoration: BoxDecoration(
          border: Border.all(color: s.cancelInk),
          borderRadius: BorderRadius.circular(10),
        ),
        child: Text(text,
            style: TextStyle(color: s.ink, fontSize: 12.5, height: 1.4)),
      );

  /// [mark]: outlined in the accent colour while not chosen -- a kind this
  /// part is meant to be.
  Widget _chip(AppState s, String label, bool on, VoidCallback onTap,
      {bool enabled = true, bool mark = false}) {
    final bg = on ? s.accent : s.surface;
    final fg = on ? s.accentFg : (mark ? s.accent : s.ink);
    return Opacity(
      opacity: enabled ? 1 : 0.45,
      child: InkWell(
        onTap: enabled ? onTap : null,
        borderRadius: BorderRadius.circular(18),
        child: Container(
          height: 36,
          padding: const EdgeInsets.symmetric(horizontal: 14),
          alignment: Alignment.center,
          decoration: BoxDecoration(
            color: bg,
            border: Border.all(
                color: on || mark ? s.accent : s.border,
                width: mark && !on ? 1.5 : 1),
            borderRadius: BorderRadius.circular(18),
          ),
          child: Text(label,
              style: TextStyle(
                  color: fg,
                  fontSize: 13.5,
                  fontWeight: on ? FontWeight.w600 : FontWeight.w500)),
        ),
      ),
    );
  }

  /// The small word on a field saying whose value it holds.
  static const _tagWords = {'ai': 'AI', 'you': 'You', 'part': 'Part', 'ha': 'HA'};

  Widget _field(AppState s, TextEditingController c, String label,
      {String hint = '',
      String helper = '',
      int lines = 1,
      String? tag,
      TextInputType? keyboard,
      ValueChanged<String>? onChanged}) {
    final tagWord = tag == null ? null : _tagWords[tag];
    final tagColor = tag == 'ai'
        ? s.accent
        : tag == 'you'
            ? s.inkDim
            : s.inkFaint;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Stack(clipBehavior: Clip.none, children: [
          TextField(
            controller: c,
            minLines: lines,
            maxLines: lines,
            keyboardType: keyboard,
            onChanged: (v) {
              if (onChanged != null) onChanged(v);
              setState(() {});
            },
            style: TextStyle(color: s.ink, fontSize: 14.5),
            decoration: InputDecoration(
              labelText: label,
              labelStyle: TextStyle(color: s.inkDim),
              hintText: hint,
              hintStyle: TextStyle(color: s.inkFaint),
              filled: true,
              fillColor: s.surface,
              isDense: true,
              contentPadding:
                  const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
              border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(8),
                  borderSide: BorderSide(color: s.border)),
              enabledBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(8),
                  borderSide:
                      BorderSide(color: tag == 'ai' ? s.accent : s.border)),
              focusedBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(8),
                  borderSide: BorderSide(color: s.accent)),
            ),
          ),
          if (tagWord != null)
            Positioned(
              top: -7,
              right: 8,
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 5),
                color: s.bg,
                child: Text(tagWord,
                    style: TextStyle(
                        color: tagColor,
                        fontSize: 10,
                        fontWeight: FontWeight.w700,
                        letterSpacing: 0.4)),
              ),
            ),
        ]),
        if (helper.isNotEmpty)
          Padding(
            padding: const EdgeInsets.fromLTRB(4, 4, 4, 0),
            child: Text(helper,
                style: TextStyle(color: s.inkFaint, fontSize: 11)),
          ),
      ],
    );
  }

  // ── what the AI proposes, as chips beside the values ───────────────
  //
  // After "Ask the AI" the hub sends both sides (`proposal`: mine, ai).
  // Nothing is shown twice: the boxes hold the values in force, and where
  // the AI's differs a dashed chip says so. Tapping it PUTS THE AI'S VALUE
  // IN THE BOX -- it is not locked there: edit it, and it is yours again
  // (her question, 2026-10-05: "can I click on the AI suggestion and then
  // edit it, or is it permanent?").

  Map<String, dynamic> _aiSide(RegistrationFrame f) =>
      ((f.proposal ?? const <String, dynamic>{})['ai'] as Map?)
          ?.cast<String, dynamic>() ??
      const {};

  String _ai(RegistrationFrame f, String k) =>
      (_aiSide(f)[k] ?? '').toString().trim();

  /// The AI's sentence into the "what does it do" box, to keep or edit.
  void _useAiDescription() {
    final f = widget.appState.pendingRegistration;
    if (f == null || f.aiText.isEmpty) return;
    setState(() {
      _does.text = f.aiText;
      // These ARE the words the AI answered for now: not "out of date".
      _aiFor = '${_name.text.trim()}\n${f.aiText.trim()}';
    });
  }

  void _useAiKind(RegistrationFrame f) {
    final k = _ai(f, 'kind').toLowerCase();
    if (!_kinds.containsKey(k) || !_selectable(f, k)) return;
    setState(() {
      if (_kind != k) _stateOn = false;
      _kind = k;
      _prov['kind'] = 'ai';
    });
  }

  void _useAiRange(RegistrationFrame f) => setState(() {
        _min.text = _ai(f, 'min');
        _max.text = _ai(f, 'max');
        _prov['min'] = 'ai';
        _prov['max'] = 'ai';
      });

  void _useAiStep(RegistrationFrame f) => setState(() {
        _step.text = _ai(f, 'step');
        _prov['step'] = 'ai';
      });

  void _useAiUnit(RegistrationFrame f) => setState(() {
        _unit.text = _ai(f, 'unit');
        _prov['unit'] = 'ai';
      });

  bool _aiKindDiffers(RegistrationFrame f) {
    final k = _ai(f, 'kind').toLowerCase();
    return _kinds.containsKey(k) && k != _kind && _selectable(f, k);
  }

  bool _aiRangeDiffers(RegistrationFrame f) =>
      _ai(f, 'min').isNotEmpty &&
      _ai(f, 'max').isNotEmpty &&
      (_ai(f, 'min') != _min.text.trim() || _ai(f, 'max') != _max.text.trim());

  bool _aiStepDiffers(RegistrationFrame f) =>
      _ai(f, 'step').isNotEmpty && _ai(f, 'step') != _step.text.trim();

  bool _aiUnitDiffers(RegistrationFrame f) =>
      _ai(f, 'unit').isNotEmpty && _ai(f, 'unit') != _unit.text.trim();

  /// A suggestion: dashed in spirit (a tinted, accent-edged pill), with the
  /// spark the AI button carries. [wide] lets a sentence wrap.
  Widget _aiChip(AppState s, String label, VoidCallback onTap,
      {bool wide = false}) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(wide ? 10 : 16),
      child: Container(
        padding: EdgeInsets.symmetric(horizontal: 10, vertical: wide ? 8 : 6),
        decoration: BoxDecoration(
          color: s.accent.withValues(alpha: 0.07),
          border: Border.all(color: s.accent.withValues(alpha: 0.55)),
          borderRadius: BorderRadius.circular(wide ? 10 : 16),
        ),
        child: Row(
          mainAxisSize: wide ? MainAxisSize.max : MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.only(top: 1, right: 6),
              child: Icon(Icons.auto_awesome_outlined, size: 13, color: s.accent),
            ),
            Flexible(
              child: Text(label,
                  style: TextStyle(
                      color: s.ink, fontSize: 12.5, height: 1.35)),
            ),
            if (wide) ...[
              const SizedBox(width: 10),
              Text('Use',
                  style: TextStyle(
                      color: s.accent,
                      fontSize: 12.5,
                      fontWeight: FontWeight.w700)),
            ],
          ],
        ),
      ),
    );
  }

  Widget _behaviourCard(AppState s, RegistrationFrame f) {
    final options = _options(f);
    final showRange = _kind != null && _hasRange(f);
    final chosen = options.where((o) => o.kind == _kind).toList();
    final meant = options.any((o) => o.enabled) &&
        options.any((o) => !o.enabled);
    // ONE LINE UNDER THE CHIPS, and only one: what the chosen kind means
    // here, or that it is not what this part is meant to be (and why), or
    // what the coloured outline is saying.
    String kindNote = '';
    var warn = false;
    if (chosen.isNotEmpty && !chosen.single.enabled) {
      final why = chosen.single.why;
      warn = true;
      kindNote = f.kindFree
          ? 'Not what this part is meant to be'
              '${why.isEmpty ? '' : ': $why'}. You can save it this way; '
              'it may not respond as ${chosen.single.label.toLowerCase()}.'
          : (why.isEmpty ? 'Declared by the part.' : why);
    } else if (_kind == 'open' && _positional(f)) {
      kindNote =
          'This part moves to a position: “open” is the top of its travel, “half open” a position.';
    } else if (meant) {
      kindNote = f.kindFree
          ? 'Outlined in colour: what this part is meant to be. Any kind can be picked.'
          : 'Greyed kinds are not for this part.';
    }
    final aiChips = <Widget>[
      if (_aiKindDiffers(f))
        _aiChip(
            s,
            'AI: ${_kinds[_ai(f, 'kind').toLowerCase()]}',
            () => _useAiKind(f)),
      if (showRange && _aiRangeDiffers(f))
        _aiChip(s, 'AI range: ${_ai(f, 'min')} – ${_ai(f, 'max')}',
            () => _useAiRange(f)),
      if (showRange && _aiStepDiffers(f))
        _aiChip(s, 'AI step: ${_ai(f, 'step')}', () => _useAiStep(f)),
      if (showRange && _aiUnitDiffers(f))
        _aiChip(s, 'AI unit: ${_ai(f, 'unit')}', () => _useAiUnit(f)),
    ];
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: s.surface,
        border: Border.all(color: s.border),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Kind', style: TextStyle(color: s.inkFaint, fontSize: 12)),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              for (final o in options)
                _chip(s, o.label, _kind == o.kind, () {
                  setState(() {
                    if (_kind != o.kind) _stateOn = false;
                    _kind = o.kind;
                    _prov['kind'] = 'you';
                  });
                },
                    enabled: _selectable(f, o.kind) || _kind == o.kind,
                    mark: f.kindFree && meant && o.enabled),
            ],
          ),
          if (kindNote.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(4, 6, 4, 0),
              child: Text(kindNote,
                  style: TextStyle(
                      color: warn ? const Color(0xFFB7791F) : s.inkFaint,
                      fontSize: 11,
                      height: 1.4)),
            ),
          if (showRange) ...[
            const SizedBox(height: 12),
            Row(children: [
              Expanded(
                  child: _field(s, _min, 'Min',
                      hint: '0',
                      onChanged: (_) => _touched('min'),
                      keyboard: const TextInputType.numberWithOptions(
                          decimal: true, signed: true))),
              const SizedBox(width: 8),
              Expanded(
                  child: _field(s, _max, 'Max',
                      hint: '100',
                      onChanged: (_) => _touched('max'),
                      keyboard: const TextInputType.numberWithOptions(
                          decimal: true, signed: true))),
              const SizedBox(width: 8),
              Expanded(
                  child: _field(s, _step, 'Step',
                      hint: '10',
                      onChanged: (_) => _touched('step'),
                      keyboard: const TextInputType.numberWithOptions(
                          decimal: true))),
              const SizedBox(width: 8),
              Expanded(
                  child: _field(s, _unit, 'Unit',
                      hint: '%', onChanged: (_) => _touched('unit'))),
            ]),
            if (_kind == 'reading')
              Padding(
                padding: const EdgeInsets.fromLTRB(4, 4, 4, 0),
                child: Text('Optional for a reading: leave all empty if unknown.',
                    style: TextStyle(color: s.inkFaint, fontSize: 11)),
              ),
          ],
          if (aiChips.isNotEmpty) ...[
            const SizedBox(height: 10),
            Wrap(spacing: 8, runSpacing: 8, children: aiChips),
            Padding(
              padding: const EdgeInsets.fromLTRB(4, 6, 4, 0),
              child: Text(
                  'Tap a suggestion to put it in. You can still change it after.',
                  style: TextStyle(color: s.inkFaint, fontSize: 11)),
            ),
            if (aiChips.length > 1)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: SizedBox(
                  height: 40,
                  width: double.infinity,
                  child: OutlinedButton.icon(
                    onPressed: () {
                      if (_aiKindDiffers(f)) _useAiKind(f);
                      // After the kind: it decides whether a range shows.
                      if (_hasRange(f)) {
                        if (_aiRangeDiffers(f)) _useAiRange(f);
                        if (_aiStepDiffers(f)) _useAiStep(f);
                        if (_aiUnitDiffers(f)) _useAiUnit(f);
                      }
                    },
                    style: OutlinedButton.styleFrom(
                      foregroundColor: s.accent,
                      side: BorderSide(color: s.accent),
                      shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(20)),
                    ),
                    icon: const Icon(Icons.auto_awesome_outlined, size: 15),
                    label: const Text('Use all AI suggestions',
                        style: TextStyle(
                            fontSize: 13.5, fontWeight: FontWeight.w600)),
                  ),
                ),
              ),
          ],
          if (f.aiAvailable) ...[
            const SizedBox(height: 10),
            Align(
              alignment: Alignment.centerLeft,
              child: TextButton.icon(
                onPressed: _generating || _name.text.trim().isEmpty
                    ? null
                    : _generate,
                style: TextButton.styleFrom(
                  foregroundColor: s.accent,
                  padding: const EdgeInsets.symmetric(horizontal: 6),
                ),
                icon: _generating
                    ? SizedBox(
                        width: 14,
                        height: 14,
                        child: CircularProgressIndicator(
                            strokeWidth: 2, color: s.inkDim))
                    : const Icon(Icons.auto_awesome_outlined, size: 15),
                label: Text(
                    f.aiText.isEmpty
                        ? 'Ask the AI to suggest'
                        : _stale
                            ? 'The words changed — ask the AI again'
                            : 'Ask the AI again',
                    style: const TextStyle(
                        fontSize: 13, fontWeight: FontWeight.w600)),
              ),
            ),
          ],
        ],
      ),
    );
  }

  Widget _nowControl(AppState s) {
    if (_kind == null) {
      return Container(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 13),
        decoration: BoxDecoration(
          color: s.surface,
          border: Border.all(color: s.border),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Text(
          'Appears once a kind is picked above — on / off, locked / unlocked, open / closed, or a number.',
          style: TextStyle(color: s.inkFaint, fontSize: 12.5, height: 1.4),
        ),
      );
    }
    if (_kind == 'level') {
      return _field(s, _level, 'Current level',
          hint: 'Between min and max',
          keyboard:
              const TextInputType.numberWithOptions(decimal: true, signed: true));
    }
    final words = _kind == 'lock'
        ? const ['Locked', 'Unlocked']
        : _kind == 'open'
            ? const ['Open', 'Closed']
            : const ['On', 'Off'];
    return Container(
      height: 44,
      decoration: BoxDecoration(
        color: s.surface,
        border: Border.all(color: s.border),
        borderRadius: BorderRadius.circular(10),
      ),
      clipBehavior: Clip.antiAlias,
      child: Row(children: [
        for (var i = 0; i < 2; i++)
          Expanded(
            child: InkWell(
              onTap: () => setState(() => _stateOn = i == 0),
              child: Container(
                alignment: Alignment.center,
                color: (_stateOn == (i == 0)) ? s.accent : Colors.transparent,
                child: Text(words[i],
                    style: TextStyle(
                        color: (_stateOn == (i == 0)) ? s.accentFg : s.ink,
                        fontSize: 14,
                        fontWeight: (_stateOn == (i == 0))
                            ? FontWeight.w700
                            : FontWeight.w500)),
              ),
            ),
          ),
      ]),
    );
  }

  Widget _switchRow(AppState s,
      {required IconData icon,
      required String title,
      required String subtitle,
      required bool value,
      required ValueChanged<bool> onChanged}) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
      decoration: BoxDecoration(
        color: s.surface,
        border: Border.all(color: s.border),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(children: [
        Icon(icon, size: 19, color: s.inkDim),
        const SizedBox(width: 13),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(title,
                  style: TextStyle(
                      color: s.ink, fontSize: 14.5, fontWeight: FontWeight.w600)),
              const SizedBox(height: 2),
              Text(subtitle,
                  style: TextStyle(color: s.inkDim, fontSize: 12, height: 1.35)),
            ],
          ),
        ),
        Switch(value: value, activeThumbColor: s.accent, onChanged: onChanged),
      ]),
    );
  }
}