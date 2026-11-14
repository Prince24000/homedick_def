/// Everything the house can MEASURE, on one screen.
///
/// This replaces two things that were each half an answer. The home screen
/// carried a "House power" dropdown over a grid of three metrics that the
/// phone computed for itself -- numbers about a house, assembled by something
/// that had never asked the house. And Settings carried a "Readings &
/// statistics" row, which ran the flow and then printed its findings into the
/// scrolling server feed, where they slid past like log lines.
///
/// The rule that shapes what you see here: a card shows what a sensor SAYS,
/// and says plainly when it is saying nothing. A sensor Home Assistant has
/// not heard from reads "not reporting" rather than 0 -- a dead power meter
/// showing zero watts is indistinguishable from a quiet house, and that is
/// exactly the reading you must not invent. A sensor with a live value but no
/// numeric history shows its present alone, with no 24-hour row, because "no
/// statistics yet" is an honest thing for a sensor installed this morning.
///
/// The hub does the arithmetic. Min, max and average arrive already computed
/// from Home Assistant's own recorder -- the statistics were always being
/// kept, and this screen reads them rather than recomputing history it never
/// saw.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../app_state.dart';
import '../widgets/voice_ui.dart';

class StatisticsScreen extends StatelessWidget {
  final AppState appState;
  const StatisticsScreen({super.key, required this.appState});

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: appState,
      builder: (context, _) => _build(context),
    );
  }

  Widget _build(BuildContext context) {
    final s = appState;
    final rows = s.readingRecords;

    // Grouped by room, rooms in name order, and a device with no room last
    // under its own heading. Sorting happens here rather than on the hub
    // because the hub already sorts for ITS rendering (the text table) and
    // two clients wanting two orders is not the hub's problem.
    final byRoom = <String, List<Map<String, dynamic>>>{};
    for (final r in rows) {
      byRoom.putIfAbsent((r['room'] as String?) ?? '', () => []).add(r);
    }
    final roomNames = byRoom.keys.toList()
      ..sort((a, b) {
        if (a.isEmpty) return 1; // no-room group sinks to the bottom
        if (b.isEmpty) return -1;
        return a.toLowerCase().compareTo(b.toLowerCase());
      });
    for (final list in byRoom.values) {
      list.sort((a, b) => ((a['name'] as String?) ?? '')
          .toLowerCase()
          .compareTo(((b['name'] as String?) ?? '').toLowerCase()));
    }

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
            onPressed: () => Navigator.of(context).pop(),
          ),
          title: Text('Statistics',
              style: TextStyle(
                  color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
          actions: [
            // Re-ask the hub. Readings are a SNAPSHOT -- the moment this
            // screen opened -- and nothing pushes updates to it, so the
            // honest affordance is a button that says "ask again" rather
            // than a number that quietly ages while you look at it.
            IconButton(
              tooltip: 'Read again',
              icon: Icon(Icons.refresh,
                  color: s.isReady ? s.inkDim : s.inkFaint, size: 20),
              onPressed: s.isReady ? () => s.requestReadings() : null,
            ),
          ],
        ),
        body: SafeArea(
          child: rows.isEmpty
              ? _empty(s)
              : VoiceUiScope(
                  state: s,
                  builder: (ctx, scroll) => ListView(
                  controller: scroll,
                  padding: const EdgeInsets.fromLTRB(16, 8, 16, 90),
                  children: [
                    Padding(
                      padding: const EdgeInsets.only(bottom: 10, left: 4),
                      child: Text(
                        '${rows.length} sensor${rows.length == 1 ? '' : 's'} · '
                        'live value, then the last 24 hours',
                        style: TextStyle(color: s.inkFaint, fontSize: 12),
                      ),
                    ),
                    for (final room in roomNames) ...[
                      _group(s, room.isEmpty ? 'NO ROOM' : room.toUpperCase()),
                      for (final r in byRoom[room]!) _card(s, r),
                    ],
                  ],
                ),
                ),
        ),
      ),
    );
  }

  Widget _empty(AppState s) => Center(
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 40),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(
                  s.awaitingReadings
                      ? Icons.hourglass_empty
                      : Icons.insights_outlined,
                  size: 44,
                  color: s.inkFaint),
              const SizedBox(height: 16),
              Text(
                s.awaitingReadings
                    ? 'Reading the sensors…'
                    : 'Nothing here measures anything yet',
                textAlign: TextAlign.center,
                style: TextStyle(
                    color: s.ink, fontSize: 15, fontWeight: FontWeight.w600),
              ),
              const SizedBox(height: 8),
              Text(
                s.awaitingReadings
                    ? 'The hub is asking Home Assistant for each sensor\'s '
                        'last 24 hours.'
                    : 'Add a sensor — temperature, power, distance — and its '
                        'readings appear here on their own.',
                textAlign: TextAlign.center,
                style: TextStyle(color: s.inkDim, fontSize: 13, height: 1.4),
              ),
            ],
          ),
        ),
      );

  Widget _group(AppState s, String label) => Padding(
        padding: const EdgeInsets.only(top: 14, bottom: 6, left: 4),
        child: Text(label,
            style: TextStyle(
                color: s.inkFaint,
                fontSize: 11,
                letterSpacing: 0.8,
                fontWeight: FontWeight.w700)),
      );

  /// One sensor. Name, then the live value large enough to read across a
  /// room, then the 24-hour trio if it exists.
  Widget _card(AppState s, Map<String, dynamic> r) {
    // Home Assistant says "unavailable" / "unknown" for a sensor it has
    // lost -- the hub passes the word through with reporting: true, and it
    // was drawn large in the accent colour as though it were a reading.
    final said = ((r['value'] as String?) ?? '').trim().toLowerCase();
    final reporting = r['reporting'] == true &&
        said != 'unavailable' &&
        said != 'unknown';
    final unit = (r['unit'] as String?) ?? '';
    final avg = _num(r['avg']);
    final hasStats = avg != null;

    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
      decoration: BoxDecoration(
        color: s.surface,
        border: Border.all(color: s.border),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Expanded(
                child: Text((r['name'] as String?) ?? '?',
                    style: TextStyle(
                        color: s.ink,
                        fontSize: 14.5,
                        fontWeight: FontWeight.w600)),
              ),
              const SizedBox(width: 10),
              if (reporting) ...[
                Text((r['value'] as String?) ?? '?',
                    style: TextStyle(
                        color: s.accent,
                        fontSize: 22,
                        height: 1.0,
                        fontWeight: FontWeight.w700)),
                if (unit.isNotEmpty) ...[
                  const SizedBox(width: 3),
                  Padding(
                    padding: const EdgeInsets.only(bottom: 2),
                    child: Text(unit,
                        style: TextStyle(color: s.inkDim, fontSize: 12.5)),
                  ),
                ],
              ] else
                // Amber, not red: a sensor that has gone quiet is a thing to
                // look into, not a failure of the app. Red is reserved for
                // what is actually broken.
                const Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(Icons.cloud_off_outlined,
                        size: 14, color: Color(0xFFD29922)),
                    SizedBox(width: 5),
                    Text('not reporting',
                        style: TextStyle(
                            color: Color(0xFFD29922),
                            fontSize: 12.5,
                            fontWeight: FontWeight.w600)),
                  ],
                ),
            ],
          ),
          if (hasStats) ...[
            const SizedBox(height: 11),
            Row(
              children: [
                _stat(s, 'MIN', _fmt(_num(r['min'])), unit),
                _divider(s),
                _stat(s, 'AVG', _fmt(avg), unit),
                _divider(s),
                _stat(s, 'MAX', _fmt(_num(r['max'])), unit),
              ],
            ),
            const SizedBox(height: 6),
            Text('last 24 hours',
                style: TextStyle(color: s.inkFaint, fontSize: 10.5)),
          ] else if (reporting) ...[
            const SizedBox(height: 8),
            // Said out loud rather than left as a blank space, because an
            // absent statistics row otherwise reads as a rendering bug.
            Text('No 24-hour history yet',
                style: TextStyle(color: s.inkFaint, fontSize: 11.5)),
          ],
        ],
      ),
    );
  }

  Widget _divider(AppState s) => Container(
      width: 1, height: 26, color: s.border, margin: const EdgeInsets.symmetric(horizontal: 12));

  Widget _stat(AppState s, String label, String value, String unit) => Expanded(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(label,
                style: TextStyle(
                    color: s.inkFaint,
                    fontSize: 9.5,
                    letterSpacing: 0.7,
                    fontWeight: FontWeight.w700)),
            const SizedBox(height: 3),
            Row(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                Flexible(
                  child: Text(value,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                          color: s.ink,
                          fontSize: 15,
                          fontWeight: FontWeight.w600)),
                ),
                if (unit.isNotEmpty) ...[
                  const SizedBox(width: 2),
                  Text(unit,
                      style: TextStyle(color: s.inkFaint, fontSize: 10)),
                ],
              ],
            ),
          ],
        ),
      );

  /// JSON numbers arrive as int or double depending on what Python sent, and
  /// a cast to one of them throws on the other. Anything non-numeric (a null
  /// from a sensor with no history) comes back null and the caller draws no
  /// statistics row at all.
  static double? _num(Object? v) => v is num ? v.toDouble() : null;

  /// Whole numbers without a trailing ".0", everything else to one decimal.
  /// A power meter reading "41.2" and a door count reading "3" should not be
  /// forced into the same shape -- "3.0 doors" is a number pretending to be
  /// more precise than it is.
  static String _fmt(double? v) {
    if (v == null) return '—';
    if (v == v.roundToDouble() && v.abs() < 100000) {
      return v.toInt().toString();
    }
    return v.toStringAsFixed(1);
  }
}