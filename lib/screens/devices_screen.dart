/// Devices — ONE screen, four doors closed behind it.
///
/// List / Add / Update / Remove were four drawer rows from the era when
/// each was its own question flow. Same merge automations and scenes got:
/// the screen IS the feature. New from the button, edit by tapping a
/// device, remove from its own menu. The hub keeps all four menu actions —
/// they are how VOICE reaches the same capability, and a spoken "add a
/// device" has nothing to point at.
///
/// BOARDS ARE HERE TOO, and they have to be. On the hub, a board's wiring,
/// name and password live behind "Update device → A board" (and the Wi-Fi
/// every board joins behind the same fork, at her request, field test
/// 2026-08-25). Folding four rows into one screen would otherwise have
/// quietly lost that door. They are listed rather than merely reachable,
/// because until now nothing in the app could answer "what boards do I
/// actually have?".
///
/// ONE DATA PATH: the `device_list` frame — real records (id, name, room,
/// description, secure, voice, state, reading, envelope, ha_entity_id) plus
/// the boards. The detail sheet shows all of it, and the row actions are
/// keyed by the hub's own id. (The fixed-width text table is written only
/// to clients that do not render data, so there is nothing to scrape.)
library;


import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../app_state.dart';
import '../widgets/voice_ui.dart';
import '../widgets/layout_cards.dart';
import '../widgets/device_controls.dart';

class _Device {
  final String name;
  final String room;
  final Map<String, dynamic>? record;
  const _Device(this.name, this.room, {this.record});

  /// The hub's own key. A record without one gets no row actions.
  String get id => (record?['id'] ?? '').toString();

  bool get actionable => id.isNotEmpty;

  bool get isOn {
    final st = record?['state'];
    if (st is Map) return st['on'] == true;
    return false; // the legacy table doesn't carry state
  }

  bool get secure => record?['secure'] == true;

  /// A secure device whose state the hub WITHHELD (`state: {}` until this
  /// connection verifies). Not off -- unknown -- and drawn as such.
  bool get stateWithheld {
    final st = record?['state'];
    return secure && st is Map && st.isEmpty;
  }

  /// Whether this device's readings appear on the home screen. Absent on
  /// an older hub, which read as yes — so absent means yes here too.
  bool get onHome => record?['on_home'] != false;

  /// Wired to a board of ours, rather than reached through Home Assistant.
  /// The hub decides this (a device id that starts with a board's slug); the
  /// app does not guess it from the name.
  bool get onEsp => record?['esp'] == true;

  /// "Living room · Power2 · GPIO27" — the board, the control the person
  /// would actually type at a console, and the pin the wire is in.
  ///
  /// This exists because the Power numbers are NOT in pin order. They follow
  /// the order things were added, so a board really can have Power2 on
  /// GPIO27 and Power3 on GPIO26 — and it did, and the only way to find out
  /// was to read a raw template array out of a console (her report,
  /// 2026-08-29: "i set pin 14, 26 and 27 high... 14 doesnt have light").
  String get wiring {
    final r = record;
    if (r == null) return '';
    final parts = <String>[
      if ((r['board_name'] ?? '').toString().isNotEmpty)
        r['board_name'].toString(),
      if ((r['control'] ?? '').toString().isNotEmpty) r['control'].toString(),
      if ((r['pin'] ?? '').toString().isNotEmpty) 'GPIO${r['pin']}',
    ];
    return parts.join(' · ');
  }

  /// Where a non-ESP device actually lives, so the row is never blank about
  /// its own origin.
  String get elsewhere {
    final ha = (record?['ha_entity_id'] ?? '').toString();
    if (ha.isEmpty || ha == 'null') return '';
    return 'Home Assistant · $ha';
  }

  String? get description {
    final d = record?['description']?.toString();
    if (d != null && d.trim().isNotEmpty) return d;
    return null;
  }
}

class DevicesScreen extends StatelessWidget {
  final AppState appState;
  const DevicesScreen({super.key, required this.appState});

  static String _title(String s) =>
      s.isEmpty ? s : s[0].toUpperCase() + s.substring(1);

  static String _prettyRoom(String room) =>
      _title(room.replaceAll('_', ' ').trim());

  static Map<String, List<_Device>> _group(AppState s) {
    final rooms = <String, List<_Device>>{};
    for (final r in s.deviceRecords) {
      final rawRoom = (r['room'] ?? '').toString().trim();
      final room = rawRoom.isEmpty ? 'Devices' : _prettyRoom(rawRoom);
      final name = (r['name'] ?? r['id'] ?? '?').toString();
      (rooms[room] ??= []).add(_Device(name, room, record: r));
    }
    return rooms;
  }

  static IconData _iconFor(String name) {
    final n = name.toLowerCase();
    if (n.contains('lamp') || n.contains('light') || n.contains('bulb')) {
      return Icons.lightbulb_outline;
    }
    if (n.contains('fan')) return Icons.air;
    // WORDS, not letters: "back door", "fireplace" and "stairs" all
    // contain "ac" or "air" and were drawn as air conditioners.
    if (RegExp(r'\b(ac|air|aircon)\b').hasMatch(n)) return Icons.ac_unit;
    if (n.contains('lock') || n.contains('door') || n.contains('gate')) {
      return Icons.lock_outline;
    }
    if (n.contains('tv') || n.contains('screen')) return Icons.tv;
    if (n.contains('plug') || n.contains('socket')) return Icons.power;
    if (n.contains('heater') || n.contains('therm')) return Icons.thermostat;
    if (n.contains('camera')) return Icons.videocam_outlined;
    if (n.contains('speaker') || n.contains('music')) return Icons.speaker;
    if (n.contains('meter') || n.contains('sensor')) return Icons.speed;
    return Icons.devices_other;
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
    final rooms = _group(s);
    final boards = s.boardRecords;
    final layouts = s.layoutRecords;
    final empty = rooms.isEmpty && boards.isEmpty && layouts.isEmpty;

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
          title: Text('Devices',
              style: TextStyle(
                  color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
          actions: [
            IconButton(
              tooltip: 'Refresh',
              icon: Icon(Icons.refresh, color: s.inkDim),
              // NO Navigator.pop first. The old refresh popped the screen
              // and relied on the result frame to push it back, which
              // works only while the list is non-empty and flashes the
              // screen out and in every time. The screen listens; the
              // records arriving redraw it where it stands.
              onPressed: () => s.requestDeviceList(),
            ),
            // ONE WHOLE-ESTATE ACTION LEFT, and it is the only one that
            // genuinely belongs to no single board: changing the Wi-Fi
            // moves EVERY board at once, which is the hub's own stated
            // reason it never fitted under "A board".
            //
            // "Change a board" and "Ask a board how it is" are GONE from
            // here (her ruling, 2026-09-11: "since board now does change
            // its wiring, ask how it is and rename... the three dot top
            // right don't need the change a board option"). Both are on
            // the board's own sheet now, with the slug under your finger.
            // A screen-level menu cannot know which board you mean, so it
            // opened a picker to ask a question the tap had already
            // answered -- the same thing the device cards, the automation
            // rows and the bin all stopped doing when the merge law was
            // applied to them. Voice still reaches both: "change a board"
            // and "ask the kitchen how it is" go to the hub's own flows,
            // which keep their pickers because a spoken request has
            // nothing to point at.
            PopupMenuButton<String>(
              icon: Icon(Icons.more_vert, color: s.inkDim),
              color: s.surface,
              onSelected: (v) {
                if (v == 'wifi') _confirmWifiOrder(context, s);
              },
              itemBuilder: (_) => [
                PopupMenuItem(
                  value: 'wifi',
                  child: Row(children: [
                    Icon(Icons.wifi, size: 16, color: s.inkDim),
                    const SizedBox(width: 8),
                    Text('Wi-Fi every board joins',
                        style: TextStyle(color: s.ink, fontSize: 14)),
                  ]),
                ),
              ],
            ),
          ],
        ),
        // ADD LIVES HERE, not in the drawer. Pressing it starts the hub's
        // existing add ceremony — board, pins, flash, describe — which
        // pushes its own screens over this one and lands back here.
        floatingActionButton: FloatingActionButton.extended(
          backgroundColor: s.accent,
          foregroundColor: s.accentFg,
          onPressed: () => s.addDevice(),
          icon: const Icon(Icons.add, size: 20),
          label: const Text('Add',
              style: TextStyle(fontWeight: FontWeight.w700)),
        ),
        body: SafeArea(
          child: empty
              ? Center(
                  child: Padding(
                    padding: const EdgeInsets.all(32),
                    child: Text(
                      'Nothing registered yet.\n\nTap Add to set up a board '
                      'and everything wired to it.',
                      textAlign: TextAlign.center,
                      style: TextStyle(
                          color: s.inkDim, fontSize: 14, height: 1.5),
                    ),
                  ),
                )
              : VoiceUiScope(
                  state: s,
                  // "Select kitchen light": the detail sheet, exactly as a
                  // tap. The id is the hub's key, matched against the
                  // records on screen now.
                  onSelect: (ctx, id, label) {
                    // A board card, advertised as "board:<slug>".
                    if (id.startsWith('board:')) {
                      final slug = id.substring(6);
                      for (final b in boards) {
                        if ((b['slug'] ?? '').toString() == slug) {
                          _showBoardDetail(ctx, s, b);
                          return;
                        }
                      }
                      return;
                    }
                    for (final list in rooms.values) {
                      for (final d in list) {
                        if (d.id == id) {
                          _showDetail(ctx, s, d);
                          return;
                        }
                      }
                    }
                  },
                  builder: (ctx, scroll) => ListView(
                  controller: scroll,
                  padding: const EdgeInsets.fromLTRB(16, 4, 16, 96),
                  children: [
                    for (final entry in rooms.entries) ...[
                      _sectionHeader(s, Icons.meeting_room_outlined,
                          entry.key, '${entry.value.length}'),
                      GridView.count(
                        shrinkWrap: true,
                        physics: const NeverScrollableScrollPhysics(),
                        crossAxisCount: 2,
                        mainAxisSpacing: 10,
                        crossAxisSpacing: 10,
                        childAspectRatio: 2.2,
                        children: [
                          for (final d in entry.value)
                            _deviceCard(context, s, d),
                        ],
                      ),
                    ],
                    if (boards.isNotEmpty) ...[
                      _sectionHeader(s, Icons.developer_board, 'Boards',
                          '${boards.length}'),
                      for (final b in boards) _boardCard(context, s, b),
                    ],
                    // LAYOUTS, under Boards: every connection in one place --
                    // unfinished, never sent, and what each board is wired to.
                    // Always shown (even empty) so the New that starts one is
                    // reachable.
                    _layoutsHeader(s, layouts.length),
                    if (layouts.isEmpty)
                      Padding(
                        padding: const EdgeInsets.fromLTRB(4, 0, 4, 12),
                        child: Text(
                          'Nothing yet. A layout is a wiring plan: which '
                          'device is on which pin, for one chip type. Every '
                          'board you set up is kept here, and so is any '
                          'design you start and leave unfinished.',
                          style: TextStyle(
                              color: s.inkDim, fontSize: 13, height: 1.45),
                        ),
                      ),
                    ...LayoutCards.build(context, s),
                  ],
                ),
                ),
        ),
      ),
    );
  }

  /// The one sentence that would have saved the migration night, shown at
  /// the one moment it is actionable.
  ///
  /// A board can only be TOLD about a new network over the old one — the
  /// message and the road it travels are the same thing. Change the Wi-Fi
  /// first and every board hops on its own; move or rename the network
  /// first and every board is stranded on air that no longer exists, each
  /// one needing a USB cable. That ordering rule lived only in a document,
  /// which is to say nowhere.
  ///
  /// It MOVED HERE with the action itself. Settings held the Wi-Fi row and
  /// this warning; the row is gone from there (it was a second door to
  /// something Devices now owns) and the warning had to travel with it —
  /// deleting a dialog because its caller moved is how a hard-won sentence
  /// quietly stops being said.
  ///
  /// A dialog rather than a subtitle: subtitles are read once and never
  /// again, and this rule matters precisely on the day you are ABOUT to
  /// tear a network down.
  void _confirmWifiOrder(BuildContext context, AppState s) {
    showDialog<void>(
      context: context,
      builder: (dialog) => AlertDialog(
        backgroundColor: s.surface,
        surfaceTintColor: Colors.transparent,
        title: Text('Order matters',
            style: TextStyle(
                color: s.ink, fontSize: 16, fontWeight: FontWeight.w600)),
        content: Text(
            'Change the Wi-Fi here FIRST, while the boards can still hear '
            'the hub — they can only be told about a new network over the '
            'old one. If the old network is already gone, this still works: '
            'plug each board in by USB and the flow picks it up '
            'automatically.',
            style: TextStyle(color: s.inkDim, fontSize: 13.5, height: 1.4)),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialog).pop(),
            // Every cancel wears the disconnect red (her rule, 2026-08-27).
            child: Text('Cancel', style: TextStyle(color: s.cancelInk)),
          ),
          TextButton(
            onPressed: () {
              Navigator.of(dialog).pop();
              s.changeBoardWifi();
            },
            child: Text('Continue',
                style: TextStyle(
                    color: s.accent, fontWeight: FontWeight.w600)),
          ),
        ],
      ),
    );
  }

  /// The Layouts header, with the New that designs one (from nothing, or
  /// by copying what a board is wired to).
  Widget _layoutsHeader(AppState s, int count) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(4, 16, 4, 8),
      child: Row(
        children: [
          Icon(Icons.dashboard_customize_outlined,
              size: 15, color: s.inkFaint),
          const SizedBox(width: 6),
          Text('LAYOUTS',
              style: TextStyle(
                  fontSize: 11,
                  letterSpacing: 0.09,
                  fontWeight: FontWeight.w600,
                  color: s.inkFaint)),
          const SizedBox(width: 8),
          Expanded(child: Divider(color: s.border, height: 1)),
          const SizedBox(width: 8),
          Text('$count', style: TextStyle(fontSize: 11, color: s.inkFaint)),
          const SizedBox(width: 4),
          TextButton.icon(
            onPressed: s.isReady ? () => s.newLayout() : null,
            icon: Icon(Icons.add, size: 16, color: s.accent),
            label: Text('New',
                style: TextStyle(
                    color: s.accent,
                    fontSize: 13,
                    fontWeight: FontWeight.w600)),
            style: TextButton.styleFrom(
                visualDensity: VisualDensity.compact,
                padding: const EdgeInsets.symmetric(horizontal: 6)),
          ),
        ],
      ),
    );
  }

  Widget _sectionHeader(
      AppState s, IconData icon, String label, String count) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(4, 16, 4, 8),
      child: Row(
        children: [
          Icon(icon, size: 15, color: s.inkFaint),
          const SizedBox(width: 6),
          Text(
            label.toUpperCase(),
            style: TextStyle(
              fontSize: 11,
              letterSpacing: 0.09,
              fontWeight: FontWeight.w600,
              color: s.inkFaint,
            ),
          ),
          const SizedBox(width: 8),
          Expanded(child: Divider(color: s.border, height: 1)),
          const SizedBox(width: 8),
          Text(count, style: TextStyle(fontSize: 11, color: s.inkFaint)),
        ],
      ),
    );
  }

  /// The LIST shows only what identifies a device — icon + name (and a lock
  /// mark for secure ones). Everything else is behind the tap.
  Widget _deviceCard(BuildContext context, AppState s, _Device d) {
    final on = d.isOn;
    return InkWell(
      onTap: () => _showDetail(context, s, d),
      borderRadius: BorderRadius.circular(12),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        decoration: BoxDecoration(
          color: s.surface,
          border: Border.all(color: on ? s.accent : s.border),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Row(
          children: [
            Icon(_iconFor(d.name), size: 20, color: on ? s.accent : s.inkDim),
            const SizedBox(width: 10),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Row(
                    children: [
                      Flexible(
                        child: Text(
                          _title(d.name),
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            fontSize: 14,
                            fontWeight: FontWeight.w600,
                            color: s.ink,
                            height: 1.2,
                          ),
                        ),
                      ),
                      // THE TAG. It separates a thing wired to a board of
                      // yours from a thing reached through Home Assistant --
                      // two devices that look identical on a list and behave
                      // nothing alike when something goes wrong.
                      if (d.onEsp) ...[
                        const SizedBox(width: 7),
                        Container(
                          padding: const EdgeInsets.symmetric(
                              horizontal: 5, vertical: 1),
                          decoration: BoxDecoration(
                            border: Border.all(color: s.accent),
                            borderRadius: BorderRadius.circular(4),
                          ),
                          child: Text('ESP',
                              style: TextStyle(
                                  fontSize: 9,
                                  height: 1.3,
                                  letterSpacing: 0.5,
                                  fontWeight: FontWeight.w700,
                                  color: s.accent)),
                        ),
                      ],
                    ],
                  ),
                  if (d.wiring.isNotEmpty || d.elsewhere.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(top: 3),
                      child: Text(
                        d.wiring.isNotEmpty ? d.wiring : d.elsewhere,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                            fontSize: 11.5,
                            color: s.inkFaint,
                            height: 1.2),
                      ),
                    ),
                ],
              ),
            ),
            if (d.secure)
              const Padding(
                padding: EdgeInsets.only(right: 4),
                child: Icon(Icons.shield_outlined,
                    size: 14, color: Color(0xFFD29922)),
              ),
            Icon(Icons.chevron_right, size: 16, color: s.inkFaint),
          ],
        ),
      ),
    );
  }

  /// A board is a THING YOU HAVE, not a thing you drive — so it gets a row
  /// that says what it is and what is wired to it, and sends you to the
  /// hub's own board flow rather than pretending to be a device.
  Widget _boardCard(BuildContext context, AppState s, Map<String, dynamic> b) {
    final name = (b['name'] ?? b['slug'] ?? '?').toString();
    final chip = (b['board'] ?? '').toString();
    final owned = (b['devices'] as List? ?? const []).length;
    // TAP THE BOARD FOR EVERYTHING ABOUT IT (her ruling, 2026-09-11): the
    // sheet shows what it is, its parts and their drivers, the devices
    // wired to it, and -- behind the PIN -- its passwords. One door.
    return InkWell(
      borderRadius: BorderRadius.circular(12),
      onTap: () => _showBoardDetail(context, s, b),
      child: Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(13),
      decoration: BoxDecoration(
        color: s.surface,
        border: Border.all(color: s.border),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        children: [
          Icon(Icons.developer_board, size: 19, color: s.inkDim),
          const SizedBox(width: 11),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(_title(name),
                    style: TextStyle(
                        fontSize: 14,
                        fontWeight: FontWeight.w600,
                        color: s.ink)),
                const SizedBox(height: 2),
                Text(
                  [
                    if (chip.isNotEmpty) chip,
                    owned == 1 ? '1 device' : '$owned devices',
                  ].join(' · '),
                  style: TextStyle(fontSize: 12, color: s.inkDim),
                ),
                // WHAT IS ON EACH PIN, on the board itself.
                //
                // Pins only: no firmware version, no broker status (her
                // call, 2026-08-29). Those answer a question nobody
                // standing here is asking, and the one that IS being asked
                // -- "which Power drives which wire?" -- had no answer
                // anywhere in the app.
                ...(() {
                  final wiring = [
                    for (final w in (b['wiring'] as List? ?? const []))
                      if (w is Map<String, dynamic>) w
                  ];
                  if (wiring.isEmpty) return <Widget>[];
                  return [
                    const SizedBox(height: 10),
                    Container(height: 1, color: s.border),
                    const SizedBox(height: 8),
                    for (final w in wiring)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 5),
                        child: Row(
                          children: [
                            SizedBox(
                              width: 62,
                              child: Text((w['control'] ?? '').toString(),
                                  style: TextStyle(
                                      fontSize: 12,
                                      fontWeight: FontWeight.w600,
                                      color: s.inkDim)),
                            ),
                            SizedBox(
                              width: 62,
                              child: Text('GPIO${w['pin']}',
                                  style: TextStyle(
                                      fontSize: 12,
                                      color: s.ink,
                                      fontFeatures: const [
                                        FontFeature.tabularFigures()
                                      ])),
                            ),
                            Expanded(
                              child: Text((w['name'] ?? '').toString(),
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: TextStyle(
                                      fontSize: 12, color: s.inkDim)),
                            ),
                          ],
                        ),
                      ),
                    const SizedBox(height: 4),
                    Text(
                      'Power numbers follow the order things were added, '
                      'not the pin number.',
                      style: TextStyle(
                          fontSize: 11, color: s.inkFaint, height: 1.35),
                    ),
                  ];
                })(),
              ],
            ),
          ),
          // THE BIN, ON THE BOARD IT RETIRES. Her ruling, twice: the remove
          // icon goes beside the board, not in the screen's three dots. A
          // menu at the top of a list cannot know which board you mean, so
          // it opened a picker to ask a question the tap had already
          // answered -- the same thing the device cards and the automation
          // rows stopped doing when the merge law was applied to them.
          //
          // No confirmation dialog here on purpose: the hub's own
          // retirement lists every device, entry and file it is about to
          // delete BEFORE deleting any of them, and a second "are you
          // sure?" in front of a screen built to be read is how people
          // learn to tap through both.
          IconButton(
            tooltip: 'Retire this board',
            icon: Icon(Icons.delete_outline, size: 19, color: s.cancelInk),
            onPressed: () =>
                s.retireOneBoard((b['slug'] ?? '').toString()),
          ),
          Icon(Icons.chevron_right, size: 16, color: s.inkFaint),
        ],
      ),
      ),
    );
  }

  /// EVERYTHING ABOUT ONE BOARD, on a tap (2026-09-11: "when boards are
  /// clicked in devices, info about them is shown, its credentials gated
  /// by pin, its drivers used and the info of devices connected").
  ///
  /// THE SECOND PASS IS THE DESIGN ONE ("the design is bad"). The first
  /// draft put everything at one weight -- part rows, device rows and a
  /// raw reading dump all in the same small grey text, under labels the
  /// eye skated over, above three chips of three different widths that
  /// wrapped so "Rename" sat alone on a line. It listed the right things
  /// and looked like a log file.
  ///
  /// What changed. Each section is a CARD on the sheet's ground, so the
  /// three groups read as three groups without a single divider. A part
  /// carries its pins as small monospace chips ("GPIO33 · Relay") instead
  /// of a two-column table nested inside a list. A device's reading is cut
  /// to its first clause -- the power meter was printing thirteen
  /// comma-separated fields, which is the device's own card's job, not
  /// this one's. And the acts are four full-width rows of equal weight,
  /// the way Settings draws acts, with the gated one carrying the key and
  /// the word PIN rather than a paragraph of grey caption under a pill.
  void _showBoardDetail(
      BuildContext context, AppState s, Map<String, dynamic> b) {
    final slug = (b['slug'] ?? '').toString();
    final name = (b['name'] ?? slug).toString();
    final chip = (b['board'] ?? '').toString();
    final topic = (b['topic'] ?? '').toString();
    final psram = b['psram'];
    final parts = [
      for (final p in (b['parts'] as List? ?? const []))
        if (p is Map<String, dynamic>) p
    ];
    final owned = [
      for (final id in (b['devices'] as List? ?? const [])) id.toString()
    ];
    Map<String, dynamic>? recordOf(String id) {
      for (final r in s.deviceRecords) {
        if ((r['id'] ?? '').toString() == id) return r;
      }
      return null;
    }

    final facts = <String>[
      if (chip.isNotEmpty) chip,
      if (psram == true) 'PSRAM' else if (psram == false) 'no PSRAM',
      if (topic.isNotEmpty) topic,
    ];

    showModalBottomSheet(
      context: context,
      backgroundColor: s.bg,
      isScrollControlled: true,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (sheetCtx) => SafeArea(
        child: ConstrainedBox(
          constraints: BoxConstraints(
              maxHeight: MediaQuery.of(sheetCtx).size.height * 0.88),
          child: SingleChildScrollView(
            padding: const EdgeInsets.fromLTRB(16, 10, 16, 20),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                // The grab handle every bottom sheet in this app should
                // have and this one did not: without it the sheet reads
                // as a screen that arrived from the wrong direction.
                Center(
                  child: Container(
                    width: 36,
                    height: 4,
                    margin: const EdgeInsets.only(bottom: 16),
                    decoration: BoxDecoration(
                      color: s.border,
                      borderRadius: BorderRadius.circular(2),
                    ),
                  ),
                ),
                Row(
                  children: [
                    Container(
                      width: 42,
                      height: 42,
                      decoration: BoxDecoration(
                        color: s.accent.withValues(alpha: 0.12),
                        borderRadius: BorderRadius.circular(12),
                      ),
                      child: Icon(Icons.developer_board,
                          size: 22, color: s.accent),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(_title(name),
                              style: TextStyle(
                                  fontSize: 20,
                                  fontWeight: FontWeight.w700,
                                  color: s.ink)),
                          if (facts.isNotEmpty)
                            Padding(
                              padding: const EdgeInsets.only(top: 2),
                              child: Text(facts.join(' · '),
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: TextStyle(
                                      fontSize: 12.5, color: s.inkFaint)),
                            ),
                        ],
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 18),

                // ── WRITE ON ITS DISPLAY ──
                if (b['display'] is Map &&
                    ((b['display'] as Map)['device'] ?? '').toString().isNotEmpty) ...[
                  Container(
                    padding: const EdgeInsets.all(14),
                    decoration: BoxDecoration(
                      color: s.surface,
                      border: Border.all(color: s.border),
                      borderRadius: BorderRadius.circular(14),
                    ),
                    child: DisplayWriter(
                      s: s,
                      deviceId: (b['display'] as Map)['device'].toString(),
                      rows: ((b['display'] as Map)['rows'] as num?)?.toInt() ?? 2,
                      cols: ((b['display'] as Map)['cols'] as num?)?.toInt() ?? 16,
                    ),
                  ),
                  const SizedBox(height: 14),
                ],

                // ── WHAT IS WIRED TO IT ──
                _sheetCard(
                  s,
                  'Wired to it',
                  '${parts.length}',
                  parts.isEmpty
                      ? [_sheetEmpty(s, 'Nothing is wired to it yet.')]
                      : [
                          for (var i = 0; i < parts.length; i++)
                            _partRow(s, parts[i], last: i == parts.length - 1),
                        ],
                ),
                const SizedBox(height: 12),

                // ── WHAT IS REGISTERED ON IT ──
                _sheetCard(
                  s,
                  'Devices on it',
                  '${owned.length}',
                  owned.isEmpty
                      ? [
                          _sheetEmpty(
                              s,
                              parts.isEmpty
                                  ? 'None registered.'
                                  : 'Wired, but not registered yet — '
                                      'Notifications offers "Register them".')
                        ]
                      : [
                          for (var i = 0; i < owned.length; i++)
                            _ownedRow(s, owned[i], recordOf(owned[i]),
                                last: i == owned.length - 1),
                        ],
                ),
                const SizedBox(height: 12),

                // ── THE ACTS, each naming THIS board ──
                Container(
                  decoration: BoxDecoration(
                    color: s.surface,
                    border: Border.all(color: s.border),
                    borderRadius: BorderRadius.circular(14),
                  ),
                  // Clipped, so a tap's ripple stops at the rounded corner
                  // instead of painting a square one over the border.
                  child: ClipRRect(
                    borderRadius: BorderRadius.circular(13),
                    child: Column(
                    children: [
                      _actRow(
                        s,
                        icon: Icons.key_outlined,
                        title: 'Show its passwords',
                        subtitle: 'Web page (user admin) and broker',
                        gated: true,
                        onTap: s.isReady
                            ? () {
                                Navigator.pop(sheetCtx);
                                s.boardPasswords(slug);
                              }
                            : null,
                      ),
                      _actDivider(s),
                      _actRow(
                        s,
                        icon: Icons.monitor_heart_outlined,
                        title: 'Ask it how it is',
                        subtitle: 'Firmware, drivers, I²C scan, live readings',
                        onTap: s.isReady
                            ? () {
                                Navigator.pop(sheetCtx);
                                s.boardReportFor(slug);
                              }
                            : null,
                      ),
                      _actDivider(s),
                      _actRow(
                        s,
                        icon: Icons.cable,
                        title: 'Change its wiring',
                        subtitle: 'The pin screen, then it restarts into it',
                        gated: true,
                        onTap: s.isReady
                            ? () {
                                Navigator.pop(sheetCtx);
                                s.editBoard(slug);
                              }
                            : null,
                      ),
                      _actDivider(s),
                      _actRow(
                        s,
                        icon: Icons.drive_file_rename_outline,
                        title: 'Rename it',
                        subtitle: 'What this hub calls it',
                        onTap: s.isReady
                            ? () {
                                Navigator.pop(sheetCtx);
                                s.renameBoard(slug);
                              }
                            : null,
                      ),
                      _actDivider(s),
                      _actRow(
                        s,
                        icon: Icons.memory,
                        title: 'Change its board type',
                        subtitle: 'If it was chosen wrong',
                        gated: true,
                        onTap: s.isReady
                            ? () {
                                Navigator.pop(sheetCtx);
                                s.changeBoardType(slug);
                              }
                            : null,
                      ),
                    ],
                  ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  /// A section of the board sheet: a quiet header, then a bordered card.
  /// The count sits in the header rather than in the title, so the titles
  /// line up down the sheet whatever the numbers are.
  Widget _sheetCard(
      AppState s, String title, String count, List<Widget> children) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Padding(
          padding: const EdgeInsets.only(left: 4, bottom: 7),
          child: Row(
            children: [
              Text(title.toUpperCase(),
                  style: TextStyle(
                      fontSize: 10.5,
                      letterSpacing: 0.9,
                      fontWeight: FontWeight.w700,
                      color: s.inkFaint)),
              const SizedBox(width: 7),
              Text(count,
                  style: TextStyle(
                      fontSize: 10.5,
                      fontWeight: FontWeight.w700,
                      color: s.inkFaint)),
            ],
          ),
        ),
        Container(
          width: double.infinity,
          decoration: BoxDecoration(
            color: s.surface,
            border: Border.all(color: s.border),
            borderRadius: BorderRadius.circular(14),
          ),
          child: Column(children: children),
        ),
      ],
    );
  }

  Widget _sheetEmpty(AppState s, String text) => Padding(
        padding: const EdgeInsets.all(14),
        child: Text(text,
            style: TextStyle(fontSize: 13, color: s.inkFaint, height: 1.4)),
      );

  /// One part: what it is, then its pins as chips. The chip is the unit a
  /// person actually looks for -- "which pin is the card reader on?" -- and
  /// as a chip it survives a long slot label instead of being squeezed by
  /// one.
  Widget _partRow(AppState s, Map<String, dynamic> p, {bool last = false}) {
    final name = (p['name'] ?? '').toString();
    final part = (p['part'] ?? '').toString();
    final needs = [
      for (final n in (p['needs'] as List? ?? const [])) n.toString()
    ];
    final pins = [
      for (final r in (p['pins'] as List? ?? const []))
        if (r is Map<String, dynamic>) r
    ];
    return Container(
      padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
      decoration: BoxDecoration(
        border: last
            ? null
            : Border(bottom: BorderSide(color: s.border.withValues(alpha: 0.6))),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(_title(name),
              style: TextStyle(
                  fontSize: 14, fontWeight: FontWeight.w600, color: s.ink)),
          if (part.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 1),
              child: Text(part,
                  style: TextStyle(fontSize: 12.5, color: s.inkDim)),
            ),
          if (pins.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Wrap(
                spacing: 6,
                runSpacing: 6,
                children: [
                  for (final r in pins)
                    _pinChip(s, 'GPIO${r['gpio']}',
                        (r['component'] ?? '').toString()),
                ],
              ),
            ),
          if (needs.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(Icons.memory, size: 13, color: s.inkFaint),
                  const SizedBox(width: 5),
                  Expanded(
                    child: Text('Needs the Omniscience build: ${needs.join(', ')}',
                        style: TextStyle(
                            fontSize: 11.5, color: s.inkFaint, height: 1.3)),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }

  /// "GPIO33 · Relay" — the pin in tabular figures so a column of them
  /// lines up, the Tasmota component beside it in the quieter ink.
  Widget _pinChip(AppState s, String pin, String component) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
          color: s.bg,
          border: Border.all(color: s.border),
          borderRadius: BorderRadius.circular(7),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(pin,
                style: TextStyle(
                    fontSize: 11.5,
                    fontWeight: FontWeight.w600,
                    color: s.ink,
                    fontFeatures: const [FontFeature.tabularFigures()])),
            if (component.isNotEmpty) ...[
              const SizedBox(width: 5),
              Text(component,
                  style: TextStyle(fontSize: 11.5, color: s.inkDim)),
            ],
          ],
        ),
      );

  /// One registered device on this board, from the records already in
  /// hand. The reading is cut to its FIRST clause: the power meter reports
  /// thirteen fields and printed all of them here, which is the device's
  /// own card's job and made this list unreadable.
  Widget _ownedRow(AppState s, String id, Map<String, dynamic>? r,
      {bool last = false}) {
    final name = (r?['name'] ?? id.split('.').last).toString();
    final room = (r?['room'] ?? '').toString();
    final control = (r?['control'] ?? '').toString();
    final pin = r?['pin'];
    final secure = r?['secure'] == true;
    var reading = (r?['reading'] ?? '').toString();
    if (reading == 'null') reading = '';
    if (reading.contains(',')) reading = reading.split(',').first.trim();
    if (reading.length > 24) reading = '${reading.substring(0, 24)}…';
    final bits = <String>[
      if (room.isNotEmpty) _prettyRoom(room),
      if (control.isNotEmpty) control + (pin != null ? ' · GPIO$pin' : ''),
      if (r == null) 'not in the list — refresh',
    ];
    return Container(
      padding: const EdgeInsets.fromLTRB(14, 11, 14, 11),
      decoration: BoxDecoration(
        border: last
            ? null
            : Border(bottom: BorderSide(color: s.border.withValues(alpha: 0.6))),
      ),
      child: Row(
        children: [
          Icon(_iconFor(name), size: 17, color: s.inkDim),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(_title(name),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                        fontSize: 14,
                        fontWeight: FontWeight.w600,
                        color: s.ink)),
                if (bits.isNotEmpty)
                  Text(bits.join(' · '),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(fontSize: 12, color: s.inkDim)),
              ],
            ),
          ),
          if (secure)
            const Padding(
              padding: EdgeInsets.only(left: 6),
              child: Icon(Icons.shield_outlined,
                  size: 14, color: Color(0xFFD29922)),
            ),
          if (reading.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(left: 8),
              child: Text(reading,
                  style: TextStyle(
                      fontSize: 12.5,
                      fontWeight: FontWeight.w600,
                      color: s.inkDim)),
            ),
        ],
      ),
    );
  }

  Widget _actDivider(AppState s) =>
      Divider(height: 1, thickness: 1, color: s.border.withValues(alpha: 0.6));

  /// One act on the board, drawn the way Settings draws a row: icon,
  /// title, one line of what it does, and a chevron. Equal weight for all
  /// four, so nothing wraps and nothing shouts.
  Widget _actRow(
    AppState s, {
    required IconData icon,
    required String title,
    required String subtitle,
    required VoidCallback? onTap,
    bool gated = false,
  }) {
    final on = onTap != null;
    return InkWell(
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 13),
        child: Row(
          children: [
            Icon(icon, size: 18, color: on ? s.accent : s.inkFaint),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Flexible(
                        child: Text(title,
                            style: TextStyle(
                                fontSize: 14.5,
                                fontWeight: FontWeight.w600,
                                color: on ? s.ink : s.inkFaint)),
                      ),
                      if (gated) ...[
                        const SizedBox(width: 7),
                        Container(
                          padding: const EdgeInsets.symmetric(
                              horizontal: 5, vertical: 1),
                          decoration: BoxDecoration(
                            border: Border.all(color: s.border),
                            borderRadius: BorderRadius.circular(4),
                          ),
                          child: Text('PIN',
                              style: TextStyle(
                                  fontSize: 9.5,
                                  letterSpacing: 0.4,
                                  fontWeight: FontWeight.w700,
                                  color: s.inkFaint)),
                        ),
                      ],
                    ],
                  ),
                  const SizedBox(height: 1),
                  Text(subtitle,
                      style: TextStyle(fontSize: 11.5, color: s.inkFaint)),
                ],
              ),
            ),
            Icon(Icons.chevron_right, size: 16, color: s.inkFaint),
          ],
        ),
      ),
    );
  }

  Widget _detailRow(AppState s, String label, String value) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(label.toUpperCase(),
              style: TextStyle(
                  fontSize: 10.5,
                  letterSpacing: 0.09,
                  fontWeight: FontWeight.w600,
                  color: s.inkFaint)),
          const SizedBox(height: 2),
          Text(value,
              style: TextStyle(fontSize: 14, height: 1.4, color: s.ink)),
        ],
      ),
    );
  }

  /// Tap → the full information for one device, from its record (state
  /// reading, description, behaviour envelope, security, matching mode, HA
  /// entity).
  ///
  /// AND THE TWO ACTIONS, at the bottom where you have just read what the
  /// thing is. Putting Edit and Remove on the card itself would mean
  /// deciding to delete something from a tile showing only its name.
  void _showDetail(BuildContext context, AppState s, _Device d) {
    final r = d.record;
    final rows = <(String, String)>[];
    if (r != null) {
      final reading = r['reading']?.toString();
      if (d.stateWithheld) {
        rows.add(('State', 'verify to see — the hub shows a secure '
            'device\'s state only after a PIN or voice check on this phone'));
      } else if (reading != null && reading.isNotEmpty) {
        rows.add(('State', reading));
      }
      if (d.description != null) rows.add(('Description', d.description!));
      final envelope = r['envelope']?.toString();
      if (envelope != null && envelope.isNotEmpty) {
        rows.add(('Behaves as', envelope));
      }
      rows.add(('Voice matching',
          r['voice'] == 'full' ? 'full — paraphrases work' : 'by exact name only'));
      // THE WIRING, above the storage keys: it is the thing you need when
      // something is not doing what you told it, and it was nowhere in the
      // app until now.
      if (d.wiring.isNotEmpty) rows.add(('Wired as', d.wiring));
      final ha = r['ha_entity_id']?.toString();
      if (ha != null && ha.isNotEmpty && ha != 'null') {
        rows.add(('Home Assistant', ha));
      }
      final id = r['id']?.toString();
      if (id != null && id.isNotEmpty) rows.add(('Device ID', id));
    }

    showModalBottomSheet(
      context: context,
      backgroundColor: s.surface,
      isScrollControlled: true,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (sheetCtx) => SafeArea(
        child: ConstrainedBox(
          constraints: BoxConstraints(
              maxHeight: MediaQuery.of(sheetCtx).size.height * 0.88),
          child: SingleChildScrollView(
          padding: EdgeInsets.fromLTRB(
              20, 16, 20, 24 + MediaQuery.of(sheetCtx).viewInsets.bottom),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Icon(_iconFor(d.name),
                      size: 26, color: d.isOn ? s.accent : s.inkDim),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Text(
                      _title(d.name),
                      style: TextStyle(
                        fontSize: 19,
                        fontWeight: FontWeight.w600,
                        color: s.ink,
                      ),
                    ),
                  ),
                  if (d.secure)
                    const Padding(
                      padding: EdgeInsets.only(right: 8),
                      child: Icon(Icons.shield_outlined,
                          size: 18, color: Color(0xFFD29922)),
                    ),
                  if (d.stateWithheld)
                    Text('state hidden',
                        style: TextStyle(fontSize: 11, color: s.inkFaint))
                  else if (d.isOn)
                    Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 9, vertical: 3),
                      decoration: BoxDecoration(
                        color: s.accent.withValues(alpha: 0.15),
                        border: Border.all(color: s.accent),
                        borderRadius: BorderRadius.circular(20),
                      ),
                      child: Text('ON',
                          style: TextStyle(
                              fontSize: 11,
                              fontWeight: FontWeight.w700,
                              color: s.accent)),
                    ),
                ],
              ),
              const SizedBox(height: 6),
              Row(
                children: [
                  Icon(Icons.meeting_room_outlined,
                      size: 14, color: s.inkFaint),
                  const SizedBox(width: 6),
                  Text(d.room, style: TextStyle(fontSize: 13, color: s.inkDim)),
                ],
              ),
              const SizedBox(height: 14),
              Divider(color: s.border, height: 1),
              const SizedBox(height: 14),
              for (final row in rows) _detailRow(s, row.$1, row.$2),
              // A CAMERA'S ONE ACT IS TO BE LOOKED AT. The hub gates the
              // view as a secure device when the camera was registered as
              // one (the default), so this button may be answered with a
              // PIN or a voice check before the feed opens.
              if (d.actionable && d.record != null && AppState.isCamera(d.record!)) ...[
                const SizedBox(height: 14),
                FilledButton.icon(
                  onPressed: s.isReady
                      ? () {
                          Navigator.pop(sheetCtx);
                          s.viewCamera(d.id);
                        }
                      : null,
                  style: FilledButton.styleFrom(
                    backgroundColor: s.accent,
                    padding: const EdgeInsets.symmetric(vertical: 13),
                  ),
                  icon: const Icon(Icons.videocam_outlined, size: 18),
                  label: const Text('View feed',
                      style: TextStyle(fontWeight: FontWeight.w700)),
                ),
              ],
              // ON THE HOME SCREEN, OR NOT — a tap, with no PIN.
              //
              // Not inside Edit, deliberately. That flow is gated because
              // everything in it renames a thing, moves it between rooms
              // or marks it security-critical; this changes a preference
              // about your own screen and nothing else. Her ruling,
              // 2026-09-01: "no pin to add the device to the home screen,
              // same with taking it off."
              //
              // Offered for EVERY device, not just actionable ones — a
              // sensor is precisely the thing you want on the home
              // screen, and it is the one that can never be tapped on.
              if (d.actionable) ...[
                SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  dense: true,
                  value: d.onHome,
                  activeThumbColor: s.accent,
                  title: Text('Show on the home screen',
                      style: TextStyle(color: s.ink, fontSize: 14)),
                  // "IT", NOT "ITS READINGS". The section carries switches
                  // as well now, so promising readings on a bulb was a
                  // control that could not keep its word.
                  subtitle: Text(
                      d.onHome
                          ? 'It appears on the home screen.'
                          : 'Hidden from home. It still works and still '
                              'answers.',
                      style: TextStyle(color: s.inkFaint, fontSize: 11.5,
                          height: 1.35)),
                  // NOT WHILE DISCONNECTED: toggleOnHome flips the switch
                  // before asking the hub, and with no hub to ask the
                  // flipped value stayed on screen, never sent.
                  onChanged: s.isReady
                      ? (_) {
                          Navigator.pop(sheetCtx);
                          s.toggleOnHome(d.id);
                        }
                      : null,
                ),
              ],
              // What it draws, learned from the main meter's jumps.
              if (d.actionable && d.record?['draw'] is Map)
                _drawRow(sheetCtx, s, d),
              // A meter: whether it is the main meter.
              if (d.actionable && d.record?['meter'] is Map)
                _meterRow(context, sheetCtx, s, d),
              // An infrared receiver: learning a remote lives here.
              if (d.record?['ir_receiver'] == true) ...[
                const SizedBox(height: 8),
                SizedBox(
                  width: double.infinity,
                  child: FilledButton.icon(
                    onPressed: s.isReady
                        ? () {
                            Navigator.pop(sheetCtx);
                            s.sendMenuAction('learn_ir');
                          }
                        : null,
                    style: FilledButton.styleFrom(
                      backgroundColor: s.accent,
                      foregroundColor: s.accentFg,
                      padding: const EdgeInsets.symmetric(vertical: 12),
                    ),
                    icon: const Icon(Icons.settings_remote_outlined, size: 17),
                    label: const Text('Learn a remote'),
                  ),
                ),
                const SizedBox(height: 8),
              ],
              if (d.actionable) ...[
                const SizedBox(height: 4),
                Row(
                  children: [
                    Expanded(
                      child: OutlinedButton.icon(
                        onPressed: s.isReady
                            ? () {
                                Navigator.pop(sheetCtx);
                                s.editDevice(d.id);
                              }
                            : null,
                        style: OutlinedButton.styleFrom(
                          foregroundColor: s.accent,
                          side: BorderSide(color: s.accent),
                          padding: const EdgeInsets.symmetric(vertical: 12),
                        ),
                        icon: const Icon(Icons.edit_outlined, size: 17),
                        label: const Text('Edit'),
                      ),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: OutlinedButton.icon(
                        // Every destructive control wears the disconnect
                        // red (her rule, 2026-08-27).
                        style: s.cancelButtonStyle(),
                        onPressed: s.isReady
                            ? () async {
                                final yes = await _confirmRemove(context, s, d);
                                if (yes != true || !sheetCtx.mounted) return;
                                // Pop THE SHEET, not whatever the hub pushed
                                // over it while the dialog was up.
                                final sheet = ModalRoute.of(sheetCtx);
                                if (sheet != null && sheet.isCurrent) {
                                  Navigator.pop(sheetCtx);
                                }
                                s.removeDevice(d.id);
                              }
                            : null,
                        icon: const Icon(Icons.delete_outline, size: 17),
                        label: const Text('Remove'),
                      ),
                    ),
                  ],
                ),
              ],
            ],
          ),
          ),
        ),
      ),
    );
  }

  /// "Uses about 2000 W (from 5 switches)": learned from the jumps in the
  /// main meter's reading when it switched.
  Widget _drawRow(BuildContext sheetCtx, AppState s, _Device d) {
    final draw = d.record!['draw'] as Map;
    final value = draw['value'];
    final unit = (draw['unit'] ?? '').toString();
    final n = draw['samples'];
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        children: [
          Icon(Icons.bolt, size: 18, color: s.inkFaint),
          const SizedBox(width: 12),
          Expanded(
            child: Text('Uses about $value $unit (from $n switches)',
                style: TextStyle(color: s.ink, fontSize: 14)),
          ),
          TextButton(
            onPressed: s.isReady
                ? () {
                    Navigator.pop(sheetCtx);
                    s.forgetDraw(d.id);
                  }
                : null,
            child: Text('Forget', style: TextStyle(color: s.accent)),
          ),
        ],
      ),
    );
  }

  /// A meter: whether its jumps teach the hub what each device draws.
  Widget _meterRow(BuildContext context, BuildContext sheetCtx, AppState s,
      _Device d) {
    final meter = d.record!['meter'] as Map;
    final isMain = meter['main'] == true;
    final keys = <Map>[
      for (final k in (meter['keys'] as List? ?? const []))
        if (k is Map) k
    ];
    final String line;
    if (isMain) {
      line = 'This is the main meter. Its jumps show what each device draws.';
    } else if (s.mainMeter == null) {
      line = 'No main meter is chosen yet.';
    } else {
      line = 'Another meter is the main meter.';
    }
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        children: [
          Icon(Icons.speed, size: 18, color: isMain ? s.accent : s.inkFaint),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Main meter',
                    style: TextStyle(color: s.ink, fontSize: 14)),
                Text(line,
                    style: TextStyle(
                        color: s.inkFaint, fontSize: 11.5, height: 1.35)),
              ],
            ),
          ),
          TextButton(
            onPressed: !s.isReady || keys.isEmpty
                ? null
                : isMain
                    ? () {
                        Navigator.pop(sheetCtx);
                        s.clearMainMeter();
                      }
                    : () async {
                        final pick = await _pickMeterReading(context, s, keys);
                        if (pick == null || !sheetCtx.mounted) return;
                        Navigator.pop(sheetCtx);
                        s.setMainMeter(d.id, pick.$1, pick.$2);
                      },
            child: Text(isMain ? 'Stop using' : 'Use as main meter',
                style: TextStyle(color: s.accent)),
          ),
        ],
      ),
    );
  }

  /// Which reading is the main meter, and whether it is power or current:
  /// asked only when there is a choice. Returns (key, measures).
  Future<(String, String)?> _pickMeterReading(
      BuildContext context, AppState s, List<Map> keys) async {
    final options = <(String, String, String)>[];
    for (final k in keys) {
      final key = (k['key'] ?? '').toString();
      final words = _title((k['words'] ?? key).toString());
      final measures = (k['measures'] ?? '').toString();
      if (measures == 'power' || measures == 'current') {
        options.add((key, measures, '$words ($measures)'));
      }
    }
    if (options.isEmpty) return null;
    if (options.length == 1) return (options.first.$1, options.first.$2);
    if (!context.mounted) return null;
    return showModalBottomSheet<(String, String)>(
      context: context,
      backgroundColor: s.surface,
      builder: (ctx) => SafeArea(
        child: ListView(shrinkWrap: true, children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
            child: Text('Which reading is the main meter?',
                style: TextStyle(
                    color: s.ink, fontSize: 15, fontWeight: FontWeight.w600)),
          ),
          for (final o in options)
            ListTile(
              title: Text(o.$3, style: TextStyle(color: s.ink)),
              onTap: () => Navigator.pop(ctx, (o.$1, o.$2)),
            ),
        ]),
      ),
    );
  }

  /// Removal is FOUR layers on the hub — the registry, the board's plan,
  /// the chip itself, and possibly the Home Assistant set-up — and it asks
  /// its own questions along the way when a decision is needed. This
  /// confirmation is only the "did you mean this one", said before any of
  /// that starts.
  Future<bool?> _confirmRemove(
      BuildContext context, AppState s, _Device d) {
    return showDialog<bool>(
      context: context,
      builder: (dctx) => AlertDialog(
        backgroundColor: s.surface,
        surfaceTintColor: Colors.transparent,
        title: Text('Remove ${_title(d.name)}?',
            style: TextStyle(color: s.ink, fontSize: 17)),
        content: Text(
            'It is unregistered here and stripped from its board, so it '
            'stops running. Any scene using it keeps working, with this '
            'device skipped. The hub will ask before touching anything in '
            'Home Assistant.',
            style: TextStyle(color: s.inkDim, fontSize: 13.5, height: 1.4)),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dctx, false),
            child: Text('Cancel', style: TextStyle(color: s.cancelInk)),
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
  }
}