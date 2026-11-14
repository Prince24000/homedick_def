/// The board, drawn, with its pins tappable.
///
/// This replaces nine questions with one picture. Setting up a board used to
/// mean: pick a category, pick a device type, name it, pick a pin from a
/// list, repeat — with no way to see the board, no sense of how much of it
/// was used, and no way to change your mind about pin 13 without starting
/// over.
///
/// THE RULES ARE NOT DUPLICATED HERE. This screen knows nothing about GPIO34
/// being input-only or ADC2 dying when Wi-Fi comes up. The hub sends each pin
/// the ROLES it satisfies ('in', 'adc', …) and each device slot the role it
/// NEEDS, and the intersection decides what a tap offers. So the one pin
/// table in the project stays in Python, and the worst this file can do is
/// draw it badly — it cannot disagree with it.
///
/// The hub validates the whole layout again when it comes back. Not from
/// distrust: this is a second implementation in another language on its own
/// release schedule, so it will drift eventually, and drift caught at the
/// boundary is a warning while drift discovered later is a board that does
/// nothing.
library;

import 'package:flutter/material.dart';

import '../app_state.dart';

class PinMapScreen extends StatefulWidget {
  final AppState appState;
  const PinMapScreen({super.key, required this.appState});

  @override
  State<PinMapScreen> createState() => _PinMapScreenState();
}

/// One device the person has placed, while they are still placing things.
class _Placed {
  final String typeId;
  String name;
  final Map<String, int> pins;      // slot key -> GPIO

  /// "Use it anyway" was chosen for one of its pins. Sent to the hub as
  /// `confirmed`, which is the ONLY thing that lets a won't-work pin
  /// through `_apply` -- without it the hub silently dropped the device.
  bool confirmed;
  _Placed(this.typeId, this.name, this.pins, {this.confirmed = false});
}

class _PinMapScreenState extends State<PinMapScreen> {
  final List<_Placed> _placed = [];

  /// EDITING STARTS FROM WHAT IS THERE. The hub sends the board's existing
  /// devices in the pin_map frame (`items`), and this screen used to ignore
  /// them: _placed started empty, so a board with two devices opened as a
  /// bare chip -- nothing green, nothing to keep -- and a Save from that
  /// screen described a board with NOTHING on it. The hub's "retire it
  /// instead" guard is the only reason that never wiped a real board.
  /// A fresh board sends items: [], so this seeds nothing there.
  @override
  void initState() {
    super.initState();
    _adopt(s.pinMap);
  }

  /// The frame on screen. A NEWER pin_map frame (the hub asked again
  /// while this screen was still up -- no second route is pushed for it)
  /// replaces all of it: board, catalogue and devices. It used to swap
  /// the catalogue under a board seeded from the OLD frame's items.
  PinMapFrame? _frame;

  /// Start from a frame: its catalogue captured, its devices placed, and
  /// nothing half-done carried over from whatever was on screen before.
  void _adopt(PinMapFrame? frame) {
    _frame = frame;
    _catCache = frame?.catalogue ?? const {};
    _placed.clear();
    _awaitingType = null;
    _awaitingPins.clear();
    _awaitingSlot = 0;
    _awaitingConfirmed = false;
    _movingPlaced = null;
    _movingSlotKey = null;
    _leaving = false;
    _voiceReleased = false;
    for (final item in (frame?.items ?? const [])) {
      if (item is! Map) continue;
      final typeId = item['type_id'] as String?;
      final name = item['name'] as String?;
      final pins = item['pins'];
      if (typeId == null || name == null || pins is! Map) continue;
      // A line the catalogue no longer has is not wired to anything the
      // firmware drives -- but the hub counted its pin as taken for ever.
      // Only the catalogue's own lines are carried (all of them when the
      // type itself is unknown, so the record is shown as it is).
      final known = {
        for (final sl in (_type(typeId)?['slots'] as List? ?? const []))
          if (sl is Map) sl['key'].toString()
      };
      final placed = _Placed(
        typeId,
        name,
        {
          for (final e in pins.entries)
            if (e.value is int &&
                (known.isEmpty || known.contains(e.key.toString())))
              e.key.toString(): e.value as int
        },
      );
      // A saved board may already hold a pin the person confirmed once;
      // re-sending it without `confirmed` would have the hub drop it now.
      placed.confirmed = item['confirmed'] == true ||
          placed.pins.entries.any((e) {
            final pin = _pin(e.value);
            final slot = _slotOf(typeId, e.key);
            return pin != null && slot != null && !_fits(pin, slot);
          });
      _placed.add(placed);
    }
  }

  /// GIVE THE MIC BACK. This screen borrows it for its own list; a screen
  /// that leaves without saying so would have the flow still matching
  /// against pins that are no longer on any screen. Given back when the
  /// screen decides to leave (Save, Cancel, the frame gone), not only at
  /// dispose: dispose runs after the exit animation, by which time the
  /// NEXT screen may have armed its own list -- and this took it away.
  bool _voiceReleased = false;

  void _releaseVoice() {
    if (_voiceReleased) return;
    _voiceReleased = true;
    s.disarmScreenChoice();
  }

  @override
  void dispose() {
    _releaseVoice();
    super.dispose();
  }

  /// This screen's route: its `isCurrent` says whether a sheet or dialog
  /// of ours is on top (and so owns the mic), and it is what a leave or
  /// a replacement pops back to. Read in build, which also makes the
  /// screen rebuild -- and re-arm -- when a sheet closes.
  ModalRoute<dynamic>? _route;

  /// Set once the frame is gone and the screen has scheduled its exit.
  bool _leaving = false;

  /// The last gesture came from the voice. Decides only whether the next
  /// sheet or dialog SPEAKS its question and, for the name box, whether
  /// the mic opens behind a text field (never on a tap: that rule came
  /// from a PIN that submitted itself).
  bool _byVoice = false;

  /// Set while a multi-pin device is half-placed: the remaining slots are
  /// asked for by tapping, not by another list, so the person keeps looking
  /// at the board the whole time.
  Map<String, dynamic>? _awaitingType;
  final Map<String, int> _awaitingPins = {};
  int _awaitingSlot = 0;
  bool _awaitingConfirmed = false;

  AppState get s => widget.appState;

  /// THE CATALOGUE IS CAPTURED, NOT READ LIVE, and that is a bug fix.
  ///
  /// These used to read through `s.pinMap` on every build. `sendPinMap`
  /// clears `pinMap` and THEN notifies -- so between the clear and the
  /// route actually popping, this screen rebuilt with an empty catalogue:
  /// no pins, no types, nothing to draw. That is the BLACK SCREEN. Holding
  /// our own copy means the screen keeps rendering what it was given until
  /// it is genuinely gone, which is what a screen being dismissed should
  /// look like.
  Map<String, dynamic> _catCache = const {};
  Map<String, dynamic> get _cat => _catCache;
  List<dynamic> get _pins => (_cat['pins'] as List?) ?? const [];

  /// Pins still free to wire: every pin drawn (the header's and the
  /// off-header row's), less the taken and the module's own.
  int _freePinCount(Map<int, _Placed> taken) => _pins
      .where((p) =>
          p is Map &&
          p['n'] is int &&
          !taken.containsKey(p['n']) &&
          p['severity'] != 'impossible')
      .length;
  List<dynamic> get _types => (_cat['types'] as List?) ?? const [];

  // ── what the board currently looks like ─────────────────────────────
  Map<int, _Placed> get _taken {
    final out = <int, _Placed>{};
    for (final p in _placed) {
      for (final gpio in p.pins.values) {
        out.putIfAbsent(gpio, () => p);
      }
    }
    return out;
  }

  Map<String, dynamic>? _pin(int n) {
    for (final p in _pins) {
      if (p is Map<String, dynamic> && p['n'] == n) return p;
    }
    return null;
  }

  /// A pin as this board prints it -- "D26", "TX2", "GPIO4" -- from the
  /// frame's own label; "GPIO n" when the hub sent none or the pin is not
  /// on this board at all.
  String _pinName(int n) {
    final label = _pin(n)?['label'];
    return label is String && label.isNotEmpty ? label : 'GPIO$n';
  }

  /// A pin number to use in an example sentence ("like pin 26"): one THIS
  /// board has, free and without caveats when there is one.
  int _examplePin() {
    final taken = _taken;
    int? any;
    for (final p in _pins) {
      if (p is! Map || p['n'] is! int) continue;
      final n = p['n'] as int;
      any ??= n;
      if (p['severity'] == 'ok' && !taken.containsKey(n)) return n;
    }
    return any ?? 4;
  }

  /// Which board this is, from the frame's `board_info` (a hub too old to
  /// send it is the classic board it always drew).
  Map<String, dynamic> get _boardInfo =>
      (_cat['board_info'] as Map?)?.cast<String, dynamic>() ?? const {};

  Map<String, dynamic>? _type(String id) {
    for (final t in _types) {
      if (t is Map<String, dynamic> && t['id'] == id) return t;
    }
    return null;
  }

  /// Does this pin satisfy this slot's role? The whole rule, in one line,
  /// because the hub did the thinking.
  bool _fits(Map<String, dynamic> pin, Map<String, dynamic> slot) {
    final can = (pin['can'] as List?)?.cast<String>() ?? const [];
    return can.contains(slot['role'] as String? ?? '');
  }

  /// A shared bus (I²C) already on the board — its pins are reused, not
  /// re-chosen, which is why three sensors cost two pins and not six.
  int? _sharedPinFor(String typeId, String slotKey) {
    final slot = _slotOf(typeId, slotKey);
    if (slot == null || slot['shared'] != true) return null;
    for (final p in _placed) {
      final key = _busKeyIn(p, slot, slotKey);
      final gpio = key == null ? null : p.pins[key];
      if (gpio != null) return gpio;
    }
    return null;
  }

  /// The line of `p` that is the same bus wire as `slot` (key `slotKey`),
  /// or null. The same wire is the same firmware `family` (the hub's
  /// BoardPlan._shared_pin compares component codes); a hub too old to
  /// send families is matched by key, which is 1:1 with them built in.
  String? _busKeyIn(_Placed p, Map<String, dynamic> slot, String slotKey) {
    if (slot['shared'] != true) return null;
    final fam = slot['family'];
    for (final os in (_type(p.typeId)?['slots'] as List? ?? const [])) {
      if (os is! Map || os['shared'] != true) continue;
      if (fam != null ? os['family'] == fam : os['key'] == slotKey) {
        return os['key'].toString();
      }
    }
    return null;
  }

  /// Why one more of `type` would pass a firmware family's limit, or null.
  ///
  /// BoardPlan.add refuses the device past a family's limit (PWM 16,
  /// rotary 2, DHT 4, and 1 for most one-offs -- a second DS18B20 line, a
  /// second buzzer), and at Save that refusal was a device quietly gone
  /// from the board. Counted the hub's way: every non-shared line of
  /// every device, per family. Silent against a hub that sends no limits.
  String? _familyFull(Map<String, dynamic> type) {
    final counts = <Object, int>{};
    for (final p in _placed) {
      for (final sl in (_type(p.typeId)?['slots'] as List? ?? const [])) {
        if (sl is! Map || sl['shared'] == true) continue;
        final fam = sl['family'];
        if (fam != null) counts[fam as Object] = (counts[fam] ?? 0) + 1;
      }
    }
    for (final sl in (type['slots'] as List? ?? const [])) {
      if (sl is! Map || sl['shared'] == true) continue;
      final fam = sl['family'];
      final limit = sl['limit'];
      if (fam == null || limit is! int) continue;
      final n = (counts[fam as Object] ?? 0) + 1;
      counts[fam] = n;
      if (n > limit) {
        final what = (sl['family_name'] ?? 'of that kind').toString();
        final label = (type['label'] as String? ?? 'device').toLowerCase();
        return 'This board already has $limit $what, which is all the '
            'firmware allows -- a $label would be number $n. Remove one '
            'first.';
      }
    }
    return null;
  }

  /// A pin the hub refuses whatever "use it anyway" says: the module's
  /// own (marked impossible), or one this catalogue does not have at all.
  bool _blocked(int gpio) {
    final pin = _pin(gpio);
    return pin == null || pin['severity'] == 'impossible';
  }

  Map<String, dynamic>? _slotOf(String typeId, String key) {
    for (final sl in (_type(typeId)?['slots'] as List? ?? const [])) {
      if (sl is Map<String, dynamic> && sl['key'] == key) return sl;
    }
    return null;
  }

  // ── tapping a pin ───────────────────────────────────────────────────
  /// A pin chosen, by a tap or by saying it. `hint` is the rest of a
  /// spoken "put a relay on pin 26" -- the device sheet starts from it.
  Future<void> _tapPin(int gpio, {String hint = ''}) async {
    final pin = _pin(gpio);
    if (pin == null) return;
    // A pin the module keeps for itself is not a choice: the hub refuses it
    // at save whatever "use it anyway" says.
    if (pin['severity'] == 'impossible' && _taken[gpio] == null) {
      _say((pin['caution'] as String?) ?? 'This pin is not available.');
      return;
    }

    // A single-pin MOVE in flight: this tap is its destination.
    if (_movingPlaced != null) {
      await _finishMove(gpio, pin);
      return;
    }

    // Mid-placement: this tap answers the outstanding slot.
    if (_awaitingType != null) {
      await _fillAwaitingSlot(gpio, pin);
      return;
    }

    final owner = _taken[gpio];
    if (owner != null) {
      await _showPlaced(owner);
      return;
    }
    await _offerDevices(gpio, pin, hint: hint);
  }

  /// The next slot of the device being placed that has no pin yet, as
  /// `_awaitingSlot` (one past the end when none is left).
  ///
  /// NOT `_awaitingSlot + 1`: slots can be filled out of order -- entered
  /// at the HC-SR04's echo on an input-only pin, or a bus line adopted
  /// from the board -- and the old increment walked back onto a filled
  /// one, asking for the echo again and replacing the pin just placed
  /// (or, for an adopted SPI line, moving it off its bus, which the hub
  /// then refused at Save).
  int _nextOpenSlot(List<Map<String, dynamic>> slots) {
    final next = slots
        .indexWhere((sl) => !_awaitingPins.containsKey(sl['key'] as String));
    return next < 0 ? slots.length : next;
  }

  /// Say which line is wanted next, while one is.
  void _sayNextSlot() {
    final type = _awaitingType;
    if (type == null) return;
    final slots = (type['slots'] as List).cast<Map<String, dynamic>>();
    if (_awaitingSlot >= slots.length) return;
    s.speak('Now the pin for the '
        '${(slots[_awaitingSlot]['label'] as String? ?? '').toLowerCase()}.');
  }

  Future<void> _fillAwaitingSlot(int gpio, Map<String, dynamic> pin) async {
    final type = _awaitingType!;
    final slots = (type['slots'] as List).cast<Map<String, dynamic>>();
    // Every line is set and the name box is up: there is nothing for a
    // pin to answer (a spoken pin used to index past the end here).
    if (_awaitingSlot >= slots.length) return;
    final slot = slots[_awaitingSlot];

    // A shared line only REUSES a pin when it is that bus's own pin -- and
    // an existing bus is adopted before any tap is asked for. A FRESH bus
    // on a taken pin is refused by the hub (BoardPlan.add), so it is
    // refused here too rather than dropped from the board at Save.
    final busPin = slot['shared'] == true
        ? _sharedPinFor(type['id'] as String, slot['key'] as String)
        : null;
    if (busPin != null && busPin != gpio) {
      _say('${slot['label'] ?? slot['key']} is already on ${_pinName(busPin)} -- '
          'everything on that bus shares it.');
      return;
    }
    if (_taken.containsKey(gpio) && busPin != gpio) {
      _say('${_pinName(gpio)} is already used by ${_taken[gpio]!.name}.');
      return;
    }
    // ONE DEVICE, ONE PIN PER JOB: the hub refuses a pin named twice in
    // one device -- shared lines included -- and it was only checked
    // against OTHER devices here.
    if (_awaitingPins.containsValue(gpio)) {
      _say('${_pinName(gpio)} is already one of this device\'s own lines.');
      return;
    }
    if (!_fits(pin, slot)) {
      final go = await _confirmWontWork(gpio, pin, type, slot);
      // The placement may have been stopped (or the frame replaced) while
      // the won't-work dialog was up.
      if (!go || !mounted || !identical(_awaitingType, type)) return;
      _awaitingConfirmed = true;
    }
    setState(() {
      _awaitingPins[slot['key'] as String] = gpio;
      _awaitingSlot = _nextOpenSlot(slots);
    });
    if (_awaitingSlot >= slots.length) {
      await _finishPlacement();
    } else {
      _sayNextSlot();
    }
  }

  /// DEVICE FIRST — the CLI's 2026-08-24 reversal, on glass (2026-08-25c).
  /// The person is holding a READER, not a wire: the device is the thing
  /// they know, so the full catalogue is the entry point and the pins are
  /// walked afterwards, one slot at a time, by tapping the board. The
  /// existing placement flow already does the walking; this only starts
  /// it without a pin in hand. Shared buses already on the board are
  /// joined silently, exactly as the pin-tap path joins them.
  Future<void> _addDevice() async {
    final chosen = await _modal(() => showModalBottomSheet<Map<String, dynamic>>(
          context: context,
          backgroundColor: s.surface,
          isScrollControlled: true,
          shape: const RoundedRectangleBorder(
              borderRadius: BorderRadius.vertical(top: Radius.circular(16))),
          builder: (context) => _DeviceSheet(
            appState: s,
            pin: null,
            types: _types,
            groups: (_cat['groups'] as List?)?.cast<String>() ?? const [],
            fits: _fits,
            spokenPrompt: _byVoice ? 'What are you adding?' : '',
          ),
        ));
    if (chosen == null || !mounted) return;

    final type = _type(chosen['type_id'] as String);
    if (type == null) return;
    final full = _familyFull(type);
    if (full != null) {
      _say(full);
      return;
    }
    final slots = (type['slots'] as List).cast<Map<String, dynamic>>();
    setState(() {
      _awaitingType = type;
      _awaitingPins.clear();
      _awaitingSlot = 0;
      _awaitingConfirmed = false;
      _adoptBuses(type, slots);
      _adoptDefaults(type, slots);
      _awaitingSlot = _nextOpenSlot(slots);
    });
    if (_awaitingSlot >= slots.length) {
      await _finishPlacement();
    } else {
      _sayNextSlot();
    }
  }

  /// Fill every shared line of the device being placed whose bus is
  /// already on the board. The bus pin carries its riders' "use it
  /// anyway" too: a bus someone confirmed onto a pin that cannot do the
  /// job needs the same override on every device that joins it, or the
  /// hub refuses the newcomer (WONT_WORK without `confirmed`) at Save.
  void _adoptBuses(Map<String, dynamic> type, List<Map<String, dynamic>> slots) {
    for (final sl in slots) {
      final key = sl['key'] as String;
      if (_awaitingPins.containsKey(key)) continue;
      final shared = _sharedPinFor(type['id'] as String, key);
      if (shared == null) continue;
      _awaitingPins[key] = shared;
      final bp = _pin(shared);
      if (bp != null && !_fits(bp, sl)) _awaitingConfirmed = true;
    }
  }

  /// Fill every slot whose driver names its own pin (`default_pin`: a
  /// custom driver's code says `digitalRead(26)`), when that pin is free
  /// and can do the job. Only a starting place: the person can still tap
  /// the device and move it, and the driver follows the pin chosen here.
  void _adoptDefaults(
      Map<String, dynamic> type, List<Map<String, dynamic>> slots) {
    for (final sl in slots) {
      final key = sl['key'] as String;
      final gpio = sl['default_pin'];
      if (gpio is! int || _awaitingPins.containsKey(key)) continue;
      if (_taken.containsKey(gpio) || _awaitingPins.containsValue(gpio)) {
        continue;
      }
      final bp = _pin(gpio);
      if (bp == null || !_fits(bp, sl)) continue;
      _awaitingPins[key] = gpio;
    }
  }

  Future<void> _offerDevices(int gpio, Map<String, dynamic> pin,
      {String hint = ''}) async {
    final chosen = await _modal(() => showModalBottomSheet<Map<String, dynamic>>(
          context: context,
          backgroundColor: s.surface,
          isScrollControlled: true,
          shape: const RoundedRectangleBorder(
              borderRadius: BorderRadius.vertical(top: Radius.circular(16))),
          builder: (context) => _DeviceSheet(
            appState: s,
            pin: pin,
            types: _types,
            groups: (_cat['groups'] as List?)?.cast<String>() ?? const [],
            fits: _fits,
            hint: hint,
            spokenPrompt:
                _byVoice && hint.isEmpty ? 'What is on ${_pinName(gpio)}?' : '',
          ),
        ));
    if (chosen == null || !mounted) return;

    final type = _type(chosen['type_id'] as String);
    final pin0 = _pin(gpio);
    if (type == null || pin0 == null) return;
    final full = _familyFull(type);
    if (full != null) {
      _say(full);
      return;
    }
    final slotKey = chosen['slot_key'] as String;
    final slots = (type['slots'] as List).cast<Map<String, dynamic>>();
    final slot = slots.firstWhere((sl) => sl['key'] == slotKey);

    // Entering at a SHARED line whose bus already runs on another pin:
    // everything on a bus shares its pins, and the hub refuses a second
    // one ("already on GPIOx"), dropping the device at Save.
    final bus = slot['shared'] == true
        ? _sharedPinFor(type['id'] as String, slotKey)
        : null;
    if (bus != null && bus != gpio) {
      _say('${(slot['label'] as String? ?? slotKey)} is already on ${_pinName(bus)} '
          '-- everything on that bus shares it. Use Add a device instead.');
      return;
    }
    // The pin may have been taken while the sheet was up (a frame
    // replaced the board); the same occupancy rule as every other path.
    if (_taken.containsKey(gpio)) {
      _say('${_pinName(gpio)} is already used by ${_taken[gpio]!.name}.');
      return;
    }

    var confirmed = false;
    if (!_fits(pin0, slot)) {
      final go = await _confirmWontWork(gpio, pin0, type, slot);
      if (!go || !mounted) return;
      confirmed = true;
    }

    // Anything else this device needs, filled from the board where the bus
    // already exists, or asked for by tapping.
    setState(() {
      _awaitingType = type;
      _awaitingPins
        ..clear()
        ..[slotKey] = gpio;
      _awaitingSlot = 0;
      _awaitingConfirmed = confirmed;
      _adoptBuses(type, slots);
      _adoptDefaults(type, slots);
      _awaitingSlot = _nextOpenSlot(slots);
    });
    if (_awaitingSlot >= slots.length) {
      await _finishPlacement();
    } else {
      _sayNextSlot();
    }
  }

  Future<void> _finishPlacement() async {
    final type = _awaitingType!;
    final name = await _askName(type['label'] as String);
    if (!mounted) return;
    // Stopped, or the frame replaced, while the name box was up.
    if (!identical(_awaitingType, type)) return;
    if (name == null) {
      setState(() {
        _awaitingType = null;
        _awaitingPins.clear();
        _awaitingSlot = 0;
        _awaitingConfirmed = false;
      });
      return;
    }
    final pins = Map<String, int>.from(_awaitingPins);
    setState(() {
      _placed.add(_Placed(type['id'] as String, name, pins,
          confirmed: _awaitingConfirmed));
      _awaitingType = null;
      _awaitingPins.clear();
      _awaitingSlot = 0;
      _awaitingConfirmed = false;
    });

    // A PLACEMENT THAT WORKED USED TO SAY NOTHING. Only the refusals
    // talked, so the one outcome worth confirming -- with your hands on
    // the board and your eyes off the phone -- was the silent one. Pin
    // and device in one breath, then the word that ends the job, while
    // the mic is still open to hear it.
    final where = pins.values.map(_pinName).join(' and ');
    s.speak(_canSave()
        ? '$where, $name. Say save when you are done.'
        : '$where, $name.');
  }

  Future<String?> _askName(String label) async {
    final controller = TextEditingController();
    // The hub refuses a name already on the board (case-insensitive) and
    // drops the device; said here instead, while the box is still open.
    String? taken;
    BuildContext? box;
    void close([String? name]) {
      final d = box;
      if (d != null && d.mounted) Navigator.of(d).pop(name);
    }

    // SAID, WHEN THE PLACING WAS SAID. Placing by voice used to end at
    // this box: pins and part by voice, then a keyboard. The words are
    // dictated (not matched -- a name is not on any list), cleaned of the
    // "call it" and the full stop, and checked by the same rules as the
    // typed name; a refusal is said and the box listens again.
    void listen(String prompt) {
      s.armScreenDictation(
        prompt: prompt,
        onCancel: close,
        onText: (text) {
          if (!mounted) return;
          final name = _spokenName(text);
          final why =
              name.isEmpty ? 'I did not catch a name.' : _nameProblem(name);
          if (why != null) {
            listen('$why Say another name.');
            return;
          }
          controller.text = name;
          close(name);
        },
      );
    }

    final v = await _modal(
      () => showDialog<String>(
        context: context,
        builder: (context) {
          box = context;
          return StatefulBuilder(
            builder: (context, setLocal) => AlertDialog(
              backgroundColor: s.surface,
              title: Text('Name this ${label.toLowerCase()}',
                  style: TextStyle(color: s.ink, fontSize: 16)),
              content: TextField(
                controller: controller,
                autofocus: true,
                autocorrect: false,
                enableIMEPersonalizedLearning: false,
                style: TextStyle(color: s.ink),
                decoration: InputDecoration(
                  hintText: 'e.g. kitchen lamp',
                  hintStyle: TextStyle(color: s.inkFaint),
                  errorText: taken,
                ),
                onChanged: (v) {
                  final dup = _nameProblem(v);
                  if (dup != taken) setLocal(() => taken = dup);
                },
                onSubmitted: (v) {
                  if (_nameProblem(v) == null) {
                    Navigator.of(context).pop(v.trim());
                  }
                },
              ),
              actions: [
                TextButton(
                    onPressed: () => Navigator.of(context).pop(),
                    // Every cancel wears the disconnect red (her rule).
                    child:
                        Text('Cancel', style: TextStyle(color: s.cancelInk))),
                TextButton(
                    onPressed: () {
                      if (_nameProblem(controller.text) == null) {
                        Navigator.of(context).pop(controller.text.trim());
                      }
                    },
                    child: const Text('Add')),
              ],
            ),
          );
        },
      ),
      voice: _byVoice ? () => listen('What is it called?') : null,
    );
    return (v == null || v.trim().isEmpty) ? null : v.trim();
  }

  /// A dictated name as it should be stored: "Call it kitchen lamp." is
  /// "kitchen lamp".
  static String _spokenName(String text) => text
      .trim()
      .replaceFirst(
          RegExp(r"^(call it|name it|it is|it's|its)\s+",
              caseSensitive: false),
          '')
      .replaceAll(RegExp(r'^[\s.,!?]+|[\s.,!?]+$'), '')
      .trim();

  /// The complaint for a name the hub would refuse, or null: one this
  /// board already has (the hub compares case-insensitively), or one
  /// carrying ';' -- the name rides in a FriendlyName command inside a
  /// Backlog line, where ';' starts the next command. The hub's naming
  /// question refuses it; the pin screen now does too.
  String? _nameProblem(String name) {
    final want = name.trim().toLowerCase();
    if (want.isEmpty) return null;
    if (want.contains(';')) {
      return 'A name cannot contain a semicolon -- a comma works.';
    }
    for (final p in _placed) {
      if (p.name.trim().toLowerCase() == want) {
        return 'This board already has something called that.';
      }
    }
    return null;
  }

  /// The pin cannot do this, and saying so is the entire point of the
  /// screen — but it is a warning, not a veto. The person can see their own
  /// board; this app cannot.
  Future<bool> _confirmWontWork(int gpio, Map<String, dynamic> pin,
      Map<String, dynamic> type, Map<String, dynamic> slot) async {
    final role = slot['role'] as String? ?? '';
    final name = _pinName(gpio);
    // The pins of THIS board that can do it, from the frame's `can`.
    final able = [
      for (final p in _pins)
        if (p is Map<String, dynamic> && p['n'] is int && _fits(p, slot))
          _pinName(p['n'] as int)
    ];
    final these = able.join(', ');
    final why = switch (role) {
      'out' || 'bidir' =>
        '$name has no output driver: it can be read, but it physically '
            'cannot switch anything, so this device would never respond.',
      'adc' => able.isEmpty
          ? 'This board has no analogue input that works with Wi-Fi on, '
              'so readings would be nonsense.'
          : '$name cannot measure with Wi-Fi on: only ADC1 pins can, and the '
              'radio takes ADC2 over whenever Wi-Fi is on — and it always '
              'is. Readings would be nonsense. On this board: $these.',
      'touch' => able.isEmpty
          ? 'This board has no touch channels.'
          : '$name has no touch channel wired to it. Touch works on $these.',
      _ => '$name cannot do this.',
    };
    return _confirm(
      title: 'This will not work',
      body: '$why\n\nThe board will accept it without complaining, which is '
          'why this is worth saying now.',
      spoken: '$why Use it anyway?',
      no: 'Pick another pin',
      yes: 'Use it anyway',
      yesQuiet: true,
    );
  }

  /// A yes/no question as a dialog, answerable by voice too: "yes" /
  /// "no" or the buttons' own words. Spoken aloud when the last gesture
  /// was a spoken one.
  Future<bool> _confirm({
    required String title,
    required String body,
    required String spoken,
    required String no,
    required String yes,
    bool yesQuiet = false,
  }) async {
    BuildContext? dialog;
    void answer(bool v) {
      final d = dialog;
      if (d != null && d.mounted) Navigator.of(d).pop(v);
    }

    final go = await _modal(
      () => showDialog<bool>(
        context: context,
        builder: (context) {
          dialog = context;
          return AlertDialog(
            backgroundColor: s.surface,
            title: Text(title, style: TextStyle(color: s.ink)),
            content: Text(body,
                style: TextStyle(color: s.inkDim, fontSize: 13.5)),
            actions: [
              TextButton(
                  onPressed: () => Navigator.of(context).pop(false),
                  child: Text(no)),
              TextButton(
                  onPressed: () => Navigator.of(context).pop(true),
                  child: Text(yes,
                      style: yesQuiet
                          ? TextStyle(color: s.inkFaint)
                          : null)),
            ],
          );
        },
      ),
      voice: () => s.armScreenChoice(
        prompt: spoken,
        speakPrompt: _byVoice,
        repeatLine: spoken,
        options: [
          const {'value': 'yes', 'label': 'yes'},
          const {'value': 'no', 'label': 'no'},
          {'value': 'yes', 'label': yes.toLowerCase()},
          {'value': 'no', 'label': no.toLowerCase()},
        ],
        onCancel: () => answer(false),
        onPick: (value, _) => answer(value == 'yes'),
      ),
    );
    return go == true;
  }

  /// Every sheet and dialog this screen opens goes through here, so the
  /// mic always belongs to what is on top.
  ///
  /// The board's own vocabulary comes off before the modal opens -- it
  /// used to stay armed under every dialog, so "pin 4" said at the name
  /// box answered a slot that was already filled -- and `voice` arms the
  /// modal's own list, if it has one. When the modal closes its list is
  /// taken off and the board re-arms on its next frame, as the top route
  /// again. (The device sheet arms itself; it no longer disarms in its
  /// dispose, which ran AFTER the board had re-armed and took the board's
  /// vocabulary away with it.)
  Future<T?> _modal<T>(Future<T?> Function() open, {VoidCallback? voice}) async {
    s.disarmScreenChoice();
    final shown = open();
    voice?.call();
    try {
      return await shown;
    } finally {
      if (mounted) {
        s.disarmScreenChoice();
        setState(() {}); // rebuild, which re-arms once this is on top
      }
    }
  }

  /// Which of a device's lines are still unassigned -- the CATALOGUE's
  /// slots, not the placement's pins, are the list that decides.
  ///
  /// A placement can be short of a slot in two honest ways: the person
  /// stopped mid-way, or the catalogue grew. The second happened for real:
  /// the card reader gained a reset line after boards had already saved
  /// it with four pins, and the hub then refused the four-pin record with
  /// "still needs its reset" -- correctly, but from a screen that offered
  /// no way to add one. Missing slots are now shown on the device's own
  /// sheet and assignable there, and Save waits until nothing is missing.
  List<Map<String, dynamic>> _missingSlots(_Placed placed) {
    return [
      for (final sl in (_type(placed.typeId)?['slots'] as List? ?? const []))
        if (sl is Map<String, dynamic> &&
            !placed.pins.containsKey(sl['key'] as String))
          sl,
    ];
  }

  /// Tap a placed device: every line the catalogue says it has, each one
  /// movable ALONE, and any it does not yet have assignable in place.
  ///
  /// This used to be a one-line summary with Keep/Remove -- so fixing a
  /// single mis-tapped pin meant removing the whole device and placing it
  /// again, re-answering every slot. Moving ONE pin is the single most
  /// common correction there is, and it now costs one tap on the line and
  /// one tap on the new pin. Adding a missing line is the same gesture. A
  /// missing SHARED line whose bus already exists on the board is adopted
  /// with the one tap and no pin hunt, because there is nothing to choose.
  /// Remove stays for the real thing.
  Future<void> _showPlaced(_Placed placed) async {
    final type = _type(placed.typeId);
    final slots = (type?['slots'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .toList();
    String slotLabel(String key) {
      for (final sl in slots) {
        if (sl['key'] == key) return (sl['label'] as String?) ?? key;
      }
      return key;
    }

    // Its lines and Remove are sayable while it is up: "data pin" (or
    // "move it" for a one-line device) does what tapping the line does.
    BuildContext? open;
    void choose(Map<String, dynamic>? action) {
      final d = open;
      if (d != null && d.mounted) Navigator.of(d).pop(action);
    }

    Map<String, dynamic> lineAction(Map<String, dynamic> sl) {
      final key = sl['key'] as String;
      final bus = (placed.pins[key] == null && sl['shared'] == true)
          ? _sharedPinFor(placed.typeId, key)
          : null;
      return bus != null ? {'adopt': key, 'gpio': bus} : {'move': key};
    }

    final action = await _modal(
      () => showModalBottomSheet<Map<String, dynamic>>(
      context: context,
      backgroundColor: s.surface,
      shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.vertical(top: Radius.circular(16))),
      builder: (sheet) {
        open = sheet;
        return SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 16, 20, 4),
              child: Text(placed.name,
                  style: TextStyle(
                      color: s.ink,
                      fontSize: 16,
                      fontWeight: FontWeight.w700)),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 0, 20, 10),
              child: Text('${type?['label'] ?? placed.typeId} — tap a line '
                  'to move just that pin, or to set one that is missing',
                  style: TextStyle(color: s.inkFaint, fontSize: 12)),
            ),
            for (final sl in slots)
              _slotLine(sheet, placed, sl),
            const Divider(height: 18),
            ListTile(
              dense: true,
              leading:
                  const Icon(Icons.delete_outline, size: 18, color: Color(0xFFF85149)),
              title: const Text('Remove this device',
                  style: TextStyle(color: Color(0xFFF85149), fontSize: 14)),
              onTap: () => Navigator.of(sheet).pop({'remove': true}),
            ),
            const SizedBox(height: 8),
          ],
        ),
        );
      },
      ),
      voice: () {
        final question = '${placed.name}. Say the line to move, or remove.';
        s.armScreenChoice(
          prompt: question,
          speakPrompt: _byVoice,
          repeatLine: question,
          options: [
            for (final sl in slots)
              {
                'value': 'line:${sl['key']}',
                'label': ((sl['label'] as String?) ?? sl['key']).toString()
              },
            if (slots.length == 1)
              {'value': 'line:${slots.first['key']}', 'label': 'move it'},
            const {'value': 'remove', 'label': 'remove it'},
          ],
          onCancel: () => choose(null),
          onPick: (value, _) {
            if (value == 'remove') {
              choose({'remove': true});
              return;
            }
            final key = value.startsWith('line:') ? value.substring(5) : '';
            for (final sl in slots) {
              if (sl['key'] == key) choose(lineAction(sl));
            }
          },
        );
      },
    );
    if (action == null || !mounted || !_placed.contains(placed)) return;

    if (action['remove'] == true) {
      setState(() => _remove(placed));
      return;
    }

    // A missing shared line whose bus is already on the board: adopted
    // outright. There is exactly one right answer and asking for it would
    // only invite a wrong one. The bus pin brings its "use it anyway"
    // with it, as it does for a device placed onto the bus.
    final adoptKey = action['adopt'] as String?;
    if (adoptKey != null) {
      final gpio = action['gpio'] as int;
      final bp = _pin(gpio);
      final sl = _slotOf(placed.typeId, adoptKey);
      setState(() {
        placed.pins[adoptKey] = gpio;
        if (bp != null && sl != null && !_fits(bp, sl)) placed.confirmed = true;
      });
      return;
    }

    final moveKey = action['move'] as String?;
    if (moveKey != null) {
      setState(() {
        _movingPlaced = placed;
        _movingSlotKey = moveKey;
      });
      _say('Tap or say the pin for ${slotLabel(moveKey).toLowerCase()}.');
    }
  }

  /// One line of a placed device's sheet: assigned (tap to move) or not
  /// yet (tap to set). The gesture and the destination are the same
  /// either way -- the next pin tap -- so the person learns one thing.
  Widget _slotLine(BuildContext sheet, _Placed placed,
      Map<String, dynamic> slot) {
    final key = slot['key'] as String;
    final label = (slot['label'] as String?) ?? key;
    final shared = slot['shared'] == true;
    final gpio = placed.pins[key];
    // A missing shared line, when its bus already exists on the board:
    // the answer is known, so offer it rather than a pin hunt.
    final busPin = (gpio == null && shared)
        ? _sharedPinFor(placed.typeId, key)
        : null;

    // On a pin the hub refuses outright (the module's own, or one this
    // catalogue no longer has): it must move before Save, so it says so.
    final blocked = gpio != null && _blocked(gpio);

    final String subtitle;
    if (gpio == null) {
      subtitle = busPin != null
          ? 'not set — tap to use the bus already on ${_pinName(busPin)}'
          : 'not set — tap, then tap the pin it is wired to';
    } else if (blocked) {
      subtitle =
          '${_pinName(gpio)} is not available on this board — tap to move it';
    } else if (shared) {
      // A SHARED line moves for every device on the bus at once, and the
      // subtitle says so before the tap, not after.
      subtitle = 'shared bus — moving it moves it for every device using it';
    } else {
      subtitle = '';
    }

    return ListTile(
      dense: true,
      leading: Icon(
          gpio == null
              ? Icons.add_circle_outline
              : shared
                  ? Icons.share
                  : Icons.swap_horiz,
          size: 18,
          color: gpio == null || blocked ? const Color(0xFFD29922) : s.inkDim),
      title: Text(label, style: TextStyle(color: s.ink, fontSize: 14)),
      subtitle: subtitle.isEmpty
          ? null
          : Text(subtitle,
              style: TextStyle(
                  color: gpio == null || blocked
                      ? const Color(0xFFD29922)
                      : s.inkFaint,
                  fontSize: 11)),
      trailing: Text(gpio == null ? 'not set' : _pinName(gpio),
          style: TextStyle(
              color: gpio == null || blocked
                  ? const Color(0xFFD29922)
                  : s.accent,
              fontSize: 13.5,
              fontWeight: FontWeight.w700)),
      onTap: () => Navigator.of(sheet).pop(busPin != null
          ? {'adopt': key, 'gpio': busPin}
          : {'move': key}),
    );
  }

  /// Every device still short of a line, for the banner and the Save
  /// guard. Saving an incomplete device would have the hub drop it from
  /// the board with a one-line warning on the hub side -- from the phone
  /// that looks like the reader simply vanished. Better said here first.
  List<(_Placed, List<String>)> _incomplete() {
    return [
      for (final p in _placed)
        if (_problems(p).isNotEmpty) (p, _problems(p)),
    ];
  }

  /// What the hub would refuse this device for, in the words the banner
  /// and the summary use: a line with no pin, a line on a pin the hub
  /// refuses outright (a saved board's GPIO17 once the module is known to
  /// keep it), or a type the catalogue no longer has. Each of these was
  /// drawn as a finished green device and then dropped at Save.
  List<String> _problems(_Placed placed) {
    final type = _type(placed.typeId);
    if (type == null) {
      return ['removing (this hub no longer knows what it is)'];
    }
    return [
      for (final sl in _missingSlots(placed))
        ((sl['label'] as String?) ?? sl['key'].toString()).toLowerCase(),
      for (final e in placed.pins.entries)
        if (_blocked(e.value))
          'a new pin for the '
              '${((_slotOf(placed.typeId, e.key)?['label'] as String?) ?? e.key).toLowerCase()}'
              ' (${_pinName(e.value)} is not available)',
    ];
  }

  /// Take a device off the board, and any move in flight that was about
  /// it -- otherwise the banner kept asking for a pin for a device that
  /// no longer exists, and Save stayed held until it was answered.
  void _remove(_Placed placed) {
    _placed.remove(placed);
    if (_movingPlaced == placed) {
      _movingPlaced = null;
      _movingSlotKey = null;
    }
  }

  /// The single-pin move in flight: which device, which of its lines.
  /// Set by _showPlaced, consumed by the next pin tap in _tapPin.
  _Placed? _movingPlaced;
  String? _movingSlotKey;

  /// Complete a single-pin move: the tapped pin becomes the new home of
  /// the one slot being moved. Same rules as first placement -- occupancy
  /// checked, role checked with the same confirm-anyway door -- because a
  /// move is a placement that happens to have a past.
  Future<bool> _finishMove(int gpio, Map<String, dynamic> pin) async {
    final placed = _movingPlaced!;
    final key = _movingSlotKey!;
    final type = _type(placed.typeId);
    final slots = (type?['slots'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .toList();
    final slot = slots.firstWhere((sl) => sl['key'] == key,
        orElse: () => const {'key': '?', 'label': '?', 'role': 'in'});

    // Who else holds this pin. A shared line may land where the same
    // line of the same bus already is (a no-op); anything else on the pin
    // -- another device, or another line of this one -- is a clash the hub
    // refuses (BoardPlan.add), so it is refused here instead of the device
    // vanishing at Save. The shared exemption used to skip this entirely.
    final shared = slot['shared'] == true;
    _Placed? clash;
    for (final p in _placed) {
      final busKey = shared ? _busKeyIn(p, slot, key) : null;
      for (final e in p.pins.entries) {
        if (e.value != gpio) continue;
        if (e.key == busKey || (p == placed && e.key == key)) continue;
        clash ??= p;
      }
    }
    if (clash != null) {
      _say('${_pinName(gpio)} is already used by ${clash.name}.');
      return false;                      // stay in moving mode; pick another
    }
    if (!_fits(pin, slot)) {
      final go = await _confirmWontWork(gpio, pin, type ?? const {}, slot);
      if (!go || !mounted || !identical(_movingPlaced, placed)) return false;
      placed.confirmed = true;
      // A bus line moves for every rider, and each one needs the override.
      if (shared) {
        for (final p in _placed) {
          final busKey = _busKeyIn(p, slot, key);
          if (busKey != null && p.pins.containsKey(busKey)) p.confirmed = true;
        }
      }
    }
    if (!mounted) return false;
    setState(() {
      // A shared line is ONE wire: move it on this device and every other
      // device on the bus follows, or the screen would describe a board
      // where two devices disagree about where the clock is.
      if (shared) {
        for (final p in _placed) {
          final busKey = _busKeyIn(p, slot, key);
          if (busKey != null && p.pins.containsKey(busKey)) {
            p.pins[busKey] = gpio;
          }
        }
      }
      // The device in hand always gets it -- including the case where it
      // did not have the line before (a bus being SET, not moved, on the
      // first device to need it), which the loop above cannot reach.
      placed.pins[key] = gpio;
      _movingPlaced = null;
      _movingSlotKey = null;
    });
    return true;
  }

  /// SEEN AND HEARD. This was a SnackBar alone despite its name -- the
  /// screen already had the right sentences ("GPIO4 is already used by
  /// light bulb") and no voice to say them, which is exactly backwards on
  /// the one screen where your eyes are on the board and not on the phone.
  void _say(String text) {
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(text), duration: const Duration(seconds: 2)));
    s.speak(text);
  }

  // ── what this screen is listening for ───────────────────────────────
  //
  // Everything you can DO here, not just the pins: Add, Save, the devices
  // already placed, and -- read on the phone, never matched -- the pins.
  // A vocabulary that stopped at the list would leave you saying "save"
  // to a screen that ignores it, which is worse than not being heard.
  //
  // THE PINS ARE NOT ON THE LIST THE HUB MATCHES. They used to be ("pin
  // 14" per free pin), and the hub's matcher is fuzzy by design: with 26
  // taken, "pin 26" scored 0.91 against "pin 2" and placed the device on
  // GPIO2. A wrong pin is a wiring mistake, so pin numbers are read
  // exactly by _Spoken (see there) through the flow's `resolve` hook, and
  // anything it cannot read exactly is refused, not guessed.
  //
  // Re-armed after every change, because it changes: devices come and go,
  // and Add is offered only while nothing is half placed.
  List<Map<String, dynamic>> _vocabulary() {
    final idle = _awaitingType == null && _movingPlaced == null;
    return [
      // Never matched to a pin: it is what a garbled "pin twenny" lands
      // on, and it is what makes "say it again" mention pins at all.
      {'value': 'pins', 'label': 'a pin number'},
      if (idle) {'value': 'add', 'label': 'add a device'},
      // Always sayable: when Save is held, saying it tells you why.
      {'value': 'save', 'label': 'save'},
      if (idle)
        for (final p in _placed) {'value': 'dev:${p.name}', 'label': p.name},
    ];
  }

  /// Why Save is held, in a sentence, or null when it is not. The word
  /// and the button are gated on this ONE expression, so they can never
  /// disagree about whether this board is ready.
  String? _whyNotSave() {
    if (_awaitingType != null || _movingPlaced != null) {
      return 'Finish the pin you are placing first, or say cancel to stop.';
    }
    if (_placed.isEmpty) return 'Nothing is on the board yet.';
    final incomplete = _incomplete();
    if (incomplete.isNotEmpty) {
      final (p, missing) = incomplete.first;
      return '${p.name} still needs ${missing.join(', ')}.';
    }
    return null;
  }

  bool _canSave() => _whyNotSave() == null;

  void _rearmVoice() {
    if (!mounted || _voiceReleased || _leaving) return;
    // A sheet or dialog of ours is on top: it owns the mic (or has none).
    // Re-arming here replaced the device sheet's list with the pins on
    // every AppState change while the sheet was open.
    final route = _route;
    if (route != null && !route.isCurrent) return;
    s.armScreenChoice(
      options: _vocabulary(),
      onCancel: _voiceCancel,
      resolve: _readSpoken,
      onPick: (value, heard) {
        if (!mounted) return;
        _byVoice = true;
        final idle = _awaitingType == null && _movingPlaced == null;
        if (value == 'add') {
          if (idle) _addDevice();
          return;
        }
        if (value == 'save') {
          final why = _whyNotSave();
          if (why == null) {
            _send();
          } else {
            _say(why);
          }
          return;
        }
        if (value == 'pins') {
          _say('Say the pin number, like pin ${_examplePin()}.');
          return;
        }
        if (value.startsWith('dev:') && idle) {
          final name = value.substring(4);
          for (final p in _placed) {
            if (p.name == name) {
              _showPlaced(p);
              return;
            }
          }
        }
      },
    );
  }

  /// A spoken "cancel". Mid-placement or mid-move it stops THAT, like the
  /// banner's X -- throwing away the whole board for a word said to end
  /// one device would lose minutes of work. Otherwise it is the screen's
  /// Cancel.
  void _voiceCancel() {
    if (!mounted) return;
    if (_awaitingType != null || _movingPlaced != null) {
      final moving = _movingPlaced != null;
      setState(_stopPlacing);
      _say(moving ? 'Left where it was.' : 'Stopped. Nothing was added.');
      return;
    }
    _cancel();
  }

  void _stopPlacing() {
    _movingPlaced = null;
    _movingSlotKey = null;
    _awaitingType = null;
    _awaitingPins.clear();
    _awaitingSlot = 0;
    _awaitingConfirmed = false;
  }

  /// The pins as THIS board prints them -- "rx2", "vp", "d26" on the
  /// classic DevKit, "gpio4" on a generic board -> GPIO -- for _Spoken.
  Map<String, int> _boardNames() => {
        for (final p in _pins)
          if (p is Map &&
              p['n'] is int &&
              (p['label'] as String?)?.isNotEmpty == true)
            (p['label'] as String)
                .toLowerCase()
                .replaceAll(RegExp(r'[^a-z0-9]'), ''): p['n'] as int,
      };

  /// What the screen does with words it can read itself, or null to let
  /// the hub match them against the vocabulary. Anything carrying a pin
  /// number is decided HERE and never reaches the hub.
  VoidCallback? _readSpoken(String heard) {
    if (!mounted) return null;
    final said = _Spoken.read(heard, _boardNames());
    final idle = _awaitingType == null && _movingPlaced == null;
    final pins = said.pins;

    if (idle && said.verb == 'move') {
      if (pins.length == 2) return () => _voiceMovePin(pins[0], pins[1]);
      if (pins.length == 1 && said.rest.isNotEmpty) {
        return () => _voiceMoveNamed(said.rest, pins[0]);
      }
      return () => _voiceSay(
          'Say what and where, like: move the relay to pin ${_examplePin()}.');
    }
    if (idle && said.verb == 'remove') {
      if (pins.length == 1) {
        return () {
          final owner = _taken[pins[0]];
          if (owner == null) {
            _voiceSay('Nothing is on ${_pinName(pins[0])}.');
          } else {
            _voiceRemove(owner);
          }
        };
      }
      if (pins.isEmpty && said.rest.isNotEmpty) {
        return () => _voiceRemoveNamed(said.rest);
      }
      if (pins.isEmpty) {
        return () => _voiceSay('Say which one, like: remove the fan.');
      }
    }
    if (pins.length > 1) {
      return () => _voiceSay(
          'I heard more than one pin number. Say one pin at a time.');
    }
    if (pins.isEmpty) {
      return said.pinWordOnly
          ? () => _voiceSay(
              'Which pin? Say its number, like pin ${_examplePin()}.')
          : null;
    }
    final n = pins.first;
    return () {
      if (!mounted) return;
      _byVoice = true;
      if (_pin(n) == null) {
        _say('There is no GPIO$n to wire to on this board.');
        return;
      }
      // "put a relay on pin 26" with 26 already wired: the answer is that
      // it is taken, not the other device's sheet.
      final owner = _taken[n];
      if (idle && said.rest.isNotEmpty && owner != null) {
        _say('${_pinName(n)} is already used by ${owner.name}.');
        return;
      }
      _tapPin(n, hint: idle ? said.rest : '');
    };
  }

  void _voiceSay(String text) {
    if (!mounted) return;
    _byVoice = true;
    _say(text);
  }

  /// "move pin 26 to 27": the line on 26, whatever it belongs to, to 27.
  Future<void> _voiceMovePin(int from, int to) async {
    if (!mounted) return;
    _byVoice = true;
    final owner = _taken[from];
    if (owner == null) {
      _say('Nothing is on ${_pinName(from)}.');
      return;
    }
    final key = owner.pins.entries.firstWhere((e) => e.value == from).key;
    await _voiceMove(owner, key, to);
  }

  /// "move the relay to 27": a one-line device's line. The name is the
  /// hub's to match, so the move is confirmed before it happens.
  Future<void> _voiceMoveNamed(String words, int to) async {
    if (!mounted) return;
    _byVoice = true;
    final placed = await _placedNamed(words);
    if (!mounted) return;
    if (placed == null) {
      _say('I did not catch which device.');
      return;
    }
    if (placed.pins.length != 1) {
      _say('${placed.name} has more than one wire. Say which one.');
      await _showPlaced(placed);
      return;
    }
    if (!await _confirm(
      title: 'Move ${placed.name}?',
      body: 'From ${_pinName(placed.pins.values.first)} to ${_pinName(to)}.',
      spoken: 'Move ${placed.name} to ${_pinName(to)}?',
      no: 'Leave it',
      yes: 'Move it',
    )) {
      return;
    }
    if (!mounted || !_placed.contains(placed)) return;
    await _voiceMove(placed, placed.pins.keys.first, to);
  }

  /// A move is the tap path's move: the line is picked up, and the pin
  /// is "tapped" -- every rule (taken, won't work, the module's own)
  /// applies exactly as it does to a finger.
  Future<void> _voiceMove(_Placed placed, String key, int to) async {
    if (_pin(to) == null) {
      _say('There is no GPIO$to to wire to on this board.');
      return;
    }
    setState(() {
      _movingPlaced = placed;
      _movingSlotKey = key;
    });
    await _tapPin(to);
    if (mounted && _movingPlaced == null && placed.pins[key] == to) {
      _say('Moved ${placed.name} to ${_pinName(to)}.');
    }
  }

  Future<void> _voiceRemoveNamed(String words) async {
    if (!mounted) return;
    _byVoice = true;
    final placed = await _placedNamed(words);
    if (!mounted) return;
    if (placed == null) {
      _say('I did not catch which device.');
      return;
    }
    await _voiceRemove(placed);
  }

  Future<void> _voiceRemove(_Placed placed) async {
    _byVoice = true;
    final go = await _confirm(
      title: 'Remove ${placed.name}?',
      body: 'It comes off this board. Nothing is sent until you save.',
      spoken: 'Remove ${placed.name}?',
      no: 'Keep it',
      yes: 'Remove',
    );
    if (!go || !mounted || !_placed.contains(placed)) return;
    setState(() => _remove(placed));
    _say('Removed ${placed.name}.');
  }

  /// The placed device a spoken name means, by the hub's matcher (names
  /// are words; only pin numbers are read on the phone). Null when
  /// nothing is close.
  Future<_Placed?> _placedNamed(String words) async {
    final opts = [
      for (final p in _placed) {'value': p.name, 'label': p.name}
    ];
    if (opts.isEmpty) return null;
    final value = await s.matchHeard(words, opts);
    if (value == null || !mounted) return null;
    for (final p in _placed) {
      if (p.name == value) return p;
    }
    return null;
  }

  // ── done ────────────────────────────────────────────────────────────
  void _send() {
    if (!_canSave()) return;
    _releaseVoice();
    s.sendPinMap([
      for (final p in _placed)
        {
          'type_id': p.typeId,
          'name': p.name,
          'pins': p.pins,
          if (p.confirmed) 'confirmed': true,
        }
    ]);
  }

  /// Cancel, and ALWAYS leave. `sendPinMap` returns early when the frame
  /// is already gone, so a screen that somehow stayed mounted after the
  /// first cancel had a back button that did nothing at all -- a dead end
  /// with no way out but killing the app. If there is nothing left to
  /// tell the hub, just go -- but only if this screen is still the one on
  /// top: a second tap during the exit animation used to pop the screen
  /// UNDER it too.
  void _cancel() {
    _releaseVoice();
    if (s.pinMap == null) {
      final route = _route;
      if (route == null || route.isCurrent) Navigator.of(context).pop();
      return;
    }
    s.sendPinMap(null, draft: [
      for (final p in _placed)
        {
          'type_id': p.typeId,
          'name': p.name,
          'pins': p.pins,
        }
    ]);
  }

  /// The frame went away without this screen sending anything (a flow
  /// ended, the connection dropped, a cancel from elsewhere). The route
  /// pop that should follow is not guaranteed -- the flow unwind stops at
  /// any sheet or dialog of ours, and it used to leave a blank page under
  /// it -- so the screen closes its own modals and leaves.
  void _leave() {
    if (!mounted) return;
    if (s.pinMap != null) {
      _leaving = false; // a new frame arrived in the meantime
      return;
    }
    _releaseVoice();
    final route = _route;
    if (route == null || !route.isActive) return; // already on its way out
    final nav = Navigator.of(context);
    if (!route.isCurrent) nav.popUntil((r) => r == route);
    nav.pop();
  }

  /// A newer frame replaced the board: whatever sheet or dialog was open
  /// belonged to the old one.
  void _closeModals() {
    final route = _route;
    if (!mounted || route == null || !route.isActive || route.isCurrent) {
      return;
    }
    Navigator.of(context).popUntil((r) => r == route);
  }

  // ── drawing ─────────────────────────────────────────────────────────
  @override
  Widget build(BuildContext context) {
    _route = ModalRoute.of(context);
    return ListenableBuilder(
      listenable: s,
      builder: (context, _) {
        final live = s.pinMap;
        // A route already on its way out adopts nothing: a newer frame
        // that finds this screen popped gets a screen of its own.
        final active = _route?.isActive ?? true;
        if (live != null && !identical(live, _frame) && active) {
          _adopt(live);
          WidgetsBinding.instance.addPostFrameCallback((_) => _closeModals());
        } else if (live == null && !_leaving) {
          // Keep drawing the last board while leaving: an empty box here
          // was the black page behind any sheet the flow unwind stopped at.
          _leaving = true;
          WidgetsBinding.instance.addPostFrameCallback((_) => _leave());
        }
        return _build(context);
      },
    );
  }

  /// One column of the board as drawn: GPIO pins as tappable tiles, power
  /// and control pins (EN, GND, VIN, 3V3) as inert markers in their real
  /// places. From the hub's `layout` when it sends one -- silkscreen names
  /// in the order they sit on the header, USB at the top -- so the picture
  /// on the phone has the shape of the board in the hand. A hub too old to
  /// send a layout gets the old two columns of ascending numbers.
  List<Widget> _column(List<dynamic>? layoutSide, List<dynamic> fallback,
      Map<int, _Placed> taken, Map<String, dynamic>? awaitingSlot) {
    if (layoutSide == null) {
      return [
        for (final p in fallback)
          _pinTile(p as Map<String, dynamic>, taken, awaitingSlot)
      ];
    }
    final byNumber = <int, Map<String, dynamic>>{
      for (final p in _pins)
        if (p is Map<String, dynamic> && p['n'] is int) p['n'] as int: p
    };
    return [
      for (final e in layoutSide)
        if (e is Map && e['n'] is int && byNumber[e['n']] != null)
          _pinTile(byNumber[e['n']]!, taken, awaitingSlot)
        else if (e is Map)
          _inertPin(e['label'] as String? ?? ''),
    ];
  }

  /// EN, GND, VIN, 3V3, NC: on the board, not for wiring devices to.
  Widget _inertPin(String label) => Container(
        margin: const EdgeInsets.symmetric(vertical: 3),
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
        decoration: BoxDecoration(
          color: s.bg,
          border: Border.all(color: s.border.withValues(alpha: 0.5)),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(label,
                style: TextStyle(
                    color: s.inkFaint,
                    fontSize: 13,
                    fontWeight: FontWeight.w700)),
            const SizedBox(height: 2),
            Text(
                switch (label) {
                  'GND' => 'ground',
                  'VIN' => '5 V in',
                  '3V3' => '3.3 V out',
                  '5V' => '5 V',
                  'EN' || 'RST' => 'reset',
                  'NC' => 'not connected',
                  // The module's flash, brought out to the header: drawn
                  // because it is on the real board, never for wiring.
                  'CLK' || 'D0' || 'D1' || 'CMD' => 'flash, not for use',
                  _ => '',
                },
                style: TextStyle(color: s.inkFaint, fontSize: 10.5)),
          ],
        ),
      );

  Widget _build(BuildContext context) {
    final taken = _taken;
    final half = (_pins.length + 1) ~/ 2;
    // The board's drawing: its two header columns. A layout with neither
    // column (a board file drawn some other way) is treated as none, and a
    // missing column is empty rather than half of the plain list.
    final rawLayout = _cat['layout'];
    final layout = rawLayout is Map &&
            (rawLayout['left'] is List || rawLayout['right'] is List)
        ? rawLayout
        : null;
    final leftSide =
        layout == null ? null : (layout['left'] as List?) ?? const [];
    final rightSide =
        layout == null ? null : (layout['right'] as List?) ?? const [];
    final left = _pins.take(half).toList();
    final right = _pins.skip(half).toList();
    // Pins the hub offers that the drawn header does not bring out (the
    // classic 30-pin DevKit's drawing has no GPIO0, 9 or 10). They were
    // sayable and placeable (and a saved board could already use one) yet
    // drawn nowhere. Only the two drawn columns count as drawn.
    final onHeader = <int>{
      for (final side in [leftSide, rightSide])
        if (side != null)
          for (final e in side)
            if (e is Map && e['n'] is int) e['n'] as int
    };
    final offHeader = layout != null
        ? [
            for (final p in _pins)
              if (p is Map<String, dynamic> &&
                  p['n'] is int &&
                  !onHeader.contains(p['n']))
                p
          ]
        : const <Map<String, dynamic>>[];

    // The slot the next pin tap will answer, whether that is a fresh
    // placement's next line or a single line being moved or set on a
    // device already placed. Same banner, same dimming: to the person it
    // is the same question -- "which pin?".
    final Map<String, dynamic>? awaitingSlot;
    if (_awaitingType != null) {
      // Every slot can be filled before any tap -- a second I²C sensor
      // adopts the whole bus -- and the screen still rebuilds while the
      // name dialog is up. One past the end is "nothing owed", not an
      // index (2026-08-25c; reachable from the device-first door).
      final aSlots =
          (_awaitingType!['slots'] as List).cast<Map<String, dynamic>>();
      awaitingSlot =
          _awaitingSlot < aSlots.length ? aSlots[_awaitingSlot] : null;
    } else if (_movingPlaced != null && _movingSlotKey != null) {
      awaitingSlot = _slotOf(_movingPlaced!.typeId, _movingSlotKey!);
    } else {
      awaitingSlot = null;
    }
    final incomplete = _incomplete();
    final canSave = _canSave();

    // THE VOCABULARY FOLLOWS THE SCREEN, from the one place that runs on
    // every change. Re-arming from each setState call site would mean
    // finding all of them, today and every time one is added; a screen
    // that has just drawn itself knows exactly what it offers.
    WidgetsBinding.instance.addPostFrameCallback((_) => _rearmVoice());

    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) _cancel();
      },
      child: Scaffold(
        backgroundColor: s.bg,
        appBar: AppBar(
          backgroundColor: s.bg,
          elevation: 0,
          leading: IconButton(
              icon: Icon(Icons.close, color: s.ink), onPressed: _cancel),
          title: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(_frame?.boardName ?? 'Board',
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                      color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
              if (_boardKind().isNotEmpty)
                Text(_boardKind(),
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(color: s.inkFaint, fontSize: 12)),
            ],
          ),
          actions: [
            TextButton(
              onPressed: canSave ? _send : null,
              child: Text('Save',
                  style: TextStyle(
                      color: canSave ? s.ink : s.inkFaint,
                      fontWeight: FontWeight.w600)),
            ),
          ],
        ),
        // DEVICE FIRST has a first-class door: the full catalogue, one
        // row per device, then the pins by tapping. Hidden while a pin
        // tap is owed so it cannot start a second placement mid-first.
        floatingActionButton: (awaitingSlot == null && _movingPlaced == null)
            ? FloatingActionButton.extended(
                backgroundColor: s.accent,
                foregroundColor: s.accentFg,
                icon: const Icon(Icons.add, size: 20),
                label: const Text('Add a device',
                    style: TextStyle(fontWeight: FontWeight.w600)),
                onPressed: () {
                  _byVoice = false;
                  _addDevice();
                },
              )
            : null,
        body: SafeArea(
          child: Column(
            children: [
              _banner(awaitingSlot, incomplete),
              Expanded(
                child: SingleChildScrollView(
                  padding: const EdgeInsets.fromLTRB(12, 4, 12, 100),
                  child: Column(
                    children: [
                      if ((_boardInfo['note'] as String? ?? '').isNotEmpty)
                        Padding(
                          padding: const EdgeInsets.fromLTRB(4, 4, 4, 8),
                          child: Row(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Icon(Icons.info_outline,
                                  size: 14, color: s.inkFaint),
                              const SizedBox(width: 6),
                              Expanded(
                                child: Text(_boardInfo['note'] as String,
                                    style: TextStyle(
                                        color: s.inkFaint, fontSize: 11.5)),
                              ),
                            ],
                          ),
                        ),
                      Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Expanded(
                              child: Column(
                                  children: _column(
                                      leftSide, left, taken, awaitingSlot))),
                          Container(
                              width: 34,
                              margin: const EdgeInsets.symmetric(vertical: 8),
                              decoration: BoxDecoration(
                                  color: s.surface,
                                  border: Border.all(color: s.border),
                                  borderRadius: BorderRadius.circular(6)),
                              child: RotatedBox(
                                  quarterTurns: 1,
                                  child: Center(
                                      child: Text(
                                          (_boardInfo['family_label']
                                                  as String?) ??
                                              'ESP32',
                                          style: TextStyle(
                                              color: s.inkFaint,
                                              fontSize: 11,
                                              letterSpacing: 3))))),
                          Expanded(
                              child: Column(
                                  children: _column(
                                      rightSide, right, taken, awaitingSlot))),
                        ],
                      ),
                      if (offHeader.isNotEmpty)
                        _offHeader(offHeader, taken, awaitingSlot),
                      if (_placed.isNotEmpty) _summary(),
                    ],
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  /// "ESP32 DevKit V1 (30 pins) · ESP32 (classic)", "Any ESP32-S3 board":
  /// the board being wired, under its name. Empty from an older hub.
  String _boardKind() {
    final label = (_boardInfo['label'] as String?)?.trim() ?? '';
    final family = (_boardInfo['family_label'] as String?)?.trim() ?? '';
    if (label.isEmpty) return family;
    if (family.isEmpty || label.contains(family)) return label;
    return '$label · $family';
  }

  Widget _offHeader(List<Map<String, dynamic>> pins, Map<int, _Placed> taken,
      Map<String, dynamic>? awaitingSlot) {
    return Padding(
      padding: const EdgeInsets.only(top: 14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('ALSO ON THIS CHIP (NOT IN THE DRAWING)',
              style: TextStyle(
                  color: s.inkFaint,
                  fontSize: 11,
                  letterSpacing: 0.8,
                  fontWeight: FontWeight.w700)),
          const SizedBox(height: 4),
          Wrap(
            spacing: 8,
            children: [
              for (final p in pins)
                SizedBox(width: 140, child: _pinTile(p, taken, awaitingSlot)),
            ],
          ),
        ],
      ),
    );
  }

  Widget _banner(Map<String, dynamic>? awaitingSlot,
      List<(_Placed, List<String>)> incomplete) {
    // Three states, in priority order: a pin is being asked for; something
    // placed is still short of a line (Save is held until it is not); or
    // the plain count. The middle one is what makes an incomplete device
    // impossible to overlook -- it sits above the board in amber until the
    // line is set, naming the device and the line.
    final moving = _movingPlaced != null;
    final placing = _awaitingType != null;
    final String text;
    Color tint = Colors.transparent;
    Color ink = s.inkDim;
    if (awaitingSlot != null) {
      // Name whose pin is being asked for: the device mid-move, or the
      // device mid-placement — a bare "tap the pin for: data pin" reads
      // as the board's question when it is one device's.
      final who = moving
          ? ' (${_movingPlaced!.name})'
          : placing
              ? ' (${(_awaitingType!['label'] as String? ?? '').toLowerCase()})'
              : '';
      text = 'Now tap or say the pin for: '
          '${(awaitingSlot['label'] as String).toLowerCase()}$who';
      tint = const Color(0xFF1F6FEB).withValues(alpha: 0.16);
      ink = s.ink;
    } else if (incomplete.isNotEmpty) {
      final (p, missing) = incomplete.first;
      text = '${p.name} still needs: ${missing.join(', ')} — tap it to fix.';
      tint = const Color(0xFFD29922).withValues(alpha: 0.14);
      ink = s.ink;
    } else if (_placed.isEmpty) {
      text = 'Add a device, or tap a pin to say what is wired to it.';
    } else {
      text = '${_placed.length} placed, '
          '${_freePinCount(_taken)} pins free.';
    }
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(16, 10, 8, 10),
      color: tint,
      child: Row(
        children: [
          Expanded(
              child: Text(text, style: TextStyle(color: ink, fontSize: 13))),
          // A move or a half-placed device that turns out to be a mistake
          // needs a way out that is not "pick some pin and carry on".
          // Backing out mid-pins loses nothing, the CLI's rule.
          if (moving || placing)
            IconButton(
              tooltip: moving ? 'Stop moving' : 'Stop adding',
              // A cancel affordance -- it wears the disconnect red.
              icon: Icon(Icons.close, size: 18, color: s.cancelInk),
              onPressed: () => setState(_stopPlacing),
            ),
        ],
      ),
    );
  }

  Widget _pinTile(Map<String, dynamic> pin, Map<int, _Placed> taken,
      Map<String, dynamic>? awaitingSlot) {
    final n = pin['n'] as int;
    final owner = taken[n];
    final severity = pin['severity'] as String? ?? 'ok';

    // While a slot is outstanding, pins that cannot take it recede. They are
    // NOT disabled — the confirm dialog is still reachable — but the eye
    // should land on the ones that work.
    final dim = awaitingSlot != null && !_fits(pin, awaitingSlot);

    Color border = s.border;
    if (owner != null) {
      border = const Color(0xFF2EA043);
    } else if (severity == 'warn') {
      border = const Color(0xFFD29922);
    }

    final caution = pin['caution'] as String? ?? '';
    return Opacity(
      opacity: dim ? 0.35 : 1,
      child: InkWell(
        onTap: () {
          _byVoice = false;
          _tapPin(n);
        },
        onLongPress: caution.isEmpty ? null : () => _say(caution),
        borderRadius: BorderRadius.circular(8),
        child: Container(
          margin: const EdgeInsets.symmetric(vertical: 3),
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
          decoration: BoxDecoration(
            color: owner != null
                ? const Color(0xFF2EA043).withValues(alpha: 0.10)
                : s.surface,
            border: Border.all(color: border),
            borderRadius: BorderRadius.circular(8),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  // THE BOARD'S OWN NAME FIRST -- D32, TX2, VP -- because that
                  // is what is printed beside the pin and drawn on the
                  // schematic; the GPIO number stays, smaller, for the
                  // datasheet. A label is only sent by a hub that knows
                  // this board; without one the number stands alone.
                  Flexible(
                    child: Text(_pinName(n),
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                            color: s.ink,
                            fontSize: 13,
                            fontWeight: FontWeight.w700)),
                  ),
                  // "D26" and "GPIO4" already say their number; "TX2" and
                  // "VP" do not.
                  if (RegExp(r'(\d+)$').firstMatch(_pinName(n))?.group(1) !=
                      '$n') ...[
                    const SizedBox(width: 5),
                    Text('GPIO$n',
                        style: TextStyle(color: s.inkFaint, fontSize: 10)),
                  ],
                  if (severity == 'warn') ...[
                    const SizedBox(width: 5),
                    const Icon(Icons.info_outline,
                        size: 12, color: Color(0xFFD29922)),
                  ],
                ],
              ),
              const SizedBox(height: 2),
              Text(owner?.name ?? (pin['purpose'] as String? ?? ''),
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                      color: owner != null
                          ? const Color(0xFF2EA043)
                          : s.inkFaint,
                      fontSize: 10.5)),
            ],
          ),
        ),
      ),
    );
  }

  Widget _summary() {
    return Container(
      margin: const EdgeInsets.only(top: 18),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
          color: s.surface,
          border: Border.all(color: s.border),
          borderRadius: BorderRadius.circular(10)),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('ON THIS BOARD',
              style: TextStyle(
                  color: s.inkFaint,
                  fontSize: 11,
                  letterSpacing: 0.8,
                  fontWeight: FontWeight.w700)),
          const SizedBox(height: 8),
          // Each row IS the device's editor: tapping it opens the same
          // sheet a tap on one of its pins does, so a device can be
          // corrected from the list without hunting for its pin on the
          // board. A device short of a line says so, in amber, right here.
          for (final p in _placed)
            InkWell(
              onTap: () {
                _byVoice = false;
                _showPlaced(p);
              },
              borderRadius: BorderRadius.circular(6),
              child: Padding(
                padding: const EdgeInsets.only(bottom: 6),
                child: Row(
                  children: [
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                              '${p.name}  ·  '
                              '${_type(p.typeId)?['label'] ?? ''}',
                              style: TextStyle(color: s.ink, fontSize: 13)),
                          if (_problems(p).isNotEmpty)
                            Text('needs: ${_problems(p).join(', ')}',
                                style: const TextStyle(
                                    color: Color(0xFFD29922), fontSize: 11)),
                        ],
                      ),
                    ),
                    Text(p.pins.values.map(_pinName).join(', '),
                        style: TextStyle(color: s.inkFaint, fontSize: 11)),
                    IconButton(
                      icon: Icon(Icons.close, size: 15, color: s.inkFaint),
                      onPressed: () => setState(() => _remove(p)),
                    ),
                  ],
                ),
              ),
            ),
        ],
      ),
    );
  }
}

/// The whole catalogue at once — one row per device, grouped, both doors.
///
/// THE BRANCH QUESTION IS GONE (2026-08-25c), here exactly as on the CLI.
/// There used to be a does/reads/something-else question in front of every
/// add, and it earned its keep exactly once — stopping a yellow LED from
/// being described as an RGB light — while charging every single add one
/// extra answer for it. That lesson is carried by the LIST now: "Something
/// that turns on and off" leads the Switching group, so a bare LED has an
/// obvious home without a toll booth in front of it. Augusta, 2026-08-24:
/// "should just show everything at once." Each row carries its group and
/// its note, so the scan stays honest.
///
/// TWO DOORS, ONE SHEET. Opened from a pin tap (`pin` set), rows are
/// filtered to what that pin can carry, with "Show everything" one tap
/// away and won't-work rows labelled. Opened from Add a device (`pin`
/// null — device first, the CLI's order), the full catalogue shows
/// unfiltered, because there is no pin yet to disagree with.
///
/// NO RULES LIVE HERE. What fits is `fits` — the pin's `can` against the
/// slot's role, both sent by the hub. The phone has never known that
/// GPIO34 is input-only and still does not.
class _DeviceSheet extends StatefulWidget {
  final AppState appState;

  /// The tapped pin, or null when the person started from the device
  /// list — device first, pins after.
  final Map<String, dynamic>? pin;
  final List<dynamic> types;
  final List<String> groups;
  final bool Function(Map<String, dynamic>, Map<String, dynamic>) fits;

  /// What else was said with the pin ("put a RELAY on pin 26"): the
  /// sheet opens already asking "Relay. Is that it?".
  final String hint;

  /// Said when the sheet first arms, when it was opened by voice.
  final String spokenPrompt;

  const _DeviceSheet({
    required this.appState,
    required this.pin,
    required this.types,
    required this.groups,
    required this.fits,
    this.hint = '',
    this.spokenPrompt = '',
  });

  @override
  State<_DeviceSheet> createState() => _DeviceSheetState();
}

class _DeviceSheetState extends State<_DeviceSheet> {
  bool _showAll = false;

  /// The catalogue crossed ninety entries with the sweep, and scrolling
  /// ninety rows to find "heater" is a wall of its own ("should have a
  /// search bar for quicker finding", field test 2026-08-25). Matches
  /// against the label, the group, and the note, so "co2", "soil" and
  /// "shutter" all land.
  String _query = '';

  // ── say it, then say yes ────────────────────────────────────────────
  //
  // Her format (2026-08-31): you speak the device, it names one back and
  // asks; you answer yes, no, or "say it again". "No" reaches the NEXT
  // best from the thing you already said -- being made to repeat yourself
  // would be the punishment for its own wrong guess. When nothing else is
  // close it stops guessing and hands you the list, which is why the
  // search box stays exactly where it was.
  //
  // A TAP ALWAYS WINS: touching a row pops the sheet, dispose runs, the
  // mic is handed back. You never have to say "no" to reach something you
  // can already see.

  AppState get s => widget.appState;

  /// The type_id being confirmed right now, and the utterance that
  /// proposed it.
  String? _confirming;
  String _heard = '';
  final Set<String> _rejected = {};

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _armDevices());
  }

  // NO DISARM IN dispose. The board that opened this sheet takes its list
  // off the mic the moment the sheet's future completes (_modal), and
  // re-arms its own on its next frame. This dispose runs after the exit
  // animation -- AFTER that re-arm -- and its disarm used to leave the
  // board deaf until something else rebuilt it.

  /// Close the sheet with nothing chosen (a spoken "cancel").
  void _close() {
    if (mounted) Navigator.of(context).pop();
  }

  bool _hintTried = false;
  bool _prompted = false;

  /// A label as it should be SAID. The pin_map payload carries `label`
  /// ("Temperature + humidity (DHT11)") and not the catalogue's `plain`,
  /// and a plus sign read aloud is a plus sign. The part number in
  /// brackets is kept -- it is how you tell a DHT11 from a DHT22 -- just
  /// unbracketed.
  static String _spoken(String label) => label
      .replaceAll('+', 'and')
      .replaceAll(RegExp(r'[()]'), '')
      .replaceAll(RegExp(r'\s+'), ' ')
      .trim();

  /// `every`: rows this pin cannot carry too (for the words said with
  /// the pin, which may name one -- "a relay on 34").
  List<Map<String, dynamic>> _voiceOptions({bool every = false}) => [
        for (final r in _rows(all: true, every: every))
          if ((every || r['works'] == true) &&
              !_rejected.contains((r['type_id'] ?? '').toString())) ...[
            {
              'value': (r['type_id'] ?? '').toString(),
              'label': _spoken((r['label'] ?? '').toString()),
            },
            for (final alias in _aliases((r['type_label'] ?? '').toString()))
              {'value': (r['type_id'] ?? '').toString(), 'label': alias},
          ]
      ];

  /// The other names a device answers to, each its own voice option.
  ///
  /// Said alone, "relay" or "DHT22" was nowhere near a whole label: the
  /// hub found "relay" inside a dozen labels ("Fan on a relay", "Lamp on
  /// a relay") and so committed to none, and a bare part number scored
  /// nothing against "Temperature and humidity DHT22 AM2301". So the
  /// label's first alternative ("Relay / socket / pump"), the thing on
  /// the relay ("Fan on a relay") and the part numbers in its brackets
  /// are offered as names too. Every pick still
  /// ends at "... Is that it?", so a near miss costs a "no".
  static List<String> _aliases(String label) {
    final out = <String>{};
    final bracket = RegExp(r'\(([^)]*)\)').firstMatch(label);
    final outside = label.replaceAll(RegExp(r'\([^)]*\)'), ' ');
    // The first of "Relay / socket / pump" only: the later ones are
    // generic ("light" in "Aquarium pump / light") and would take the
    // word from every other light on the list.
    if (outside.contains('/')) {
      final w = outside.split('/').first.trim().toLowerCase();
      if (w.isNotEmpty) out.add(w);
    }
    // "Fan on a relay" is a fan.
    final head = RegExp(r'^(.+?) on a relay$', caseSensitive: false)
        .firstMatch(outside.trim());
    if (head != null) out.add(head.group(1)!.trim().toLowerCase());
    for (final w in (bracket?.group(1) ?? '').split(RegExp(r'[/,:\s]+'))) {
      if (w.length >= 4 &&
          RegExp(r'[A-Za-z]').hasMatch(w) &&
          RegExp(r'[0-9]').hasMatch(w)) {
        out.add(w.toLowerCase());
      }
    }
    return out.toList();
  }

  Future<void> _armDevices() async {
    if (!mounted) return;
    if (_confirming != null) setState(() => _confirming = null);
    final opts = _voiceOptions();
    if (opts.isEmpty) {
      s.disarmScreenChoice();
      return;
    }
    // "put a relay on pin 26": the words after the pin already name the
    // device, so the first thing the sheet does is ask about it.
    // Matched against EVERY device, not only what fits this pin: "a relay
    // on 34" is a relay, and the sheet shows everything and asks about it
    // -- the won't-work warning follows, as it does for a tap there.
    if (!_hintTried && widget.hint.trim().isNotEmpty) {
      _hintTried = true;
      final guess = await s.matchHeard(widget.hint, _voiceOptions(every: true));
      if (!mounted) return;
      if (guess != null) {
        final fits = opts.any((o) => o['value'] == guess);
        if (!fits && !_showAll) setState(() => _showAll = true);
        _heard = widget.hint;
        _askIsThisIt(guess);
        return;
      }
    }
    final prompt = _prompted ? '' : widget.spokenPrompt;
    _prompted = true;
    s.armScreenChoice(
      options: opts,
      prompt: prompt,
      speakPrompt: prompt.isNotEmpty,
      onCancel: _close,
      onPick: (value, heard) {
        _heard = heard;
        _askIsThisIt(value);
      },
    );
  }

  Map<String, dynamic>? _rowFor(String typeId) {
    for (final r in _rows(all: true)) {
      if ((r['type_id'] ?? '').toString() == typeId) return r;
    }
    return null;
  }

  void _askIsThisIt(String typeId) {
    if (!mounted) return;
    final row = _rowFor(typeId);
    if (row == null) {
      _armDevices();
      return;
    }
    setState(() => _confirming = typeId);
    final question = '${_spoken((row['label'] ?? '').toString())}. Is that it?';
    s.armScreenChoice(
      prompt: question,
      speakPrompt: true,
      // "Say it again" repeats the CANDIDATE. Reading back "yes, no" would
      // answer nothing.
      repeatLine: question,
      options: const [
        {'value': 'yes', 'label': 'yes'},
        {'value': 'no', 'label': 'no'},
      ],
      onCancel: _close,
      onPick: (value, _) async {
        if (!mounted) return;
        if (value == 'yes') {
          Navigator.of(context).pop(row);
          return;
        }
        _rejected.add(typeId);
        // Awaited now: the matching is the hub's since 2026-09-11.
        final next = await s.matchHeard(_heard, _voiceOptions());
        if (!mounted) return;
        if (next != null) {
          _askIsThisIt(next);
          return;
        }
        // NOTHING ELSE CLOSE. It stops talking rather than reading a
        // catalogue aloud; the list is on screen and the search box is
        // where it has always been.
        setState(() => _confirming = null);
        s.disarmScreenChoice();
        s.speak('Nothing else close. Pick it from the list, '
            'or type what you are after.');
      },
    );
  }

  bool _matches(Map<String, dynamic> t) {
    if (_query.isEmpty) return true;
    final hay = '${t['label'] ?? ''} ${t['group'] ?? ''} ${t['note'] ?? ''}'
        .toLowerCase();
    return _query
        .toLowerCase()
        .split(' ')
        .where((w) => w.isNotEmpty)
        .every(hay.contains);
  }

  /// ONE ROW PER DEVICE, entered at the first line this pin can carry.
  ///
  /// It used to be one row per SLOT, and the reasoning was sound for a
  /// two-pin device: an HC-SR04's echo is happy on an input-only pin while
  /// its trigger is not, so the list had to be able to offer "Distance —
  /// echo pin" and go on to ask for the trigger. Offering the device whole
  /// would either hide it or promise something the pin cannot keep.
  ///
  /// It stopped being sound at four pins. An RC522 filled the sheet with
  /// four near-identical rows -- sck, mosi, miso, chip select -- and asked
  /// the person to decide which line this particular pin was, before they
  /// had placed anything. Three of those four are a SHARED BUS: not a
  /// choice about this pin at all, just wiring the device needs.
  ///
  /// So: one row, and the row works out its own entry point -- the first
  /// slot this pin genuinely satisfies. On an input-only pin the HC-SR04
  /// row still enters at echo, which is what the old shape existed to
  /// protect; on an ordinary pin it enters at trigger. The remaining lines
  /// are then asked for by tapping, which the placement flow already does,
  /// and shared buses already on the board are filled in silently.
  ///
  /// With no pin in hand (device first) every device is offered whole,
  /// entered at its first slot — there is nothing yet to fit or fail.
  /// `all: true` ignores the typed query -- the voice path offers what
  /// the PIN allows, not what the search box happens to be filtering.
  List<Map<String, dynamic>> _rows({bool all = false, bool every = false}) {
    final pin = widget.pin;
    final rows = <Map<String, dynamic>>[];
    for (final t in widget.types) {
      if (t is! Map<String, dynamic>) continue;
      if (!all && !_matches(t)) continue;
      final slots = (t['slots'] as List? ?? const [])
          .whereType<Map<String, dynamic>>()
          .toList();
      if (slots.isEmpty) continue;
      final label = t['label'] as String? ?? '';
      if (pin == null) {
        rows.add({
          'type_id': t['id'],
          'slot_key': slots.first['key'],
          'group': t['group'],
          'note': t['note'] ?? '',
          'works': true,
          'label': label,
          'type_label': label,
        });
        continue;
      }
      // The entry point: the first line this pin can actually be. -1 means
      // no line fits, in which case the row still appears under "show
      // everything" and enters at the device's first slot, so the
      // won't-work confirmation names something real rather than nothing.
      final idx = slots.indexWhere((sl) => widget.fits(pin, sl));
      final works = idx >= 0;
      final entry = slots[works ? idx : 0];
      if (!works && !_showAll && !every) continue;
      rows.add({
        'type_id': t['id'],
        'slot_key': entry['key'],
        'group': t['group'],
        'note': t['note'] ?? '',
        'works': works,
        // Multi-pin devices say which line THIS pin becomes, because the
        // answer is not obvious and the next question depends on it.
        'type_label': label,
        'label': slots.length > 1
            ? '$label — ${(entry['label'] as String).toLowerCase()}'
            : label,
      });
    }
    // Working rows first, original catalogue order preserved within each
    // half -- sort() is not stable, so the index is the tie-breaker.
    final order = {for (var i = 0; i < rows.length; i++) rows[i]['type_id']: i};
    rows.sort((a, b) {
      final byWorks =
          (a['works'] == true ? 0 : 1) - (b['works'] == true ? 0 : 1);
      if (byWorks != 0) return byWorks;
      return (order[a['type_id']] ?? 0) - (order[b['type_id']] ?? 0);
    });
    return rows;
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.appState;
    final pin = widget.pin;
    final rows = _rows();
    final caution = (pin?['caution'] as String?) ?? '';

    return DraggableScrollableSheet(
      expand: false,
      initialChildSize: 0.75,
      maxChildSize: 0.92,
      builder: (context, controller) => Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 14, 16, 6),
            child: Row(
              children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                          pin == null
                              ? 'What are you adding?'
                              : 'What is on ${(pin['label'] as String?)?.isNotEmpty == true ? pin['label'] : 'GPIO${pin['n']}'}?',
                          style: TextStyle(
                              color: s.ink,
                              fontSize: 16,
                              fontWeight: FontWeight.w600)),
                      if (pin != null)
                        Text(pin['purpose'] as String? ?? '',
                            style:
                                TextStyle(color: s.inkFaint, fontSize: 11.5)),
                    ],
                  ),
                ),
                if (pin != null)
                  TextButton(
                    onPressed: () => setState(() => _showAll = !_showAll),
                    child: Text(_showAll ? 'Only what fits' : 'Show everything',
                        style: TextStyle(color: s.inkFaint, fontSize: 12)),
                  ),
              ],
            ),
          ),
          if (caution.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 0, 20, 4),
              child: Align(
                alignment: Alignment.centerLeft,
                child: Text(caution,
                    style:
                        const TextStyle(color: Color(0xFFD29922), fontSize: 11.5)),
              ),
            ),
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 4, 16, 4),
            child: TextField(
              autofocus: false,
              onChanged: (v) => setState(() => _query = v),
              style: TextStyle(color: s.ink, fontSize: 14),
              decoration: InputDecoration(
                isDense: true,
                prefixIcon: Icon(Icons.search, size: 18, color: s.inkFaint),
                hintText: 'Search devices — heater, co2, soil…',
                hintStyle: TextStyle(color: s.inkFaint, fontSize: 13),
                enabledBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(10),
                    borderSide: BorderSide(color: s.border)),
                focusedBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(10),
                    borderSide: BorderSide(color: s.accent)),
                contentPadding:
                    const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
              ),
            ),
          ),
          if (_query.isNotEmpty && rows.isEmpty)
            Padding(
              padding: const EdgeInsets.all(16),
              child: Text('Nothing matches — try a shorter word.',
                  style: TextStyle(color: s.inkFaint, fontSize: 13)),
            ),
          Expanded(
            child: ListView(
              controller: controller,
              padding: const EdgeInsets.fromLTRB(12, 0, 12, 24),
              children: [
                for (final group in widget.groups)
                  if (rows.any((r) => r['group'] == group)) ...[
                    Padding(
                      padding: const EdgeInsets.fromLTRB(4, 12, 4, 6),
                      child: Text(group.toUpperCase(),
                          style: TextStyle(
                              color: s.inkFaint,
                              fontSize: 11,
                              letterSpacing: 0.8,
                              fontWeight: FontWeight.w700)),
                    ),
                    for (final r in rows.where((r) => r['group'] == group))
                      _tile(context, r),
                  ],
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _tile(BuildContext context, Map<String, dynamic> r) {
    final s = widget.appState;
    // THE ROW IT IS ASKING ABOUT, marked while it asks. Hearing "is that
    // it?" without seeing WHICH one leaves you answering yes to a name you
    // have to hold in your head -- and the row is the tap that overrides
    // the guess, so it had better be the obvious one.
    final asking = _confirming != null &&
        (r['type_id'] ?? '').toString() == _confirming;
    return Opacity(
      opacity: r['works'] == true ? 1 : 0.45,
      child: Container(
        decoration: asking
            ? BoxDecoration(
                color: s.surface,
                border: Border.all(color: s.accent),
                borderRadius: BorderRadius.circular(12))
            : null,
        child: ListTile(
        dense: true,
        title: Text(r['label'] as String,
            style: TextStyle(color: s.ink, fontSize: 14)),
        subtitle: (r['note'] as String).isEmpty
            ? null
            : Text(r['note'] as String,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(color: s.inkFaint, fontSize: 11.5)),
        trailing: r['works'] == true
            ? null
            : const Text('will not work',
                style: TextStyle(color: Color(0xFFD29922), fontSize: 10)),
        onTap: () => Navigator.of(context).pop(r),
        ),
      ),
    );
  }
}

/// What was said on the pin screen, read HERE and not by the hub.
///
/// PIN NUMBERS ARE NEVER FUZZY-MATCHED. The hub's matcher scores "pin 26"
/// 0.91 against "pin 2" -- enough to commit -- so a taken 26 became a free
/// 2, and a wrong pin is a wiring mistake. So a number is read exactly or
/// not at all:
///
///   * words or digits: "twenty six", "twenty-six", "26.", "thirty nine";
///     two single digits are one number ("two six" = 26);
///   * a pin word before it: "pin", "gpio" (also spelled "g p i o", or
///     glued, "gpio26"), "d" as the board prints it ("D26");
///   * the names THIS board prints, from the frame's pin labels (RX2,
///     TX2, VP, VN on the classic DevKit; GPIO4 on a generic board), also
///     spelled out ("r x 2");
///   * the two mis-hearings that only make sense where a number belongs:
///     "pin for" = 4 and "pin to"/"too" = 2 (never when a number follows:
///     "move it to 27" keeps its "to"), and "thirty for" = 34.
///
/// A number counts as a PIN when it starts the sentence or follows a pin
/// word, "on/to/at/from", a verb ("move 26 to 27") or another pin ("26
/// and 27" -- two pins, which the screen refuses rather than picking one).
/// After any other word it is part of a name: "DHT 22", "lamp 2".
class _Spoken {
  /// Every pin mentioned, in the order said, up to 999. Not validated: the
  /// screen says "there is no GPIO20" (a number this board does not have)
  /// rather than this quietly dropping it.
  final List<int> pins;

  /// The words that are not a pin, a verb or filler -- a device ("relay",
  /// "dht 22") or a placed name ("kitchen lamp").
  final String rest;

  /// 'remove', 'move' or ''.
  final String verb;

  /// A pin word was said with no number after it ("pin", "pin twenny").
  final bool pinWordOnly;

  const _Spoken(this.pins, this.rest, this.verb, this.pinWordOnly);

  static const Map<String, int> _numbers = {
    'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
    'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
    'nineteen': 19, 'twenty': 20, 'thirty': 30, 'forty': 40,
    'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90,
  };
  static const Set<String> _pinWords = {
    'pin', 'pins', 'gpio', 'io', 'gp', 'd', 'dee',
  };
  static const Set<String> _leadIns = {
    'on', 'to', 'at', 'onto', 'into', 'in', 'from',
  };
  // Words a number may stand behind and still be introduced by what is
  // before them: "pin NUMBER 26", "on THE 26".
  static const Set<String> _see = {'number', 'no', 'the'};
  static const Set<String> _also = {'and', 'or', 'then', 'plus'};
  static const Map<String, String> _verbs = {
    'remove': 'remove', 'delete': 'remove', 'clear': 'remove',
    'unplace': 'remove', 'drop': 'remove',
    'move': 'move', 'shift': 'move', 'change': 'move', 'swap': 'move',
  };
  // Letters said one at a time that make a word: "g p i o". The board's
  // own letter names ("r x", "v p") are added from its labels in read().
  static const Set<String> _spelled = {'gpio', 'io', 'gp'};
  static const Set<String> _filler = {
    'a', 'an', 'the', 'please', 'put', 'place', 'add', 'wire', 'wired',
    'connect', 'connected', 'plug', 'plugged', 'is', 'it', 'its', 's',
    'there', 'here', 'i', 'want', 'my', 'this', 'that', 'new', 'for',
    'of', 'with', 'up', 'off', 'away', 'out', 'now', 'number', 'no',
    'and', 'or', 'then', 'plus', 'take',
  };

  static final RegExp _digits = RegExp(r'^\d{1,3}$');
  static final RegExp _glued = RegExp(r'^([a-z]+)(\d+)$');

  static bool _numberWord(String w) =>
      _digits.hasMatch(w) || _numbers.containsKey(w);

  static _Spoken read(String heard, Map<String, int> named) {
    // 1. Words: lower case, anything not a letter or digit is a gap --
    //    "twenty-six" is two words, "26." is 26, "D26" is "d26".
    final words = heard
        .toLowerCase()
        .replaceAll(RegExp(r'[^a-z0-9]+'), ' ')
        .trim()
        .split(' ')
        .where((w) => w.isNotEmpty)
        .toList();

    // 2. Spelled-out letters: "g p i o" -> "gpio", "r x" -> "rx" -- the
    //    letters of this board's own names, whatever the board is.
    final spelled = {
      ..._spelled,
      for (final name in named.keys)
        if (RegExp(r'^[a-z]{2,4}').firstMatch(name) case final m?)
          m.group(0)!,
    };
    final joined = <String>[];
    for (var i = 0; i < words.length; i++) {
      var took = 0;
      for (var len = 4; len >= 2 && took == 0; len--) {
        if (i + len > words.length) continue;
        final run = words.sublist(i, i + len);
        if (run.every((w) => w.length == 1 && !_digits.hasMatch(w)) &&
            spelled.contains(run.join())) {
          took = len;
        }
      }
      if (took > 0) {
        joined.add(words.sublist(i, i + took).join());
        i += took - 1;
      } else {
        joined.add(words[i]);
      }
    }

    // 3. A pin word glued to its number: "gpio26", "d26". A board name
    //    ("rx2") and anything else ("dht22") stays whole.
    final split = <String>[];
    for (final w in joined) {
      final m = _glued.firstMatch(w);
      if (m != null && !named.containsKey(w) && _pinWords.contains(m.group(1))) {
        split
          ..add(m.group(1)!)
          ..add(m.group(2)!);
      } else {
        split.add(w);
      }
    }

    // 4. Numbers, exactly.
    final toks = <_SpokenWord>[];
    for (var i = 0; i < split.length; i++) {
      final w = split[i];
      int? v = _digits.hasMatch(w) ? int.parse(w) : _numbers[w];
      final prev = toks.isEmpty ? null : toks.last;
      final nextIsNumber = i + 1 < split.length && _numberWord(split[i + 1]);
      final afterPinWord = prev != null &&
          prev.number == null &&
          _pinWords.contains(prev.word);
      if (v == null && (w == 'for' || w == 'fore')) {
        // "thirty for" is thirty-four: a tens word cannot be followed by
        // "for" in anything said to this screen.
        final tens = prev?.number;
        if (prev != null && tens != null && !prev.whole &&
            tens >= 20 && tens <= 90 && tens % 10 == 0) {
          prev.settle(tens + 4);
          continue;
        }
        if (afterPinWord && !nextIsNumber) v = 4;
      }
      if (v == null && (w == 'to' || w == 'too') && afterPinWord &&
          !nextIsNumber) {
        v = 2;
      }
      final p = prev?.number;
      if (v != null && prev != null && p != null && !prev.whole) {
        if (p >= 20 && p <= 90 && p % 10 == 0 && v >= 1 && v <= 9) {
          prev.settle(p + v); // "twenty six"
          continue;
        }
        if (p <= 9 && v <= 9) {
          prev.settle(p * 10 + v); // "two six"
          continue;
        }
      }
      toks.add(_SpokenWord(v == null ? w : '$v', v));
    }

    // 5. The board's own names: "rx2" whole, or "rx" then 2.
    final merged = <_SpokenWord>[];
    for (var i = 0; i < toks.length; i++) {
      final t = toks[i];
      if (t.number == null && named.containsKey(t.word)) {
        merged.add(_SpokenWord(t.word, named[t.word], board: true));
        continue;
      }
      if (t.number == null && i + 1 < toks.length) {
        final n = toks[i + 1].number;
        final both = n == null ? null : named['${t.word}$n'];
        if (both != null) {
          merged.add(_SpokenWord('${t.word}$n', both, board: true));
          i++;
          continue;
        }
      }
      merged.add(t);
    }

    // 6. What each word is.
    String before(int i) {
      for (var j = i - 1; j >= 0; j--) {
        final b = merged[j];
        if (b.number != null) return '#';
        if (_see.contains(b.word)) continue;
        return b.word;
      }
      return '';
    }

    final pins = <int>[];
    final rest = <String>[];
    var verb = '';
    var pinWordOnly = false;
    for (var i = 0; i < merged.length; i++) {
      final t = merged[i];
      final n = t.number;
      if (n != null) {
        final b = before(i);
        if (t.board ||
            b.isEmpty ||
            b == '#' ||
            _pinWords.contains(b) ||
            _leadIns.contains(b) ||
            _verbs.containsKey(b) ||
            _also.contains(b)) {
          pins.add(n);
        } else {
          rest.add(t.word);
        }
        continue;
      }
      if (_pinWords.contains(t.word)) {
        var j = i + 1;
        while (j < merged.length && _see.contains(merged[j].word)) {
          j++;
        }
        if (j >= merged.length || merged[j].number == null) pinWordOnly = true;
        continue;
      }
      if (verb.isEmpty && rest.isEmpty && pins.isEmpty) {
        final v = _verbs[t.word];
        if (v != null) {
          verb = v;
          continue;
        }
        if (t.word == 'take' &&
            i + 1 < merged.length &&
            const {'off', 'away', 'out'}.contains(merged[i + 1].word)) {
          verb = 'remove';
          continue;
        }
      }
      if (_filler.contains(t.word) || _leadIns.contains(t.word)) continue;
      rest.add(t.word);
    }
    return _Spoken(pins, rest.join(' '), verb, pinWordOnly && pins.isEmpty);
  }
}

/// One word of an utterance on its way through _Spoken.read.
class _SpokenWord {
  String word;
  int? number;

  /// Already a two-part number ("twenty six"): nothing more joins it.
  bool whole = false;

  /// A silkscreen name: always a pin, whatever came before it.
  final bool board;

  _SpokenWord(this.word, this.number, {this.board = false});

  void settle(int n) {
    number = n;
    word = '$n';
    whole = true;
  }
}