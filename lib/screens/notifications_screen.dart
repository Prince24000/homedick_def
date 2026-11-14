import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../app_state.dart';
import '../widgets/voice_ui.dart';

/// Pending actions, each one a door to the place that finishes it.
///
/// The rule this screen exists to enforce: a message that tells you something
/// is unfinished, and then makes you go and find the screen that fixes it, is
/// only half a notification. Every card here carries the action that clears
/// it — "Decide now" asks the rewire question, a route entry opens the
/// screen — so the message and the fix are one tap apart.
///
/// SEEN IS NOT RESOLVED. Opening this screen marks everything seen — the red
/// dot on the bell goes out — but the messages themselves stay, through any
/// number of viewings and taps, until the HUB says their condition cleared
/// (a notice_gone frame, or the id dropping off the next recheck). A rewire
/// waiting for its one answer waits here, visibly, for as long as it takes.
class NotificationsScreen extends StatefulWidget {
  final AppState appState;
  const NotificationsScreen({super.key, required this.appState});

  @override
  State<NotificationsScreen> createState() => _NotificationsScreenState();
}

class _NotificationsScreenState extends State<NotificationsScreen> {
  @override
  void initState() {
    super.initState();
    // Being here is what "seen" means. Post-frame, because marking notifies
    // listeners and this frame is still building.
    WidgetsBinding.instance
        .addPostFrameCallback((_) => widget.appState.markNoticesSeen());
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: widget.appState,
      builder: (context, _) {
        // A notice that arrives WHILE the screen is open is also seen —
        // the person is looking straight at it.
        if (widget.appState.hasUnseenNotices) {
          WidgetsBinding.instance
              .addPostFrameCallback((_) => widget.appState.markNoticesSeen());
        }
        return _build(context);
      },
    );
  }

  Widget _build(BuildContext context) {
    final s = widget.appState;
    final notices = s.notices;

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
            onPressed: () => Navigator.of(context).maybePop(),
          ),
          title: Text('Pending actions',
              style: TextStyle(
                  color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
          actions: [
            IconButton(
              tooltip: 'Check again',
              icon: Icon(Icons.refresh, color: s.inkDim, size: 20),
              onPressed: s.isReady ? () => s.requestNotices() : null,
            ),
          ],
        ),
        body: SafeArea(
          child: notices.isEmpty
              ? _empty(s)
              : VoiceUiScope(
                  state: s,
                  builder: (ctx, scroll) => ListView.builder(
                  controller: scroll,
                  // Bottom padding clears the floating voice dock.
                  padding: const EdgeInsets.fromLTRB(16, 16, 16, 80),
                  itemCount: notices.length,
                  itemBuilder: (context, i) => _card(context, s, notices[i]),
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
              Icon(Icons.check_circle_outline, size: 44, color: s.inkFaint),
              const SizedBox(height: 14),
              Text('Nothing outstanding',
                  style: TextStyle(
                      color: s.ink, fontSize: 16, fontWeight: FontWeight.w600)),
              const SizedBox(height: 8),
              Text(
                'When a board is rewired and waiting for a decision, or a '
                'device has no description for voice matching, it will show '
                'here — and stay here until it is settled.',
                textAlign: TextAlign.center,
                style: TextStyle(color: s.inkDim, fontSize: 13, height: 1.45),
              ),
            ],
          ),
        ),
      );

  Widget _card(BuildContext context, AppState s, Notice n) {
    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.fromLTRB(14, 14, 8, 12),
      decoration: BoxDecoration(
        color: s.surface,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: n.actionable ? s.accent : s.border),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: Padding(
                  padding: const EdgeInsets.only(top: 2, right: 8),
                  child: Text(n.title,
                      style: TextStyle(
                          color: s.ink,
                          fontSize: 14.5,
                          fontWeight: FontWeight.w700,
                          height: 1.35)),
                ),
              ),
              // Hiding is allowed — it is the person's list — but it is
              // HIDING, not resolving: a sticky notice returns on the next
              // recheck for as long as its condition holds.
              IconButton(
                tooltip: 'Hide until next check',
                icon: Icon(Icons.close, size: 18, color: s.inkFaint),
                onPressed: () => s.dismissNotice(n.id),
              ),
            ],
          ),
          Padding(
            padding: const EdgeInsets.only(right: 8, top: 2),
            child: Text(n.body,
                style:
                    TextStyle(color: s.inkDim, fontSize: 13, height: 1.45)),
          ),
          if (n.actionable) ...[
            const SizedBox(height: 12),
            SizedBox(
              width: double.infinity,
              child: FilledButton(
                style: FilledButton.styleFrom(
                  backgroundColor: s.accent,
                  foregroundColor: s.accentFg,
                  disabledBackgroundColor: s.accent.withValues(alpha: 0.35),
                  padding: const EdgeInsets.symmetric(vertical: 12),
                  shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(10)),
                ),
                // A menu action needs a live, idle hub. Offering a button
                // that can only come back "busy" is worse than greying it.
                // The card does NOT vanish on tap: it goes when the hub
                // confirms the condition cleared, and not before.
                // ...and idle, for a MENU notice: runNotice refuses one while
                // a flow runs, and the tap used to close this screen first,
                // leaving a refusal toast over whatever lay beneath.
                onPressed: s.isReady &&
                        !(n.actionKind == 'menu' && s.menuBusy)
                    ? () {
                        // A letter's "Got it" stays here: it only marks
                        // the letter read. Every other action leaves.
                        if (!n.actionValue.startsWith('seen:')) {
                          Navigator.of(context).maybePop();
                        }
                        s.runNotice(n);
                      }
                    : null,
                child: Text(
                    n.actionLabel.isEmpty ? 'Do it now' : n.actionLabel,
                    style: const TextStyle(
                        fontWeight: FontWeight.w700, fontSize: 14)),
              ),
            ),
          ],
        ],
      ),
    );
  }
}