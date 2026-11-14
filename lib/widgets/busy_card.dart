/// The one card that says "the hub is working, and here is what on."
///
/// Shared between the overlay in main.dart (any screen) and the Working
/// screen (mid-flow), so the two can never drift apart -- the person sees
/// the same card wherever the wait finds them, drawn in the app's own
/// palette.
///
/// Wrapped in a transparent [Material] on purpose. The overlay lives above
/// the Navigator, outside any Scaffold, and a Text with no Material
/// ancestor renders in Flutter's error style: yellow double underlines,
/// which is exactly what the first live flash screen showed. The Material
/// is not decoration; it is what makes the text render as text.
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../app_state.dart';

class BusyCard extends StatefulWidget {
  const BusyCard({super.key, required this.appState, this.boxed = true});

  final AppState appState;

  /// Boxed (surface, border, corners) when floating as an overlay above
  /// another screen; bare when it IS the screen -- the Working screen
  /// shows the content directly on the background, because a box drawn on
  /// a page of its own just looks like the popup it replaced.
  final bool boxed;

  @override
  State<BusyCard> createState() => _BusyCardState();
}

class _BusyCardState extends State<BusyCard> {
  Timer? _ticker;
  double _elapsed = 0;
  String? _lastText;

  @override
  void initState() {
    super.initState();
    _ticker = Timer.periodic(const Duration(milliseconds: 100), (_) {
      if (!mounted) return;
      // Redrawn only when there is a countdown to move: an indeterminate
      // bar animates itself, and ten rebuilds a second of an unchanged
      // card (under the overlay, on every screen) bought nothing.
      if (widget.appState.busySeconds == null) {
        _elapsed += 0.1;
        return;
      }
      setState(() => _elapsed += 0.1);
    });
  }

  @override
  void dispose() {
    _ticker?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.appState;

    // A NEW PHASE RESTARTS THE CLOCK. "Erasing the chip" and "Writing the
    // firmware" arrive as separate busy frames; if the elapsed time carried
    // over, the second bar would start half full and lie about the wait.
    final text = s.busyText ?? '';
    if (text != _lastText) {
      _lastText = text;
      _elapsed = 0;
    }

    final total = s.busySeconds;
    // Capped just below 1. Reaching a full bar while still waiting reads as
    // "finished, and stuck" -- worse than a bar that is visibly nearly done.
    final progress = (total == null || total <= 0)
        ? null
        : (_elapsed / total).clamp(0.0, 0.98);
    final left = total == null ? null : (total - _elapsed).ceil();

    return Material(
      type: MaterialType.transparency,
      child: Container(
        padding: const EdgeInsets.fromLTRB(22, 24, 22, 22),
        decoration: widget.boxed
            ? BoxDecoration(
                color: s.surface,
                border: Border.all(color: s.border),
                borderRadius: BorderRadius.circular(14),
              )
            : null,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(text,
                textAlign: TextAlign.center,
                style: TextStyle(
                    color: s.ink,
                    fontSize: 15,
                    fontWeight: FontWeight.w600,
                    decoration: TextDecoration.none)),
            if ((s.busyDetail ?? '').isNotEmpty) ...[
              const SizedBox(height: 6),
              Text(s.busyDetail!,
                  textAlign: TextAlign.center,
                  style: TextStyle(
                      color: s.inkDim,
                      fontSize: 12.5,
                      decoration: TextDecoration.none)),
            ],
            const SizedBox(height: 18),
            ClipRRect(
              borderRadius: BorderRadius.circular(3),
              child: LinearProgressIndicator(
                value: progress, // null => indeterminate
                minHeight: 5,
                backgroundColor: s.border,
                color: s.accent,
              ),
            ),
            if (left != null) ...[
              const SizedBox(height: 10),
              Text(
                  left > 0
                      ? 'about $left second${left == 1 ? '' : 's'} left'
                      : 'nearly there',
                  textAlign: TextAlign.center,
                  style: TextStyle(
                      color: s.inkFaint,
                      fontSize: 11.5,
                      decoration: TextDecoration.none)),
            ],
          ],
        ),
      ),
    );
  }
}