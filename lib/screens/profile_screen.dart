/// Profile — who you are, and everything that proves it.
///
/// Reached one way only: drawer › Settings › Profile (her ruling,
/// 2026-08-29). The nameplate at the top of the drawer is a NAMEPLATE — it
/// states whose voice this phone is bound to and opens nothing.
///
/// THE RULE THIS SCREEN IS BUILT ON: a row shows a credential's state AND is
/// the way to change it. You change your PIN on the row that says the PIN is
/// set. That is why `Set PIN` and `Forgot PIN` left Settings entirely rather
/// than being copied here — two doors to one change is the fork the drawer
/// merges existed to remove, and on a security setting it is worse than
/// untidy: the two doors drift, and one of them ends up with a weaker lock.
///
/// ONE DATA PATH, and deliberately no fallback. Devices and Scenes parse the
/// hub's printed table when the structured frame is missing; this screen
/// refuses to. Every fact here is a claim about a credential, and "PIN: set"
/// recovered by regex out of a print statement is a security claim made by a
/// pattern match. A hub too old to send the `profile` frame gets an honest
/// "could not read your profile" instead.
///
/// WHAT IS NOT SHOWN, because it cannot be: the recovery words. pin_auth
/// keeps five HMACs and a timestamp, never the words — they exist on screen
/// once, when the PIN is set, and after that only in her handwriting. So the
/// row says when they were made and offers the one thing they are FOR. A
/// "show again" button here would be the worst kind of lie: the sort you
/// only discover while already locked out.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:homevoice/socket_client.dart';   // ConnState

import '../app_state.dart';
import '../widgets/voice_ui.dart';

class ProfileScreen extends StatelessWidget {
  final AppState appState;
  const ProfileScreen({super.key, required this.appState});

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: appState,
      builder: (context, _) => _build(context),
    );
  }

  // ── small helpers over the hub's record ─────────────────────────────

  static bool _flag(Map<String, dynamic>? m, String key) =>
      m != null && m[key] == true;

  /// Dates cross the wire as STRINGS — every number on this socket does,
  /// since the AI review card lost three rows to an `as String?` cast on an
  /// int. Parsed back here, and an unparseable one simply goes unshown.
  static String _when(Map<String, dynamic>? m, String key) {
    final raw = (m?[key] ?? '').toString();
    final secs = int.tryParse(raw);
    if (secs == null || secs <= 0) return '';
    final d = DateTime.fromMillisecondsSinceEpoch(secs * 1000).toLocal();
    const months = [
      'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
      'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'
    ];
    // THE YEAR, once it is not this one (audit, 2026-09-12). "shown once on
    // 14 Mar" is undatable past twelve months, on a screen whose whole job
    // is telling you how old a credential is. Kept off the recent dates,
    // where it is noise.
    final now = DateTime.now();
    final year = d.year == now.year ? '' : ' ${d.year}';
    return '${d.day} ${months[d.month - 1]}$year';
  }

  Widget _build(BuildContext context) {
    final s = appState;
    // THE SAME EXPRESSION THE DRAWER AND SETTINGS USE, copied verbatim
    // rather than paraphrased. `running` means the socket client has been
    // STARTED, not that it is busy — reading it as "busy" is what greyed
    // out every row on the Settings screen the day it was written.
    final canManage = s.running &&
        s.connState == ConnState.ready &&
        s.pendingPrompt == null &&
        s.pendingChoice == null &&
        s.pendingRecord == null &&
        s.challengePhrase == null &&
        !s.isStarting;

    final you = s.profileYou;
    final me = s.profileMe;

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
          title: Text('Profile',
              style: TextStyle(
                  color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
        ),
        body: SafeArea(
          // THREE STATES, not two (audit, 2026-09-12). "Nothing has
          // arrived" and "a re-read is in flight" used to be the same
          // state, because requestProfile nulled every field — so the
          // instant a change SUCCEEDED, this screen drew the hub-failure
          // text until the round trip came back.
          child: you == null
              ? (s.profileLoading ? _checking(s) : _unreadable(s))
              : Opacity(
                  // Dimmed while re-reading, so nothing on it is taken for
                  // current. A stale "PIN: set" is a claim about the past.
                  opacity: s.profileLoading ? 0.45 : 1,
                  child: VoiceUiScope(
                    // "Scroll down" by voice, like every list screen.
                    state: s,
                    builder: (ctx, scroll) => ListView(
                      controller: scroll,
                  padding: const EdgeInsets.fromLTRB(16, 4, 16, 90),
                  children: [
                    _header(s, me, you),
                    _group(s, 'YOU'),
                    _row(
                      s,
                      icon: Icons.person_outline,
                      title: 'Name',
                      value: me.isEmpty ? 'not known' : me,
                      enabled: canManage && me.isNotEmpty,
                      gated: true,
                      onTap: () => _confirmRename(context, s, me),
                    ),
                    _group(s, 'HOW YOU PROVE IT'),
                    _row(
                      s,
                      icon: Icons.mic_none_outlined,
                      title: 'Voice',
                      value: _flag(you, 'has_voice')
                          ? 'Enrolled'
                          : 'Not enrolled',
                      enabled: canManage,
                      gated: true,
                      warn: !_flag(you, 'has_voice'),
                      onTap: s.reenrollVoice,
                    ),
                    _row(
                      s,
                      icon: Icons.lock_outline,
                      title: 'PIN',
                      value: _flag(you, 'has_pin') ? 'Set' : 'Not set',
                      enabled: canManage,
                      gated: true,
                      warn: !_flag(you, 'has_pin'),
                      onTap: s.changePin,
                    ),
                    // THIRD BESIDE VOICE AND PIN, warned the same way when
                    // it is not done. Her architecture, 2026-09-01: "in
                    // profile add card identity, with an exclamation ...
                    // which meant not done".
                    //
                    // Its old line ("a card says who is here; it opens
                    // nothing on its own") went with every writeup on this
                    // screen (her design, 2026-10-11); the card's own
                    // enrolment flow still says it.
                    _row(
                      s,
                      icon: Icons.badge_outlined,
                      title: 'Card',
                      value: _flag(you, 'has_card')
                          ? 'Enrolled'
                          : 'Not enrolled',
                      enabled: canManage,
                      gated: true,
                      warn: !_flag(you, 'has_card'),
                      onTap: s.enrollCard,
                    ),
                    _recoveryRow(context, s, you, canManage),
                    _group(s, 'THIS PHONE'),
                    _phoneCard(s),
                    _peopleHeader(s),
                    ..._people(s, me, canManage),
                    const SizedBox(height: 6),
                    _addPersonButton(context, s, canManage),
                    const SizedBox(height: 4),
                    _leaveButton(context, s, me, canManage),
                    if (!canManage)
                      Padding(
                        padding: const EdgeInsets.fromLTRB(4, 16, 4, 0),
                        child: Text(
                          // NOT CONNECTED comes first, because that is the
                          // common case and `running` cannot tell them
                          // apart: it is set once at startup and cleared
                          // only by unpairing, so `running && !ready` IS
                          // the ordinary disconnected state. The old order
                          // said "Busy — finish what is on screen first"
                          // with nothing on screen (audit, 2026-09-12).
                          s.connState != ConnState.ready
                              ? 'Not connected to the hub yet.'
                              : 'Busy — finish what is on screen first.',
                          style:
                              TextStyle(color: s.inkFaint, fontSize: 12),
                        ),
                      ),
                    ],
                  )),
                ),
        ),
      ),
    );
  }

  /// A re-read is in flight. Distinct from _unreadable: one means the hub
  /// has not answered, the other that it has not answered YET.
  Widget _checking(AppState s) => Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            SizedBox(
              width: 20,
              height: 20,
              child: CircularProgressIndicator(
                  strokeWidth: 2, color: s.inkFaint),
            ),
            const SizedBox(height: 14),
            Text('checking with the hub…',
                style: TextStyle(color: s.inkDim, fontSize: 13)),
          ],
        ),
      );

  /// No frame, no screen. Drawing an empty person with every credential
  /// reading "not set" would not be "we could not ask" — it would be a lie
  /// that looks like an answer.
  Widget _unreadable(AppState s) => Center(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.person_off_outlined, size: 34, color: s.inkFaint),
              const SizedBox(height: 14),
              Text(
                'The hub did not send your profile, so nothing here can be '
                'trusted to be current. Nothing is shown rather than '
                'something that might be wrong.',
                textAlign: TextAlign.center,
                style: TextStyle(color: s.inkDim, fontSize: 13, height: 1.5),
              ),
            ],
          ),
        ),
      );

  /// `you` is read for one line only: whether the house actually knows this
  /// voice. That line used to be printed unconditionally, directly above a
  /// Voice row saying "not enrolled" in amber (audit, 2026-09-12) —
  /// reachable whenever a voiceprint exists but no longer decrypts under a
  /// rotated key, which is exactly when the header must not claim it.
  Widget _header(AppState s, String me, Map<String, dynamic>? you) => Padding(
        padding: const EdgeInsets.fromLTRB(4, 2, 4, 16),
        child: Row(
          children: [
            Container(
              width: 54,
              height: 54,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                border: Border.all(color: s.accent, width: 1.5),
              ),
              alignment: Alignment.center,
              child: Text(
                me.isEmpty ? '?' : me.substring(0, 1).toUpperCase(),
                style: TextStyle(
                    color: s.accent, fontSize: 22, fontWeight: FontWeight.w600),
              ),
            ),
            const SizedBox(width: 13),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(me.isEmpty ? 'not identified' : me,
                      style: TextStyle(
                          color: s.ink,
                          fontSize: 22,
                          fontWeight: FontWeight.w600)),
                ],
              ),
            ),
          ],
        ),
      );

  Widget _recoveryRow(BuildContext context, AppState s,
      Map<String, dynamic>? you, bool canManage) {
    final has = _flag(you, 'has_recovery');
    final made = _when(you, 'recovery_created_at');
    return _row(
      s,
      icon: Icons.vpn_key_outlined,
      title: 'Recovery words',
      // NEVER "show again". They are held as HMACs; nothing on this hub can
      // read them back. See the library comment. Words held the OLD way
      // (per-word HMACs) say so: a fresh PIN mints the upgraded set.
      value: !has
          ? 'None'
          : _flag(you, 'recovery_legacy')
              ? 'Re-set PIN to upgrade'
              : (made.isEmpty ? 'Set' : 'Made $made'),
      enabled: canManage && has,
      warn: !has || _flag(you, 'recovery_legacy'),
      onTap: () => _confirmRecovery(context, s),
    );
  }

  Widget _phoneCard(AppState s) {
    final p = s.profilePhone;
    if (p == null) {
      // WHAT NULL CAN ACTUALLY MEAN (audit, 2026-09-12). The hub refuses
      // every menu action from an unbound phone, so if the profile read
      // ran at all, a registry record exists and carries a person. A null
      // phone therefore means the hub could not READ the registry — never
      // "nobody is recorded here", which is what this card used to assert
      // as fact, in amber, on a screen that refuses everywhere else to
      // claim what it cannot prove.
      return _plainCard(
        s,
        icon: Icons.smartphone_outlined,
        title: 'This phone',
        value: 'Not readable',
        warn: true,
      );
    }
    final device = (p['device'] ?? '').toString();
    final id = (p['id'] ?? '').toString();
    final short = id.length > 8 ? id.substring(0, 8) : id;
    final paired = _when(p, 'paired_at');
    final bits = <String>[
      if (device.isNotEmpty) device,
      if (short.isNotEmpty) short,
      if (paired.isNotEmpty) 'paired $paired',
    ];
    // THE OTHER HANDSETS bound to you, if the hub listed them: a reinstall
    // or a second phone should be visible here, not only at the console.
    final others = [
      for (final o in s.profilePhones)
        if ((o['id'] ?? '').toString() != id) o
    ];
    final otherNames = [
      for (final o in others)
        [
          (o['name'] ?? '').toString(),
          (o['device'] ?? '').toString(),
        ].where((t) => t.isNotEmpty).join(' · ')
    ].where((t) => t.isNotEmpty).toList();
    if (otherNames.isNotEmpty) {
      bits.add('also on: ${otherNames.join(', ')}');
    }
    // WHO IT IS BOUND TO, by name when that is not you: the record names
    // its person, and "Bound to you" was drawn for anyone at all.
    final boundTo = (p['user_id'] ?? '').toString();
    return _plainCard(
      s,
      icon: Icons.smartphone_outlined,
      title: boundTo.isEmpty
          ? 'Not bound to anyone'
          : (boundTo == s.profileMe ? 'Bound to you' : 'Bound to $boundTo'),
      // The handset's name only; the rest (its id, when it was paired,
      // other phones) was a writeup (her design, 2026-10-11).
      value: device.isNotEmpty ? device : (bits.isEmpty ? '' : bits.first),
    );
  }

  Widget _peopleHeader(AppState s) => Padding(
        padding: const EdgeInsets.fromLTRB(4, 18, 4, 6),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.center,
          children: [
            Text('EVERYONE ON THIS HUB',
                style: TextStyle(
                    color: s.inkFaint,
                    fontSize: 11,
                    letterSpacing: 0.8,
                    fontWeight: FontWeight.w700)),
            const SizedBox(width: 8),
            Expanded(child: Container(height: 1, color: s.border)),
            const SizedBox(width: 8),
            Text('${s.profilePeople.length}',
                style: TextStyle(color: s.inkFaint, fontSize: 11)),
          ],
        ),
      );

  List<Widget> _people(AppState s, String me, bool canManage) {
    return [
      for (final p in s.profilePeople)
        Builder(builder: (_) {
          final uid = (p['user_id'] ?? '').toString();
          final isMe = uid == me;
          // THE HUB SENDS FACTS ABOUT YOU ONLY. Everyone else arrives as a
          // bare `user_id` -- their credentials are theirs -- so marks are
          // drawn only when the record actually carries them. Reading an
          // absent key as "not set" painted every housemate "nothing set"
          // in amber, which was a security claim made about nobody's data.
          final known = p.containsKey('has_pin') ||
              p.containsKey('has_voice') ||
              p.containsKey('has_recovery');
          final marks = <String>[
            if (p['has_voice'] == true) 'voice',
            if (p['has_pin'] == true) 'PIN',
            if (p['has_recovery'] == true) 'recovery',
          ];
          // A missing credential is worth SAYING, not hiding: registration
          // is one transaction on the hub, so a person holding only half of
          // one is a state that is not supposed to be constructible.
          final incomplete = known && marks.length < 3;
          return Container(
            margin: const EdgeInsets.only(bottom: 8),
            constraints: const BoxConstraints(minHeight: _rowHeight),
            padding: EdgeInsets.fromLTRB(13, 8, isMe ? 13 : 2, 8),
            decoration: BoxDecoration(
              color: s.surface,
              border: Border.all(color: s.border),
              borderRadius: BorderRadius.circular(12),
            ),
            child: Row(
              children: [
                Container(
                  width: 28,
                  height: 28,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    border:
                        Border.all(color: isMe ? s.accent : s.inkFaint),
                  ),
                  alignment: Alignment.center,
                  child: Text(
                    uid.isEmpty ? '?' : uid.substring(0, 1).toUpperCase(),
                    style: TextStyle(
                        color: isMe ? s.accent : s.inkDim,
                        fontSize: 12,
                        fontWeight: FontWeight.w600),
                  ),
                ),
                const SizedBox(width: 11),
                Expanded(
                  child: Text(uid,
                      style: TextStyle(
                          color: s.ink,
                          fontSize: 14.5,
                          fontWeight: FontWeight.w600)),
                ),
                // A missing credential is still SAID, in a word: the
                // record that is not supposed to be constructible.
                if (incomplete)
                  _value(s, marks.isEmpty ? 'Nothing set' : 'Incomplete', true)
                else if (isMe)
                  _value(s, 'you', false),
                // REMOVE, on THEIR row (her design, 2026-10-11). The hub
                // asks "sure?", then your PIN, then what becomes of what
                // they made -- the app asks nothing of its own.
                if (!isMe && uid.isNotEmpty) _personMenu(s, uid, canManage),
              ],
            ),
          );
        }),
    ];
  }

  Widget _personMenu(AppState s, String uid, bool canManage) =>
      PopupMenuButton<String>(
        enabled: canManage,
        tooltip: 'More for $uid',
        icon: Icon(Icons.more_vert, size: 18, color: s.inkFaint),
        color: s.surface,
        onSelected: (v) {
          if (v == 'remove') s.removePerson(uid);
        },
        itemBuilder: (_) => [
          PopupMenuItem(
            value: 'remove',
            child: Row(children: [
              Icon(Icons.person_remove_outlined, size: 16, color: s.cancelInk),
              const SizedBox(width: 8),
              Text('Remove $uid',
                  style: TextStyle(color: s.cancelInk, fontSize: 14)),
              const SizedBox(width: 10),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                decoration: BoxDecoration(
                    border: Border.all(color: const Color(0xFFD29922)),
                    borderRadius: BorderRadius.circular(4)),
                child: const Text('PIN',
                    style: TextStyle(fontSize: 10, color: Color(0xFFD29922))),
              ),
            ]),
          ),
        ],
      );

  /// AT THE BOTTOM, under the list — not beside your own voice row.
  ///
  /// Beside it, somebody re-enrolling a housemate would reasonably think
  /// they were fixing their own voiceprint. Under the list of who is
  /// already here, the question "is this person already on the hub?" is
  /// answered before the button is pressed.
  Widget _addPersonButton(BuildContext context, AppState s, bool canManage) =>
      Opacity(
        opacity: canManage ? 1 : 0.45,
        child: OutlinedButton.icon(
          onPressed: canManage ? () => _confirmAddPerson(context, s) : null,
          icon: Icon(Icons.person_add_alt_outlined, size: 17, color: s.accent),
          // THE BADGE BELONGS TO THE CHANGE, and this one is gated on the
          // hub like every row above it. It was the only act on this
          // screen that asked for a PIN without saying so first (audit,
          // 2026-09-12).
          label: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text('Add another person',
                  style: TextStyle(
                      color: s.accent,
                      fontSize: 14.5,
                      fontWeight: FontWeight.w600)),
              const SizedBox(width: 8),
              Container(
                padding:
                    const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                decoration: BoxDecoration(
                    border: Border.all(color: const Color(0xFFD29922)),
                    borderRadius: BorderRadius.circular(4)),
                child: const Text('PIN',
                    style:
                        TextStyle(fontSize: 10, color: Color(0xFFD29922))),
              ),
            ],
          ),
          style: OutlinedButton.styleFrom(
            padding: const EdgeInsets.symmetric(vertical: 14),
            minimumSize: const Size.fromHeight(_rowHeight),
            side: BorderSide(color: s.accent),
            shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(12)),
          ),
        ),
      );

  /// Leave this house: under everything, quiet until tapped.
  Widget _leaveButton(
          BuildContext context, AppState s, String me, bool canManage) =>
      TextButton.icon(
        onPressed: canManage && me.isNotEmpty
            ? () => _confirmLeave(context, s, me)
            : null,
        icon: Icon(Icons.logout_rounded, size: 17, color: s.cancelInk),
        label: Text('Leave this house',
            style: TextStyle(
                color: s.cancelInk,
                fontSize: 14.5,
                fontWeight: FontWeight.w600)),
      );

  /// The pop-up: what stays, and (for the last person) whether to clear
  /// the whole hub. X closes and nothing happens. The PIN comes next.
  Future<void> _confirmLeave(
      BuildContext context, AppState s, String me) async {
    final last = !s.profilePeople.any((p) {
      final uid = (p['user_id'] ?? '').toString();
      return uid.isNotEmpty && uid != me;
    });
    final body = TextStyle(color: s.inkDim, fontSize: 13.5, height: 1.45);
    final how = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: s.surface,
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(16),
            side: BorderSide(color: s.border)),
        titlePadding: const EdgeInsets.fromLTRB(20, 8, 4, 0),
        title: Row(children: [
          Expanded(
            child: Text('Leave this house?',
                style: TextStyle(
                    color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
          ),
          IconButton(
            tooltip: 'Close',
            icon: Icon(Icons.close_rounded, color: s.inkDim),
            onPressed: () => Navigator.of(ctx).pop(),
          ),
        ]),
        content: Text(
            last
                ? 'You are the last person here. Your voice, PIN and cards '
                    'are deleted and this phone signs out.\n\n'
                    'The devices and all settings stay on the hub, unless '
                    'you clear it.'
                : 'Your voice, PIN and cards are deleted and this phone '
                    'signs out.\n\nThe devices and all settings stay on '
                    'the hub.',
            style: body),
        actions: [
          if (last)
            TextButton(
              onPressed: () => Navigator.of(ctx).pop('clear'),
              child: Text('Clear the whole hub',
                  style: TextStyle(
                      color: s.cancelInk, fontWeight: FontWeight.w700)),
            ),
          TextButton(
            onPressed: () => Navigator.of(ctx).pop('keep'),
            child: Text(last ? 'Keep everything' : 'Leave',
                style:
                    TextStyle(color: s.accent, fontWeight: FontWeight.w700)),
          ),
        ],
      ),
    );
    if (how == null) return;
    s.leaveHouse(how);
  }

  // ── the three that need saying-first ────────────────────────────────

  /// A rename is not a label change, and the person doing it deserves to
  /// know that before the PIN box appears — the same courtesy the Tailscale
  /// dialog pays before it spends a key.
  Future<void> _confirmRename(
      BuildContext context, AppState s, String me) async {
    final go = await _ask(
      context,
      s,
      title: 'Change your name',
      // WHAT THE HUB ACTUALLY PROMISES (audit, 2026-09-12). This said all
      // of it moves together or none of it does; menu_rename_user says the
      // opposite for its last two steps — the enrolment journal and the
      // CARD STORE are explicitly not fatal and not rolled back. Cards
      // were not even in the list, so an unwritable cards.json would stop
      // your card being recognised with nothing on screen saying why.
      body: 'Your name is how the hub knows you everywhere — your voiceprint, '
          'your PIN, your recovery words, and this phone. Those four move '
          'together, or none of them do.\n\n'
          'Your PIN and your five recovery words keep working, unchanged.\n\n'
          'Two smaller things are moved afterwards and are not undone if '
          'they fail: the record of who you have added, and any card you '
          'have enrolled. If your card stops being recognised after this, '
          'tap it again from the Card row.',
      confirm: 'Continue',
    );
    if (go == true) s.renameUser();
  }

  Future<void> _confirmRecovery(BuildContext context, AppState s) async {
    final go = await _ask(
      context,
      s,
      title: 'Recovery words',
      body: 'Your old words cannot be shown — the hub never keeps them, only '
          'a fingerprint.\n\n'
          'Enter your PIN and you get five NEW words to write down. The old '
          'ones stop working. Forgot your PIN as well? You can use your old '
          'words to set a new PIN from the same screen.',
      confirm: 'Get new words',
    );
    if (go == true) s.showRecovery();
  }

  Future<void> _confirmAddPerson(BuildContext context, AppState s) async {
    final go = await _ask(
      context,
      s,
      title: 'Add another person',
      body: 'They record their own voice and set their own PIN. Yours will '
          'not verify them and theirs will not verify you.\n\n'
          'This hub writes down that you added them, and when.',
      confirm: 'Continue',
    );
    if (go == true) s.addPerson();
  }

  Future<bool?> _ask(BuildContext context, AppState s,
      {required String title,
      required String body,
      required String confirm}) {
    return showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: s.surface,
        // Explicit, because Material 3's default surface tint bleeds purple
        // through an unstyled dialog — the one thing every dialog in this
        // app has had to say out loud since the Tailscale one did not.
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(16),
            side: BorderSide(color: s.border)),
        title: Text(title,
            style: TextStyle(
                color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
        content: Text(body,
            style: TextStyle(fontSize: 13, color: s.inkDim, height: 1.45)),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            // Every cancel wears the disconnect red (her rule, 2026-08-27).
            child: Text('Cancel', style: TextStyle(color: s.cancelInk)),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
                backgroundColor: s.accent, foregroundColor: s.accentFg),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: Text(confirm),
          ),
        ],
      ),
    );
  }

  // ── chrome ──────────────────────────────────────────────────────────

  Widget _group(AppState s, String label) => Padding(
        padding: const EdgeInsets.only(top: 18, bottom: 6, left: 4),
        child: Text(label,
            style: TextStyle(
                color: s.inkFaint,
                fontSize: 11,
                letterSpacing: 0.8,
                fontWeight: FontWeight.w700)),
      );

  /// One height for every box here and on Settings (her design,
  /// 2026-10-11).
  static const double _rowHeight = 64;

  /// A box with nothing to tap: its name, and its state at the right.
  Widget _plainCard(AppState s,
      {required IconData icon,
      required String title,
      String value = '',
      bool warn = false}) {
    return Container(
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
          Icon(icon,
              size: 19,
              color: warn ? const Color(0xFFD29922) : s.inkDim),
          const SizedBox(width: 13),
          Expanded(
            child: Text(title,
                style: TextStyle(
                    color: s.ink,
                    fontSize: 14.5,
                    fontWeight: FontWeight.w600)),
          ),
          if (value.isNotEmpty) _value(s, value, warn),
        ],
      ),
    );
  }

  /// The state at the right of a box: "Enrolled", "Set" -- amber when it
  /// is something not done. NO WRITEUPS (her design, 2026-10-11): what a
  /// row is for is in its name, what it holds in one or two words.
  Widget _value(AppState s, String value, bool warn) => Flexible(
        child: Padding(
          padding: const EdgeInsets.only(left: 8, right: 4),
          child: Text(value,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              textAlign: TextAlign.right,
              style: TextStyle(
                  color: warn ? const Color(0xFFD29922) : s.inkDim,
                  fontSize: 13)),
        ),
      );

  /// Same shape as the Settings row, on purpose: this screen took two rows
  /// from that one, and a person who used them should not have to learn a
  /// new control to find them again.
  Widget _row(
    AppState s, {
    required IconData icon,
    required String title,
    String value = '',
    required bool enabled,
    required VoidCallback onTap,
    bool gated = false,
    bool warn = false,
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
              Icon(icon,
                  size: 19,
                  color: warn ? const Color(0xFFD29922) : s.inkDim),
              const SizedBox(width: 13),
              Expanded(
                child: Text(title,
                    style: TextStyle(
                        color: s.ink,
                        fontSize: 14.5,
                        fontWeight: FontWeight.w600)),
              ),
              if (value.isNotEmpty) _value(s, value, warn),
              if (gated)
                Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                  decoration: BoxDecoration(
                      border: Border.all(color: const Color(0xFFD29922)),
                      borderRadius: BorderRadius.circular(4)),
                  child: const Text('PIN',
                      style:
                          TextStyle(fontSize: 10, color: Color(0xFFD29922))),
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