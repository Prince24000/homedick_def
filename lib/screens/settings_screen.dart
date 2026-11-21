/// One place for everything that is CONFIGURATION rather than a task.
///
/// These used to be scattered: Set PIN and Forgot PIN sat in the drawer's
/// ACTIONS list alongside "Add device", which put a security setting next to
/// a daily chore; "Change shared secret" was a separate drawer row; the ESP
/// Wi-Fi was reachable only as the first question of "build a new ESP board",
/// so changing a password meant starting to describe hardware you did not
/// want; and the ESPHome key had no home at all — it was pushed at you
/// mid-flow on a card titled "Recovery phrase".
///
/// The rule that decides what belongs here: a task acts on a DEVICE, a
/// setting acts on the HUB. Add device, scenes and IR learning are tasks.
/// The PIN, the shared secret, the ESP Wi-Fi and the board credentials are
/// settings — each one is about this hub, not about any one thing plugged
/// into it.
///
/// A second rule arrived with the boards, and it is sharper: SETTINGS NEVER
/// CREATE ANYTHING. Add device is the only front door, including for a
/// brand-new board. Everything here changes something that already exists.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:homevoice/socket_client.dart';   // ConnState

import '../app_state.dart';
import '../widgets/voice_ui.dart';
import 'network_editor.dart';
import 'pair_screen.dart';

class SettingsScreen extends StatelessWidget {
  final AppState appState;
  const SettingsScreen({super.key, required this.appState});

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: appState,
      builder: (context, _) => _build(context),
    );
  }

  Widget _build(BuildContext context) {
    final s = appState;
    // THE SAME EXPRESSION THE DRAWER USES. Not a paraphrase of it.
    //
    // I first wrote `!s.running`, reading "running" as "busy". It is not --
    // it means the socket client has been STARTED. So the condition was
    // false the moment the app connected, and every row on this screen was
    // permanently greyed out. Copied verbatim now, because the one thing
    // worse than duplicated logic is duplicated logic that disagrees.
    final canManage = s.running &&
        s.connState == ConnState.ready &&
        s.pendingPrompt == null &&
        s.pendingChoice == null &&
        s.pendingRecord == null &&
        s.challengePhrase == null &&
        !s.isStarting;

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
          title: Text('Settings',
              style: TextStyle(
                  color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
        ),
        body: SafeArea(
          child: VoiceUiScope(
            // "Scroll down" by voice, like every list screen.
            state: s,
            builder: (ctx, scroll) => ListView(
              controller: scroll,
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 90),
            children: [
              // THE ONLY DOOR TO PROFILE (her ruling, 2026-08-29): the
              // drawer's nameplate opens nothing, the drawer's Settings row
              // lands here, and this row carries you the rest of the way.
              // One door at each step.
              _group(s, 'YOU'),
              _row(
                s,
                icon: Icons.person_outline,
                title: 'Profile',
                // NOT gated, because opening it proves nothing. Every row
                // INSIDE it is gated exactly as the action it sends always
                // was -- the badge belongs to the change, not to the door.
                enabled: canManage,
                // NO POP FIRST (audit, 2026-09-12). Settings used to
                // close itself before starting anything, which destroyed
                // the return address: Profile then opened over HOME, and
                // Back from it left you on Home instead of back here.
                // _popFlowRoutes exists precisely so a flow started from
                // Settings ends on Settings -- it cannot honour that if
                // Settings is already gone.
                onTap: () => s.requestProfile(),
              ),
              _group(s, 'THIS PHONE'),
              // EVERY HUB THIS PHONE IS PAIRED WITH (2026-09-24): which one
              // is in use, a tap to switch, and forgetting one. Not gated
              // and not server-driven, like pairing: switching is how you
              // reach a hub while another one is unreachable.
              _row(
                s,
                icon: Icons.hub_outlined,
                title: 'Hubs',
                value: s.hubList.isEmpty ? '' : s.activeHubLabel,
                enabled: s.hubList.isNotEmpty,
                onTap: () => _hubsDialog(context, s),
              ),
              _row(
                s,
                icon: Icons.phonelink_lock_outlined,
                title: s.hubList.isEmpty ? 'Pair with a hub' : 'Pair another hub',
                // NOT gated and NOT server-driven: pairing is the one thing
                // you must be able to reach while the connection is
                // REFUSING you -- a revoked phone repairs itself here.
                enabled: true,
                // Pushed WITH A NAME, and without popping Settings
                // first. The name matters: an unnamed route leaves
                // topRoute reading '/settings', so the voice dock drew
                // itself over the pairing screen and main.dart reserved
                // 72px at its foot -- the same screen looked different
                // depending on which door you came through, because from
                // Home topRoute read '/' and the dock hides there.
                onTap: () => Navigator.of(context).push(MaterialPageRoute(
                      settings: const RouteSettings(name: '/pair'),
                      builder: (_) => PairScreen(state: s),
                    )),
              ),
              _row(
                s,
                icon: Icons.link_off,
                title: 'Unpair from this hub',
                // NOT gated by the connection: unpairing is what you do
                // when the hub is refusing you.
                enabled: true,
                onTap: () => _confirmUnpair(context, s),
              ),
              _row(
                s,
                icon: Icons.phone_android_outlined,
                title: 'Pair a new phone',
                // NO NUMBER HERE. The window's length is the hub's to
                // decide, and this row cannot check it -- it typed "two
                // minutes" and would have gone on saying so after the
                // hub moved to five. The live countdown, which the hub
                // DOES send, is on the home bar where it is true.
                // Gated: household authority. The voice/PIN check this runs
                // is what lets a verified person at a phone do what used to
                // need SSH into a headless Pi.
                enabled: canManage,
                gated: true,
                onTap: () => _run(context, s, 'pair_phone'),
              ),
              // THE SPEECH MODELS, FOR ANOTHER PHONE: copied to
              // Download/HomeVoice models, to send. On that phone the
              // "Add the model files" button takes them. Not gated: they
              // are the public model files, nothing of this house.
              _row(
                s,
                icon: Icons.download_outlined,
                title: 'Copy speech models to Download',
                enabled: true,
                onTap: () => s.copyModelsToDownload(),
              ),
              // THE WHOLE SECURITY GROUP IS GONE, and this screen is
              // smaller for it (her ruling, 2026-08-29).
              //
              // "Set PIN" and "Forgot PIN" were the profile wearing a
              // settings hat. They are now the PIN row and the Recovery
              // words row on the Profile screen -- rows that SHOW the state
              // and are the way to change it, so you change your PIN on the
              // row that says the PIN is set.
              //
              // Keeping copies here would have been two doors to one
              // security change, which is the fork the drawer merges
              // existed to remove -- and on a credential it is worse than
              // untidy: two doors drift, and one ends up with the weaker
              // lock.
              //
              // The hub keeps `set_pin` and `forgot_pin` in MENU_ACTIONS
              // either way; that is how VOICE reaches them, and a spoken
              // "set my PIN" has nothing to tap.
              // FIRMWARE FROM THIS PHONE: Tasmota images picked here and
              // installed on the hub, for a hub with no internet. Gated:
              // they replace what every board is flashed with.
              _row(
                s,
                icon: Icons.memory_outlined,
                title: 'Install firmware on hub',
                enabled: canManage,
                gated: true,
                onTap: () => s.installFirmwareOnHub(),
              ),
              // THE HUB'S GITHUB LINK: the token and the fork it builds on,
              // a check that they work, and the standard firmware built or
              // fetched without opening GitHub. Gated: the hub keeps a
              // credential here, and a fetched build replaces what every
              // board is flashed with. The token is typed into the hub's
              // secret field and never shown back.
              _row(
                s,
                icon: Icons.cloud_sync_outlined,
                title: 'GitHub builds',
                enabled: canManage,
                gated: true,
                onTap: () => _run(context, s, 'github_builds'),
              ),
              _group(s, 'NETWORK'),
              // WHERE THIS PHONE LOOKS FOR THE HUB. Moved here from the
              // home drawer (2026-09-08): it is a setting, and this is the
              // screen for settings. Not gated and not server-driven --
              // like pairing, it must be reachable while the connection is
              // failing, because it is how you fix the connection.
              _row(
                s,
                icon: Icons.settings_ethernet,
                title: 'Server addresses',
                value: s.connectedUrl != null
                    ? Uri.parse(s.connectedUrl!).host
                    : '',
                enabled: true,
                onTap: () => showNetworkEditor(context, s),
              ),
              _row(
                s,
                icon: Icons.public_outlined,
                title: 'Remote access',
                // ALWAYS ENABLED, deliberately: this is the row you reach
                // for when you are cut off.
                enabled: true,
                // ...and BADGED, because the hub PIN-gates it
                // (confirm_with_pin, "change remote access"). The badge
                // was missing while the footer two rows down promised
                // "Items marked PIN will ask you to verify first" — so the
                // one row that asks without warning was the one with no
                // warning on it (audit, 2026-09-12).
                gated: true,
                onTap: () => _tailscaleDialog(context, s),
              ),
              // NO "ESP BOARDS" GROUP ANY MORE. "Board credentials" was the
              // last row here, kept on the argument that reading a
              // password back is a hub secret rather than a board change.
              // Her ruling (2026-09-11): a board's passwords are ABOUT THE
              // BOARD, and there is one door to everything about a board --
              // tap it on the Devices screen. Its sheet shows what it is,
              // its parts and their drivers, the devices on it, and its
              // passwords behind the same PIN this row asked for. Two
              // doors to one popup was the duplication the merges exist
              // to remove, and the popup itself had misled: "Username:
              // hv-kitchen" above BOTH passwords, when the web page's user
              // is admin.
              _group(s, 'VOICE'),
              _switchRow(
                s,
                icon: Icons.explore_outlined,
                title: 'Voice navigation',
                value: s.voiceNavigation,
                onChanged: (v) => s.setVoiceNavigation(v),
              ),
              const SizedBox(height: 18),
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 4),
                child: Text(
                  // NOT CONNECTED is asked FIRST. `running` is set once at
                  // startup and cleared only by unpairing, so it cannot
                  // tell "the hub is mid-flow" from "the phone cannot
                  // reach the hub" — and the second is the common one.
                  // The old order told you to finish what was on screen
                  // when nothing was (audit, 2026-09-12).
                  canManage
                      ? ''
                      : (s.connState != ConnState.ready
                          ? 'Not connected to the hub yet.'
                          : 'Busy — finish what is on screen first.'),
                  style: TextStyle(color: s.inkFaint, fontSize: 12),
                ),
              ),
            ],
          )),
        ),
      ),
    );
  }

  Widget _switchRow(
    AppState s, {
    required IconData icon,
    required String title,
    required bool value,
    required ValueChanged<bool> onChanged,
  }) {
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      // THE PROFILE BOX'S SIZE, on every row (her design, 2026-10-11).
      constraints: const BoxConstraints(minHeight: _rowHeight),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
      decoration: BoxDecoration(
        color: s.surface,
        border: Border.all(color: s.border),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        children: [
          Icon(icon, size: 19, color: s.inkDim),
          const SizedBox(width: 13),
          Expanded(
            child: Text(title,
                style: TextStyle(
                    color: s.ink,
                    fontSize: 14.5,
                    fontWeight: FontWeight.w600)),
          ),
          Switch(value: value, activeThumbColor: s.accent, onChanged: onChanged),
        ],
      ),
    );
  }

  /// NO POP. The flow's question screens push OVER Settings and
  /// _popFlowRoutes unwinds back to it when they end -- which is the
  /// documented contract ("a flow started from Settings now ends back ON
  /// Settings"). Popping here first broke that promise before it could
  /// apply, and every flow launched from this screen finished on Home
  /// (audit, 2026-09-12).
  void _run(BuildContext context, AppState s, String action) {
    s.sendMenuAction(action);
  }

  /// One height for every box on Settings and Profile (her design,
  /// 2026-10-11): the size the Profile boxes had with two lines in them.
  static const double _rowHeight = 64;

  Widget _group(AppState s, String label) => Padding(
        padding: const EdgeInsets.only(top: 18, bottom: 6, left: 4),
        child: Text(label,
            style: TextStyle(
                color: s.inkFaint,
                fontSize: 11,
                letterSpacing: 0.8,
                fontWeight: FontWeight.w700)),
      );

  /// A row: its name, and -- only where there is one -- its current
  /// setting at the right ("Built-in", "Nova"). NO DESCRIPTIONS (her
  /// ruling, 2026-10-11): what a row is for is in its name.
  Widget _row(
    AppState s, {
    required IconData icon,
    required String title,
    String value = '',
    required bool enabled,
    required VoidCallback onTap,
    bool gated = false,
  }) {
    return Opacity(
      opacity: enabled ? 1 : 0.45,
      child: InkWell(
        onTap: enabled ? onTap : null,
        borderRadius: BorderRadius.circular(12),
        child: Container(
          margin: const EdgeInsets.only(bottom: 8),
          constraints: const BoxConstraints(minHeight: _rowHeight),
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
          decoration: BoxDecoration(
            color: s.surface,
            border: Border.all(color: s.border),
            borderRadius: BorderRadius.circular(12),
          ),
          child: Row(
            children: [
              Icon(icon, size: 19, color: s.inkDim),
              const SizedBox(width: 13),
              Expanded(
                child: Text(title,
                    style: TextStyle(
                        color: s.ink,
                        fontSize: 14.5,
                        fontWeight: FontWeight.w600)),
              ),
              if (value.isNotEmpty)
                Flexible(
                  child: Padding(
                    padding: const EdgeInsets.only(left: 8, right: 4),
                    child: Text(value,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        textAlign: TextAlign.right,
                        style: TextStyle(color: s.inkDim, fontSize: 13)),
                  ),
                ),
              if (gated)
                Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                  decoration: BoxDecoration(
                      border: Border.all(color: const Color(0xFFD29922)),
                      borderRadius: BorderRadius.circular(4)),
                  child: const Text('PIN',
                      style: TextStyle(
                          fontSize: 10, color: Color(0xFFD29922))),
                ),
              const SizedBox(width: 6),
              Icon(Icons.chevron_right, size: 18, color: s.inkFaint),
            ],
          ),
        ),
      ),
    );
  }
}


/// Say what unpairing costs before doing it, then leave the person on the
/// pair screen -- the only place that can make this phone useful again.
Future<void> _confirmUnpair(BuildContext context, AppState s) async {
  final go = await showDialog<bool>(
    context: context,
    builder: (ctx) => AlertDialog(
      backgroundColor: s.surface,
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(16),
          side: BorderSide(color: s.border)),
      title: Text(
          s.hubList.length > 1
              ? 'Unpair from ${s.activeHubLabel}?'
              : 'Unpair this phone?',
          style: TextStyle(
              color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
      content: Text(
        s.hubList.length > 1
            ? 'This phone forgets ${s.activeHubLabel}: its connection, '
                'addresses and pending actions go, and the app switches to '
                'your next paired hub. To use ${s.activeHubLabel} again you '
                'will need to pair once more with its hub key.'
            : 'This phone forgets the hub: its connection, addresses and '
                'pending actions go. To use the house from it again you will '
                'need to pair it once more with the hub key.',
        style: TextStyle(fontSize: 13, color: s.inkDim, height: 1.45),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(ctx).pop(false),
          child: Text('Cancel', style: TextStyle(color: s.cancelInk)),
        ),
        FilledButton(
          style: FilledButton.styleFrom(
              backgroundColor: s.cancelInk, foregroundColor: Colors.white),
          onPressed: () => Navigator.of(ctx).pop(true),
          child: const Text('Unpair'),
        ),
      ],
    ),
  );
  if (go != true) return;
  // unpairPhone() pops to home itself. Another paired hub takes over when
  // there is one; otherwise the pair screen goes on top.
  if (await s.unpairPhone()) return;
  s.navigatorKey.currentState?.push(MaterialPageRoute(
    settings: const RouteSettings(name: '/pair'),
    builder: (_) => PairScreen(state: s),
  ));
}

/// Every paired hub: tap one to switch to it, the bin to forget it. The
/// hub in use is forgotten through Unpair (it ends that session too).
Future<void> _hubsDialog(BuildContext context, AppState s) async {
  await showDialog<void>(
    context: context,
    builder: (ctx) => StatefulBuilder(
      builder: (ctx, setLocal) => AlertDialog(
        backgroundColor: s.surface,
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(16),
            side: BorderSide(color: s.border)),
        title: Text('Hubs',
            style: TextStyle(
                color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
        content: SizedBox(
          width: double.maxFinite,
          child: ListView(
            shrinkWrap: true,
            children: [
              for (final hub in s.hubList)
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  leading: Icon(
                      hub.id == s.activeHubId
                          ? Icons.radio_button_checked
                          : Icons.radio_button_unchecked,
                      color: hub.id == s.activeHubId ? s.accent : s.inkFaint),
                  title: Text(hub.label,
                      style: TextStyle(color: s.ink, fontSize: 14)),
                  subtitle: Text(
                      hub.id == s.activeHubId ? 'In use' : 'Tap to switch',
                      style: TextStyle(color: s.inkDim, fontSize: 12)),
                  trailing: hub.id == s.activeHubId
                      ? null
                      : IconButton(
                          tooltip: 'Forget this hub',
                          icon: Icon(Icons.delete_outline, color: s.inkDim),
                          onPressed: () async {
                            final sure = await showDialog<bool>(
                              context: ctx,
                              builder: (c2) => AlertDialog(
                                backgroundColor: s.surface,
                                surfaceTintColor: Colors.transparent,
                                title: Text('Forget ${hub.label}?',
                                    style: TextStyle(color: s.ink, fontSize: 16)),
                                content: Text(
                                    'This phone stops trusting it. Using it '
                                    'again needs pairing with its hub key.',
                                    style: TextStyle(
                                        color: s.inkDim, fontSize: 13)),
                                actions: [
                                  TextButton(
                                      onPressed: () => Navigator.of(c2).pop(false),
                                      child: Text('Cancel',
                                          style: TextStyle(color: s.cancelInk))),
                                  TextButton(
                                      onPressed: () => Navigator.of(c2).pop(true),
                                      child: Text('Forget',
                                          style: TextStyle(color: s.accent))),
                                ],
                              ),
                            );
                            if (sure != true) return;
                            await s.forgetOtherHub(hub.id);
                            setLocal(() {});
                          },
                        ),
                  onTap: hub.id == s.activeHubId
                      ? null
                      : () {
                          Navigator.of(ctx).pop();
                          s.switchHub(hub.id);
                        },
                ),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () {
              Navigator.of(ctx).pop();
              Navigator.of(context).push(MaterialPageRoute(
                settings: const RouteSettings(name: '/pair'),
                builder: (_) => PairScreen(state: s),
              ));
            },
            child: Text('Pair another hub', style: TextStyle(color: s.accent)),
          ),
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: Text('Done', style: TextStyle(color: s.accent)),
          ),
        ],
      ),
    ),
  );
}

/// REMOTE ACCESS (2026-11-10): a sheet, opened where the person is
/// (Settings, or a notice's Details). It says the hub's real state; signing
/// in opens the browser; another account keeps the old one until the new
/// sign-in succeeds; Log out asks first.
Future<void> showRemoteAccess(BuildContext context, AppState s) =>
    _tailscaleDialog(context, s);

Future<void> _tailscaleDialog(BuildContext context, AppState s) async {
  final state = s.hubTailnetState.isNotEmpty
      ? s.hubTailnetState
      : (s.hubTailnet.isNotEmpty ? 'on' : 'not_set_up');
  // Not connected: nothing can be started from here; say how to connect.
  final offline = !s.isReady;
  final signedIn = !offline &&
      const {'on', 'paused', 'off', 'expired', 'needs_approval'}
          .contains(state);
  final account = s.hubTailnetAccount;
  const warnInk = Color(0xFF8A5A00);
  final (String status, bool good) = offline
      ? ('Not connected to the hub', false)
      : switch (state) {
    'on' => ('On', true),
    'paused' => ('Paused: the hub has no internet', false),
    'off' => ('Switched off on the hub', false),
    'expired' => ('Its sign-in has run out', false),
    'needs_approval' => ('Waiting for approval', false),
    'absent' => ('Tailscale is not installed on the hub', false),
    'daemon_down' => ('Tailscale is not running on the hub', false),
    _ => ('Not set up', false),
  };
  final String explain = offline
      ? 'The phone cannot reach the hub right now. Be on the hub\'s Wi-Fi, '
          'or switch Tailscale on in this phone (same account), then open '
          'this again.'
      : switch (state) {
    'on' => 'Away from home: turn Tailscale on in this phone, signed in to '
        'the same account, and open the app.',
    'paused' => 'Nothing to set up again. As soon as the hub has internet, '
        'away-from-home access comes back by itself. At home everything '
        'still works.',
    'off' => 'Tailscale is stopped on the hub. Turn it on again on the Pi: '
        'sudo tailscale up',
    'expired' => 'Sign in again to bring it back. Tip: in the Tailscale '
        'admin page, turn off key expiry for the hub.',
    'needs_approval' => 'Approve the hub in the Tailscale admin page, and it '
        'starts working.',
    'absent' => 'Install it on the Pi (curl -fsSL '
        'https://tailscale.com/install.sh | sh), then sign in here.',
    'daemon_down' => 'Turning the hub off and on usually fixes it.',
    _ => '1. Install the Tailscale app on this phone and sign in.\n'
        '2. Tap Sign in. The page opens in your browser: use the same '
        'account.\n\nThe hub needs internet for this. Your PIN is asked '
        'first.',
  };
  final body = TextStyle(fontSize: 13.5, color: s.inkDim, height: 1.45);
  final primaryLabel = offline
      ? 'OK'
      : !signedIn
      ? 'Sign in'
      : state == 'expired'
          ? 'Sign in again'
          : 'OK';
  final choice = await showModalBottomSheet<String>(
    context: context,
    backgroundColor: s.surface,
    isScrollControlled: true,
    shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(24))),
    builder: (ctx) => SafeArea(
      child: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(22, 12, 22, 18),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Center(
              child: Container(
                width: 40,
                height: 4,
                decoration: BoxDecoration(
                    color: s.border, borderRadius: BorderRadius.circular(2)),
              ),
            ),
            const SizedBox(height: 16),
            Row(children: [
              Container(
                width: 40,
                height: 40,
                decoration: BoxDecoration(
                  color: s.accent.withValues(alpha: 0.12),
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Icon(Icons.public, size: 21, color: s.accent),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('Remote access',
                        style: TextStyle(
                            color: s.ink,
                            fontSize: 17,
                            fontWeight: FontWeight.w600)),
                    Text(status,
                        style: TextStyle(
                            color: good ? s.accent : warnInk,
                            fontSize: 12.5,
                            fontWeight: FontWeight.w600)),
                  ],
                ),
              ),
            ]),
            if (signedIn) ...[
              const SizedBox(height: 14),
              Container(
                padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
                decoration: BoxDecoration(
                  color: s.bg,
                  borderRadius: BorderRadius.circular(14),
                  border: Border.all(color: s.border),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                        account.isEmpty
                            ? 'Signed in to Tailscale'
                            : 'Signed in as $account',
                        style: TextStyle(
                            color: s.ink,
                            fontSize: 13.5,
                            fontWeight: FontWeight.w600)),
                    if (s.hubTailnet.isNotEmpty || s.hubTailnetIp.isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.only(top: 3),
                        child: Text(
                            [
                              if (s.hubTailnet.isNotEmpty)
                                s.hubTailnet.split('.').first,
                              if (s.hubTailnetIp.isNotEmpty) s.hubTailnetIp,
                            ].join('  ·  '),
                            style:
                                TextStyle(color: s.inkFaint, fontSize: 12.5)),
                      ),
                  ],
                ),
              ),
            ],
            const SizedBox(height: 12),
            Text(explain, style: body),
            const SizedBox(height: 18),
            Row(children: [
              Expanded(
                child: OutlinedButton(
                  style: OutlinedButton.styleFrom(
                    foregroundColor: signedIn ? s.ink : s.cancelInk,
                    side: BorderSide(
                        color: signedIn ? s.accent : s.border, width: 1.5),
                    padding: const EdgeInsets.symmetric(vertical: 13),
                    shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(12)),
                  ),
                  onPressed: () =>
                      Navigator.of(ctx).pop(signedIn ? 'join' : null),
                  child: Text(signedIn ? 'Another account' : 'Cancel',
                      maxLines: 1,
                      style: const TextStyle(
                          fontSize: 14, fontWeight: FontWeight.w600)),
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: FilledButton(
                  style: FilledButton.styleFrom(
                    backgroundColor: s.accent,
                    foregroundColor: s.accentFg,
                    padding: const EdgeInsets.symmetric(vertical: 13),
                    shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(12)),
                  ),
                  onPressed: () => Navigator.of(ctx).pop(!offline &&
                          (!signedIn || state == 'expired')
                      ? 'join'
                      : null),
                  child: Text(primaryLabel,
                      maxLines: 1,
                      style: const TextStyle(
                          fontSize: 14, fontWeight: FontWeight.w600)),
                ),
              ),
            ]),
            if (signedIn) ...[
              const SizedBox(height: 6),
              TextButton(
                onPressed: () => Navigator.of(ctx).pop('logout'),
                child: Text('Log out of Tailscale',
                    style: TextStyle(
                        color: s.cancelInk, fontWeight: FontWeight.w600)),
              ),
            ],
          ],
        ),
      ),
    ),
  );
  if (choice == 'join') {
    // The hub answers with the sign-in page, opened in the browser.
    await s.startTailscaleSignIn();
  } else if (choice == 'logout' && context.mounted) {
    final yes = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: s.surface,
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(16),
            side: BorderSide(color: s.border)),
        title: Text('Log out of Tailscale?',
            style: TextStyle(
                color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
        content: Text(
            '• ${account.isEmpty ? 'The account' : account} is removed from '
            'the hub.\n'
            '• Away from home, no phone can reach the hub until someone '
            'signs in again (and the hub needs internet for that).\n'
            '• At home on the Wi-Fi, everything keeps working.\n\n'
            'Your PIN is asked next.',
            style: body),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: Text('Keep it', style: TextStyle(color: s.ink)),
          ),
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: Text('Log out',
                style: TextStyle(
                    color: s.cancelInk, fontWeight: FontWeight.w700)),
          ),
        ],
      ),
    );
    if (yes == true) s.sendTailscaleLogout();
  }
}
