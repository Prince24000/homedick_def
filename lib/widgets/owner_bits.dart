/// WHO MADE WHAT (her design, 2026-10-11): the tag on a scene or
/// automation card, the dates, and the details panel a long press opens.
///
/// The hub decides the words. `chip` arrives as "Just mine", "by you",
/// "by chidi" or "Everyone" -- empty while one person lives here, and then
/// nothing is drawn. `by` / `changed_by` arrive as "you" or a name, also
/// empty for one person. The app only formats dates and draws.
library;

import 'dart:ui' show ImageFilter;

import 'package:flutter/material.dart';
import '../app_state.dart';

const _months = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'
];

/// A hub time: epoch seconds (automations) or ISO text (scenes).
DateTime? hubTime(dynamic v) {
  if (v is num && v > 0) {
    return DateTime.fromMillisecondsSinceEpoch((v * 1000).round());
  }
  if (v is String && v.isNotEmpty) return DateTime.tryParse(v)?.toLocal();
  return null;
}

/// "3 Oct"; with [time], "3 Oct, 14:20". Empty when there is no date.
String dayOf(dynamic v, {bool time = false}) {
  final t = hubTime(v);
  if (t == null) return '';
  final d = '${t.day} ${_months[t.month - 1]}';
  if (!time) return d;
  final hh = t.hour.toString().padLeft(2, '0');
  final mm = t.minute.toString().padLeft(2, '0');
  return '$d, $hh:$mm';
}

/// The bright tag beside a card's name. Nothing for an empty [text].
class OwnerTag extends StatelessWidget {
  final AppState s;
  final String text;
  const OwnerTag(this.s, this.text, {super.key});

  @override
  Widget build(BuildContext context) {
    if (text.isEmpty) return const SizedBox.shrink();
    return Container(
      margin: const EdgeInsets.only(left: 6),
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 3),
      decoration: BoxDecoration(
        color: s.accent.withValues(alpha: 0.14),
        border: Border.all(color: s.accent.withValues(alpha: 0.35)),
        borderRadius: BorderRadius.circular(20),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Icon(text == 'Everyone' ? Icons.groups_outlined : Icons.person_outline,
            size: 13, color: s.accent),
        const SizedBox(width: 4),
        Text(text,
            style: TextStyle(
                fontSize: 12, fontWeight: FontWeight.w700, color: s.accent)),
      ]),
    );
  }
}

/// "Made by you · 1 Oct" and "Changed by chidi · 10 Oct" for a record
/// carrying by/at/changed_by/changed_at. One person alone: "Made 1 Oct".
List<Widget> whoLines(AppState s, Map r) {
  String line(String verb, dynamic who, dynamic at) {
    final w = (who ?? '').toString();
    final d = dayOf(at);
    if (w.isEmpty && d.isEmpty) return '';
    return [if (w.isEmpty) verb else '$verb by $w', if (d.isNotEmpty) d]
        .join(w.isEmpty ? ' ' : ' · ');
  }

  Widget row(IconData icon, String text) => Padding(
        padding: const EdgeInsets.only(bottom: 8),
        child: Row(children: [
          Icon(icon, size: 16, color: s.ink),
          const SizedBox(width: 8),
          Expanded(
            child: Text(text,
                style: TextStyle(
                    fontSize: 14, fontWeight: FontWeight.w600, color: s.ink)),
          ),
        ]),
      );

  final made = line('Made', r['by'], r['at']);
  final changed = line('Changed', r['changed_by'], r['changed_at']);
  return [
    if (made.isNotEmpty) row(Icons.add, made),
    // A change with no one named and no date says nothing.
    if (changed.isNotEmpty && changed != 'Changed') row(Icons.edit_outlined, changed),
  ];
}

/// A small heading inside the details panel.
Widget detailsHeading(AppState s, String text) => Padding(
      padding: const EdgeInsets.only(top: 6, bottom: 8),
      child: Text(text,
          style: TextStyle(
              fontSize: 11,
              letterSpacing: 0.8,
              fontWeight: FontWeight.w700,
              color: s.inkFaint)),
    );

/// THE LONG-PRESS PANEL: slides up from the bottom while the screen behind
/// blurs. Tap outside it, or drag it down, to close.
Future<void> showDetailsPanel(
  BuildContext context,
  AppState s, {
  required String title,
  String tag = '',
  String subtitle = '',
  bool italicSubtitle = false,
  required List<Widget> body,
}) {
  return showGeneralDialog<void>(
    context: context,
    barrierDismissible: true,
    barrierLabel: 'Close details',
    barrierColor: Colors.transparent,
    transitionDuration: const Duration(milliseconds: 260),
    pageBuilder: (ctx, anim, _) {
      final curve = CurvedAnimation(parent: anim, curve: Curves.easeOutCubic);
      return Stack(children: [
        Positioned.fill(
          child: GestureDetector(
            onTap: () => Navigator.of(ctx).pop(),
            child: FadeTransition(
              opacity: curve,
              child: BackdropFilter(
                filter: ImageFilter.blur(sigmaX: 5, sigmaY: 5),
                child: Container(color: Colors.black.withValues(alpha: 0.4)),
              ),
            ),
          ),
        ),
        Align(
          alignment: Alignment.bottomCenter,
          child: SlideTransition(
            position: Tween(begin: const Offset(0, 1), end: Offset.zero)
                .animate(curve),
            child: GestureDetector(
              onVerticalDragEnd: (d) {
                if ((d.primaryVelocity ?? 0) > 250) Navigator.of(ctx).pop();
              },
              child: Material(
                color: s.surface,
                shape: RoundedRectangleBorder(
                  side: BorderSide(color: s.border),
                  borderRadius:
                      const BorderRadius.vertical(top: Radius.circular(20)),
                ),
                child: SafeArea(
                  top: false,
                  child: ConstrainedBox(
                    constraints: BoxConstraints(
                        maxHeight: MediaQuery.of(ctx).size.height * 0.75),
                    child: SingleChildScrollView(
                      padding: const EdgeInsets.fromLTRB(20, 10, 20, 24),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Center(
                            child: Container(
                              width: 36,
                              height: 4,
                              decoration: BoxDecoration(
                                color: s.inkFaint.withValues(alpha: 0.5),
                                borderRadius: BorderRadius.circular(2),
                              ),
                            ),
                          ),
                          const SizedBox(height: 14),
                          Row(children: [
                            Expanded(
                              child: Text(title,
                                  style: TextStyle(
                                      fontSize: 19,
                                      fontWeight: FontWeight.w700,
                                      color: s.ink)),
                            ),
                            OwnerTag(s, tag),
                          ]),
                          if (subtitle.isNotEmpty) ...[
                            const SizedBox(height: 6),
                            Text(subtitle,
                                style: TextStyle(
                                    fontSize: 13.5,
                                    height: 1.45,
                                    fontStyle: italicSubtitle
                                        ? FontStyle.italic
                                        : FontStyle.normal,
                                    color: s.inkDim)),
                          ],
                          const SizedBox(height: 8),
                          ...body,
                        ],
                      ),
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
      ]);
    },
  );
}
