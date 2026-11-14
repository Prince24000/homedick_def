/// Scenes: list, add, edit and remove in one screen. New from the button,
/// edit by tapping a scene, remove from its ⋮ menu (asked first). The hub
/// keeps its four menu actions for voice.
///
/// THE SCENE SCREEN (2026-11-08): while the hub hands over a scene to build
/// or change (`scene_builder`), this screen is its editor (SceneEditor,
/// below): name (what you say), who it is for, what it is for ("Ask the
/// AI to suggest" waits, as registration does, and the suggestion appears
/// beside your words to tap and use), each device with its own controls
/// and a cross, + Add device, Try it now and Save. Back is a cancel, asked
/// first ("Discard changes?") when something was changed.
///
/// One data path: the `scene_list` frame (name, trigger, actions with each
/// device's name and room, whether it still exists, and whether any member
/// is security-critical). Printed rows are never parsed.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../app_state.dart';
import '../widgets/voice_ui.dart';
import '../widgets/owner_bits.dart';
import '../widgets/device_controls.dart';

class _Scene {
  String name;
  String trigger = '';
  final List<String> actions = [];
  bool secure = false;
  /// Who made it, as the hub words it -- see widgets/owner_bits.dart.
  String chip = '';
  Map<String, dynamic> record = const {};

  _Scene(this.name);
}

class ScenesScreen extends StatelessWidget {
  final AppState appState;
  const ScenesScreen({super.key, required this.appState});

  /// Structured records -> cards. One action reads "Living room lamp —
  /// on", by the device's own name, not its id.
  static List<_Scene> _fromRecords(List<Map<String, dynamic>> records) {
    final out = <_Scene>[];
    for (final r in records) {
      final sc = _Scene((r['name'] ?? '').toString())
        ..trigger = (r['trigger'] ?? '').toString()
        ..secure = r['secure'] == true
        ..chip = (r['chip'] ?? '').toString()
        ..record = r;
      for (final a in (r['actions'] as List? ?? const [])) {
        if (a is! Map) continue;
        final name = (a['device_name'] ?? a['device'] ?? '?').toString();
        final reading = (a['reading'] ?? '').toString();
        final state = a['state'];
        final what = reading.isNotEmpty
            ? reading
            : (state is Map ? state.toString() : '');
        // A scene may still point at a device that is gone: say so.
        final gone = a['known'] == false ? '  (no longer registered)' : '';
        sc.actions.add(what.isEmpty ? '$name$gone' : '$name — $what$gone');
      }
      out.add(sc);
    }
    return out;
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: appState,
      builder: (context, _) => _build(context),
    );
  }

  Widget _build(BuildContext context) {
    final s = appState;
    // A scene in hand: the screen is its editor.
    if (s.sceneBuilder != null) {
      return SceneEditor(appState: s, key: const ValueKey('scene-editor'));
    }
    final scenes = _fromRecords(s.sceneRecords);

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
      child: Scaffold(
        backgroundColor: s.bg,
        appBar: AppBar(
          backgroundColor: s.bg,
          elevation: 0,
          leading: IconButton(
            icon: Icon(Icons.arrow_back, color: s.ink),
            onPressed: () => Navigator.pop(context),
          ),
          title: Text('Scenes',
              style: TextStyle(
                  color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
          actions: [
            IconButton(
              tooltip: 'Refresh',
              icon: Icon(Icons.refresh, color: s.inkDim),
              // No Navigator.pop: the screen listens and redraws in place.
              onPressed: () => s.requestSceneList(),
            ),
          ],
        ),
        // Add starts the hub's add-scene flow, which lands back here.
        floatingActionButton: FloatingActionButton.extended(
          backgroundColor: s.accent,
          foregroundColor: s.accentFg,
          onPressed: () => s.addScene(),
          icon: const Icon(Icons.add, size: 20),
          label: const Text('New',
              style: TextStyle(fontWeight: FontWeight.w700)),
        ),
        body: SafeArea(
          child: scenes.isEmpty
              ? Center(
                  child: Padding(
                    padding: const EdgeInsets.all(32),
                    child: Text(
                      'No scenes yet.\n\nA scene is one sentence that moves '
                      'several things at once — "movie time" dims the lamp '
                      'and shuts the blind. Tap New to teach one.',
                      textAlign: TextAlign.center,
                      style: TextStyle(
                          color: s.inkDim, fontSize: 14, height: 1.5),
                    ),
                  ),
                )
              : VoiceUiScope(
                  state: s,
                  builder: (ctx, scroll) => ListView.builder(
                  controller: scroll,
                  // Build offscreen rows too: the voice reads what is built.
                  cacheExtent: 4000,
                  padding: const EdgeInsets.fromLTRB(16, 8, 16, 96),
                  itemCount: scenes.length + 1,
                  itemBuilder: (context, i) => i == scenes.length
                      ? Padding(
                          padding: const EdgeInsets.only(top: 4),
                          child: Text('Tap a scene to change it.',
                              textAlign: TextAlign.center,
                              style: TextStyle(
                                  color: s.inkFaint, fontSize: 12.5)),
                        )
                      : _card(context, s, scenes[i]),
                ),
                ),
        ),
      ),
    );
  }

  Widget _card(BuildContext context, AppState s, _Scene sc) {
    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      decoration: BoxDecoration(
        color: s.surface,
        border: Border.all(color: s.border),
        borderRadius: BorderRadius.circular(12),
      ),
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        // Tap = edit.
        onTap: () => s.editScene(sc.name),
        // Long press = details: what it does, who made and changed it.
        onLongPress: () => _details(context, s, sc),
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Icon(Icons.movie_filter_outlined, size: 20, color: s.accent),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      sc.name,
                      style: TextStyle(
                          fontSize: 15.5,
                          fontWeight: FontWeight.w600,
                          color: s.ink),
                    ),
                  ),
                  OwnerTag(s, sc.chip),
                  // A scene that can move a lock is marked, as devices are.
                  if (sc.secure)
                    const Padding(
                      padding: EdgeInsets.only(right: 2),
                      child: Icon(Icons.shield_outlined,
                          size: 15, color: Color(0xFFD29922)),
                    ),
                  _menu(context, s, sc),
                ],
              ),
              if (sc.trigger.isNotEmpty) ...[
                const SizedBox(height: 4),
                Text('say: "${sc.trigger}"',
                    style: TextStyle(
                        fontSize: 12.5,
                        fontStyle: FontStyle.italic,
                        color: s.inkDim)),
              ],
              // What it does, each device in words.
              for (final a in sc.actions.take(4))
                Padding(
                  padding: const EdgeInsets.only(top: 3),
                  child: Text(a,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(fontSize: 13, color: s.ink)),
                ),
              if (sc.actions.length > 4)
                Text('and ${sc.actions.length - 4} more',
                    style: TextStyle(fontSize: 12.5, color: s.inkFaint)),
            ],
          ),
        ),
      ),
    );
  }

  void _details(BuildContext context, AppState s, _Scene sc) {
    showDetailsPanel(
      context,
      s,
      title: sc.name,
      tag: sc.chip,
      subtitle: sc.trigger.isEmpty ? '' : 'say: "${sc.trigger}"',
      italicSubtitle: true,
      body: [
        detailsHeading(s, 'WHAT IT DOES'),
        for (final a in sc.actions)
          Padding(
            padding: const EdgeInsets.only(bottom: 6),
            child: Text('↳ $a',
                style: TextStyle(fontSize: 14, color: s.ink, height: 1.35)),
          ),
        if (whoLines(s, sc.record).isNotEmpty) ...[
          const SizedBox(height: 6),
          Divider(color: s.border, height: 1),
          detailsHeading(s, 'WHO'),
          ...whoLines(s, sc.record),
        ],
      ],
    );
  }

  /// Remove, behind a confirmation (not undoable).
  Widget _menu(BuildContext context, AppState s, _Scene sc) {
    return PopupMenuButton<String>(
      icon: Icon(Icons.more_vert, size: 18, color: s.inkFaint),
      color: s.surface,
      onSelected: (v) async {
        if (v != 'remove') return;
        final yes = await showDialog<bool>(
          context: context,
          builder: (dctx) => AlertDialog(
            backgroundColor: s.surface,
            surfaceTintColor: Colors.transparent,
            title: Text('Remove ${sc.name}?',
                style: TextStyle(color: s.ink, fontSize: 17)),
            content: Text(
                'The scene goes. Every device it touched is left exactly '
                'as it is.',
                style: TextStyle(color: s.inkDim, fontSize: 13.5)),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(dctx, false),
                // Every cancel wears the disconnect red.
                child:
                    Text('Cancel', style: TextStyle(color: s.cancelInk)),
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
        s.removeScene(sc.name);
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
}

/// One device's part in the scene.
class _Part {
  final String device;
  String label;
  String room;
  bool known;

  /// The controls that fit it ({kind, ...}), from the hub's targets.
  Map<String, dynamic> use;

  /// What it does: on, level, colour, press, or show.
  Map<String, dynamic> state;

  /// The hub's words for a part this screen has no controls for.
  String words;
  _Part(this.device,
      {this.label = '',
      this.room = '',
      this.known = true,
      this.use = const {},
      Map<String, dynamic>? state,
      this.words = ''})
      : state = state ?? {};

  String get kind => (use['kind'] ?? '').toString();
}

class SceneEditor extends StatefulWidget {
  final AppState appState;
  const SceneEditor({super.key, required this.appState});

  @override
  State<SceneEditor> createState() => _SceneEditorState();
}

class _SceneEditorState extends State<SceneEditor> {
  AppState get s => widget.appState;

  final _name = TextEditingController();
  final _desc = TextEditingController();
  bool _private = false;
  final List<_Part> _parts = [];

  /// Anything changed since it opened (Back then asks first).
  bool _dirty = false;

  /// Waiting on the hub after Save / Try it now; cleared by its answer.
  bool _saving = false;
  bool _trying = false;
  SceneBuilderFrame? _frame;

  static const _palette = <List<int>>[
    [255, 214, 170], [255, 160, 40], [255, 40, 30], [255, 60, 160],
    [150, 70, 255], [40, 110, 255], [0, 200, 220], [40, 200, 90],
  ];

  @override
  void initState() {
    super.initState();
    final f = s.sceneBuilder;
    _frame = f;
    if (f == null) return;
    _name.text = (f.scene['name'] ?? '').toString();
    _desc.text = (f.scene['description'] ?? '').toString();
    _private = f.scene['private'] == true;
    for (final a in (f.scene['actions'] as List? ?? const [])) {
      if (a is! Map) continue;
      final did = (a['device'] ?? '').toString();
      final t = _target(f, did);
      _parts.add(_Part(did,
          label: (a['device_name'] ?? t?['label'] ?? did).toString(),
          room: (a['room'] ?? t?['room'] ?? '').toString(),
          known: a['known'] != false,
          use: ((t?['use'] as Map?) ?? const {}).cast<String, dynamic>(),
          state: Map<String, dynamic>.from((a['state'] as Map?) ?? const {}),
          words: (a['words'] ?? '').toString()));
    }
    _name.addListener(_touched);
    _desc.addListener(_touched);
  }

  @override
  void dispose() {
    _name.dispose();
    _desc.dispose();
    super.dispose();
  }

  /// Typing redraws the name's helper line and the AI button.
  void _touched() => setState(() => _dirty = true);

  Map<String, dynamic>? _target(SceneBuilderFrame f, String did) {
    for (final t in f.targets) {
      if (t is Map && t['value'] == did) return t.cast<String, dynamic>();
    }
    return null;
  }

  /// The scene as it now stands, for the hub.
  Map<String, dynamic> _compose() => {
        'name': _name.text.trim(),
        'description': _desc.text.trim(),
        'private': _private,
        'actions': [
          for (final p in _parts)
            if (p.known) {'device': p.device, 'state': p.state},
        ],
      };

  void _change(VoidCallback fn) => setState(() {
        fn();
        _dirty = true;
      });

  /// Back: a cancel, asked first when something was changed.
  Future<void> _leave() async {
    if (_dirty) {
      final yes = await showDialog<bool>(
        context: context,
        builder: (dctx) => AlertDialog(
          backgroundColor: s.surface,
          surfaceTintColor: Colors.transparent,
          title: Text('Discard changes?',
              style: TextStyle(color: s.ink, fontSize: 17)),
          content: Text(
              s.sceneBuilder?.editing == true
                  ? 'The scene stays as it was.'
                  : 'The new scene is not saved.',
              style: TextStyle(color: s.inkDim, fontSize: 13.5)),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dctx, false),
              child: Text('Keep editing', style: TextStyle(color: s.ink)),
            ),
            TextButton(
              onPressed: () => Navigator.pop(dctx, true),
              child: Text('Discard',
                  style: TextStyle(
                      color: s.cancelInk, fontWeight: FontWeight.w700)),
            ),
          ],
        ),
      );
      if (yes != true) return;
    }
    s.sendSceneBuilder(null);
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(listenable: s, builder: (context, _) => _build());
  }

  Widget _build() {
    final f = s.sceneBuilder;
    if (f == null) return const SizedBox.shrink();
    if (!identical(f, _frame)) {
      // The hub answered (a refusal, or what Try it now did).
      _frame = f;
      _saving = false;
      _trying = false;
    }
    return AnnotatedRegion<SystemUiOverlayStyle>(
      value: s.isLight
          ? SystemUiOverlayStyle.dark.copyWith(
              statusBarColor: Colors.transparent,
              systemNavigationBarColor: s.bg,
              systemNavigationBarIconBrightness: Brightness.dark)
          : SystemUiOverlayStyle.light.copyWith(
              statusBarColor: Colors.transparent,
              systemNavigationBarColor: s.bg,
              systemNavigationBarIconBrightness: Brightness.light),
      child: PopScope(
        canPop: false,
        onPopInvokedWithResult: (didPop, _) {
          if (!didPop) _leave();
        },
        child: Scaffold(
          backgroundColor: s.bg,
          appBar: AppBar(
            backgroundColor: s.bg,
            elevation: 0,
            leading: IconButton(
              tooltip: 'Cancel',
              icon: Icon(Icons.arrow_back, color: s.ink),
              onPressed: _leave,
            ),
            title: Text(f.editing ? 'Edit scene' : 'New scene',
                style: TextStyle(
                    color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
          ),
          body: SafeArea(
            child: Column(children: [
              Expanded(
                child: ListView(
                  padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
                  children: [
                    if (f.error.isNotEmpty) ...[
                      _note(f.error, warn: true),
                      const SizedBox(height: 12),
                    ],
                    if (f.tried.isNotEmpty) ...[
                      _triedCard(f),
                      const SizedBox(height: 12),
                    ],
                    _field(_name, 'Name', hint: 'e.g. Movie night'),
                    _helper(_name.text.trim().isEmpty
                        ? 'Its name is what you say to run it.'
                        : 'Its name is what you say: '
                            '"${_name.text.trim().toLowerCase()}".'),
                    if (f.whom) ...[
                      _heading("WHO IT'S FOR"),
                      Wrap(spacing: 8, children: [
                        _chip('Everyone', !_private,
                            () => _change(() => _private = false)),
                        _chip('Just me', _private,
                            () => _change(() => _private = true)),
                      ]),
                      _helper(_private
                          ? 'Only your voice runs it, and only you see it.'
                          : 'Anyone in the house can say it.'),
                    ],
                    _heading("WHAT IT'S FOR"),
                    _field(_desc, '',
                        hint: 'Optional: say it in your own words',
                        lines: 3),
                    _aiBlock(f),
                    _heading('DEVICES'),
                    for (var i = 0; i < _parts.length; i++) _partCard(i),
                    _addButton(f),
                  ],
                ),
              ),
              _saveBar(f),
            ]),
          ),
        ),
      ),
    );
  }

  // ── pieces ─────────────────────────────────────────────────────────

  Widget _heading(String text) => Padding(
        padding: const EdgeInsets.fromLTRB(2, 20, 2, 8),
        child: Text(text,
            style: TextStyle(
                color: s.inkFaint,
                fontSize: 11.5,
                fontWeight: FontWeight.w700,
                letterSpacing: 0.8)),
      );

  Widget _helper(String text) => Padding(
        padding: const EdgeInsets.fromLTRB(4, 6, 4, 0),
        child: Text(text, style: TextStyle(color: s.inkFaint, fontSize: 12)),
      );

  Widget _note(String text, {bool warn = false}) => Container(
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: (warn ? const Color(0xFFD29922) : s.accent)
              .withValues(alpha: 0.10),
          border: Border.all(
              color: warn ? const Color(0xFFD29922) : s.border),
          borderRadius: BorderRadius.circular(10),
        ),
        child: Text(text,
            style: TextStyle(color: s.ink, fontSize: 13.5, height: 1.35)),
      );

  Widget _field(TextEditingController c, String label,
      {String hint = '', int lines = 1}) {
    return TextField(
      controller: c,
      minLines: lines,
      maxLines: lines,
      textCapitalization: TextCapitalization.sentences,
      style: TextStyle(color: s.ink, fontSize: 15),
      decoration: InputDecoration(
        labelText: label.isEmpty ? null : label,
        hintText: hint,
        hintStyle: TextStyle(color: s.inkFaint),
        labelStyle: TextStyle(color: s.inkDim),
        filled: true,
        fillColor: s.surface,
        contentPadding:
            const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: BorderSide(color: s.border),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: BorderSide(color: s.accent, width: 1.5),
        ),
      ),
    );
  }

  Widget _chip(String label, bool on, VoidCallback tap) => ChoiceChip(
        label: Text(label),
        selected: on,
        onSelected: (_) => tap(),
      );

  Widget _triedCard(SceneBuilderFrame f) => Container(
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: s.surface,
          border: Border.all(color: s.accent.withValues(alpha: 0.5)),
          borderRadius: BorderRadius.circular(10),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('TRIED JUST NOW — NOTHING SAVED',
                style: TextStyle(
                    color: s.inkFaint,
                    fontSize: 11,
                    fontWeight: FontWeight.w700,
                    letterSpacing: 0.6)),
            const SizedBox(height: 6),
            for (final t in f.tried)
              Padding(
                padding: const EdgeInsets.only(top: 2),
                child: Text('• $t',
                    style: TextStyle(color: s.ink, fontSize: 13.5)),
              ),
          ],
        ),
      );

  /// What the AI suggests, as registration shows it: a box beside your
  /// words; tap it to put it in, then change it as you like.
  Widget _aiBlock(SceneBuilderFrame f) {
    final text = f.aiText;
    final showSuggestion =
        text.isNotEmpty && text != _desc.text.trim() && !s.sceneAiBusy;
    // The name changed since it was asked: its words may not fit now.
    final stale = text.isNotEmpty &&
        f.aiFor.trim().toLowerCase() != _name.text.trim().toLowerCase();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (s.sceneAiBusy)
          Padding(
            padding: const EdgeInsets.fromLTRB(4, 10, 4, 0),
            child: Row(children: [
              SizedBox(
                  width: 14,
                  height: 14,
                  child: CircularProgressIndicator(
                      strokeWidth: 2, color: s.inkDim)),
              const SizedBox(width: 10),
              Expanded(
                child: Text('The AI is writing a description…',
                    style: TextStyle(color: s.inkDim, fontSize: 13)),
              ),
            ]),
          ),
        if (showSuggestion)
          Padding(
            padding: const EdgeInsets.only(top: 10),
            child: InkWell(
              borderRadius: BorderRadius.circular(12),
              onTap: () => _change(() => _desc.text = text),
              child: Container(
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: s.accent.withValues(alpha: 0.06),
                  border: Border.all(
                      color: s.accent.withValues(alpha: 0.6), width: 1.2),
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(children: [
                      Icon(Icons.auto_awesome_outlined,
                          size: 13, color: s.accent),
                      const SizedBox(width: 6),
                      Text('AI SUGGESTION · TAP TO USE',
                          style: TextStyle(
                              color: s.inkDim,
                              fontSize: 11,
                              fontWeight: FontWeight.w700,
                              letterSpacing: 0.5)),
                    ]),
                    const SizedBox(height: 6),
                    Text(text,
                        style: TextStyle(
                            color: s.ink, fontSize: 14, height: 1.35)),
                  ],
                ),
              ),
            ),
          ),
        if (f.aiError.isNotEmpty && !s.sceneAiBusy) _helper(f.aiError),
        if (f.aiAvailable)
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton.icon(
              onPressed: s.sceneAiBusy || _name.text.trim().isEmpty
                  ? null
                  : () => s.askSceneAi(_compose()),
              style: TextButton.styleFrom(
                foregroundColor: s.accent,
                padding: const EdgeInsets.symmetric(horizontal: 6),
              ),
              icon: const Icon(Icons.auto_awesome_outlined, size: 15),
              label: Text(
                  text.isEmpty
                      ? 'Ask the AI to suggest'
                      : stale
                          ? 'The name changed — ask the AI again'
                          : 'Ask the AI again',
                  style: const TextStyle(
                      fontSize: 13, fontWeight: FontWeight.w600)),
            ),
          ),
      ],
    );
  }

  Widget _partCard(int i) {
    final p = _parts[i];
    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.fromLTRB(14, 8, 6, 14),
      decoration: BoxDecoration(
        color: s.surface,
        border: Border.all(color: s.border),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(children: [
            Expanded(
              child: Text.rich(TextSpan(children: [
                TextSpan(
                    text: p.label,
                    style: TextStyle(
                        color: s.ink,
                        fontSize: 15,
                        fontWeight: FontWeight.w600)),
                if (p.room.isNotEmpty)
                  TextSpan(
                      text: '  ${p.room}',
                      style: TextStyle(color: s.inkFaint, fontSize: 12.5)),
              ])),
            ),
            if (_target(_frame!, p.device)?['secure'] == true)
              const Padding(
                padding: EdgeInsets.only(right: 2),
                child: Icon(Icons.shield_outlined,
                    size: 15, color: Color(0xFFD29922)),
              ),
            IconButton(
              tooltip: 'Take it out',
              icon: Icon(Icons.close, size: 18, color: s.cancelInk),
              onPressed: () => _change(() => _parts.removeAt(i)),
            ),
          ]),
          Padding(
            padding: const EdgeInsets.only(right: 8),
            child: _controls(p),
          ),
        ],
      ),
    );
  }

  Widget _hint(String text, {bool warn = false}) => Padding(
        padding: const EdgeInsets.only(top: 4),
        child: Text(text,
            style: TextStyle(
                color: warn ? const Color(0xFFD29922) : s.inkFaint,
                fontSize: 12.5)),
      );

  Widget _onOff(_Part p,
          {String onWord = 'On', String offWord = 'Off', double? level}) =>
      Wrap(spacing: 8, runSpacing: 6, children: [
        _chip(onWord, p.state['on'] == true, () => _change(() {
              p.state.remove('press');
              p.state['on'] = true;
              if (level != null && p.state['level'] == null) {
                p.state['level'] = level;
              }
            })),
        _chip(offWord, p.state['on'] == false, () => _change(() {
              p.state
                ..clear()
                ..['on'] = false;
            })),
      ]);

  Widget _slider(_Part p, String label, double min, double max) {
    final v = ((p.state['level'] as num?)?.toDouble() ?? max).clamp(min, max);
    return Row(children: [
      SizedBox(
        width: 104,
        child: Text('$label ${v.round()}',
            style: TextStyle(color: s.inkDim, fontSize: 12.5)),
      ),
      Expanded(
        child: Slider(
          value: v.toDouble(),
          min: min,
          max: max,
          activeColor: s.accent,
          onChanged: (x) => _change(() => p.state['level'] = x.roundToDouble()),
        ),
      ),
    ]);
  }

  /// The controls that fit the device -- the same as its page in Devices.
  Widget _controls(_Part p) {
    if (!p.known) {
      return _hint('No longer registered — it is left out when you save.',
          warn: true);
    }
    final min = (p.use['min'] as num?)?.toDouble() ?? 0;
    final max = (p.use['max'] as num?)?.toDouble() ?? 100;
    switch (p.kind) {
      case 'switch':
        return _onOff(p);
      case 'dimmer':
        return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          _onOff(p, level: max),
          if (p.state['on'] == true) _slider(p, 'Brightness', min, max),
        ]);
      case 'colour':
        return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          _onOff(p),
          if (p.state['on'] == true) ...[
            const SizedBox(height: 10),
            Wrap(spacing: 8, runSpacing: 8, children: [
              for (final c in _palette)
                GestureDetector(
                  onTap: () => _change(() => p.state['colour'] = c),
                  child: Container(
                    width: 30,
                    height: 30,
                    decoration: BoxDecoration(
                      color: Color.fromARGB(255, c[0], c[1], c[2]),
                      shape: BoxShape.circle,
                      border: Border.all(
                          color: _sameColour(p.state['colour'], c)
                              ? s.ink
                              : s.border,
                          width: 2),
                    ),
                  ),
                ),
            ]),
            _slider(p, 'Brightness', 0, 100),
          ],
        ]);
      case 'cover':
        final position = p.state.containsKey('level') && !p.state.containsKey('on');
        return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Wrap(spacing: 8, runSpacing: 6, children: [
            _chip('Open', p.state['on'] == true && !position,
                () => _change(() => p.state
                  ..clear()
                  ..['on'] = true)),
            _chip('Close', p.state['on'] == false && !position,
                () => _change(() => p.state
                  ..clear()
                  ..['on'] = false)),
            _chip('Position', position,
                () => _change(() => p.state
                  ..clear()
                  ..['level'] = 50.0)),
          ]),
          if (position) _slider(p, 'Open', 0, 100),
        ]);
      case 'servo':
        return _slider(p, 'Position', min, max);
      case 'ir':
        final buttons = [
          for (final b in (p.use['buttons'] as List? ?? const [])) b.toString()
        ];
        if (buttons.isEmpty) return _hint('No buttons learned for it yet.');
        return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Wrap(spacing: 8, runSpacing: 6, children: [
            for (final b in buttons)
              _chip(b.replaceAll('_', ' '), p.state['press'] == b,
                  () => _change(() => p.state
                    ..clear()
                    ..['press'] = b)),
          ]),
          if (p.state['press'] == null) _hint('Pick the button it presses.'),
        ]);
      case 'press':
        return _hint('It is pressed when the scene runs.');
      case 'display':
        final d =
            ((p.use['display'] as Map?) ?? const {}).cast<String, dynamic>();
        return DisplayWriter(
          key: ValueKey('scene-writer-${p.device}-${identityHashCode(p)}'),
          s: s,
          deviceId: p.device,
          rows: (d['rows'] as num?)?.toInt() ?? 2,
          cols: (d['cols'] as num?)?.toInt() ?? 16,
          initial: (p.state['show'] as Map?)?.cast<String, dynamic>(),
          onChanged: (a) => _change(() => p.state
            ..clear()
            ..['show'] = a),
        );
      default:
        return _hint(p.words.isEmpty ? 'Kept as it is.' : 'Kept: ${p.words}');
    }
  }

  static bool _sameColour(Object? a, List<int> c) =>
      a is List &&
      a.length == 3 &&
      a[0] == c[0] &&
      a[1] == c[1] &&
      a[2] == c[2];

  /// A new part starts with what the device most likely does in a scene.
  Map<String, dynamic> _startState(Map<String, dynamic> use) {
    switch ((use['kind'] ?? '').toString()) {
      case 'switch':
      case 'colour':
      case 'cover':
        return {'on': true};
      case 'dimmer':
        return {'on': true, 'level': (use['max'] as num?)?.toDouble() ?? 100};
      case 'servo':
        return {'level': 50.0};
      case 'press':
        return {'press': ''};
      default:
        return {}; // an IR button or a message: chosen on the card
    }
  }

  Widget _addButton(SceneBuilderFrame f) {
    final used = {for (final p in _parts) p.device};
    final free = [
      for (final t in f.targets)
        if (t is Map && !used.contains(t['value'])) t.cast<String, dynamic>()
    ];
    return SizedBox(
      height: 48,
      child: OutlinedButton.icon(
        onPressed: free.isEmpty ? null : () => _pickDevice(free),
        style: OutlinedButton.styleFrom(
          foregroundColor: s.accent,
          side: BorderSide(color: s.accent.withValues(alpha: 0.6)),
          shape:
              RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        ),
        icon: const Icon(Icons.add, size: 18),
        label: Text(free.isEmpty && f.targets.isNotEmpty
            ? 'Every device is in it'
            : 'Add device',
            style: const TextStyle(fontWeight: FontWeight.w600)),
      ),
    );
  }

  Future<void> _pickDevice(List<Map<String, dynamic>> free) async {
    final picked = await showModalBottomSheet<Map<String, dynamic>>(
      context: context,
      backgroundColor: s.surface,
      isScrollControlled: true,
      builder: (ctx) => SafeArea(
        child: ConstrainedBox(
          constraints: BoxConstraints(
              maxHeight: MediaQuery.of(ctx).size.height * 0.7),
          child: ListView(shrinkWrap: true, children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 16, 20, 8),
              child: Text('Add which device?',
                  style: TextStyle(
                      color: s.ink,
                      fontSize: 16,
                      fontWeight: FontWeight.w600)),
            ),
            for (final t in free)
              ListTile(
                leading: t['secure'] == true
                    ? const Icon(Icons.shield_outlined,
                        size: 18, color: Color(0xFFD29922))
                    : Icon(Icons.devices_other_outlined,
                        size: 18, color: s.inkDim),
                title: Text((t['label'] ?? '').toString(),
                    style: TextStyle(color: s.ink)),
                subtitle: (t['room'] ?? '').toString().isEmpty
                    ? null
                    : Text(t['room'].toString(),
                        style: TextStyle(color: s.inkFaint)),
                onTap: () => Navigator.pop(ctx, t),
              ),
          ]),
        ),
      ),
    );
    if (picked == null) return;
    final use = ((picked['use'] as Map?) ?? const {}).cast<String, dynamic>();
    _change(() => _parts.add(_Part((picked['value'] ?? '').toString(),
        label: (picked['label'] ?? '').toString(),
        room: (picked['room'] ?? '').toString(),
        use: use,
        state: _startState(use))));
  }

  Widget _saveBar(SceneBuilderFrame f) {
    // While the AI writes, the hub is busy with it: the screen waits.
    final busy = _saving || _trying || s.sceneAiBusy;
    return Container(
      padding: const EdgeInsets.fromLTRB(16, 10, 16, 14),
      decoration: BoxDecoration(
        color: s.bg,
        border: Border(top: BorderSide(color: s.border)),
      ),
      child: Row(children: [
        Expanded(
          child: OutlinedButton(
            style: OutlinedButton.styleFrom(
              foregroundColor: s.ink,
              side: BorderSide(color: s.accent, width: 1.5),
              padding: const EdgeInsets.symmetric(vertical: 14),
              shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12)),
            ),
            onPressed: busy || _parts.isEmpty
                ? null
                : () {
                    setState(() => _trying = true);
                    s.trySceneBuilder(_compose());
                  },
            child: Text(_trying ? 'Trying…' : 'Try it now',
                maxLines: 1,
                style: const TextStyle(
                    fontWeight: FontWeight.w600, fontSize: 14)),
          ),
        ),
        const SizedBox(width: 10),
        Expanded(
          child: FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: s.accent,
              foregroundColor: s.accentFg,
              padding: const EdgeInsets.symmetric(vertical: 14),
              shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12)),
            ),
            onPressed: busy
                ? null
                : () {
                    FocusScope.of(context).unfocus();
                    setState(() => _saving = true);
                    s.sendSceneBuilder(_compose());
                  },
            child: Text(_saving ? 'Saving…' : 'Save',
                style: const TextStyle(
                    fontWeight: FontWeight.w700, fontSize: 15)),
          ),
        ),
      ]),
    );
  }
}
