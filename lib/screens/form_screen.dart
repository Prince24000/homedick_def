import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../app_state.dart';

/// A whole set of fields on ONE screen, with help under each.
///
/// This exists because Home Assistant's config flows are forms, and asking a
/// six-field schema as six separate questions is how "what is the host
/// field?" happens: you cannot see that host and port belong together, you
/// cannot look back at what you already typed, and a bare machine name like
/// `host` arrives on screen with nothing to explain it.
class FormScreen extends StatefulWidget {
  final AppState appState;
  const FormScreen({super.key, required this.appState});

  @override
  State<FormScreen> createState() => _FormScreenState();
}

class _FormScreenState extends State<FormScreen> {
  final Map<String, TextEditingController> _controllers = {};
  final Map<String, String> _picked = {};

  /// Secret fields shown in the clear right now, by field name. Reset
  /// with the controllers, so revealing one password never carries over.
  final Set<String> _revealed = {};
  String? _builtFor;
  bool _didSchedulePop = false;

  @override
  void initState() {
    super.initState();
    // THE DOCK'S TAP BELONGS TO THIS SCREEN while it is up (2026-09-11).
    // Before this it fell through to a house command the hub refused as
    // busy; now it arms the field list and listens.
    widget.appState.screenVoiceRequest = _startVoice;
  }

  @override
  void dispose() {
    final s = widget.appState;
    if (s.screenVoiceRequest == _startVoice) s.screenVoiceRequest = null;
    s.disarmScreenChoice();
    for (final c in _controllers.values) {
      c.dispose();
    }
    super.dispose();
  }

  // ── FILLING THIS IN BY VOICE ────────────────────────────────────────
  //
  // Two steps, because a form is not a question: SAY THE FIELD, then say
  // what goes in it. The field list is an ordinary local choice -- same
  // matcher, same "repeat", same "cancel" -- and the value is a dictation,
  // which matches nothing and hands the words over as heard.
  //
  // Nothing arms itself. The window opens on the dock's tap and on nothing
  // else: a mic that opens itself behind a text field is what answered a
  // PIN prompt with silence, and the rule that came out of that is "never
  // without being asked".

  /// The last frame's fields, for the voice to name. Read at arm time so a
  /// re-armed list is always the one on screen.
  FormFrame? _form;

  void _startVoice() {
    final s = widget.appState;
    final form = _form;
    if (form == null) return;
    _armFields(s, form, speak: true);
  }

  /// `say` is spoken BEFORE the list re-arms, and is how a filled field is
  /// confirmed: one utterance, then the window. Acknowledging separately
  /// would open the mic while the phone was still speaking, and it would
  /// hear itself.
  void _armFields(AppState s, FormFrame form,
      {bool speak = false, String say = ''}) {
    final options = <Map<String, dynamic>>[
      for (final f in form.fields)
        {
          'value': 'field:${f.name}',
          'label': f.label.isEmpty ? f.name : f.label,
        },
      {'value': '__submit', 'label': 'continue'},
    ];
    s.armScreenChoice(
      prompt: say.isEmpty
          ? 'Say a field to fill in, or say continue.'
          : '$say Say another field, or say continue.',
      options: options,
      speakPrompt: speak || say.isNotEmpty,
      repeatLine: 'Say a field to fill in: '
          '${form.fields.map((f) => f.label.isEmpty ? f.name : f.label).join(', ')}. '
          'Or say continue.',
      onCancel: () => s.sendCancel(),
      onPick: (value, heard) {
        if (!mounted) return;
        if (value == '__submit') {
          _submit(s, form);
          return;
        }
        final name = value.substring('field:'.length);
        final field = form.fields.firstWhere((f) => f.name == name,
            orElse: () => form.fields.first);
        _fillByVoice(s, form, field);
      },
    );
  }

  void _fillByVoice(AppState s, FormFrame form, FlowField field) {
    final label = field.label.isEmpty ? field.name : field.label;
    // A SECRET IS TYPED. Never dictated (a password spoken aloud is a
    // password overheard) and never read back. The field list stays armed
    // so the next field can still be said.
    if (field.secret) {
      _armFields(s, form, say: '$label has to be typed — it is a secret.');
      return;
    }
    // A field with a fixed set of answers is a CHOICE, not dictation:
    // "which protocol?" wants one of three words, and matching them beats
    // transcribing them.
    if (field.choices.isNotEmpty) {
      s.armScreenChoice(
        prompt: '$label. Say one of: ${field.choices.join(', ')}.',
        speakPrompt: true,
        repeatLine: '$label. Say one of: ${field.choices.join(', ')}.',
        options: [
          for (final c in field.choices) {'value': c, 'label': c}
        ],
        onCancel: () => _armFields(s, form),
        onPick: (value, heard) {
          if (!mounted) return;
          setState(() => _picked[field.name] = value);
          _armFields(s, form, say: '$label, $value.');
        },
      );
      return;
    }
    s.armScreenDictation(
      prompt: '$label?',
      onCancel: () => _armFields(s, form),
      onText: (heard) {
        if (!mounted) return;
        // The transcriber writes a sentence ("192.168.1.42."); a field
        // value is not one, and the full stop went to Home Assistant with
        // the address.
        final text = heard.trim().replaceFirst(RegExp(r'[\s.!?,;:]+$'), '');
        if (text.isEmpty) {
          _armFields(s, form, say: 'I did not catch that.');
          return;
        }
        setState(() => _controllers[field.name]?.text = text);
        _armFields(s, form, say: '$label, $text.');
      },
    );
  }

  void _prepare(FormFrame form) {
    // Rebuild controllers only when the FORM changes, not on every notify —
    // otherwise every rebuild wipes what is being typed.
    // Keyed on the hub's question serial too: the SAME form asked again
    // (a refused password) must not come back pre-filled with the secret.
    // Kept for the voice: which fields are on screen right now.
    _form = form;
    final key = '${widget.appState.questionSerial}|${form.signature}';
    if (_builtFor == key) return;
    _builtFor = key;
    // A NEW form means the old vocabulary is wrong -- drop whatever the
    // voice was armed with rather than letting a field name from the last
    // form answer this one.
    // POST-FRAME, NOT NOW: _prepare runs inside build, and disarming
    // moves the flow's state, which notifies AppState, which is
    // "setState() called during build" -- the very trap the comment above
    // this method warns about for controller listeners.
    // The old boxes are still attached to the TextFields being rebuilt in
    // THIS frame, so they are disposed after it, not in the middle of it.
    final spent = List<TextEditingController>.of(_controllers.values);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      for (final c in spent) {
        c.dispose();
      }
      if (mounted) widget.appState.disarmScreenChoice();
    });
    _controllers.clear();
    _picked.clear();
    _revealed.clear();
    for (final f in form.fields) {
      if (f.choices.isNotEmpty) {
        // A yes/no field's default arrives as Python's bool ("True" /
        // "False" once stringified), which matches neither chip: nothing
        // looked picked while "True" was what Continue would send.
        final d = f.defaultValue;
        final low = d.toLowerCase();
        _picked[f.name] = f.choices.contains(d)
            ? d
            : (low == 'true' && f.choices.contains('yes'))
                ? 'yes'
                : (low == 'false' && f.choices.contains('no'))
                    ? 'no'
                    : (d.isNotEmpty ? d : f.choices.first);
      } else {
        _controllers[f.name] =
            TextEditingController(text: f.defaultValue);
      }
    }
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
    final form = s.pendingForm;

    if (form == null) {
      if (!_didSchedulePop) {
        _didSchedulePop = true;
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (!mounted) return;
          final route = ModalRoute.of(context);
          // Same double-pop guard the other question screens use: a route
          // mid-exit is not current, and popping again removes HOME.
          if (route != null &&
              route.isCurrent &&
              widget.appState.pendingForm == null) {
            Navigator.of(context).pop();
          } else {
            _didSchedulePop = false;
          }
        });
      }
      return const SizedBox.shrink();
    }
    _didSchedulePop = false;
    _prepare(form);

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
              // Back steps out to the last screen (PopScope tells the
              // hub; the ending stays quiet). sendCancel here made the
              // back arrow behave like X -- cancel everything, jump home.
              icon: Icon(Icons.arrow_back, color: s.ink),
              onPressed: () => Navigator.of(context).maybePop(),
            ),
            title: Text(
              form.title.isEmpty ? 'Details' : form.title,
              style: TextStyle(
                  color: s.ink, fontSize: 17, fontWeight: FontWeight.w600),
            ),
          ),
          body: SafeArea(
            child: SingleChildScrollView(
              // Bottom padding clears the floating voice dock.
              padding: const EdgeInsets.fromLTRB(16, 14, 16, 80),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  if (form.note.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 16),
                      child: Text(form.note,
                          style: TextStyle(
                              color: s.inkDim, fontSize: 13.5, height: 1.45)),
                    ),
                  for (final f in form.fields) _field(s, f),
                  const SizedBox(height: 8),
                  FilledButton(
                    style: FilledButton.styleFrom(
                      backgroundColor: s.accent,
                      foregroundColor: s.accentFg,
                      padding: const EdgeInsets.symmetric(vertical: 14),
                      shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(12)),
                    ),
                    onPressed: () => _submit(s, form),
                    child: const Text('Continue',
                        style: TextStyle(
                            fontWeight: FontWeight.w700, fontSize: 15)),
                  ),
                  const SizedBox(height: 8),
                  OutlinedButton(
                    // The DISCONNECT dress: every cancel wears the
                    // disconnect button's red/burgundy (her rule,
                    // 2026-08-27).
                    style: s.cancelButtonStyle(),
                    onPressed: () => s.sendCancel(),
                    child: const Text('Cancel',
                        style: TextStyle(
                            fontWeight: FontWeight.w600, fontSize: 15)),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _field(AppState s, FlowField f) {
    final isChoice = f.choices.isNotEmpty;
    return Padding(
      padding: const EdgeInsets.only(bottom: 18),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Text(f.label,
                  style: TextStyle(
                      color: s.ink,
                      fontSize: 14,
                      fontWeight: FontWeight.w700)),
              if (f.required)
                Padding(
                  padding: const EdgeInsets.only(left: 6),
                  child: Text('required',
                      style: TextStyle(color: s.accent, fontSize: 11)),
                ),
            ],
          ),
          // THE HELP LINE. A field called "host" means nothing on its own;
          // this is where it gets to say what to actually type.
          if (f.help.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 4, bottom: 8),
              child: Text(f.help,
                  style: TextStyle(
                      color: s.inkDim, fontSize: 12.5, height: 1.4)),
            )
          else
            const SizedBox(height: 8),
          if (isChoice)
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                for (final c in f.choices)
                  OutlinedButton(
                    style: OutlinedButton.styleFrom(
                      backgroundColor: _picked[f.name] == c
                          ? s.accent.withValues(alpha: 0.16)
                          : Colors.transparent,
                      foregroundColor: s.accent,
                      side: BorderSide(
                          color: _picked[f.name] == c ? s.accent : s.border,
                          width: _picked[f.name] == c ? 1.4 : 1),
                      padding: const EdgeInsets.symmetric(
                          horizontal: 18, vertical: 10),
                      shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(20)),
                    ),
                    onPressed: () => setState(() => _picked[f.name] = c),
                    child: Text(c,
                        style: const TextStyle(
                            fontSize: 13.5, fontWeight: FontWeight.w600)),
                  ),
              ],
            )
          else
            TextField(
              controller: _controllers[f.name],
              // THE HUB SAYS WHICH FIELDS ARE SECRET (`secret: true` on a
              // password, PSK, API key or token). Masked, with an eye, and
              // never offered to the keyboard's learning -- for every
              // field: nothing typed here is prose.
              obscureText: f.secret && !_revealed.contains(f.name),
              autocorrect: false,
              enableSuggestions: !f.secret,
              enableIMEPersonalizedLearning: false,
              keyboardType: (!f.secret &&
                      (f.type == 'integer' || f.type == 'float'))
                  ? const TextInputType.numberWithOptions(decimal: true)
                  : TextInputType.text,
              style: TextStyle(color: s.ink),
              decoration: InputDecoration(
                isDense: true,
                filled: true,
                fillColor: s.surface,
                hintText: f.defaultValue.isEmpty ? null : f.defaultValue,
                hintStyle: TextStyle(color: s.inkFaint),
                suffixIcon: f.secret
                    ? IconButton(
                        tooltip: _revealed.contains(f.name) ? 'Hide' : 'Show',
                        icon: Icon(
                            _revealed.contains(f.name)
                                ? Icons.visibility_off_outlined
                                : Icons.visibility_outlined,
                            color: s.inkDim),
                        onPressed: () => setState(() {
                          if (!_revealed.remove(f.name)) {
                            _revealed.add(f.name);
                          }
                        }),
                      )
                    : null,
                border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(8),
                    borderSide: BorderSide(color: s.border)),
                focusedBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(8),
                    borderSide: BorderSide(color: s.accent)),
              ),
            ),
        ],
      ),
    );
  }

  void _submit(AppState s, FormFrame form) {
    // A voice "continue" armed against an EARLIER form must not submit
    // this one with that form's field list.
    if (!identical(s.pendingForm, form)) return;
    final values = <String, dynamic>{};
    final missing = <String>[];
    for (final f in form.fields) {
      // A secret is sent exactly as typed: a password's own spaces are
      // part of it, and the hub hands string answers to HA unstripped.
      final text = _controllers[f.name]?.text ?? '';
      final raw = f.choices.isNotEmpty
          ? (_picked[f.name] ?? '')
          : (f.secret && text.trim().isNotEmpty ? text : text.trim());
      final value = raw.isEmpty ? f.defaultValue : raw;
      if (value.isEmpty) {
        if (f.required) missing.add(f.label);
        continue;
      }
      values[f.name] = value;
    }
    if (missing.isNotEmpty) {
      // Say WHICH, on the screen the fields are on — a server-side refusal
      // would arrive after the form is gone.
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        backgroundColor: s.surface,
        content: Text('Still needed: ${missing.join(', ')}',
            style: TextStyle(color: s.ink)),
      ));
      return;
    }
    s.sendFormResponse(values);
  }
}