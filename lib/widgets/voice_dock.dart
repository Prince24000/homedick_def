/// The compact voice presence that follows every screen the big orb doesn't
/// own. Rendered ONCE, above the Navigator (MaterialApp.builder), so no
/// screen has to remember to embed it — which is exactly how EnrollScreen
/// got an orb and PromptScreen didn't.
///
/// Design contract:
///  * Visibility and control, not capability — VoiceFlow already works on
///    every screen; this makes the hot mic VISIBLE (a system that records
///    voiceprints must never listen invisibly) and gives cancel/tap-to-talk
///    everywhere.
///  * Hidden on '/' and '/enroll', where the full orb is the centerpiece.
///  * Tap = answer what the server is waiting on (AppState.dockTap routes to
///    the outstanding question's window), else a fresh command capture.
///
/// ONE CONTRACT, EVERY SCREEN, NO EXCEPTIONS. The dock is the microphone
/// and nothing else. Tap = talk (or send, while recording). X = stop
/// listening — never cancel. It used to be cleverer than that: on some
/// screens the X cancelled the whole flow, on others it would have muted,
/// and the same little button quietly meant different things in different
/// rooms, which is how a mic control ends up cancelling someone's
/// selection. Cancelling a flow is the SCREEN's job — every question
/// screen carries its own Cancel — so the dock gave that power back.
/// Change the dock once, and it changes everywhere, because it only ever
/// had one job.
library;

import 'dart:ui' show ImageFilter;

import 'package:flutter/material.dart';

import '../app_state.dart';
import '../flow/voice_flow.dart';
import '../socket_client.dart';

class VoiceDock extends StatefulWidget {
  final AppState appState;
  const VoiceDock({super.key, required this.appState});

  /// Routes where the dock stays out of the way entirely.
  /// '/working' as well (her call, 2026-08-31): that screen is a job in
  /// flight -- no question to answer, no Back, no Cancel -- so a mic pill
  /// there offers something the screen cannot do.
  // '/pair' joins the list (audit, 2026-09-12): the pairing screen is a
  // ceremony with no hub connection behind it — there is nothing for the
  // dock to talk to. It was reachable WITHOUT the dock from Home and WITH
  // it from Settings, because both pushes were unnamed and topRoute kept
  // reporting whatever screen was underneath. Both are named now, so this
  // entry is what decides it, once, for both doors.
  static const hiddenOn = {
    '/', '/enroll', '/working', '/scan', '/camera', '/pair',
  };

  /// Will the dock render right now? Shared with main.dart, which
  /// RESERVES this much room under the Navigator whenever it is true --
  /// the dock used to float OVER the bottom of every list ("don't let it
  /// hover any info", field test 2026-08-25), hiding exactly the row the
  /// person was reaching for. Reserved space instead of overlay: content
  /// ends where the dock begins.
  static bool visibleOn(AppState s) =>
      s.running && !hiddenOn.contains(s.topRoute);

  /// Does it take its strip of screen? Not while hidden: it is then a
  /// small tab on the edge and the screen gets the room back.
  static bool reservesSpace(AppState s) => visibleOn(s) && !s.dockHidden;

  /// The strip of screen the dock owns when visible: pill + margins.
  static const double reservedHeight = 72;

  @override
  State<VoiceDock> createState() => _VoiceDockState();
}

class _VoiceDockState extends State<VoiceDock>
    with SingleTickerProviderStateMixin {
  late final AnimationController _pulse = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1100),
  )..repeat(reverse: true);

  @override
  void dispose() {
    _pulse.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: widget.appState,
      builder: (context, _) {
        final s = widget.appState;
        if (!VoiceDock.visibleOn(s)) {
          return const SizedBox.shrink();
        }

        final flow = s.flowState;
        final capturing = flow == FlowState.capturing;
        final thinking = flow == FlowState.thinking;
        final speaking = flow == FlowState.speaking;
        final connected = s.connState == ConnState.ready;
        final choiceListed = s.pendingChoice != null;

        final fg = s.accentFg;

        // HIDDEN (round 2): a small tab on the right edge. The voice still
        // works -- only the pill is put away. A tap brings it back.
        if (s.dockHidden) {
          return Positioned(
            right: 0,
            bottom: MediaQuery.of(context).padding.bottom + 140,
            child: Semantics(
              button: true,
              label: 'Show the voice dock',
              child: GestureDetector(
                onTap: () => s.setDockHidden(false),
                child: Container(
                  width: 24,
                  height: 64,
                  decoration: BoxDecoration(
                    color: s.accent,
                    borderRadius: const BorderRadius.horizontal(
                        left: Radius.circular(12)),
                    boxShadow: [
                      BoxShadow(
                          color: Colors.black.withValues(alpha: 0.2),
                          blurRadius: 8,
                          offset: const Offset(0, 2)),
                    ],
                  ),
                  child: Icon(
                      capturing ? Icons.mic : Icons.mic_none,
                      size: 15,
                      color: fg),
                ),
              ),
            ),
          );
        }

        // ICONS AT REST, WORDS WHILE IT WORKS (her design, 2026-10-15):
        // the pill says nothing while idle -- the microphone's own icon
        // says on or off -- and speaks up only while something is
        // happening: listening, working, speaking, a choice to say.
        String? label;
        IconData icon;
        if (!connected) {
          label = 'offline';
          icon = Icons.cloud_off;
        } else if (capturing) {
          label = s.micStarved
              ? 'not hearing anything'
              : (choiceListed
                  ? 'say an option…'
                  : (s.awaitingCapture
                      ? 'recording — tap to send'
                      : 'listening…'));
          icon = Icons.mic;
        } else if (thinking) {
          label = 'working… tap to cancel';
          icon = Icons.hourglass_empty;
        } else if (speaking) {
          label = s.ttsMuted ? 'muted' : 'speaking';
          icon = s.ttsMuted ? Icons.volume_off : Icons.volume_up;
        } else if (choiceListed && s.dockMuted) {
          icon = Icons.mic_off;
        } else if (s.awaitingCapture) {
          label = 'tap to record';
          icon = Icons.mic_none;
        } else if (s.screenTakesVoice) {
          label = 'tap to fill by voice';
          icon = Icons.edit_note;
        } else if (s.pendingForm != null ||
            s.pendingRegistration != null ||
            s.ruleBuilder != null) {
          // Drawn but not fillable by voice: the tap reads the screen out.
          label = 'tap to hear it';
          icon = Icons.record_voice_over_outlined;
        } else {
          icon = Icons.mic_none;
        }

        final Widget leading = capturing
            ? AnimatedBuilder(
                animation: _pulse,
                builder: (_, __) => Container(
                  width: 26,
                  height: 26,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: fg.withValues(alpha: 0.15 + 0.25 * _pulse.value),
                  ),
                  child: Icon(icon, size: 17, color: fg),
                ),
              )
            : thinking
                ? SizedBox(
                    width: 18,
                    height: 18,
                    child: CircularProgressIndicator(
                        strokeWidth: 2, color: fg))
                : Icon(icon, size: 20, color: fg);

        // The X: stop listening / stop speaking -- never cancel a flow.
        final showMute =
            capturing || speaking || (choiceListed && !s.dockMuted);

        Widget tapIcon(IconData i, String tip, VoidCallback onTap,
                {double size = 19}) =>
            Semantics(
              button: true,
              label: tip,
              child: InkWell(
                borderRadius: BorderRadius.circular(16),
                onTap: onTap,
                child: SizedBox(
                    width: 34,
                    height: 34,
                    child: Icon(i, size: size, color: fg)),
              ),
            );

        return Positioned(
          left: 0,
          right: 0,
          bottom: MediaQuery.of(context).padding.bottom + 12,
          child: Center(
            child: DecoratedBox(
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(24),
                boxShadow: [
                  BoxShadow(
                      color: Colors.black.withValues(alpha: 0.22),
                      blurRadius: 18,
                      offset: const Offset(0, 6)),
                ],
              ),
              child: ClipRRect(
                borderRadius: BorderRadius.circular(24),
                // FLOATING (her design): the screen behind shows through,
                // blurred, under the app's own accent.
                child: BackdropFilter(
                  filter: ImageFilter.blur(sigmaX: 14, sigmaY: 14),
                  child: Material(
                    color: s.accent.withValues(alpha: 0.95),
                    child: InkWell(
                      // Tap: talk, or send while recording, or stop the
                      // voice, or cancel while working (AppState.dockTap).
                      onTap: connected ? () => s.dockTap() : null,
                      child: Container(
                        height: 48,
                        padding: const EdgeInsets.symmetric(horizontal: 7),
                        decoration: BoxDecoration(
                          borderRadius: BorderRadius.circular(24),
                          border: Border.all(
                              color: Colors.white.withValues(alpha: 0.22)),
                        ),
                        child: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            SizedBox(
                                width: 34,
                                height: 34,
                                child: Center(child: leading)),
                            if (label != null)
                              Padding(
                                padding:
                                    const EdgeInsets.only(left: 2, right: 6),
                                child: Text(label,
                                    style: TextStyle(
                                        fontSize: 13,
                                        fontWeight: FontWeight.w600,
                                        color: fg)),
                              ),
                            Container(
                                width: 1,
                                height: 22,
                                margin:
                                    const EdgeInsets.symmetric(horizontal: 4),
                                color: fg.withValues(alpha: 0.3)),
                            // The speaker: the remembered mute.
                            tapIcon(
                                s.ttsMuted
                                    ? Icons.volume_off
                                    : Icons.volume_up_outlined,
                                s.ttsMuted ? 'Voice off' : 'Voice on',
                                () => s.toggleMute()),
                            if (showMute)
                              tapIcon(Icons.close, 'Stop', () => s.muteDock(),
                                  size: 18),
                            // Put the dock away (the voice keeps working).
                            Padding(
                              padding: const EdgeInsets.only(left: 2),
                              child: Container(
                                width: 28,
                                height: 28,
                                decoration: BoxDecoration(
                                  shape: BoxShape.circle,
                                  color: fg.withValues(alpha: 0.12),
                                ),
                                child: tapIcon(Icons.keyboard_arrow_down,
                                    'Hide the voice dock',
                                    () => s.setDockHidden(true),
                                    size: 18),
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ),
          ),
        );
      },
    );
  }
}