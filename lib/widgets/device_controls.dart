/// "Use it": the controls a device's page offers, fitted to what the device
/// is (the hub says which, in the record's `use`), and the display writer.
///
/// Every act is one `usedevice` action on the hub; a secure device is
/// asked for the PIN there, every time. The controls move at once (the
/// page shows what you asked for) and the device list is re-read after.
library;

import 'dart:convert';

import 'package:flutter/cupertino.dart';
import 'package:flutter/material.dart';

import '../app_state.dart';

/// The "Use it" card for one device record, or nothing when the device has
/// nothing to use (a reading, an old hub without `use`).
class DeviceUseCard extends StatefulWidget {
  final AppState s;
  final Map<String, dynamic> record;
  const DeviceUseCard({super.key, required this.s, required this.record});

  /// Whether a record has anything to show here.
  static bool hasControls(Map<String, dynamic>? record) {
    final use = record?['use'];
    if (use is! Map) return false;
    return const {
      'switch', 'dimmer', 'colour', 'cover', 'servo', 'press', 'ir',
      'display'
    }.contains(use['kind']);
  }

  @override
  State<DeviceUseCard> createState() => _DeviceUseCardState();
}

class _DeviceUseCardState extends State<DeviceUseCard> {
  double? _level;

  AppState get s => widget.s;
  Map<String, dynamic> get use =>
      (widget.record['use'] as Map?)?.cast<String, dynamic>() ?? const {};
  String get id => (widget.record['id'] ?? '').toString();
  Map get state => widget.record['state'] is Map
      ? widget.record['state'] as Map
      : const {};

  void _act(String what, [String value = '']) => s.useDevice(id, what, value);

  @override
  Widget build(BuildContext context) {
    final kind = (use['kind'] ?? '').toString();
    final body = switch (kind) {
      'switch' => _onOff(),
      'dimmer' => Column(children: [_slider('Brightness', 'level'), _onOff()]),
      'colour' => Column(children: [_colours(), _onOff()]),
      'cover' => Column(children: [
          _slider('Position', 'position', off: false),
          _buttons([('Open', 'open', Icons.arrow_upward),
                    ('Close', 'close', Icons.arrow_downward)]),
        ]),
      'servo' => _slider('Angle', 'position', off: false),
      'press' => _buttons([('Press', 'press', Icons.touch_app_outlined)]),
      'ir' => _irButtons(),
      'display' => DisplayWriter(
          s: s,
          deviceId: ((use['display'] as Map?)?['device'] ?? id).toString(),
          rows: ((use['display'] as Map?)?['rows'] as num?)?.toInt() ?? 2,
          cols: ((use['display'] as Map?)?['cols'] as num?)?.toInt() ?? 16),
      _ => const SizedBox.shrink(),
    };
    return Container(
      margin: const EdgeInsets.only(top: 12),
      padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
      decoration: BoxDecoration(
        color: s.bg,
        border: Border.all(color: s.border),
        borderRadius: BorderRadius.circular(14),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(children: [
            Icon(Icons.touch_app_outlined, size: 16, color: s.accent),
            const SizedBox(width: 6),
            Text('USE IT',
                style: TextStyle(
                    color: s.inkFaint,
                    fontSize: 11,
                    letterSpacing: 1,
                    fontWeight: FontWeight.w700)),
            if (widget.record['secure'] == true) ...[
              const Spacer(),
              Text('asks for your PIN',
                  style: TextStyle(color: s.inkFaint, fontSize: 11)),
            ],
          ]),
          const SizedBox(height: 10),
          body,
        ],
      ),
    );
  }

  Widget _onOff() => _buttons([
        ('On', 'on', Icons.power_settings_new),
        ('Off', 'off', Icons.power_off_outlined),
      ]);

  Widget _buttons(List<(String, String, IconData)> items) => Row(children: [
        for (var i = 0; i < items.length; i++) ...[
          if (i > 0) const SizedBox(width: 10),
          Expanded(
            child: OutlinedButton.icon(
              onPressed: s.isReady ? () => _act(items[i].$2) : null,
              style: OutlinedButton.styleFrom(
                foregroundColor: s.accent,
                side: BorderSide(color: s.accent),
                padding: const EdgeInsets.symmetric(vertical: 12),
              ),
              icon: Icon(items[i].$3, size: 17),
              label: Text(items[i].$1),
            ),
          ),
        ],
      ]);

  Widget _slider(String label, String what, {bool off = true}) {
    final min = (use['min'] as num?)?.toDouble() ?? 0;
    final max = (use['max'] as num?)?.toDouble() ?? 100;
    final now = (state['level'] as num?)?.toDouble();
    final value = (_level ?? now ?? (max / 2)).clamp(min, max).toDouble();
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Row(children: [
        SizedBox(
            width: 82,
            child: Text(label,
                style: TextStyle(color: s.inkDim, fontSize: 13))),
        Expanded(
          child: Slider(
            value: value,
            min: min,
            max: max,
            activeColor: s.accent,
            onChanged: s.isReady ? (v) => setState(() => _level = v) : null,
            // Sent when the finger lifts, not on every pixel of the drag.
            onChangeEnd: (v) => _act(what, v.round().toString()),
          ),
        ),
        SizedBox(
            width: 40,
            child: Text('${value.round()}%',
                textAlign: TextAlign.right,
                style: TextStyle(color: s.ink, fontSize: 13))),
      ]),
    );
  }

  static const _palette = <(String, Color)>[
    ('Warm white', Color(0xFFFFE4B5)),
    ('White', Color(0xFFFFFFFF)),
    ('Red', Color(0xFFFF0000)),
    ('Orange', Color(0xFFFF8000)),
    ('Yellow', Color(0xFFFFE000)),
    ('Green', Color(0xFF00FF40)),
    ('Cyan', Color(0xFF00E5FF)),
    ('Blue', Color(0xFF0040FF)),
    ('Purple', Color(0xFF8000FF)),
    ('Pink', Color(0xFFFF40B0)),
  ];

  Widget _colours() => Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Wrap(spacing: 10, runSpacing: 10, children: [
          for (final (name, c) in _palette)
            Tooltip(
              message: name,
              child: InkWell(
                customBorder: const CircleBorder(),
                onTap: s.isReady
                    ? () {
                        int ch(double v) => (v * 255).round().clamp(0, 255);
                        _act('colour', '${ch(c.r)},${ch(c.g)},${ch(c.b)}');
                      }
                    : null,
                child: Container(
                  width: 34,
                  height: 34,
                  decoration: BoxDecoration(
                    color: c,
                    shape: BoxShape.circle,
                    border: Border.all(color: s.border, width: 1.5),
                  ),
                ),
              ),
            ),
        ]),
      );

  Widget _irButtons() {
    final names = [
      for (final b in (use['buttons'] as List? ?? const [])) b.toString()
    ];
    if (names.isEmpty) {
      return Text('No buttons learned yet.',
          style: TextStyle(color: s.inkFaint, fontSize: 13));
    }
    return Wrap(spacing: 8, runSpacing: 8, children: [
      for (final b in names)
        ActionChip(
          label: Text(b.replaceAll('_', ' ')),
          onPressed: s.isReady ? () => _act('press', b) : null,
        ),
    ]);
  }
}

/// Write on a text display: one box per line, each placed left, middle or
/// right; live readings; several pages turned over, with the clock between
/// when asked; a preview of the glass.
///
/// On a board's page it sends straight away (Show it / Back to the clock).
/// In an automation's "Do" (`onChanged` given) it only fills in the action,
/// and "Share with the clock" is one more choice of how long.
class DisplayWriter extends StatefulWidget {
  final AppState s;
  final String deviceId;
  final int rows;
  final int cols;

  /// Editor mode: called with the display action's fields on every change.
  final ValueChanged<Map<String, dynamic>>? onChanged;

  /// Editor mode: the action being edited (pages or the older one `text`).
  final Map<String, dynamic>? initial;
  const DisplayWriter(
      {super.key,
      required this.s,
      required this.deviceId,
      required this.rows,
      required this.cols,
      this.onChanged,
      this.initial});

  @override
  State<DisplayWriter> createState() => _DisplayWriterState();
}

class _WriterLine {
  final TextEditingController text;
  String align;
  _WriterLine([String t = '', this.align = 'left'])
      : text = TextEditingController(text: t);
}

class _DisplayWriterState extends State<DisplayWriter> {
  final List<List<_WriterLine>> _pages = [];
  int _page = 0;
  int _line = 0; // the line "Add a reading" goes into
  double _pageS = 10;
  bool _clockBetween = false;
  double? _hold = 0; // 0 = keep it up; null = share with the clock

  AppState get s => widget.s;
  bool get _editor => widget.onChanged != null;
  int get _rows => widget.rows < 1 ? 1 : widget.rows;
  int get _cols => widget.cols < 1 ? 1 : widget.cols;

  @override
  void initState() {
    super.initState();
    final a = widget.initial ?? const {};
    final pages = a['pages'];
    if (pages is List && pages.isNotEmpty) {
      for (final p in pages) {
        _pages.add([
          for (final l in (p is List ? p : const []))
            if (l is Map)
              _WriterLine(_forPerson((l['text'] ?? '').toString()),
                  (l['align'] ?? 'left').toString()),
        ]);
      }
    } else if ((a['text'] ?? '').toString().isNotEmpty) {
      _pages.add([_WriterLine(_forPerson(a['text'].toString()))]);
    }
    if (_pages.isEmpty) _pages.add([]);
    for (final p in _pages) {
      while (p.length < _rows) {
        p.add(_WriterLine());
      }
    }
    if (a['page_s'] is num) _pageS = (a['page_s'] as num).toDouble();
    _clockBetween = a['clock_between'] == true;
    if (_editor) {
      _hold = a.containsKey('for_s') && a['for_s'] == null
          ? null
          : (a['for_s'] as num?)?.toDouble() ?? 0;
    }
  }

  @override
  void dispose() {
    for (final p in _pages) {
      for (final l in p) {
        l.text.dispose();
      }
    }
    super.dispose();
  }

  // ── readings: [power meter voltage] for the person, the token for the hub

  String _label(Map<String, dynamic> r) =>
      '[${(r['label'] ?? '').toString().replaceAll(' · ', ' ')}]';

  String _forHub(String text) {
    var out = text;
    for (final r in s.displayReadings) {
      out = out.replaceAll(_label(r), (r['token'] ?? '').toString());
    }
    return out;
  }

  String _forPerson(String text) {
    var out = text;
    for (final r in s.displayReadings) {
      final token = (r['token'] ?? '').toString();
      if (token.isNotEmpty) out = out.replaceAll(token, _label(r));
    }
    return out;
  }

  /// What the glass will show for one line: readings as their value now.
  String _shown(String text) {
    var out = text;
    for (final r in s.displayReadings) {
      final v = (r['value'] ?? '').toString();
      out = out.replaceAll(_label(r), v.isEmpty ? '--' : v);
    }
    return out.trim();
  }

  String _placed(String text, String align) {
    var t = text.length > _cols ? text.substring(0, _cols) : text;
    final room = _cols - t.length;
    if (align == 'middle') {
      final left = room ~/ 2;
      t = ' ' * left + t + ' ' * (room - left);
    } else if (align == 'right') {
      t = ' ' * room + t;
    } else {
      t = t + ' ' * room;
    }
    return t;
  }

  // ── what goes to the hub

  Map<String, dynamic> _action() => {
        'pages': [
          for (final p in _pages)
            [
              for (final l in p.take(_rows))
                {'text': _forHub(l.text.text.trim()), 'align': l.align},
            ],
        ],
        'page_s': _pageS.round(),
        'clock_between': _clockBetween,
        'for_s': _hold?.round(),
      };

  bool get _hasWords =>
      _pages.any((p) => p.any((l) => l.text.text.trim().isNotEmpty));

  void _changed() {
    setState(() {});
    final cb = widget.onChanged;
    if (cb != null) cb(_action());
  }

  Future<void> _addReading() async {
    final picked = await showModalBottomSheet<Map<String, dynamic>>(
      context: context,
      backgroundColor: s.surface,
      builder: (ctx) => SafeArea(
        child: s.displayReadings.isEmpty
            ? Padding(
                padding: const EdgeInsets.all(20),
                child: Text('Nothing in the house is reporting a reading yet.',
                    style: TextStyle(color: s.inkDim)))
            : ListView(shrinkWrap: true, children: [
                for (final r in s.displayReadings)
                  ListTile(
                    title: Text((r['label'] ?? '').toString(),
                        style: TextStyle(color: s.ink)),
                    trailing: Text((r['value'] ?? '').toString(),
                        style: TextStyle(color: s.inkFaint)),
                    onTap: () => Navigator.pop(ctx, r),
                  ),
              ]),
      ),
    );
    if (picked == null) return;
    final line = _pages[_page][_line.clamp(0, _rows - 1)].text;
    final at = line.selection.isValid ? line.selection.start : line.text.length;
    final insert = _label(picked);
    final t = line.text;
    line.value = TextEditingValue(
      text: t.substring(0, at) + insert + t.substring(at),
      selection: TextSelection.collapsed(offset: at + insert.length),
    );
    _changed();
  }

  void _addPage() {
    _pages.add([for (var i = 0; i < _rows; i++) _WriterLine()]);
    _page = _pages.length - 1;
    _line = 0;
    _changed();
  }

  void _removePage() {
    if (_pages.length <= 1) return;
    final gone = _pages.removeAt(_page);
    // Its fields are still on screen until the next frame.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      for (final l in gone) {
        l.text.dispose();
      }
    });
    _page = _page.clamp(0, _pages.length - 1);
    _changed();
  }

  // ── the pieces

  Widget _heading(String text) => Padding(
        padding: const EdgeInsets.only(top: 12, bottom: 6),
        child: Text(text,
            style: TextStyle(
                fontSize: 11,
                letterSpacing: 0.6,
                fontWeight: FontWeight.w600,
                color: s.inkFaint)),
      );

  Widget _preview() {
    final page = _pages[_page];
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: const Color(0xFF14321A),
        borderRadius: BorderRadius.circular(10),
      ),
      child: FittedBox(
        fit: BoxFit.scaleDown,
        alignment: Alignment.centerLeft,
        child: Text(
          [
            for (var i = 0; i < _rows; i++)
              _placed(i < page.length ? _shown(page[i].text.text) : '',
                  i < page.length ? page[i].align : 'left'),
          ].join('\n'),
          style: const TextStyle(
              fontFamily: 'monospace',
              fontSize: 15,
              height: 1.4,
              color: Color(0xFFA8E66A)),
        ),
      ),
    );
  }

  Widget _alignButtons(_WriterLine l) {
    const icons = {
      'left': Icons.format_align_left,
      'middle': Icons.format_align_center,
      'right': Icons.format_align_right,
    };
    return ToggleButtons(
      isSelected: [for (final k in icons.keys) l.align == k],
      onPressed: (i) {
        l.align = icons.keys.elementAt(i);
        _changed();
      },
      borderRadius: BorderRadius.circular(20),
      constraints: const BoxConstraints(minWidth: 34, minHeight: 34),
      color: s.inkFaint,
      selectedColor: s.ink,
      fillColor: s.accent.withValues(alpha: 0.14),
      borderColor: s.border,
      selectedBorderColor: s.border,
      children: [
        for (final e in icons.entries)
          Icon(e.value,
              size: 17,
              semanticLabel: e.key == 'middle'
                  ? 'Middle'
                  : (e.key == 'left' ? 'Left' : 'Right')),
      ],
    );
  }

  Widget _lineRow(int i) {
    final l = _pages[_page][i];
    final long = _shown(l.text.text).length > _cols;
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Row(crossAxisAlignment: CrossAxisAlignment.center, children: [
        SizedBox(
          width: 44,
          child: Text('Line ${i + 1}',
              style: TextStyle(fontSize: 11.5, color: s.inkDim)),
        ),
        Expanded(
          child: TextField(
            controller: l.text,
            onTap: () => _line = i,
            onChanged: (_) {
              _line = i;
              _changed();
            },
            style: TextStyle(
                color: s.ink, fontFamily: 'monospace', fontSize: 14),
            decoration: InputDecoration(
              isDense: true,
              hintText: i == 0 ? 'Type here' : 'leave empty',
              hintStyle: TextStyle(color: s.inkFaint),
              errorText: long ? 'Too long: it will be cut' : null,
              contentPadding:
                  const EdgeInsets.symmetric(horizontal: 10, vertical: 10),
              enabledBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(10),
                  borderSide: BorderSide(color: s.border)),
              focusedBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(10),
                  borderSide: BorderSide(color: s.accent)),
            ),
          ),
        ),
        const SizedBox(width: 6),
        _alignButtons(l),
      ]),
    );
  }

  Widget _chip(String label, bool on, VoidCallback tap) => ChoiceChip(
        label: Text(label),
        selected: on,
        onSelected: (_) => tap(),
      );

  /// "2 min 30 s", "1 h 5 min", "45 s".
  static String _say(double seconds) {
    final t = seconds.round();
    final h = t ~/ 3600, m = (t % 3600) ~/ 60, sec = t % 60;
    return [
      if (h > 0) '$h h',
      if (m > 0) '$m min',
      if (sec > 0 || t == 0) '$sec s',
    ].join(' ');
  }

  /// A time wheel in a sheet: hours (when allowed), minutes, seconds.
  Future<double?> _pickTime(double start,
      {required double min, required bool hours}) async {
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
                mode: hours
                    ? CupertinoTimerPickerMode.hms
                    : CupertinoTimerPickerMode.ms,
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
    return secs < min ? min : secs;
  }

  @override
  Widget build(BuildContext context) {
    final several = _pages.length > 1;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      _preview(),
      Padding(
        padding: const EdgeInsets.only(top: 6, bottom: 8),
        child: Text(
            several
                ? 'Preview of page ${_page + 1} of ${_pages.length}'
                : 'Preview',
            style: TextStyle(fontSize: 11.5, color: s.inkFaint)),
      ),
      Wrap(spacing: 8, runSpacing: 6, children: [
        for (var i = 0; i < _pages.length; i++)
          _chip('Page ${i + 1}', i == _page, () {
            setState(() {
              _page = i;
              _line = 0;
            });
          }),
        ActionChip(
          avatar: Icon(Icons.add, size: 16, color: s.inkDim),
          label: const Text('Add page'),
          onPressed: _addPage,
        ),
        if (several)
          ActionChip(
            avatar: Icon(Icons.delete_outline, size: 16, color: s.inkDim),
            label: Text('Remove page ${_page + 1}'),
            onPressed: _removePage,
          ),
      ]),
      const SizedBox(height: 10),
      for (var i = 0; i < _rows; i++) _lineRow(i),
      Row(children: [
        TextButton.icon(
          onPressed: _addReading,
          icon: Icon(Icons.show_chart, size: 17, color: s.accent),
          label: Text('Add a reading', style: TextStyle(color: s.accent)),
        ),
        const Spacer(),
        Text('$_cols letters fit on each line',
            style: TextStyle(fontSize: 11.5, color: s.inkFaint)),
      ]),
      _heading(several ? 'EACH PAGE STAYS' : 'THE CLOCK'),
      Wrap(spacing: 8, runSpacing: 6, children: [
        if (several)
          ActionChip(
            avatar: Icon(Icons.timer_outlined, size: 16, color: s.accent),
            label: Text(_say(_pageS)),
            onPressed: () async {
              final t = await _pickTime(_pageS, min: 2, hours: false);
              if (t != null) {
                _pageS = t;
                _changed();
              }
            },
          ),
        FilterChip(
          label: const Text('Clock between'),
          selected: _clockBetween,
          onSelected: (v) {
            _clockBetween = v;
            _changed();
          },
        ),
      ]),
      _heading('HOW LONG'),
      Wrap(spacing: 8, runSpacing: 6, children: [
        _chip('Keep it up', _hold == 0, () {
          _hold = 0;
          _changed();
        }),
        if (_editor)
          _chip('Share with the clock', _hold == null, () {
            _hold = null;
            _changed();
          }),
        // Any time: tap, set it on the wheel.
        ChoiceChip(
          avatar: Icon(Icons.timer_outlined, size: 16, color: s.accent),
          label: Text((_hold ?? 0) > 0 ? 'For ${_say(_hold!)}' : 'For a time…'),
          selected: (_hold ?? 0) > 0,
          onSelected: (_) async {
            final t = await _pickTime(
                (_hold ?? 0) > 0 ? _hold! : 600, min: 5, hours: true);
            if (t != null) {
              _hold = t;
              _changed();
            }
          },
        ),
      ]),
      if (!_editor) ...[
        const SizedBox(height: 14),
        Row(children: [
          Expanded(
            child: OutlinedButton(
              onPressed: s.isReady
                  ? () => s.useDevice(widget.deviceId, 'clear')
                  : null,
              style: OutlinedButton.styleFrom(
                foregroundColor: s.ink,
                side: BorderSide(color: s.accent, width: 1.5),
                shape: const StadiumBorder(),
                padding: const EdgeInsets.symmetric(vertical: 12),
              ),
              child: const Text('Back to clock',
                  style: TextStyle(fontWeight: FontWeight.w700)),
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: FilledButton(
              onPressed: s.isReady && _hasWords
                  ? () => s.useDevice(
                      widget.deviceId, 'show', jsonEncode(_action()))
                  : null,
              style: FilledButton.styleFrom(
                backgroundColor: s.accent,
                foregroundColor: s.accentFg,
                shape: const StadiumBorder(),
                padding: const EdgeInsets.symmetric(vertical: 12),
              ),
              child: const Text('Show it',
                  style: TextStyle(fontWeight: FontWeight.w700)),
            ),
          ),
        ]),
      ],
    ]);
  }
}
