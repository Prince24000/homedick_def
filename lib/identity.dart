/// THIS PHONE'S IDENTITY, and the hubs it is paired with.
///
/// One file owns everything the app stores about who it is: the Ed25519
/// keypair it generated, the id the hub knows it by, and the certificate it
/// learned during pairing. Nothing else in the app touches secure storage
/// for these, so "where does the private key live?" has exactly one answer.
///
/// THE PRIVATE KEY NEVER LEAVES. Not during pairing, not afterwards, not to
/// the transport layer -- SocketClient takes a signing CALLBACK rather than
/// a key, so the code that owns the socket cannot leak, log or mis-send it.
/// The hub only ever sees the public half, which is why the hub's phone
/// list contains no secret at all.
///
/// The API key is deliberately absent. It is typed once during pairing,
/// spent, and forgotten -- it is not this phone's identity, it is the
/// permission slip that let this phone register one.
///
/// MANY HUBS, ONE PHONE (2026-09-24). The keypair is the PHONE's and is the
/// same for every hub -- each hub simply holds its public half. What is per
/// hub is what pairing with THAT hub produced: its certificate (the pin),
/// the id it knows this phone by, and its name. Those live in a list; one
/// entry is the ACTIVE hub, and everything that asks "which hub?" --
/// the socket's pin, the handshake's phone id -- reads that one. Pairing
/// another hub ADDS to the list instead of replacing it. A hub's entry is
/// keyed by its key's fingerprint, so re-pairing the same hub (after a
/// certificate renewal that kept the key) updates its entry, while a new
/// machine with a copied API key is -- correctly -- a different hub.
library;

import 'dart:convert';

import 'package:crypto/crypto.dart' as crypto show sha256;
import 'package:cryptography/cryptography.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import 'protocol.dart';
import 'socket_client.dart' show SocketClient;

/// One paired hub, as this phone remembers it.
class HubEntry {
  const HubEntry({
    required this.id,
    required this.name,
    required this.certificatePem,
    required this.phoneId,
  });

  /// The first 16 hex of SHA-256 over the hub's public key (SPKI): stable
  /// across certificate renewals, different for every hub.
  final String id;
  final String name;
  final String certificatePem;
  final String phoneId;

  /// What to call it on screen: its name, or its id when it sent none.
  String get label => name.trim().isNotEmpty ? name.trim() : 'Hub $id';

  Map<String, dynamic> toJson() =>
      {'id': id, 'name': name, 'cert': certificatePem, 'phone_id': phoneId};

  static HubEntry? fromJson(Object? raw) {
    if (raw is! Map) return null;
    final id = raw['id'], cert = raw['cert'], phone = raw['phone_id'];
    if (id is! String || cert is! String || phone is! String) return null;
    if (id.isEmpty || cert.isEmpty || phone.isEmpty) return null;
    return HubEntry(
        id: id,
        name: (raw['name'] as String?) ?? '',
        certificatePem: cert,
        phoneId: phone);
  }
}

class PhoneIdentity {
  PhoneIdentity({FlutterSecureStorage? store})
      : _store = store ?? const FlutterSecureStorage();

  final FlutterSecureStorage _store;

  // Keys in secure storage. `shared_secret` is deliberately NOT among them:
  // it belonged to the old design where every phone signed with one key.
  static const _kPrivate = 'phone_private_key';
  static const _kHubs = 'hubs';
  static const _kActive = 'active_hub';
  // The ONE-HUB layout, read once to move an existing pairing into the list
  // (so updating the app never costs a re-pair), then deleted.
  static const _kPhoneId = 'phone_id';
  static const _kCertificate = 'hub_certificate';
  static const _kHubName = 'hub_name';

  /// The id a hub's certificate gives it (see HubEntry.id).
  static String hubIdOf(String certificatePem) {
    final spki = SocketClient.spkiFromPem(certificatePem);
    if (spki == null) {
      throw const FormatException('not a certificate this app can read');
    }
    return crypto.sha256.convert(spki).toString().substring(0, 16);
  }

  /// Every hub this phone is paired with, in the order they were paired.
  Future<List<HubEntry>> hubs() async {
    final raw = await _store.read(key: _kHubs);
    if (raw == null) return _migrateOneHub();
    try {
      final list = jsonDecode(raw);
      if (list is! List) return [];
      return [for (final e in list) if (HubEntry.fromJson(e) case final h?) h];
    } catch (_) {
      return [];
    }
  }

  Future<List<HubEntry>> _migrateOneHub() async {
    final id = await _store.read(key: _kPhoneId);
    final cert = await _store.read(key: _kCertificate);
    final out = <HubEntry>[];
    if (id != null && id.isNotEmpty && cert != null && cert.isNotEmpty) {
      String hubId;
      try {
        hubId = hubIdOf(cert);
      } catch (_) {
        hubId = crypto.sha256.convert(utf8.encode(cert)).toString().substring(0, 16);
      }
      out.add(HubEntry(
          id: hubId,
          name: await _store.read(key: _kHubName) ?? '',
          certificatePem: cert,
          phoneId: id));
      await _store.write(key: _kActive, value: hubId);
    }
    await _writeHubs(out);
    await _store.delete(key: _kPhoneId);
    await _store.delete(key: _kCertificate);
    await _store.delete(key: _kHubName);
    return out;
  }

  Future<void> _writeHubs(List<HubEntry> list) => _store.write(
      key: _kHubs, value: jsonEncode([for (final h in list) h.toJson()]));

  /// The hub in use; null when none is (never paired, or all forgotten).
  Future<HubEntry?> activeHub() async {
    final list = await hubs();
    if (list.isEmpty) return null;
    final id = await _store.read(key: _kActive);
    for (final h in list) {
      if (h.id == id) return h;
    }
    return null;
  }

  Future<String?> activeHubId() async => (await activeHub())?.id;

  /// Make `id` the hub in use. False when this phone is not paired with it.
  Future<bool> setActiveHub(String id) async {
    if (!(await hubs()).any((h) => h.id == id)) return false;
    await _store.write(key: _kActive, value: id);
    return true;
  }

  /// The FUTURE is memoised, not the result: two callers arriving before the
  /// first generation is written (a pairing tap, a challenge on a live
  /// socket) share one generation instead of racing two sets of writes and
  /// leaving one pair's private key beside the other's public key.
  Future<SimpleKeyPair>? _cached;

  /// Has this phone paired with a hub?
  ///
  /// Both halves are required. A phone with a keypair but no certificate
  /// never finished pairing; one with a certificate but no id cannot name
  /// itself in the handshake. Either alone is a half-finished state to be
  /// restarted, not limped along with.
  Future<bool> isPaired() async => (await activeHub()) != null;

  /// This phone's keypair, generated once and kept for its whole life.
  ///
  /// Generated LAZILY and stored immediately, so the key exists before
  /// pairing needs it and survives every later launch. If it were made
  /// during the pairing exchange instead, a pairing that failed halfway
  /// would leave the hub holding a public key whose private half had been
  /// thrown away -- an entry that can never authenticate and never expires.
  Future<SimpleKeyPair> keyPair() {
    // Synchronous up to the assignment, so a second caller in the same
    // event-loop turn already finds the Future and awaits it.
    final pending = _cached;
    if (pending != null) return pending;
    final started = _loadOrGenerate();
    _cached = started;
    // A failed load (storage unavailable) must not be remembered as the
    // keypair for the life of the process. The error itself still reaches
    // the caller through `started`; this branch only forgets the memo.
    started.then<void>((_) {}, onError: (Object e) {
      if (identical(_cached, started)) _cached = null;
    });
    return started;
  }

  Future<SimpleKeyPair> _loadOrGenerate() async {
    final stored = await _store.read(key: _kPrivate);
    if (stored != null && stored.isNotEmpty) {
      return Ed25519().newKeyPairFromSeed(base64Decode(stored));
    }
    final fresh = await Ed25519().newKeyPair();
    final seed = await fresh.extractPrivateKeyBytes();
    await _store.write(key: _kPrivate, value: base64Encode(seed));
    return fresh;
  }

  /// The id the ACTIVE hub knows this phone by.
  Future<String?> phoneId() async => (await activeHub())?.phoneId;

  /// The ACTIVE hub's certificate, learned at pairing. REPLACES the bundled
  /// asset that used to make a new hub mean a new APK.
  Future<String?> certificate() async => (await activeHub())?.certificatePem;

  /// Save what a completed pairing produced, and make that hub the active
  /// one. Called once, from the pairing screen, after every check in
  /// pairing.dart has passed. A hub already in the list (same key) is
  /// updated in place; any other hub is added. Returns the hub's id.
  Future<String> savePairing({
    required String phoneId,
    required String certificatePem,
    String hubName = '',
  }) async {
    final id = hubIdOf(certificatePem);
    final entry = HubEntry(
        id: id, name: hubName, certificatePem: certificatePem, phoneId: phoneId);
    final list = await hubs();
    final at = list.indexWhere((h) => h.id == id);
    if (at >= 0) {
      list[at] = entry;
    } else {
      list.add(entry);
    }
    await _writeHubs(list);
    await _store.write(key: _kActive, value: id);
    return id;
  }

  /// Forget ONE hub (the active one when `id` is null), KEEP the keypair.
  ///
  /// Pairing with a different hub reuses this phone's identity rather than
  /// minting a new one, so the same handset is recognisably the same
  /// handset. Throwing the key away would also orphan this phone's entry
  /// on every other hub -- registrations nothing could authenticate
  /// against. When the active hub goes, the first remaining one becomes
  /// active; with none left the phone is unpaired.
  Future<void> forgetHub([String? id]) async {
    final gone = id ?? await _store.read(key: _kActive);
    final list = (await hubs())..removeWhere((h) => h.id == gone);
    await _writeHubs(list);
    final active = await _store.read(key: _kActive);
    if (active == gone || !list.any((h) => h.id == active)) {
      if (list.isEmpty) {
        await _store.delete(key: _kActive);
      } else {
        await _store.write(key: _kActive, value: list.first.id);
      }
    }
  }

  /// Sign a challenge. THE ONLY WAY the private key is ever used.
  ///
  /// `spkiHex` is the fingerprint of the certificate THIS connection was
  /// shown -- passed in by the transport rather than read from storage,
  /// because signing for the hub we expect instead of the hub we are
  /// actually talking to is exactly the substitution the binding exists to
  /// catch.
  ///
  /// Returns null when this phone has no identity yet, which the caller
  /// must treat as "pair first", never as "skip the check".
  Future<Map<String, dynamic>?> signAuth(String nonce, String spkiHex) async {
    final id = await phoneId();
    if (id == null || id.isEmpty) return null;
    final ts = DateTime.now().millisecondsSinceEpoch ~/ 1000;
    final pair = await keyPair();
    final signature = await Ed25519()
        .sign(utf8.encode(signedMessage(nonce, ts, spkiHex)), keyPair: pair);
    return authFrameSigned(
      phoneId: id,
      signatureB64: base64Encode(signature.bytes),
      timestampSeconds: ts,
    );
  }
}