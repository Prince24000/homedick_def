/// The WebSocket wire protocol, transcribed from ws_server.py and the
/// browser client -- every frame in one place so drift between what the
/// server speaks and what the app understands is a one-file diff.
///
/// SERVER -> CLIENT
///   auth_challenge {nonce}            answer with `auth` within the timeout
///   auth_ok / auth_failed {reason,text}
///   view {name: enroll|command}       server owns which "screen" is current
///   record_required {phrase, context, index, total}   speak phrase, reply `recording`
///   challenge_required {phrase}       liveness check -- reply `challenge_response`
///   prompt {text}                     free-text question -- reply `prompt_reply`
///   choice {...}                      option list -- reply `choice_response`
///   output {text, level}              informational; `important` must be voiced
///   result {ok, reason?, text?}       terminal frame for a command/menu action
///   automation_builder {devices, targets, scenes, else_kinds, rule?}
///                                     the whole automation, for building as
///                                     blocks; answered ONCE with
///                                     automation_builder_response
///                                     {rule} or {cancelled:true}. Same shape
///                                     and same reason as pin_map: some things
///                                     are a screen, not a question.
///   automation_list {automations[], journal[]}
///   scene_builder {editing, scene, targets, whom, ai_available, ai_text,
///                  ai_error, ai_for, tried, error}
///                                     the scene screen; answered with
///                                     scene_builder_response {action:
///                                     save|try|generate, scene} or
///                                     {cancelled:true}. After `generate`
///                                     it comes back with the AI's
///                                     suggestion in ai_text.
///                                     structured rules for the Automations
///                                     screen (the text listing still streams
///                                     as output rows, as with device_list)
///   error {reason, text}
///   pair_window {open, seconds, reason?, phone?}
///                                     the hub's pairing door, hub-announced:
///                                     sent when a window opens (menu action,
///                                     --pair, or the boot window), on every
///                                     connect while one is open, and when it
///                                     closes EARLY (reason "paired" carries
///                                     who arrived). Natural expiry is never
///                                     announced -- the app holds the
///                                     deadline and lets its countdown die.
///
/// CLIENT -> SERVER
///   auth {timestamp, phone_id, signature}   a PAIRED phone: Ed25519 over
///                                     "<nonce>:<ts>:<spki>", signature b64
///   auth {timestamp, hmac}            a phone with no keypair yet, asking
///                                     to pair: HMAC-SHA256(api key, the
///                                     same string) hex. Grants an
///                                     ENROLLMENT session only -- one `pair`
///                                     frame, then the server hangs up.
///   pair {public_key, name}           register this phone's public key
///
/// SERVER -> CLIENT, pairing only
///   pair_ok {phone_id, fingerprint, certificate, spki, hub_name}
///   pair_failed {reason, text}
///
/// <spki> is the SHA-256 of the hub's public key and is NEVER SENT in
/// either direction -- see signedMessage().
///   command {text, audio?, audio_format: "wav"}  audio rides for speaker verification
///   recording {audio, audio_format} | {cancelled: true}
///   challenge_response {audio, audio_format}
///   prompt_reply {value} | {cancelled: true, value}
///                                     the cancelled form was added 12 Sep
///                                     2026; prompt_reply was the only
///                                     answer frame without one, so a
///                                     cancel had to travel as the VALUE —
///                                     the word "cancel", or EMPTY for a
///                                     masked box (the word would have been
///                                     checked as the PIN). Empty is also
///                                     what "I pressed send on an empty
///                                     box" looks like, which get_input
///                                     answers by asking again: cancelling
///                                     a PIN prompt looped for ever. The
///                                     value still rides along for a hub
///                                     that predates the flag.
///   choice_response {values: [...]} | {cancelled: true, values: []}
///   menu {action}
///   file_response {files: [{name, data}]} | {cancelled: true}
///                                     answers file_request {text, purpose,
///                                     accept, multiple, max_bytes} --
///                                     only after auth announced
///                                     features: [files]
///
/// One server behavior worth naming because the flow depends on it: a
/// `command` frame arriving while nobody is enrolled returns
/// result{reason: "not_enrolled"} AND starts enrollment server-side. Sending
/// an empty command is therefore the deliberate "begin enrollment" gesture --
/// the refusal that comes back is the handshake working, not an error.
library;

import 'dart:convert';

import 'package:crypto/crypto.dart';

/// THE ONE STRING both ends sign. Mirrors `phones.signed_message()` on the
/// hub, and the two must not drift: a changed separator here surfaces as
/// "wrong secret", which sends you looking at the key instead of the code.
///
/// The third field is the SHA-256 of the HUB'S PUBLIC KEY, and neither side
/// ever transmits it. Each supplies it from what it holds -- this app from
/// the certificate it was actually shown, the hub from the key it actually
/// has. Anyone terminating TLS in the middle and relaying holds a different
/// certificate, so the two strings differ and the proof fails; he cannot
/// correct for it without a key whose hash he does not control.
String signedMessage(String nonce, int timestampSeconds, String spkiHex) =>
    '$nonce:$timestampSeconds:$spkiHex';

/// hex(HMAC-SHA256(secret, message)). The secret never crosses the wire.
///
/// Used ONLY during pairing now. Once a phone has registered a keypair it
/// signs with that instead and never touches the shared key again -- which
/// is why the key can be typed once from paper and then forgotten, rather
/// than living in the app forever.
String hmacProof(String secret, String message) =>
    Hmac(sha256, utf8.encode(secret)).convert(utf8.encode(message)).toString();

/// What this app can do beyond the base protocol, announced in both auth
/// frames. `files`: it answers `file_request` with the system file picker.
/// The hub sends none to a connection that did not announce it; an older
/// hub ignores the field.
const List<String> kClientFeatures = ['files'];

/// The auth frame for a PAIRED phone: an Ed25519 signature over the same
/// string, plus the id the hub knows this phone by.
///
/// `signatureB64` is produced by the caller rather than here, because
/// signing is asynchronous and this file is deliberately synchronous and
/// dependency-light -- it is the wire contract, not a crypto layer.
Map<String, dynamic> authFrameSigned({
  required String phoneId,
  required String signatureB64,
  required int timestampSeconds,
}) =>
    {
      'type': 'auth',
      'timestamp': timestampSeconds,
      'phone_id': phoneId,
      'signature': signatureB64,
      'features': kClientFeatures,
    };

/// The auth frame for a phone that has NOT paired yet. Proves knowledge of
/// the shared key, which buys the right to register a keypair and nothing
/// else: the server grants an enrollment session, handles one `pair` frame,
/// and hangs up.
Map<String, dynamic> authFrameForPairing({
  required String apiKey,
  required String nonce,
  required String spkiHex,
  required int timestampSeconds,
}) =>
    {
      'type': 'auth',
      'timestamp': timestampSeconds,
      'hmac':
          hmacProof(apiKey, signedMessage(nonce, timestampSeconds, spkiHex)),
      'features': kClientFeatures,
    };

/// Carry a one-off Tailscale auth key to the hub, which spends it on
/// `tailscale up` and reports back its .ts.net name. Only accepted on a
/// session authenticated by this phone's own keypair -- an enrollment
/// session is refused, so knowing the API key alone cannot attach the hub
/// to someone's network.
///
/// Replies: tailscale_joined {tailnet_name, cert_ok, text}
///          tailscale_failed {reason, text}
/// With `authKey` empty, the hub starts a KEYLESS sign-in instead: it runs
/// `tailscale up` with no key, pushes the login URL to this phone as a web
/// screen, and completes when the sign-in finishes. Nothing typed, no key
/// ever minted.
Map<String, dynamic> tailscaleJoinFrame({
  String authKey = '',
  String hostname = '',
}) =>
    {
      'type': 'tailscale_join',
      if (authKey.isNotEmpty) 'auth_key': authKey,
      if (hostname.isNotEmpty) 'hostname': hostname,
    };

/// Hand the hub the PUBLIC half of a keypair this phone generated, plus a
/// name to show in `setup_manager.py --phones`, plus what the handset says
/// it IS (`device` -- the OS model string, e.g. "TECNO KJ5"). Name and
/// device are two identities on purpose: the person chooses the first, the
/// hardware reports the second, and the hub matches a reinstall on BOTH
/// (same phone, then the same name -- then the acceptance).
Map<String, dynamic> pairFrame({
  required String publicKeyB64,
  required String name,
  String device = '',
}) =>
    {
      'type': 'pair',
      'public_key': publicKeyB64,
      'name': name,
      if (device.isNotEmpty) 'device': device,
    };

/// `ui`, when given, is the screen the phone is on, the labels voice may
/// pick there and the management actions it may start
/// ({screen, targets:[{id,label,kind}], actions:[menu action id, ...]}) --
/// what lets the hub tell "open automations" from "open the garage", and
/// "add a device" from "add a little light". Never a field's contents. The
/// hub answers navigation with a `ui` frame: {action: open|back|scroll|
/// select|menu, ...}; `menu` carries the action id, and the app runs it
/// exactly as a tap on that row would (AppState.voiceMenuActions is the
/// allowlist on this side).
Map<String, dynamic> commandFrame(String text,
        {String? audioB64, Map<String, dynamic>? ui}) =>
    {
      'type': 'command',
      'text': text,
      // The phone's own clock, so "what is the time" is answered from it
      // (a Pi has no RTC and can be hours out).
      'now': phoneClock(),
      if (audioB64 != null) 'audio': audioB64,
      if (audioB64 != null) 'audio_format': 'wav',
      if (ui != null) 'ui': ui,
    };

/// The phone's local time as ISO-8601 WITH its UTC offset
/// ("2026-10-01T17:42:10+01:00"); Dart's own toIso8601String drops the
/// offset for a local time, and the hub needs it.
String phoneClock([DateTime? at]) {
  final t = at ?? DateTime.now();
  final off = t.timeZoneOffset;
  String two(int n) => n.toString().padLeft(2, '0');
  final a = off.abs();
  return '${t.year.toString().padLeft(4, '0')}-${two(t.month)}-${two(t.day)}'
      'T${two(t.hour)}:${two(t.minute)}:${two(t.second)}'
      '${off.isNegative ? '-' : '+'}${two(a.inHours)}:${two(a.inMinutes % 60)}';
}

Map<String, dynamic> recordingFrame(String audioB64) =>
    {'type': 'recording', 'audio': audioB64, 'audio_format': 'wav'};

Map<String, dynamic> challengeResponseFrame(String audioB64) =>
    {'type': 'challenge_response', 'audio': audioB64, 'audio_format': 'wav'};

Map<String, dynamic> promptReply(String value) =>
    {'type': 'prompt_reply', 'value': value};

/// A spoken answer to a server list: the WORDS, for the hub to match.
///
/// The phone stopped matching server lists on 2026-09-11 -- see
/// voice_flow's note. `values` is empty on purpose: it means "I have no
/// selection, here is what I heard", which is what nlu/choice_match on the
/// hub answers. An older hub, which does not read `heard`, sees an empty
/// selection and asks again -- the same thing it did when nothing matched.
///
/// `selected` is what was already TICKED on a multi-select when the words
/// were spoken; the hub adds the spoken picks to it ("and the porch").
Map<String, dynamic> choiceHeard(String heard,
        {List<String> selected = const <String>[]}) =>
    {
      'type': 'choice_response',
      'values': const <String>[],
      'via': 'voice',
      'heard': heard,
      if (selected.isNotEmpty) 'selected': selected,
    };

Map<String, dynamic> menuFrame(String action) =>
    {'type': 'menu', 'action': action};

/// The answer to a `file_request`: bare file names and their bytes,
/// base64 (see phone_files.dart, which builds the list).
Map<String, dynamic> fileResponseFrame(List<Map<String, String>> files) =>
    {'type': 'file_response', 'files': files};

/// A `file_request` declined -- the hub raises CancelledByUser, exactly as
/// for every other question's cancel.
Map<String, dynamic> fileResponseCancelled() =>
    {'type': 'file_response', 'cancelled': true};

/// Server frame, decoded. `type` is never null for a well-formed frame;
/// malformed JSON is surfaced as null by the socket layer and never reaches
/// the flow.
class ServerFrame {
  ServerFrame(this.raw);
  final Map<String, dynamic> raw;

  String get type => raw['type'] as String? ?? '?';
  String? get text => raw['text'] as String?;
  String? get reason => raw['reason'] as String?;
  String? get phrase => raw['phrase'] as String?;
  String? get nonce => raw['nonce'] as String?;
  String? get viewName => raw['name'] as String?;
  String? get level => raw['level'] as String?;
  bool get ok => raw['ok'] == true;

  /// A human sentence for TTS: the server's `text` where present, else the
  /// reason code de-snaked -- the server guarantees every error and failed
  /// result carries both (its module docstring says so).
  String describe() =>
      text ?? (reason ?? 'something went wrong').replaceAll('_', ' ');
}