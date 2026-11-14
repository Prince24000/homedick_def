import 'dart:math' show min;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:homevoice/flow/voice_flow.dart';
import 'package:homevoice/socket_client.dart';
import '../app_state.dart';
import 'pair_screen.dart';
import '../widgets/orb.dart';

class HomeScreen extends StatefulWidget {
  final AppState appState;
  const HomeScreen({super.key, required this.appState});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  bool _showingConnectFail = false;

  @override
  void initState() {
    super.initState();
    widget.appState.addListener(_onStateChange);
  }

  @override
  void dispose() {
    widget.appState.removeListener(_onStateChange);
    _readingPage.dispose();
    super.dispose();
  }

  void _onStateChange() {
    // FIX (screens not rendering, root cause #2): the ListenableBuilder in
    // main.dart rebuilds MaterialApp, but live routes are NOT rebuilt by a
    // MaterialApp rebuild — so status text, the connection dot, the orb and
    // the connect button all froze at their first frame. Each screen must
    // rebuild itself on AppState notifications.
    if (mounted) setState(() {});
    if (widget.appState.connectFailed && !_showingConnectFail) {
      _showingConnectFail = true;
      widget.appState.clearConnectFailed();
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _showConnectHelp();
      });
    }
    // Keepsafe (recovery phrase) is shown by AppState itself now — a themed
    // card with a copy icon over a blurred backdrop, whose dismissal also
    // acknowledges the server. The dialog that lived here answered nothing,
    // so the server sat waiting on its "written it down" prompt: the grey
    // hang.
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.appState;
    // Flag-driven, not substring-driven: the status line gets overwritten
    // by every later event, which is how a wrong secret became a dead end.
    final needsPairing = s.needPairing;
    final isBlocked = s.blockingProblem != null;
    final showOverlay = isBlocked || needsPairing;

    final flow = s.flowState;
    final isFlowActive = flow == FlowState.capturing || flow == FlowState.speaking;
    final isFlowThinking = flow == FlowState.thinking;
    // The home cards folded away (or none to show): the orb takes the
    // middle of the screen, larger.
    final cardsHidden = s.readingsCollapsed || s.homeReadings.isEmpty;

    // NO X WHILE THE HUB THINKS (her design, 2026-10-09): the thinking
    // orb itself is the cancel -- a tap on it (AppState.dockTap), or a
    // spoken "cancel". The X stays for a question, a recording, a phrase.
    final canCancel = s.pendingRecord != null ||
        s.pendingChoice != null ||
        s.pendingPrompt != null || // FIX: prompts are cancellable too
        s.challengePhrase != null ||
        (s.isStarting && !isFlowThinking);

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
        drawer: s.running ? _buildDrawer() : null,
        body: SafeArea(
          child: Stack(
            children: [
              Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  _buildTopBar(),
                  const SizedBox(height: 20),
                  // The "House power" dropdown and its metrics grid were
                  // REMOVED, not parked: readings live in Settings >
                  // Readings & statistics now, where every sensor shows
                  // under the name its owner gave it, with its 24-hour
                  // history -- instead of a phone-side grid guessing at
                  // three numbers.
                  // The server-log card was removed by request: devices have
                  // their own screen now, and the raw row feed read as noise
                  // on the home screen. The status line still shows the
                  // latest server line; serverMessages stays collected in
                  // AppState should a screen want it later.
                  //
                  // THE READINGS CAME BACK (2026-08-31), and not as the old
                  // grid: that one guessed at three phone-side numbers.
                  // These are the board's own values, the same list the
                  // hallway panel rotates, pushed while a phone is
                  // connected. One tile per READING -- a DHT is two of
                  // them -- and it folds to a single row so the orb never
                  // moves.
                  if (s.running && !showOverlay) _buildReadings(s),
                  // THE ORB IN THE MIDDLE OF WHAT IS LEFT (her design,
                  // 2026-10-11): a spacer above it and one below its
                  // status, so with the cards folded away it sits in the
                  // middle of the screen, and larger.
                  const Spacer(),
                  if (!s.running && !showOverlay && s.isBooting) ...[
                    _buildBootSpinner(),
                    const Spacer(), // spinner + text dead-centre while booting
                  ],
                  if (s.running && !showOverlay)
                    Center(
                      child: Orb(
                        appState: s,
                        // Back to the cards-open size while it speaks,
                        // so the waves have room round it.
                        size: cardsHidden && flow != FlowState.speaking
                            ? 170
                            : 140,
                        phrase: s.challengePhrase,
                        isActive: isFlowActive,
                        isSpeaking: flow == FlowState.speaking,
                        isThinking: isFlowThinking,
                        showCancel: canCancel,
                        onCancel: () => s.sendCancel(),
                        // Route by what the server is waiting on. A bare
                        // triggerCapture here would send a challenge answer
                        // out as a command frame.
                        onTap: () => s.dockTap(),
                      ),
                    ),
                  const SizedBox(height: 20),
                  // CONNECTING SAYS IT ON ITS BUTTON, and nowhere else (her
                  // design, 2026-10-11): no line under the orb meanwhile.
                  if (s.running && !showOverlay && !s.isConnecting)
                    Text(
                      s.status,
                      textAlign: TextAlign.center,
                      // BOUNDED. The status line carries the hub's latest
                      // sentence, and some of those are paragraphs
                      // ("Erasing the chip completely first -- its
                      // history is unknown..."). Three lines of hub prose
                      // under the orb pushed the Connect button off the
                      // screen and made home look like a log. Two lines,
                      // then an ellipsis; the whole account lives on the
                      // Working screen where there is room for it.
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: s.orbColor,
                        fontSize: 15,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                  // Working indicator: a menu action is running server-side
                  // — show LIFE, and nothing else. It used to print the
                  // hub's latest line here (broker pids, chip erasures),
                  // which is the home screen narrating a job it does not
                  // own (field test 2026-08-27). The account lives on the
                  // Working screen now; home only says that something is
                  // happening, for the brief moments before that screen
                  // exists or after it has gone.
                  if (s.running && !showOverlay && s.menuBusy)
                    Padding(
                      padding: const EdgeInsets.fromLTRB(24, 0, 24, 4),
                      child: Row(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          SizedBox(
                            width: 14,
                            height: 14,
                            child: CircularProgressIndicator(
                                strokeWidth: 2, color: s.accent),
                          ),
                          const SizedBox(width: 10),
                          Flexible(
                            child: Text(
                              'working…',
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: TextStyle(
                                  color: s.inkDim, fontSize: 13, height: 1.3),
                            ),
                          ),
                        ],
                      ),
                    ),
                  if (s.running && !showOverlay) const Spacer(),
                  const SizedBox(height: 16),
                  // NO Connect button during boot: the loading screen is the
                  // spinner and its text, nothing else.
                  if (!showOverlay && s.running) _buildConnectButton(),
                  // Raised off the bottom edge (her design, 2026-10-11).
                  const SizedBox(height: 40),
                ],
              ),
              if (showOverlay) _buildSetupOverlay(),
            ],
          ),
        ),
      ),
    );
  }

  /// Connection-failure HELP, not a status-line shrug. Names the three real
  /// causes in the order they actually occur in this architecture.
  void _showConnectHelp() {
    final s = widget.appState;
    Widget item(IconData icon, String title, String detail) => Padding(
          padding: const EdgeInsets.only(bottom: 12),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Icon(icon, size: 18, color: s.accent),
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(title,
                        style: TextStyle(
                            color: s.ink,
                            fontSize: 13.5,
                            fontWeight: FontWeight.w600)),
                    Text(detail,
                        style: TextStyle(
                            color: s.inkDim, fontSize: 12.5, height: 1.35)),
                  ],
                ),
              ),
            ],
          ),
        );
    showDialog(
      context: context,
      builder: (dialogCtx) => AlertDialog(
        backgroundColor: s.surface,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
        title: Text("Couldn't reach the hub",
            style: TextStyle(
                color: s.ink, fontSize: 16, fontWeight: FontWeight.w600)),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            item(Icons.wifi, 'Check your Wi-Fi',
                "Is the phone's Wi-Fi on, and joined to the hub's network?"),
            item(Icons.power, 'Check the hub',
                'Is it powered and nearby? Give it a minute after a restart.'),
            item(Icons.vpn_lock, 'Away from home?',
                'Then Tailscale must be connected on this phone and on the hub.'),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () {
              Navigator.pop(dialogCtx);
              _showingConnectFail = false;
            },
            child: Text('Close', style: TextStyle(color: s.inkDim)),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
                backgroundColor: s.accent, foregroundColor: s.accentFg),
            onPressed: () {
              Navigator.pop(dialogCtx);
              _showingConnectFail = false;
              s.toggleConnection();
            },
            child: const Text('Try again'),
          ),
        ],
      ),
    ).then((_) => _showingConnectFail = false);
  }

  void _showLink() {
    final s = widget.appState;
    final q = s.link;
    final road = q.relayed
        ? "Tailscale is relaying this connection${q.path.length > 6 ? ' (${q.path.substring(6)})' : ''}, so it is slower than a direct one."
        : q.path == 'direct'
            ? 'Tailscale connects this phone and the hub directly.'
            : 'On the same network as the hub.';
    showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('Connection: ${s.linkWord}'),
        content: Text([
          if (q.rttMs != null) 'This phone to the hub and back: ${q.rttMs} ms.',
          if (q.hubRttMs != null) 'The hub to this phone and back: ${q.hubRttMs} ms.',
          road,
          if (s.wifiRssi != null)
            'Wi-Fi signal at this phone: ${s.wifiWord} (${s.wifiRssi} dBm).'
          else
            'This phone is not on Wi-Fi.',
          if (q.missed > 0) '${q.missed} check(s) in a row went unanswered.',
          if ((s.connectedUrl ?? '').isNotEmpty) 'Address: ${s.connectedUrl}',
        ].join('\n\n')),
        actions: [
          TextButton(
              onPressed: () => Navigator.of(ctx).pop(),
              child: const Text('OK')),
        ],
      ),
    );
  }

  Widget _buildTopBar() {
    final s = widget.appState;
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 12, 16, 0),
      child: Row(
        children: [
          if (s.running)
            Builder(
              builder: (context) => IconButton(
                icon: Icon(Icons.menu, color: s.inkDim),
                onPressed: () => Scaffold.of(context).openDrawer(),
              ),
            )
          else
            const SizedBox(width: 12),
          // THE CONNECTION, GAUGED. A dot said only "connected"; the bars
          // say how good the road is, and a tap says why (round trip, and
          // whether Tailscale is relaying). A hub that does not answer the
          // gauge's ping keeps the dot.
          if (s.connState == ConnState.ready && s.link.rttMs != null)
            _LinkBars(
                bars: s.linkBars,
                word: s.linkWord,
                dim: s.inkDim,
                onTap: _showLink)
          else
            Container(
              width: 8,
              height: 8,
              decoration: BoxDecoration(
                color: s.connState == ConnState.ready
                    ? const Color(0xFF3FB950)
                    : s.inkDim,
                shape: BoxShape.circle,
              ),
            ),
          const SizedBox(width: 8),
          Text(
            'Omniscience',
            style: TextStyle(
              color: s.inkDim,
              fontSize: 13,
              fontWeight: FontWeight.w600,
              letterSpacing: 0.02,
            ),
          ),
          const Spacer(),
          // THE DOOR IS OPEN, and for how long. A live countdown, not a
          // notice: notices are chores that wait to be done, this is a
          // security state that is true right now and stops being true on
          // its own. Hub-announced (pair_window frame) so it appears for
          // ANY opener — this phone's menu action, --pair at the hub's
          // keyboard, or the hub's own two-minute boot window — and it
          // removes itself at the deadline the hub named, or the moment
          // a phone pairs and the hub says so.
          if (s.running && s.pairWindowCountdown != null)
            Container(
              margin: const EdgeInsets.only(right: 8),
              padding:
                  const EdgeInsets.symmetric(horizontal: 9, vertical: 4),
              decoration: BoxDecoration(
                color: s.accent.withValues(alpha: 0.12),
                border: Border.all(color: s.accent),
                borderRadius: BorderRadius.circular(20),
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(Icons.phonelink_ring, size: 13, color: s.accent),
                  const SizedBox(width: 5),
                  Text(
                    'pairing ${s.pairWindowCountdown}',
                    style: TextStyle(
                        color: s.accent,
                        fontSize: 11.5,
                        fontWeight: FontWeight.w700,
                        fontFeatures: const [FontFeature.tabularFigures()]),
                  ),
                ],
              ),
            )
          else if (s.running)
            Text(
              '"${s.wakePhraseShown}"',
              style: TextStyle(color: s.inkFaint, fontSize: 12),
            ),
          // The bell. A red dot means the hub is waiting on something the
          // person has to do — an unregistered board, a device with no
          // description. Silent when there is nothing outstanding.
          if (s.running)
            IconButton(
              tooltip: 'Pending actions',
              icon: Stack(
                clipBehavior: Clip.none,
                children: [
                  Icon(
                    s.notices.isEmpty
                        ? Icons.notifications_none
                        : Icons.notifications,
                    size: 20,
                    color: s.notices.isEmpty ? s.inkDim : s.accent,
                  ),
                  // The dot is UNSEEN, not unresolved: it clears the moment the list
                  // is opened, while the messages themselves stay until their
                  // conditions clear. Seen and resolved are different things.
                  if (s.hasUnseenNotices)
                    Positioned(
                      right: -1,
                      top: -1,
                      child: Container(
                        width: 9,
                        height: 9,
                        decoration: BoxDecoration(
                          color: const Color(0xFFF85149),
                          shape: BoxShape.circle,
                          border: Border.all(color: s.bg, width: 1.5),
                        ),
                      ),
                    ),
                ],
              ),
              onPressed: () =>
                  s.openScreen('/notifications'),
            ),
          if (s.running)
            IconButton(
              tooltip: s.ttsMuted ? 'Unmute voice' : 'Mute voice',
              icon: Icon(
                s.ttsMuted ? Icons.volume_off : Icons.volume_up,
                size: 20,
                color: s.ttsMuted ? s.accent : s.inkDim,
              ),
              onPressed: () => s.toggleMute(),
            ),
        ],
      ),
    );
  }

  /// The readings section: a header row that folds, and a grid of tiles.
  ///
  /// Silent when there is nothing to show. An empty card saying "no
  /// readings" on the home screen of a house with three lamps and no
  /// sensors is a fault marker for a house with nothing wrong with it.
  Widget _buildReadings(AppState s) {
    final rows = s.homeReadings;
    if (rows.isEmpty) return const SizedBox.shrink();
    final collapsed = s.readingsCollapsed;
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 4, 16, 0),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          InkWell(
            onTap: s.toggleReadings,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(4, 12, 4, 8),
              child: Row(children: [
                Icon(Icons.show_chart, size: 15, color: s.inkFaint),
                const SizedBox(width: 6),
                // NOT 'READINGS' ANY MORE. The section carries switches
                // too now (2026-09-02), and it is named after the control
                // that puts things here — "Show on the home screen" —
                // rather than after only half of what it holds.
                Text('ON HOME',
                    style: TextStyle(
                        color: s.inkFaint,
                        fontSize: 11,
                        letterSpacing: 0.09 * 11,
                        fontWeight: FontWeight.w600)),
                const SizedBox(width: 6),
                Expanded(child: Container(height: 1, color: s.border)),
                const SizedBox(width: 6),
                Text('${rows.length}',
                    style: TextStyle(color: s.inkFaint, fontSize: 11)),
                Icon(
                    collapsed
                        ? Icons.keyboard_arrow_down
                        : Icons.keyboard_arrow_up,
                    size: 15,
                    color: s.inkFaint),
              ]),
            ),
          ),
          // EVERY TILE, six to a page: swipe sideways for the rest, the
          // dots say how many pages (it used to show the first six only).
          if (!collapsed) _readingPages(s, rows),
        ],
      ),
    );
  }

  /// Cards come in swipeable pages of six with dots, so the orb stays put.
  static const int _perPage = 6;
  final ValueNotifier<int> _readingPage = ValueNotifier<int>(0);

  Widget _readingPages(AppState s, List<Map<String, dynamic>> rows) {
    // Wide and short, so the orb is not pushed down.
    const aspect = 1.78, gap = 12.0;
    Widget grid(List<Map<String, dynamic>> page) => GridView.count(
          crossAxisCount: 2,
          shrinkWrap: true,
          physics: const NeverScrollableScrollPhysics(),
          padding: EdgeInsets.zero,
          mainAxisSpacing: gap,
          crossAxisSpacing: gap,
          childAspectRatio: aspect,
          children: [for (final r in page) _readingTile(s, r)],
        );
    if (rows.length <= _perPage) return grid(rows);
    final pages = <List<Map<String, dynamic>>>[
      for (var i = 0; i < rows.length; i += _perPage)
        rows.sublist(i, min(i + _perPage, rows.length)),
    ];
    return LayoutBuilder(builder: (context, box) {
      final tileH = (box.maxWidth - gap) / 2 / aspect;
      final height = 3 * tileH + 2 * gap;     // always room for a full page
      return Column(children: [
        SizedBox(
          height: height,
          child: PageView.builder(
            itemCount: pages.length,
            onPageChanged: (i) => _readingPage.value = i,
            itemBuilder: (_, i) => Align(
                alignment: Alignment.topCenter, child: grid(pages[i])),
          ),
        ),
        const SizedBox(height: 8),
        ValueListenableBuilder<int>(
          valueListenable: _readingPage,
          builder: (_, page, __) {
            final at = page.clamp(0, pages.length - 1);
            return Row(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                for (var i = 0; i < pages.length; i++)
                  AnimatedContainer(
                    duration: const Duration(milliseconds: 200),
                    margin: const EdgeInsets.symmetric(horizontal: 3),
                    width: i == at ? 16 : 6,
                    height: 6,
                    decoration: BoxDecoration(
                      color: i == at ? s.accent : s.border,
                      borderRadius: BorderRadius.circular(3),
                    ),
                  ),
              ],
            );
          },
        ),
      ]);
    });
  }

  Widget _readingTile(AppState s, Map<String, dynamic> r) {
    final unit = (r['unit'] as String?) ?? '';
    final room = (r['room'] as String?) ?? '';
    // A SWITCH READS DIFFERENTLY FROM A MEASUREMENT. Same card, so the
    // grid stays one grid, but a lit dot says at a glance which of them
    // are on — which is the only reason to put a switch here at all.
    // Absent on an older hub, where every row is a reading.
    final isDevice = (r['kind'] as String?) == 'device';
    final isOn = isDevice && (r['value'] as String?) == 'on';
    return Container(
      padding: const EdgeInsets.fromLTRB(14, 13, 14, 13),
      decoration: BoxDecoration(
        color: s.surface,
        border: Border.all(color: s.border),
        borderRadius: BorderRadius.circular(14),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Row(children: [
            if (isDevice) ...[
              Container(
                width: 7,
                height: 7,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: isOn ? s.accent : s.inkFaint,
                ),
              ),
              const SizedBox(width: 6),
            ],
            Expanded(
              child: Text((r['name'] as String?) ?? '?',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                      color: s.ink,
                      fontSize: 12.5,
                      fontWeight: FontWeight.w600)),
            ),
          ]),
          const SizedBox(height: 5),
          Row(crossAxisAlignment: CrossAxisAlignment.baseline,
              textBaseline: TextBaseline.alphabetic, children: [
            // A DASH IS NOT A NUMBER, and it should not be dressed as one.
            // The hub sends "—" for a reading the board has not sent yet,
            // rather than a confident 0 — so the tile shows the channel
            // exists and says plainly that nothing has arrived on it.
            Text((r['value'] as String?) ?? '—',
                style: TextStyle(
                    color: r['reporting'] == false ? s.inkFaint : s.ink,
                    fontSize: 21,
                    fontWeight: FontWeight.w600,
                    fontFeatures: const [FontFeature.tabularFigures()])),
            if (unit.isNotEmpty) ...[
              const SizedBox(width: 4),
              Text(unit,
                  style: TextStyle(color: s.inkDim, fontSize: 12)),
            ],
          ]),
          if (room.isNotEmpty) ...[
            const SizedBox(height: 5),
            Text(room,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(color: s.inkFaint, fontSize: 11.5)),
          ],
        ],
      ),
    );
  }

  Widget _buildBootSpinner() {
    final s = widget.appState;
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          SizedBox(
            width: 44,
            height: 44,
            child: CircularProgressIndicator(
              color: s.accent,
              backgroundColor: s.border,
              strokeWidth: 3,
            ),
          ),
          const SizedBox(height: 20),
          Text(
            s.status,
            textAlign: TextAlign.center,
            style: TextStyle(
              color: s.inkDim,
              fontSize: 15,
              fontWeight: FontWeight.w500,
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildConnectButton() {
    final s = widget.appState;
    // FIX: derive "connecting" from the connection state machine, not from a
    // status-string comparison that any onStatus() overwrite defeated.
    final isConnecting = s.isConnecting;
    // No hub yet (pairing was put off): the same place offers pairing.
    if (s.unpaired) {
      return Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16),
        child: SizedBox(
          width: double.infinity,
          child: FilledButton.icon(
            style: FilledButton.styleFrom(
              backgroundColor: s.connectBg,
              foregroundColor: s.connectFg,
              padding: const EdgeInsets.symmetric(vertical: 14),
              shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12)),
            ),
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute(
                settings: const RouteSettings(name: '/pair'),
                builder: (_) => PairScreen(state: s),
              ),
            ),
            icon: const Icon(Icons.link, size: 20),
            label: const Text('Pair this phone',
                style: TextStyle(fontWeight: FontWeight.w700, fontSize: 15)),
          ),
        ),
      );
    }
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16),
      child: SizedBox(
        width: double.infinity,
        child: s.connState == ConnState.ready
            ? _buildDisconnectButton()
            : _buildConnectInner(isConnecting),
      ),
    );
  }

  Widget _buildConnectInner(bool isConnecting) {
    final s = widget.appState;
    // Deep teal at rest (light mode), light deep teal while connecting.
    final bg = isConnecting ? s.connectingBg : s.connectBg;
    final fg = isConnecting ? s.connectingFg : s.connectFg;
    return FilledButton.icon(
      style: FilledButton.styleFrom(
        backgroundColor: bg,
        foregroundColor: fg,
        // FIX: a disabled FilledButton ignores backgroundColor and paints
        // Material's grey — the connecting tint never showed. Pin the
        // disabled colors to the same connecting pair.
        disabledBackgroundColor: bg,
        disabledForegroundColor: fg,
        padding: const EdgeInsets.symmetric(vertical: 14),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      ),
      onPressed: isConnecting ? null : () => s.toggleConnection(),
      icon: isConnecting
          ? SizedBox(
              width: 20,
              height: 20,
              child: CircularProgressIndicator(strokeWidth: 2.5, color: fg),
            )
          : const Icon(Icons.play_arrow, size: 20),
      label: isConnecting
          ? const _AnimatedConnectingText()
          : const Text('Connect', style: TextStyle(fontWeight: FontWeight.w700, fontSize: 15)),
    );
  }

  Widget _buildDisconnectButton() {
    return OutlinedButton.icon(
      style: ButtonStyle(
        backgroundColor: WidgetStateProperty.resolveWith((states) {
          if (states.contains(WidgetState.pressed)) return const Color(0xFF800020);
          return Colors.transparent;
        }),
        foregroundColor: WidgetStateProperty.resolveWith((states) {
          if (states.contains(WidgetState.pressed)) return const Color(0xFFFFFFFF);
          return const Color(0xFFF85149);
        }),
        side: WidgetStateProperty.resolveWith((states) {
          if (states.contains(WidgetState.pressed)) {
            return const BorderSide(color: Color(0xFF800020), width: 1.5);
          }
          return const BorderSide(color: Color(0xFFF85149), width: 1.5);
        }),
        padding: WidgetStateProperty.all(const EdgeInsets.symmetric(vertical: 14)),
        shape: WidgetStateProperty.all(RoundedRectangleBorder(borderRadius: BorderRadius.circular(12))),
      ),
      onPressed: () => widget.appState.toggleConnection(),
      icon: const Icon(Icons.stop, size: 20),
      label: const Text('Disconnect', style: TextStyle(fontWeight: FontWeight.w700, fontSize: 15)),
    );
  }

  Widget _buildSetupOverlay() {
    final s = widget.appState;
    return Container(
      color: s.bg.withValues(alpha: 0.97),
      padding: const EdgeInsets.all(24),
      child: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 400),
          child: Container(
            padding: const EdgeInsets.all(24),
            decoration: BoxDecoration(
              color: s.surface,
              borderRadius: BorderRadius.circular(12),
              border: Border.all(color: s.border),
            ),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                if (s.blockingProblem != null) ...[
                  const Icon(Icons.error_outline, color: Color(0xFFF85149), size: 48),
                  const SizedBox(height: 16),
                  Text(
                    s.blockingProblem!,
                    textAlign: TextAlign.center,
                    style: TextStyle(color: s.ink, fontSize: 15, height: 1.4),
                  ),
                  const SizedBox(height: 20),
                  // A WAY BACK IN. The problem used to sit here with no
                  // button: fix the permission, return, and nothing
                  // re-tried until the app was killed.
                  FilledButton.icon(
                    style: FilledButton.styleFrom(
                      backgroundColor: s.accent,
                      foregroundColor: s.accentFg,
                      padding: const EdgeInsets.symmetric(vertical: 14),
                    ),
                    onPressed: () => s.retryBoot(),
                    icon: const Icon(Icons.refresh, size: 20),
                    label: const Text('Retry',
                        style: TextStyle(fontWeight: FontWeight.w600)),
                  ),
                ] else ...[
                  Text(
                    'Finish setup',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: s.ink, fontSize: 17, fontWeight: FontWeight.w600),
                  ),
                  const SizedBox(height: 4),
                  Text(
                    'This phone needs pairing with your hub before it can '
                    'talk to the house.',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: s.inkDim, fontSize: 13),
                  ),
                  const SizedBox(height: 20),
                  FilledButton(
                    // HER PALETTE, NOT A LEFTOVER: this was the last
                    // hardcoded blue in the pairing journey ("the pair
                    // phone should carry the theme not blue botton",
                    // field test 2026-08-27).
                    style: FilledButton.styleFrom(
                      backgroundColor: s.accent,
                      foregroundColor: s.accentFg,
                      padding: const EdgeInsets.symmetric(vertical: 14),
                    ),
                    // The exchange lives on its own screen -- several steps,
                    // several distinct failures, each needing room to say
                    // what went wrong. See PairScreen.
                    // NAMED, like every other screen: an unnamed route
                    // leaves topRoute reading whatever was underneath, so
                    // the dock and every topRoute guard believed they were
                    // still on the screen behind this one.
                    onPressed: () => Navigator.of(context).push(
                      MaterialPageRoute(
                        settings: const RouteSettings(name: '/pair'),
                        builder: (_) => PairScreen(state: s),
                      ),
                    ),
                    child: const Text('Pair this phone',
                        style: TextStyle(fontWeight: FontWeight.w600)),
                  ),
                  // Escape hatch for an accidental "Pair with a hub" -- or,
                  // on a phone with no hub yet, a way into the app without
                  // one (see AppState.dismissPairing).
                  ...[
                    const SizedBox(height: 8),
                    TextButton(
                      onPressed: () => s.dismissPairing(),
                      child: Text(
                          s.running && !s.unpaired
                              ? 'Stay paired as I am'
                              : 'Later',
                          style:
                              TextStyle(color: s.inkDim, fontSize: 13.5)),
                    ),
                  ],
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildDrawer() {
    final s = widget.appState;
    // FIX: Added s.challengePhrase == null. During a security challenge the
    // shell lock is held by the active command; letting the user fire a menu
    // action from the drawer would race and be rejected by the server anyway.
    final canManage = s.running &&
        s.connState == ConnState.ready &&
        s.pendingPrompt == null &&
        s.pendingChoice == null &&
        s.pendingRecord == null &&
        s.challengePhrase == null &&
        !s.isStarting;

    return Drawer(
      backgroundColor: s.surface,
      child: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Row(
              children: [
                Expanded(
                  child: Text('Manage', style: TextStyle(fontSize: 15, fontWeight: FontWeight.w600, color: s.ink)),
                ),
                IconButton(
                  icon: Icon(Icons.close, color: s.inkDim),
                  onPressed: () => Navigator.pop(context),
                ),
              ],
            ),
            // A NAMEPLATE, NOT A BUTTON (her ruling, 2026-08-29).
            //
            // It answers a question the drawer could not answer at all
            // before -- whose voice is this phone bound to? -- and then
            // stops. No InkWell, no border, no chevron, no ripple: every
            // one of those is a promise that tapping does something, and
            // the way to the Profile screen is deliberately Settings ›
            // Profile, one door at each step.
            //
            // The name comes from the SPEAKER the hub resolved, not from
            // the phone record's `name`: those are two different identities
            // (see phones.bind_user) and the one this line is about is the
            // person.
            if (s.boundUser.isNotEmpty) ...[
              const SizedBox(height: 6),
              Padding(
                padding: const EdgeInsets.fromLTRB(12, 6, 12, 12),
                child: Row(
                  children: [
                    Container(
                      width: 32,
                      height: 32,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        border: Border.all(color: s.accent, width: 1.5),
                      ),
                      alignment: Alignment.center,
                      child: Text(
                        s.boundUser.substring(0, 1).toUpperCase(),
                        style: TextStyle(
                            color: s.accent,
                            fontSize: 14,
                            fontWeight: FontWeight.w600),
                      ),
                    ),
                    const SizedBox(width: 11),
                    Expanded(
                      child: Text(s.boundUser,
                          style: TextStyle(
                              fontSize: 14.5,
                              fontWeight: FontWeight.w600,
                              color: s.ink)),
                    ),
                  ],
                ),
              ),
              Container(height: 1, color: s.border),
            ],
            const SizedBox(height: 4),
            InkWell(
              onTap: () {
                s.cycleTheme();
                Navigator.pop(context);
              },
              borderRadius: BorderRadius.circular(8),
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                decoration: BoxDecoration(color: s.bg, border: Border.all(color: s.border), borderRadius: BorderRadius.circular(8)),
                child: Row(
                  children: [
                    Icon(s.isLight ? Icons.wb_sunny : Icons.nights_stay, size: 18,
                        color: s.isLight ? const Color(0xFFD29922) : s.accent),
                    const SizedBox(width: 10),
                    Text(s.isLight ? 'Light mode' : 'Dark mode', style: TextStyle(fontSize: 13, fontWeight: FontWeight.w500, color: s.ink)),
                    const Spacer(),
                    Icon(Icons.chevron_right, size: 18, color: s.inkDim),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 16),
            _drawerGroup('PENDING'),
            InkWell(
              onTap: () {
                Navigator.pop(context);
                s.openScreen('/notifications');
              },
              borderRadius: BorderRadius.circular(8),
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                child: Row(
                  children: [
                    Icon(
                      s.notices.isEmpty
                          ? Icons.notifications_none
                          : Icons.notifications,
                      size: 16,
                      color: s.notices.isEmpty ? s.inkDim : s.accent,
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Text(
                        s.notices.isEmpty
                            ? 'Nothing outstanding'
                            : '${s.notices.length} pending action'
                                '${s.notices.length == 1 ? '' : 's'}',
                        style: TextStyle(fontSize: 13, color: s.ink),
                      ),
                    ),
                    if (s.hasUnseenNotices)
                      Container(
                        width: 8,
                        height: 8,
                        decoration: const BoxDecoration(
                          color: Color(0xFFF85149),
                          shape: BoxShape.circle,
                        ),
                      ),
                  ],
                ),
              ),
            ),
            // NO CONNECTION GROUP HERE ANY MORE (2026-09-08). Everything
            // about reaching the hub -- which address is in use, the list
            // the phone dials, remote access -- lives under Settings ->
            // NETWORK. One place, not two.
            _drawerGroup('ACTIONS'),
            // Settings sits HERE, with the other things you tap to go
            // somewhere. Reaching the hub at all -- the address list, remote
            // access -- is inside it, under NETWORK. It looks and behaves
            // like every other row in this list.
            ListTile(
              dense: true,
              title: Text('Settings',
                  style: TextStyle(fontSize: 14, color: s.ink)),
              onTap: () {
                Navigator.pop(context);
                s.openScreen('/settings');
              },
            ),
            for (final action in [
              // ONE ROW, NOT FOUR — the third and last of the merges. The
              // Devices screen carries all of it now: Add from the button,
              // edit by tapping a device, remove from its detail sheet,
              // and the BOARDS section plus the overflow for the two
              // board-wide acts ("Update device → A board" and the Wi-Fi
              // every board joins). Losing that fork is the one thing a
              // drawer merge could quietly have broken.
              ('Devices', 'list_devices'),
              // ONE ROW, NOT FOUR (her merge, extended to scenes
              // 2026-08-27). Same reasoning as Automations below: the
              // Scenes screen IS the feature now — New from the button,
              // edit by tapping a scene, remove from its own menu. The hub
              // keeps all four doors, because they are how VOICE reaches
              // the same capability and a spoken "add a scene" has nothing
              // to point at.
              ('Scenes', 'list_scenes'),
              // Automations (2026-08-26): a trigger stapled onto a scene —
              // "when the kitchen is quiet for ten minutes, run kitchen
              // off". Authored here, evaluated on the hub's own MQTT.
              //
              // ONE ROW, NOT FOUR (her merge, 2026-08-27). List / Add /
              // Update / Remove were four doors into one thing, from the
              // era when each was its own question flow. The Automations
              // screen IS the feature now: new from the button, edit by
              // tapping a rule, pause with its switch, remove from its
              // menu. The hub keeps all four doors — they are how VOICE
              // reaches the same capability — but a drawer that lists a
              // verb per door is a menu describing the implementation.
              ('Automations', 'list_automations'),
              ('Statistics', 'readings'),
              // 'Enroll a voice' IS GONE FROM HERE (her ruling, 2026-08-29).
              //
              // It sent exactly the action Profile's "Add another person"
              // sends, so it was a second door to one capability -- and the
              // weaker of the two: there, the button sits UNDER the list of
              // who is already enrolled, which is the thing that stops
              // somebody adding "Augusta" a second time. A drawer row can
              // show no such list.
              //
              // Reached now at Settings › Profile › Add another person. The
              // hub keeps `enroll_voice` in MENU_ACTIONS, because that is
              // how VOICE gets there and a spoken "enrol a voice" has
              // nothing to tap.
              // Set PIN and Forgot PIN moved to Settings. They are not tasks
              // you do to a device, they are configuration of the hub -- and
              // sitting between "Add device" and "Learn IR codes" is how a
              // security setting starts reading like a daily chore.
              // Learning a remote lives on the infrared receiver's page in
              // Devices (and starts right after the receiver is added).
            ])
              ListTile(
                dense: true,
                enabled: canManage,
                title: Text(action.$1, style: TextStyle(fontSize: 14, color: s.ink)),
                // THE HUB'S GATED SET IS THE TRUTH, mirrored here by action
                // id — never guessed from the label. The label guess missed
                // "Enroll a voice" and "Learn IR codes", which the hub DOES
                // gate: rows with no badge were asking for the PIN ("even
                // the ones without pin marked on them", field test
                // 2026-08-26). If the hub's MENU_ACTIONS gating changes,
                // change this set in the same commit. Only the rows this
                // list actually draws: of those, learn_ir alone is gated
                // (the list_* doors and readings are not).
                trailing: const {'learn_ir'}.contains(action.$2)
                    ? Container(
                        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                        decoration: BoxDecoration(border: Border.all(color: const Color(0xFFD29922)), borderRadius: BorderRadius.circular(4)),
                        child: const Text('PIN', style: TextStyle(fontSize: 10, color: Color(0xFFD29922))),
                      )
                    : null,
                onTap: () {
                  // list_devices, list_scenes and readings get their own
                  // structured screens; everything else stays server-driven.
                  //
                  // "Statistics" sends `readings`. The action it used to send
                  // was `statistics`, which ran a hub method that counted
                  // devices and rooms and never read a measurement -- that
                  // method is gone, and this row now opens the screen that
                  // shows what the sensors actually say.
                  if (action.$2 == 'list_devices') {
                    s.requestDeviceList();
                  } else if (action.$2 == 'list_scenes') {
                    s.requestSceneList();
                  } else if (action.$2 == 'list_automations') {
                    // The rules get their own screen too -- and it is the
                    // way in to the block builder (tap a rule to edit it).
                    s.requestAutomationList();
                  } else if (action.$2 == 'readings') {
                    s.requestReadings();
                  } else {
                    s.sendMenuAction(action.$2);
                  }
                  Navigator.pop(context);
                },
              ),
            const SizedBox(height: 20),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 4),
              child: Text(
                canManage
                    ? 'Actions marked PIN verify you once, then trust you '
                        'for a few minutes.'
                    // Not connected first — see settings_screen for why
                    // `running` cannot tell the two apart.
                    : (s.connState != ConnState.ready
                        ? 'Not connected to the hub yet.'
                        : 'Busy — finish what\'s on screen first.'),
                style: TextStyle(fontSize: 12, color: s.inkFaint),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _drawerGroup(String title) {
    final s = widget.appState;
    return Padding(
      padding: const EdgeInsets.fromLTRB(4, 12, 4, 6),
      child: Text(
        title,
        style: TextStyle(fontSize: 11, letterSpacing: 0.09, fontWeight: FontWeight.w600, color: s.inkFaint),
      ),
    );
  }
}

class _AnimatedConnectingText extends StatefulWidget {
  const _AnimatedConnectingText();

  @override
  State<_AnimatedConnectingText> createState() => _AnimatedConnectingTextState();
}

class _AnimatedConnectingTextState extends State<_AnimatedConnectingText>
    with SingleTickerProviderStateMixin {
  late AnimationController _controller;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      duration: const Duration(milliseconds: 1400),
      vsync: this,
    )..repeat();
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _controller,
      builder: (_, __) {
        final dots = (_controller.value * 4).floor() % 4;
        return Text(
          'Connecting${'.' * dots}',
          style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 15),
        );
      },
    );
  }
}

/// Four bars for the connection gauge: green when good, amber when slow,
/// red when poor or unanswered. The rest are drawn dim.
class _LinkBars extends StatelessWidget {
  const _LinkBars(
      {required this.bars,
      required this.word,
      required this.dim,
      required this.onTap});

  /// 0-4: the worse of the hub's answer time and the Wi-Fi signal.
  final int bars;
  final String word;
  final Color dim;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final lit = bars >= 3
        ? const Color(0xFF3FB950)
        : bars == 2
            ? const Color(0xFFD29922)
            : const Color(0xFFF85149);
    return Semantics(
      label: 'Connection $word',
      button: true,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(6),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 6),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              for (var i = 1; i <= 4; i++)
                Container(
                  width: 3,
                  height: 3.0 + 3 * i,
                  margin: const EdgeInsets.only(right: 2),
                  decoration: BoxDecoration(
                    // With no answer at all, one red stub still shows.
                    color: (i <= bars || (bars == 0 && i == 1))
                        ? lit
                        : dim.withValues(alpha: 0.35),
                    borderRadius: BorderRadius.circular(1),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
