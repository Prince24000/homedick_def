// ignore_for_file: unused_element_parameter

import 'dart:async';
import 'dart:convert' show jsonDecode, jsonEncode;
import 'dart:io';
import 'dart:ui' show ImageFilter;
import 'package:flutter/foundation.dart' show kDebugMode;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:flutter_foreground_task/flutter_foreground_task.dart';
import 'identity.dart';
import 'package:path_provider/path_provider.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'audio/capture.dart';
import 'audio/pipeline.dart';
import 'config.dart';
import 'connection_watch.dart';
import 'phone_files.dart';
import 'flow/tts.dart';
import 'flow/voice_flow.dart';
import 'protocol.dart';
// Prefixed as well: the bare name is shadowed here by the choiceHeard field.
import 'protocol.dart' as wire show choiceHeard;
import 'service/foreground.dart';
import 'socket_client.dart';

enum _AppTheme { light, dark }

// ── Server frame models ──────────────────────────────────────────────

class PromptFrame {
  final String text;

  /// Optional structured hints from the server (protocol addition). Today:
  /// meta['wizard'] = {index, total, last} on multi-field update flows, so
  /// the screen can show Next/Back/Cancel and turn Next into Finish on the
  /// last field without parsing English.
  final Map<String, dynamic>? meta;

  /// Must this answer be masked as it is typed?
  ///
  /// THE SERVER DECIDES. This used to be inferred here, with
  /// `rawText.contains('pin')` — which was wrong in both directions at once:
  /// it hid GPIO pin numbers, because "pin" appears in "Data pin", while
  /// showing the ESPHome API key in full. A field is secret because the flow
  /// asking for it says so, not because of a word in the question.
  final bool secret;
  const PromptFrame({required this.text, this.meta, this.secret = false});
}

class ChoiceOption {
  final String value;
  final String? label;
  final String? sub;
  const ChoiceOption({required this.value, this.label, this.sub});
}

class ChoiceFrame {
  final String text;
  final List<ChoiceOption> options;
  final bool multi;
  final bool allowAll;

  /// What the screen's own Cancel control should SAY for this question.
  ///
  /// Empty means the default wording. It exists because "Cancel" alone is a
  /// poor answer to a destructive question: walking away from "overwrite this
  /// chip?" has a consequence, and the button should name it — "Cancel —
  /// leave the chip as 'kitchen'". The hub sends it; nothing here guesses it
  /// from the question's wording.
  final String cancelLabel;

  const ChoiceFrame({
    required this.text,
    required this.options,
    this.multi = false,
    this.allowAll = false,
    this.cancelLabel = '',
  });
}

/// One instruction from a spoken navigation for the screen on top: scroll
/// the list, or select a row by the id the screen itself advertised.
class VoiceUiCommand {
  final String action; // 'scroll' | 'select'
  final String direction; // scroll: up | down | top | bottom
  final String id; // select: the target id
  final String label;

  /// Set by the VoiceUiScope that carried it out. Still false after
  /// posting means no screen on top could act (nothing to scroll, a sheet
  /// over the list), and the person is told so instead of hearing silence.
  bool handled = false;

  VoiceUiCommand.scroll(this.direction)
      : action = 'scroll',
        id = '',
        label = '';
  VoiceUiCommand.select(this.id, this.label)
      : action = 'select',
        direction = '';
}

/// Something the hub knows is UNFINISHED, with the action that finishes it.
///
/// Computed SERVER-side and pushed as a `notice` frame. The app deliberately
/// does not infer these from result codes — guessing what a flow implied is
/// the fragile pattern that has bitten this app repeatedly.
class Notice {
  final String id;          // stable: a repeat REPLACES, never duplicates
  final String title;
  final String body;
  final String actionKind;  // 'menu' | 'route' | 'none'
  final String actionValue;
  final String actionLabel;
  final DateTime at;

  const Notice({
    required this.id,
    required this.title,
    required this.body,
    this.actionKind = 'none',
    this.actionValue = '',
    this.actionLabel = '',
    required this.at,
  });

  bool get actionable => actionKind == 'menu' || actionKind == 'route';

  Map<String, dynamic> toJson() => {
        'id': id,
        'title': title,
        'body': body,
        'action_kind': actionKind,
        'action_value': actionValue,
        'action_label': actionLabel,
        'at': at.toIso8601String(),
      };

  static Notice fromJson(Map<String, dynamic> m) => Notice(
        id: m['id'] as String? ?? '',
        title: m['title'] as String? ?? '',
        body: m['body'] as String? ?? '',
        actionKind: m['action_kind'] as String? ?? 'none',
        actionValue: m['action_value'] as String? ?? '',
        actionLabel: m['action_label'] as String? ?? '',
        at: DateTime.tryParse(m['at'] as String? ?? '') ?? DateTime.now(),
      );
}

/// One field of a server-driven form.
///
/// Named FlowField, not FormField: Flutter's own `FormField` widget comes in
/// with material.dart, and any screen importing both got an ambiguous_import
/// on the bare name. FlowField also matches what the hub calls these —
/// `_flow_fields` builds them from a Home Assistant config FLOW.
class FlowField {
  final String name;
  final String label;
  final String type;
  final String defaultValue;
  final bool required;
  final String help;
  final List<String> choices;

  /// Masked as typed -- a password field in an integration's config flow.
  /// The hub says so (`secret: true`); nothing here guesses from the name.
  final bool secret;
  const FlowField({
    required this.name,
    required this.label,
    this.type = 'string',
    this.defaultValue = '',
    this.required = false,
    this.help = '',
    this.choices = const [],
    this.secret = false,
  });

  static FlowField fromJson(Map<String, dynamic> m) => FlowField(
        name: m['name'] as String? ?? '',
        label: m['label'] as String? ?? (m['name'] as String? ?? 'Value'),
        type: m['type'] as String? ?? 'string',
        defaultValue: m['default'] == null ? '' : '${m['default']}',
        required: m['required'] == true,
        help: m['help'] as String? ?? '',
        secret: m['secret'] == true,
        choices: [
          for (final c in (m['choices'] as List? ?? const [])) '$c'
        ],
      );
}

/// One credential in a `secret` frame: `secrets: [{label, value}, ...]`.
/// The dialog masks each with its own eye and copy button; `items` in the
/// same frame are plain lines and carry no password any more.
class SecretEntry {
  final String label;
  final String value;
  const SecretEntry({required this.label, required this.value});
}

/// A whole set of fields, asked at once. Home Assistant's config flows are
/// forms; asking them one question at a time is what made a bare field
/// called "host" unanswerable.
/// THE ONE REGISTRATION SCREEN's content, straight from the hub
/// (`registration`). `values` is what the fields show; `rooms` the chips;
/// `kindLocked` greys the kind row (a board part declares its own);
/// `stateKnown` hides "Right now it is" (Home Assistant or the board
/// reports it); `aiText` is the sentence the AI wrote, when it did;
/// `error` the reason the last save was refused.
/// One kind chip as the hub offers it: which kind, its label, whether this
/// part can be it, and the reason when it cannot (2026-09-10 -- the kind
/// is a family, not a lock: a relay may be on/off, open/closed or
/// locked/unlocked; a sensor is a reading and nothing else).
class KindOption {
  final String kind;
  final String label;
  final bool enabled;
  final String why;
  const KindOption(
      {required this.kind,
      required this.label,
      required this.enabled,
      required this.why});
}

class RegistrationFrame {
  final String title;
  final String source;
  final bool editing;
  final bool kindLocked;

  /// The hub takes ANY kind: `enabled` on a kind option then means "what
  /// this part is meant to be" (marked on the form), not "may be picked".
  /// False from an older hub, which refuses the others.
  final bool kindFree;
  final bool stateKnown;
  final bool aiAvailable;
  final String aiText;
  final String error;
  final List<String> rooms;
  final Map<String, dynamic> values;

  /// The kind chips for THIS part, greyed where it cannot honour them.
  /// Empty from an older hub: the screen falls back to its own five.
  final List<KindOption> kindOptions;

  /// Kinds that carry a range on this part: 'level' always; 'open' when
  /// the part has a position (a gate, a blind, a servo, an HA cover).
  final List<String> positionKinds;

  /// After "Fill these in with AI": {mine: {...}, ai: {...}, for: {...}}
  /// -- both sides, so the screen can show them side by side and let the
  /// person choose per row. Null until the AI has answered.
  final Map<String, dynamic>? proposal;

  const RegistrationFrame({
    required this.title,
    required this.source,
    required this.editing,
    required this.kindLocked,
    this.kindFree = false,
    required this.stateKnown,
    required this.aiAvailable,
    required this.aiText,
    required this.error,
    required this.rooms,
    required this.values,
    this.kindOptions = const [],
    this.positionKinds = const ['level'],
    this.proposal,
  });

  static RegistrationFrame fromJson(Map<String, dynamic> m) {
    final raw = m['values'];
    final rawRooms = m['rooms'];
    final rawKinds = m['kind_options'];
    final rawPos = m['position_kinds'];
    final rawProposal = m['proposal'];
    return RegistrationFrame(
      title: (m['title'] as String? ?? 'Add device').trim(),
      source: (m['source'] as String? ?? '').trim(),
      editing: m['editing'] == true,
      kindLocked: m['kind_locked'] == true,
      kindFree: m['kind_free'] == true,
      stateKnown: m['state_known'] == true,
      aiAvailable: m['ai_available'] == true,
      aiText: (m['ai_text'] as String? ?? '').trim(),
      error: (m['error'] as String? ?? '').trim(),
      rooms: [
        for (final r in (rawRooms is List ? rawRooms : const []))
          if (r is String && r.trim().isNotEmpty) r.trim()
      ],
      values: raw is Map<String, dynamic> ? Map<String, dynamic>.from(raw) : {},
      kindOptions: [
        for (final o in (rawKinds is List ? rawKinds : const []))
          if (o is Map && (o['kind'] as String? ?? '').isNotEmpty)
            KindOption(
              kind: o['kind'] as String,
              label: (o['label'] as String? ?? o['kind'] as String).trim(),
              enabled: o['enabled'] != false,
              why: (o['why'] as String? ?? '').trim(),
            )
      ],
      positionKinds: [
        for (final k in (rawPos is List ? rawPos : const ['level']))
          if (k is String && k.isNotEmpty) k
      ],
      proposal: rawProposal is Map<String, dynamic>
          ? Map<String, dynamic>.from(rawProposal)
          : (rawProposal is Map ? Map<String, dynamic>.from(rawProposal) : null),
    );
  }
}

class FormFrame {
  final String title;
  final String note;
  final List<FlowField> fields;
  const FormFrame(
      {required this.title, required this.note, required this.fields});

  /// Identity for the screen's controller cache — a new form must reset the
  /// boxes, a mere rebuild must not.
  String get signature =>
      '$title|${fields.map((f) => f.name).join(',')}';
}

/// A whole board, handed over for laying out.
///
/// Unlike every other frame, this one is not a question with an answer — it
/// is a SCREEN with a result. The catalogue rides along inside it rather
/// than being fetched separately, so the screen can never open against a
/// pin table it does not have, and the hub can change the table without the
/// app being updated.
class PinMapFrame {
  final String boardName;
  final Map<String, dynamic> catalogue;
  final List<dynamic> items;        // already on the board, if editing
  const PinMapFrame(
      {required this.boardName, required this.catalogue, required this.items});
}

/// The whole automation, handed over for building (her Tinkercad design,
/// 2026-08-27). Same shape as PinMapFrame and for the same reason: some
/// things are a SCREEN, not a question. Everything the blocks need
/// arrives at once and the finished rule goes back at once, so nothing is
/// committed while a person changes their mind.
class RuleBuilderFrame {
  /// device id -> {name, room, description, secure, heard, ha_only,
  /// options[]} — every registered device, plus Home Assistant's own
  /// things, each carrying what IT can be asked.
  final Map<String, dynamic> devices;

  /// What a rule may DRIVE: id, label, room, secure, and the envelope.
  final List<dynamic> targets;
  final List<dynamic> scenes;
  final List<dynamic> elseKinds;

  /// The rule being edited, or null when this is a new one.
  final Map<String, dynamic>? rule;

  /// What "Try it now" did, in words (empty until it is tried).
  final List<String> tried;

  /// The sentence the boxes were filled from, and the boxes it could not
  /// fill with confidence ("when", "only if", "do").
  final String heard;
  final List<String> unsure;
  const RuleBuilderFrame({
    required this.devices,
    required this.targets,
    required this.scenes,
    required this.elseKinds,
    this.rule,
    this.tried = const [],
    this.heard = '',
    this.unsure = const [],
  });
}

/// THE SCENE SCREEN: one scene, handed over to build or change. Save,
/// Try it now and "Ask the AI" each send it back; Back cancels it.
class SceneBuilderFrame {
  /// A scene being changed (false: a new one).
  final bool editing;

  /// {name, description, private, actions: [{device, state, device_name,
  /// room, known, words}]}
  final Map<String, dynamic> scene;

  /// What a scene can move: {value, label, room, secure, use}.
  final List<dynamic> targets;

  /// Whether "Everyone / Just me" is offered.
  final bool whom;
  final bool aiAvailable;

  /// What Try it now did, in words, and why Save was refused.
  final List<String> tried;
  final String error;

  /// The AI's suggestion for what the scene is for (shown beside the
  /// person's words, tapped to use), why there is none, and the name it
  /// was asked for.
  final String aiText;
  final String aiError;
  final String aiFor;
  const SceneBuilderFrame({
    required this.editing,
    required this.scene,
    required this.targets,
    this.whom = false,
    this.aiAvailable = false,
    this.tried = const [],
    this.error = '',
    this.aiText = '',
    this.aiError = '',
    this.aiFor = '',
  });
}

class RecordFrame {
  final String phrase;
  final String context;
  final int index;
  final int total;
  const RecordFrame({
    required this.phrase,
    required this.context,
    this.index = 0,
    this.total = 0,
  });
}

// ── AppState ─────────────────────────────────────────────────────────

/// THE MICROPHONE FOLLOWS THE APP — UNLESS THE WAKE WORD IS LISTENING.
///
/// THE MISTAKE THIS CORRECTS (2026-08-29). The first cut released the mic
/// whenever the app was backgrounded, which is the right rule for a
/// push-to-talk app and the WRONG one for this app. The log said so within a
/// minute of shipping:
///
///     08:36:29  [kws] fed 2200 chunks
///     08:36:29  [mic] app backgrounded — microphone released
///     08:36:46  [mic] app resumed — microphone reopened
///     08:36:54  [kws] fed 2300 chunks      <- 25s fed NOTHING
///
/// A wake word you can only say while looking at the app is not a wake word.
/// The green indicator staying lit was never the bug: an always-listening
/// phone SHOULD hold the microphone, and the indicator is Android telling
/// the truth about it.
///
/// THE ACTUAL BUG was the other half — MicCapture's watchdog reopening the
/// stream every ten seconds, which fought WhatsApp for the mic in the middle
/// of a voice note. That fix stays. Holding the mic and CONTESTING it are
/// different things: we hold it, another app takes priority, Android hands
/// us silence, and we accept the silence instead of snatching it back.
///
/// So the rule is now conditional. While [AppState.wakeListening] is on the
/// mic is kept in the background, held up by the foreground service and its
/// visible notification — the honest case, because the person switched it on
/// and can see that it is on. With it off, the old lifecycle rule applies and
/// leaving the app gives the microphone back.
class _MicLifecycle extends WidgetsBindingObserver {
  _MicLifecycle(this._state);
  final AppState _state;

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // if/else rather than a switch over every enum value ON PURPOSE:
    // `AppLifecycleState.hidden` only exists on newer Flutter, and naming a
    // value the SDK does not have is a compile error rather than a warning.
    // This shape behaves identically and builds on either.
    if (state == AppLifecycleState.resumed) {
      _state._resumeMic();
      // Back in the app: what is outstanding now, at once.
      _state._appForeground = true;
      _state.requestNotices();
    } else if (state == AppLifecycleState.inactive) {
      // DELIBERATELY NOT RELEASED. `inactive` fires for a notification shade
      // pull, a permission dialog, an incoming-call banner — moments the
      // person has not actually left. Dropping the mic here would make the
      // orb go deaf mid-sentence every time a notification arrived.
    } else {
      // paused, detached, and hidden where it exists: really gone.
      _state._appForeground = false;
      _state._releaseMic();
    }
  }
}

class AppState extends ChangeNotifier {
  final GlobalKey<NavigatorState> navigatorKey = GlobalKey<NavigatorState>();
  late final _MicLifecycle _micLifecycle = _MicLifecycle(this);

  /// For toasts ("Done -- kitchen is settled."): a messenger that belongs
  /// to the app, not to whichever screen happened to be up when the hub
  /// finished something.
  final GlobalKey<ScaffoldMessengerState> scaffoldMessengerKey =
      GlobalKey<ScaffoldMessengerState>();
  /// THIS PHONE'S IDENTITY -- its keypair, its id, and the hub certificate
  /// it learned at pairing. See identity.dart; nothing else in the app
  /// touches those storage keys. (The FlutterSecureStorage field that used
  /// to sit here held one `shared_secret` every phone typed identically;
  /// identity.dart owns secure storage now.)
  final identity = PhoneIdentity();

  // Pipeline
  VoiceFlow? _flow;
  SocketClient? _socket;
  MicCapture? _mic;
  Voice? _voice; // kept so the mute toggle reaches the live TTS engine

  // FIX: Track the SocketClient state subscription so we can cancel it on dispose.
  StreamSubscription<ConnState>? _connSub;
  StreamSubscription<LinkQuality>? _linkSub;

  // FIX (screens not rendering, root cause #1): AppState.onServerFrame existed
  // but NOTHING ever subscribed it to SocketClient.frames — only VoiceFlow
  // listened. So pendingPrompt/pendingChoice/pendingRecord/challengePhrase
  // were never set and no screen was ever pushed. This subscription is the
  // missing wire.
  StreamSubscription<ServerFrame>? _frameSub;

  // Boot / connection
  String status = 'starting…';
  bool running = false;
  bool isBooting = true;
  String? blockingProblem;
  ConnState connState = ConnState.idle;

  // Theme. LIGHT UNTIL CHANGED: a fresh install opens light, and the
  // switch in Settings is remembered (`is_light` in prefs) so the mode you
  // left the app in is the mode it opens in. _loadTheme runs inside
  // init(), which main() awaits before runApp, so the first frame is
  // already the remembered mode -- no dark flash before a light app.
  _AppTheme _theme = _AppTheme.light;
  bool get isLight => _theme == _AppTheme.light;

  // TTS mute (persisted)
  bool ttsMuted = false;

  /// The wake phrase for the engines that read words: "hello nova".
  String get wakePhraseText => AppConfig.wakePhraseText;

  // ── THE DOCK, HIDDEN OR SHOWN (2026-10-15) ────────────────────────────
  //
  // Hidden, the dock folds to a small tab on the right edge and gives the
  // screen its strip back; the voice keeps working (wake phrase, replies).
  // Remembered across restarts.
  bool dockHidden = false;

  Future<void> setDockHidden(bool hidden) async {
    dockHidden = hidden;
    notifyListeners();
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setBool('dock_hidden', hidden);
    } catch (_) {}
  }

  /// May a spoken command move around the app ("open automations", "go
  /// back", "scroll down", "select kitchen night")? When on, every command
  /// frame carries the current screen and its labels, and the hub's `ui`
  /// frames are carried out against _handleUi's allowlist.
  bool voiceNavigation = true;

  // NO OUTPUT FEED AND NO ROW COLLECTORS. `serverMessages` (the home
  // "Server" card's feed) and the `deviceLines`/`sceneLines` collectors
  // outlived everything that read them: the card and the screens' text
  // fallbacks are gone, and the hub no longer prints device or scene rows
  // to a phone at all (IOBackend.renders_data) -- the structured frames
  // are the only source. The lines they held were written and never read.

  // Board-report collection: firmware, drivers, the I2C scan and live
  // readings, asked over the air -- the one collector left, cleared
  // wherever a flow ends, for the reason the note below gives: a collect
  // flag that fails to clear eats output for ever. Read once in a dialog
  // rather than pushed as a screen: it is four paragraphs you read and
  // dismiss, not a list you act on.
  bool _collectingReport = false;
  final List<String> reportLines = [];

  // NO _collectingProfile FLAG. There was one, and it was a mistake I made
  // and she caught: it swallowed the hub's profile lines so they would not
  // land in the orb's feed, and it was cleared in exactly ONE place — the
  // `menu:profile` result. Its siblings (the device and scene collectors,
  // since removed) were cleared in six and four places (refusal, error,
  // disconnect, each list's own result), because a collect-flag that
  // fails to clear keeps eating output forever.
  //
  // Mine was worse than theirs in kind, not just in count: they COLLECT into
  // a list, so a stuck flag misfiles lines. Mine DROPPED them, so a stuck
  // flag destroyed them with nothing on screen to say so.
  //
  // The hub decides now (IOBackend.renders_data): it prints the words only
  // for a terminal, which cannot draw the frame. A rule that lives where the
  // answer is certain cannot get stuck in the wrong position.

  /// The SAME scenes as structured records (`scene_list`), and the path the
  /// screen prefers.
  ///
  /// The text rows above were the only source until 2026-08-27, so the
  /// Scenes screen recovered each scene's name by regex-parsing the printed
  /// line "  1. movie time". That made a console format into an API. It was
  /// survivable while the screen only displayed scenes; it stopped being
  /// survivable when tapping a row began to edit and delete them, because
  /// the scraped name is the argument naming WHAT gets deleted. The rows
  /// stay as the fallback for an older hub.
  final List<Map<String, dynamic>> sceneRecords = [];

  /// SAVED LAYOUTS (`layout_list`): wiring plans kept under a name for one
  /// chip family. Each record: id, name, chip, board_label, count, parts
  /// (name / part / pins [{slot, gpio, component}]), created, updated,
  /// source. They control nothing; only the person deletes one.
  final List<Map<String, dynamic>> layoutRecords = [];

  /// Informational lines from the running flow since the last answered
  /// question — shown as a banner on question screens and beside the busy
  /// spinner. ACCUMULATED, not overwritten: the AI flow writes
  /// "Description: …" then "Behaves as: …" back-to-back, and keeping only
  /// the last line hid the description the person is being asked to
  /// approve. Cleared when a question is answered and when the flow ends.
  final List<String> _flowLines = [];
  String? get lastFlowMessage =>
      _flowLines.isEmpty ? null : _flowLines.join('\n');

  /// The running account, oldest first — what the Working screen reads.
  /// Unmodifiable: the screen shows this list, it does not own it.
  List<String> get flowLines => List.unmodifiable(_flowLines);

  /// True when `text` is (about to be / just been) answered by the script —
  /// VoiceFlow consults this so auto-answered questions are never spoken.
  /// The keepsafe acknowledgement ("Type OK once you've written it down")
  /// is included: the phrase dialog's button answers it, not a screen.
  bool promptHandledSilently(String text) =>
      keepsafeMessage != null &&
      text.toLowerCase().contains('written it down');

  /// True from a menu action being sent until its result arrives — drives
  /// the working spinner (AI generation takes tens of seconds on the Pi).
  bool menuBusy = false;

  /// One-shot: a connect attempt failed. HomeScreen turns this into a help
  /// dialog (Wi-Fi on? hub powered? Tailscale up?) instead of the bare
  /// "server unreachable" status line.
  bool connectFailed = false;
  void clearConnectFailed() => connectFailed = false;

  /// The enter-secret overlay's REAL gate. It used to be inferred from a
  /// substring of the status line, which any later status write erased —
  /// so a refused phone left no way back. Set on first boot (this phone
  /// has no identity yet), when the hub refuses this phone's signature,
  /// and by "Pair with a hub"; cleared by completePairing().
  //
  // Remote access (Tailscale) keeps NO state here: the key is never kept
  // anywhere on this phone, and the hub's answer goes to `status` and a
  // popup. (A `tailscaleResult` field held that answer too; nothing but
  // the same handlers ever read it, so it went, like `tailscaleBusy`.)

  /// Ask the hub to join a tailnet. With a key, it spends it on
  /// `tailscale up`; with an EMPTY key it starts the keyless sign-in -- the
  /// hub pushes Tailscale's login page back as a web screen and completes
  /// when the person finishes there.
  ///
  /// FIRE AND LISTEN, and there is now a conversation in between: the hub
  /// asks for this person's PIN before it will touch remote access, and if
  /// it is already on a tailnet it asks whether to spend the key and risk
  /// moving. Both arrive as the ordinary prompt/choice frames this app
  /// already draws, so nothing here has to know about them. The ending
  /// still comes back as tailscale_joined/_failed.
  void sendTailscaleJoin(String authKey, {String hostname = ''}) {
    final sock = _socket;
    if (sock == null || connState != ConnState.ready) {
      // THIS USED TO BE SILENT (audit, 2026-09-12): it wrote a field no
      // widget renders and returned, so the dialog closed, nothing
      // appeared anywhere, and a one-shot tskey- was spent on nothing.
      //
      // Worse than an ordinary silent failure, because this is the row you
      // reach for WHEN you are cut off: it is deliberately enabled while
      // disconnected, and the hub pushes a sticky notice routing you
      // straight to it.
      status = 'not connected to the hub, so sign-in did not start';
      notifyListeners();
      WidgetsBinding.instance.addPostFrameCallback((_) => _showResultPopup(
          ok: false,
          title: 'Not connected to the hub',
          message: 'Sign-in starts on the hub, so the phone has to reach it '
              'first: be on the hub\'s Wi-Fi, or have Tailscale switched on '
              'in this phone. Nothing was changed.'));
      return;
    }
    sock.send(tailscaleJoinFrame(authKey: authKey, hostname: hostname));
  }

  /// Sign the hub out of Tailscale (the hub asks for the PIN).
  void sendTailscaleLogout() {
    final sock = _socket;
    if (sock == null || connState != ConnState.ready) {
      WidgetsBinding.instance.addPostFrameCallback((_) => _showResultPopup(
          ok: false,
          title: 'Not connected',
          message: 'The phone is not connected to the hub right now, so '
              'nothing was changed.'));
      return;
    }
    sock.send({'type': 'tailscale_logout'});
  }

  /// Sign in: asks once whether the app may come back by itself when the
  /// hub is done (Android's "Display over other apps"), then starts.
  Future<void> startTailscaleSignIn() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final ctx = navigatorKey.currentContext;
      if (Platform.isAndroid &&
          !(prefs.getBool('ts_return_asked') ?? false) &&
          !await FlutterForegroundTask.canDrawOverlays &&
          ctx != null &&
          ctx.mounted) {
        await prefs.setBool('ts_return_asked', true);
        final allow = await showDialog<bool>(
          context: ctx,
          builder: (d) => AlertDialog(
            backgroundColor: surface,
            surfaceTintColor: Colors.transparent,
            content: Text(
                'Come back to the app by itself when sign-in is done?',
                style: TextStyle(color: ink, fontSize: 15)),
            actions: [
              TextButton(
                  onPressed: () => Navigator.pop(d, false),
                  child: Text('Not now', style: TextStyle(color: inkDim))),
              TextButton(
                  onPressed: () => Navigator.pop(d, true),
                  child: Text('Allow', style: TextStyle(color: accent))),
            ],
          ),
        );
        if (allow == true) {
          await FlutterForegroundTask.openSystemAlertWindowSettings();
        }
      }
    } catch (_) {
      // Asking is a convenience: sign-in starts either way.
    }
    sendTailscaleJoin('');
  }

  /// The hub says done: back to the app (if Android lets it), then one
  /// short line -- and whether Tailscale is on in this phone.
  Future<void> _tailscaleDone(String title, String sameText) async {
    try {
      if (Platform.isAndroid && await FlutterForegroundTask.canDrawOverlays) {
        FlutterForegroundTask.launchApp();
      }
    } catch (_) {}
    if (sameText.isNotEmpty) {
      _showResultPopup(ok: true, title: title, message: sameText);
      return;
    }
    final on = await SocketClient.phoneOnTailnet();
    _showResultPopup(
        ok: true,
        title: title,
        message: on
            ? 'Ready. It works away from home.'
            : 'Now switch Tailscale on in this phone (same account).');
    if (!on) _watchPhoneTailscale();
  }

  /// Tailscale switched on in this phone after all: say so, once.
  void _watchPhoneTailscale() {
    var tries = 0;
    Timer.periodic(const Duration(seconds: 3), (t) async {
      if (++tries > 60) {
        t.cancel();
        return;
      }
      if (await SocketClient.phoneOnTailnet()) {
        t.cancel();
        floatPill('Tailscale is on. It works away from home.');
      }
    });
  }

  /// Drop a sign-in halfway: the hub stops it and stays as it was.
  void cancelTailscaleSignIn() {
    if (!tailscaleSigningIn) return;
    _socket?.send({'type': 'tailscale_cancel'});
    _closeTailscaleWaiting();
  }

  Future<void> _openInBrowser(String url) async {
    try {
      final ok = await launchUrl(Uri.parse(url),
          mode: LaunchMode.externalApplication);
      if (!ok) _showToast('No browser opened -- tap Copy link instead.');
    } catch (_) {
      _showToast('No browser opened -- tap Copy link instead.');
    }
  }

  /// "Finish signing in in your browser": up until the hub answers. Only
  /// its success changes anything; Cancel drops the sign-in.
  void _showTailscaleWaiting() {
    final ctx = navigatorKey.currentContext;
    if (ctx == null || !tailscaleSigningIn || _tsWaitCtx != null) return;
    showDialog<void>(
      context: ctx,
      barrierDismissible: false,
      builder: (dctx) {
        _tsWaitCtx = dctx;
        return PopScope(
          canPop: false,
          child: AlertDialog(
            backgroundColor: surface,
            surfaceTintColor: Colors.transparent,
            shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(16),
                side: BorderSide(color: border)),
            title: Row(children: [
              SizedBox(
                  width: 18,
                  height: 18,
                  child: CircularProgressIndicator(
                      strokeWidth: 2, color: accent)),
              const SizedBox(width: 12),
              Expanded(
                child: Text('Sign in to Tailscale',
                    style: TextStyle(
                        color: ink, fontSize: 17, fontWeight: FontWeight.w600)),
              ),
            ]),
            content: StreamBuilder<int>(
              stream: Stream.periodic(const Duration(seconds: 1), (i) => i),
              builder: (_, __) {
                final at = _tsSignInAt ?? DateTime.now();
                final left = const Duration(minutes: 15) -
                    DateTime.now().difference(at);
                final secs = left.isNegative ? 0 : left.inSeconds;
                final clock = '${secs ~/ 60}:'
                    '${(secs % 60).toString().padLeft(2, '0')}';
                return Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                        'Sign in on the page that opened.\n'
                        'It needs internet on this phone. No internet? '
                        'Copy link to another device.\n'
                        'Don\'t change this phone\'s network.',
                        style: TextStyle(
                            color: inkDim, fontSize: 13.5, height: 1.45)),
                    const SizedBox(height: 12),
                    Text('Waiting for you: $clock left',
                        style: TextStyle(
                            color: ink,
                            fontSize: 13.5,
                            fontWeight: FontWeight.w600)),
                  ],
                );
              },
            ),
            actions: [
              TextButton(
                onPressed: () => cancelTailscaleSignIn(),
                child: Text('Cancel', style: TextStyle(color: cancelInk)),
              ),
              TextButton(
                onPressed: () async {
                  await Clipboard.setData(
                      ClipboardData(text: tailscaleSignInUrl));
                  _showToast('Link copied -- open it in any browser.');
                },
                child: Text('Copy link', style: TextStyle(color: accent)),
              ),
              TextButton(
                onPressed: () => _openInBrowser(tailscaleSignInUrl),
                child: Text('Open again', style: TextStyle(color: accent)),
              ),
            ],
          ),
        );
      },
    ).whenComplete(() => _tsWaitCtx = null);
  }

  void _closeTailscaleWaiting() {
    tailscaleSigningIn = false;
    final c = _tsWaitCtx;
    _tsWaitCtx = null;
    if (c != null && c.mounted) Navigator.of(c).pop();
  }

  bool needPairing = false;

  /// This phone has no hub it is paired with. The app can still be opened
  /// (see [dismissPairing]); it dials nothing, and Home offers "Pair this
  /// phone" where Connect would be.
  bool unpaired = false;
  bool _pairLater = false;

  /// Back out of an accidental "Pair with a hub" — the stored identity
  /// stays untouched and the app carries on as it was.
  ///
  /// "LATER" ON A PHONE THAT HAS NEVER PAIRED used to clear the flag and
  /// nothing else: no models unpacked, nothing running, so Home drew an
  /// empty screen ("when I select later nothing happens, just plain
  /// screen", 2026-10-06). Now the app starts without a hub: the models
  /// are unpacked and Home, Settings and the rest open as usual.
  void dismissPairing() {
    needPairing = false;
    if (!running && !isBooting) {
      _pairLater = true;
      notifyListeners();
      _boot();
      return;
    }
    notifyListeners();
  }

  // ── Pending actions ────────────────────────────────────────────────
  //
  // Persisted, because "there is something you still need to do" that
  // evaporates when the app restarts is not a reminder.
  final List<Notice> notices = [];
  String get _noticesKey => _hubKey('notices');

  /// SEEN AND RESOLVED ARE DIFFERENT THINGS, and the red dot tracks the
  /// first while the list tracks the second. Opening Notifications marks
  /// everything seen -- the dot goes -- but a message leaves the LIST only
  /// when its condition actually clears (the hub says notice_gone, or its
  /// id drops off the recheck). A rewire waiting for its one answer stays
  /// visible through any number of viewings.
  final Set<String> _seenNoticeIds = {};
  String get _seenNoticesKey => _hubKey('notices_seen');

  bool get hasUnseenNotices =>
      notices.any((n) => !_seenNoticeIds.contains(n.id));

  void markNoticesSeen() {
    var changed = false;
    for (final n in notices) {
      changed = _seenNoticeIds.add(n.id) || changed;
    }
    if (!changed) return;
    _saveNotices();
    notifyListeners();
  }

  void _pruneNotices(List<String> currentIds) {
    final keep = currentIds.toSet();
    final before = notices.length;
    notices.removeWhere((n) => !keep.contains(n.id));
    _seenNoticeIds.retainWhere(keep.contains);
    if (notices.length != before) {
      _saveNotices();
      notifyListeners();
    }
  }

  Future<void> _loadNotices() async {
    final prefs = await SharedPreferences.getInstance();
    final raw = prefs.getStringList(_noticesKey) ?? const [];
    notices
      ..clear()
      ..addAll([
        for (final entry in raw)
          if (jsonDecode(entry) case final Map<String, dynamic> m)
            Notice.fromJson(m)
      ]);
    _seenNoticeIds
      ..clear()
      ..addAll(prefs.getStringList(_seenNoticesKey) ?? const []);
  }

  Future<void> _saveNotices() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setStringList(
        _noticesKey, [for (final n in notices) jsonEncode(n.toJson())]);
    await prefs.setStringList(_seenNoticesKey, _seenNoticeIds.toList());
  }

  /// The hub's Tailscale address is already among this phone's server
  /// addresses: remote access was set up.
  bool get _hasTailnetAddress =>
      (_socket?.candidates ?? const <String>[]).any((u) {
        final h = Uri.tryParse(u)?.host ?? '';
        return SocketClient.isTailnetIp(h) || h.endsWith('.ts.net');
      });

  /// "not_set_up" from the hub while its Tailscale address is known here:
  /// it was set up (the hub may just have no internet), so "paused".
  String _seenOnTailnet(String state) =>
      state == 'not_set_up' && _hasTailnetAddress ? 'paused' : state;

  void _handleNotice(Map<String, dynamic> msg) {
    if (msg['id'] == 'tailnet:not_joined' &&
        hubTailnetState == 'paused' &&
        msg['title'] == 'Remote access is not set up' &&
        _hasTailnetAddress) {
      msg = {
        ...msg,
        'title': 'Remote access is paused',
        'body': 'The hub has no internet, so Tailscale cannot connect. At '
            'home everything still works; away-from-home access comes back '
            'by itself once the hub has internet.',
      };
    }
    final notice = Notice.fromJson({...msg, 'at': DateTime.now().toIso8601String()});
    if (notice.id.isEmpty) return;
    // Same id replaces (the hub recomputes these on every connect).
    final fresh = notices.every(
        (n) => n.id != notice.id || n.title != notice.title);
    notices.removeWhere((n) => n.id == notice.id);
    notices.insert(0, notice);
    _saveNotices();
    if (msg['now'] == true && notice.actionable) {
      // Asked about at once -- but once, not at every recheck.
      if (fresh) {
        WidgetsBinding.instance
            .addPostFrameCallback((_) => _showNextStepPopup(notice));
      }
    } else if (fresh && !_routineNotice(notice.id)) {
      // New (or changed) and important: it floats once; the bell keeps it.
      // Several arriving together become one card.
      _floatQueue.add(notice);
      _floatFlush ??=
          Timer(const Duration(milliseconds: 400), _flushNoticeFloats);
    }
    notifyListeners();
  }

  /// Routine notices go quietly to the bell, with no card.
  static const _routinePrefixes = [
    'devices:', 'ha:', 'boards:', 'custom:', 'layout:', 'resumelayout:',
    'setup:',
  ];
  bool _routineNotice(String id) => _routinePrefixes.any(id.startsWith);

  final List<Notice> _floatQueue = [];
  Timer? _floatFlush;

  void _flushNoticeFloats() {
    _floatFlush = null;
    final batch = List<Notice>.of(_floatQueue);
    _floatQueue.clear();
    if (batch.isEmpty) return;
    if (batch.length == 1) {
      final n = batch.first;
      floatNotification(
          title: n.title,
          body: n.body,
          warn: true,
          actionLabel: n.actionable
              ? (n.actionLabel.isEmpty ? 'Open' : n.actionLabel)
              : '',
          onAction: n.actionable ? () => runNotice(n) : null);
      return;
    }
    floatNotification(
        title: '${batch.length} new notices',
        body: batch.take(3).map((n) => n.title).join(' · '),
        warn: true,
        icon: Icons.notifications_active_outlined,
        actionLabel: 'Open',
        onAction: () => openScreen('/notifications'));
  }

  /// Notices kept current while the app is open: asked again every 45 s
  /// (and on coming back to the app), so nothing waits for a reconnect.
  Timer? _noticeTimer;
  bool _appForeground = true;

  void _keepNoticesCurrent() {
    _noticeTimer ??= Timer.periodic(const Duration(seconds: 45), (_) {
      if (_appForeground && isReady) requestNotices();
    });
  }

  void dismissNotice(String id) {
    notices.removeWhere((n) => n.id == id);
    _saveNotices();
    notifyListeners();
  }

  /// Do what a notice asks. It does NOT retire it.
  ///
  /// It used to -- dismiss first, then act -- which meant cancelling the
  /// action half-way left no trace that anything was still owed. The hub
  /// is the one who knows when a condition has actually cleared, and it
  /// says so (notice_gone, or the id dropping off a recheck). Until then
  /// the message stands, however it is tapped or viewed.
  void runNotice(Notice notice) {
    switch (notice.actionKind) {
      case 'menu':
        // One flow at a time on this connection: firing a second action
        // mid-flow only earns a `busy` refusal from the hub, and used to
        // wipe the running flow's local state on the way.
        if (menuBusy) {
          _showToast('Finish what is on screen first.');
          return;
        }
        sendMenuAction(notice.actionValue);
        break;
      case 'route':
        // Remote access opens its sheet where the person is.
        if (notice.actionValue == '/remote_access') {
          final open = remoteAccessOpener;
          if (open != null) {
            open();
          } else {
            openScreen('/settings');
          }
          return;
        }
        // The same allowlist `open` uses: notices are replayed from prefs,
        // so never route to an arbitrary string.
        final route = _routableScreens[notice.actionValue];
        if (route == null) {
          debugPrint('[AppState] notice: unknown route "${notice.actionValue}"');
          return;
        }
        openScreen(route);
        break;
    }
  }

  /// Routes a notice or an `open` frame may send the app to -- every
  /// destination screen in main.dart's table, none of the flow screens.
  static const _routableScreens = {
    '/settings': '/settings',
    '/devices': '/devices',
    '/scenes': '/scenes',
    '/notifications': '/notifications',
    '/automations': '/automations',
    '/profile': '/profile',
    '/stats': '/stats',
  };

  /// A passing SnackBar, on whatever screen is up.
  /// "Hello, Bruce Wayne. Good evening and welcome." -- SHOWN here once the
  /// hub reports a real sign-in or a new user (never on a plain re-pair); the
  /// voice flow says it (VoiceFlow.welcomeLine).
  void _greet(String name) {
    final line = VoiceFlow.welcomeLine(name.trim());
    if (line.isEmpty) return;
    _showToast(line);
    status = line;
    notifyListeners();
  }

  void _showToast(String text) {
    if (text.isEmpty) return;
    scaffoldMessengerKey.currentState?.showSnackBar(SnackBar(
      content: Text(text,
          style: TextStyle(color: accentFg, fontWeight: FontWeight.w600)),
      backgroundColor: accent,
      behavior: SnackBarBehavior.floating,
      duration: const Duration(seconds: 3),
    ));
  }

  /// Menu actions the APP fires by itself. They must never produce a
  /// success or failure popup: nobody pressed anything, so a card saying
  /// "Done" is an interruption reporting on a question they never asked.
  static const _backgroundActions = {'menu:check_notices'};

  /// Ask the hub what is outstanding. Cheap, read-only and ungated, so it
  /// runs on connect rather than waiting to be asked for.
  void requestNotices() {
    if (!isReady) return;
    _flow?.sendMenuAction('check_notices');
  }

  /// Structured device records from the server's `device_list` frame
  /// (added alongside the text table — see main_py_device_list_patch).
  /// The Devices screen renders from these. (The text rows that were the
  /// fallback for an unpatched server are gone; the hub no longer prints
  /// them to a phone.)
  final List<Map<String, dynamic>> deviceRecords = [];

  /// The BOARDS behind those devices, from the same frame — slug, name,
  /// topic, chip, and which device ids live on each.
  ///
  /// They are here because the merged Devices screen has to show them.
  /// Reaching a board's wiring, name or password means "Update device → A
  /// board" on the hub, so collapsing four drawer rows into one screen
  /// would have quietly lost that door. Listing them is also the first
  /// time the app can answer "what boards do I actually have?".
  final List<Map<String, dynamic>> boardRecords = [];

  /// Every reading a display line can quote: {label, token}. The token is
  /// what the hub fills in live ({device:key}).
  final List<Map<String, dynamic>> displayReadings = [];

  /// The main meter, from the same frame: {device, key, measures}, or null.
  /// Its jumps teach the hub what each device draws.
  Map<String, dynamic>? mainMeter;

  /// Use a device from its page: on/off, a level, a colour, a press, a
  /// line on a display... (the hub's `usedevice` action). False when
  /// nothing was sent (not connected, or a flow is running).
  bool useDevice(String id, String what, [String value = '']) {
    if (!isReady) {
      _showToast('Not connected to the hub.');
      return false;
    }
    if (menuBusy || _pendingKind != null) {
      _showToast('Finish what is on screen first.');
      return false;
    }
    return sendMenuAction('usedevice:$id|$what|$value');
  }

  /// Make one reading of a meter the main meter (the hub's `mainmeter`
  /// action); `measures` is "power" or "current". Changing it makes the
  /// hub forget what it learned.
  bool setMainMeter(String id, String key, String measures) =>
      _deviceAct('mainmeter:$id|$key|$measures');

  /// No main meter: nothing more is learned.
  bool clearMainMeter() => _deviceAct('mainmeter:none');

  /// Forget what one device draws (it is learned again).
  bool forgetDraw(String id) => _deviceAct('forgetdraw:$id');

  bool _deviceAct(String action) {
    if (!isReady) {
      _showToast('Not connected to the hub.');
      return false;
    }
    if (menuBusy || _pendingKind != null) {
      _showToast('Finish what is on screen first.');
      return false;
    }
    return sendMenuAction(action);
  }

  /// WHO YOU ARE, from the hub's `profile` frame.
  ///
  /// [profileYou] is your own record — has_voice / has_pin / has_recovery and
  /// the date the recovery words were minted. [profilePeople] is everyone the
  /// hub knows, [profilePhone] is the handset actually asking.
  ///
  /// There is NO text fallback for any of it. Every field here is a FACT the
  /// hub looked up in a store, and the one thing this screen must never do is
  /// infer a credential's state from a printed sentence: "PIN: set" scraped
  /// out of prose is a security claim made by a regex. A hub too old to send
  /// the frame shows an empty profile and says so.
  Map<String, dynamic>? profileYou;
  Map<String, dynamic>? profilePhone;
  String profileMe = '';
  final List<Map<String, dynamic>> profilePeople = [];
  /// Every handset bound to YOU (`phones` in the profile frame): name and
  /// device string, so a reinstall or a second phone is visible.
  final List<Map<String, dynamic>> profilePhones = [];

  /// The enrolled person this handset is bound to, from `auth_ok`.
  ///
  /// Separate from [profileMe], which arrives only once somebody opens the
  /// Profile screen — and the drawer's nameplate has to be able to name you
  /// BEFORE anything is tapped. Empty against an older hub, and empty on a
  /// paired phone whose holder has not enrolled yet; the nameplate is then
  /// simply absent rather than guessing.
  String boundUser = '';

  /// Every sensor the house can read, from the hub's `readings` frame: live
  /// value and unit, plus the last 24 hours as min/max/avg where Home
  /// Assistant has numeric history for it.
  ///
  /// There is NO text fallback here (nor, now, for devices). The readings flow
  /// gained its data frame and its screen in the same change, so a server old
  /// enough to omit the frame is old enough to have no Statistics door
  /// either -- and a screen that scraped "kitchen power: 41.2 W" back out of
  /// a printed table would be parsing prose to recover numbers the hub
  /// already had as numbers.
  final List<Map<String, dynamic>> readingRecords = [];

  /// The home screen's tiles: the same rows, without the 24-hour trio,
  /// pushed by the hub every few seconds while a phone is connected. Kept
  /// apart from [readingRecords] so the cheap frame can never overwrite
  /// what the Statistics screen paid Home Assistant for.
  final List<Map<String, dynamic>> homeReadings = [];

  /// Is the readings section on home folded shut? Per phone, remembered
  /// only for this run -- a fold is a glance, not a setting.
  bool readingsCollapsed = false;
  void toggleReadings() {
    readingsCollapsed = !readingsCollapsed;
    notifyListeners();
  }

  /// True between asking for readings and the result frame arriving, so the
  /// screen can say "reading the sensors" instead of "no sensors" -- the hub
  /// queries Home Assistant once per sensor for history, which on a Pi is
  /// visibly not instant.
  bool _awaitingReadings = false;
  bool get awaitingReadings => _awaitingReadings;

  /// When the hub's pairing window closes, or null when it is shut. HUB-
  /// ANNOUNCED, never assumed: the hub sends `pair_window` when a window
  /// opens (menu action, --pair at its console, or its own two-minute boot
  /// window) and on every connect while one is open -- so a fresh install
  /// pairing in through the boot window sees the countdown too. The app
  /// only ever counts down to the deadline the hub named; it never starts
  /// a clock of its own, so a dead hub cannot leave a door looking open.
  DateTime? pairWindowUntil;
  Timer? _pairTicker;

  /// "1:47" for the chip. Null when the window is shut.
  String? get pairWindowCountdown {
    final until = pairWindowUntil;
    if (until == null) return null;
    final left = until.difference(DateTime.now()).inSeconds;
    if (left <= 0) return null;
    return '${left ~/ 60}:${(left % 60).toString().padLeft(2, '0')}';
  }

  /// The chip reports the HUB'S state, so losing the hub clears it: a
  /// countdown surviving a disconnect would claim knowledge the app no
  /// longer has. On reconnect the hub re-announces an open window anyway.
  void _clearPairWindow() {
    pairWindowUntil = null;
    _pairTicker?.cancel();
    _pairTicker = null;
  }

  void _handlePairWindow(Map<String, dynamic> map) {
    if (map['open'] == true) {
      // EVERY OPEN WINDOW IS SHOWN, the hub's boot window included: the
      // moment a phone is connected it sees how long pairing stays open.
      final seconds = (map['seconds'] as num?)?.toInt() ?? 0;
      pairWindowUntil = seconds > 0
          ? DateTime.now().add(Duration(seconds: seconds))
          : null;
      _pairTicker?.cancel();
      if (pairWindowUntil != null) {
        // One repaint per second while the door is open. The ticker also
        // notices the deadline passing and shuts ITSELF down -- natural
        // expiry is deliberately not announced by the hub, because every
        // phone already holds the absolute deadline.
        _pairTicker = Timer.periodic(const Duration(seconds: 1), (_) {
          if (pairWindowUntil == null ||
              !pairWindowUntil!.isAfter(DateTime.now())) {
            pairWindowUntil = null;
            _pairTicker?.cancel();
            _pairTicker = null;
          }
          notifyListeners();
        });
      }
    } else {
      pairWindowUntil = null;
      _pairTicker?.cancel();
      _pairTicker = null;
      // Somebody came through the door: a card that names them, shown and
      // gone. The VOICE announcement is not made here -- the hub sends it
      // as an `important` output in the same breath, and the existing
      // speech path voices those; two speakers would say it twice.
      final who = map['phone'] as String?;
      if (map['reason'] == 'paired' && who != null && who.isNotEmpty) {
        _showPairedPopup(who);
        // ...and the notice that asks whether it is ours (Check it): the
        // hub computes it per phone, so ask for the list again.
        requestNotices();
      }
    }
    notifyListeners();
  }

  /// A DROP-DOWN banner from the top of the screen: an event announced
  /// and gone, blocking nothing ("shows a drop down notification", field
  /// test 2026-08-25 — this replaced a centre dialog). Auto-dismisses;
  /// OK dismisses it sooner.
  void _showDropBanner(String text,
      {IconData icon = Icons.phonelink_ring}) {
    floatNotification(title: text, icon: icon);
  }

  // ── FLOATING NOTIFICATIONS (2026-11-10) ──────────────────────────────
  //
  // A card over the top of whatever is on screen: nothing under it moves.
  // Swipe it up or tap the cross; it also tucks itself away. One at a
  // time (a newer one replaces it). The bell keeps every notice.

  OverlayEntry? _floatEntry;
  Timer? _floatTimer;

  void floatNotification({
    required String title,
    String body = '',
    IconData icon = Icons.notifications_none_rounded,
    bool warn = false,
    String actionLabel = '',
    VoidCallback? onAction,
    Duration stay = const Duration(seconds: 6),
  }) {
    _putFloat(
        (close) => _FloatCard(
              s: this,
              title: title,
              body: body,
              icon: icon,
              warn: warn,
              actionLabel: actionLabel,
              onAction: onAction == null
                  ? null
                  : () {
                      close();
                      onAction();
                    },
              onClose: close,
            ),
        stay);
  }

  /// Good news, small: a pill that fades.
  void floatPill(String text) {
    _putFloat((close) => _FloatPill(s: this, text: text, onClose: close),
        const Duration(milliseconds: 2600));
  }

  void _putFloat(Widget Function(VoidCallback close) build, Duration stay) {
    final overlay = navigatorKey.currentState?.overlay;
    if (overlay == null) return;
    _hideFloat();
    late final OverlayEntry entry;
    void close() {
      if (identical(_floatEntry, entry)) _hideFloat();
    }

    entry = OverlayEntry(builder: (_) => build(close));
    _floatEntry = entry;
    overlay.insert(entry);
    _floatTimer = Timer(stay, close);
  }

  void _hideFloat() {
    _floatTimer?.cancel();
    _floatTimer = null;
    final e = _floatEntry;
    _floatEntry = null;
    if (e != null && e.mounted) e.remove();
  }

  /// Who just paired -- shown as the drop-down banner. Vanishes on its
  /// own because it reports an EVENT, not a task -- there is nothing to
  /// do about it, so it must not wait for a tap the way notices do.
  void _showPairedPopup(String who) {
    _showDropBanner(
        '$who has joined — paired with this hub. The pairing window is '
        'closed.');
  }

  // Server-driven flow
  String currentView = 'command';

  /// Bumped on every prompt, choice and form the hub asks. A screen keys
  /// its per-question state (typed text, ticked options) on it, so the
  /// next question of a multi-step flow -- which swaps content into the
  /// SAME route -- starts clean instead of inheriting the last answer.
  int questionSerial = 0;
  PromptFrame? pendingPrompt;
  ChoiceFrame? pendingChoice;
  RecordFrame? pendingRecord;
  FormFrame? pendingForm;
  PinMapFrame? pinMap;
  RuleBuilderFrame? ruleBuilder;
  SceneBuilderFrame? sceneBuilder;

  /// The hub's Tailscale name and address, as it said on connect
  /// (empty: not on Tailscale). Settings > Remote access shows it.
  String hubTailnet = '';
  String hubTailnetIp = '';

  /// Which Tailscale account the hub is signed in with ("" when unknown).
  String hubTailnetAccount = '';

  /// Remote access as the hub says it is: on | paused | off | expired |
  /// needs_approval | not_set_up | absent | daemon_down ("" from an older
  /// hub: read from hubTailnet).
  String hubTailnetState = '';

  /// Opens the Remote access sheet where the person is (set by main.dart).
  VoidCallback? remoteAccessOpener;

  /// A Tailscale sign-in is open in the browser; the hub has not answered.
  bool tailscaleSigningIn = false;
  String tailscaleSignInUrl = '';
  DateTime? _tsSignInAt;
  BuildContext? _tsWaitCtx;

  /// "Ask the AI" was tapped and the hub is writing: the scene screen
  /// waits, as registration does, until the screen comes back.
  bool sceneAiBusy = false;

  /// The hub's outstanding `file_request` (firmware for the hub). It has no
  /// screen of its own: the answer is the system picker.
  FileRequestFrame? pendingFileRequest;

  /// Which file_request the running picker belongs to. A picker that
  /// returns after its question ended (a cancel, a dropped connection, a
  /// newer question) answers nothing.
  int _fileRequestSerial = 0;


  /// Structured automation rows from the hub's `automation_list` frame.
  final List<Map<String, dynamic>> automationRecords = [];
  final List<Map<String, dynamic>> automationJournal = [];

  /// A long wait the hub has told us about, or null.
  ///
  /// The hub does not draw anything -- it says "I am waiting, here is why".
  /// What that looks like is this app's business, exactly as with prompts
  /// and choices. Without this the frame arrived and was silently ignored,
  /// which is what an unhandled type does, so a twenty-five second wait
  /// after unplugging a cable read as a hang.
  String? busyText;
  String? busyDetail;

  /// How long the hub expects to wait, when it knows. Null means it does
  /// not, and the bar falls back to an indeterminate one -- which is the
  /// honest rendering of "no idea how long".
  double? busySeconds;
  String? challengePhrase;
  String? keepsafeMessage;

  // Enrollment / menu tracking
  bool isStarting = false;
  Timer? startTimer;

  // Voice transcript interception
  String? lastTranscript;
  bool interceptTranscript = false;

  /// What the voice matcher heard on the selection screen, and what it made
  /// of it. Set by VoiceFlow (the ONLY matcher now) and displayed by
  /// ChoiceScreen — whose own fuzzy matcher never ran, because choice
  /// transcripts never travel through lastTranscript.
  String? choiceHeard;

  /// The dock's X while options are listed MUTES THE EARS, it does not
  /// cancel the selection -- the list stays on screen, fingers still work,
  /// and a tap on the dock re-arms listening. True only between that X
  /// and the next tap/next question; every new choice starts unmuted.
  bool dockMuted = false;

  /// The rows ticked on the listed multi-select, as ChoiceScreen reports
  /// them (noteChoiceTicks). They ride beside SPOKEN words so "and the
  /// porch" adds to what is already ticked instead of replacing it.
  final Set<String> _choiceTicks = {};

  void noteChoiceTicks(Iterable<String> ticks) {
    _choiceTicks
      ..clear()
      ..addAll(ticks);
  }

  /// True from a SPOKEN answer to a server list until the hub's next frame.
  /// The question is answered (pendingChoice is null, exactly as after a
  /// tap) and ChoiceScreen holds its working view in place, so a narrowed
  /// "did you mean…?" replaces the list where it stands.
  bool choiceAwaitingHub = false;

  /// `choice` frames answered WITHOUT being shown (the rule a tap already
  /// chose, an armed cancel), by identity: VoiceFlow is handed the very
  /// same map and asks, once, before it reads the list aloud and opens a
  /// mic for a question that no longer exists. It used to do both.
  final Set<Map<String, dynamic>> _silentChoices = Set.identity();

  void _markSilentChoice(Map<String, dynamic> frame) {
    if (_silentChoices.length > 8) _silentChoices.clear(); // never asked
    _silentChoices.add(frame);
  }

  bool _takeSilentChoice(Map<String, dynamic> frame) =>
      _silentChoices.remove(frame);

  /// A SPOKEN answer to the listed server question: the words, for the
  /// hub to match (nlu/choice_match), with the rows already ticked on a
  /// multi-select beside them.
  ///
  /// IT ANSWERS THE QUESTION, like a tap. The hub either acts on the words
  /// or asks a NEW question ("did you mean…?"). pendingChoice used to stay
  /// set after the words went, so the list stayed tappable while the hub
  /// was already past it -- a tap then answered whatever the hub asked
  /// next -- and the next prompt was pushed on top of it, leaving the
  /// stale list underneath to come back. It also used to hand the screen
  /// a "submit" first, which on a multi-select SENT THE TICKED ROWS as the
  /// answer and threw the words away.
  ///
  /// Null when there is no question left to answer (a tap got there
  /// first), false when nothing could be sent.
  bool? sendChoiceHeard(String said) {
    final choice = pendingChoice;
    if (choice == null) return null;
    final socket = _socket;
    if (socket == null || connState != ConnState.ready) return false;
    final ticks = choice.multi
        ? [
            for (final o in choice.options)
              if (_choiceTicks.contains(o.value)) o.value
          ]
        : const <String>[];
    if (!socket.send(wire.choiceHeard(said, selected: ticks))) return false;
    pendingChoice = null;
    choiceAwaitingHub = true;
    _choiceTicks.clear();
    dockMuted = false; // the mute belonged to that question, now answered
    _flowLines.clear(); // same fresh-banner rule as sendChoiceResponse
    notifyListeners();
    return true;
  }

  /// Name of the route currently on top, kept fresh by the RouteTracker
  /// observer in main.dart. The voice dock hides itself on routes that
  /// render their own full orb ('/' and '/enroll').
  String topRoute = '/';

  /// The NAMED routes currently on the navigator, oldest first.
  ///
  /// topRoute alone answers "what am I looking at"; it cannot answer "is
  /// Devices already open BEHIND this". That second question is the whole
  /// of the duplicate-screen bug: `/devices` was pushed unconditionally
  /// by every list result, so one tap of Refresh — or any device change,
  /// which re-reads the list — put a second copy on the stack, and Back
  /// showed Devices again. And again ("when i go back in device screen it
  /// repeats", 2026-09-12).
  ///
  /// Maintained by _RouteTracker in main.dart. A HINT, never an authority:
  /// if it ever drifts, the worst openScreen does is push, which is what
  /// the whole app did before it existed.
  final List<String> routeStack = [];

  void noteRoutePushed(String name) {
    if (name.isEmpty) return;
    routeStack.add(name);
  }

  void noteRouteRemoved(String name) {
    if (name.isEmpty) return;
    final i = routeStack.lastIndexOf(name);
    if (i >= 0) routeStack.removeAt(i);
  }

  /// Open a SCREEN (as opposed to pushing a route).
  ///
  /// Three cases, and only the third is a push:
  ///   * already looking at it      -> nothing. The screens rebuild from
  ///     AppState, so a refreshed list appears without any navigation.
  ///   * open, but buried           -> come back to it rather than stack
  ///     a second copy on top.
  ///   * not open                   -> push it.
  ///
  /// The unwind carries `route.isFirst` as a floor so a stale stack can
  /// never empty the navigator — the worst a wrong guess costs is landing
  /// on Home, which is where the old code put you anyway.
  void openScreen(String route) {
    final nav = navigatorKey.currentState;
    if (nav == null) return;
    if (topRoute == route) return;
    if (routeStack.contains(route)) {
      nav.popUntil((r) => r.settings.name == route || r.isFirst);
      return;
    }
    nav.pushNamed(route);
  }

  void setTopRoute(String name) {
    // The dialogs this class names (see _flowRoutes) are modals ON a
    // screen, not screens: the last real route stands, exactly as the
    // route tracker already treats unnamed dialogs.
    if (name.startsWith('/dialog/')) return;
    if (topRoute == name) return;
    topRoute = name;
    // Post-frame: didPush for the initial route fires DURING the first
    // build, and notifying listeners mid-build throws.
    WidgetsBinding.instance.addPostFrameCallback((_) => notifyListeners());
  }

  // ── Theme colors ───────────────────────────────────────────────────

  // Dark mode is TRULY DARK: neutral near-black surfaces (no teal tint —
  // the greenish-black read as murky, not dark). The teal lives in the
  // ACCENT and the ivory ink carries the warmth. The only blue left in the
  // app is the enter-secret overlay (deliberate).
  Color get bg => isLight ? const Color(0xFFFFFFFF) : const Color(0xFF0A0A0B);
  Color get surface => isLight ? const Color(0xFFF5F5F5) : const Color(0xFF151517);
  Color get border => isLight ? const Color(0xFFE0E0E0) : const Color(0xFF27272B);
  Color get ink => isLight ? const Color(0xFF1A1A1A) : const Color(0xFFF7F3E8);
  Color get inkDim => isLight ? const Color(0xFF666666) : const Color(0xFFA9A9A9);
  Color get inkFaint => isLight ? const Color(0xFF999999) : const Color(0xFF6A6A6A);

  /// THE theme rule: interactive accents are DEEP TEAL in light mode and
  /// IVORY in dark mode — every button, outline, focus border, pip, on-state
  /// card border. (The light-teal dark accent read as "weird teal"; dark
  /// mode's identity is ivory.) Loading states are the ONE exception — see
  /// connectingBg below.
  Color get accent => isLight ? const Color(0xFF00695C) : const Color(0xFFFFFFF0);
  Color get accentFg => isLight ? const Color(0xFFFFFFFF) : const Color(0xFF1A1A1A);
  Color get orbColor => blockingProblem != null
      ? const Color(0xFFF85149)
      : (isLight ? const Color(0xFF00695C) : const Color(0xFFFFF8E1));
  // Connect button — DEEP teal in light mode (bg deep, text light), and a
  // LIGHTER deep teal while a connection attempt is in flight. The old light
  // mode had it inverted: pale teal background with deep-teal text.
  Color get connectBg => isLight ? const Color(0xFF00695C) : const Color(0xFFFFF8E1);
  Color get connectFg => isLight ? const Color(0xFFFFFFFF) : const Color(0xFF1A1A1A);
  // LOADING buttons only (connecting, working): LIGHT deep teal in light
  // mode, SOFT light ivory in dark mode — visibly "in progress" next to the
  // solid ivory of settled dark-mode buttons.
  // MUTED, her design 2026-10-11: a greyer teal with black text in light
  // mode, a greyer ivory with white text in dark.
  Color get connectingBg =>
      isLight ? const Color(0xFF9DB5B1) : const Color(0xFF6F6B61);
  Color get connectingFg =>
      isLight ? const Color(0xFF1A1A1A) : const Color(0xFFFFFFFF);
  // Cancel affordances — her rule, corrected (2026-08-27 evening): EVERY
  // cancel wears the DISCONNECT button's identity — its red at rest, its
  // burgundy fill when pressed — never deep teal, never the border grey.
  // Cancelling and disconnecting are the same gesture at different
  // sizes: both walk away from something live, so they look like kin.
  Color get cancelInk => const Color(0xFFF85149);
  Color get cancelPressed => const Color(0xFF800020);

  /// The full disconnect dress for the big Cancel buttons (prompt /
  /// choice / form): red outline and text at rest, burgundy fill with
  /// white text while pressed — exactly what Disconnect does on home.
  ButtonStyle cancelButtonStyle({double radius = 12}) => ButtonStyle(
        backgroundColor: WidgetStateProperty.resolveWith((st) =>
            st.contains(WidgetState.pressed)
                ? cancelPressed
                : Colors.transparent),
        foregroundColor: WidgetStateProperty.resolveWith((st) =>
            st.contains(WidgetState.pressed)
                ? const Color(0xFFFFFFFF)
                : cancelInk),
        side: WidgetStateProperty.resolveWith((st) => BorderSide(
            color:
                st.contains(WidgetState.pressed) ? cancelPressed : cancelInk,
            width: 1.2)),
        padding: WidgetStateProperty.all(
            const EdgeInsets.symmetric(vertical: 14)),
        shape: WidgetStateProperty.all(RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(radius))),
      );

  /// FIX: "connecting" is a connection-state fact, not a status-string match.
  /// The button used to test `status == 'connecting…'`, which broke the
  /// moment VoiceFlow's onStatus overwrote the string mid-probe.
  bool get isConnecting =>
      connState == ConnState.probing || connState == ConnState.authenticating;

  /// Live, authenticated connection — the question/working screens hold
  /// themselves open mid-flow only while this is true, so a dropped socket
  /// can never leave a spinner screen stranded.
  bool get isReady => connState == ConnState.ready;

  // ── Exposed pipeline getters ───────────────────────────────────────

  List<String> get candidates => _socket?.candidates ?? AppConfig.defaultCandidates;
  String? get connectedUrl => _socket?.connectedUrl;

  /// The connection gauge (socket_client.dart, LinkQuality): how fast the
  /// hub answers, and which road Tailscale is using. The home bar draws it.
  LinkQuality get link => _socket?.link ?? LinkQuality.none;

  // ── THE WI-FI RADIO ITSELF ──────────────────────────────────────────
  //
  // The ping says the road is slow once it IS slow; the radio says it is
  // about to be. A Wi-Fi signal can fade a long way and still answer in
  // 20 ms, so walking away from the router showed four bars until the
  // connection was already struggling. Read from Android (MainActivity,
  // channel homevoice/wifi) each time the gauge ticks; null when this
  // phone is not on Wi-Fi (mobile data, or it is the hotspot itself) or
  // the build has no such channel.
  static const MethodChannel _wifiChannel = MethodChannel('homevoice/wifi');

  /// The Wi-Fi signal in dBm (-40 strong ... -90 nearly gone), or null.
  int? wifiRssi;

  /// 4 strong, 3 good, 2 weak, 1 very weak; null when not on Wi-Fi.
  static int? wifiBarsOf(int? rssi) {
    if (rssi == null) return null;
    if (rssi >= -60) return 4;
    if (rssi >= -70) return 3;
    if (rssi >= -80) return 2;
    return 1;
  }

  int? get wifiBars => wifiBarsOf(wifiRssi);

  String get wifiWord => const ['', 'very weak', 'weak', 'good', 'strong'][wifiBars ?? 0];

  /// WHAT THE HOME BAR DRAWS: the worse of the two. A fast answer over a
  /// fading signal shows the fading signal; a strong signal with a slow hub
  /// shows the slow hub. No answer at all is no bars, whatever the radio says.
  int get linkBars {
    final answer = link.bars;
    final radio = wifiBars;
    if (answer == 0 || radio == null) return answer;
    return answer < radio ? answer : radio;
  }

  String get linkWord =>
      const ['no answer', 'poor', 'slow', 'good', 'excellent'][linkBars];

  Future<void> _readWifi() async {
    int? rssi;
    try {
      rssi = await _wifiChannel.invokeMethod<int>('rssi');
    } on MissingPluginException {
      rssi = null; // an APK built before the channel existed
    } on PlatformException {
      rssi = null;
    }
    final changed = wifiBarsOf(rssi) != wifiBars;
    wifiRssi = rssi;
    if (changed) {
      debugPrint('[link] wifi ${rssi == null ? 'not in use' : '$wifiWord ($rssi dBm)'}');
      notifyListeners();
    }
  }

  /// A FRESH COMMAND capture, unconditionally. Do NOT wire this to an orb
  /// or dock: on any screen where the server is waiting on a recording, a
  /// challenge, a prompt or a choice, it sends the capture to the WRONG
  /// destination — an enrollment phrase left the phone as a `command`
  /// frame, the server refused it as "a command is already running", and
  /// enrollment timed out waiting for a `recording` that was never coming.
  /// Use [dockTap], which picks the role from what is actually pending.
  void triggerCapture() => _flow?.triggerCapture();

  // ── What a SCREEN can ask of the voice ─────────────────────────────
  //
  // Screens that answer themselves -- the pin map is the only one today --
  // reach the mic through here rather than holding VoiceFlow. Two calls,
  // both safe before the pipeline exists (nothing happens, the screen still
  // works by touch).

  /// Say it out loud. The screens had sentences and no voice: pin_map's
  /// `_say` composes "GPIO4 is already used by light bulb" and shows a
  /// SnackBar, despite the name.
  void speak(String text) => _flow?.speakAside(text);

  /// Hand the voice the list this screen is drawing. Re-arm after every
  /// change: the vocabulary shrinks as pins fill, and "save" only becomes
  /// sayable once nothing is half placed. `resolve`, when given, reads
  /// each utterance FIRST and returns the action for words the screen
  /// understands exactly (the pin map's pin numbers), or null to let the
  /// hub match them against `options`.
  void armScreenChoice({
    required List<Map<String, dynamic>> options,
    required void Function(String value, String heard) onPick,
    String prompt = '',
    VoidCallback? onCancel,
    String repeatLine = '',
    bool speakPrompt = false,
    VoidCallback? Function(String heard)? resolve,
  }) =>
      _flow?.armLocalChoice(
          prompt: prompt,
          options: options,
          onPick: onPick,
          onCancel: onCancel,
          repeatLine: repeatLine,
          speakPrompt: speakPrompt,
          resolve: resolve);

  /// Take ONE spoken value for a field this screen is drawing.
  ///
  /// The other half of armScreenChoice: that one matches what was said
  /// against a list, this one hands the words over as they were heard.
  /// Between them a form can be filled in by voice -- say the field, then
  /// say what goes in it.
  void armScreenDictation({
    required String prompt,
    required void Function(String text) onText,
    VoidCallback? onCancel,
    bool speakPrompt = true,
  }) =>
      _flow?.armLocalDictation(
          prompt: prompt,
          onText: onText,
          onCancel: onCancel,
          speakPrompt: speakPrompt);

  void disarmScreenChoice() => _flow?.disarmLocalChoice();

  /// WHAT THE DOCK'S TAP MEANS ON THIS SCREEN.
  ///
  /// A screen that can be filled in by voice registers here on the way in
  /// and clears it on the way out; the dock then hands it the tap instead
  /// of firing a house command the hub would refuse. Nothing is armed
  /// until it is -- a form carries text fields, and the rule that came out
  /// of the PIN that submitted itself is "never open the mic without being
  /// asked", not "never open it".
  VoidCallback? screenVoiceRequest;

  /// True when the dock should offer to fill this screen in by voice.
  bool get screenTakesVoice => screenVoiceRequest != null;

  /// Run a remembered utterance past the matcher again, against a
  /// different list and without the mic. The confirm loop's "no" uses it
  /// to reach the next best without making you say it twice.
  /// Run a remembered utterance past the matcher again, against a
  /// different list and without the mic -- the confirm loop's "no" reaches
  /// the next best without making you say it twice.
  ///
  /// ASYNC SINCE 2026-09-11, because the matching is the hub's now. The
  /// old version borrowed VoiceFlow's own scorer, which is the code that
  /// scored "bob" 0.000 against "bulb"; there is no scorer on the phone
  /// any more. Null when nothing is close, or when the hub did not answer.
  Future<String?> matchHeard(
      String heard, List<Map<String, dynamic>> options) async {
    final flow = _flow;
    if (flow == null) return null;
    final ask = await flow.askHubToMatch(heard, options);
    if (ask.values.isNotEmpty) return ask.values.first;
    // Nothing certain -- take the closest it would have OFFERED, since
    // this call is already the second guess at one utterance.
    if (ask.near.isNotEmpty) {
      return (ask.near.first['value'] ?? '').toString();
    }
    return null;
  }

  /// True while the server is waiting on a capture from us (recording,
  /// challenge) — the orb reads this to say "tap to record".
  bool get awaitingCapture =>
      pendingRecord != null || challengePhrase != null;

  // ── Init ───────────────────────────────────────────────────────────

  Future<void> init() async {
    // Neither load may stop the boot: a throw here used to skip _boot()
    // entirely (main() only logs it), leaving isBooting true for ever with
    // no Retry -- one corrupt stored notice was enough.
    try {
      await _loadTheme();
    } catch (e) {
      debugPrint('[init] preferences unreadable: $e');
    }
    // WHICH HUB, before anything that is kept per hub (its addresses, its
    // notices) is read.
    try {
      await _loadHubs();
    } catch (e) {
      debugPrint('[init] paired hubs unreadable: $e');
    }
    // Load BEFORE booting: a pending action from last session must show its
    // red dot immediately, not only after the hub answers.
    try {
      await _loadNotices();
    } catch (e) {
      debugPrint('[init] stored notices unreadable, dropped: $e');
      notices.clear();
      _seenNoticeIds.clear();
    }
    // NOT awaited: main() awaits init() before runApp, and the boot asks
    // for the microphone, unpacks models and dials the hub -- awaiting it
    // here held the first frame back until all of that finished. _boot()
    // catches its own failures into blockingProblem, so nothing is lost by
    // letting it run behind the first frame.
    unawaited(_boot());
  }

  /// Try the boot again after a blocking problem (the home screen's
  /// "Retry" button). Every failure in _boot lands in [blockingProblem]
  /// rather than in an unhandled exception, so there is always a way back.
  Future<void> retryBoot() async {
    blockingProblem = null;
    notifyListeners();
    await _boot();
  }

  /// A boot that catches EVERYTHING. `_boot` used to let a thrown
  /// permission plugin, a storage failure or a model that would not load
  /// escape as an uncaught async error, leaving isBooting true and a
  /// screen that could neither explain itself nor be retried.
  Future<void> _boot() async {
    try {
      await _bootUnguarded();
    } catch (e, st) {
      debugPrint('[boot] failed: $e\n$st');
      blockingProblem = 'The app could not start: $e';
    } finally {
      isBooting = false;
      notifyListeners();
    }
  }

  Future<void> _loadTheme() async {
    final prefs = await SharedPreferences.getInstance();
    // Only an explicit "dark" choice (is_light stored false) leaves light.
    _theme = prefs.getBool('is_light') == false ? _AppTheme.dark : _AppTheme.light;
    ttsMuted = prefs.getBool('tts_muted') == true;
    voiceNavigation = prefs.getBool('voice_nav') != false;
    dockHidden = prefs.getBool('dock_hidden') == true;
    notifyListeners();
  }

  /// The phrase to say, for the engine in use.
  String get wakePhraseShown => wakePhraseText;

  /// What the status says at rest: the assistant's name (her design,
  /// 2026-10-11: "just Nova is okay").
  String get assistantLabel => 'Nova';

  Future<void> setVoiceNavigation(bool value) async {
    if (value == voiceNavigation) return;
    voiceNavigation = value;
    notifyListeners();
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool('voice_nav', value);
  }

  Future<void> toggleMute() async {
    ttsMuted = !ttsMuted;
    _voice?.muted = ttsMuted;
    if (ttsMuted) await _voice?.stop(); // cut off anything mid-sentence
    notifyListeners();
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool('tts_muted', ttsMuted);
  }

  Future<void> saveTheme() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool('is_light', isLight);
  }

  void cycleTheme() {
    _theme = isLight ? _AppTheme.dark : _AppTheme.light;
    saveTheme();
    notifyListeners();
  }

  // ── Boot ───────────────────────────────────────────────────────────

  Future<void> _bootUnguarded() async {
    isBooting = true;
    notifyListeners();

    final mic = await Permission.microphone.request();
    if (!mic.isGranted) {
      blockingProblem = 'Microphone permission is required. Grant it in system settings.';
      isBooting = false;
      notifyListeners();
      return;
    }
    await Permission.notification.request();

    if (Platform.isAndroid &&
        !await FlutterForegroundTask.isIgnoringBatteryOptimizations) {
      await FlutterForegroundTask.requestIgnoreBatteryOptimization();
    }

    // PAIRED, NOT "HAS A SECRET". The old gate asked whether a shared key
    // had been typed in; every phone typed the same one and the hub could
    // not tell them apart. The question now is whether THIS phone has an
    // identity the hub knows and a certificate to pin -- both produced by
    // pairing, neither typed.
    unpaired = !await identity.isPaired();
    if (unpaired && !_pairLater) {
      status = 'pair this phone with your hub to finish setup';
      needPairing = true;
      isBooting = false;
      notifyListeners();
      return;
    }

    final ext = await getExternalStorageDirectory();
    if (ext == null) {
      blockingProblem = 'No external storage available.';
      isBooting = false;
      notifyListeners();
      return;
    }
    final modelRoot = Directory('${ext.path}/models')..createSync(recursive: true);

    status = 'preparing models…';
    notifyListeners();
    await _extractBundledModels(modelRoot);

    final missing = <String>[
      for (final p in [
        '${AppConfig.moonshineDir}/preprocess.onnx',
        AppConfig.vadModel,
        '${AppConfig.kwsDir}/encoder.onnx',
      ])
        if (!File('${modelRoot.path}/$p').existsSync()) p
    ];
    if (missing.isNotEmpty) {
      blockingProblem =
          'Models missing under ${modelRoot.path}: ${missing.join(', ')}.\n'
          'Follow README "Models" to fetch them, then restart.';
      isBooting = false;
      notifyListeners();
      return;
    }

    await _startPipeline(modelRoot.path);
    isBooting = false;
    notifyListeners();
  }

  Future<void> _extractBundledModels(Directory root) async {
    const prefix = 'assets/models/';
    try {
      final manifest = await AssetManifest.loadFromAssetBundle(rootBundle);
      var copied = 0;
      for (final asset in manifest.listAssets()) {
        if (!asset.startsWith(prefix)) continue;
        final rel = asset.substring(prefix.length);
        if (rel.isEmpty || rel.endsWith('.gitkeep') || rel.endsWith('.txt.placeholder')) continue;
        final out = File('${root.path}/$rel');
        if (out.existsSync()) continue;
        out.parent.createSync(recursive: true);
        final data = await rootBundle.load(asset);
        await out.writeAsBytes(
          data.buffer.asUint8List(data.offsetInBytes, data.lengthInBytes),
          flush: true,
        );
        copied++;
      }
      debugPrint('[models] extraction done: $copied file(s) copied');
    } catch (e) {
      debugPrint('[models] bundled extraction FAILED: $e');
    }
  }

  Future<void> _startPipeline(String modelRoot) async {
    // ONE PIPELINE. A re-pair (completePairing -> _boot) and a retry both
    // come through here on an app that already booted, and the old flow,
    // mic and -- still authenticated -- socket used to be left running
    // beside the new ones: the second MicCapture then failed to open.
    await _stopPipeline();
    // _stopPipeline drops the state listener BEFORE closing the socket, so
    // the old session's flow mirror has to be ended here, not there -- a
    // re-pair mid-flow otherwise carried menuBusy and a dead question into
    // the new session.
    _endConnectionFlow();
    final prefs = await SharedPreferences.getInstance();
    // Growable COPY: defaultCandidates is a const list, and the Network
    // editor mutates SocketClient.candidates in place — clear() on a const
    // list throws.
    final candidates = [
      ...(prefs.getStringList(_hubKey('candidates')) ?? AppConfig.defaultCandidates)
    ];
    await _dropLegacyDefaults(prefs, candidates);

    _socket = SocketClient(
      candidates: candidates,
      // The certificate learned at pairing, in place of the asset that
      // used to be baked into the APK.
      getPinnedCertificate: identity.certificate,
      // A CALLBACK, not a key: the private key never enters the transport
      // layer. See PhoneIdentity.signAuth.
      signAuth: identity.signAuth,
    );

    // FIX: Mirror SocketClient's state machine so unexpected drops, auth
    // transitions, and reconnects are reflected immediately in UI guards.
    _linkSub?.cancel();
    var linkShown = LinkQuality.none;
    _linkSub = _socket!.links.listen((q) {
      unawaited(_readWifi()); // the radio, on the gauge's own beat
      // Redraw when the PICTURE changes (bars or road), not on every ping.
      if (q.bars == linkShown.bars && q.path == linkShown.path) {
        linkShown = q;
        return;
      }
      linkShown = q;
      notifyListeners();
    });
    _connSub?.cancel();
    _connSub = _socket!.states.listen((s) {
      final was = connState;
      connState = s;
      // FIX (mic dead until restart, cause #2): a socket that dropped while
      // a command was in flight meant the result that would return the flow
      // to idle NEVER arrived — stuck in `thinking`, wake word ignored.
      // Any connection teardown resets the flow to idle listening.
      if (s == ConnState.closed || s == ConnState.idle) {
        _flow?.abortCapture();
      }
      // Leaving `ready` ends the hub's flow (see _endConnectionFlow), so
      // it ends ours too -- a drop the supervisor is retrying included.
      if (was == ConnState.ready && s != ConnState.ready) {
        _endConnectionFlow();
      }
      // A DROP THAT IS BEING RETRIED IS NOT A DISCONNECTION, and saying
      // "disconnected" for both is what made a network blip look like the
      // app giving up. The supervisor in SocketClient keeps trying; the
      // status line now says so, and says WHY it dropped -- the fault that
      // started this was never identified, and a connection that heals in
      // silence would hide it for good.
      final sock = _socket;
      if (s == ConnState.closed && sock != null && sock.reconnecting) {
        final why = sock.lastDropReason;
        status = sock.attempt <= 1
            ? 'connection lost — reconnecting…'
            : 'reconnecting… (try ${sock.attempt})';
        if (why.isNotEmpty) debugPrint('[ws] $why');
      }
      notifyListeners();
    });

    // FIX: the missing frame wire — without this, every prompt/choice/
    // record_required/challenge/result frame reached VoiceFlow (voice path)
    // but never AppState, so the visual screens never appeared.
    _frameSub?.cancel();
    _frameSub = _socket!.frames.listen((f) => onServerFrame(f.raw));

    _mic = MicCapture();
    final micOk = await _mic!.start();
    if (!micOk) {
      blockingProblem = 'Could not open the microphone.';
      notifyListeners();
      return;
    }
    // Registered ONCE, here, where the mic first exists. From now on the
    // microphone follows the app: released when you leave, reopened when
    // you come back. See _MicLifecycle for why this was missing and what it
    // cost.
    WidgetsBinding.instance.removeObserver(_micLifecycle); // idempotent
    WidgetsBinding.instance.addObserver(_micLifecycle);

    // ONE speech engine for the life of the app: a new one per pipeline
    // build started cold every time, and the old connection was never freed.
    _voice ??= Voice();
    _voice!.muted = ttsMuted;
    // (listener added right after construction below)
    _flow = VoiceFlow(
      socket: _socket!,
      mic: _mic!,
      wake: WakeWordSpotter(
        modelDir: '$modelRoot/${AppConfig.kwsDir}',
        keywordsFile: '$modelRoot/${AppConfig.kwsDir}/keywords.txt',
      ),
      endpointer: Endpointer(modelPath: '$modelRoot/${AppConfig.vadModel}'),
      transcriber: Transcriber(modelDir: '$modelRoot/${AppConfig.moonshineDir}'),
      voice: _voice!,
      // DEBUG BUILDS ONLY. A release app must not leave WAVs of what was
      // said on external storage; see VoiceFlow._saveDebugPair.
      debugDir: kDebugMode ? '$modelRoot/debug' : null,
      onStatus: (s) {
        status = s;
        notifyListeners();
      },
      // FIX: VoiceFlow expects bool Function(String), not void.
      onTranscript: (text) => onTranscript(text),
      // A spoken answer to a server list: the words go to the hub.
      onChoiceSpoken: sendChoiceHeard,
      // Spoken "cancel" on a selection takes the button's exact path.
      onChoiceCancel: sendCancel,
      // "Be quiet" / "speak up", heard in any open window. Routed to the
      // same persistent flag the speaker button sets, so the two controls
      // cannot disagree and the choice survives a restart.
      onMuteSpoken: (muted) {
        if (muted != ttsMuted) unawaited(toggleMute());
      },
      onChoiceHeard: (heard, matched) {
        choiceHeard = matched.isEmpty
            ? '"$heard" — no match'
            : '"$heard" \u2192 ${matched.join(', ')}';
        notifyListeners();
      },
      wakeText: () => wakePhraseText,
      restText: () => assistantLabel,
      // The screen and its labels, attached to every command frame so the
      // hub can tell "open automations" from "open the garage". Nothing
      // when navigation is off, and never the contents of a field.
      uiContext: () => voiceNavigation ? uiContext() : null,
    );

    // FIX: flow state (capturing/thinking/speaking) is now observable, so
    // the orb, the dock and the cancel affordances repaint on every
    // transition instead of only when a status string happened to change.
    _flow!.stateListenable.addListener(_onFlowChanged);
    // Auto-answered questions (the AI-review edit chain) must not be
    // spoken — the person never sees them either.
    _flow!.isAutoAnswered = promptHandledSilently;
    _flow!.isChoiceAnsweredSilently = _takeSilentChoice;
    // A picker inside a menu action is worked through on screen — no hot mic.
    _flow!.isMenuFlow = () => menuBusy;
    // A voice-captured recording is sent by the FLOW, straight onto the
    // socket — nothing here sees it go, so the pending mirrors stayed set. The stale record then outranked the next question in the
    // ceremony room: after the last enrollment phrase the PIN prompt
    // arrived and the room kept showing the phrase (field test 2026-08-27,
    // "no pin screen to write the pin").
    _flow!.onRecordingSent = () {
      pendingRecord = null;
      challengePhrase = null;
      notifyListeners();
    };
    // The same mirror for a prompt answered by voice: the question is
    // answered, so its screen must not keep asking it (and a later cancel
    // must not pick it as the thing to cancel).
    _flow!.onPromptSent = () {
      if (pendingPrompt == null) return;
      pendingPrompt = null;
      _flowLines.clear();
      if (topRoute == '/prompt' && !menuBusy) {
        navigatorKey.currentState?.pop();
      }
      notifyListeners();
    };

    await startForegroundService();
    await _flow!.start();
    running = true;
    notifyListeners();

    // AUTO-CONNECT. Boot used to end here with everything prepared and
    // nobody dialling -- the person had to know the orb wanted a tap.
    // A paired phone's home screen should simply come up connected.
    // (Not a phone with no hub: there is nothing to dial, and nothing to
    // sign with.)
    if (unpaired) {
      status = 'not paired with a hub yet';
      notifyListeners();
      return;
    }
    if (connState == ConnState.idle || connState == ConnState.closed) {
      unawaited(toggleConnection());
    }
  }

  /// WHERE THE PIPELINE IS: listening, capturing, thinking or speaking.
  ///
  /// The truth lives on VoiceFlow, which owns the microphone and the
  /// turn; this is the one window onto it, so no screen has to reach
  /// past AppState into the pipeline to draw a dot. Every change already
  /// arrives here — _onFlowChanged is wired to the flow's own notifier
  /// and calls notifyListeners — so a widget listening to AppState
  /// redraws without listening to anything else.
  ///
  /// BEFORE THE PIPELINE EXISTS there is no flow object yet (the app is
  /// starting, or has been disposed). That is idle: nothing is captured
  /// and nothing is spoken. Reading it as anything else would have the
  /// dock claim a live microphone before there is one.
  FlowState get flowState => _flow?.state ?? FlowState.idleListening;

  /// THE WINDOW IS OPEN AND NOTHING IS COMING IN. A capture that hears
  /// nothing looks exactly like a quiet room, which is how a microphone
  /// that was never handed over stayed invisible (2026-09-11). The dock
  /// says so instead of sitting on "listening…".
  bool get micStarved => _flow?.micStarved ?? false;

  void _onFlowChanged() {
    // AUTO-ARM FOR CHOICES. Whenever the flow comes to rest while a
    // selection is on screen, listening resumes on its own -- saying an
    // option must not require finding the mic first. Menu flows included:
    // the old "hands on the phone, no hot mic" rule made the dock look
    // broken exactly when a list of options was begging to be spoken at.
    // The X on the dock is the opt-out, and it mutes ONLY the ears.
    _armChoiceEars();
    notifyListeners();
  }

  /// Open the capture window for the listed choice, if everything is at
  /// rest and the person has not muted the dock. Safe to call eagerly:
  /// reopenFor only acts from true idle, so a window already open, TTS
  /// mid-sentence, or a command in flight is never stomped. Re-armed on
  /// every return to idle, so a silent window that closes quietly starts
  /// listening again -- the ears stay on for as long as the options do.
  void _armChoiceEars() {
    if (dockMuted || pendingChoice == null) return;
    if (flowState != FlowState.idleListening) return;
    if (connState != ConnState.ready) return;
    _flow?.reopenFor('choice');
  }

  /// The dock's tap. One rule: re-open the capture window for whatever the
  /// server is currently waiting on; with nothing outstanding, behave like a
  /// wake-word hit (a fresh command capture).
  void dockTap() {
    // WORKING? THEN THE TAP MEANS "STOP" (her design, 2026-10-09: no Cancel
    // button while the hub thinks -- a tap on the thinking orb, or the
    // dock, cancels; so does a spoken "cancel" or anything meaning it).
    // The same act the X was: the hub stops before it touches a device.
    if (flowState == FlowState.thinking) {
      sendCancel();
      return;
    }
    // Any deliberate tap on the dock means "I want the ears" -- it undoes
    // the X, which only ever meant "stop listening for now".
    dockMuted = false;
    // TALKING? THEN THE TAP MEANS "STOP, I WANT TO SPEAK" (2026-09-11).
    // Barge-in, and the same act as the X: you cannot talk to it while it
    // is talking at you, so the one gesture that means "listen to me" has
    // to end the sentence first. The flow then walks into the window it
    // was going to open anyway -- for a list, that is the mic, at once.
    // Reached from the dock AND from the home orb, which taps through
    // here too.
    if (flowState == FlowState.speaking) {
      _flow?.hush();
      return;
    }
    // Already recording? A second tap ends it and sends — the "tap again
    // to stop" half of tap-to-record.
    if (flowState == FlowState.capturing) {
      _flow?.finishNow();
      return;
    }
    if (challengePhrase != null) {
      _flow?.reopenFor('challenge');
    } else if (pendingRecord != null) {
      _flow?.reopenFor('record');
    } else if (pendingChoice != null) {
      _flow?.reopenFor('choice');
    } else if (pendingPrompt != null) {
      _flow?.reopenFor('prompt');
    } else if (_flow?.hasLocalChoice == true) {
      // A SCREEN THAT ANSWERS ITSELF owns the mic while it is up -- the
      // pin map, and anything else that arms a list of its own. Its
      // window closes on silence like any other, and the tap that
      // re-opens it must re-open THAT, not start a house command on top
      // of a flow the hub is still inside (audit, 2026-09-11).
      _flow?.reopenFor('choice');
    } else if (screenVoiceRequest != null) {
      // A SCREEN THAT CAN BE FILLED IN BY VOICE gets the tap: it arms its
      // own field list and starts listening. This is the deliberate act
      // the dictation window waits for -- nothing on a form opens a mic
      // by itself.
      screenVoiceRequest!();
    } else if (pendingForm != null ||
        pendingRegistration != null ||
        ruleBuilder != null ||
        sceneBuilder != null) {
      // A FORM IS READ, NOT ANSWERED BY VOICE -- yet. These three screens
      // were drawn in silence AND left the dock firing a command frame
      // into a hub that was waiting for a form_response and refused it as
      // busy: the dock looked live and could do nothing. A form carries
      // text fields, and a hot mic behind a text field is what submitted
      // a PIN eight seconds in, so the tap does the useful, safe thing --
      // reads the screen out again, fields and all.
      unawaited(_flow?.sayScreenAgain() ?? Future<void>.value());
    } else {
      _flow?.triggerCapture();
    }
  }

  /// The dock's X while options are listed: EARS OFF, FLOW UNTOUCHED.
  /// The selection stays on screen and every button keeps working; only
  /// the listening stops, until the dock is tapped or the next question
  /// arrives. Cancelling the selection itself is the screen's own Cancel
  /// (or the dock's X when no choice is up), exactly as before.
  void muteDock() {
    // WHILE IT IS TALKING, THE X MEANS "STOP TALKING" (2026-09-11: "why is
    // the dock not having an x to stop speaking"). One button, one idea --
    // "stop the thing you are doing at me" -- and what that is depends on
    // which of the two the pill is currently doing. It does NOT cancel: a
    // list you have stopped listening to is a list you are ready to answer,
    // so the flow carries straight on into its capture window. Cancelling
    // is still the screen's own Cancel.
    if (flowState == FlowState.speaking) {
      _flow?.hush();
      return; // the flow's own transition repaints the dock
    }
    dockMuted = true;
    _flow?.abortCapture();
    notifyListeners();
  }

  // ── Transcript entry-point ─────────────────────────────────────────

  /// Returns true ONLY when the transcript was intercepted (e.g. the choice
  /// screen is matching by voice). Otherwise false, so VoiceFlow sends the
  /// command frame itself.
  ///
  /// FIX (logic break): this used to consume every transcript and resend it
  /// as {'type':'command','text':...} — WITHOUT the audio clip. The server
  /// verifies the speaker against the very audio that was transcribed, so
  /// stripping it silently downgraded every voice command to an unverified
  /// text command (forcing a challenge, or failing verification outright).
  /// VoiceFlow's own send includes audioB64; let it do its job.
  bool onTranscript(String text) {
    final trimmed = text.trim();
    if (trimmed.isEmpty) return false;

    if (interceptTranscript) {
      lastTranscript = trimmed;
      notifyListeners();
      return true;
    }
    return false;
  }

  void clearTranscript() {
    lastTranscript = null;
    notifyListeners();
  }

  // ── Public frame entry-point ───────────────────────────────────────

  void onServerFrame(Map<String, dynamic> msg) => _handleFrame(msg);

  /// Frames that mean the hub has moved on from a spoken choice answer
  /// (a new question, a screen, or the flow's end), so the choice screen's
  /// "working" spinner comes down whatever the next step turned out to be.
  static const _movesPastChoice = {
    'view', 'prompt', 'choice', 'record_required', 'challenge_required',
    'form', 'pin_map', 'registration', 'automation_builder',
    'scene_builder', 'result',
    'error', 'open', 'file_request',
  };

  void _handleFrame(Map<String, dynamic> map) {
    final type = map['type'] as String?;
    if (choiceAwaitingHub && _movesPastChoice.contains(type)) {
      choiceAwaitingHub = false;
    }
    switch (type) {
      case 'view':
        _handleView(map['name'] as String? ?? 'command');
        break;
      case 'prompt':
        _handlePrompt(map);
        break;
      case 'choice':
        _handleChoice(map);
        break;
      case 'record_required':
        _handleRecordRequired(map);
        break;
      case 'challenge_required':
        _handleChallenge(map);
        break;
      case 'still_there':
        _handleStillThere(map);
        break;
      case 'left_house':
        unawaited(_leftHouse(map['cleared'] == true));
        break;
      case 'aside_ack':
        // What was said over the running command, as the hub took it.
        // A cancel comes back as cancel_ack and its cancelled result;
        // "ignored" (not for the hub, or not this phone's person's voice)
        // changes nothing on screen.
        final kind = map['kind'];
        if (kind == 'add') {
          status = 'adding that too…';
          notifyListeners();
        } else if (kind == 'correct') {
          status = 'changing it to that…';
          notifyListeners();
        }
        break;
      case 'cancel_ack':
        // The hub heard Cancel. Nothing was running: back to ready now.
        // Something was: its own cancelled result follows in a moment.
        if (map['running'] != true) {
          _flowCancelNext = false;
          if (status == 'cancelling…') status = 'cancelled';
          notifyListeners();
        }
        break;
      case 'device_list':
        settleStarting(); // the hub answered; the 15 s "no response" is off
        final raw = map['devices'];
        deviceRecords
          ..clear()
          ..addAll([
            for (final d in (raw is List ? raw : const []))
              if (d is Map<String, dynamic>) d
          ]);
        // The boards ride along in the same frame. An older hub sends no
        // `boards` key at all, which clears the list rather than throwing —
        // the screen just shows no BOARDS section, as it did before.
        boardRecords
          ..clear()
          ..addAll([
            for (final b in (map['boards'] as List? ?? const []))
              if (b is Map<String, dynamic>) b
          ]);
        // The readings a display line can quote ({label, token}).
        displayReadings
          ..clear()
          ..addAll([
            for (final r in (map['display_readings'] as List? ?? const []))
              if (r is Map<String, dynamic>) r
          ]);
        final meter = map['main_meter'];
        mainMeter = meter is Map<String, dynamic> ? meter : null;
        // LAYOUTS RIDE IN THE SAME FRAME (one list, one truth): every
        // board's wiring, every draft, every saved plan. An older hub sends
        // no `layouts` key, which leaves the list as it was.
        if (map.containsKey('layouts')) {
          layoutRecords
            ..clear()
            ..addAll([
              for (final l in (map['layouts'] as List? ?? const []))
                if (l is Map<String, dynamic>) l
            ]);
        }
        break;
      case 'scene_list':
        // Replaced wholesale, like readings and devices: a scene removed
        // since the last visit must DISAPPEAR, and merging would keep
        // drawing one that is gone.
        sceneRecords
          ..clear()
          ..addAll([
            for (final s in (map['scenes'] as List? ?? const []))
              if (s is Map<String, dynamic>) s
          ]);
        break;
      case 'layout_list':
        // Replaced wholesale, like scenes: a layout deleted since the last
        // visit must DISAPPEAR.
        layoutRecords
          ..clear()
          ..addAll([
            for (final l in (map['layouts'] as List? ?? const []))
              if (l is Map<String, dynamic>) l
          ]);
        break;
      case 'profile':
        // Replaced wholesale, like every other list frame: a person removed
        // since the last visit must DISAPPEAR, and a merge would keep
        // drawing them.
        profileLoading = false;
        profileMe = (map['me'] as String? ?? '').trim();
        profileYou = map['you'] is Map<String, dynamic>
            ? map['you'] as Map<String, dynamic>
            : null;
        profilePhone = map['phone'] is Map<String, dynamic>
            ? map['phone'] as Map<String, dynamic>
            : null;
        profilePeople
          ..clear()
          ..addAll([
            for (final p in (map['people'] as List? ?? const []))
              if (p is Map<String, dynamic>) p
          ]);
        profilePhones
          ..clear()
          ..addAll([
            for (final p in (map['phones'] as List? ?? const []))
              if (p is Map<String, dynamic>) p
          ]);
        // WHO THIS PHONE BELONGS TO, from the hub's own answer.
        //
        // This used to be guarded by `boundUser.isEmpty`, which is never
        // true after login — auth_ok fills it — so a RENAME left the drawer
        // nameplate and its avatar showing the old name for the rest of the
        // session while the Profile header showed the new one (audit,
        // 2026-09-12). Two identity displays disagreeing, on the screen
        // whose stated law is that a name moves together or not at all.
        //
        // `me` is the hub's current answer to "who is this connection", so
        // it OUTRANKS what auth_ok said earlier; the guard was only ever
        // needed to avoid blanking it, and an empty `me` still cannot.
        if (profileMe.isNotEmpty) boundUser = profileMe;
        break;
      case 'pair_window':
        _handlePairWindow(map);
        break;
      case 'readings_live':
        // THE HOME TILES, and deliberately NOT the same list as
        // `readings`. That one is built by menu_readings and carries the
        // 24-hour min/max/avg, each of which costs a call to Home
        // Assistant; this one is the board's own cache, pushed on a timer.
        // Sharing a list would let the cheap frame quietly overwrite the
        // expensive screen.
        final live = map['sensors'];
        homeReadings
          ..clear()
          ..addAll([
            for (final d in (live is List ? live : const []))
              if (d is Map<String, dynamic>) d
          ]);
        break;
      case 'readings':
        // Replaced wholesale, never merged. A sensor that has been retired
        // since the last visit must DISAPPEAR from the screen; merging would
        // keep drawing its final value forever, which is the most convincing
        // kind of wrong number -- one that used to be right.
        final raw = map['sensors'];
        readingRecords
          ..clear()
          ..addAll([
            for (final d in (raw is List ? raw : const []))
              if (d is Map<String, dynamic>) d
          ]);
        break;
      case 'form':
        _handleForm(map);
        break;
      case 'registration':
        _handleRegistration(map);
        break;
      case 'pin_map':
        _handlePinMap(map);
        break;
      case 'automation_builder':
        _handleRuleBuilder(map);
        break;
      case 'scene_builder':
        _handleSceneBuilder(map);
        break;
      case 'file_request':
        _handleFileRequest(map);
        break;
      case 'automation_list':
        automationRecords
          ..clear()
          ..addAll([
            for (final a in (map['automations'] as List? ?? const []))
              if (a is Map<String, dynamic>) a
          ]);
        automationJournal
          ..clear()
          ..addAll([
            for (final j in (map['journal'] as List? ?? const []))
              if (j is Map<String, dynamic>) j
          ]);
        notifyListeners();
        break;
      case 'busy':
        settleStarting();
        busyText = map['done'] == true ? null : map['text'] as String?;
        busyDetail = map['done'] == true ? null : map['detail'] as String?;
        busySeconds = map['done'] == true
            ? null
            : (map['seconds'] as num?)?.toDouble();
        // A JOB GETS ITS OWN ROOM (field test 2026-08-27: "this shouldnt
        // be in the home screen"). The hub saying "this will take a while"
        // is exactly the moment to stop borrowing the orb's screen for a
        // progress bar and pid chatter.
        //
        // THE GUARD ASKS "IS A QUESTION LIVE?", NOT "AM I ON HOME?".
        //
        // It used to be `topRoute == '/'`, which meant the room opened only
        // from home — and flashing is started from inside Add device, where
        // you are standing on /prompt or /choice. So the push never fired:
        // you watched an already-answered question while esptool ran blind,
        // until the flow ended and popped the route (her report,
        // 2026-08-29: "when flashing the esp it doesnt go to the working
        // screen immediately").
        //
        // The original guard was protecting something real — a question on
        // screen must not be buried — but it was testing the wrong thing.
        // What must not be interrupted is an OUTSTANDING QUESTION, and a
        // job never runs while one is open: the question that started it
        // was answered a moment before. So ask that instead.
        //
        // The room is still popped with the other flow routes when the
        // ribbon ends, so nothing here has to guess when the work finished.
        // The registration screen counts too: its "Fill in with AI"
        // keeps the form open while the hub says busy, and the form has
        // its own spinner. Pushing /working over it stacked a second form
        // on top when the filled frame came back.
        final questionLive = _pendingKind != null;
        // NOT ALREADY ANYWHERE ON THE STACK, not merely "not on top"
        // (audit, 2026-09-12). Inside Add device the hub can go busy,
        // ask a question, and go busy again -- and while menuBusy holds
        // the answered /prompt open, topRoute reads '/prompt', so the
        // old guard let a SECOND /working in behind it.
        if (busyText != null &&
            !questionLive &&
            !routeStack.contains('/working')) {
          navigatorKey.currentState?.pushNamed('/working');
        }
        notifyListeners();
        break;
      case 'notice':
        _handleNotice(map);
        break;
      case 'notice_gone':
        // The hub says a notice's CONDITION has cleared -- the one thing
        // that removes a message. Viewing never does.
        dismissNotice(map['id'] as String? ?? '');
        break;
      case 'notice_ids':
        // The hub's full current list after a recheck. Anything held here
        // that is no longer on it was resolved some other way (another
        // phone, the CLI) -- prune it, so resolved notices cannot linger
        // as stale nags. Sticky-until-done, in both directions.
        _pruneNotices((map['ids'] as List?)?.cast<String>() ?? const []);
        break;
      case 'toast':
        _showToast(map['text'] as String? ?? '');
        break;
      case 'welcome':
        _greet(map['name'] as String? ?? '');
        break;
      case 'open':
        _handleOpen(map);
        break;
      case 'ui':
        _handleUi(map);
        break;
      case 'secret':
        _showSecret(map);
        break;
      case 'output':
        _handleOutput(map);
        break;
      case 'result':
        _handleResult(map);
        break;
      case 'error':
        _handleError(map);
        break;
      case 'match_result':
        // Consumed by VoiceFlow.askHubToMatch on its own subscription;
        // nothing for the screens here. Named so it is not logged as
        // unhandled. (auth_challenge never arrives here at all:
        // SocketClient answers it and does not forward it.)
        break;
      case 'auth_ok':
        connState = ConnState.ready;
        status = 'connected';
        // WHO THIS PHONE BELONGS TO, straight from the binding. Empty on a
        // hub too old to send it, and empty on a handset that has paired
        // but whose holder has not yet enrolled -- in both cases the
        // drawer simply shows no nameplate rather than a guess.
        boundUser = (map['user_id'] as String? ?? '').trim();
        // THE HUB'S NAME AWAY FROM HOME, learned on every connect.
        //
        // This used to arrive only in the `tailscale_joined` result, so
        // the ONE phone that spent the key knew the address and no other
        // phone ever did — and neither did that phone after a reinstall.
        // Two AP addresses in the list and no way in from outside.
        //
        // APPENDED, NEVER REORDERED, and never removed: the addresses
        // that already work keep their place at the front of the dial
        // order, so a name that cannot resolve costs nothing until
        // everything else has already been tried. That is the guard the
        // first attempt at this lacked.
        _learnTailnet((map['tailnet'] as String? ?? '').trim());
        // AND THE HUB'S TAILNET ADDRESS, learned the same way. The name
        // alone was not enough: on this phone the .ts.net name fails
        // through Dart's resolver and Android's root store where the IP,
        // pinned against local.crt, simply works -- and until now the IP
        // had to be typed into Server addresses by hand, once per hub
        // (the Pi and the Windows bench have different ones). Filed ahead
        // of the name and behind the local roads; see _learnTailnetIp.
        _learnTailnetIp((map['tailnet_ip'] as String? ?? '').trim());
        hubTailnet = (map['tailnet'] as String? ?? '').trim();
        hubTailnetIp = (map['tailnet_ip'] as String? ?? '').trim();
        hubTailnetAccount = (map['tailnet_account'] as String? ?? '').trim();
        hubTailnetState =
            _seenOnTailnet((map['tailnet_state'] as String? ?? '').trim());
        // Remote access problems: the hub's one notice says them (the
        // phone's own "no Tailscale" guess is gone).
        // THE ADDRESS THAT ANSWERED GOES FIRST -- for IP addresses only.
        // A hub that moved to a house router is found first through the
        // tailnet or a fresh pairing; after that, its LAN address should
        // be the first thing dialled at home rather than two dead hotspot
        // entries. Names (the tailnet) are never promoted: that is the
        // _learnTailnet guard, and it stays.
        final onUrl = connectedUrl;
        if (onUrl != null) {
          final h = Uri.tryParse(onUrl)?.host ?? '';
          if (InternetAddress.tryParse(h) != null) rememberAddress(onUrl);
        }
        // What is still outstanding is a property of the HUB, so ask it the
        // moment there is a hub to ask. Ungated and read-only.
        WidgetsBinding.instance
            .addPostFrameCallback((_) => requestNotices());
        _keepNoticesCurrent();
        notifyListeners();
        break;
      case 'tailscale_joined':
        // Remote access came up. Add the tailnet name to the candidate
        // list right away, below the AP entries -- the whole point of the
        // join is that nothing else should need typing.
        final name = map['tailnet_name'] as String? ?? '';
        // The hub's tailnet ADDRESS beside its name (2026-09-09), so that
        // Remote access finishing IS the list being updated. Empty from a
        // hub that predates the field.
        final ip = (map['tailnet_ip'] as String? ?? '').trim();
        if (name.isNotEmpty) hubTailnet = name;
        if (ip.isNotEmpty) hubTailnetIp = ip;
        final account = (map['account'] as String? ?? '').trim();
        if (account.isNotEmpty) hubTailnetAccount = account;
        hubTailnetState = 'on';
        // Only the hub's success ends the sign-in.
        _closeTailscaleWaiting();
        // WHICH KIND of success: 'joined' (the hub had no tailnet),
        // 'same_tailnet' (the key belonged here, nothing moved) or
        // 'switched' (a different tailnet, so the hub MOVED). Defaulted,
        // so a hub that predates the field still reads as a plain join.
        final kind = map['reason'] as String? ?? 'joined';
        final was = map['previous_name'] as String? ?? '';
        final wasIp = (map['previous_ip'] as String? ?? '').trim();
        // `text` may be EMPTY (the hub defaults it to ""), which `??` would
        // take as the answer and leave a blank status line.
        final sent = (map['text'] as String? ?? '').trim();
        final joinedText = sent.isNotEmpty
            ? sent
            : (name.isEmpty ? 'joined' : 'joined as $name');
        status = joinedText;
        // A POPUP, NOT A TOAST (her call, 2026-08-27). Remote access
        // coming up is a real outcome and always deserved the tick -- but
        // this particular sentence names a machine and then says
        // something consequential about it ("the key was NOT spent"), and
        // a toast that slides away is the wrong shape for something you
        // may need to act on. The popup holds it, sized to its length.
        final headline = kind == 'switched'
            ? 'The hub moved tailnet'
            : kind == 'same_tailnet'
                ? 'Nothing moved'
                : 'Remote access is on';
        // AND THE ONE THING THE PHONE MUST DO (2026-11-05): away from
        // home it reaches the hub only through its own Tailscale app.
        unawaited(_tailscaleDone(headline, kind == 'same_tailnet'
            ? joinedText
            : ''));
        // The sticky "set up remote access" notice has served its purpose.
        dismissNotice('tailnet:not_joined');
        if (_socket != null) {
          // A SWITCH KILLS THE OLD ADDRESS. The previous .ts.net name
          // belongs to a tailnet this hub is no longer on, so leaving it
          // in the candidate list means every future reconnect spends its
          // timeout dialling a name that cannot resolve before finding
          // the one that works. Dropped here, where the app is being told
          // the move happened.
          var touched = false;
          if (kind == 'switched' && was.isNotEmpty) {
            touched = _socket!.candidates.remove('wss://$was:8765') || touched;
          }
          // A move hands the hub a new 100.x address too; the old one is
          // as dead as the old name, and would cost a full tailnet budget
          // on every sweep. Never dropped on 'same_tailnet' or 'joined'.
          if (kind == 'switched' && wasIp.isNotEmpty && wasIp != ip) {
            touched =
                _socket!.candidates.remove('wss://$wasIp:8765') || touched;
          }
          if (name.isNotEmpty) {
            final url = 'wss://$name:8765';
            if (!_socket!.candidates.contains(url)) {
              _socket!.candidates.add(url);
              touched = true;
            }
          }
          if (touched) {
            SharedPreferences.getInstance().then(
                (p) => p.setStringList(_hubKey('candidates'), _socket!.candidates));
          }
          // The address, ahead of the name just added (it persists itself).
          _learnTailnetIp(ip);
        }
        if (_tailscaleEndingIsOurs(null)) _endTailscaleFlow();
        notifyListeners();
        break;
      case 'tailnet_state':
        // Remote access changed on the hub (Tailscale up after the hub,
        // paused, back). Its notice comes or goes on its own frame.
        final was = hubTailnetState;
        hubTailnetState =
            _seenOnTailnet((map['state'] as String? ?? '').trim());
        if (hubTailnetState == 'on') {
          final n = (map['tailnet_name'] as String? ?? '').trim();
          final i = (map['tailnet_ip'] as String? ?? '').trim();
          if (n.isNotEmpty) hubTailnet = n;
          if (i.isNotEmpty) hubTailnetIp = i;
          final a = (map['account'] as String? ?? '').trim();
          if (a.isNotEmpty) hubTailnetAccount = a;
          _learnTailnet(n);
          _learnTailnetIp(i);
          dismissNotice('tailnet:not_joined');
          if (was.isNotEmpty && was != 'on') {
            WidgetsBinding.instance.addPostFrameCallback(
                (_) => floatPill('Remote access is back'));
          }
        }
        notifyListeners();
        break;
      case 'tailscale_left':
        // Signed out: the Tailscale addresses are dead; the rest stays.
        final leftName = (map['previous_name'] as String? ?? '').trim();
        final leftIp = (map['previous_ip'] as String? ?? '').trim();
        if (_socket != null) {
          var gone = false;
          for (final h in [leftName, leftIp, hubTailnet, hubTailnetIp]) {
            if (h.isEmpty) continue;
            gone = _socket!.candidates.remove('wss://$h:8765') || gone;
          }
          if (gone) {
            SharedPreferences.getInstance().then((p) =>
                p.setStringList(_hubKey('candidates'), _socket!.candidates));
          }
        }
        hubTailnet = '';
        hubTailnetIp = '';
        hubTailnetAccount = '';
        hubTailnetState = 'not_set_up';
        final leftText = (map['text'] as String? ?? '').trim();
        status = leftText.isEmpty ? 'signed out of Tailscale' : leftText;
        WidgetsBinding.instance.addPostFrameCallback((_) => _showResultPopup(
            ok: true, title: 'Signed out of Tailscale', message: leftText));
        if (_tailscaleEndingIsOurs(null)) _endTailscaleFlow();
        notifyListeners();
        break;
      case 'tailscale_failed':
        _closeTailscaleWaiting();
        final sentWhy = (map['text'] as String? ?? '').trim();
        final failedText = sentWhy.isNotEmpty
            ? sentWhy
            : (map['reason'] as String? ?? 'failed');
        // THE SAME POPUP AS THE SUCCESS. The dialog that sent this has
        // already closed, so a refusal left only in the status line is an
        // answer nobody sees -- and these refusals are the ones that name
        // the fix ("no internet", "that does not look like a key", "the
        // key was already used"). Status keeps it too, for afterwards.
        status = failedText;
        WidgetsBinding.instance.addPostFrameCallback(
            (_) => _showResultPopup(ok: false, message: failedText));
        // See _tailscaleEndingIsOurs: a refusal before the join started
        // tears nothing down, and a late ending tears down only when no
        // other flow has the screen.
        if (_tailscaleEndingIsOurs(map['reason'] as String?)) {
          _endTailscaleFlow();
        }
        notifyListeners();
        break;
      case 'auth_failed':
        // A REVOCATION ARRIVES MID-SESSION ("revoked"), while `ready`. This
        // frame is delivered before the socket's own `idle`, so writing
        // connState here first hid the ready->idle transition from the
        // state listener and _endConnectionFlow never ran: the dead
        // question, menuBusy and the Working spinner stayed up.
        if (connState == ConnState.ready) _endConnectionFlow();
        connState = ConnState.idle;
        status = 'Authentication failed: ${map['text'] ?? map['reason'] ?? 'pair this phone again'}';
        // WHICH REFUSALS MEAN "PAIR AGAIN". bad_signature is the hub saying
        // it does not know this phone -- revoked, or its records rebuilt.
        // not_paired/no_identity is this app noticing it has nothing to
        // sign with. revoked is the hub removing this phone MID-SESSION
        // ("pair it again to reconnect"). All are fixed by pairing, and
        // none may be left as a status line that scrolls away -- that dead
        // end used to require reinstalling the app.
        final failReason = map['reason'] as String?;
        const repair = {
          'bad_signature', 'not_paired', 'no_identity', 'revoked',
          'bad_hmac', 'missing_proof',
        };
        if (repair.contains(failReason)) {
          needPairing = true;
        }
        notifyListeners();
        break;
      default:
        debugPrint('[AppState] Unhandled server frame type: $type');
        break;
    }
    notifyListeners();
  }

  /// May a tailscale_joined/_failed (reason `reason`, null for joined)
  /// unwind the flow screens?
  ///
  /// NOT ALWAYS. A keyless sign-in finishes OUTSIDE the hub's flow: the hub
  /// pushes the login page, releases its lock and reports minutes later
  /// (_finish_join_unlocked) -- by when this phone may be deep in another
  /// flow, whose question the unwind would wipe with the hub still waiting
  /// on it.
  bool _tailscaleEndingIsOurs(String? reason) {
    // Refused BEFORE the join started: somebody else's flow (busy,
    // server_busy, setup_in_progress -- enrolment) or no flow at all.
    const refusedAtDoor = {
      'busy', 'server_busy', 'setup_in_progress', 'not_enrolled', 'not_allowed',
    };
    if (refusedAtDoor.contains(reason)) return false;
    // Endings of the join's OWN questions (the PIN, "spend the key?"),
    // sent while the hub still holds the lock -- the question on screen,
    // if any, is the join's.
    const endedInFlow = {
      'cancelled', 'pin_refused', 'unchanged', 'not_bound', 'internal_error',
    };
    if (endedInFlow.contains(reason)) return true;
    // Anything else may be late: only unwind when nothing else is live.
    return !menuBusy && busyText == null && _pendingKind == null;
  }

  void _handleView(String name) {
    // FIX: guard against stacking a second copy of the enroll route when the
    // server re-sends view:enroll (it does, on every "still nobody enrolled").
    final wasEnroll = currentView == 'enroll';
    currentView = name;
    _clearPending();
    // THE LOGIN/SIGN-UP CEREMONY ENDS WITH THIS FRAME, not a result, and
    // `boundUser` only ever arrived on auth_ok -- so a phone that just
    // proved whose it is kept an empty nameplate until the next reconnect.
    // One profile read refreshes it from `me` (see the profile frame).
    if (name == 'command' && boundUser.isEmpty && isReady && !menuBusy) {
      _profileForBinding = true;
      WidgetsBinding.instance.addPostFrameCallback((_) => requestProfile());
    }
    if (name == 'enroll') {
      // A held /prompt or /choice would otherwise stay on top of the
      // enrollment screen for the rest of the flow.
      _releaseHeldQuestion();
      if (!wasEnroll || topRoute != '/enroll') {
        navigatorKey.currentState?.pushNamed('/enroll');
      }
    } else {
      _popFlowRoutes();
    }
  }

  void _handlePrompt(Map<String, dynamic> msg) {
    settleStarting();
    // ONE OUTSTANDING QUESTION AT A TIME is the hub's law, so a new prompt
    // PROVES any earlier question is over — the hub has moved on. Belt to
    // the flow's onRecordingSent hook: whatever path the recording took, a
    // stale record must never outrank this question.
    _dropQuestions();
    final text = msg['text'] as String? ?? '';

    // An ARMED ceremony cancel answers the next question sight unseen —
    // unless that question IS the restarted name question, in which case
    // the cancel already has what it wanted: the beginning.
    // The hub asks "Name to enroll: " (register_person); "User ID to
    // enroll" is the wording of hubs before 2026-09-14.
    if (_ceremonyCancelNext) {
      _ceremonyCancelNext = false;
      if (!text.contains('Name to enroll') &&
          !text.contains('User ID to enroll')) {
        sendJson(_cancelFrame('prompt')!);
        return;
      }
    }
    if (_takeFlowCancel('prompt')) return;

    // The keepsafe acknowledgement is answered by the phrase dialog's
    // button, never by a Question screen — showing one under the dialog is
    // the grey hang. Hold it; ackKeepsafe() replies when the dialog closes.
    if (keepsafeMessage != null &&
        text.toLowerCase().contains('written it down')) {
      _keepsafeAckPending = true;
      return;
    }

    final promptMeta =
        msg['meta'] is Map<String, dynamic> ? msg['meta'] as Map : null;

    questionSerial++;
    pendingPrompt = PromptFrame(
      text: text,
      meta: promptMeta == null
          ? null
          : Map<String, dynamic>.from(promptMeta),
      secret: msg['secret'] == true,
    );
    // FIX (frozen empty "Question" screen): consecutive prompts used to
    // push a NEW /prompt route each, while a reply pops only ONE — so
    // multi-question flows (add_device asks 4-5 things) could leave a
    // stale route underneath with no pending question: the blank screen
    // that wouldn't go away. ONE route; the next question swaps its
    // content in place via notifyListeners.
    // A PIN asked mid-login renders inside AuthScreen -- same pending
    // frame, same sendPromptReply, different room. The ENROLL room is the
    // same kind of place (the owner's ceremony, 2026-08-27): registration
    // is one ritual on one screen -- the orb, the phrase, then the PIN
    // boxes, then the recovery words -- so its questions render IN the
    // room instead of pushing a generic Question screen over it.
    if (topRoute == '/auth' || topRoute == '/enroll') {
      notifyListeners();
      return;
    }
    if (topRoute != '/prompt') {
      // A HELD choice screen (question answered, flow still busy, spinner
      // showing) is REPLACED rather than stacked on — stacking would leave
      // a ghost route underneath for every prompt↔choice handover.
      if (topRoute == '/choice' && pendingChoice == null) {
        navigatorKey.currentState?.pushReplacementNamed('/prompt');
      } else {
        navigatorKey.currentState?.pushNamed('/prompt');
      }
    }
  }

  void _handleChoice(Map<String, dynamic> msg) {
    settleStarting();
    // Same supersession rule as _handlePrompt: a new question ends any
    // outstanding one.
    _dropQuestions();
    // A rule TAPPED in the list already answered "which one?" -- the hub
    // asks anyway (it has no idea a screen exists), so the app answers it
    // silently and the person goes straight to the blocks.
    // ONLY the automation picker ("Update which scene?" shares the prefix),
    // and only while the tapped rule is still on its list: a value the hub
    // did not offer is dropped there, which reads as a cancel.
    final rawOpts = msg['options'] is List ? msg['options'] as List : const [];
    if (_pendingEditId != null &&
        (msg['text'] as String? ?? '').startsWith('Update which automation')) {
      final id = _pendingEditId!;
      _pendingEditId = null;
      if (rawOpts.any((o) => o is Map && '${o['value']}' == id)) {
        _markSilentChoice(msg);
        sendJson({'type': 'choice_response', 'values': <String>[id]});
        return;
      }
    }

    // Same armed-cancel rule as _handlePrompt.
    if (_ceremonyCancelNext) {
      _ceremonyCancelNext = false;
      _markSilentChoice(msg);
      sendJson(_cancelFrame('choice')!);
      return;
    }
    if (_takeFlowCancel('choice')) {
      _markSilentChoice(msg);
      return;
    }
    // Tolerant: one malformed option (a non-map, a numeric value) used to
    // throw a cast error out of the frame handler and leave the hub waiting
    // on a question that never appeared.
    final options = [
      for (final o in rawOpts)
        if (o is Map)
          ChoiceOption(
            value: '${o['value'] ?? ''}',
            label: o['label']?.toString(),
            sub: o['sub']?.toString(),
          )
    ];
    choiceHeard = null;
    // A new question: nothing ticked yet, and the hub has answered the
    // words it was matching (if any) with this very frame.
    _choiceTicks.clear();
    choiceAwaitingHub = false;
    // A NEW question gets fresh ears: the mute belonged to the previous
    // one. Armed post-frame as a belt to the flow-state hook -- if the
    // flow is already at rest when this list appears, there may be no
    // transition to fire it.
    dockMuted = false;
    WidgetsBinding.instance.addPostFrameCallback((_) => _armChoiceEars());
    questionSerial++;
    pendingChoice = ChoiceFrame(
      text: msg['text'] as String? ?? 'Choose:',
      options: options,
      multi: msg['multi'] == true,
      allowAll: msg['allow_all'] == true,
      // Absent on an older hub, which simply keeps the default wording.
      cancelLabel: (msg['cancel_label'] as String? ?? '').trim(),
    );
    // THE AUTH STEPS GET THEIR OWN ROOM. "Who is holding this phone?"
    // and "Who are you?" are the login/sign-up fork right after pairing --
    // the first impression of the whole system -- and the generic list
    // screen renders them like a settings quiz. AuthScreen shows the same
    // choices styled as a welcome, and answers through the exact same
    // sendChoiceResponse, so the server flow cannot tell the difference.
    if (_isAuthStep(pendingChoice!.text)) {
      if (topRoute != '/auth') {
        navigatorKey.currentState?.pushNamed('/auth');
      }
      return;
    }
    // Same no-stacking rule as _handlePrompt.
    if (topRoute != '/choice') {
      // Same held-screen replacement rule as _handlePrompt, mirrored.
      if (topRoute == '/prompt' && pendingPrompt == null) {
        navigatorKey.currentState?.pushReplacementNamed('/choice');
      } else {
        navigatorKey.currentState?.pushNamed('/choice');
      }
    }
  }

  /// The exact questions ws_server's login/sign-up flow asks. Matched by
  /// text because the wire has no other stable marker -- if these strings
  /// change on the hub, change them here in the same commit.
  static bool _isAuthStep(String text) =>
      text == 'Who is holding this phone?' || text == 'Who are you?';

  /// Pop a question route that is only still on screen because the flow is
  /// busy (the mid-flow "Working…" hold). It must not sit on top of the
  /// NEXT thing the server shows.
  ///
  /// This is what broke enrollment: enroll_voice runs as a menu action, so
  /// menuBusy stays true for the whole flow; after "User ID to enroll" was
  /// answered the /prompt route was held instead of popped, and every
  /// record_required afterwards landed UNDER it. The phrase to say and the
  /// listening orb were rendered on /enroll the whole time — behind a
  /// spinner.
  void _releaseHeldQuestion() {
    // A sign-in page is spent the moment the flow moves on. Leaving it set
    // would strand the person on a finished consent screen with the next
    // question stacked invisibly on top of it.
    pendingWebUrl = null;
    final held = (topRoute == '/prompt' && pendingPrompt == null) ||
        (topRoute == '/choice' && pendingChoice == null) ||
        (topRoute == '/auth' &&
            pendingChoice == null && pendingPrompt == null) ||
        (topRoute == '/form' && pendingForm == null) ||
        (topRoute == '/registration' && pendingRegistration == null) ||
        (topRoute == '/pins' && pinMap == null);
    if (held) navigatorKey.currentState?.pop();
  }

  /// Show a credential the hub was ASKED for: masked, with an eye to reveal
  /// and a button to copy.
  ///
  /// Deliberately not the keepsafe dialog. That one is built for the BIP-39
  /// recovery phrase — it is titled "Recovery phrase", it says "I've written
  /// it down", and it shows the value outright, because five words on paper
  /// is the entire point of it. Sending an API key through it produced a
  /// screen that was wrong in its title, its instruction and its visibility
  /// all at once.
  void _showSecret(Map<String, dynamic> msg) {
    final ctx = navigatorKey.currentContext;
    if (ctx == null) return;
    // `secrets: [{label, value}, ...]` is where every password rides now;
    // the bare `value` stays as the single-secret shape an older hub sends.
    final secrets = <SecretEntry>[
      for (final s in (msg['secrets'] as List? ?? const []))
        if (s is Map && (s['value'] as String? ?? '').isNotEmpty)
          SecretEntry(
              label: s['label'] as String? ?? '', value: s['value'] as String),
    ];
    if (secrets.isEmpty) {
      final value = msg['value'] as String? ?? '';
      if (value.isEmpty) return;
      secrets.add(SecretEntry(label: '', value: value));
    }
    final items = [
      for (final i in (msg['items'] as List? ?? const [])) '$i'
    ];
    // One eye per secret; all masked to start.
    final revealed = List<bool>.filled(secrets.length, false);
    showDialog<void>(
      context: ctx,
      builder: (dctx) => StatefulBuilder(
        builder: (dctx, setLocal) => Dialog(
          backgroundColor: surface,
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
          child: Padding(
            padding: const EdgeInsets.all(20),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(msg['title'] as String? ?? 'Secret',
                    style: TextStyle(
                        color: ink, fontSize: 17, fontWeight: FontWeight.w700)),
                if ((msg['note'] as String? ?? '').isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: Text(msg['note'] as String,
                        style: TextStyle(
                            color: inkDim, fontSize: 13, height: 1.45)),
                  ),
                for (var n = 0; n < secrets.length; n++) ...[
                  const SizedBox(height: 16),
                  if (secrets[n].label.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 6),
                      child: Text(secrets[n].label,
                          style: TextStyle(
                              color: inkFaint,
                              fontSize: 11,
                              letterSpacing: 0.6,
                              fontWeight: FontWeight.w700)),
                    ),
                  Container(
                    padding: const EdgeInsets.fromLTRB(14, 12, 6, 12),
                    decoration: BoxDecoration(
                      color: bg,
                      border: Border.all(color: border),
                      borderRadius: BorderRadius.circular(10),
                    ),
                    child: Row(
                      children: [
                        Expanded(
                          child: Text(
                            // A fixed-length mask, so the dots never hint at
                            // how long the real value is.
                            revealed[n] ? secrets[n].value : '•' * 24,
                            style: TextStyle(
                                color: ink,
                                fontSize: revealed[n] ? 14 : 18,
                                height: 1.4,
                                fontFamily: revealed[n] ? 'monospace' : null,
                                fontWeight: FontWeight.w600),
                          ),
                        ),
                        IconButton(
                          tooltip: revealed[n] ? 'Hide' : 'Show',
                          icon: Icon(
                              revealed[n]
                                  ? Icons.visibility_off_outlined
                                  : Icons.visibility_outlined,
                              color: inkDim, size: 20),
                          onPressed: () =>
                              setLocal(() => revealed[n] = !revealed[n]),
                        ),
                        IconButton(
                          tooltip: 'Copy',
                          icon: Icon(Icons.copy, color: inkDim, size: 20),
                          onPressed: () {
                            Clipboard.setData(
                                ClipboardData(text: secrets[n].value));
                            ScaffoldMessenger.of(dctx).showSnackBar(SnackBar(
                              backgroundColor: surface,
                              content: Text('Copied.',
                                  style: TextStyle(color: ink)),
                            ));
                          },
                        ),
                      ],
                    ),
                  ),
                ],
                if (items.isNotEmpty) ...[
                  const SizedBox(height: 16),
                  Text(msg['items_label'] as String? ?? 'Applies to',
                      style: TextStyle(
                          color: inkFaint,
                          fontSize: 11,
                          letterSpacing: 0.6,
                          fontWeight: FontWeight.w700)),
                  const SizedBox(height: 6),
                  for (final i in items)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 2),
                      child: Text('•  $i',
                          style: TextStyle(color: inkDim, fontSize: 13)),
                    ),
                ],
                const SizedBox(height: 18),
                FilledButton(
                  style: FilledButton.styleFrom(
                      backgroundColor: accent, foregroundColor: accentFg),
                  onPressed: () => Navigator.pop(dctx),
                  child: const Text('Done'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  /// Routes the hub is allowed to send the app to.
  ///
  /// A fixed map, not `pushNamed(msg['screen'])`: the server names a
  /// DESTINATION and the app decides what that means, so a malformed or
  /// unknown name is a log line rather than a push onto a route that does
  /// not exist.
  // 'esphome' USED TO BE HERE. It opened a WebView onto the ESPHome
  // dashboard on port 6052 -- a service this project no longer runs, from
  // a build system it no longer uses. Nothing has sent that name since the
  // Tasmota migration; the pin screen at /pins replaced it.
  static const _openableScreens = {
    'devices': '/devices',
    'scenes': '/scenes',
    'notifications': '/notifications',
  };

  /// May a sign-in page be opened over PLAIN http? Only on an address that
  /// cannot leave the house: loopback, RFC 1918 ranges, or a .local name
  /// (a Home Assistant add-on's own page). Anything public must be https.
  static bool _privateHttpHost(String host) {
    final h = host.toLowerCase();
    if (h == 'localhost' || h.endsWith('.local')) return true;
    final ip = InternetAddress.tryParse(h);
    if (ip == null || ip.type != InternetAddressType.IPv4) return false;
    final b = ip.rawAddress;
    if (b[0] == 127 || b[0] == 10) return true;
    if (b[0] == 172 && b[1] >= 16 && b[1] <= 31) return true;
    if (b[0] == 192 && b[1] == 168) return true;
    return false;
  }

  /// The hub asking the app to open a screen.
  ///
  /// Add device uses this for the ESP path. Flashing a board is a
  /// multi-minute job in the ESPHome installer — a SCREEN, not a question —
  /// so the flow hands over and ends rather than trying to drive it through
  /// prompts while holding the shell lock. Before this the flow could only
  /// say "now go and open the ESPHome page yourself", which is exactly the
  /// step nobody takes.
  // ── Voice navigation ───────────────────────────────────────────────
  //
  // The hub decides that "open automations" was said (nlu/app_nav.py);
  // this class decides what that may DO. Two allowlists, one on each side,
  // and the phone's is the narrower: screens by id, back/home, scrolling,
  // and selecting a labelled item that a tap would open anyway. Nothing
  // that types, submits, pairs, unpairs or enters a PIN.

  /// Route -> the screen id the hub knows. Flow routes and everything
  /// else map to null, which means "no navigation from here".
  static const _screenIds = {
    '/': 'home',
    '/devices': 'devices',
    '/scenes': 'scenes',
    '/automations': 'automations',
    '/notifications': 'notifications',
    '/stats': 'stats',
    '/settings': 'settings',
    '/profile': 'profile',
    '/camera': 'camera',
  };

  /// The management ACTIONS voice may start -- "add a device", "new
  /// automation", "learn a remote" -- advertised to the hub with every
  /// command frame (`ui.actions`) so nlu/app_nav.py may answer with one of
  /// them, and the app then does exactly what a tap on that row does,
  /// PIN/voice gate included. This is the same set the Home sheet offers
  /// and the screens' own buttons start; it is the phone's allowlist, and
  /// the hub only ever names an id that is in it. Not here, on purpose:
  /// pair_phone, set_pin, forgot_pin, rename_user, enroll_voice,
  /// enroll_card, edit_board -- the rows that mint or move a credential,
  /// which a mis-hearing must never reach (the same line uiContext draws
  /// for Settings and Profile labels).
  ///
  /// Before this, a spoken "add device" fell through to the house path,
  /// where "add" reads as "a little more" -- and the answer was "No device
  /// matched" (field test 2026-09-05: said on Home and on Devices, nothing
  /// happened).
  static const voiceMenuActions = {
    'add_device', 'update_device', 'remove_device',
    'add_scene', 'update_scene', 'remove_scene',
    'add_automation', 'update_automation', 'remove_automation',
    'learn_ir',
  };

  /// May a menu action start right now? The Home sheet's own test, so a
  /// spoken action is offered exactly when the tapped one is.
  bool get _canStartMenuAction =>
      running &&
      connState == ConnState.ready &&
      pendingPrompt == null &&
      pendingChoice == null &&
      pendingRecord == null &&
      challengePhrase == null &&
      !isStarting &&
      !menuBusy;

  /// The screen and its selectable labels, for the command frame. Labels
  /// only -- a name the person already sees on screen -- never a value, a
  /// reading or a field. Settings and Profile advertise nothing: their
  /// rows unpair, forget and re-key, and a mis-hearing must not reach them.
  /// `actions` rides along whenever a menu action could be tapped.
  Map<String, dynamic>? uiContext() {
    final screen = _screenIds[topRoute];
    if (screen == null) return null;
    final targets = <Map<String, String>>[];
    void add(String id, String label, String kind) {
      if (id.isEmpty || label.isEmpty) return;
      if (targets.length >= 150) return;
      targets.add({'id': id, 'label': label, 'kind': kind});
    }
    // CAMERAS FROM ANYWHERE: "show me the front camera" is asked from
    // whatever screen is up, so they ride in every context. Advertised
    // as kind 'camera', which the hub treats as the one device a loose
    // verb ("show", "open") may select.
    for (final r in deviceRecords) {
      if (isCamera(r)) {
        add((r['id'] ?? '').toString(), (r['name'] ?? '').toString(),
            'camera');
      }
    }
    switch (screen) {
      case 'devices':
        for (final r in deviceRecords) {
          if (isCamera(r)) continue; // already listed, as a camera
          add((r['id'] ?? '').toString(), (r['name'] ?? '').toString(),
              'device');
        }
        // The board cards are tappable too ("select the kitchen board"
        // opens its sheet). Prefixed so a slug can never collide with a
        // device id; the Devices screen strips it.
        for (final b in boardRecords) {
          final slug = (b['slug'] ?? '').toString();
          if (slug.isEmpty) continue;
          add('board:$slug', (b['name'] ?? slug).toString(), 'board');
        }
        break;
      case 'scenes':
        for (final r in sceneRecords) {
          final name = (r['name'] ?? '').toString();
          add(name, name, 'scene');
        }
        break;
      case 'automations':
        for (final r in automationRecords) {
          add((r['id'] ?? '').toString(), (r['name'] ?? '').toString(),
              'automation');
        }
        break;
      case 'notifications':
        // Only cards with a button: selecting one presses it, and a
        // card with none would take the words and do nothing.
        for (final n in notices) {
          if (n.actionable) add(n.id, n.title, 'notice');
        }
        break;
    }
    return {
      'screen': screen,
      'targets': targets,
      // From any screen -- "add a device" is said wherever the person
      // happens to be, as it was in the field test -- but only while the
      // tapped equivalent would be enabled too.
      'actions': _canStartMenuAction ? voiceMenuActions.toList() : <String>[],
    };
  }

  /// What the topmost list screen is asked to do (scroll, select). Set
  /// only while its listeners run (see _postVoiceUi); the screen that is
  /// current acts, the others ignore it.
  final ValueNotifier<VoiceUiCommand?> voiceUi = ValueNotifier(null);

  // ── Cameras ────────────────────────────────────────────────────────
  //
  // A camera lives in Home Assistant; the hub never holds a frame. On
  // "View" (or "show me the front camera") the hub reads the camera's own
  // rotating stream token from HA and sends `open camera` with a stream
  // URL and a snapshot URL; CameraScreen plays the stream in a WebView
  // under the same navigation policy as every `open web`. When the token
  // rotates the stream stops and the screen asks the hub again.

  /// The camera the CameraScreen is showing: url, snapshot, title,
  /// device_id, state. Null when none.
  Map<String, String>? pendingCamera;

  /// Bumped on every `open camera`, so a Reconnect that comes back with
  /// the SAME url (HA rotates tokens only every few minutes) still
  /// reloads the feed -- the screen watches this, not the url.
  int cameraSeq = 0;

  /// Is this device record a camera? Derived from its HA entity, the same
  /// way the Devices screen tells a lock from a light.
  static bool isCamera(Map<String, dynamic> r) =>
      (r['ha_entity_id']?.toString() ?? '').startsWith('camera.');

  /// Ask the hub for a fresh feed of one camera (a menu action; the hub
  /// gates it as a secure device when the camera was registered as one).
  bool viewCamera(String id) {
    if (menuBusy) {
      _showToast('Finish what is on screen first.');
      return false;
    }
    // False when nothing was sent, so the screen does not spin waiting.
    return sendMenuAction('viewcam:$id');
  }

  void closeCamera() {
    pendingCamera = null;
    notifyListeners();
  }

  void _handleUi(Map<String, dynamic> msg) {
    if (!voiceNavigation) return;
    final action = (msg['action'] as String? ?? '').trim();
    // A question on screen owns the screen. The hub already refuses a
    // command while a flow runs (busy), so this is the belt to that brace.
    if (menuBusy || _flowRoutes.contains(topRoute)) {
      _showToast('Finish what is on screen first.');
      return;
    }
    final nav = navigatorKey.currentState;
    if (nav == null) return;
    switch (action) {
      case 'open':
        final screen = (msg['screen'] as String? ?? '').trim();
        _openScreenByVoice(screen);
        break;
      case 'back':
        if (nav.canPop()) {
          // maybePop, the back gesture's own path: a screen's PopScope
          // (the rule builder's unsaved blocks, a camera closing its feed)
          // gets its say. pop() walked straight past it.
          unawaited(nav.maybePop());
        } else {
          _showToast('Already at the start.');
        }
        break;
      case 'scroll':
        final direction = (msg['direction'] as String? ?? '').trim();
        if (!const {'up', 'down', 'top', 'bottom'}.contains(direction)) return;
        if (!_postVoiceUi(VoiceUiCommand.scroll(direction))) {
          _showToast('Nothing to scroll here.');
        }
        break;
      case 'select':
        final target = (msg['target'] as String? ?? '').trim();
        final label = (msg['label'] as String? ?? '').trim();
        if (target.isEmpty) return;
        // THE ID MUST BE ON SCREEN NOW. The hub echoes what the phone sent,
        // but a frame that arrives after the list changed, or one from a
        // hub that guessed, names nothing here and does nothing.
        final ctx = uiContext();
        final known = ctx == null
            ? const <Map<String, String>>[]
            : (ctx['targets'] as List<Map<String, String>>);
        final hit = known.where((t) => t['id'] == target).toList();
        if (hit.isEmpty) {
          _showToast('"$label" is not on this screen.');
          return;
        }
        // A sheet or dialog over the list (another device's details) is
        // closed first, as a tap outside it would -- otherwise the list
        // under it is not the route on top and nothing answers.
        _dismissPopups(nav);
        _selectByVoice(hit.first['kind'] ?? '', target, hit.first['label'] ?? label);
        break;
      case 'menu':
        // A spoken management action: "add a device", "new automation".
        // THE ID MUST BE ON THE PHONE'S OWN LIST, checked here again even
        // though the hub only answers from what this app advertised -- a
        // frame from a hub that guessed names nothing here and does
        // nothing. Then exactly what the Home sheet's row does: send the
        // menu action and let the hub run its flow, gate included.
        final menu = (msg['menu'] as String? ?? '').trim();
        final say = (msg['say'] as String? ?? menu).trim();
        if (!voiceMenuActions.contains(menu)) {
          debugPrint('[AppState] ui: refusing menu action "$menu"');
          return;
        }
        if (!_canStartMenuAction) {
          _showToast(isReady ? 'Finish what is on screen first.' : 'Not connected to the hub.');
          return;
        }
        // A spoken action names no row: a rule id left over from a tap
        // whose request was refused must not answer this flow's "Update
        // which automation?" by itself.
        _pendingEditId = null;
        _showToast(say);
        sendMenuAction(menu);
        break;
      default:
        debugPrint('[AppState] ui: unknown action "$action"');
    }
  }

  /// Open a screen the way the drawer does: the data screens ask the hub
  /// for their list and open when it arrives; the rest are plain routes.
  void _openScreenByVoice(String screen) {
    final nav = navigatorKey.currentState;
    if (nav == null) return;
    // ALREADY HERE: nothing to open. Asking the hub again cleared the list
    // under the person's eyes (the request empties it before the answer
    // refills it) and threw away where they had scrolled to.
    final route = screen == 'home' ? '/' : '/$screen';
    if (_screenIds[route] == screen && topRoute == route) {
      _dismissPopups(nav);
      _showToast(screen == 'home' ? 'Home' : 'Already here.');
      return;
    }
    // A sheet or dialog on the current screen goes first; the new screen
    // would otherwise open on top of it and Back would land in it again.
    if (screen != 'home') _dismissPopups(nav);
    switch (screen) {
      case 'home':
        nav.popUntil((r) => r.isFirst);
        _showToast('Home');
        break;
      case 'devices':
        if (!isReady) {
          _showToast('Not connected to the hub.');
          return;
        }
        requestDeviceList();
        break;
      case 'scenes':
        if (!isReady) {
          _showToast('Not connected to the hub.');
          return;
        }
        requestSceneList();
        break;
      case 'automations':
        if (!isReady) {
          _showToast('Not connected to the hub.');
          return;
        }
        requestAutomationList();
        break;
      case 'stats':
        if (!isReady) {
          _showToast('Not connected to the hub.');
          return;
        }
        requestReadings();
        break;
      case 'profile':
        if (!isReady) {
          _showToast('Not connected to the hub.');
          return;
        }
        requestProfile();
        break;
      case 'notifications':
      case 'settings':
        openScreen(route);
        break;
      default:
        debugPrint('[AppState] ui: unknown screen "$screen"');
    }
  }

  /// Select = exactly what a tap on that row does, and nothing a tap does
  /// not. Devices open their detail sheet (the screen owns it, so it is
  /// asked); scenes and automations open their editor; a notice runs its
  /// action, which the hub gates as it gates a tap.
  void _selectByVoice(String kind, String id, String label) {
    switch (kind) {
      case 'camera':
        if (!isReady) {
          _showToast('Not connected to the hub.');
          return;
        }
        viewCamera(id);
        break;
      case 'device':
      case 'board':
        // The Devices screen owns the detail sheets, so it is asked.
        if (!_postVoiceUi(VoiceUiCommand.select(id, label))) {
          _showToast('Open Devices to pick "$label".');
        }
        break;
      case 'scene':
        editScene(id);
        break;
      case 'automation':
        editAutomation(id);
        break;
      case 'notice':
        final n = notices.where((n) => n.id == id).toList();
        if (n.isEmpty || !n.first.actionable) return;
        if (!isReady) {
          _showToast('Not connected to the hub.');
          return;
        }
        // The card's button, step for step: leave Pending actions, then
        // run the notice.
        final nav = navigatorKey.currentState;
        if (nav != null && topRoute == '/notifications') {
          unawaited(nav.maybePop());
        }
        runNotice(n.first);
        break;
    }
  }

  /// Hand a scroll/select to the list screen on top. Listeners run
  /// synchronously, so by the time the value is cleared the screen has
  /// acted -- or nothing could, and the caller says so. Cleared at once
  /// rather than after a frame: a post-frame callback waits for a frame
  /// nobody scheduled.
  bool _postVoiceUi(VoiceUiCommand cmd) {
    voiceUi.value = cmd;
    voiceUi.value = null;
    return cmd.handled;
  }

  /// Close the dialogs, sheets and popup menus over the current screen --
  /// what a tap outside each would do. Screens (page routes) stay.
  void _dismissPopups(NavigatorState nav) {
    nav.popUntil((r) => r is! PopupRoute);
  }

  void _handleOpen(Map<String, dynamic> msg) {
    final screen = (msg['screen'] as String? ?? '').trim();

    // 'web' is the one destination that carries its own address: an
    // integration's sign-in page, which Home Assistant names at the moment
    // it needs it and which is different every time.
    if (screen == 'web') {
      final url = (msg['url'] as String? ?? '').trim();
      final parsed = Uri.tryParse(url);
      // https anywhere; http only to an address inside the house.
      final allowed = url.isNotEmpty &&
          parsed != null &&
          (parsed.isScheme('https') ||
              (parsed.isScheme('http') && _privateHttpHost(parsed.host)));
      if (!allowed) {
        debugPrint('[AppState] open: refusing web url "$url"');
        _showToast('The hub asked to open a page this app will not load.');
        return;
      }
      // TAILSCALE: in the phone's browser (Google and others refuse to sign
      // in inside an app), with a waiting card here until the hub answers.
      if (msg['external'] == true) {
        _releaseHeldQuestion();
        _handedOff = true;
        tailscaleSignInUrl = url;
        tailscaleSigningIn = true;
        _tsSignInAt = DateTime.now();
        unawaited(_openInBrowser(url));
        WidgetsBinding.instance
            .addPostFrameCallback((_) => _showTailscaleWaiting());
        notifyListeners();
        return;
      }
      // Order matters: releasing clears any spent sign-in page, so the new
      // one is set AFTER it, not before.
      _releaseHeldQuestion();
      pendingWebUrl = url;
      pendingWebTitle = (msg['title'] as String? ?? '').trim();
      // Why this page might not load, when the hub knows why in advance.
      pendingWebNote = (msg['note'] as String? ?? '').trim();
      if (topRoute != '/web') {
        navigatorKey.currentState?.pushNamed('/web');
      }
      // NOT a hand-off: the flow is still running and still waiting on this
      // sign-in, so its result — success or timeout — is real news and must
      // keep its popup.
      notifyListeners();
      return;
    }

    if (screen == 'camera') {
      final url = (msg['url'] as String? ?? '').trim();
      final snap = (msg['snapshot'] as String? ?? '').trim();
      bool allowed(String u) {
        final p = Uri.tryParse(u);
        return u.isNotEmpty &&
            p != null &&
            (p.isScheme('https') ||
                (p.isScheme('http') && _privateHttpHost(p.host)));
      }
      if (!allowed(url) || (snap.isNotEmpty && !allowed(snap))) {
        debugPrint('[AppState] open camera: refusing url "$url"');
        _showToast('The hub sent a camera address this app will not load.');
        return;
      }
      // A PHONE'S LOOPBACK IS NEVER THE HOUSE. The hub swaps its own
      // "localhost" for a reachable address before sending; if one slips
      // through, say what is wrong rather than showing a dead feed.
      final host = Uri.parse(url).host.toLowerCase();
      if (host == 'localhost' || host.startsWith('127.') || host == '::1') {
        _showToast('The hub sent its own local address for the camera. '
            'Set internal_url in Home Assistant to an address on the '
            'house network.');
        return;
      }
      _releaseHeldQuestion();
      cameraSeq++;
      pendingCamera = {
        'url': url,
        'snapshot': snap,
        'title': (msg['title'] as String? ?? 'Camera').trim(),
        'device_id': (msg['device_id'] as String? ?? '').trim(),
        'state': (msg['state'] as String? ?? '').trim(),
      };
      // A refresh while the screen is already up replaces the feed in
      // place; a first view pushes the screen.
      if (topRoute != '/camera') {
        navigatorKey.currentState?.pushNamed('/camera');
      }
      _handedOff = true;
      notifyListeners();
      return;
    }

    final route = _openableScreens[screen];
    if (route == null) {
      debugPrint('[AppState] open: unknown screen "$screen"');
      return;
    }
    // The flow that sent this is ending. Drop the spent question screen
    // FIRST — pushing over it would leave it buried, and its own pop guard
    // (route.isCurrent) would then refuse to clear it, so backing out of
    // the installer would land on a dead prompt.
    _releaseHeldQuestion();
    openScreen(route);
    _handedOff = true;
  }

  /// Set by _handleOpen, cleared by the result that follows it. The hand-off
  /// IS the outcome — a "Done" card on top of the installer the person was
  /// just sent to is noise covering the thing they need to read.
  bool _handedOff = false;

  /// The page a sign-in step asked for, and what to call it. Held here rather
  /// than passed as route arguments so the screen survives a rebuild.
  String? pendingWebUrl;
  String pendingWebTitle = '';

  /// Why this sign-in page may not open here, when the hub knows
  /// in advance -- the Tailscale case: the page needs internet on
  /// the PHONE, and the hub's access point deliberately has none.
  String pendingWebNote = '';

  /// Backing out of a sign-in page. The flow itself is NOT cancelled — the
  /// hub is watching Home Assistant, not this screen, and the sign-in may
  /// well have completed in another tab. It carries on until it advances or
  /// times out, which is also what happens if the phone is simply locked.
  void closeWebPage() {
    pendingWebUrl = null;
    pendingWebTitle = '';
    pendingWebNote = '';
    notifyListeners();
  }

  /// The registration screen the hub is showing, or null.
  RegistrationFrame? pendingRegistration;

  /// Bumped on every `registration` frame, so the screen can tell a fresh
  /// form (reset the boxes) from a rebuild (keep what is being typed).
  int registrationSerial = 0;

  void _handleRegistration(Map<String, dynamic> msg) {
    settleStarting();
    // One outstanding question at a time: this form proves any earlier
    // question is over (as _handlePrompt). Its OWN mirror stays: after
    // "Fill in with AI" the screen is still up and this frame refills it,
    // and clearing it here would make _releaseHeldQuestion pop it.
    _dropQuestions(except: 'registration');
    _releaseHeldQuestion();
    if (_takeFlowCancel('registration')) return;
    questionSerial++;
    registrationSerial++;
    pendingRegistration = RegistrationFrame.fromJson(msg);
    if (topRoute != '/registration') {
      navigatorKey.currentState?.pushNamed('/registration');
    }
    notifyListeners();
  }

  /// Answer the registration screen: `generate` keeps it open (the hub
  /// fills the fields and sends the form again), `save` and `cancel` end
  /// it. The screen stays up until the hub's next frame decides.
  void sendRegistrationReply(String action, Map<String, dynamic> values) {
    if (pendingRegistration == null) return;
    _flow?.abortCapture();
    sendJson({
      'type': 'registration_response',
      'action': action,
      'values': values,
    });
    if (action != 'generate') {
      pendingRegistration = null;
      _flowLines.clear();
      if (topRoute == '/registration' && !menuBusy) {
        navigatorKey.currentState?.pop();
      }
    }
    notifyListeners();
  }

  void _handleForm(Map<String, dynamic> msg) {
    settleStarting();
    // Same supersession rule as _handlePrompt; a re-sent form replaces
    // its own screen in place.
    _dropQuestions(except: 'form');
    _releaseHeldQuestion();
    if (_takeFlowCancel('form')) return;
    questionSerial++;
    pendingForm = FormFrame(
      title: msg['text'] as String? ?? '',
      note: msg['note'] as String? ?? '',
      fields: [
        for (final f in (msg['fields'] as List? ?? const []))
          if (f is Map<String, dynamic>) FlowField.fromJson(f)
      ],
    );
    if (topRoute != '/form') {
      navigatorKey.currentState?.pushNamed('/form');
    }
  }

  /// Send a completed form. Idempotent and targeted, exactly like
  /// sendPromptReply — a double-tap must not pop a second route.
  void sendFormResponse(Map<String, dynamic> values) {
    if (pendingForm == null) return;
    _flow?.abortCapture();
    sendJson({'type': 'form_response', 'values': values});
    pendingForm = null;
    _flowLines.clear();
    if (topRoute == '/form' && !menuBusy) {
      navigatorKey.currentState?.pop();
    }
    notifyListeners();
  }

  void _handleRuleBuilder(Map<String, dynamic> msg) {
    settleStarting();
    _dropQuestions(); // same supersession rule as _handlePrompt
    _releaseHeldQuestion();
    if (_takeFlowCancel('automation_builder')) return;
    ruleBuilder = RuleBuilderFrame(
      devices: (msg['devices'] as Map?)?.cast<String, dynamic>() ?? const {},
      targets: (msg['targets'] as List?) ?? const [],
      scenes: (msg['scenes'] as List?) ?? const [],
      elseKinds: (msg['else_kinds'] as List?) ?? const [],
      rule: (msg['rule'] as Map?)?.cast<String, dynamic>(),
      tried: [for (final t in (msg['tried'] as List? ?? const [])) '$t'],
      heard: (msg['heard'] ?? '').toString(),
      unsure: [for (final t in (msg['unsure'] as List? ?? const [])) '$t'],
    );
    // THE SAME SCREEN, a different face. Already on Automations (the FAB,
    // or a tapped rule)? Nothing is pushed -- the body simply becomes the
    // blocks. Arriving from anywhere else (a spoken "add an automation")
    // opens Automations, which shows the blocks because a rule is in
    // flight.
    if (topRoute != '/automations') {
      openScreen('/automations');
    }
    notifyListeners();
  }

  void _handleSceneBuilder(Map<String, dynamic> msg) {
    settleStarting();
    _dropQuestions(); // same supersession rule as _handlePrompt
    _releaseHeldQuestion();
    if (_takeFlowCancel('scene_builder')) return;
    sceneBuilder = SceneBuilderFrame(
      editing: msg['editing'] == true,
      scene: (msg['scene'] as Map?)?.cast<String, dynamic>() ?? const {},
      targets: (msg['targets'] as List?) ?? const [],
      whom: msg['whom'] == true,
      aiAvailable: msg['ai_available'] == true,
      tried: [for (final t in (msg['tried'] as List? ?? const [])) '$t'],
      error: (msg['error'] ?? '').toString(),
      aiText: (msg['ai_text'] ?? '').toString().trim(),
      aiError: (msg['ai_error'] ?? '').toString(),
      aiFor: (msg['ai_for'] ?? '').toString(),
    );
    sceneAiBusy = false; // the hub has answered
    // The Scenes screen turns into the editor; from elsewhere, open it.
    if (topRoute != '/scenes') {
      openScreen('/scenes');
    }
    notifyListeners();
  }

  /// Save the scene, or null to abandon it (Back, after "Discard changes?").
  /// The hub answers with the screen again when it cannot be saved.
  void sendSceneBuilder(Map<String, dynamic>? scene) {
    if (sceneBuilder == null) return;
    _flow?.abortCapture();
    if (scene == null) {
      sendJson({'type': 'scene_builder_response', 'cancelled': true});
      sceneBuilder = null;
      sceneAiBusy = false;
    } else {
      sendJson({'type': 'scene_builder_response', 'action': 'save',
                'scene': scene});
    }
    notifyListeners();
  }

  /// TRY IT NOW: each device does its part once; nothing is saved.
  void trySceneBuilder(Map<String, dynamic> scene) {
    if (sceneBuilder == null) return;
    sendJson({'type': 'scene_builder_response', 'action': 'try',
              'scene': scene});
  }

  /// Ask the AI what the scene is for. As registration's "Ask the AI":
  /// the screen waits, and comes back with the suggestion beside the
  /// person's words.
  void askSceneAi(Map<String, dynamic> scene) {
    if (sceneBuilder == null || sceneAiBusy) return;
    sceneAiBusy = true;
    sendJson({'type': 'scene_builder_response', 'action': 'generate',
              'scene': scene});
    notifyListeners();
  }

  /// Send the finished rule, or null to abandon it.
  ///
  /// Same idempotence and same targeted pop as sendPinMap, for the same
  /// reason: building a rule takes minutes and a double-tap on Save must
  /// not pop a second route out from under whatever the hub does next.
  void sendRuleBuilder(Map<String, dynamic>? rule) {
    if (ruleBuilder == null) return;
    _flow?.abortCapture();
    sendJson(rule == null
        ? {'type': 'automation_builder_response', 'cancelled': true}
        : {'type': 'automation_builder_response', 'rule': rule});
    ruleBuilder = null;
    // NO POP. Clearing the frame is the whole transition: the screen
    // falls back to the list it was already on. The hub's result then
    // refreshes that list with the rule just saved.
    notifyListeners();
  }

  /// TRY IT NOW: the hub runs the rule's THEN once, saves nothing, and
  /// sends the builder back as it was, with what happened.
  void tryRuleBuilder(Map<String, dynamic> rule) {
    if (ruleBuilder == null) return;
    sendJson({'type': 'automation_builder_response', 'try': rule});
  }

  /// THE SENTENCE BOX: the hub reads it and sends the builder back with the
  /// boxes filled (and which ones to check).
  void fillRuleFromSentence(String text) {
    if (ruleBuilder == null || text.trim().isEmpty) return;
    sendJson({'type': 'automation_builder_response', 'sentence': text.trim()});
  }

  /// Open the builder for an EXISTING rule: the same door the hub owns,
  /// asked for by name. The hub replies with the builder frame pre-filled,
  /// so add and edit really are one screen.
  void editAutomation(String id) {
    _pendingEditId = id;
    sendMenuAction('update_automation');
  }

  String? _pendingEditId;

  /// Pause or resume one rule, and remove one — both named by id, so no
  /// "which one?" is asked for something a tap already answered. The hub
  /// gates them exactly as it gates Update and Remove.
  void pauseAutomation(String id) => sendMenuAction('pause:$id');

  void removeAutomation(String id) => sendMenuAction('drop:$id');

  /// Ask the hub for the automation list, then show it (mirrors
  /// requestDeviceList / requestSceneList).
  /// No collecting flag here, unlike devices and scenes: those two are
  /// assembled from the OUTPUT lines as they stream, so they need to know
  /// a listing is in flight. Automations arrive whole in one
  /// `automation_list` frame, so there is nothing to gather and nothing
  /// to remember.
  void requestAutomationList() => sendMenuAction('list_automations');

  void _handlePinMap(Map<String, dynamic> msg) {
    settleStarting();
    // Supersession as _handlePrompt; a newer board frame replaces its own
    // screen in place, so the pin map's mirror stays until reassigned.
    _dropQuestions(except: 'pin_map');
    _releaseHeldQuestion();
    if (_takeFlowCancel('pin_map')) return;
    var catalogue = (msg['catalogue'] as Map?)?.cast<String, dynamic>() ??
        const <String, dynamic>{};
    // PINS THE MODULE KEEPS (GPIO16/17 on a WROVER, or on a board not yet
    // flashed): the hub refuses them at save, so they are not offered.
    final owned = ((msg['pins_owned_by_module'] as List?) ?? const [])
        .whereType<int>()
        .toSet();
    final pinList = catalogue['pins'];
    if (owned.isNotEmpty && pinList is List) {
      catalogue = Map<String, dynamic>.from(catalogue);
      catalogue['pins'] = [
        for (final p in pinList)
          if (p is Map && owned.contains(p['n']))
            <String, dynamic>{
              ...p.cast<String, dynamic>(),
              'can': const <String>[],
              'severity': 'impossible',
              'caution': 'Not available on this board: the module may keep '
                  'GPIO${p['n']} for its own memory. The hub confirms this '
                  'at the first flash.',
            }
          else
            p,
      ];
    }
    pinMap = PinMapFrame(
      boardName: msg['board_name'] as String? ?? 'Board',
      catalogue: catalogue,
      items: (msg['items'] as List?) ?? const [],
    );
    if (topRoute != '/pins') {
      navigatorKey.currentState?.pushNamed('/pins');
    }
    notifyListeners();
  }

  /// Send a finished layout, or null to abandon it.
  ///
  /// Idempotent and targeted, exactly like sendFormResponse: laying out a
  /// board takes minutes, and a double-tap on Save must not pop a second
  /// route out from under whatever the hub does next.
  void sendPinMap(List<Map<String, dynamic>>? items,
      {List<Map<String, dynamic>>? draft}) {
    if (pinMap == null) return;
    _flow?.abortCapture();
    // A CANCEL CARRIES WHAT WAS PLACED (`draft`): the hub keeps an
    // unfinished layout in Saved layouts instead of throwing minutes of
    // pin-by-pin work away. An older hub ignores the extra key.
    sendJson(items == null
        ? {
            'type': 'pin_map_response',
            'cancelled': true,
            if (draft != null && draft.isNotEmpty) 'draft': draft,
          }
        : {'type': 'pin_map_response', 'items': items});
    pinMap = null;
    // POP WHENEVER WE ARE ON THAT SCREEN. The condition used to also
    // demand `!menuBusy` -- but Edit a board IS a menu flow, so cancelling
    // from inside one cleared the frame and then declined to leave the
    // route. The screen rebuilt with no catalogue and painted nothing: the
    // black screen with no way back. `topRoute == '/pins'` is already the
    // whole guard needed -- it pops this screen and only this screen, and
    // if the hub has another question it pushes its own route next.
    if (topRoute == '/pins') {
      navigatorKey.currentState?.pop();
    }
    notifyListeners();
  }

  void _handleRecordRequired(Map<String, dynamic> msg) {
    settleStarting();
    _dropQuestions(); // same supersession rule as _handlePrompt
    _releaseHeldQuestion();
    // Same armed-cancel rule as _handlePrompt: a ceremony cancel pressed
    // while the hub was between questions lands on whatever it asks next.
    if (_ceremonyCancelNext) {
      _ceremonyCancelNext = false;
      sendJson(_cancelFrame('record')!);
      return;
    }
    if (_takeFlowCancel('record')) return;
    pendingRecord = RecordFrame(
      phrase: msg['phrase'] as String? ?? '',
      context: msg['context'] as String? ?? 'recording',
      index: (msg['index'] as num?)?.toInt() ?? 0,
      total: (msg['total'] as num?)?.toInt() ?? 0,
    );
    if (pendingRecord!.context == 'enrollment') {
      // FIX: enrollment sends one record_required PER PHRASE (3–5 of them);
      // pushing unconditionally stacked an EnrollScreen per phrase and the
      // back button had to be hammered through all of them.
      final wasEnroll = currentView == 'enroll';
      currentView = 'enroll';
      if (!wasEnroll) navigatorKey.currentState?.pushNamed('/enroll');
    } else {
      notifyListeners();
    }
  }

  void _handleChallenge(Map<String, dynamic> msg) {
    settleStarting();
    _dropQuestions(); // same supersession rule as _handlePrompt
    _releaseHeldQuestion(); // same rule as record_required
    if (_takeFlowCancel('challenge')) return;
    challengePhrase = msg['phrase'] as String? ?? '';
    notifyListeners();
  }

  // ── Files: file_request (firmware for the hub) ─────────────────────

  void _handleFileRequest(Map<String, dynamic> msg) {
    settleStarting();
    _dropQuestions(); // same supersession rule as _handlePrompt
    // A held /choice ("Send from this phone", answered, spinner showing)
    // comes down: the picker is this question's screen.
    _releaseHeldQuestion();
    // Same armed-cancel rules as record_required.
    if (_ceremonyCancelNext) {
      _ceremonyCancelNext = false;
      sendJson(_cancelFrame('file_request')!);
      return;
    }
    if (_takeFlowCancel('file_request')) return;
    final req = FileRequestFrame.fromJson(msg);
    questionSerial++;
    pendingFileRequest = req;
    final serial = ++_fileRequestSerial;
    notifyListeners();
    unawaited(_answerFileRequest(req, serial));
  }

  /// Is [req] still the question the hub is waiting on?
  bool _fileRequestLive(FileRequestFrame req, int serial) =>
      identical(pendingFileRequest, req) && serial == _fileRequestSerial;

  /// Answer a file_request from the system picker.
  Future<void> _answerFileRequest(FileRequestFrame req, int serial) async {
    while (true) {
      PickOutcome out;
      try {
        out = await PhoneFiles.pick(
            multiple: req.multiple, accept: req.accept, family: req.family);
      } catch (e) {
        debugPrint('[AppState] file picker failed: $e');
        if (!_fileRequestLive(req, serial)) return;
        _showToast('The file picker could not open.');
        _cancelFileRequest(req);
        return;
      }
      if (!_fileRequestLive(req, serial)) return;
      if (out.cancelled) {
        _cancelFileRequest(req);
        return;
      }
      final total = PhoneFiles.totalBytes(out.files);
      String? problem;
      if (out.files.isEmpty) {
        problem = 'None of those is a file the hub asked for'
            '${req.accept.isEmpty ? '' : ' (${req.accept.join(', ')})'}.';
      } else if (req.maxBytes > 0 && total > req.maxBytes) {
        problem = 'Those files come to ${PhoneFiles.sizeLabel(total)}; the '
            'hub takes at most ${PhoneFiles.sizeLabel(req.maxBytes)} at once.';
      }
      if (problem != null) {
        if (out.skipped.isNotEmpty) {
          problem = '$problem\n\nLeft out: ${out.skipped.join(', ')}';
        }
        final again = await _askPickAgain(problem);
        if (!_fileRequestLive(req, serial)) return;
        if (!again) {
          _cancelFileRequest(req);
          return;
        }
        continue;
      }
      if (out.skipped.isNotEmpty) {
        _showToast('Left out: ${out.skipped.join(', ')}');
      }
      _sendFileResponse(req, out.files);
      return;
    }
  }

  void _sendFileResponse(FileRequestFrame req, List<PickedFile> files) {
    if (!identical(pendingFileRequest, req)) return;
    _flow?.abortCapture();
    sendJson(fileResponseFrame(PhoneFiles.wireFiles(files)));
    pendingFileRequest = null;
    status = 'sent ${files.length} file${files.length == 1 ? '' : 's'} '
        'to the hub';
    notifyListeners();
  }

  void _cancelFileRequest(FileRequestFrame req) {
    if (!identical(pendingFileRequest, req)) return;
    sendJson(_cancelFrame('file_request')!);
    pendingFileRequest = null;
    status = 'cancelled';
    notifyListeners();
  }

  /// "Pick again" or "Cancel" after a pick the hub would refuse. A tap
  /// outside is Cancel: nothing may leave the hub waiting.
  Future<bool> _askPickAgain(String problem) async {
    final ctx = navigatorKey.currentContext;
    if (ctx == null) return false;
    final again = await showDialog<bool>(
      context: ctx,
      builder: (dctx) => AlertDialog(
        backgroundColor: surface,
        surfaceTintColor: Colors.transparent,
        title: Text('Those files cannot go',
            style: TextStyle(
                color: ink, fontSize: 17, fontWeight: FontWeight.w600)),
        content: SingleChildScrollView(
          child: Text(problem,
              style: TextStyle(color: inkDim, fontSize: 13.5, height: 1.4)),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dctx, false),
            child: Text('Cancel', style: TextStyle(color: cancelInk)),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
                backgroundColor: accent, foregroundColor: accentFg),
            onPressed: () => Navigator.pop(dctx, true),
            child: const Text('Pick again'),
          ),
        ],
      ),
    );
    return again == true;
  }

  /// Install firmware images on this hub, picked on this phone (Settings).
  /// False when nothing was sent (not connected, or a flow running).
  bool installFirmwareOnHub() {
    if (!isReady) {
      _showToast('Not connected to the hub.');
      return false;
    }
    if (menuBusy || _pendingKind != null) {
      _showToast('Finish what is on screen first.');
      return false;
    }
    return sendMenuAction('firmware_from_phone');
  }

  /// The hub's refusal or checkpoint for a firmware install, IN FULL and until
  /// dismissed: it is the list of what this hub is missing (the minimum
  /// requirements), and the timed popup and the 4 s banner both took it
  /// away before it could be read.
  void _showFirmwareVerdict(String text, {required bool checkpointed}) {
    final ctx = navigatorKey.currentContext;
    if (ctx == null) return;
    showDialog<void>(
      context: ctx,
      builder: (dctx) => AlertDialog(
        backgroundColor: surface,
        surfaceTintColor: Colors.transparent,
        title: Text(checkpointed ? 'Saved on the hub, not installed' : 'Not installed',
            style: TextStyle(
                color: ink, fontSize: 17, fontWeight: FontWeight.w600)),
        content: SingleChildScrollView(
          child: SelectableText(text,
              style: TextStyle(color: inkDim, fontSize: 13.5, height: 1.45)),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dctx),
            child: Text('OK', style: TextStyle(color: accent)),
          ),
        ],
      ),
    );
  }

  void _handleOutput(Map<String, dynamic> msg) {
    final level = msg['level'] as String?;
    final text = msg['text'] as String? ?? '';
    settleStarting(); // any line from the hub is a response
    // A CHECK THAT WAS SKIPPED SAYS SO: the hub allowed a gated action
    // inside its verified window. A passing line, not silence -- silence
    // reads as "the PIN gate is missing".
    if (msg['kind'] == 'gate_skipped') {
      _showToast(text.trim());
      status = text.trim();
      notifyListeners();
      return;
    }
    if (level == 'important' &&
        text.trimLeft().startsWith('RECOVERY PHRASE')) {
      // ONLY the recovery words (main.py menu_setup_pin). `important` is
      // also how the hub announces a newly paired phone to every handset,
      // and that sentence used to land here as a "Recovery phrase" card.
      keepsafeMessage = text;
      // Shown from HERE, exactly like the result popup — a themed card over
      // a dimmed, blurred backdrop, owned by AppState so no screen has to
      // be watching. HomeScreen's old dialog (grey hang) is gone.
      WidgetsBinding.instance
          .addPostFrameCallback((_) => _showKeepsafeDialog());
    } else if (level == 'important') {
      // Announced and gone: a banner, and the line in the flow record.
      final line = text.trim();
      if (line.isNotEmpty) {
        if (_flowLines.isEmpty || _flowLines.last != line) {
          _flowLines.add(line);
          if (_flowLines.length > 8) _flowLines.removeAt(0);
        }
        WidgetsBinding.instance
            .addPostFrameCallback((_) => _showDropBanner(line));
        status = line;
      }
    } else {
      // Feed AND status: the feed is the record (multi-row listings survive),
      // the status line stays the at-a-glance latest. Blank spacer rows are
      // log-only, exactly like the browser client.
      final trimmed = text.trim();
      // Mid-flow banner: anything informative, skipping pure decoration.
      if (trimmed.isNotEmpty &&
          !trimmed.startsWith('=') &&
          !RegExp(r'^-{3,}').hasMatch(trimmed)) {
        // ANSWER LINES ARE KEPT FOR THE RESULT. While a spoken/typed
        // command is being worked (thinking, not a menu flow), the lines
        // the hub streams back ARE the answer — "[STATE] ceiling light:
        // on". They were spoken and then gone; now they also become the
        // answer card shown at the result (field test 2026-08-25: "is
        // there light — it says the status and also shows a pop up").
        if (flowState == FlowState.thinking && !menuBusy) {
          var answer = trimmed;
          if (answer.startsWith('[STATE]')) {
            answer = answer.substring(7).trim();
          }
          if (answer.isNotEmpty && !answer.startsWith('[')) {
            _queryAnswerLines.add(answer);
          }
        }
        if (_flowLines.isEmpty || _flowLines.last != trimmed) {
          _flowLines.add(trimmed);
          if (_flowLines.length > 8) _flowLines.removeAt(0);
        }
      }
      if (_collectingReport) {
        // A BOARD REPORT IS TWENTY LINES AND IT WAS A FLASH. Every menu
        // action that produces a LIST has a collector and somewhere to put
        // it; board_report had neither, so its lines went into _flowLines
        // — eight of them, rolling — and the result frame then cleared the
        // flow half a second later. The report arrived, was truncated, and
        // was thrown away (2026-09-01: "just did a quick flash").
        //
        // Blank lines are KEPT here. This is a report with four sections
        // and the gaps between them are how it is read.
        reportLines.add(trimmed);
      }
      status = text;
    }
    notifyListeners();
  }

  void _handleResult(Map<String, dynamic> msg) {
    settleStarting();
    final ok = msg['ok'] == true;
    final reason = msg['reason'] as String?;
    final text = msg['text'] as String?;
    final parts = msg['parts'] as Map<String, dynamic>?;

    // FIX (ported from the browser client): a result that REFUSES a
    // concurrent request must not tear down the flow that is still asking a
    // question. A command sent mid-enrollment comes back busy/
    // setup_in_progress WHILE the server still waits on a recording —
    // clearing pending and popping here killed the enroll screen and left
    // the server blocked with nothing on screen able to answer it. And
    // not_enrolled is the handshake, not an error: our command frame just
    // started enrollment server-side; the record_required frames follow.
    // not_bound is the same handshake for a paired phone nobody is behind
    // yet: the login/sign-up choice follows it.
    //
    // EVALUATED FIRST, before menuBusy, the flow lines or the AI-review
    // script are touched: this verdict answers a request that is NOT the
    // running flow's, so nothing of that flow may be lowered by it.
    const concurrentRefusals = {
      'busy', 'server_busy', 'setup_in_progress', 'not_enrolled', 'not_bound',
    };
    if (!ok && concurrentRefusals.contains(reason)) {
      // WHICH request was refused. The hub names it (`action`: "command"
      // or "menu:<action>"); only that request's own flag comes down, so a
      // refused background read (check_notices on connect) can no longer
      // lower the flag of a list request that is still arriving. An older
      // hub sends no `action`, and every flag comes down as before.
      final refused = msg['action'] as String?;
      bool mine(String action) =>
          refused == null || refused == action || refused.startsWith('$action:');
      if (mine('menu:boardreport') || mine('menu:checkreader')) {
        _collectingReport = false;
      }
      if (mine('menu:readings')) _awaitingReadings = false;
      // Only the profile read's own refusal (or an unnamed one) ends its
      // "checking…": a refused tap elsewhere used to drop it while the
      // read was still in flight, showing the old rows as current.
      if (mine('menu:profile')) {
        _profileForBinding = false;
        profileLoading = false;
      }
      // menuBusy belongs to a flow of OURS that is still running -- a
      // question on screen, a job room, a held spinner. With none of those
      // the refused action was the only one, and its flag comes down.
      if (!_ourFlowRunning) {
        menuBusy = false;
        // ...and so do the flags that refused action raised for its own
        // ending: a tapped rule's id would otherwise answer the NEXT
        // "Update which" picker sight unseen, and a profile-row flag would
        // claim the next set_pin from anywhere as a profile change.
        _pendingEditId = null;
        _profileFlow = false;
      }
      status = reason == 'not_enrolled'
          ? 'starting enrollment…'
          : (text ?? reason!.replaceAll('_', ' '));
      notifyListeners();
      return; // leave pending questions, screens and challenge alone
    }

    // NAVIGATION: the `ui` frame that follows this result does the work;
    // the result itself is only the flow coming to rest. No popup, no
    // wipe of anything on screen.
    if (reason != null && reason.startsWith('ui:')) {
      status = (text != null && text.isNotEmpty) ? '\u2192 $text' : status;
      // The command HAS ended, though: a cancel armed against it (the orb's
      // X while it was thinking) is spent, and must not ride on to cancel
      // the first question of whatever flow comes next.
      _flowCancelNext = false;
      notifyListeners();
      return;
    }

    // A read that ENDED, however it ended. Without this a refused profile
    // read (a second phone holding the shell lock) would leave the screen
    // saying "checking…" with nothing ever coming to clear it — which is
    // the stuck half of the flash this flag was added to fix.
    profileLoading = false;
    menuBusy = false;
    // A flow that ended by opening a screen has already shown its outcome:
    // the screen. Consumed HERE, before any branch can return, so it can
    // never leak into the NEXT flow's popup -- the early endings below
    // (failure, checkpoint, the list refreshes) used to leave it raised,
    // and the next Add device finished without its Done window.
    final handedOff = _handedOff;
    _handedOff = false;
    // The hub's LAST spoken line, rescued before the wipe below. A flow
    // that refuses says why in its final write ("No board carries an
    // infrared receiver…") and then returns False -- the failure popup
    // used to clear that sentence and show the generic "did not
    // complete" in its place, so Learn IR's teaching refusal was never
    // seen (field test 2026-08-26).
    final lastFlowLine =
        _flowLines.isNotEmpty ? _flowLines.last : null;
    _flowLines.clear();

    // READ ONCE, LOWERED ALWAYS. Whatever this result turns out to be, the
    // "started from the profile screen" flag does not survive it — a flow
    // that ended down some path this branch never sees (a cancel, a
    // disconnect) would otherwise leave it raised, and the NEXT set_pin
    // from anywhere at all would be treated as a profile change.
    final fromProfile = _profileFlow;
    _profileFlow = false;
    // The silent profile read that refreshes the nameplate after login.
    final forBinding = _profileForBinding;
    _profileForBinding = false;

    // The flow this dialog was checking on has ENDED -- quiet dismissal,
    // no still_here for a question that no longer exists. (Below the
    // concurrent-refusal return on purpose: a busy verdict for someone
    // else's request ends nothing of ours.)
    _dismissStillThere();

    // A FIRMWARE INSTALL'S REFUSAL is shown IN FULL until dismissed: its
    // text is what was wrong with the files.
    if (reason == 'menu:firmware_from_phone') {
      final generic = text == null || text.endsWith('did not complete');
      final said = (!ok && generic && msg['checkpointed'] != true)
          ? (lastFlowLine ?? text)
          : text;
      final full = (said == null || said.trim().isEmpty) ? null : said.trim();
      if (!ok && full != null) {
        final checkpointed = msg['checkpointed'] == true;
        status = checkpointed ? 'Saved on the hub' : 'Not installed';
        WidgetsBinding.instance.addPostFrameCallback(
            (_) => _showFirmwareVerdict(full, checkpointed: checkpointed));
        _stopCollecting();
        _clearPending();
        _queryAnswerLines.clear();
        currentView = 'command';
        _popFlowRoutes();
        notifyListeners();
        return;
      }
    }

    // THE RIBBON'S THIRD OUTCOME, decided BEFORE any per-list branch: a
    // job that PARKED its work (checkpointed) is neither the Done window
    // nor the amber alarm, and a job flow that FAILED gets its popup.
    // Both used to be caught first by the add/update/remove branches
    // below, which only ever wrote a status line for them.
    if (!ok && msg['checkpointed'] == true) {
      status = text ?? 'Saved — continue from Notifications';
      final line = status;
      WidgetsBinding.instance.addPostFrameCallback(
          (_) => _showDropBanner(line, icon: Icons.bookmark_outline));
      _stopCollecting();
      _clearPending();
      _queryAnswerLines.clear();
      currentView = 'command';
      _popFlowRoutes();
      notifyListeners();
      return;
    }
    // A card reader check finished, passed or not: its step lines in a
    // dialog held until dismissed (the failing step is the last line).
    if ((reason ?? '').startsWith('menu:checkreader:')) {
      _collectingReport = false;
      _clearPending();
      _queryAnswerLines.clear();
      currentView = 'command';
      _popFlowRoutes();
      final body = reportLines.join('\n').trim();
      status = ok ? 'card reader checked' : (text ?? 'the check did not finish');
      if (body.isNotEmpty) {
        WidgetsBinding.instance.addPostFrameCallback(
            (_) => _showReportPopup(body, title: 'Card reader check'));
      } else if (!ok) {
        final why = status;
        WidgetsBinding.instance.addPostFrameCallback(
            (_) => _showResultPopup(ok: false, message: why));
      }
      notifyListeners();
      return;
    }
    if (!ok &&
        reason != null &&
        reason.startsWith('menu:') &&
        !forBinding &&
        !_backgroundActions.contains(reason)) {
      final generic = text == null || text.endsWith('did not complete');
      final why = generic ? (lastFlowLine ?? text ?? 'did not complete') : text;
      WidgetsBinding.instance
          .addPostFrameCallback((_) => _showResultPopup(ok: false, message: why));
      status = text ?? reason;
      _stopCollecting(); // a list that failed collects nothing more
      _clearPending();
      _queryAnswerLines.clear();
      currentView = 'command';
      _popFlowRoutes();
      notifyListeners();
      return;
    }

    // A rule was SAVED from the blocks: the screen is about to show the
    // list again, and that list predates the rule. Ask for it afresh.
    if (ok &&
        (reason == 'menu:add_automation' ||
            reason == 'menu:update_automation') &&
        topRoute == '/automations') {
      requestAutomationList();
    }

    // A rule was paused, resumed or removed FROM the list: the list is
    // now stale, so ask for it again -- the result of THAT re-pushes the
    // screen with the truth on it.
    if (reason != null &&
        (reason.startsWith('menu:pause:') || reason.startsWith('menu:drop:'))) {
      _clearPending();
      currentView = 'command';
      _popFlowRoutes();
      if (ok) {
        requestAutomationList();
      } else {
        status = text ?? 'that change did not stick';
      }
      notifyListeners();
      return;
    }

    // list_automations finished: the rules get their own screen too, and
    // it is the way IN to the builder -- tapping a rule reopens it as
    // blocks (her ruling: one builder for add and edit).
    if (reason == 'menu:list_automations') {
      _clearPending();
      currentView = 'command';
      _popFlowRoutes();
      if (ok) {
        status = automationRecords.isEmpty
            ? 'no automations yet'
            : '${automationRecords.length} automation(s)';
        openScreen('/automations');
      } else {
        status = text ?? 'could not list automations';
      }
      notifyListeners();
      return;
    }

    // The board report finished. A DIALOG, NOT A SCREEN — it is read once
    // and dismissed, so it wants no route of its own — and NOT the result
    // popup either, which dismisses itself on a timer. That is exactly
    // wrong for the one output somebody is going to squint at and read
    // twice.
    // Both doors to the report: the picker ('board_report') and the
    // board sheet's own ('boardreport:<slug>', 2026-09-11).
    if (reason == 'menu:board_report' ||
        (reason ?? '').startsWith('menu:boardreport:')) {
      _collectingReport = false;
      _clearPending();
      currentView = 'command';
      _popFlowRoutes();
      if (ok && reportLines.isNotEmpty) {
        final body = reportLines.join('\n').trim();
        WidgetsBinding.instance
            .addPostFrameCallback((_) => _showReportPopup(body));
      } else {
        status = text ?? 'the board did not answer';
      }
      notifyListeners();
      return;
    }

    // list_profile finished: the same dedicated-screen treatment as scenes,
    // minus the empty case — there is always at least one person, because
    // the hub cannot be reached at all before somebody has enrolled.
    if (reason == 'menu:profile') {
      _clearPending();
      currentView = 'command';
      _popFlowRoutes();
      if (forBinding) {
        // A silent read for the nameplate -- no screen, no verdict.
      } else if (ok && profileYou != null) {
        status = profileMe.isEmpty ? 'profile' : profileMe;
        openScreen('/profile');
      } else {
        // NO SILENT FALL-THROUGH. Without the frame the screen would draw
        // an empty person with every credential reading "not set" — which
        // is not "we could not ask", it is a lie that looks like an
        // answer.
        status = text ?? 'could not read your profile';
      }
      notifyListeners();
      return;
    }

    // A profile change finished: the screen behind it is now stale, so ask
    // again -- exactly the scenes rule below.
    //
    // GATED ON _profileFlow, NOT on the action name. set_pin, forgot_pin
    // and enroll_voice are the same actions first-run registration and the
    // old Settings rows have always sent, and they have working endings of
    // their own further down. Matching on the name alone would have
    // swallowed those endings — a successful first enrolment would lose its
    // result card and finish in silence, which on this app reads as Done
    // and is the one thing a result must never do by accident.
    //
    // So only a change STARTED from the profile screen is handled here.
    if (fromProfile &&
        (reason == 'menu:rename_user' ||
            reason == 'menu:set_pin' ||
            reason == 'menu:forgot_pin' ||
            reason == 'menu:enroll_voice' ||
            // BOTH were missing (audit, 2026-09-12). enroll_card left the
            // Card row saying "enrolled" after you removed the card, and
            // "not enrolled" in amber after you tapped your first one —
            // for as long as you stayed on the screen. add_person left the
            // household list without the person you had just added.
            reason == 'menu:enroll_card' ||
            reason == 'menu:add_person' ||
            // Removing someone from their row: the list loses them.
            (reason?.startsWith('menu:removeperson:') ?? false))) {
      _clearPending();
      currentView = 'command';
      _popFlowRoutes();
      if (ok) {
        // The re-read re-pushes the screen with the truth on it, so the
        // row that said "not set" a moment ago now says what it is.
        requestProfile();
      } else {
        status = text ?? 'that change did not stick';
      }
      notifyListeners();
      return;
    }

    // A scene was edited, removed or added FROM the list: the list is now
    // stale, so ask for it again -- the result of THAT re-pushes the
    // screen with the truth on it. Exactly the automations rule above,
    // with `add_scene` included because the + lives on the screen now and
    // a new scene that does not appear reads as a failed save.
    if (reason != null &&
        (reason.startsWith('menu:editscene:') ||
            reason.startsWith('menu:dropscene:') ||
            reason == 'menu:add_scene')) {
      _clearPending();
      currentView = 'command';
      _popFlowRoutes();
      if (ok) {
        requestSceneList();
      } else {
        status = text ?? 'that change did not stick';
      }
      notifyListeners();
      return;
    }

    // A saved layout was made, edited, renamed or deleted FROM the list:
    // the list is stale, so ask for it again.
    if (reason != null &&
        (reason.startsWith('menu:editlayout:') ||
            reason.startsWith('menu:renamelayout:') ||
            reason.startsWith('menu:droplayout:') ||
            reason == 'menu:new_layout')) {
      _clearPending();
      currentView = 'command';
      _popFlowRoutes();
      if (ok) {
        requestDeviceList(); // layouts come with the devices
      } else {
        status = text ?? 'that change did not stick';
      }
      notifyListeners();
      return;
    }

    // list_layouts finished (said aloud, say): layouts live in Devices ->
    // Layouts now, so that is the screen that opens. An empty list still
    // opens it, so the New that makes the first one is reachable.
    if (reason == 'menu:list_layouts') {
      _clearPending();
      currentView = 'command';
      _popFlowRoutes();
      if (ok) {
        status = layoutRecords.isNotEmpty
            ? '${layoutRecords.length} saved layout(s)'
            : 'no saved layouts yet';
        openScreen('/devices');
      } else {
        status = text ?? 'could not list saved layouts';
      }
      notifyListeners();
      return;
    }

    // list_scenes finished: same dedicated-screen treatment as devices.
    if (reason == 'menu:list_scenes') {
      _clearPending();
      currentView = 'command';
      _popFlowRoutes();
      if (ok) {
        // NO SCENES IS NOT AN ERROR, and it still opens the screen --
        // otherwise the + button that creates the first one lives behind
        // a list you cannot reach until it is non-empty.
        status = sceneRecords.isNotEmpty
            ? '${sceneRecords.length} scene(s)'
            : 'no scenes yet';
        openScreen('/scenes');
      } else {
        status = text ?? 'could not list scenes';
      }
      notifyListeners();
      return;
    }

    // A device was edited, removed or added FROM the list — the same rule
    // scenes and automations follow. The list is stale, so ask for it
    // again and let THAT result re-push the screen with the truth on it.
    //
    // The board doors are here too: editing a board's wiring renames or
    // renumbers its devices, and change_wifi moves every board at once.
    // Both leave the list out of date in ways that are invisible until you
    // look.
    //
    // "Got it" on a letter (who changed your scene or automation): read,
    // and that is all. No result window -- the notice_gone frame that
    // came before this has already taken the card off the list.
    // A DEVICE USED FROM ITS PAGE: quiet when it worked (the page already
    // shows the new state), said when it did not. The list is asked again
    // either way, so every page shows what the house really is.
    if (reason != null &&
        (reason.startsWith('menu:usedevice:') ||
            reason.startsWith('menu:mainmeter:') ||
            reason.startsWith('menu:forgetdraw:'))) {
      _clearPending();
      currentView = 'command';
      _popFlowRoutes();
      if (!ok) {
        final why = (text ?? '').trim();
        _showToast(why.isEmpty ? 'That did not work.' : why);
      }
      requestDeviceList();
      notifyListeners();
      return;
    }

    if (reason != null && reason.startsWith('menu:seen:')) {
      _clearPending();
      currentView = 'command';
      notifyListeners();
      return;
    }

    // homeflag BELONGS HERE, and its absence was a real bug: the toggle
    // saved on the hub but the rows in hand still carried the old
    // `on_home`, so the switch sprang back to where it was. Tapping it
    // again then flipped the SAVED value back — which is why it looked
    // like the toggle would turn on but never off.
    if (reason != null &&
        (reason.startsWith('menu:editdev:') ||
            reason.startsWith('menu:dropdev:') ||
            reason.startsWith('menu:dropboard:') ||
            // The board sheet's own acts change what the list shows --
            // wiring and name -- so the list is asked again (2026-09-11).
            reason.startsWith('menu:editboard:') ||
            // A saved layout put on a board IS an edit of that board.
            reason.startsWith('menu:uselayout:') ||
            reason.startsWith('menu:boardtype:') ||
            reason.startsWith('menu:renameboard:') ||
            reason.startsWith('menu:homeflag:') ||
            reason == 'menu:add_device' ||
            reason == 'menu:update_device' ||
            reason == 'menu:remove_device' ||
            reason == 'menu:change_wifi')) {
      _clearPending();
      currentView = 'command';
      _popFlowRoutes();
      if (ok) {
        requestDeviceList();
      } else {
        status = text ?? 'that change did not stick';
      }
      notifyListeners();
      return;
    }

    // list_devices finished: open the dedicated Devices screen instead of
    // leaving the rows to scroll past on the home feed.
    if (reason == 'menu:list_devices') {
      _clearPending();
      currentView = 'command';
      _popFlowRoutes();
      if (ok) {
        // AN EMPTY HOUSE STILL OPENS THE SCREEN. The + that registers the
        // first device lives on it, so refusing to open until the list is
        // non-empty puts the only way in behind the thing it creates.
        status = deviceRecords.isNotEmpty
            ? '${deviceRecords.length} device(s)'
            : (boardRecords.isNotEmpty
                ? '${boardRecords.length} board(s)'
                : 'no devices yet');
        openScreen('/devices');
      } else {
        status = text ?? 'could not list devices';
      }
      notifyListeners();
      return;
    }

    // readings finished: open Statistics on the numbers that just arrived.
    //
    // Opened even when the list is EMPTY, which is the difference from
    // list_devices above. "No devices on file" is a fact about a registry
    // the person filled in themselves and can be said in a status line;
    // "nothing here measures anything yet" is a fact about their HOUSE, and
    // it deserves the screen that explains what would change it.
    if (reason == 'menu:readings') {
      _awaitingReadings = false;
      _clearPending();
      currentView = 'command';
      _popFlowRoutes();
      if (ok) {
        status = readingRecords.isEmpty
            ? 'no sensors reporting'
            : '${readingRecords.length} sensor(s)';
        openScreen('/stats');
      } else {
        status = text ?? 'could not read the sensors';
      }
      notifyListeners();
      return;
    }

    if (ok) {
      // The app's own background read (check_notices on every connect)
      // answered nobody: it must not replace "connected" with a "Done".
      if (!_backgroundActions.contains(reason)) status = text ?? 'Done';
      // Flow finished cleanly: a passing TOAST, not a modal — and ONLY
      // for the three flows that change what the house IS (field test
      // 2026-08-25: "the popup is only for successful adding of device
      // ... or removing device and updating device"). Everything else
      // updates the status line and gets out of the way; list flows push
      // their own screen, which IS their outcome. FAILURE keeps its
      // dialog for every menu flow — "silence must never look like
      // Done" was a lesson about failure, and it stands.
      // THE CENTRE DONE WINDOW, and only at a job's TRUE end (the ribbon
      // law, 2026-08-25): the hub now reports checkpoints separately, so
      // an ok result on a job flow means the whole journey finished --
      // flashed, registered, described. That, and nothing earlier, gets
      // the window.
      // An APPROVAL is a job flow whose id names one moment, so it can
      // never be a fixed member of the set above -- but "the front door
      // unlocked" is exactly the kind of ending that must be SEEN, not
      // left in a status line.
      if (reason != null &&
          (_toastedFlows.contains(reason) ||
              reason.startsWith('menu:approve:')) &&
          !handedOff) {
        WidgetsBinding.instance
            .addPostFrameCallback((_) => _showResultPopup(ok: true));
      }
      // A QUERY'S ANSWER, as a card. "Is the light on?" was spoken and
      // then gone; the collected answer lines now also show as a passing
      // card, so the answer can be re-read instead of re-asked. Command
      // results only — menu flows have their own endings above.
      if ((reason == null || !reason.startsWith('menu:')) &&
          !handedOff &&
          _queryAnswerLines.isNotEmpty) {
        final shown = _queryAnswerLines.take(4).join('\n');
        final extra = _queryAnswerLines.length - 4;
        final card = extra > 0 ? '$shown\nand $extra more' : shown;
        WidgetsBinding.instance
            .addPostFrameCallback((_) => _showAnswerToast(card));
      }
    } else if (reason == 'cancelled') {
      // Cancelled → the Cancelled window, centre screen, then HOME (the
      // ribbon law). The work itself was checkpointed by the hub --
      // Delete in Notifications is the only thing that erases it.
      //
      // EXCEPT when the cancel came from the BACK gesture. Back means
      // "step out to the last screen", and the route is already popped;
      // yanking the person home on the hub's confirmation made Back
      // behave exactly like X ("still wrong in the navigation", field
      // test 2026-08-26). A back-cancel ends quietly where they stand;
      // X and the still-there timeout keep the popup-then-home ending.
      status = text ?? 'Cancelled';
      final fromBack = _backCancel;
      _backCancel = false;
      if (!fromBack) {
        final action = msg['action'];
        if (action is String && _toastedFlows.contains(action)) {
          WidgetsBinding.instance
              .addPostFrameCallback((_) => _showCancelPopup());
        }
        // _popFlowRoutes, NOT popUntil(isFirst) (audit, 2026-09-12).
        //
        // sendCancel has already unwound the flow screens by the time
        // this arrives, and the hub's confirmation can land a moment
        // later, after the person has opened something else. popUntil(isFirst) tore that away
        // with no explanation. Unwinding only the FLOW screens leaves
        // whatever they are standing on alone, which is the rule the rest
        // of this class already follows.
        _popFlowRoutes();
      }
    } else {
      // A FAILED menu flow already got its popup (the early branch above,
      // before any per-list handling); what is left here is a failed
      // COMMAND, which speaks its verdict and keeps the status line.
      if (parts != null && parts['not_done'] is List && (parts['not_done'] as List).isNotEmpty) {
        final notDone = (parts['not_done'] as List).join('; ');
        status = text ?? 'Did not do: $notDone';
      } else {
        status = text ?? reason ?? 'Failed';
      }
    }

    _clearPending();
    // Consumed above or not, a result ends the command the lines belonged
    // to -- the next command starts with a clean answer card.
    _queryAnswerLines.clear();
    // FIX: reset the view tracker along with the navigator, otherwise a
    // later view:enroll / record_required is judged "already on enroll" and
    // never pushes the screen again.
    currentView = 'command';
    _popFlowRoutes();
    notifyListeners();
  }

  void _handleError(Map<String, dynamic> msg) {
    settleStarting();
    status = msg['text'] as String? ?? msg['reason'] as String? ?? 'Error';
    // FIX: same concurrency rule as _handleResult — 'server_busy' means
    // ANOTHER connection holds the shell; nothing of OURS ended, so nothing
    // of ours may be torn down -- checked BEFORE anything is.
    if (msg['reason'] == 'server_busy') {
      notifyListeners();
      return;
    }
    // ONE FRAME REFUSED, NO FLOW ENDED. The hub answers a frame it could
    // not handle (malformed_json, bad_frame -- its reader loop), a menu
    // frame without an action, or an oversized challenge clip with an
    // `error` and carries on: the flow that was asking is still waiting
    // on its answer. Tearing everything down here popped the question the
    // hub was still asking. Only a refused request of our own with nothing
    // else running lowers its flags, exactly as the busy results do.
    const frameRefusals = {
      'malformed_json', 'bad_frame', 'bad_menu_frame', 'audio_too_large',
    };
    if (frameRefusals.contains(msg['reason'])) {
      if (!_ourFlowRunning) {
        menuBusy = false;
        _stopCollecting();
        _pendingEditId = null;
        _profileFlow = false;
        _profileForBinding = false;
        profileLoading = false;
      }
      notifyListeners();
      return;
    }
    menuBusy = false;
    // Every per-flow flag ends with the flow (the result path consumes
    // these; an error that ends a flow used to leave them raised).
    _handedOff = false;
    _profileFlow = false;
    _profileForBinding = false;
    profileLoading = false;
    _queryAnswerLines.clear();
    _flowLines.clear();
    _stopCollecting();
    // The pairing chip reports the HUB'S door, which an unrelated error
    // (a malformed frame, an enrolment timeout) does not close. Only the
    // hub itself, via pair_window, or losing the hub, clears it.
    _dismissStillThere(); // same rule as _handleResult: the flow ended
    _backCancel = false; // an error verdict consumes any pending gesture
    _clearPending();
    currentView = 'command'; // FIX: same view-tracker reset as _handleResult
    _popFlowRoutes();
    notifyListeners();
  }

  /// Is a flow of OURS still in progress -- a question on screen, a job
  /// room, a held spinner? Decides whether a refusal of some other request
  /// may lower menuBusy (it may only when nothing of ours is running).
  bool get _ourFlowRunning =>
      _pendingKind != null ||
      busyText != null ||
      _flowRoutes.contains(topRoute);

  /// The list collectors, all off. A collect-flag that fails to clear eats
  /// output forever (see the note above _collectingReport).
  void _stopCollecting() {
    _collectingReport = false;
    _awaitingReadings = false;
  }

  /// True while the `profile` action was sent only to refresh [boundUser]
  /// after login/sign-up -- its result must open no screen and show no
  /// verdict. See _handleView.
  bool _profileForBinding = false;

  /// WHICH QUESTION THE HUB IS WAITING ON, or null -- the one order every
  /// cancel path reads. The hub asks one question at a time, and every
  /// question handler drops the other mirrors (_dropQuestions), so at most
  /// one of these is set. The three cancel paths used to walk them in three
  /// different orders.
  /// A question from the hub is on screen, waiting for an answer.
  bool get questionLive => _pendingKind != null;

  String? get _pendingKind {
    if (pendingRecord != null) return 'record';
    if (challengePhrase != null) return 'challenge';
    if (pendingChoice != null) return 'choice';
    if (pendingPrompt != null) return 'prompt';
    if (pendingForm != null) return 'form';
    if (pendingRegistration != null) return 'registration';
    if (pinMap != null) return 'pin_map';
    if (ruleBuilder != null) return 'automation_builder';
    if (sceneBuilder != null) return 'scene_builder';
    if (pendingFileRequest != null) return 'file_request';
    return null;
  }

  /// Every question mirror, cleared -- except [except]'s own, for a frame
  /// that legitimately replaces itself in place (the registration form
  /// after "Fill in with AI", a newer board layout).
  ///
  /// A NEW QUESTION PROVES THE HUB HAS MOVED PAST ANY EARLIER ONE. Only
  /// the prompt/choice/registration handlers used to say so, and only for
  /// record/challenge; a stale mirror that survived then outranked the
  /// live question in a cancel, which answered with the wrong frame type
  /// -- one the hub discards -- and left it waiting.
  void _dropQuestions({String? except}) {
    if (except != 'record') pendingRecord = null;
    if (except != 'challenge') challengePhrase = null;
    if (except != 'choice') pendingChoice = null;
    if (except != 'prompt') pendingPrompt = null;
    if (except != 'form') pendingForm = null;
    if (except != 'registration') pendingRegistration = null;
    if (except != 'pin_map') pinMap = null;
    if (except != 'automation_builder') ruleBuilder = null;
    if (except != 'scene_builder') sceneBuilder = null;
    if (except != 'file_request') pendingFileRequest = null;
  }

  /// The frame that answers a question of [kind] with a cancel, or null for
  /// a kind this app does not know. [promptValue] rides a cancelled
  /// prompt_reply for a hub that predates the `cancelled` flag (see
  /// _promptCancelValue).
  static Map<String, dynamic>? _cancelFrame(String kind,
      {String promptValue = 'cancel'}) {
    switch (kind) {
      case 'prompt':
        return {'type': 'prompt_reply', 'cancelled': true, 'value': promptValue};
      case 'choice':
        return {'type': 'choice_response', 'cancelled': true,
                'values': <String>[]};
      case 'record':
        return {'type': 'recording', 'cancelled': true};
      case 'challenge':
        return {'type': 'challenge_response', 'cancelled': true};
      case 'form':
        return {'type': 'form_response', 'cancelled': true};
      case 'registration':
        return {'type': 'registration_response', 'action': 'cancel',
                'cancelled': true};
      case 'pin_map':
        return {'type': 'pin_map_response', 'cancelled': true};
      case 'automation_builder':
        return {'type': 'automation_builder_response', 'cancelled': true};
      case 'scene_builder':
        return {'type': 'scene_builder_response', 'cancelled': true};
      case 'file_request':
        return fileResponseCancelled();
    }
    return null;
  }

  void _clearPending() {
    _ceremonyCancelNext = false; // whatever ended the flow outranks it
    _flowCancelNext = false; // ...and so does it for the ordinary cancel
    _pendingEditId = null; // a tapped rule's id must not answer a later picker
    // challengePhrase included: every caller used to clear it by hand
    // beside this, and _handleError and _endTailscaleFlow did not.
    _dropQuestions();
    // A wait cannot outlive the thing it was waiting for. Cleared here as
    // well as on the 'done' frame, because a dropped connection never sends
    // one and a spinner nobody can dismiss is worse than no spinner.
    busyText = null;
    busyDetail = null;
    busySeconds = null;
    choiceHeard = null;
    choiceAwaitingHub = false;
    _choiceTicks.clear();
    pendingWebUrl = null;
  }

  /// The next-step popup: what just finished, what comes next, Continue or
  /// Cancel. Cancel does NOT discard the notice — it stays in the list and
  /// the bell keeps its red dot, which is the whole point: "not now" and
  /// "never" are different answers.
  void _showNextStepPopup(Notice notice) {
    final ctx = navigatorKey.currentContext;
    if (ctx == null) return;
    showDialog<void>(
      context: ctx,
      barrierDismissible: true,
      routeSettings: const RouteSettings(name: _nextStepRouteName),
      builder: (dctx) => Dialog(
        backgroundColor: surface,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(22, 22, 22, 16),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                children: [
                  Container(
                    width: 34,
                    height: 34,
                    decoration:
                        BoxDecoration(shape: BoxShape.circle, color: accent),
                    child: Icon(Icons.arrow_forward, color: accentFg, size: 18),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Text('Next step',
                        style: TextStyle(
                            color: ink,
                            fontSize: 16,
                            fontWeight: FontWeight.w700)),
                  ),
                ],
              ),
              const SizedBox(height: 14),
              Text(notice.title,
                  style: TextStyle(
                      color: ink, fontSize: 14.5, fontWeight: FontWeight.w600)),
              const SizedBox(height: 6),
              Text(notice.body,
                  style: TextStyle(color: inkDim, fontSize: 13, height: 1.45)),
              const SizedBox(height: 18),
              FilledButton(
                style: FilledButton.styleFrom(
                  backgroundColor: accent,
                  foregroundColor: accentFg,
                  padding: const EdgeInsets.symmetric(vertical: 13),
                  shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(12)),
                ),
                onPressed: () {
                  Navigator.of(dctx).pop();
                  runNotice(notice);
                },
                child: Text(
                    notice.actionLabel.isEmpty
                        ? 'Continue'
                        : notice.actionLabel,
                    style: const TextStyle(
                        fontWeight: FontWeight.w700, fontSize: 15)),
              ),
              const SizedBox(height: 8),
              TextButton(
                onPressed: () => Navigator.of(dctx).pop(),
                child: Text('Not now',
                    style: TextStyle(color: inkDim, fontSize: 13.5)),
              ),
            ],
          ),
        ),
      ),
    );
  }

  /// The JOB flows (the ribbon law, 2026-08-25): each is one journey
  /// with one true ending, and only that ending earns the centre Done
  /// window. Everything else finishes into the status line.
  static const _toastedFlows = {
    'menu:add_device',
    'menu:remove_device',
    'menu:update_device',
    'menu:learn_ir',
    'menu:resume_setup',
    // Scenes and automations change what the house DOES — their true
    // endings earn the same window (2026-08-26).
    'menu:add_scene',
    'menu:update_scene',
    'menu:remove_scene',
    'menu:add_automation',
    'menu:update_automation',
    'menu:remove_automation',
  };

  /// Answer lines collected while a command is being worked -- the
  /// "[STATE] ..." rows a query streams back. Consumed by the answer
  /// card at the result, cleared with every result either way.
  final List<String> _queryAnswerLines = [];

  /// The FLOW screens: questions, forms, the pin map -- routes a finished
  /// flow should unwind. Destination screens (settings, devices, stats)
  /// are deliberately absent.
  static const _flowRoutes = {
    '/prompt', '/choice', '/enroll', '/form', '/registration', '/pins', '/web', '/auth',
    '/working', _stillThereRouteName, _nextStepRouteName,
  };

  /// Unwind ONLY the flow screens when a flow ends -- never the screen
  /// the person was standing on. popUntil(isFirst) used to dump every
  /// ending onto home, so Back never returned anywhere ("when back arrow
  /// is pressed last screen should be selected", field test 2026-08-25).
  /// A flow started from Settings now ends back ON Settings, and Back
  /// walks the real history from there.
  void _popFlowRoutes() {
    // BY NAME ONLY. The transient dialogs THIS class opens (still-there,
    // next-step) carry a name in _flowRoutes and unwind with the flow; a
    // dialog somebody is reading (a board's passwords, a report) has none
    // and stops the unwind, as before.
    if (_stillThereShowing) _stillThereQuietDismiss = true; // no still_here
    navigatorKey.currentState?.popUntil((route) =>
        route.isFirst || !_flowRoutes.contains(route.settings.name));
  }

  /// Route names for the dialogs the flow may unwind through.
  static const _stillThereRouteName = '/dialog/still_there';
  static const _nextStepRouteName = '/dialog/next_step';

  /// A tailnet join is a FLOW now, so it has to end like one.
  ///
  /// It used to be one frame out and one frame back, with no screens in
  /// between -- so tailscale_joined/_failed could just show their popup and
  /// stop. Since the hub started asking for a PIN (and, when it is already
  /// on a tailnet, whether to spend the key at all), the answer arrives
  /// with a question screen still standing: without this, the tick lands on
  /// top of the PIN boxes and Back is the only way out of a flow that has
  /// already finished. The 'result' frame does exactly this; these two
  /// endings are results in everything but name.
  void _endTailscaleFlow() {
    _clearPending();
    currentView = 'command';
    _popFlowRoutes();
  }

  /// A query's answer as a passing card: what was just spoken, held on
  /// screen long enough to read twice.
  void _showAnswerToast(String text) {
    final messenger = scaffoldMessengerKey.currentState;
    if (messenger == null) return;
    messenger.clearSnackBars();
    messenger.showSnackBar(SnackBar(
      behavior: SnackBarBehavior.floating,
      duration: const Duration(milliseconds: 3500),
      backgroundColor: surface,
      elevation: 3,
      shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
          side: BorderSide(color: border)),
      margin: const EdgeInsets.fromLTRB(16, 0, 16, 92),
      content: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Icon(Icons.lightbulb_outline, size: 20, color: accent),
        const SizedBox(width: 10),
        Expanded(
            child: Text(text,
                maxLines: 5,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(color: ink, fontSize: 13.5, height: 1.4))),
      ]),
    ));
  }

  // ── "Still there?" (the owner's design, 2026-08-26) ────────────────
  //
  // The hub no longer ejects a quiet screen: after its window it sends a
  // still_there frame, and only STILL_THERE_GRACE of further silence puts
  // the flow away (cancelled path, progress checkpointed). This dialog is
  // that ask. ANY human dismissal -- the button, or a tap outside -- is
  // presence and sends still_here, restarting the hub's window; only a
  // programmatic dismissal (the flow ended underneath it) stays silent.
  bool _stillThereShowing = false;
  bool _stillThereQuietDismiss = false;

  /// THE DIALOG'S OWN ROUTE, so a quiet dismissal removes exactly it --
  /// not whatever happens to be on top (a next-step popup from a broadcast
  /// notice used to be popped in its place, leaving this one standing).
  Route<dynamic>? _stillThereRoute;

  void _handleStillThere(Map<String, dynamic> msg) {
    final grace = (msg['grace'] as num?)?.toDouble() ?? 300.0;
    WidgetsBinding.instance
        .addPostFrameCallback((_) => _showStillThereDialog(grace));
  }

  void _dismissStillThere() {
    if (!_stillThereShowing) return;
    _stillThereQuietDismiss = true;
    final route = _stillThereRoute;
    final nav = navigatorKey.currentState;
    if (route != null && nav != null && route.isActive) {
      nav.removeRoute(route);
    }
    // Settled here as well as in the dialog's own close-out, so the flags
    // are right even if that future settles late.
    _stillThereShowing = false;
    _stillThereRoute = null;
  }

  void _showStillThereDialog(double graceSeconds) {
    final ctx = navigatorKey.currentContext;
    if (ctx == null || _stillThereShowing) return;
    _stillThereShowing = true;
    _stillThereQuietDismiss = false;
    final mins = (graceSeconds / 60).round();
    final graceText = mins >= 2
        ? 'about $mins minutes'
        : '${graceSeconds.round()} seconds';
    showDialog<void>(
      context: ctx,
      barrierDismissible: true,
      routeSettings: const RouteSettings(name: _stillThereRouteName),
      builder: (dctx) {
        _stillThereRoute = ModalRoute.of(dctx);
        return Dialog(
        backgroundColor: surface,
        shape:
            RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(28, 26, 28, 22),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                width: 64,
                height: 64,
                decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    border: Border.all(color: inkDim, width: 2)),
                child:
                    Icon(Icons.hourglass_empty, size: 30, color: inkDim),
              ),
              const SizedBox(height: 14),
              Text('Still there?',
                  style: TextStyle(
                      color: ink,
                      fontSize: 16,
                      fontWeight: FontWeight.w700)),
              const SizedBox(height: 6),
              ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 240),
                child: Text(
                  'This has been waiting a while. In $graceText it will '
                  'be put away safely and you\'ll be back home.',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: inkDim, fontSize: 12.5),
                ),
              ),
              const SizedBox(height: 16),
              FilledButton(
                style: FilledButton.styleFrom(
                  backgroundColor: accent,
                  foregroundColor: accentFg,
                  padding: const EdgeInsets.symmetric(
                      horizontal: 28, vertical: 12),
                  shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(10)),
                ),
                onPressed: () =>
                    Navigator.of(dctx, rootNavigator: true).pop(),
                child: const Text("I'm here"),
              ),
            ],
          ),
        ),
      );
      },
    ).then((_) {
      _stillThereShowing = false;
      _stillThereRoute = null;
      // A quiet dismissal (the flow ended, or the flow unwind took the
      // route) sends nothing; ONLY a human dismissal proves presence.
      if (!_stillThereQuietDismiss) {
        sendJson({'type': 'still_here'});
      }
      _stillThereQuietDismiss = false;
    });
  }

  /// The Cancelled window, centre screen (the ribbon law, 2026-08-25):
  /// a deliberate cancel is acknowledged plainly and without alarm --
  /// neutral outlined circle, no amber -- then it dismisses itself and
  /// the person is already home. The work was checkpointed; Delete in
  /// Notifications is the only eraser.
  void _showCancelPopup() {
    final ctx = navigatorKey.currentContext;
    if (ctx == null) return;
    showDialog<void>(
      context: ctx,
      barrierDismissible: true,
      builder: (dctx) {
        _autoDismiss(dctx, const Duration(milliseconds: 1600));
        return Dialog(
          backgroundColor: surface,
          shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(16)),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(28, 26, 28, 22),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Container(
                  width: 64,
                  height: 64,
                  // The cancel identity (disconnect red), not neutral
                  // grey -- the verdict popup wears the same colour as
                  // the gesture that caused it.
                  decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      border: Border.all(color: cancelInk, width: 2)),
                  child: Icon(Icons.close, size: 30, color: cancelInk),
                ),
                const SizedBox(height: 14),
                Text('Cancelled',
                    style: TextStyle(
                        color: ink,
                        fontSize: 16,
                        fontWeight: FontWeight.w700)),
                const SizedBox(height: 6),
                Text('Your progress is saved in Notifications.',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: inkDim, fontSize: 12.5)),
              ],
            ),
          ),
        );
      },
    );
  }

  /// Success, as a passing toast: a tick and the outcome sentence,
  /// floating clear of the voice dock, gone in under two seconds,
  /// blocking nothing. The messenger belongs to the app, so it lands on
  /// whatever screen is up without owning any of them.

  /// Flow-result popup — FAILURE ONLY since 2026-08-25 (success became
  /// the toast above): an amber circle with "!" and the reason, held
  /// long enough to read. Auto-dismisses; tap anywhere to close.
  /// The verdict window: a tick or an alarm, a headline, and — when there
  /// is one — the sentence that says what actually happened.
  ///
  /// THE MESSAGE USED TO BE DROPPED ON SUCCESS. `if (!ok && message != null)`
  /// meant every successful caller's text was computed, used to size the
  /// dwell, and then never drawn: the window said "Done" for eight seconds
  /// and the words naming the machine went nowhere. That is precisely what
  /// the tailnet popup was asked for on 2026-08-27 ("show it already joined
  /// the network"), so it has been failing that request silently ever since.
  /// A success that has something to say now says it.
  ///
  /// `title` lets a caller name its own outcome. "Done" is right for a
  /// finished command and wrong for "The hub moved tailnet" — where the
  /// headline IS the news, and a generic tick above it buries the one line
  /// that matters.
  /// The board report, held until it is dismissed.
  ///
  /// Monospace and scrollable, because it is column-aligned output from a
  /// chip — firmware version, the drivers that build contains, an I2C scan
  /// — and proportional text turns "0x27  PCF8574" into soup. No timer:
  /// this is the screen you read while holding a multimeter.
  void _showReportPopup(String body, {String title = 'Board report'}) {
    final ctx = navigatorKey.currentContext;
    if (ctx == null) return;
    showDialog<void>(
      context: ctx,
      builder: (dctx) => Dialog(
        backgroundColor: surface,
        shape:
            RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
        child: ConstrainedBox(
          constraints: BoxConstraints(
            maxHeight: MediaQuery.of(dctx).size.height * 0.75,
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 18, 20, 10),
                child: Text(title,
                    style: TextStyle(
                        color: ink,
                        fontSize: 16,
                        fontWeight: FontWeight.w600)),
              ),
              Container(height: 1, color: border),
              Flexible(
                child: SingleChildScrollView(
                  padding: const EdgeInsets.fromLTRB(20, 14, 20, 14),
                  child: SelectableText(
                    body,
                    style: TextStyle(
                        color: inkDim,
                        fontSize: 12.5,
                        height: 1.45,
                        fontFamily: 'monospace'),
                  ),
                ),
              ),
              Container(height: 1, color: border),
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 10, 20, 14),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.end,
                  children: [
                    // COPYABLE, because the next thing anybody does with a
                    // board report is send it to somebody. SelectableText
                    // above covers a careful reader; this covers everyone
                    // else.
                    TextButton(
                      onPressed: () =>
                          Clipboard.setData(ClipboardData(text: body)),
                      child: Text('Copy',
                          style: TextStyle(color: inkDim, fontSize: 13.5)),
                    ),
                    const SizedBox(width: 4),
                    TextButton(
                      onPressed: () =>
                          Navigator.of(dctx, rootNavigator: true).pop(),
                      child: Text('Close',
                          style: TextStyle(color: accent, fontSize: 13.5)),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  /// Close THIS dialog after [after] -- never whatever is on top by then.
  /// A plain pop from the timer (mounted-guarded only) popped the TOP
  /// route, and a dialog stays mounted while something covers it: a
  /// question the next flow pushed during the dwell (a verdict can hold
  /// for eleven seconds) was the route that vanished, leaving the hub
  /// waiting on an answer nothing showed.
  void _autoDismiss(BuildContext dctx, Duration after) {
    final route = ModalRoute.of(dctx);
    Future.delayed(after, () {
      if (!dctx.mounted || route == null || !route.isActive) return;
      if (route.isCurrent) {
        Navigator.of(dctx, rootNavigator: true).pop();
      } else {
        route.navigator?.removeRoute(route);
      }
    });
  }

  void _showResultPopup({required bool ok, String? message, String? title}) {
    final ctx = navigatorKey.currentContext;
    if (ctx == null) return;
    const warn = Color(0xFFD29922);
    showDialog<void>(
      context: ctx,
      barrierDismissible: true,
      builder: (dctx) {
        // HOW LONG IS HOW MUCH THERE IS TO READ. A Done needs a glance;
        // a refusal that names a CAUSE and the way back is a sentence or
        // two, and the flat 3.2s took the Learn IR explanation away at
        // about the third word of the fix ("The IR receiver isn't
        // registered... Add device -> 'Infrared receiver (TSOP38xx)'
        // brings it back" is twenty-six words -- eight seconds of
        // reading). Roughly 200 words a minute, floored so a short
        // refusal is not curt and capped so nothing overstays; tapping
        // still dismisses it early either way.
        final read = '${title ?? ''} ${message ?? ''}'.trim();
        final words =
            read.isEmpty ? 0 : read.split(RegExp(r'\s+')).length;
        final dwell = ok
            // A bare "Done" is a glance; a success that reports a name or
            // a consequence is not. Same rule, lower FLOOR -- but the same
            // ceiling: the 8s cap was set when a success message was never
            // drawn at all, and "the hub moved, every phone needs the new
            // address" is a paragraph that deserves what a refusal gets.
            ? (1200 + words * 260).clamp(1600, 11000)
            : (2200 + words * 300).clamp(3200, 11000);
        _autoDismiss(dctx, Duration(milliseconds: dwell));
        return Dialog(
          backgroundColor: surface,
          shape:
              RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(28, 26, 28, 22),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Container(
                  width: 64,
                  height: 64,
                  // THE THEME'S ACCENT for Done (owner's call 2026-08-26:
                  // "change the colour to the theme, no need for the
                  // green"). Amber stays failure's alone.
                  decoration: BoxDecoration(
                      shape: BoxShape.circle, color: ok ? accent : warn),
                  child: Icon(ok ? Icons.check : Icons.priority_high,
                      color: ok ? accentFg : Colors.white, size: 32),
                ),
                const SizedBox(height: 14),
                Text(title ?? (ok ? 'Done' : 'Not done'),
                    textAlign: TextAlign.center,
                    style: TextStyle(
                        color: ink,
                        fontSize: 16,
                        fontWeight: FontWeight.w700)),
                if (message != null && message.trim().isNotEmpty) ...[
                  const SizedBox(height: 8),
                  // BOUNDED AND SCROLLABLE. A refusal is a sentence; a
                  // tailnet switch is a paragraph, and a Column that just
                  // grows would run off a small phone into the yellow
                  // overflow stripe. It scrolls instead, and the dwell is
                  // already long enough to read it.
                  ConstrainedBox(
                    constraints:
                        const BoxConstraints(maxWidth: 260, maxHeight: 300),
                    child: SingleChildScrollView(
                      child: Text(
                        message,
                        textAlign: TextAlign.center,
                        style: TextStyle(
                            color: inkDim, fontSize: 13.5, height: 1.4),
                      ),
                    ),
                  ),
                ],
              ],
            ),
          ),
        );
      },
    );
  }

  void settleStarting() {
    isStarting = false;
    startTimer?.cancel();
    startTimer = null;
  }

  // ── User actions ───────────────────────────────────────────────────

  /// When to dial again, and the remote-access notes (connection_watch.dart).
  final ConnectionWatch _watch = ConnectionWatch();

  /// Add the hub's tailnet address to the candidate list, once.
  ///
  /// The whole method is "append if missing". It does not reorder, does
  /// not remove, and does not change which address is in use — so the
  /// worst it can do is add one entry at the END of a list the app only
  /// reaches after everything before it has failed. Removing a stale name
  /// stays where it belongs: the `switched` branch of `tailscale_joined`,
  /// which is the one place that KNOWS an address is dead.
  void _learnTailnet(String name) {
    if (name.isEmpty || _socket == null) return;
    final url = 'wss://$name:8765';
    if (_socket!.candidates.contains(url)) return;
    _socket!.candidates.add(url);
    SharedPreferences.getInstance()
        .then((p) => p.setStringList(_hubKey('candidates'), _socket!.candidates));
  }

  /// Add the hub's tailnet ADDRESS (100.64.0.0/10) to the candidate list,
  /// once -- filed just AHEAD of the hub's tailnet name when that is in
  /// the list, else at the end.
  ///
  /// Why it exists (2026-09-09): the hub told the phone its .ts.net NAME
  /// on every connect and nothing else, and on the first real phone the
  /// name was the road that failed -- Dart's resolver did not answer for
  /// MagicDNS and Android's root store did not trust the Let's Encrypt
  /// chain -- while the IP, pinned against local.crt exactly like the
  /// home Wi-Fi, worked. So the IP was typed into Server addresses by
  /// hand, and typed again for the Windows bench, whose address differs;
  /// "when remote access is done shouldn't that update the list?" Now the
  /// hub sends the address on every `auth_ok` and in `tailscale_joined`,
  /// and this files it. Ahead of the name because the name is the fragile
  /// road and should be the last thing tried; behind the local roads for
  /// the same reason `_learnTailnet` never reorders -- an address that
  /// only answers through the tailnet must not cost the hotspot and the
  /// LAN their turn. `rememberAddress` still promotes it to the front the
  /// moment it is the one that answered, like any other IP.
  ///
  /// Refuses anything that is not an IPv4 literal in Tailscale's block:
  /// a hub bug or a hostile hub must not be able to put an arbitrary name
  /// into the dial order through this door. (A name goes through
  /// `_learnTailnet` and is checked against the phone's root store at
  /// dial time; an IP filed here is pinned against local.crt at dial
  /// time like every other IP, so a wrong one simply never connects.)
  void _learnTailnetIp(String ip) {
    if (ip.isEmpty || _socket == null) return;
    if (!SocketClient.isTailnetIp(ip)) return;
    final url = 'wss://$ip:8765';
    final list = _socket!.candidates;
    if (list.contains(url)) return;
    // Ahead of the first NAME in the list (the tailnet's .ts.net entry --
    // every IP, local or tailnet, stays where it is), else last.
    var at = list.length;
    for (var i = 0; i < list.length; i++) {
      final h = Uri.tryParse(list[i])?.host ?? '';
      if (h.isNotEmpty && InternetAddress.tryParse(h) == null) {
        at = i;
        break;
      }
    }
    list.insert(at, url);
    SharedPreferences.getInstance()
        .then((p) => p.setStringList(_hubKey('candidates'), list));
  }

  /// Put `url` at the FRONT of the candidate list and persist it: the
  /// address the phone just paired on, or just connected on. The opposite
  /// of _learnTailnet's "append, never reorder" -- and deliberately so:
  /// that guard protects against a NAME that may not resolve costing the
  /// local roads their turn; this is an ADDRESS that answered a moment ago.
  /// A hub on a house router lives at an address the compiled-in list has
  /// never heard of, and this is how the phone learns it without typing.
  /// The two hotspot addresses that used to be built into every list: taken
  /// out ONCE per hub, except the first entry (the address that last
  /// answered -- rememberAddress puts it there). A laptop hub keeps its
  /// 192.168.137.1 and stops dialling the Pi's 192.168.4.1.
  Future<void> _dropLegacyDefaults(
      SharedPreferences prefs, List<String> list) async {
    final flag = _hubKey('legacy_defaults_dropped');
    if (prefs.getBool(flag) == true) return;
    final keep = list.isEmpty ? null : list.first;
    list.removeWhere(
        (u) => u != keep && AppConfig.legacyDefaults.contains(u));
    await prefs.setStringList(_hubKey('candidates'), list);
    await prefs.setBool(flag, true);
  }

  void rememberAddress(String url) {
    final u = Uri.tryParse(url.trim());
    if (u == null || !u.isScheme('wss') || u.host.isEmpty) return;
    final clean = u.toString();
    final list = _socket?.candidates;
    if (list == null) {
      SharedPreferences.getInstance().then((p) {
        final stored = [
          ...(p.getStringList(_hubKey('candidates')) ?? AppConfig.defaultCandidates)
        ]..remove(clean);
        p.setStringList(_hubKey('candidates'), [clean, ...stored]);
      });
      return;
    }
    if (list.isNotEmpty && list.first == clean) return;
    list
      ..remove(clean)
      ..insert(0, clean);
    SharedPreferences.getInstance()
        .then((p) => p.setStringList(_hubKey('candidates'), list));
  }

  /// Persist a new candidate list and hand it to the live SocketClient.
  /// This is what makes the server address USER-settable: candidates were
  /// only ever defaults because nothing wrote the per-hub candidates pref.
  /// Takes effect on the next connect (the probe loop reads the list fresh).
  /// Returns TRUE when the list was actually written. The dialog needs to
  /// know: it used to call this without awaiting and pop unconditionally on
  /// the next line, so emptying the box and pressing Save closed like a
  /// success and changed nothing — the `status` set below renders on the
  /// home screen, two routes underneath (audit, 2026-09-12).
  Future<bool> saveCandidates(List<String> urls) async {
    // wss:// ONLY, defensively -- the dialog validates too, but a stored
    // ws:// line would be dialled in plaintext and the pin never consulted
    // (app audit, 2026-09-02). socket_client refuses such a URL as well.
    final cleaned = [
      for (final u in urls)
        if (u.trim().isNotEmpty &&
            Uri.tryParse(u.trim())?.scheme == 'wss' &&
            (Uri.tryParse(u.trim())?.host ?? '').isNotEmpty)
          u.trim()
    ];
    if (cleaned.isEmpty) {
      status = 'no valid wss:// address to save';
      notifyListeners();
      return false;
    }
    final prefs = await SharedPreferences.getInstance();
    await prefs.setStringList(_hubKey('candidates'), cleaned);
    final live = _socket?.candidates;
    if (live != null) {
      live
        ..clear()
        ..addAll(cleaned);
    }
    status = 'server addresses updated';
    notifyListeners();
    return true;
  }

  // ── Hubs: every hub this phone is paired with ───────────────────────
  //
  // MANY HUBS, ONE PHONE (2026-09-24). Pairing another hub ADDS it; the
  // app talks to one at a time (the active hub) and switches on a tap.
  // What belongs to a hub is kept per hub, under keys ending @<its id>:
  // its addresses (a tailnet name is one machine's), its notices. What
  // belongs to the phone -- the keypair, the theme, the wake engine -- is
  // shared.

  /// The hub in use ('' when none) and every paired hub, in pairing order.
  String activeHubId = '';
  List<HubEntry> hubList = [];

  /// The active hub's name for the screen ('' when none).
  String get activeHubLabel {
    for (final h in hubList) {
      if (h.id == activeHubId) return h.label;
    }
    return '';
  }

  /// A preference kept per hub: `base@<active hub id>`, or `base` itself
  /// before any hub is paired (the one-hub layout, moved on first load).
  String _hubKey(String base) =>
      activeHubId.isEmpty ? base : '$base@$activeHubId';

  static const _perHubPrefs = ['candidates', 'notices', 'notices_seen'];

  Future<void> _loadHubs() async {
    hubList = await identity.hubs();
    activeHubId = await identity.activeHubId() ?? '';
    if (activeHubId.isEmpty) return;
    // The one-hub layout kept these under their bare names. They belong to
    // the hub this phone was paired with, so they move to its keys once.
    final prefs = await SharedPreferences.getInstance();
    for (final base in _perHubPrefs) {
      final old = prefs.getStringList(base);
      if (old == null) continue;
      if (prefs.getStringList('$base@$activeHubId') == null) {
        await prefs.setStringList('$base@$activeHubId', old);
      }
      await prefs.remove(base);
    }
  }

  /// Everything the app holds about the hub it was talking to: who you
  /// are there, its devices, boards, scenes, readings, automations,
  /// reports and notices. Cleared when the hub changes or goes, so one
  /// house's devices are never shown as another's.
  void _clearHubState() {
    boundUser = '';
    // The next hub says its own Tailscale name on connect.
    hubTailnet = '';
    hubTailnetIp = '';
    hubTailnetAccount = '';
    hubTailnetState = '';
    _closeTailscaleWaiting();
    _hideFloat();
    profileYou = null;
    profilePhone = null;
    profileMe = '';
    profilePeople.clear();
    profilePhones.clear(); // the old hub's handsets, as the people above
    deviceRecords.clear();
    boardRecords.clear();
    sceneRecords.clear();
    layoutRecords.clear();
    readingRecords.clear();
    homeReadings.clear();
    automationRecords.clear();
    automationJournal.clear();
    reportLines.clear();
    notices.clear();
    _seenNoticeIds.clear();
    keepsafeMessage = null;
  }

  /// Called by the pairing screen once pairing.dart has verified every
  /// step. Stores what pairing produced, makes that hub the active one,
  /// then boots normally. `address` is where it answered: it goes first
  /// in THAT hub's address list.
  ///
  /// The API key the user typed is NOT stored. It was spent proving this
  /// phone may register a key; from here the keypair does the work. So the
  /// key exists in exactly two places -- the hub's environment and whatever
  /// its owner wrote it on -- and never in an app.
  Future<void> completePairing({
    required String phoneId,
    required String certificatePem,
    String hubName = '',
    String address = '',
  }) async {
    final previous = activeHubId;
    final id = await identity.savePairing(
      phoneId: phoneId,
      certificatePem: certificatePem,
      hubName: hubName,
    );
    final added = !hubList.any((h) => h.id == id);
    await _loadHubs();
    if (previous.isNotEmpty && previous != id) {
      // A DIFFERENT HUB: the old one's session and everything shown from
      // it go now, before anything of the new one is read or written.
      await _stopPipeline();
      _endConnectionFlow();
      _clearHubState();
      await _loadNotices();
    }
    final u = Uri.tryParse(address.trim());
    if (u != null && u.isScheme('wss') && u.host.isNotEmpty) {
      final prefs = await SharedPreferences.getInstance();
      final stored = [
        ...(prefs.getStringList(_hubKey('candidates')) ??
            AppConfig.defaultCandidates)
      ]..remove(u.toString());
      await prefs.setStringList(_hubKey('candidates'), [u.toString(), ...stored]);
    }
    // Drop the cached pin so the new certificate is used without a restart.
    _socket?.forgetPin();
    blockingProblem = null;
    needPairing = false;
    status = 'paired -- connecting…';
    // Announce it HERE too. The "X has joined" banner rides a broadcast
    // that this phone, mid-pairing, was not yet connected to receive --
    // so the very phone that paired saw nothing (field test 2026-08-25).
    final name = activeHubLabel;
    WidgetsBinding.instance.addPostFrameCallback((_) => _showDropBanner(
        added && hubList.length > 1
            ? 'Paired with $name — added to your hubs, and now in use.'
            : 'Paired — this phone is now trusted by the hub.'));
    notifyListeners();
    _boot();
  }

  /// Talk to another paired hub. Its addresses, notices and certificate
  /// take over; nothing of the one being left is kept on screen. The
  /// hubs' own records are untouched -- switching back needs no pairing.
  Future<void> switchHub(String id) async {
    if (id == activeHubId || !await identity.setActiveHub(id)) return;
    await _stopPipeline();
    _endConnectionFlow();
    _clearHubState();
    await _loadHubs();
    await _loadNotices();
    navigatorKey.currentState?.popUntil((route) => route.isFirst);
    blockingProblem = null;
    needPairing = false;
    status = 'switching to $activeHubLabel…';
    notifyListeners();
    _boot();
  }

  /// Forget a hub this phone is NOT using now: its entry, its addresses,
  /// its notices. The session with the active hub carries on. (The active
  /// hub goes through unpairPhone, which also ends its session.)
  Future<void> forgetOtherHub(String id) async {
    if (id == activeHubId) return;
    await identity.forgetHub(id);
    final prefs = await SharedPreferences.getInstance();
    for (final base in _perHubPrefs) {
      await prefs.remove('$base@$id');
    }
    await _loadHubs();
    status = 'hub forgotten';
    notifyListeners();
  }

  /// Unpair this phone from the ACTIVE hub. Keeps the keypair (see
  /// PhoneIdentity.forgetHub) so pairing again -- with this hub or another
  /// -- reuses the same identity.
  ///
  /// EVERYTHING ELSE ABOUT THAT HUB GOES: the live session (an unpaired
  /// phone must not stay authenticated), its notices, its addresses
  /// (including the learned tailnet name), whose phone it was, and the
  /// feed. `forgetHub` used to drop the keys and leave all of it running.
  ///
  /// Returns true when another paired hub took over (the app is already
  /// connecting to it), false when no hub is left and the phone must pair.
  Future<bool> unpairPhone() async {
    final gone = activeHubId;
    await identity.forgetHub(gone.isEmpty ? null : gone);
    _socket?.forgetPin();
    await _stopPipeline();
    _endConnectionFlow();
    _clearHubState();
    final prefs = await SharedPreferences.getInstance();
    for (final base in _perHubPrefs) {
      await prefs.remove(gone.isEmpty ? base : '$base@$gone');
    }
    await _loadHubs();
    navigatorKey.currentState?.popUntil((route) => route.isFirst);
    if (activeHubId.isNotEmpty) {
      await _loadNotices();
      needPairing = false;
      status = 'unpaired -- now using $activeHubLabel';
      notifyListeners();
      _boot();
      return true;
    }
    await _stopForegroundService();
    unpaired = true;
    _pairLater = false;
    needPairing = true;
    status = 'this phone is no longer paired';
    notifyListeners();
    return false;
  }

  /// The old name, kept for any caller: the full unpair.
  Future<void> forgetHub() => unpairPhone();

  // FIX: ConnState enum has no 'connecting'; use the values SocketClient
  // actually emits (idle, probing, authenticating, ready, closed).
  // We also stop manually overwriting connState — the states listener
  // above keeps it in sync with SocketClient automatically.
  Future<void> toggleConnection() async {
    // No hub to connect to: whatever asked, the answer is the pairing card.
    if (unpaired && connState != ConnState.ready) {
      needPairing = true;
      notifyListeners();
      return;
    }
    if (connState == ConnState.ready) {
      await _socket?.close();
      status = 'disconnected';
      // Full reset, like the browser client's teardown(): leaving challenge/
      // starting set meant a reconnect inherited a locked UI. (menuBusy was
      // missing from it, and stayed raised across the reconnect.)
      _endConnectionFlow();
      navigatorKey.currentState?.popUntil((route) => route.isFirst);
    } else {
      status = 'connecting…';
      notifyListeners();
      final sock = _socket;
      final url = await sock?.connect();
      if (url == null) {
        // Not a status-line shrug: HomeScreen turns this flag into a help
        // card naming the three real causes (Wi-Fi, hub power, Tailscale).
        // But null also means SUPERSEDED -- the resume redial (_resumeMic)
        // took this sweep over and may well be connecting. Only a sweep
        // that really ended, with the socket reporting `closed`, failed.
        if (sock == null || sock.state == ConnState.closed) {
          connectFailed = true;
          status = 'could not reach the hub';
        }
      } else {
        status = 'connected via $url';
      }
    }
    notifyListeners();
  }

  void sendPromptReply(String value) {
    // FIX (double-tap → double-pop → HOME popped → dead black UI): this was
    // not idempotent. A fast double-tap ran it twice; the second call found
    // pendingPrompt already null but STILL popped a route — with the prompt
    // screen already gone, that pop removed the HOME route, leaving an
    // empty navigator nothing on screen could recover. Now: second call is
    // a no-op, and the pop only fires while the prompt screen is actually
    // on top.
    if (pendingPrompt == null) return;
    // Close VoiceFlow's open voice-capture window for the same question, or
    // the mic capture finishes later and sends a SECOND prompt_reply the
    // server treats as the answer to its NEXT question.
    _flow?.abortCapture();
    sendJson({'type': 'prompt_reply', 'value': value});
    pendingPrompt = null;
    // Fresh banner for the NEXT question: lines written before this answer
    // belonged to the question just closed, not the one coming.
    _flowLines.clear();
    // The screen stays put while the flow is busy (spinner-in-place); it
    // pops itself only when the flow ends. Popping here bounced the person
    // to home for every gap between questions.
    if (topRoute == '/prompt' && !menuBusy) {
      navigatorKey.currentState?.pop();
    }
    notifyListeners();
  }

  void sendChoiceResponse(List<String> values, {bool cancelled = false}) {
    // FIX: same idempotence + targeted-pop as sendPromptReply — a double-tap
    // on a picker option (Update device's list, any Manage flow) was the
    // reproducible path to the popped-home black screen.
    if (pendingChoice == null) return;
    _flow?.abortCapture();
    sendJson({
      'type': 'choice_response',
      if (cancelled) 'cancelled': true,
      'values': values,
      // Diagnostic only (the hub logs how a list was answered). A SPOKEN
      // answer never comes through here: it is words, sent by
      // sendChoiceHeard with `via: voice`.
      'via': 'screen',
    });
    _choiceTicks.clear();
    pendingChoice = null;
    dockMuted = false; // the mute belonged to that question, now answered
    _flowLines.clear(); // same fresh-banner rule as sendPromptReply
    // Same hold-in-place rule as sendPromptReply: mid-flow, the screen
    // shows the working spinner instead of flashing home between steps.
    if (topRoute == '/choice' && !menuBusy) {
      navigatorKey.currentState?.pop();
    }
    notifyListeners();
  }

  void sendCancel() {
    _flow?.abortCapture(); // FIX: cancel must close the voice window too
    final kind = _pendingKind;
    if (kind != null) {
      // Every kind of question answered with its own cancel frame -- a
      // kind missing here (registration once was) sends nothing and
      // leaves the hub holding the shell lock. A prompt's VALUE rides
      // along for a hub that predates the `cancelled` flag: see
      // _promptCancelValue.
      sendJson(_cancelFrame(kind, promptValue: _promptCancelValue())!);
    } else {
      // NOTHING PENDING: the hub is busy thinking (or already done). TELL
      // THE HUB (2026-09-30: "it's meant to send the cancel and then stop
      // it immediately in the hub"). It stops the running flow at its next
      // step -- before any device is touched -- and answers `cancel_ack`
      // at once, so this never sits on "cancelling…" waiting for a
      // question that will not come. (It used to be ARMED here instead,
      // which hung whenever the hub had nothing more to ask.)
      sendJson({'type': 'cancel'});
      status = 'cancelling…';
    }
    _backCancel = false; // X is the eject seat -- its cancel ends loudly
    _dismissStillThere(); // the flow is being abandoned -- no still_here
    _clearPending();
    _flowCancelNext = false; // the hub stops itself now -- nothing armed
    currentView = 'command'; // FIX: keep the view tracker honest after cancel
    settleStarting();
    // CANCEL GOES BACK TO WHERE YOU STARTED (2026-09-30: cancelling
    // Recovery words dropped her on Home instead of Profile). The flow's
    // own screens unwind -- exactly what a finished flow does -- and the
    // screen the flow was opened from stays. Opened from Home, that is
    // still Home.
    _popFlowRoutes();
    notifyListeners();
  }

  /// The ceremony room's cancel — tells the hub, STAYS in the room.
  ///
  /// sendCancel() is the eject seat: it pops to home instantly, which is
  /// exactly what the field test caught ("when i cancelled, it took me to
  /// the home screen — it should just take me back to the beginning of
  /// enrollment", 2026-08-27). Registration's cancel means something
  /// different: the hub rolls the ceremony back (its one-transaction law)
  /// and RE-ASKS the name question, which renders right here in this same
  /// room. So nothing pops. If the hub instead ENDS the ceremony (a cancel
  /// at the untouched first question), its view frame decides where the
  /// app goes — command → home, enroll → the room stays held.
  void sendCeremonyCancel() {
    _flow?.abortCapture();
    final kind = _pendingKind;
    if (kind != null) {
      // Any kind, not only record/choice/prompt: one it did not name (a
      // voice challenge) used to be ARMED instead, and the hub sat waiting
      // on the answer the armed cancel was saving for the next question.
      sendJson(_cancelFrame(kind, promptValue: _promptCancelValue())!);
    } else {
      // NOTHING PENDING — the spinner moment: a recording just went off,
      // or the hub is building the voiceprint. The first build sent
      // nothing here, so Cancel silently did nothing and the hub sat
      // waiting (field test 2026-08-27, "when cancel was pressed it
      // didn't go to the enroll screen"). There is no frame that can
      // interrupt the hub mid-thought, so the cancel is ARMED instead:
      // the hub's very next question is answered with a cancel the
      // moment it arrives, which triggers the rollback-and-restart.
      _ceremonyCancelNext = true;
      status = 'cancelling…';
    }
    _dropQuestions();
    _backCancel = true; // should a cancelled result arrive, it ends quietly
    settleStarting();
    notifyListeners();
  }

  /// Armed by a ceremony cancel that found nothing to cancel YET (the hub
  /// was between questions). Consumed by the next prompt/choice/record.
  /// Cleared with the rest of the pending state — a flow ending clears it.
  bool _ceremonyCancelNext = false;

  /// The same arming, for the ORDINARY cancel (sendCancel) — the working
  /// screen's button, which fires while the hub is busy and nothing is
  /// pending. Kept separate from _ceremonyCancelNext on purpose: that one
  /// carries the registration exemption (a cancel that lands on the
  /// restarted name question has already got what it wanted, so it lets
  /// that one through). An ordinary cancel means stop, whatever is asked
  /// next, so it has no exemption -- and it answers MORE kinds of
  /// question, because a busy hub can come back with any of them.
  ///
  /// Cleared in _clearPending like its twin: an arming cannot outlive the
  /// flow it was aimed at, or it would cancel something unrelated minutes
  /// later.
  bool _flowCancelNext = false;

  /// Answer an armed ordinary cancel, if one is waiting. Returns true when
  /// the frame has been answered and the caller must not go on to draw it.
  bool _takeFlowCancel(String frameType) {
    if (!_flowCancelNext) return false;
    // Every kind _cancelFrame knows, the pin map and rule builder included:
    // an arm that skipped a kind would survive past it and land on some
    // later question instead -- it answers the NEXT question, whatever kind
    // it turns out to be.
    final frame = _cancelFrame(frameType);
    if (frame == null) return false;
    _flowCancelNext = false;
    sendJson(frame);
    status = 'cancelled';
    notifyListeners();
    return true;
  }

  /// List devices via its own screen: collect the streamed rows, then the
  /// result frame (reason "menu:list_devices") pushes /devices.
  void requestDeviceList() {
    deviceRecords.clear();
    boardRecords.clear();
    sendMenuAction('list_devices');
  }

  /// The acts on ONE device, from the row it belongs to — the third and
  /// last of the merges, after automations and scenes.
  ///
  /// Named by the device id, which is its key on the hub, and the hub looks
  /// it up rather than trusting it. Remove runs the hub's FULL removal:
  /// the scenes warning, the Home Assistant sibling check, the strip from
  /// the chip, the offered HA delete. It is a shortcut through the picker,
  /// not around any of that.
  void editDevice(String id) => sendMenuAction('editdev:$id');

  void removeDevice(String id) => sendMenuAction('dropdev:$id');

  /// Put one device's readings on the home screen, or take them off.
  ///
  /// NO PIN, and it is the only device action without one. Editing renames
  /// a thing, moves it between rooms or marks it security-critical, so it
  /// is gated. This changes a preference about your own screen — it opens
  /// nothing and reveals nothing, and asking for a PIN to hide a
  /// temperature teaches people to type it without reading the prompt.
  /// Her ruling, 2026-09-01.
  void toggleOnHome(String id) {
    // FLIP IT HERE FIRST, then ask the hub.
    //
    // The hub is the authority and the refresh that follows is what
    // settles it — but a round trip is a menu action, a save, and a whole
    // device list rebuilt, and until it came back the switch sat where it
    // was. It read as a control that had not noticed the tap. Her report,
    // 2026-09-02: "the toggle works now but its slow".
    //
    // Safe to do optimistically because the flag has exactly two states
    // and the hub computes the same flip from the same stored value: if
    // the action fails, the refresh puts it straight back, and if the app
    // is out of date the refresh corrects it either way. Nothing is
    // decided here that is not re-decided there.
    void flip() {
      for (final r in deviceRecords) {
        if ((r['id'] ?? '').toString() != id) continue;
        r['on_home'] = r['on_home'] == false;
      }
      notifyListeners();
    }

    flip();
    // Nothing was sent (no connection): no refresh will come to put it
    // back, so undo the flip here.
    if (!sendMenuAction('homeflag:$id')) flip();
  }

  void addDevice() => sendMenuAction('add_device');

  void changeBoardWifi() => sendMenuAction('change_wifi');

  // `retireBoard()` -- the picker version -- is GONE with the menu row it
  // served. It sent `remove_device` and let the hub ask which board, which
  // is exactly the question the bin on the card answers by being tapped.
  // Voice still reaches the same place: "remove a device" opens the hub's
  // own "one device / a whole board" fork, which is where that flow lives
  // and where it stays.

  /// Retire THE BOARD UNDER YOUR FINGER — no picker, because the tap
  /// already said which one (2026-09-01: "remove icon beside board, not on
  /// the top three dots").
  ///
  /// Gated on the hub exactly as `remove_device` is, and the hub still
  /// lists every device, entry and file it is about to delete before it
  /// deletes any of them. The icon is a shortcut through the question
  /// "which board?", not around the one that matters.
  void retireOneBoard(String slug) => sendMenuAction('dropboard:$slug');

  // THE BOARD'S OWN SHEET (her ruling, 2026-09-11: "when boards are
  // clicked in devices, info about them is shown, its credentials gated
  // by pin, its drivers used and the devices connected"). Tapping a board
  // card opens everything about that board in one place, and each act
  // below names the board under the finger -- no picker asks a question
  // the tap already answered. The hub gates each exactly as the picker
  // version is gated: passwords as `show_esp_key`, wiring as
  // `update_device`; rename and the report are ungated there too.

  /// This board's passwords: web page (user admin) and broker. PIN on the
  /// hub. Replaces the "Board credentials" row Settings used to carry.
  void boardPasswords(String slug) => sendMenuAction('boardkey:$slug');

  /// "Ask it how it is" for THIS board -- firmware, the drivers its build
  /// offers, the I2C scan and live readings, over the air.
  void boardReportFor(String slug) {
    _collectingReport = true;
    reportLines.clear();
    sendMenuAction('boardreport:$slug');
  }

  /// Check one card reader: its board online, the firmware's driver, its
  /// pins, then a real tap. The hub's step lines are shown in a dialog.
  void checkReader(String id) {
    _collectingReport = true;
    reportLines.clear();
    if (!sendMenuAction('checkreader:$id')) _collectingReport = false;
  }

  /// The pin screen for this board, prefilled; the new template goes out
  /// over the broker and the rewire question follows.
  void editBoard(String slug) => sendMenuAction('editboard:$slug');

  void renameBoard(String slug) => sendMenuAction('renameboard:$slug');

  /// Say this board is a different kind of board (chosen wrong).
  void changeBoardType(String slug) => sendMenuAction('boardtype:$slug');

  /// Readings via their own screen, mirroring requestDeviceList: ask, then
  /// the result frame (reason "menu:readings") opens /stats.
  ///
  /// The old list is NOT cleared here. The screen opens on arrival, not on
  /// the request, so clearing now would only blank a screen nobody is
  /// looking at -- and if the hub refuses the request, the last known
  /// readings are better than an empty screen that implies the house stopped
  /// measuring.
  void requestReadings() {
    _awaitingReadings = true;
    sendMenuAction('readings');
  }

  /// Scenes via their own screen, mirroring requestDeviceList.
  void requestSceneList() {
    sceneRecords.clear();
    sendMenuAction('list_scenes');
  }

  /// The three small acts on ONE scene, from the row it belongs to.
  ///
  /// Named by the scene's own name, which is its key on the hub — and the
  /// hub LOOKS IT UP rather than trusting it, so a stale list edits or
  /// removes nothing rather than something else. `add` needs no argument:
  /// it is the existing wizard, started from the + button instead of a
  /// drawer row.
  ///
  /// All three are gated on the hub exactly as the four menu actions they
  /// replace were, so the PIN badge on the screen is the truth and not a
  /// decoration.
  void editScene(String name) => sendMenuAction('editscene:$name');

  void removeScene(String name) => sendMenuAction('dropscene:$name');

  /// Take someone else out of the house. The hub confirms, asks for the
  /// remover's PIN, then asks what becomes of what they made.
  void removePerson(String user) {
    _profileFlow = true;          // its result re-reads the household
    sendMenuAction('removeperson:$user');
  }

  /// Leave this house yourself: "keep" (devices and settings stay) or,
  /// for the last person, "clear" (the whole hub, as new). The hub asks
  /// for the PIN; when done it says `left_house` and this phone forgets it.
  void leaveHouse(String how) {
    _profileFlow = true;
    sendMenuAction('leavehouse:$how');
  }

  /// The hub says this person left (or cleared it): forget the hub here.
  Future<void> _leftHouse(bool cleared) async {
    await unpairPhone();
    _showToast(
        cleared
            ? 'The hub is cleared. It restarts as new.'
            : 'You left the house.');
  }

  void addScene() => sendMenuAction('add_scene');

  // ── Saved layouts ──────────────────────────────────────────────────
  //
  // A layout is a wiring plan for one chip family, kept under a name. It
  // controls nothing until it is put on a board, and the hub never deletes
  // one -- not after a flash, not after an edit. Only `removeLayout` does,
  // and the screen asks "are you sure" first.

  /// Layouts arrive WITH the devices and boards (one frame, one truth), so
  /// asking for them is asking for the Devices list.
  void requestLayoutList() => requestDeviceList();

  /// Design a new layout, or copy a board's current wiring.
  void newLayout() => sendMenuAction('new_layout');

  /// The pin screen on this layout; saving updates it.
  void editLayout(String id) => sendMenuAction('editlayout:$id');

  void renameLayout(String id) => sendMenuAction('renamelayout:$id');

  void removeLayout(String id) => sendMenuAction('droplayout:$id');

  /// Put this layout on one of your boards of the same chip. Opens the
  /// pin screen on it; nothing changes on the board until you save.
  /// Gated on the hub like any edit of a board.
  void useLayout(String id) => sendMenuAction('uselayout:$id');

  /// Who you are, from the hub, every time the screen is opened.
  ///
  /// Never cached across a visit. A profile is the one screen where a stale
  /// answer is actively dangerous: "PIN: set" drawn from a snapshot taken
  /// before somebody removed it is a security claim about the past.
  /// True while a profile read is in flight. The screen dims itself and
  /// says it is checking, instead of the old behaviour: this method used to
  /// NULL every field, which made the screen draw "The hub did not send
  /// your profile, so nothing here can be trusted to be current" for the
  /// length of a round trip — after a change that had just SUCCEEDED
  /// (audit, 2026-09-12). A success rendered as a failure.
  ///
  /// Nulling was not paranoia for its own sake: a stale "PIN: set" is a
  /// security claim about the past. Marking the rows as being re-checked
  /// says the same thing honestly, and the data is replaced wholesale the
  /// moment the frame lands.
  bool profileLoading = false;

  void requestProfile() {
    profileLoading = true;
    sendMenuAction('profile');
  }

  /// The four changes the Profile screen offers, and the one it borrows.
  ///
  /// Each is the SAME hub action the old Settings row or drawer row sent —
  /// `set_pin`, `forgot_pin`, `enroll_voice` — so the gate that guarded them
  /// there guards them here, unchanged. Only the door moved. `rename_user`
  /// is the one genuinely new action, and it is the most heavily guarded
  /// thing in the hub's table: a name is a key in five stores.
  ///
  /// [reenrollVoice] and [addPerson] send DIFFERENT actions, and the day
  /// they sent the same one cost a person their way into the house. Both
  /// ask who, and both end on the enrolment screen, so they looked
  /// interchangeable — but re-recording your own voice is a voice and
  /// nothing else (you already hold a PIN), while adding somebody is the
  /// whole ceremony or none of it.
  /// Raised only by the five calls below, and read by exactly one branch of
  /// _handleResult. It is what tells a `set_pin` STARTED HERE from the
  /// identical one first-run registration sends — see that branch for why
  /// matching on the action name alone silently ate a result card.
  bool _profileFlow = false;

  void renameUser() {
    _profileFlow = true;
    sendMenuAction('rename_user');
  }

  void changePin() {
    _profileFlow = true;
    sendMenuAction('set_pin');
  }

  /// Recovery words: the hub asks for the current PIN (or offers the
  /// forgotten-PIN road), then shows five NEW words once. The old words
  /// cannot be shown -- the hub never kept them, only their fingerprint.
  void showRecovery() {
    _profileFlow = true;
    sendMenuAction('new_recovery_words');
  }

  void reenrollVoice() {
    _profileFlow = true;
    sendMenuAction('enroll_voice');
  }

  /// Adding somebody ELSE is not the same act as re-recording your own
  /// voice, and sending the same action for both is what built a person
  /// with a voiceprint, no PIN and no way into their own house (audit,
  /// 2026-09-12). `add_person` runs the hub's whole ceremony — voice, PIN,
  /// recovery words, one transaction — which is what the dialog above this
  /// button has always promised.
  void addPerson() {
    _profileFlow = true;
    sendMenuAction('add_person');
  }

  /// Bind a card by tapping it. ONE DOOR for "mine" and "someone else's",
  /// exactly as [reenrollVoice] and [addPerson] are one door: the hub asks
  /// who, and what differs is only where that person is standing.
  ///
  /// A card is a claim, not a key — enrolling one grants no authority it
  /// did not already have. What it buys is that a tap can be read: the
  /// journal says whose card, and a rule can answer to a person.
  void enrollCard() {
    _profileFlow = true;
    sendMenuAction('enroll_card');
  }

  /// The Android SYSTEM back button (or gesture) pops a question route
  /// without going through sendCancel — the server was left waiting on a
  /// reply that would never come, holding the shell lock, and every next
  /// action bounced off "busy". Wired from PopScope on the prompt/choice
  /// screens. No navigation here: the route is already gone; this only
  /// tells the server. All programmatic pops clear pending BEFORE popping,
  /// so this is a no-op for them.
  /// What a cancelled prompt answers with: empty for a secret (PIN) field,
  /// the cancel word for everything else. Only a hub that predates the
  /// `cancelled` flag reads it: 'cancel' is in its CANCEL_TOKENS, and a
  /// SECRET prompt sends empty because "cancel" typed into a PIN box used
  /// to be checked AS the PIN and cost an attempt.
  String _promptCancelValue() =>
      (pendingPrompt?.secret ?? false) ? '' : 'cancel';

  /// Which step of a wizard the open question is, or 0 when it is not one.
  ///
  /// THE HUB DECIDES, not the phone. `meta.wizard` is attached only by
  /// flows written as a LOOP OVER A STEP LIST -- the one shape that can
  /// go backwards, because going back there is `i -= 1`. A flow written
  /// as straight-line awaits cannot rewind (you cannot resume a coroutine
  /// at an earlier await), sends no wizard metadata, and so is never
  /// offered a step back. The day Add device is rewritten as a step
  /// machine and starts sending this, Back begins working there with no
  /// change to this file.
  ///
  /// 1-BASED, as the hub numbers it (`index=1, total=3`).
  int get wizardStep {
    final w = pendingPrompt?.meta?['wizard'];
    if (w is! Map) return 0;
    final i = w['index'];
    return i is int ? i : 0;
  }

  /// Can the BACK GESTURE step this question back instead of cancelling?
  ///
  /// Step 1 must not: the hub answers `prev` on its first field with
  /// `continue  # already first`, so it re-asks the same question -- and a
  /// Back that re-opens the question you pressed Back on is a trap with no
  /// way out. At step 1 Back keeps its old meaning and leaves the flow.
  bool get canStepBack => wizardStep > 1;

  void onQuestionRouteDismissed() {
    // The SAME kinds, the same frames and the same order as the Cancel
    // button (the two used to drift: each once lacked a kind the other
    // had). In the ceremony room a back during an enrollment phrase is a
    // cancelled recording, and the hub erases anything partial.
    final kind = _pendingKind;
    if (kind == null) return; // nothing outstanding — nothing to tell
    sendJson(_cancelFrame(kind, promptValue: _promptCancelValue())!);
    _dropQuestions();
    // Mark WHY the coming 'cancelled' result exists: the person stepped
    // BACK, and is already standing on the last screen. _handleResult
    // reads this and skips the popup-then-home ending X gets.
    _backCancel = true;
    _flow?.abortCapture();
    settleStarting();
    notifyListeners();
  }

  /// True while a 'cancelled' result should end QUIETLY because it was
  /// caused by the back gesture (route already popped, person already on
  /// the last screen). Set in onQuestionRouteDismissed, consumed by
  /// _handleResult, cleared by any fresh gesture.
  bool _backCancel = false;

  /// Returns false when nothing was sent (no live connection).
  bool sendMenuAction(String action) {
    settleStarting();
    final flow = _flow;
    if (flow == null || connState != ConnState.ready) {
      // NOTHING WILL ANSWER, so nothing may wait for an answer. This used
      // to raise menuBusy anyway and hand the frame to a socket that was
      // not there -- no result ever came to lower it, so every later action
      // met "Finish what is on screen first" until the app restarted. The
      // per-request flags the callers raised just before are lowered too.
      _stopCollecting();
      _profileFlow = false;
      _profileForBinding = false;
      profileLoading = false;
      _pendingEditId = null;
      status = 'not connected — cannot send that yet';
      notifyListeners();
      return false;
    }
    // A flow of OURS still running means the hub will refuse this one as
    // busy -- so the running flow's banner lines and its AI-review script
    // are not this request's to wipe (the busy branch of _handleResult
    // keeps the same rule).
    final flowRunning = menuBusy;
    menuBusy = true; // cleared by the result/error frame
    _backCancel = false; // a fresh flow owes nothing to an old gesture
    // NOR TO AN ARMED CANCEL. Pressed while a flow was between questions,
    // the cancel waits for "the next question" -- and used to survive a
    // busy refusal and land on the FIRST question of the next flow (the
    // enrol that ended at its name question, 2026-09-03). The ordinary
    // cancel's arm is the same hazard and was left out.
    _ceremonyCancelNext = false;
    _flowCancelNext = false;
    if (!flowRunning) {
      _flowLines.clear();
    }
    flow.sendMenuAction(action);
    isStarting = true;
    startTimer = Timer(const Duration(seconds: 15), () {
      if (isStarting) {
        settleStarting();
        status = 'No response — try again';
        notifyListeners();
      }
    });
    notifyListeners();
    return true;
  }

  /// True while the server waits on the "Type OK once you've written it
  /// down" acknowledgement — answered by the keepsafe dialog's button.
  bool _keepsafeAckPending = false;

  /// Close-out for the keepsafe dialog: acknowledge the server's pending
  /// prompt (so the flow finishes and routes home) and clear the message.
  void ackKeepsafe() {
    if (_keepsafeAckPending) {
      _keepsafeAckPending = false;
      sendJson({'type': 'prompt_reply', 'value': 'ok'});
    }
    keepsafeMessage = null;
    notifyListeners();
  }

  bool _keepsafeShowing = false;

  /// The recovery-phrase card: themed to the mode (same surface as the Done
  /// popup), words in the accent colour with a copy icon, over a dimmed
  /// blurred backdrop. Dismissing it (button OR barrier tap) acknowledges
  /// the server, which ends the flow and routes home — no more grey hang.
  void _showKeepsafeDialog() {
    if (_keepsafeShowing) return;
    final ctx = navigatorKey.currentContext;
    final message = keepsafeMessage;
    if (ctx == null || message == null) return;
    _keepsafeShowing = true;

    // The words are the last non-empty line of the message; everything
    // above is the warning text.
    final lines = message
        .split('\n')
        .map((l) => l.trim())
        .where((l) => l.isNotEmpty)
        .toList();
    final words = lines.isNotEmpty ? lines.last : '';
    final intro = lines.length > 1
        ? lines.sublist(0, lines.length - 1).join('\n')
        : 'Write this down now.';

    showDialog<void>(
      context: ctx,
      barrierDismissible: true,
      barrierColor: Colors.black54,
      builder: (dctx) => BackdropFilter(
        filter: ImageFilter.blur(sigmaX: 4, sigmaY: 4),
        child: Dialog(
          backgroundColor: surface,
          shape:
              RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(22, 22, 22, 16),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text('Recovery phrase',
                    style: TextStyle(
                        color: ink,
                        fontSize: 16,
                        fontWeight: FontWeight.w700)),
                const SizedBox(height: 10),
                Text(intro,
                    style: TextStyle(
                        color: inkDim, fontSize: 13, height: 1.45)),
                const SizedBox(height: 14),
                Container(
                  padding: const EdgeInsets.fromLTRB(14, 12, 6, 12),
                  decoration: BoxDecoration(
                    color: bg,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: accent),
                  ),
                  child: Row(
                    children: [
                      Expanded(
                        child: SelectableText(
                          words,
                          style: TextStyle(
                            color: accent,
                            fontSize: 17,
                            fontWeight: FontWeight.w700,
                            height: 1.5,
                            letterSpacing: 0.4,
                          ),
                        ),
                      ),
                      IconButton(
                        icon: Icon(Icons.copy_rounded,
                            color: accent, size: 20),
                        tooltip: 'Copy',
                        onPressed: () => Clipboard.setData(
                            ClipboardData(text: words)),
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 16),
                FilledButton(
                  style: FilledButton.styleFrom(
                    backgroundColor: accent,
                    foregroundColor: accentFg,
                    padding: const EdgeInsets.symmetric(vertical: 13),
                    shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(12)),
                  ),
                  onPressed: () => Navigator.of(dctx).pop(),
                  child: const Text("I've written it down",
                      style: TextStyle(
                          fontWeight: FontWeight.w700, fontSize: 15)),
                ),
              ],
            ),
          ),
        ),
      ),
    ).then((_) {
      // ONE close-out for every way the dialog can go away (button, barrier
      // tap, system back): acknowledge the server so the flow finishes and
      // the result routes everything home.
      _keepsafeShowing = false;
      ackKeepsafe();
    });
  }

  // ── Internal ───────────────────────────────────────────────────────

  void sendJson(Map<String, dynamic> obj) {
    final socket = _socket;
    if (socket == null || connState != ConnState.ready) {
      status = 'not connected — cannot send';
      notifyListeners();
      return;
    }
    final sent = socket.send(obj);
    if (sent == false) {
      status = 'not connected — cannot send';
      notifyListeners();
    }
  }

  /// IS THE WAKE WORD LISTENING? While true the microphone is held in the
  /// background, which is the whole point of a wake word.
  ///
  /// Defaults TRUE because that is what the app does today: pipeline.dart's
  /// WakeWordSpotter is fed unconditionally from the moment the pipeline
  /// starts. When the Settings toggle lands it writes here, and turning the
  /// wake word off restores the plain lifecycle rule — leave the app, lose
  /// the mic. The flag exists now so the two behaviours are already
  /// separated rather than tangled together later.
  bool wakeListening = true;

  /// Leaving the app: give the microphone back — unless we are listening.
  ///
  /// The SOCKET is left alone either way. Losing the mic can be what the
  /// person asked for by leaving; losing the connection never is — it would
  /// spend a reconnect on every return, and the hub's notices and approvals
  /// would stop arriving while the app was away.
  Future<void> _releaseMic() async {
    final flow = _flow;
    if (flow != null && flow.phoneWake != null) {
      // The flow owns the microphone in this mode (two engines, one mic).
      // Wake word on: it keeps listening, as the spotter would. Off: both
      // engines let go.
      if (wakeListening) return;
      await flow.suspendMic();
      debugPrint('[mic] app backgrounded — recogniser and microphone released');
      notifyListeners();
      return;
    }
    final mic = _mic;
    if (mic == null || !mic.isRunning) return;
    if (wakeListening) {
      // HELD ON PURPOSE. The foreground service and its notification are
      // what make this honest, and MicCapture no longer fights anything for
      // the stream: if another app takes priority we are handed silence and
      // we sit in it until they are done.
      debugPrint('[mic] app backgrounded — microphone HELD (wake word on)');
      return;
    }
    await mic.stop();
    debugPrint('[mic] app backgrounded — microphone released');
    notifyListeners();
  }

  /// Back in the app: take it again, if we had it.
  ///
  /// Guarded on the pipeline existing rather than assumed: resuming into an
  /// app that never finished connecting must not open a microphone for a
  /// pipeline that cannot use it.
  Future<void> _resumeMic() async {
    final flow = _flow;
    final mic = _mic;
    if (flow != null && flow.phoneWake != null) {
      await flow.resumeMic();
    } else if (mic != null && !mic.isRunning) {
      final ok = await mic.start();
      if (!ok) {
        // Another app may still hold it (a call in progress). Say so rather
        // than leaving a dead orb that looks ready.
        blockingProblem = 'The microphone is in use by another app.';
      } else {
        if (blockingProblem == 'The microphone is in use by another app.') {
          blockingProblem = null;
        }
        debugPrint('[mic] app resumed — microphone reopened');
      }
    }
    // NOT BEHIND THE MIC. This used to sit after `if (mic.isRunning)
    // return;` -- and with the wake word on (the default) the mic is HELD
    // in the background, so it is always running on resume and the redial
    // below was never reached. After the three-rung ladder gave up in the
    // background, coming back to the app left it disconnected.
    //
    // AND THE SOCKET, on the same breath. Android freezes timers for a
    // backgrounded app, so a supervisor that dropped while you were away
    // may be sitting on a 20-second rung that has not been allowed to
    // tick. Coming back to the app is the strongest possible signal that
    // somebody wants the connection NOW, so it does not wait its turn.
    //
    // Safe to call unconditionally: connect() supersedes any in-flight
    // attempt by generation, and does nothing harmful when one is already
    // healthy — but the guard keeps it from disturbing a live socket at all.
    // GATED ON THE SUPERVISOR'S OWN INTENT, not on the connection state.
    // Checking only "am I closed?" would have dragged you back online after
    // you deliberately pressed Disconnect -- the app deciding it knows
    // better than you do, every time you switched apps.
    // redial(), not connect(): connect() cleared the supervisor's "this
    // hub has worked" mark, so when this one sweep failed nothing retried
    // again until the app was reopened.
    final sock = _socket;
    if (_watch.shouldDial(sock, connState)) {
      debugPrint('[ws] resumed — reconnecting now instead of waiting');
      unawaited(sock!.redial());
    }
    notifyListeners();
  }

  /// THE HUB ENDS A CONNECTION'S FLOW WITH THE CONNECTION: ws_server's
  /// run() cancels the running command/menu task when the socket goes, and
  /// a reconnect is a new handler with nothing outstanding -- a late answer
  /// is "arrived with no question outstanding -- ignored". So every mirror
  /// of that flow goes too. Before this only the Disconnect button reset
  /// anything; a drop the supervisor healed left the dead question on
  /// screen, menuBusy raised (every later action: "Finish what is on screen
  /// first", voice menu actions refused) and the Working spinner up.
  /// No status write and no notify: callers do both.
  void _endConnectionFlow() {
    _dismissStillThere(); // nothing left to reassure
    _flow?.abortCapture();
    _clearPending();
    currentView = 'command';
    _stopCollecting();
    _clearPairWindow();
    settleStarting();
    menuBusy = false;
    _flowLines.clear();
    _queryAnswerLines.clear();
    _backCancel = false;
    _handedOff = false;
    _profileFlow = false;
    _profileForBinding = false;
    profileLoading = false;
    // The recovery words stay on screen to be written down; only the
    // acknowledgement is dropped, since no hub is waiting for it now.
    _keepsafeAckPending = false;
    _popFlowRoutes();
  }

  /// Stop and forget the whole pipeline -- flow, subscriptions, mic and
  /// socket -- so a fresh one can be built (a re-pair, a retried boot) or
  /// nothing at all is left running (dispose). Safe with no pipeline.
  Future<void> _stopPipeline() async {
    // FIX: Cancel the state listener so we don't leak subscriptions.
    await _connSub?.cancel();
    _connSub = null;
    await _linkSub?.cancel();
    _linkSub = null;
    await _frameSub?.cancel();
    _frameSub = null;
    final flow = _flow;
    final mic = _mic;
    final socket = _socket;
    _flow = null;
    _mic = null;
    _socket = null;
    // The engine itself stays (see _startPipeline); only what it was saying stops.
    unawaited(_voice?.stop());
    running = false;
    flow?.stateListenable.removeListener(_onFlowChanged);
    try {
      await flow?.stop();
    } catch (_) {}
    // Removed before the mic goes: an observer left registered against a
    // disposed AppState would call _resumeMic() on the next resume and open
    // a microphone nothing owns.
    WidgetsBinding.instance.removeObserver(_micLifecycle);
    try {
      await mic?.dispose();
    } catch (_) {}
    try {
      await socket?.close();
    } catch (_) {}
    if (connState != ConnState.idle) connState = ConnState.idle;
  }

  /// THE NOTIFICATION GOES WHEN THE PIPELINE GOES FOR GOOD (unpair,
  /// dispose). Nothing stopped it: after Unpair it went on saying
  /// "Listening for the wake word" over a process listening to nothing,
  /// holding the Wi-Fi lock and keeping the process alive. Not in
  /// _stopPipeline, which every rebuild runs just before _startPipeline
  /// starts the service again -- a stop and a start back to back.
  Future<void> _stopForegroundService() async {
    try {
      if (await FlutterForegroundTask.isRunningService) {
        await FlutterForegroundTask.stopService();
      }
    } catch (_) {}
  }

  @override
  void dispose() {
    startTimer?.cancel();
    _pairTicker?.cancel();
    unawaited(_stopPipeline().then((_) => _stopForegroundService()));
    _voice?.dispose();
    _voice = null;
    super.dispose();
  }
}


/// A notification card floating over the top of the screen.
class _FloatCard extends StatefulWidget {
  final AppState s;
  final String title;
  final String body;
  final IconData icon;
  final bool warn;
  final String actionLabel;
  final VoidCallback? onAction;
  final VoidCallback onClose;
  const _FloatCard({
    required this.s,
    required this.title,
    required this.body,
    required this.icon,
    required this.warn,
    required this.actionLabel,
    required this.onAction,
    required this.onClose,
  });

  @override
  State<_FloatCard> createState() => _FloatCardState();
}

class _FloatCardState extends State<_FloatCard> {
  bool _in = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) setState(() => _in = true);
    });
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.s;
    const warnInk = Color(0xFF8A5A00);
    return SafeArea(
      child: Align(
        alignment: Alignment.topCenter,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 12, 0),
          child: AnimatedSlide(
            offset: _in ? Offset.zero : const Offset(0, -1.4),
            duration: const Duration(milliseconds: 260),
            curve: Curves.easeOutCubic,
            child: Dismissible(
              key: const ValueKey('float-card'),
              direction: DismissDirection.up,
              onDismissed: (_) => widget.onClose(),
              child: Material(
                color: s.surface,
                elevation: 10,
                shadowColor: Colors.black38,
                borderRadius: BorderRadius.circular(18),
                child: Container(
                  constraints: const BoxConstraints(maxWidth: 520),
                  padding: const EdgeInsets.fromLTRB(14, 12, 4, 12),
                  decoration: BoxDecoration(
                    borderRadius: BorderRadius.circular(18),
                    border: Border.all(color: s.border),
                  ),
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Container(
                        width: 36,
                        height: 36,
                        decoration: BoxDecoration(
                          color: (widget.warn ? const Color(0xFFD29922) : s.accent)
                              .withValues(alpha: 0.14),
                          borderRadius: BorderRadius.circular(12),
                        ),
                        child: Icon(widget.icon,
                            size: 20, color: widget.warn ? warnInk : s.accent),
                      ),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Text(widget.title,
                                maxLines: 3,
                                overflow: TextOverflow.ellipsis,
                                style: TextStyle(
                                    color: s.ink,
                                    fontSize: 14,
                                    height: 1.3,
                                    fontWeight: widget.body.isEmpty
                                        ? FontWeight.w500
                                        : FontWeight.w600)),
                            if (widget.body.isNotEmpty) ...[
                              const SizedBox(height: 3),
                              Text(widget.body,
                                  maxLines: 2,
                                  overflow: TextOverflow.ellipsis,
                                  style: TextStyle(
                                      color: s.inkDim,
                                      fontSize: 12.5,
                                      height: 1.35)),
                            ],
                            if (widget.onAction != null) ...[
                              const SizedBox(height: 8),
                              SizedBox(
                                height: 32,
                                child: FilledButton(
                                  onPressed: widget.onAction,
                                  style: FilledButton.styleFrom(
                                    backgroundColor: s.accent,
                                    foregroundColor: s.accentFg,
                                    padding: const EdgeInsets.symmetric(
                                        horizontal: 14),
                                    shape: RoundedRectangleBorder(
                                        borderRadius:
                                            BorderRadius.circular(16)),
                                  ),
                                  child: Text(widget.actionLabel,
                                      style: const TextStyle(
                                          fontSize: 13,
                                          fontWeight: FontWeight.w600)),
                                ),
                              ),
                            ],
                          ],
                        ),
                      ),
                      IconButton(
                        tooltip: 'Dismiss',
                        icon: Icon(Icons.close, size: 18, color: s.inkFaint),
                        onPressed: widget.onClose,
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// Good news: a small dark pill at the top that fades by itself.
class _FloatPill extends StatefulWidget {
  final AppState s;
  final String text;
  final VoidCallback onClose;
  const _FloatPill({required this.s, required this.text, required this.onClose});

  @override
  State<_FloatPill> createState() => _FloatPillState();
}

class _FloatPillState extends State<_FloatPill> {
  bool _in = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) setState(() => _in = true);
    });
  }

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: Align(
        alignment: Alignment.topCenter,
        child: Padding(
          padding: const EdgeInsets.only(top: 10),
          child: AnimatedOpacity(
            opacity: _in ? 1 : 0,
            duration: const Duration(milliseconds: 220),
            child: GestureDetector(
              onTap: widget.onClose,
              child: Material(
                color: const Color(0xFF1F2A28),
                elevation: 8,
                borderRadius: BorderRadius.circular(22),
                child: Padding(
                  padding: const EdgeInsets.fromLTRB(10, 9, 16, 9),
                  child: Row(mainAxisSize: MainAxisSize.min, children: [
                    Container(
                      width: 24,
                      height: 24,
                      decoration: const BoxDecoration(
                          color: Color(0xFF2E7D6F), shape: BoxShape.circle),
                      child: const Icon(Icons.check_rounded,
                          size: 16, color: Colors.white),
                    ),
                    const SizedBox(width: 10),
                    Text(widget.text,
                        style: const TextStyle(
                            color: Colors.white,
                            fontSize: 13.5,
                            fontWeight: FontWeight.w600)),
                  ]),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
