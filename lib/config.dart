/// Central configuration. Everything a deployment might tune lives here,
/// mirroring the server's own config.py discipline: one file answers
/// "how long", "how big", "where".
library;

class AppConfig {
  AppConfig._();

  // ── Where the server lives ─────────────────────────────────────────────
  //
  // PREFERENCE order, access points first, tailnet last -- the same order
  // the server injects into the browser page. The app probes top-down and
  // keeps the first socket that opens, so a phone in hotspot range uses the
  // local radio even with Tailscale up. Editable in Settings; these are the
  // first-run defaults.
  // NO TAILNET ENTRY HERE, DELIBERATELY. There used to be one, and it was
  // one developer's laptop -- 'desktop-2jkh971.tail0d1738.ts.net' -- shipped
  // to every phone as a default. On any other hub it is a name that will
  // never resolve, so the app spends its longest probe budget on it at every
  // start-up and remote access appears broken rather than unconfigured.
  //
  // A tailnet name belongs to a MACHINE, not to this app. Add yours once in
  // Settings -> Network; it is remembered. The server has the matching fix:
  // it asks Tailscale for its own name rather than being told.
  //
  // NO ADDRESS IS BUILT IN (2026-09-28). A hub's addresses come only from
  // its own pairing code or the hub itself once connected --
  // so a phone paired with the laptop never dials the Pi's 192.168.4.1.
  static const List<String> defaultCandidates = [];

  /// The addresses that USED to be built in. Stripped once from each hub's
  /// saved list (AppState._dropLegacyDefaults), keeping whichever of them is
  /// first -- the one that last answered.
  static const List<String> legacyDefaults = [
    'wss://192.168.137.1:8765',
    'wss://192.168.4.1:8765',
  ];

  /// Per-candidate connect budget. Field-tested at 8s: budget-phone TLS
  /// handshakes (pinned-cert verification on this CPU) have blown a 2s
  /// budget before, and a too-short probe reads as "server unreachable"
  /// when everything is actually fine. The cost of a longer budget is only
  /// paid when a candidate is genuinely down (slower fallback), never on
  /// the happy path. DO NOT lower without re-testing on the slowest phone.
  static const Duration probeTimeout = Duration(seconds: 8);

  /// Probe budget for HOSTNAME candidates (the tailnet). Kept separate and
  /// much larger: a Tailscale path that falls back to a DERP relay adds a
  /// long round-trip to every handshake step, and field logs showed the
  /// full wss handshake completing ~10s after the probe started -- 2s AFTER
  /// an 8s budget gave up, leaving an orphaned socket the server then
  /// auth-timed-out. The AP budget stays tight because the AP is local and
  /// a dead AP should fall through fast; the tailnet is the LAST candidate,
  /// so patience here delays nothing else.
  static const Duration hostnameProbeTimeout = Duration(seconds: 25);

  // ── Audio ──────────────────────────────────────────────────────────────
  //
  // 16 kHz mono 16-bit: the single format the whole chain agrees on --
  // Moonshine's input, Silero's input, and the WAV the server's audio_io
  // decoder expects ("expected 16-bit PCM WAV" is its literal error text).
  static const int sampleRate = 16000;

  /// Hard ceiling on one utterance. The server rejects b64 above 7,000,000
  /// chars (config.MAX_AUDIO_B64_LEN); 15 s of 16k/16-bit is ~480 KB raw,
  /// ~640 KB b64 -- an order of magnitude under the limit.
  static const Duration maxUtterance = Duration(seconds: 15);

  /// The active window opened by the wake word. Silence closes it sooner.
  static const Duration activeWindow = Duration(seconds: 8);

  /// Trailing silence that ends an utterance inside the active window.
  static const Duration endpointSilence = Duration(milliseconds: 900);

  // ── Models (downloaded on first run, not bundled -- see README) ────────
  //
  // Filenames under the app's EXTERNAL files dir /models (the adb-reachable
  // one -- see README). base int8 is today's deployment model: sherpa-onnx
  // ships Moonshine v1 tiny/base only. Small and Medium are v2 (streaming,
  // different layout); they become an option when sherpa releases converted
  // English v2 models, via an edit to audio/pipeline.dart alone. Medium's
  // ~400 MB resident stays a bad fit for a 3-4 GB phone regardless.
  static const String moonshineDir = 'moonshine'; // preprocess/encode/... .onnx
  static const String vadModel = 'silero_vad.onnx';

  /// Optional GTCRN speech-enhancement model for the transcript path.
  /// Sub-megabyte; when the file is absent the cleaner simply runs without
  /// the neural stage -- never a startup failure.
  static const String gtcrnModel = 'gtcrn_simple.onnx';
  static const String kwsDir = 'kws'; // zipformer keyword-spotter files

  /// Wake phrase, in the keyword-spotter's expected token format. Rebuild
  /// keywords.txt (see README) if you change the phrase.
  static const String wakePhrase = 'hey home';

  /// The phrase for the engines that READ WORDS -- "Only when you speak"
  /// and the phone's recogniser. They need no keywords.txt, so this can be
  /// changed freely. LONGER AND MORE DISTINCT than "hey home" on purpose
  /// (field log 2026-09-30: the short phrase came back as "Here google",
  /// "He room", "Um"): "hello" is a word every recogniser knows, and three
  /// words give the matcher something to hold on to. The bundled spotter
  /// keeps [wakePhrase], because its model is built for that one.
  static const String wakePhraseText = 'hello nova';

  // ── Protocol constants that must match the server ──────────────────────
  static const int maxAudioB64 = 7000000; // config.MAX_AUDIO_B64_LEN
}