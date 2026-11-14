/// Pairing this phone with a hub.
///
/// Replaces the old "enter the shared secret" screen, and the difference is
/// not cosmetic. That screen stored a key the app then used for ever, and
/// every phone in the house used the same one. This screen spends a key
/// once and stores none: what it walks away with is a keypair this phone
/// generated and a certificate the hub handed over, so the hub can tell
/// this handset from any other and can revoke it alone.
///
/// WHAT IT DELIBERATELY DOES NOT DO is offer a way past a failure. A
/// certificate that does not match, a hub that will not prove itself, a
/// fingerprint two parsers disagree on -- each stops here with its own
/// sentence and no "continue anyway". This is the one moment trust is
/// established; a button that skips a check at this moment silently
/// disables every check that follows it.
///
/// IT ALSO DOES NOT RETRY ON A TIMER, which is why the reconnect counter
/// from SocketClient's supervisor is not here. That supervisor is right for
/// a connection already trusted; a typed key is not something to hammer an
/// address with. One pass, every address -- but it now SAYS so, which it
/// did not (2026-08-30): it kept only the last failure, so two silent
/// addresses were reported as one, naming the second. That reads as "the
/// app is looking in the wrong place" when the right place was tried first
/// and said nothing.
library;

import 'dart:async';
import 'dart:io' show Platform;

import 'package:device_info_plus/device_info_plus.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show Clipboard, ClipboardData;

import '../app_state.dart';
import '../pairing.dart';
import 'scan_screen.dart';

class PairScreen extends StatefulWidget {
  const PairScreen({super.key, required this.state});

  final AppState state;

  /// Read a `homevoice://pair?...` code, or null. Public HERE, on the
  /// widget, because the scan screen asks it too -- the parser itself
  /// lives on the state class, and a static on a private class cannot be
  /// reached from another file. One parser, so the two never disagree.
  static Map<String, String>? parsePairCode(String raw) =>
      _PairScreenState.parsePairCode(raw);

  @override
  State<PairScreen> createState() => _PairScreenState();
}

class _PairScreenState extends State<PairScreen> {
  final _keyCtl = TextEditingController();
  /// The hub's address, typed. `--pair` prints "address: host:port" under
  /// the code for exactly this box. Empty means "try the list".
  final _hostCtl = TextEditingController();

  /// The person's OWN word for this handset -- a name or username, typed.
  /// It used to be pre-filled with the OS model string, because the name
  /// carried the whole reinstall-matching burden; the model now travels in
  /// its own field (_deviceModel, read automatically, never typed), so the
  /// two identities are finally separate (the owner's design, 2026-08-26):
  /// the hub matches a reinstall on BOTH -- same phone, then the same
  /// name, then the acceptance. 'my phone' remains the fallback for an
  /// empty field -- the hub knows that name is generic and refuses to
  /// purge on it, so the worst case is accumulation, never a wrong revoke.
  final _nameCtl = TextEditingController();

  /// What the handset says it IS ("TECNO KJ5", "samsung SM-A225F") --
  /// stable across reinstalls, shown read-only under the name field and
  /// attached to the pairing automatically.
  String _deviceModel = '';
  bool _busy = false;
  bool _revealKey = false;
  String? _error;
  String? _progress;

  /// EVERY address tried this run, with what it did. One line each.
  final List<String> _tried = [];

  /// The counter. Ticks once a second so a 12-second probe looks like a
  /// probe and not like a hang.
  Timer? _tick;
  DateTime? _deadline;

  /// WHEN THE HUB'S PAIRING WINDOW CLOSES, as far as this phone knows.
  /// First from the scanned code (`until`), then -- authoritatively --
  /// from the hub itself the moment it answers (`pair_window_seconds`).
  /// Null when nobody has said. Drives the countdown at the top of the
  /// screen, which ticks whether or not a pairing attempt is running.
  DateTime? _windowUntil;
  Timer? _windowTick;

  int get _windowLeft {
    final w = _windowUntil;
    if (w == null) return 0;
    final n = w.difference(DateTime.now()).inSeconds;
    return n < 0 ? 0 : n;
  }

  void _setWindow(DateTime? until) {
    _windowUntil = until;
    _windowTick?.cancel();
    _windowTick = null;
    if (until != null) {
      _windowTick = Timer.periodic(const Duration(seconds: 1), (_) {
        if (!mounted) return;
        setState(() {});
        if (_windowLeft <= 0) {
          _windowTick?.cancel();
          _windowTick = null;
        }
      });
    }
    if (mounted) setState(() {});
  }

  static String _mmss(int seconds) {
    final m = seconds ~/ 60;
    final s = seconds % 60;
    return '$m:${s.toString().padLeft(2, '0')}';
  }

  @override
  void initState() {
    super.initState();
    _readDeviceModel();
  }

  Future<void> _readDeviceModel() async {
    try {
      final info = DeviceInfoPlugin();
      String model = '';
      String personal = '';
      if (Platform.isAndroid) {
        final a = await info.androidInfo;
        // "TECNO KJ5", "samsung SM-A225F" -- manufacturer dropped when the
        // model already carries it, which Tecno's does and Samsung's not.
        final maker = a.manufacturer.trim();
        final m = a.model.trim();
        model = m.toLowerCase().startsWith(maker.toLowerCase())
            ? m
            : '$maker $m'.trim();
      } else if (Platform.isIOS) {
        final i = await info.iosInfo;
        model = i.model.trim();
        // The person's own device name ("Prince's iPhone") when iOS grants
        // it makes a fine STARTING name -- but it is a name, not a model,
        // so it goes in the name box, not the device field.
        personal = i.name.trim();
      }
      if (!mounted) return;
      setState(() {
        _deviceModel = model;
        // Never overwrite something the person already typed while this
        // was in flight.
        if (personal.isNotEmpty && _nameCtl.text.trim().isEmpty) {
          _nameCtl.text = personal;
        }
      });
    } catch (_) {
      // No plugin, no permission, odd platform: the device field stays
      // empty, the hub's legacy name-only matching applies, and the
      // generic-name rule keeps 'my phone' safe.
    }
  }

  @override
  void dispose() {
    _recheck?.cancel();
    _hubTick?.cancel();
    _tick?.cancel();
    _windowTick?.cancel();
    _keyCtl.dispose();
    _hostCtl.dispose();
    _nameCtl.dispose();
    super.dispose();
  }

  /// The address the pasted code named, tried BEFORE the compiled-in
  /// list. Null until a code is read.
  String? _qrHost;

  /// The typed address as a `wss://host:port` URL, or null when the box
  /// is empty. Accepts `host`, `host:port`, or a full `wss://` URL; the
  /// port defaults to 8765. Anything unparseable is null, and the list
  /// is tried instead -- a typo must not freeze the button on "Pairing…".
  String? _typedHost() {
    var raw = _hostCtl.text.trim();
    if (raw.isEmpty) return null;
    if (raw.startsWith('wss://')) {
      // Host and port only, and the hub's port when none is given: a bare
      // "wss://hub.local" used to be dialled on 443, where nothing listens.
      final u = Uri.tryParse(raw);
      if (u == null || u.host.isEmpty) return null;
      final host = u.host.contains(':') ? '[${u.host}]' : u.host; // IPv6
      return 'wss://$host:${u.hasPort ? u.port : 8765}';
    }
    if (raw.startsWith('ws://') || raw.startsWith('http')) return null;
    if (!raw.contains(':')) raw = '$raw:8765';
    final u = Uri.tryParse('wss://$raw');
    if (u == null || u.host.isEmpty) return null;
    final port = u.hasPort ? u.port : 8765;
    if (port < 1 || port > 65535) return null;
    return 'wss://${u.host}:$port';
  }

  /// Read `homevoice://pair?host=…&port=…&key=…`, or return null.
  ///
  /// STRICT ABOUT THE SCHEME. Whatever is on a clipboard is whatever was
  /// last copied -- a URL, a message, a password for something else.
  /// Anything that is not our own scheme is not a hub key and must not be
  /// poured into the key box.
  static Map<String, String>? parsePairCode(String raw) {
    final uri = Uri.tryParse(raw.trim());
    if (uri == null || uri.scheme != 'homevoice') return null;
    final key = uri.queryParameters['key'] ?? '';
    if (key.isEmpty) return null;
    final host = uri.queryParameters['host'] ?? '';
    final port = uri.queryParameters['port'] ?? '8765';
    // A port that is not a number is not a hub address: `Uri.parse` on
    // the string built below would throw inside _pair and freeze the
    // button on "Pairing…".
    final portNum = int.tryParse(port);
    if (portNum == null || portNum < 1 || portNum > 65535) return null;
    // WHEN THE DOOR SHUTS, if the code says. `--pair` on the hub opens the
    // window and prints a code carrying its expiry, so the phone can show
    // the countdown the moment it scans. A printed --pair-qr code has no
    // `until` and the countdown simply waits for the hub to say.
    final until = int.tryParse(uri.queryParameters['until'] ?? '');
    // A HOST IS A HOST. Spliced into 'wss://$host:$port', a code carrying
    // "evil.example/x" or "x@evil.example" named a path or a user instead,
    // and the phone dialled somewhere the address box never showed.
    if (host.isNotEmpty) {
      if (RegExp(r'[/?#@\\\s]').hasMatch(host)) return null;
      try {
        Uri(scheme: 'wss', host: host, port: portNum);
      } on FormatException {
        return null;
      }
    }
    return {
      'key': key,
      if (host.isNotEmpty) 'url': 'wss://$host:$port',
      if (host.isNotEmpty) 'where': '$host:$port',
      if (until != null && until > 0) 'until': '$until',
    };
  }

  /// THE CAMERA, and only the camera (her call, 2026-09-02: "remove the
  /// paste button"). ScanScreen (`mobile_scanner`, on-device) hands the
  /// raw code here; parsePairCode is the only thing that decides what it
  /// means. The scanner lives in its own file so the plugin is one import
  /// in one screen -- it was dropped once (2026-08-31) because the plugin
  /// version pulled a Kotlin/AGP upgrade into the build, and came back
  /// pinned to a version that does not. Typing the key by hand remains
  /// possible for a phone with no usable camera.
  void _applyCode(String raw) {
    final parsed = parsePairCode(raw);
    if (parsed == null) {
      setState(() => _error =
          'That code is not a hub code. Show the one the hub prints when '
          'you run  python setup_manager.py --pair  on it.');
      return;
    }
    final untilRaw = int.tryParse(parsed['until'] ?? '');
    final until = untilRaw == null
        ? null
        : DateTime.fromMillisecondsSinceEpoch(untilRaw * 1000);
    setState(() {
      _keyCtl.text = parsed['key']!;
      _qrHost = parsed['url'];
      if (parsed['where'] != null) _hostCtl.text = parsed['where']!;
      _error = null;
      _progress = _qrHost == null
          ? 'key read from the code'
          : 'key read, and the hub is at ${parsed['where']}';
    });
    // A code from `--pair` says when its window shuts; a stale one (an
    // old printout) says a time already past, which reads as "closed --
    // open it again", never as open.
    if (until != null) _setWindow(until);
  }

  /// What the hub said when asked right after the scan: open or closed
  /// (null: not asked yet, or it did not answer).
  bool? _hubOpen;
  DateTime? _hubOpenUntil;
  Timer? _recheck;
  Timer? _hubTick;

  /// Ask the hub whether pairing is open (the key's handshake, stopped
  /// before anything is recorded). Closed: asked again every 8 s.
  Future<void> _checkHubWindow() async {
    _recheck?.cancel();
    final url = _qrHost;
    final key = _keyCtl.text.trim();
    if (url == null || key.isEmpty || _busy || !mounted) return;
    try {
      await pairWithHub(
        url: url,
        apiKey: key,
        deviceName: 'check',
        identity: await widget.state.identity.keyPair(),
        timeout: const Duration(seconds: 6),
        checkOnly: true,
        onWindow: (open, seconds) {
          if (!mounted) return;
          setState(() {
            _hubOpen = open;
            _hubOpenUntil = open && seconds > 0
                ? DateTime.now().add(Duration(seconds: seconds))
                : null;
            _progress = null;
          });
        },
      );
    } catch (_) {
      // 'checked' is the normal end; anything else: no answer to show.
    }
    if (!mounted) return;
    _hubTick?.cancel();
    if (_hubOpenUntil != null) {
      _hubTick = Timer.periodic(const Duration(seconds: 1), (_) {
        if (!mounted) return;
        setState(() {});
        if (_hubOpenUntil!.isBefore(DateTime.now())) {
          _hubTick?.cancel();
          _hubOpen = false;
          _hubOpenUntil = null;
          _recheck = Timer(const Duration(seconds: 8), _checkHubWindow);
        }
      });
    }
    if (_hubOpen == false) {
      _recheck = Timer(const Duration(seconds: 8), _checkHubWindow);
    }
  }

  Widget _hubWindowLine(AppState s) {
    final open = _hubOpen == true;
    final until = _hubOpenUntil;
    final left = until == null
        ? 0
        : until.difference(DateTime.now()).inSeconds.clamp(0, 1 << 30);
    final c = open ? s.accent : const Color(0xFFD29922);
    return Container(
      margin: const EdgeInsets.only(top: 14),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      decoration: BoxDecoration(
        color: c.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: c.withValues(alpha: 0.5)),
      ),
      child: Row(children: [
        Icon(open ? Icons.lock_open_rounded : Icons.lock_outline_rounded,
            size: 18, color: c),
        const SizedBox(width: 10),
        Expanded(
          child: Text(
              open
                  ? 'Hub pairing is open'
                  : 'Hub pairing is closed. Hold the hub\'s button for 2 '
                      'seconds.',
              style: TextStyle(color: s.ink, fontSize: 13.5)),
        ),
        if (open && left > 0)
          Text('${left ~/ 60}:${(left % 60).toString().padLeft(2, '0')}',
              style: TextStyle(
                  color: c, fontSize: 15, fontWeight: FontWeight.w600)),
      ]),
    );
  }

  Future<void> _scanCode() async {
    final raw = await Navigator.of(context).push<String>(
      MaterialPageRoute(
        builder: (_) => ScanScreen(state: widget.state),
        settings: const RouteSettings(name: '/scan'),
      ),
    );
    if (!mounted || raw == null) return;
    _applyCode(raw);
    // Is pairing open on that hub? Asked now, said on screen.
    unawaited(_checkHubWindow());
  }

  int get _left {
    final d = _deadline;
    if (d == null) return 0;
    final n = d.difference(DateTime.now()).inSeconds;
    return n < 0 ? 0 : n;
  }

  Future<void> _pair() async {
    final apiKey = _keyCtl.text.trim();
    if (apiKey.isEmpty) {
      setState(() => _error = 'Enter the hub key first.');
      return;
    }
    // ONLY THE ADDRESS GIVEN HERE: typed, or on the code. No built-in
    // hotspot addresses and no other hub's addresses -- a phone pairing the
    // laptop has no business dialling the Pi's 192.168.4.1.
    if (_typedHost() == null && _qrHost == null) {
      setState(() => _error =
          "Scan the hub's code, or type its address (for example 192.168.137.1).");
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
      _tried.clear();
      _progress = 'looking for the hub…';
    });
    _tick?.cancel();
    _tick = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) setState(() {});
    });

    // EVERY CANDIDATE, in the app's own preference order: the access point
    // first, the tailnet last. Pairing happens on the hub's own Wi-Fi; the
    // tailnet address is used afterwards.
    const budget = Duration(seconds: 12);
    // THE ADDRESS THE PERSON GAVE, and nothing else: typed, or from the
    // code (a scan fills the box, so the two agree).
    final typed = _typedHost();
    final hosts = <String>[];
    // ONE ADDRESS, ONE TRY. The three sources spell an address their own
    // way (the code's port is text, the box is typed, a beacon's is a
    // number), and two spellings of the same place were tried as two:
    // "1 of 2" and "2 of 2" at the same IP. Each is reduced to the same
    // plain host and port before it is compared.
    void add(String? url) {
      if (url == null || url.isEmpty) return;
      final u = Uri.tryParse(url.trim());
      if (u == null || u.host.isEmpty) return;
      var host = u.host.toLowerCase();
      if (host.endsWith('.')) host = host.substring(0, host.length - 1);
      if (host.contains(':')) host = '[$host]'; // IPv6
      final same = 'wss://$host:${u.hasPort ? u.port : 8765}';
      if (!hosts.contains(same)) hosts.add(same);
    }
    add(typed);
    add(_qrHost);
    PairException? stopper;
    try {
    // The keypair is made BEFORE the exchange and stored immediately, so a
    // pairing that dies halfway cannot leave the hub holding a public key
    // whose private half was never saved.
    final identity = await widget.state.identity.keyPair();
    for (var i = 0; i < hosts.length; i++) {
      final host = hosts[i];
      if (!mounted) return;
      final where = '${Uri.parse(host).host}:${Uri.parse(host).port}';
      setState(() {
        _deadline = DateTime.now().add(budget);
        _progress = '${i + 1} of ${hosts.length} — $where';
      });
      try {
        final result = await pairWithHub(
          url: host,
          apiKey: apiKey,
          timeout: budget,
          deviceName: _nameCtl.text.trim().isEmpty
              ? 'my phone'
              : _nameCtl.text.trim(),
          deviceModel: _deviceModel,
          identity: identity,
          // The door, seen from outside it: how long the hub's pairing
          // window has left, told to the one phone the countdown chip can
          // never reach. See pairWithHub.onWindowSeconds.
          onWindowSeconds: (seconds) {
            if (!mounted) return;
            // THE HUB'S OWN WORD replaces whatever the code said.
            _setWindow(DateTime.now().add(Duration(seconds: seconds)));
            setState(() {
              _deadline = null;
              _progress = 'hub is accepting this phone…';
            });
          },
        );
        _tick?.cancel();
        // THE HUB HAS ALREADY SAID YES, so this phone keeps its half even
        // if the screen was left mid-exchange (Back is live while pairing).
        // Returning here on !mounted threw away a pairing the hub had
        // recorded -- a phone the hub lists, holding no certificate.
        if (mounted) {
          setState(() {
            _deadline = null;
            _progress = 'paired as ${result.fingerprint}';
          });
        }
        await widget.state.completePairing(
          phoneId: result.phoneId,
          certificatePem: result.certificatePem,
          hubName: result.hubName,
          // Filed under THIS hub's addresses, before it is dialled.
          address: host,
        );
        // THE ADDRESS THAT JUST WORKED GOES FIRST. The code was drawn by
        // the hub you are standing at, so this is where the hub is now --
        // on a house router it is an address the compiled-in list has
        // never heard of, and without this line the phone would pair
        // here and then dial two dead hotspots before finding it again
        // (2026-09-08). Learned, not typed: nothing else to do.
        widget.state.rememberAddress(host);
        // The key was spent; it has no business staying on the clipboard
        // for the next app that reads it.
        await Clipboard.setData(const ClipboardData(text: ''));
        // STRAIGHT HOME. The first live pairing succeeded and then sat
        // here looking hung, because nothing dismissed this screen -- the
        // person had to guess that the back arrow was safe. A finished
        // ceremony leaves the room: home renders, the connection comes up,
        // and the hub's next question (log in, or record your voice)
        // arrives there as a card.
        if (mounted) {
          Navigator.of(context).popUntil((route) => route.isFirst);
        }
        return;
      } on PairException catch (e) {
        _tried.add('$where — ${e.message}');
        // A HUB THAT ANSWERED AND REFUSED IS THE ANSWER. Only an address
        // fault is worth trying the next candidate for; a wrong key or a
        // closed pairing window will say the same thing at every address,
        // and moving on would replace a precise message with "could not
        // reach your hub".
        if (!e.addressFault) {
          stopper = e;
          break;
        }
      } catch (e) {
        _tried.add('$where — $e');
        stopper = PairException('failed', '$e');
        break;
      }
    }
    } catch (e) {
      // Anything thrown OUTSIDE the per-host try (a bad address, a failed
      // keypair) used to escape this method and leave _busy true forever.
      stopper = PairException('failed', '$e');
    }

    _tick?.cancel();
    if (!mounted) return;
    setState(() {
      _busy = false;
      _progress = null;
      _deadline = null;
      _error = stopper?.message ??
          'Nothing answered.\n${_tried.join('\n')}\n\n'
              'Start the hub, let port 8765 through its firewall, and check '
              'this phone is on that Wi-Fi. The hub prints the address it '
              'is on: "[WS] Listening on …".';
    });
  }

  @override
  Widget build(BuildContext context) {
    // HER PALETTE, NOT MATERIAL'S. Every other screen draws from AppState's
    // colours -- deep teal and ivory -- and the first live run of this one
    // arrived in Material default purple, which read as someone else's app
    // at the exact moment the person is deciding whether to trust it.
    final s = widget.state;
    return Scaffold(
      backgroundColor: s.bg,
      appBar: AppBar(
        backgroundColor: s.bg,
        elevation: 0,
        leading: IconButton(
          icon: Icon(Icons.arrow_back, color: s.ink),
          onPressed: () => Navigator.of(context).maybePop(),
        ),
        title: Text('Pair with your hub',
            style: TextStyle(
                color: s.ink, fontSize: 17, fontWeight: FontWeight.w600)),
        actions: [
          if (!_busy)
            TextButton(
              onPressed: () {
                widget.state.dismissPairing();
                Navigator.of(context).maybePop();
              },
              child: Text('Later', style: TextStyle(color: s.inkDim)),
            ),
        ],
      ),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            // ONE LINE AND ONE BUTTON (2026-09-29, "make it a neat screen").
            Text('Scan the code your hub shows.',
                style: TextStyle(color: s.inkDim, fontSize: 14)),
            const SizedBox(height: 14),
            OutlinedButton.icon(
              style: OutlinedButton.styleFrom(
                foregroundColor: s.accent,
                side: BorderSide(color: s.accent),
                padding: const EdgeInsets.symmetric(vertical: 14),
                shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(10)),
              ),
              onPressed: _busy ? null : _scanCode,
              icon: const Icon(Icons.qr_code_scanner_rounded),
              label: const Text('Scan hub code',
                  style: TextStyle(fontWeight: FontWeight.w600)),
            ),
            // What the hub said, asked right after the scan.
            if (_hubOpen != null) _hubWindowLine(s),
            // THE DOOR, WITH A CLOCK ON IT. Known from the scanned code
            // first and from the hub itself once it answers; the chip
            // turns from open to closed on its own, so nobody presses
            // Pair against a window that shut while they were typing.
            // The hub's own answer, once asked, replaces the code's guess.
            if (_windowUntil != null && _hubOpen == null) ...[
              const SizedBox(height: 14),
              Container(
                padding:
                    const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                decoration: BoxDecoration(
                  color: (_windowLeft > 0 ? s.accent : s.cancelInk)
                      .withValues(alpha: 0.12),
                  borderRadius: BorderRadius.circular(10),
                  border: Border.all(
                      color: (_windowLeft > 0 ? s.accent : s.cancelInk)
                          .withValues(alpha: 0.5)),
                ),
                child: Row(children: [
                  Icon(
                      _windowLeft > 0
                          ? Icons.lock_open_rounded
                          : Icons.lock_outline_rounded,
                      size: 18,
                      color: _windowLeft > 0 ? s.accent : s.cancelInk),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      _windowLeft > 0
                          ? 'Pairing is open on the hub'
                          : 'Pairing has closed on the hub -- run --pair '
                              'again and rescan',
                      style: TextStyle(color: s.ink, fontSize: 13),
                    ),
                  ),
                  if (_windowLeft > 0)
                    Text(_mmss(_windowLeft),
                        style: TextStyle(
                            color: s.accent,
                            fontSize: 15,
                            fontWeight: FontWeight.w600,
                            fontFeatures: const [FontFeature.tabularFigures()])),
                ]),
              ),
            ],
            const SizedBox(height: 20),
            TextField(
              controller: _keyCtl,
              enabled: !_busy,
              autocorrect: false,
              enableSuggestions: false,
              // HIDDEN BY DEFAULT, revealed by the eye. This is the one
              // secret this screen ever touches; a long random string is
              // also miserable to type blind, so the reveal is a first-
              // class control rather than an apology.
              obscureText: !_revealKey,
              style: TextStyle(color: s.ink),
              decoration: InputDecoration(
                labelText: 'Hub key',
                labelStyle: TextStyle(color: s.inkDim),
                filled: true,
                fillColor: s.surface,
                suffixIcon: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    // THE HUB CODE. It carries the address AND the key
                    // (setup_manager --pair-qr), so one paste fills both
                    // boxes and skips the address guessing that made "No
                    // answer from wss://192.168.4.1" the first thing this
                    // screen ever said to her. Scan it with the phone's
                    // own camera, copy, tap here.
                    //
                    // A CLIPBOARD ICON, NOT A QR ONE (2026-09-01, "the qr
                    // scaner doesnt work why"). It wore Icons.qr_code_2,
                    // which promises a camera that opens — so tapping it
                    // and getting a clipboard message read as a broken
                    // scanner rather than as the button it actually is.
                    // The icon now says what happens; the helper line
                    // below says where the code comes from.
                    // THE CAMERA, back as a real button: it opens
                    // ScanScreen, which returns the raw code. The icon
                    // promises a camera and a camera is what opens.
                    IconButton(
                      tooltip: 'Scan the hub code',
                      icon: Icon(Icons.qr_code_scanner_rounded, color: s.accent),
                      onPressed: _busy ? null : _scanCode,
                    ),
                    IconButton(
                      tooltip: _revealKey ? 'Hide' : 'Show',
                      icon: Icon(
                          _revealKey
                              ? Icons.visibility_off_outlined
                              : Icons.visibility_outlined,
                          color: s.inkDim),
                      onPressed: () =>
                          setState(() => _revealKey = !_revealKey),
                    ),
                  ],
                ),
                border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(8),
                    borderSide: BorderSide(color: s.border)),
                enabledBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(8),
                    borderSide: BorderSide(color: s.border)),
                focusedBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(8),
                    borderSide: BorderSide(color: s.accent)),
              ),
            ),
            const SizedBox(height: 16),
            TextField(
              controller: _hostCtl,
              enabled: !_busy,
              autocorrect: false,
              enableIMEPersonalizedLearning: false,
              keyboardType: TextInputType.url,
              style: TextStyle(color: s.ink, fontFamily: 'monospace'),
              decoration: InputDecoration(
                labelText: 'Hub address',
                hintText: '192.168.0.150:8765',
                hintStyle: TextStyle(color: s.inkFaint),
                labelStyle: TextStyle(color: s.inkDim),
                filled: true,
                fillColor: s.surface,
                border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(8),
                    borderSide: BorderSide(color: s.border)),
                enabledBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(8),
                    borderSide: BorderSide(color: s.border)),
                focusedBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(8),
                    borderSide: BorderSide(color: s.accent)),
              ),
            ),
            const SizedBox(height: 16),
            TextField(
              controller: _nameCtl,
              enabled: !_busy,
              style: TextStyle(color: s.ink),
              decoration: InputDecoration(
                labelText: 'Your name for this phone',
                hintText: "e.g. Augusta, or Augusta's phone",
                hintStyle: TextStyle(color: s.inkFaint),
                labelStyle: TextStyle(color: s.inkDim),
                filled: true,
                fillColor: s.surface,
                border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(8),
                    borderSide: BorderSide(color: s.border)),
                enabledBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(8),
                    borderSide: BorderSide(color: s.border)),
                focusedBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(8),
                    borderSide: BorderSide(color: s.accent)),
              ),
            ),
            // The phone's model is still attached to the pairing, silently.
            const SizedBox(height: 24),
            if (_progress != null)
              Row(children: [
                SizedBox(
                  width: 16,
                  height: 16,
                  child: CircularProgressIndicator(
                      strokeWidth: 2, color: s.accent),
                ),
                const SizedBox(width: 12),
                Expanded(
                    child: Text(_progress!,
                        style: TextStyle(color: s.inkDim, fontSize: 13))),
                // THE COUNTER.
                if (_left > 0)
                  Text('${_left}s',
                      style: TextStyle(color: s.inkFaint, fontSize: 13)),
              ]),
            // Addresses already ruled out, kept on screen while the next
            // one is tried -- so two silent addresses look like two.
            for (final line in _tried)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text('✕  $line',
                    style: TextStyle(color: s.cancelInk, fontSize: 12)),
              ),
            if (_progress != null || _tried.isNotEmpty)
              const SizedBox(height: 16),
            if (_error != null)
              Container(
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: const Color(0xFFF85149).withValues(alpha: 0.12),
                  borderRadius: BorderRadius.circular(8),
                  border: Border.all(
                      color: const Color(0xFFF85149).withValues(alpha: 0.4)),
                ),
                child: Text(_error!,
                    style: TextStyle(
                        color: s.ink, fontSize: 13, height: 1.4)),
              ),
            const SizedBox(height: 16),
            FilledButton(
              style: FilledButton.styleFrom(
                backgroundColor: s.accent,
                foregroundColor: s.accentFg,
                padding: const EdgeInsets.symmetric(vertical: 14),
              ),
              onPressed: _busy ? null : _pair,
              child: Text(_busy ? 'Pairing…' : 'Pair',
                  style: const TextStyle(fontWeight: FontWeight.w600)),
            ),
          ],
        ),
      ),
    );
  }
}