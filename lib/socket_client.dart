/// Connection layer: candidate probing, certificate pinning, the auth
/// handshake, and a clean stream of decoded frames.
///
/// Everything the browser-client saga taught is structural here:
///  * AP-first probing -- candidates tried in preference order, first open
///    socket wins, so hotspot beats tailnet whenever it answers.
///  * PUBLIC-KEY pinning, not system trust. The app ships the server's own
///    local.crt, reads the KEY out of it, and accepts any certificate
///    carrying that key on every address -- the whole
///    install-the-CA-on-each-phone problem, and the silent wss-rejection
///    failure mode, cannot exist.
///  * Generation counter so a reconnect abandons an in-flight probe instead
///    of racing it.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart' show debugPrint;
import 'package:crypto/crypto.dart' show sha256;

import 'config.dart';
import 'protocol.dart';

enum ConnState { idle, probing, authenticating, ready, closed }

/// HOW GOOD THE ROAD TO THE HUB IS RIGHT NOW (2026-10-03: Tailscale was up,
/// the road was a relay in Paris at 0.9-1.7 s a round trip, and nothing said
/// so -- it only looked like "not working").
///
/// Measured from both ends. This phone sends `link_ping` every few seconds
/// and times the answer ([rttMs], the middle of the last three). The answer
/// carries the hub's own measure of the same road ([hubRttMs], from its
/// keepalive ping to this phone) and which road Tailscale is using
/// ([path]: "direct", "relay par", or "" off the tailnet).
class LinkQuality {
  const LinkQuality(
      {this.rttMs, this.hubRttMs, this.path = '', this.missed = 0});

  /// Nothing measured: not connected, or a hub that does not answer pings.
  static const LinkQuality none = LinkQuality();

  final int? rttMs;
  final int? hubRttMs;
  final String path;

  /// Pings in a row that got no answer.
  final int missed;

  /// 0 no answer, 1 poor, 2 slow, 3 good, 4 excellent. The same lines the
  /// hub grades by (ws_server.py, LINK_GRADES): 150 ms, 400 ms, one second.
  int get bars {
    final ms = rttMs;
    if (ms == null || missed >= 2) return 0;
    if (missed == 1) return 1;
    if (ms < 150) return 4;
    if (ms < 400) return 3;
    if (ms < 1000) return 2;
    return 1;
  }

  String get word =>
      const ['no answer', 'poor', 'slow', 'good', 'excellent'][bars];

  bool get relayed => path.startsWith('relay');

  /// "slow, 870 ms, through Tailscale's relay (par)".
  String describe() {
    final parts = <String>[word];
    if (rttMs != null) parts.add('$rttMs ms');
    if (relayed) {
      final where = path.length > 6 ? ' (${path.substring(6)})' : '';
      parts.add("through Tailscale's relay$where");
    } else if (path == 'direct') {
      parts.add('direct over Tailscale');
    }
    if (missed > 0) parts.add('$missed ping(s) unanswered');
    return parts.join(', ');
  }
}

class SocketClient {
  SocketClient({
    required this.candidates,
    required this.getPinnedCertificate,
    required this.signAuth,
  });

  /// Mutable on purpose: the Network editor updates this in place, and the
  /// probe loop in connect() reads it fresh on every attempt.
  final List<String> candidates;

  /// The hub's certificate as stored at pairing, or null if this phone has
  /// not paired. REPLACES the bundled asset -- see _loadPinnedSpki.
  final Future<String?> Function() getPinnedCertificate;

  /// Produce the `auth` frame for a challenge, or null if this phone has no
  /// identity to sign with.
  ///
  /// A CALLBACK, not a key, deliberately: the private key never enters this
  /// class, so the transport layer cannot leak, log or mis-send it. The
  /// caller (app_state) owns storage and signing; this class owns the
  /// socket. `spkiHex` is the fingerprint of the certificate THIS
  /// connection was shown, and must be signed as given.
  final Future<Map<String, dynamic>?> Function(String nonce, String spkiHex)
      signAuth;

  /// The hub's fingerprint in the exact form the server computes it:
  /// SHA-256 OF the subjectPublicKeyInfo, hex -- not the hex of the SPKI
  /// itself. Getting this wrong produces a signature that fails for a
  /// reason indistinguishable from a wrong key.
  static String _fingerprint(List<int> spkiDer) =>
      sha256.convert(spkiDer).toString();

  WebSocket? _ws;
  int _gen = 0;
  ConnState state = ConnState.idle;
  String? connectedUrl;

  final _frames = StreamController<ServerFrame>.broadcast();
  final _states = StreamController<ConnState>.broadcast();
  Stream<ServerFrame> get frames => _frames.stream;
  Stream<ConnState> get states => _states.stream;

  // ── THE GAUGE (see LinkQuality) ─────────────────────────────────────
  LinkQuality link = LinkQuality.none;
  final _links = StreamController<LinkQuality>.broadcast();
  Stream<LinkQuality> get links => _links.stream;

  static const Duration _linkEvery = Duration(seconds: 10);
  Timer? _linkTimer;
  int _linkSeq = 0;
  int? _linkAwaiting;
  final Stopwatch _linkClock = Stopwatch();
  final List<int> _linkRecent = [];

  /// Started by an auth_ok that says the hub answers pings; an older hub
  /// is never sent one (it would take the frame for a reply to a question).
  void _startGauge() {
    _stopGauge();
    _linkPing();
    _linkTimer = Timer.periodic(_linkEvery, (_) => _linkPing());
  }

  void _stopGauge() {
    _linkTimer?.cancel();
    _linkTimer = null;
    _linkAwaiting = null;
    _linkRecent.clear();
    _setLink(LinkQuality.none);
  }

  void _linkPing() {
    if (_linkAwaiting != null) {
      // The last one was never answered: the bars drop NOW, long before
      // the socket itself is given up on.
      _setLink(LinkQuality(
          rttMs: link.rttMs,
          hubRttMs: link.hubRttMs,
          path: link.path,
          missed: link.missed + 1));
    }
    final id = ++_linkSeq;
    _linkAwaiting = id;
    _linkClock
      ..reset()
      ..start();
    if (!send({'type': 'link_ping', 'id': id})) _linkAwaiting = null;
  }

  void _onLinkPong(ServerFrame f) {
    if (f.raw['id'] != _linkAwaiting) return; // an answer to an older ping
    _linkAwaiting = null;
    _linkRecent.add(_linkClock.elapsedMilliseconds);
    if (_linkRecent.length > 3) _linkRecent.removeAt(0);
    final sorted = [..._linkRecent]..sort();
    final hub = f.raw['hub_rtt_ms'];
    final path = f.raw['path'];
    _setLink(LinkQuality(
        rttMs: sorted[sorted.length ~/ 2],
        hubRttMs: hub is int ? hub : null,
        path: path is String ? path : ''));
  }

  void _setLink(LinkQuality q) {
    final was = link;
    link = q;
    // One line when the picture changes, not one per ping.
    if (q.bars != was.bars || q.path != was.path) {
      debugPrint('[link] ${q.describe()}');
    }
    _links.add(q);
  }

  // ── THE SUPERVISOR ──────────────────────────────────────────────────
  //
  // Her ruling, 2026-08-29: "make it a very rigorous connection, no mercy
  // for dropping". This layer used to give up at the first sign of trouble
  // and then STAY down: the socket listener runs cancelOnError, so any
  // transport error tore it apart, and nothing retried. From the hub's side
  // that reads as the app hanging up on its own -- which is exactly what it
  // reported.
  //
  // Underneath that, the connection had no heartbeat at all. connectionTimeout
  // covers only the initial connect, so an established socket could sit
  // silent indefinitely -- and a Windows Mobile Hotspot reaps idle NAT
  // mappings while the phone stays happily associated to the Wi-Fi. The link
  // was already dead; the app only found out on the next read.
  //
  // So: a protocol-level ping (pingInterval on the socket) keeps the path
  // warm and proves the far end is alive, and an unexpected close AFTER a
  // successful auth is followed by a retry: 2 s, 5 s, 10 s, then every 30 s
  // until close(). A drop before any auth_ok, or one the person asked for
  // (close()), is never retried. Every retry is a fresh sweep and a fresh
  // handshake -- the nonce is per connection, nothing old is reused.
  int _attempt = 0;
  Timer? _retryTimer;

  /// Retry delays, in order; after the last, every 30 s until close().
  static const List<Duration> _retryLadder = [
    Duration(seconds: 2),
    Duration(seconds: 5),
    Duration(seconds: 10),
  ];

  /// Set by auth_ok, cleared by close(). Only a proven connection is worth
  /// redialling: a drop before proof is a refusal or a misconfiguration.
  bool _provenOnce = false;

  /// The rung the ladder was on when the last auth_ok arrived. The hub sends
  /// auth_ok BEFORE its connection-cap check, so a full hub answers
  /// auth_ok then auth_failed/too_many_connections -- and the reset in
  /// _healthy() would restart the ladder at 2 s on every attempt, retrying
  /// (and announcing the refusal) for ever. The refusal puts it back.
  int _attemptAtProof = 0;

  /// True between a caller asking for a connection and a caller asking to
  /// stop. While true, a dropped socket is reconnected; while false, nothing
  /// is retried. The ONLY thing separating "you pressed Disconnect" from
  /// "the network fell over".
  bool _wantConnected = false;

  /// Does the supervisor currently INTEND to be connected? True while it is
  /// retrying, false after a deliberate close. Anything that wants to nudge
  /// a reconnect must consult this first, or it will drag the person back
  /// online after they pressed Disconnect.
  bool get wantConnected => _wantConnected;

  /// Why the last connection ended, in words. The fault that started this
  /// was never identified, and a connection that heals silently would hide
  /// it for good -- so every drop still says what happened.
  String lastDropReason = '';

  /// True while a retry is pending, so the UI can say "reconnecting" instead
  /// of "disconnected" — two different facts that used to look identical.
  bool get reconnecting => _retryTimer != null;

  /// Attempts since the last good connection. Zero while healthy.
  int get attempt => _attempt;

  SecurityContext? _pinned;
  List<int>? _pinnedSpki;

  void _setState(ConnState s) {
    state = s;
    _states.add(s);
  }

  /// The pinned PUBLIC KEY of the hub this phone paired with.
  ///
  /// WHY THE KEY AND NOT THE WHOLE CERTIFICATE. This once compared the
  /// entire DER byte-for-byte, so any change to the certificate locked
  /// every phone out. Pinning the key instead survives a renewal, new
  /// addresses and a longer expiry, because setup_manager reuses the
  /// private key when it regenerates. The pin only changes when the KEY
  /// changes -- which is exactly when every phone SHOULD stop trusting the
  /// hub.
  ///
  /// WHY IT IS LEARNED, NOT SHIPPED. This used to read
  /// `assets/certs/local.crt` out of the bundle, and that single line was
  /// the reason a new hub meant a new APK: the pinned bytes were a BUILD
  /// input, so changing hubs meant editing assets, rebuilding and
  /// reinstalling on every phone in the house. It now comes from secure
  /// storage, put there during pairing. Not one bit weaker -- same key,
  /// same byte-for-byte comparison. Only the delivery changed, from a
  /// rebuild to fifteen seconds.
  ///
  /// Why not setTrustedCertificatesBytes: that asks the TLS stack to build
  /// a trust CHAIN, and a self-signed leaf has no issuer to find --
  /// producing "unable to get local issuer certificate", or a connection
  /// that never completes and reads as a timeout, even when the
  /// certificate is exactly the right one.
  ///
  /// Returns null when this phone has not paired yet. That means "pair
  /// first", never "skip the check".
  Future<List<int>?> _loadPinnedSpki() async {
    if (_pinnedSpki != null) return _pinnedSpki;
    final pem = await getPinnedCertificate();
    if (pem == null || pem.isEmpty) return null;
    _pinnedSpki = spkiFromPem(pem);
    return _pinnedSpki;
  }

  /// Drop the cached pin so a certificate stored by pairing is picked up
  /// without restarting the app. The context is dropped too: _context()
  /// only reloads the SPKI while it has no context to hand back.
  void forgetPin() {
    _pinnedSpki = null;
    _pinned = null;
  }

  /// The subjectPublicKeyInfo of a PEM certificate, or null if it will not
  /// parse. Shared with the pairing screen, which needs this same value to
  /// bind its proof to the certificate it was actually shown.
  static List<int>? spkiFromPem(String pem) {
    final b64 = pem
        .split(RegExp(r'\r?\n'))
        .where((l) => l.isNotEmpty && !l.startsWith('-----'))
        .join();
    try {
      return _spki(base64Decode(b64));
    } catch (_) {
      return null;
    }
  }

  /// The subjectPublicKeyInfo bytes of a DER certificate, or null.
  ///
  /// A DELIBERATE WALK, NOT A SEARCH. Looking for the key's bytes anywhere
  /// inside the certificate would be far simpler and quietly broken: a
  /// forged certificate could carry the real key's bytes as decoration --
  /// in an extension, in a name -- while presenting a different key for the
  /// handshake, and a substring test would accept it. So this navigates to
  /// the one field that the TLS handshake actually uses.
  ///
  ///   Certificate ::= SEQUENCE { tbsCertificate, sigAlg, signature }
  ///   tbsCertificate ::= SEQUENCE {
  ///       [0] version OPTIONAL, serialNumber, signature, issuer,
  ///       validity, subject, subjectPublicKeyInfo, ... }
  ///
  /// so: into the certificate, into the tbs, skip the optional version and
  /// the five fields before it, and take the next element whole.
  static List<int>? _spki(List<int> der) {
    int lengthAt(int i) {
      // Returns the content length; _after is set to the first content byte.
      final n = der[i];
      if (n < 0x80) {
        _after = i + 1;
        return n;
      }
      var count = n & 0x7F, length = 0, j = i + 1;
      // A length field longer than four bytes is not a certificate we can
      // sanely handle, and is far more likely to be a malformed input.
      if (count == 0 || count > 4) return -1;
      for (; count > 0; count--) {
        length = (length << 8) | der[j++];
      }
      _after = j;
      return length;
    }

    int skip(int i) {
      final length = lengthAt(i + 1);
      if (length < 0) return -1;
      return _after + length;
    }

    try {
      if (der.isEmpty || der[0] != 0x30) return null;   // Certificate
      if (lengthAt(1) < 0) return null;
      var i = _after;
      if (der[i] != 0x30) return null;                  // tbsCertificate
      if (lengthAt(i + 1) < 0) return null;
      i = _after;
      if (der[i] == 0xA0) i = skip(i);                  // [0] version
      for (var field = 0; field < 5; field++) {
        // serialNumber, signature, issuer, validity, subject
        i = skip(i);
        if (i < 0 || i >= der.length) return null;
      }
      if (der[i] != 0x30) return null;                  // SPKI is a SEQUENCE
      final end = skip(i);
      if (end < 0 || end > der.length) return null;
      return der.sublist(i, end);
    } catch (_) {
      // Any malformed certificate lands here and is simply not a match.
      return null;
    }
  }

  /// Scratch for _spki's length parser: Dart has no out-parameters and a
  /// tuple return would allocate on every element of every walk.
  static int _after = 0;

  static bool _sameBytes(List<int>? a, List<int>? b) {
    if (a == null || b == null || a.length != b.length) return false;
    for (var i = 0; i < a.length; i++) {
      if (a[i] != b[i]) return false;
    }
    return true;
  }

  /// A context that trusts NOTHING by default; acceptance happens solely in
  /// badCertificateCallback, against the pinned bytes.
  Future<SecurityContext> _context() async {
    if (_pinned != null) return _pinned!;
    await _loadPinnedSpki();
    _pinned = SecurityContext(withTrustedRoots: false);
    return _pinned!;
  }

  /// Probe candidates in order; adopt the first that completes a WebSocket
  /// upgrade. Returns the URL connected to, or null with `states` already
  /// reporting `closed`.
  Future<String?> connect() async {
    _wantConnected = true;
    _provenOnce = false;
    _attempt = 0;
    _retryTimer?.cancel();
    _retryTimer = null;
    return _connectOnce();
  }

  /// Dial NOW, keeping the supervisor as it is -- for the app coming back
  /// to the front. connect() is wrong for that: it clears _provenOnce, so
  /// when that one sweep failed nothing retried again, ever.
  Future<String?> redial() async {
    if (!_wantConnected) return null;
    _retryTimer?.cancel();
    _retryTimer = null;
    return _connectOnce();
  }

  /// One full sweep of the candidate list. Separated from connect() so the
  /// supervisor can retry WITHOUT clearing _wantConnected or cancelling its
  /// own timer out from under itself.
  Future<String?> _connectOnce() async {
    await _teardown(); // supersede any previous attempt
    final gen = ++_gen;
    _setState(ConnState.probing);

    // A secure-storage read that THROWS (the stored certificate) must end
    // this sweep as a drop: escaping from here left the state at `probing`
    // for good, and a wake word awaiting connect() stuck in `speaking`.
    final SecurityContext ctx;
    try {
      ctx = await _context();
    } catch (e) {
      debugPrint('[ws] reading the pinned certificate failed: $e');
      if (gen == _gen) _dropped('the stored hub certificate could not be read');
      return null;
    }
    // ALL ADDRESSES AT ONCE (2026-09-28). One at a time, a sweep with
    // nothing answering took 8 + 25 + 8 + 25 = 66 s before it could even
    // start over. Now the first address to finish its pinned TLS upgrade
    // wins and the rest are closed before they authenticate (the hub only
    // counts a session after auth, so a closed loser costs it nothing).
    //
    // A COPY: the Network editor replaces the live list in place.
    final all = List.of(candidates);
    // Tailscale addresses only when THIS phone is on Tailscale: without it
    // they cannot answer, and each one held the sweep open for 25 s.
    final onTailnet = await phoneOnTailnet();
    if (gen != _gen) return null;
    final dial = [
      for (final u in all)
        if (onTailnet || !isTailnetUrl(u)) u
    ];
    skippedTailnet = dial.length < all.length;
    if (dial.isNotEmpty) {
      final won = Completer<(String, WebSocket)?>();
      var left = dial.length;
      for (final url in dial) {
        unawaited(_tryOne(url, ctx).then((ws) {
          if (ws != null) {
            if (!won.isCompleted && gen == _gen) {
              won.complete((url, ws));
            } else {
              unawaited(ws.close()); // slower, or superseded: never used
            }
          }
          if (--left == 0 && !won.isCompleted) won.complete(null);
        }));
      }
      final got = await won.future;
      if (gen != _gen) {
        if (got != null) unawaited(got.$2.close());
        return null;
      }
      if (got != null) {
        final (url, ws) = got;
        _ws = ws;
        connectedUrl = url;
        _setState(ConnState.authenticating);
        _listen(ws, gen);
        return url;
      }
    }
    // EVERY candidate refused. Not fatal -- the hotspot may still be coming
    // up, the laptop may be asleep, the hub may be restarting.
    _dropped(skippedTailnet
        ? 'no local address answered (Tailscale is off on this phone)'
        : 'no candidate answered');
    return null;
  }

  /// One socket ended when we did not ask it to. Record why, and line up
  /// the next attempt.
  void _dropped(String why) {
    lastDropReason = why;
    debugPrint('[ws] dropped: $why (attempt ${_attempt + 1})');
    _stopGauge();
    _setState(ConnState.closed);
    _scheduleReconnect();
  }

  /// The bounded retry. Runs only while the caller still wants a
  /// connection AND this cycle has proven itself once; each rung of the
  /// ladder is spent once, then it waits 30 s between sweeps.
  void _scheduleReconnect() {
    if (!_wantConnected || !_provenOnce) return;
    if (_retryTimer != null) return; // one pending retry at a time
    // PAST THE LADDER, KEEP GOING every 30 s. Giving up after 17 s was the
    // hotspot "it just breaks": a phone that lost the Wi-Fi for longer (a
    // laptop hotspot restarting, Android flipping to mobile data) stayed
    // down until the app was reopened. Only close() ends retrying now.
    final wait = _attempt < _retryLadder.length
        ? _retryLadder[_attempt]
        : const Duration(seconds: 30);
    _attempt++;
    debugPrint('[ws] reconnecting in ${wait.inSeconds}s (retry $_attempt)');
    _retryTimer = Timer(wait, () {
      _retryTimer = null;
      if (!_wantConnected) return; // close() won the race
      unawaited(_connectOnce()); // full sweep, full handshake
    });
  }

  /// Is `url` reached through the tailnet (a 100.64/10 address or a
  /// MagicDNS .ts.net name)?
  static bool isTailnetUrl(String url) {
    final host = Uri.tryParse(url)?.host ?? '';
    return isTailnetIp(host) || host.endsWith('.ts.net');
  }

  /// Does THIS phone hold a Tailscale address right now (the Tailscale app
  /// is connected)? Unreadable interfaces count as yes: never skip on a
  /// guess.
  static Future<bool> phoneOnTailnet() async {
    try {
      final ifaces = await NetworkInterface.list(
          includeLoopback: false, type: InternetAddressType.IPv4);
      for (final i in ifaces) {
        for (final a in i.addresses) {
          if (isTailnetIp(a.address)) return true;
        }
      }
      return false;
    } catch (_) {
      return true;
    }
  }

  /// True when the last sweep left Tailscale addresses out because this
  /// phone is not on Tailscale.
  bool skippedTailnet = false;

  /// Tailscale's CGNAT range, 100.64.0.0/10 -- an address that is reached
  /// through the tailnet, however it is written. Public: AppState uses it
  /// to admit a hub-sent tailnet address into the candidate list and
  /// nothing else through that door.
  static bool isTailnetIp(String host) {
    final a = InternetAddress.tryParse(host);
    if (a == null || a.type != InternetAddressType.IPv4) return false;
    final b = a.rawAddress;
    return b[0] == 100 && (b[1] & 0xC0) == 64;
  }

  Future<WebSocket?> _tryOne(String url, SecurityContext ctx) async {
    // ONE TRUST MODEL FOR EVERY ADDRESS: the pinned key. The hub serves
    // local.crt on EVERY binding since 2026-09-09 -- the tailnet listener
    // included, whether it is dialled by 100.x address or by .ts.net name
    // (config.NETWORK_BINDINGS). A hostname candidate used to get SYSTEM
    // trust roots instead, for a Let's Encrypt certificate the hub no longer
    // serves there -- so a self-signed local.crt failed public validation
    // and every learned tailnet NAME was a dead entry that burned the 25 s
    // budget on each sweep.
    // TLS OR NOTHING. A ws:// candidate would connect in the clear, never
    // reach badCertificateCallback, and the app would sign the challenge and
    // run the whole session unencrypted. Refused here regardless of what the
    // Settings editor let through.
    final uri = Uri.tryParse(url);
    if (uri == null || uri.scheme != 'wss' || uri.host.isEmpty) {
      debugPrint('[ws] refused $url: not wss');
      return null;
    }
    final host = uri.host;
    final isIpLiteral = InternetAddress.tryParse(host) != null;

    // AP probes stay tight (local radio: either it answers fast or it's
    // down); hostname probes get the relay-sized budget -- see config.
    //
    // A TAILNET ADDRESS (100.64.0.0/10) is an IP literal -- and pinned like
    // the APs, which is right now that the hub serves local.crt on its
    // tailnet listener too -- but it is NOT a local radio. The first
    // connection to a peer over Tailscale can ride a DERP relay and take
    // well over 8s (field log, 2026-09-09: the first sweep timed the
    // tailnet IP out at exactly 8s, the second connected in 2s). It gets
    // the tailnet budget, not the AP one.
    final budget = (isIpLiteral && !isTailnetIp(host))
        ? AppConfig.probeTimeout
        : AppConfig.hostnameProbeTimeout;

    final client = HttpClient(context: ctx)
      ..connectionTimeout = budget
      ..badCertificateCallback = (cert, host, port) {
        // Reached ONLY because the pinned context trusts nothing: every
        // server certificate lands here for an identity check -- a name
        // mismatch too, which is irrelevant: the KEY is the identity.
        final presented = _spki(cert.der);
        final ok = _pinnedSpki != null && _sameBytes(presented, _pinnedSpki);
        debugPrint(
            '[ws] key from $host:$port -> ${ok ? 'MATCH' : 'MISMATCH'}'
            '${ok ? '' : ' (presented '
                '${presented?.length ?? 0}B key, pinned '
                '${_pinnedSpki?.length ?? 0}B; subject ${cert.subject})'}');
        return ok;
      };
    try {
      final ws = await WebSocket.connect(url, customClient: client)
          .timeout(budget);
      // The heartbeat: a link the hotspot silently reaped is noticed when a
      // pong fails to come back, and dart:io then closes the socket (onDone).
      ws.pingInterval = const Duration(seconds: 20);
      return ws;
    } catch (e) {
      // The exception TYPE names the layer: SocketException = no route /
      // refused; HandshakeException = TLS; TimeoutException = no answer in
      // budget; WebSocketException = TCP+TLS fine, upgrade refused.
      debugPrint('[ws] probe $url failed: ${e.runtimeType}: $e');
      // FORCE-close: a timed-out connect keeps grinding in the background
      // otherwise, and the field logs caught exactly that -- an orphaned
      // socket completing its handshake 2s after abandonment, then eating
      // a server connection slot until the server's auth timeout reaped
      // it. Abandoned means dead, immediately.
      client.close(force: true);
      return null;
    }
    // NOTE: no finally -- the SUCCESS path must NOT close the client here,
    // force or not; the adopted socket rides on it. Losers are reaped in
    // the catch above.
  }

  void _listen(WebSocket ws, int gen) {
    ws.listen(
      (data) async {
        if (gen != _gen) return;
        Map<String, dynamic>? obj;
        try {
          final decoded = jsonDecode(data as String);
          if (decoded is Map<String, dynamic>) obj = decoded;
        } catch (_) {}
        if (obj == null) return; // never let malformed JSON reach the flow

        final frame = ServerFrame(obj);

        // Auth is transport-level; the flow above only ever sees an
        // authenticated stream.
        switch (frame.type) {
          case 'auth_challenge':
            // THIS CLASS NO LONGER TOUCHES A KEY. It hands the challenge to
            // the identity layer and sends back whatever frame comes out.
            //
            // WHAT IS SIGNED: the SPKI of local.crt as STORED AT PAIRING --
            // the hub signs with that same file on every binding, and the
            // pin above guarantees it equals the key this socket was shown,
            // so the string is channel-bound on every address.
            final spki = _pinnedSpki;
            if (spki == null) {
              // NOT retryable: pairing is a human act. Retrying would spin
              // forever against a door that needs a key we do not have.
              // Closed FIRST, then reported: the `idle` a close emits makes
              // AppState abort the flow, voice included, so a refusal added
              // before it was cut off after its first words.
              await close(reason: 'this phone is not paired');
              _frames.add(ServerFrame({
                'type': 'auth_failed',
                'reason': 'not_paired',
                'text': 'this phone has not been paired with a hub yet'
              }));
              return;
            }
            // A secure-storage read that THROWS (a keystore invalidated by a
            // restore, say) must not escape this listener as an uncaught
            // error and leave the hub to time the handshake out: it is the
            // same dead end as having no key, and says so.
            Map<String, dynamic>? frameToSend;
            try {
              frameToSend = await signAuth(frame.nonce ?? '', _fingerprint(spki));
            } catch (e) {
              debugPrint('[ws] signing the challenge failed: $e');
              frameToSend = null;
            }
            if (gen != _gen) return; // superseded while the key was read
            if (frameToSend == null) {
              await close(reason: 'no key for that hub'); // first: see above
              _frames.add(ServerFrame({
                'type': 'auth_failed',
                'reason': 'no_identity',
                'text': 'this phone has no key for that hub -- pair again'
              }));
              return;
            }
            send(frameToSend);
            return;
          case 'auth_ok':
            // PROVEN, so the ladder starts from the bottom next time. Without
            // this a phone that reconnects hourly would creep up to the 20s
            // rung and stay there, taking twenty seconds to recover from a
            // blip it used to shrug off in one.
            _attemptAtProof = _attempt;
            _healthy();
            _provenOnce = true; // from here a drop is worth redialling
            _setState(ConnState.ready);
            if (frame.raw['link'] == true) _startGauge();
            _frames.add(frame); // flow announces "connected" by voice
            return;
          case 'link_pong':
            _onLinkPong(frame); // the gauge's own; no screen needs it
            return;
          case 'auth_failed':
            // The hub refused this phone. Retrying cannot change its mind,
            // and hammering a refused handshake is how a phone earns a
            // lockout it did not deserve.
            //
            // EXCEPT a full house. too_many_connections comes AFTER a good
            // signature (no strike is recorded) and says "try again
            // shortly": the usual cause is this phone's own half-dead
            // sockets from a Wi-Fi blip, still counted by a hub that never
            // times a peer out. The hub hangs up; onDone hands that to the
            // bounded retry ladder instead of stopping for good.
            if (frame.reason == 'too_many_connections') {
              _attempt = _attemptAtProof; // not proven after all: see field
              _frames.add(frame);
              return;
            }
            // Closed before it is reported, as for not_paired above.
            await close(reason: 'the hub refused this phone');
            _frames.add(frame);
            return;
        }
        _frames.add(frame);
      },
      onDone: () {
        if (gen != _gen) return;
        // A CLEAN CLOSE FROM THE FAR END IS STILL A DROP. The hub finishing
        // the socket, the hotspot resetting the connection, dart:io closing
        // it because a pong never came -- all of them land here, and none of
        // them means the person wanted to stop. Only close() means that.
        _dropped(_ws == null
            ? 'socket closed'
            : 'the hub closed the connection (or a ping went unanswered)');
      },
      onError: (e) {
        if (gen != _gen) return;
        _dropped('transport error: ${e.runtimeType}');
      },
      cancelOnError: true,
    );
  }

  bool send(Map<String, dynamic> frame) {
    final ws = _ws;
    if (ws == null || ws.readyState != WebSocket.open) return false;
    ws.add(jsonEncode(frame));
    return true;
  }

  /// STOP, and mean it. The Disconnect button, dispose(), and the auth
  /// failures that no amount of retrying can fix.
  ///
  /// This is the only thing that lowers _wantConnected, which is what makes
  /// "the person left" distinguishable from "the network fell over" — a
  /// distinction the supervisor is built on and the old code did not have.
  Future<void> close({String reason = 'closed by the app'}) async {
    _wantConnected = false;
    _provenOnce = false;
    _retryTimer?.cancel();
    _retryTimer = null;
    _attempt = 0;
    lastDropReason = reason;
    await _teardown();
    if (state != ConnState.idle) _setState(ConnState.idle);
  }

  /// Drop the socket WITHOUT touching the supervisor's intent. Used by
  /// close() and by each fresh connect sweep.
  Future<void> _teardown() async {
    _gen++;
    _stopGauge();
    final ws = _ws;
    _ws = null;
    connectedUrl = null;
    if (ws != null) {
      try {
        await ws.close();
      } catch (_) {}
    }
  }

  /// Called when the handshake finishes: the connection is proven, so the
  /// backoff starts again from the top next time.
  void _healthy() {
    _attempt = 0;
    lastDropReason = '';
  }
}