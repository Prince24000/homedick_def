/// THE JOB'S OWN ROOM.
///
/// Flashing a board, erasing a chip, restarting the broker, generating a
/// description on the Pi — these take a minute and they talk while they
/// work. All of it used to happen ON the home screen: the busy card
/// floating over it, the long progress line where the orb's status
/// belongs, the broker's pid chatter under the orb (field test
/// 2026-08-27: "this shouldnt be in the home screen").
///
/// Home is where you speak to the house. A job in progress is not that,
/// so it gets a screen — the card, its progress, and the running account
/// of what the hub is actually doing, in one place that belongs to it.
///
/// It is pushed when the hub says it is going to be a while (a `busy`
/// frame) and popped with the other flow routes when the ribbon ends, so
/// nothing here has to guess when the work finished.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../app_state.dart';
import '../widgets/busy_card.dart';

class WorkingScreen extends StatelessWidget {
  final AppState appState;
  const WorkingScreen({super.key, required this.appState});

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: appState,
      builder: (context, _) => _build(context),
    );
  }

  Widget _build(BuildContext context) {
    final s = appState;
    final lines = s.flowLines;

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
      // NO BACK ARROW, and PopScope blocks the gesture: the hub is holding
      // its shell lock while this runs, so leaving would only find every
      // next action refused as "busy". The way out is Cancel, which tells
      // the hub -- or simply waiting, which is usually right.
      child: PopScope(
        canPop: false,
        child: Scaffold(
          backgroundColor: s.bg,
          body: SafeArea(
            child: Column(
              children: [
                const SizedBox(height: 28),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 20),
                  // BARE, not boxed. BusyCard's own note says why: a box
                  // drawn on a page of its own just looks like the popup
                  // it replaced.
                  child: BusyCard(appState: s, boxed: false),
                ),
                const SizedBox(height: 18),
                // WHAT IT IS ACTUALLY DOING. The same lines that used to
                // crowd the orb, given room to be read instead: newest
                // last, so it reads like a log rather than a shuffle.
                Expanded(
                  child: lines.isEmpty
                      ? Center(
                          child: Text(
                            s.busyText == null
                                ? 'Working…'
                                : 'The hub will say when it is done.',
                            style:
                                TextStyle(color: s.inkFaint, fontSize: 13),
                          ),
                        )
                      : ListView.builder(
                          padding: const EdgeInsets.fromLTRB(24, 4, 24, 8),
                          itemCount: lines.length,
                          itemBuilder: (context, i) {
                            final last = i == lines.length - 1;
                            return Padding(
                              padding: const EdgeInsets.only(bottom: 8),
                              child: Row(
                                crossAxisAlignment:
                                    CrossAxisAlignment.start,
                                children: [
                                  Padding(
                                    padding:
                                        const EdgeInsets.only(top: 5.5),
                                    child: Container(
                                      width: 5,
                                      height: 5,
                                      decoration: BoxDecoration(
                                        shape: BoxShape.circle,
                                        color: last
                                            ? s.accent
                                            : s.inkFaint.withValues(
                                                alpha: 0.5),
                                      ),
                                    ),
                                  ),
                                  const SizedBox(width: 10),
                                  Expanded(
                                    child: Text(
                                      lines[i],
                                      style: TextStyle(
                                        color: last ? s.ink : s.inkDim,
                                        fontSize: 13,
                                        height: 1.4,
                                        fontWeight: last
                                            ? FontWeight.w500
                                            : FontWeight.w400,
                                      ),
                                    ),
                                  ),
                                ],
                              ),
                            );
                          },
                        ),
                ),
                Padding(
                  padding: const EdgeInsets.fromLTRB(20, 4, 20, 16),
                  child: Column(
                    children: [
                      // WHAT THE BUTTON CAN ACTUALLY DO (audit,
                      // 2026-09-12). Nothing can interrupt the hub
                      // mid-step -- and that is deliberate: stopping
                      // esptool halfway through writing a chip is how a
                      // board gets bricked. So this stops the flow at the
                      // NEXT step, and the label now says so rather than
                      // promising to stop what is running.
                      Text(
                        'Nothing can interrupt the step that is running — '
                        'stopping a chip mid-write would ruin the board. '
                        'This stops the flow as soon as this step ends.',
                        textAlign: TextAlign.center,
                        style:
                            TextStyle(color: s.inkFaint, fontSize: 11.5),
                      ),
                      const SizedBox(height: 10),
                      SizedBox(
                        width: double.infinity,
                        child: OutlinedButton(
                          style: s.cancelButtonStyle(),
                          onPressed: () => s.sendCancel(),
                          child: const Text('Stop at the next step',
                              style: TextStyle(
                                  fontWeight: FontWeight.w600,
                                  fontSize: 15)),
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}