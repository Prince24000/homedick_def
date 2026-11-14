/// Pairing this phone with a hub, once.
///
/// WHY THIS IS ITS OWN FILE. Exactly one code path in this app connects to a
/// certificate it has not pinned, and it is this one. Keeping it here rather
/// than adding a flag to SocketClient means the ordinary connection path
/// cannot be talked into skipping the pin by a stray boolean -- and it means
/// anyone auditing "where do we accept an unknown certificate?" has one file
/// to read.
///
/// WHY ACCEPTING IT IS SAFE HERE. The certificate is not trusted on sight.
/// Its public key is hashed and folded into the string this phone HMACs with
/// the hub's API key. The hub recomputes that string using the key it
/// actually holds. If anyone has terminated TLS in the middle and is
/// relaying, his certificate hashes differently, the two strings disagree,
/// and the hub refuses -- so the worst an impostor gets is a proof that is
/// useless to him, and this phone never receives a `pair_ok` and never
/// stores anything.
///
/// ON THE HUB'S OWN WI-FI, NOT FROM AWAY. The hub folds the key of local.crt
/// into that string on every binding, and local.crt is what the access
/// point serves; the tailnet serves a Let's Encrypt certificate, so a proof
/// made there hashes differently and is refused as `bad_hmac`. Pairing
/// happens on the AP; the tailnet is for phones that already have a key.
///
/// The API key is used HERE AND NOWHERE ELSE. After pairing, the phone signs
/// with the keypair it generated itself, so the key can be typed once from
/// paper and forgotten rather than living in the app for ever.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart' show sha256;
import 'package:cryptography/cryptography.dart';

import 'protocol.dart';
import 'socket_client.dart' show SocketClient;

/// What a successful pairing yields. Everything here is public: the
/// certificate is sent in the clear during every TLS handshake anyway, and
/// the phone id is a hash of a public key.
class PairResult {
  const PairResult({
    required this.phoneId,
    required this.fingerprint,
    required this.certificatePem,
    required this.hubName,
  });

  final String phoneId;
  final String fingerprint;
  final String certificatePem;
  final String hubName;
}

class PairException implements Exception {
  PairException(this.reason, this.message);
  final String reason;
  final String message;

  /// The failure is about the ADDRESS, not about the hub's answer -- so
  /// trying the next candidate is worth it. A hub that answered and refused
  /// says the same thing at every address.
  bool get addressFault =>
      reason == 'unreachable' || reason == 'refused';

  @override
  String toString() => message;
}

/// The PUBLIC half of this phone's keypair (PhoneIdentity.keyPair), raw
/// 32 bytes base64 -- what phones._load_public_key on the hub expects.
Future<String> publicKeyB64(SimpleKeyPair pair) async {
  final key = await pair.extractPublicKey();
  return base64Encode(key.bytes);
}

/// Run the whole exchange against one candidate address.
///
/// Throws PairException with a reason the UI can show verbatim. Returns only
/// on a complete, verified pairing.
Future<PairResult> pairWithHub({
  required String url,
  required String apiKey,
  required String deviceName,

  /// The OS model string ("TECNO KJ5"), sent BESIDE the typed name so the
  /// hub can tell a reinstall (same device, same name) from a second
  /// handset. Optional: an older caller sends nothing and the hub's
  /// legacy name-only matching applies.
  String deviceModel = '',
  required SimpleKeyPair identity,
  Duration timeout = const Duration(seconds: 12),
  /// Called once, right after the hub accepts the key, with how many
  /// seconds its pairing window has left. THE ONE PHONE THAT CANNOT SEE
  /// the pair_window countdown every paired phone gets is the phone being
  /// paired -- it has no session for frames to reach. So the number rides
  /// in auth_ok instead, and the pair screen shows it. Optional and
  /// additive: an older hub sends no field and this is simply never
  /// called.
  void Function(int seconds)? onWindowSeconds,

  /// Whether pairing is open on the hub, and the seconds left (0: open
  /// with no end, or closed), once the hub has accepted the key.
  void Function(bool open, int seconds)? onWindow,

  /// Only ask whether pairing is open: stop after the key is accepted,
  /// before anything is recorded (ends with PairException 'checked').
  bool checkOnly = false,
}) async {
  // TLS OR NOTHING. Over ws:// the callback below never runs, there is no
  // certificate to bind the proof to, and the key's HMAC crosses in the clear.
  if (Uri.tryParse(url)?.scheme != 'wss') {
    throw PairException('not_wss',
        'not a wss:// address — refusing to pair over an unencrypted link');
  }

  // Captured by the certificate callback below, then folded into the proof.
  List<int>? presentedSpki;
  String? subject;

  final client = HttpClient(context: SecurityContext(withTrustedRoots: false))
    ..connectionTimeout = timeout
    ..badCertificateCallback = (cert, host, port) {
      // EVERY certificate lands here, because the context trusts nothing.
      // We accept it in order to CHALLENGE it -- see the file comment. If
      // the key cannot be parsed at all there is nothing to bind to, so
      // refuse rather than continue with a null.
      presentedSpki = SocketClient.spkiFromPem(
          '-----BEGIN CERTIFICATE-----\n'
          '${base64Encode(cert.der)}\n'
          '-----END CERTIFICATE-----');
      subject = cert.subject;
      return presentedSpki != null;
    };

  // A FRAGMENT, NOT A SENTENCE. The caller tries several addresses and now
  // prints each one with what it did, so the message here is the "what it
  // did" half. Refused is separated from silence because they mean opposite
  // things -- refused says the address is RIGHT and the hub is not running;
  // silence says nothing is there, or a firewall is dropping it, or this
  // phone is on another Wi-Fi -- and both used to print the same sentence.
  WebSocket socket;
  try {
    socket = await WebSocket.connect(url, customClient: client)
        .timeout(timeout);
  } on TimeoutException {
    throw PairException('unreachable', 'no answer in ${timeout.inSeconds}s');
  } on SocketException catch (e) {
    final refused =
        (e.osError?.message ?? '').toLowerCase().contains('refused');
    throw PairException(refused ? 'refused' : 'unreachable',
        refused ? 'port shut — the hub is not running' : 'no route to it');
  } catch (e) {
    throw PairException('unreachable', '$e');
  }

  // ONE SUBSCRIPTION FOR THE WHOLE EXCHANGE. A dart:io WebSocket is a
  // single-subscription stream: the first read consumes the only
  // subscription it will ever grant, and a second `.first` throws
  // "Bad state: Stream has already been listened to" -- which is exactly
  // how the first live pairing died, one frame after the HMAC had already
  // been verified. The iterator subscribes once and every reply comes
  // through it.
  final events = StreamIterator<dynamic>(socket);

  try {
    final challenge = await _next(events, timeout);
    // A THROTTLED ADDRESS is refused BEFORE any challenge: after five wrong
    // keys in a minute the hub opens with auth_failed/too_many_attempts
    // and hangs up. That is the hub answering, not an impostor -- say what
    // it said instead of "is this really the hub?".
    if (challenge['type'] == 'auth_failed') {
      throw PairException(
          (challenge['reason'] as String?) ?? 'auth_failed',
          (challenge['text'] as String?) ?? 'The hub refused this phone.');
    }
    if (challenge['type'] != 'auth_challenge' || challenge['nonce'] is! String) {
      throw PairException('bad_challenge',
          'The hub did not open with a challenge. Is this really the hub?');
    }

    // No certificate seen means no binding to prove -- never a bare `!`.
    final shownSpki = presentedSpki;
    if (shownSpki == null) {
      throw PairException('no_certificate',
          'The connection presented no certificate to bind the proof to.');
    }

    socket.add(jsonEncode(authFrameForPairing(
      apiKey: apiKey,
      nonce: challenge['nonce'] as String,
      spkiHex: _sha256Hex(shownSpki),
      timestampSeconds: DateTime.now().millisecondsSinceEpoch ~/ 1000,
    )));

    final authReply = await _next(events, timeout);
    if (authReply['type'] != 'auth_ok') {
      throw PairException(
          (authReply['reason'] as String?) ?? 'auth_failed',
          (authReply['text'] as String?) ??
              'The hub refused the key. Check HOMEVOICE_API_KEY, and that '
                  'pairing is open on the hub.');
    }
    final windowSeconds = authReply['pair_window_seconds'];
    if (windowSeconds is num && windowSeconds > 0) {
      onWindowSeconds?.call(windowSeconds.toInt());
    }
    final windowOpen = authReply['pair_window_open'];
    onWindow?.call(
        windowOpen is bool
            ? windowOpen
            : (windowSeconds is num && windowSeconds > 0),
        windowSeconds is num ? windowSeconds.toInt() : 0);
    if (checkOnly) {
      throw PairException('checked', 'pairing window checked');
    }

    socket.add(jsonEncode(pairFrame(
      publicKeyB64: await publicKeyB64(identity),
      name: deviceName,
      device: deviceModel,
    )));

    final paired = await _next(events, timeout);
    if (paired['type'] != 'pair_ok') {
      throw PairException((paired['reason'] as String?) ?? 'pair_failed',
          (paired['text'] as String?) ?? 'The hub would not record this phone.');
    }

    final phoneId = paired['phone_id'];
    if (phoneId is! String || phoneId.isEmpty) {
      throw PairException(
          'pair_failed', 'The hub did not say what it recorded this phone as.');
    }

    final certificate = paired['certificate'];
    if (certificate is! String || certificate.isEmpty) {
      throw PairException(
          'no_certificate', 'The hub did not send its certificate.');
    }

    // THE LAST CHECK, and the one that makes the rest mean anything. The
    // certificate we were handed must be the certificate we were SHOWN. A
    // hub that sends a different one -- by mistake or otherwise -- would
    // have this phone pin a key it has never seen used, and every later
    // connection would fail with nothing saying why.
    final delivered = SocketClient.spkiFromPem(certificate);
    if (!_sameBytes(delivered, shownSpki)) {
      throw PairException(
          'certificate_mismatch',
          'The hub sent a different certificate from the one it is using '
              '(shown: $subject). Not pairing.');
    }

    // A CROSS-LANGUAGE CHECK, and the reason it is here rather than assumed.
    //
    // Two separate implementations have to agree on what "the hub's
    // fingerprint" is: this app walks the DER by hand in
    // SocketClient._spki, the hub asks OpenSSL for the
    // subjectPublicKeyInfo. They SHOULD produce identical bytes. If they
    // ever do not, every signature after pairing fails for a reason that
    // looks exactly like a wrong key, and nothing anywhere says the two
    // parsers disagreed.
    //
    // The hub reports its own value in pair_ok purely so this can be
    // compared. It is NOT taken as the truth -- a disagreement refuses the
    // pairing and prints both, rather than adopting the hub's number.
    final hubSpki = paired['spki'];
    if (hubSpki is String && hubSpki.isNotEmpty) {
      final mine = _sha256Hex(shownSpki);
      if (hubSpki.toLowerCase() != mine.toLowerCase()) {
        throw PairException(
            'fingerprint_disagreement',
            'This phone and the hub computed different fingerprints for the '
                'same certificate.\n  phone: $mine\n  hub:   $hubSpki\n'
                'That is a bug in one of the two parsers, not a wrong key. '
                'Report both values.');
      }
    }

    return PairResult(
      phoneId: phoneId,
      fingerprint: (paired['fingerprint'] as String?) ?? '',
      certificatePem: certificate,
      hubName: (paired['hub_name'] as String?) ?? '',
    );
  } finally {
    unawaited(events.cancel());
    unawaited(socket.close());
    client.close(force: true);
  }
}

Future<Map<String, dynamic>> _next(
    StreamIterator<dynamic> events, Duration timeout) async {
  final bool hasFrame;
  try {
    hasFrame = await events.moveNext().timeout(timeout);
  } on TimeoutException {
    throw PairException('timeout', 'The hub stopped answering.');
  }
  if (!hasFrame) {
    throw PairException('closed', 'The hub closed the connection.');
  }
  final decoded = jsonDecode(events.current as String);
  if (decoded is! Map<String, dynamic>) {
    throw PairException('malformed', 'The hub sent something unreadable.');
  }
  return decoded;
}

/// The hub's fingerprint, in the exact form phones.spki_fingerprint()
/// produces: SHA-256 over the DER subjectPublicKeyInfo, lowercase hex.
///
/// Over the KEY, not the whole certificate -- hashing the certificate would
/// make every routine renewal look like a new hub and lock every phone out
/// until it paired again.
String _sha256Hex(List<int> spkiDer) => sha256.convert(spkiDer).toString();

bool _sameBytes(List<int>? a, List<int>? b) {
  if (a == null || b == null || a.length != b.length) return false;
  for (var i = 0; i < a.length; i++) {
    if (a[i] != b[i]) return false;
  }
  return true;
}