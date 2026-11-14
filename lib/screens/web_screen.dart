/// An integration's own sign-in page, inside the app.
///
/// Some Home Assistant integrations cannot be set up with a form because the
/// account is not ours to ask about: Nest, Spotify, Withings, Tesla. HA hands
/// back an `external_step` with a URL, the person signs in wherever that
/// leads, and the site redirects back to HA — which advances the flow by
/// itself. Nothing on this screen reports success; the hub is watching Home
/// Assistant, not this WebView.
///
/// A WebView rather than a browser hand-off because `webview_flutter` is
/// already here for the ESPHome screen, so this costs no new dependency.
///
/// THE ONE CAVEAT, said out loud: Google refuses OAuth inside embedded
/// WebViews (`disallowed_useragent`), so a Google-backed sign-in — Nest, most
/// obviously — will be turned away. That is why "Copy link" is in the app bar
/// rather than hidden: paste it into a real browser and the flow still
/// completes, because the hub is polling HA regardless of where the sign-in
/// happened. The "Open in browser" button in the app bar is exactly that
/// hand-off; "Copy link" stays for the person who wants a DIFFERENT browser.
library;

import 'dart:async';
import 'dart:io' show InternetAddress, InternetAddressType;

import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:flutter/services.dart';
import 'package:webview_flutter/webview_flutter.dart';

import '../app_state.dart';

/// Plain http is allowed only to an address inside the house (the same
/// rule AppState._handleOpen applies to the page the hub names): a sign-in
/// page that hops to http on the open internet would carry the password in
/// the clear.
bool _houseHost(String host) {
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

class WebScreen extends StatefulWidget {
  final AppState appState;
  const WebScreen({super.key, required this.appState});

  @override
  State<WebScreen> createState() => _WebScreenState();
}

class _WebScreenState extends State<WebScreen> {
  WebViewController? _controller;
  int _progress = 0;
  String? _error;
  String _loadedUrl = '';

  /// The host the page is on RIGHT NOW, for the app bar. A sign-in that
  /// hops between domains should be watched, and this is the only place
  /// the person can read where they are.
  String _host = '';
  bool _didSchedulePop = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  /// EVERY way off this screen forgets the sign-in -- not only the back
  /// arrow and the self-pop: a Cancel, a new flow or an unpair pops this
  /// route from AppState, and the session used to survive those.
  @override
  void dispose() {
    unawaited(WebViewCookieManager().clearCookies().catchError((_) => false));
    final c = _controller;
    if (c != null) {
      unawaited(c.clearLocalStorage().catchError((_) {}));
    }
    super.dispose();
  }

  void _load() {
    final url = widget.appState.pendingWebUrl;
    if (url == null || url.isEmpty) return;
    final controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setBackgroundColor(widget.appState.bg)
      ..setNavigationDelegate(NavigationDelegate(
        // ONLY THE WEB. Anything else a page tries to open (intent:,
        // tel:, a custom scheme) is not a sign-in step and is refused;
        // mailto/tel go to the phone's own apps instead.
        onNavigationRequest: (req) {
          final u = Uri.tryParse(req.url);
          if (u == null) return NavigationDecision.prevent;
          if (u.isScheme('https') ||
              (u.isScheme('http') && _houseHost(u.host))) {
            return NavigationDecision.navigate;
          }
          if (u.isScheme('mailto') || u.isScheme('tel')) {
            launchUrl(u, mode: LaunchMode.externalApplication);
          }
          return NavigationDecision.prevent;
        },
        onPageStarted: (url) {
          if (mounted) setState(() => _host = Uri.tryParse(url)?.host ?? '');
        },
        onUrlChange: (change) {
          final url = change.url;
          if (url != null && mounted) {
            setState(() => _host = Uri.tryParse(url)?.host ?? '');
          }
        },
        onProgress: (p) {
          if (mounted) setState(() => _progress = p);
        },
        onPageFinished: (_) {
          if (mounted) setState(() => _progress = 100);
        },
        onWebResourceError: (e) {
          // Only MAIN-frame failures. A sign-in page pulls in analytics and
          // fonts that fail all the time without the page being broken.
          if (e.isForMainFrame == true && mounted) {
            setState(() => _error =
                'Could not load the sign-in page.\n'
                'Check this phone has internet — this page lives on the '
                'integration\'s own website, not on your hub.');
          }
        },
      ))
      ..loadRequest(Uri.parse(url));
    setState(() {
      _loadedUrl = url;
      _host = Uri.tryParse(url)?.host ?? '';
      _controller = controller;
      _error = null;
      _progress = 0;
    });
  }

  /// Leave the page: forget its cookies, then tell AppState. A sign-in
  /// session must not outlive the screen it was typed into.
  void _close() {
    WebViewCookieManager().clearCookies();
    widget.appState.closeWebPage();
  }

  Future<void> _copyLink() async {
    await Clipboard.setData(ClipboardData(text: _loadedUrl));
    if (!mounted) return;
    final s = widget.appState;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      backgroundColor: s.surface,
      duration: const Duration(seconds: 4),
      content: Text(
        'Link copied. Open it in your browser and sign in there — '
        'the hub is watching and will carry on by itself.',
        style: TextStyle(color: s.ink),
      ),
    ));
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: widget.appState,
      builder: (context, _) => _build(context),
    );
  }

  Widget _build(BuildContext context) {
    final s = widget.appState;

    // The flow moved on — a form arrived, or it finished, or it timed out.
    // This page is spent, so it leaves rather than sitting under whatever
    // came next.
    if (s.pendingWebUrl == null) {
      if (!_didSchedulePop) {
        _didSchedulePop = true;
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (!mounted) return;
          final route = ModalRoute.of(context);
          // Same double-pop guard the question screens use: a route already
          // mid-exit is not current, and popping again removes HOME.
          if (route != null &&
              route.isCurrent &&
              widget.appState.pendingWebUrl == null) {
            WebViewCookieManager().clearCookies(); // spent page, spent session
            Navigator.of(context).pop();
          } else {
            _didSchedulePop = false;
          }
        });
      }
      return const SizedBox.shrink();
    }
    _didSchedulePop = false;

    // The hub asked for a DIFFERENT page while this one was open (a second
    // sign-in in the same flow). Follow it.
    if (s.pendingWebUrl != _loadedUrl) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _load();
      });
    }

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
      child: PopScope(
        // Back walks the SIGN-IN's own history first — consent screens are
        // several pages deep and leaving on the first Back would lose them.
        canPop: false,
        onPopInvokedWithResult: (didPop, _) async {
          if (didPop) return;
          final navigator = Navigator.of(context);
          // Looked up BEFORE the await: a BuildContext is not to be used
          // across an async gap (use_build_context_synchronously).
          final route = ModalRoute.of(context);
          final c = _controller;
          if (c != null && await c.canGoBack()) {
            c.goBack();
            return;
          }
          if (!mounted) return;
          // Leaving does NOT cancel the set-up. The hub is watching Home
          // Assistant, so a sign-in finished in a real browser still lands.
          _close();
          // Only while this page is still on top: across the await a hub
          // frame may have pushed a screen over it, and a bare pop would
          // take THAT one. _close() already lets the page leave by itself.
          if (route?.isCurrent ?? false) navigator.pop();
        },
        child: Scaffold(
          backgroundColor: s.bg,
          appBar: AppBar(
            backgroundColor: s.bg,
            elevation: 0,
            leading: IconButton(
              icon: Icon(Icons.arrow_back, color: s.ink),
              onPressed: () {
                _close();
                Navigator.of(context).pop();
              },
            ),
            title: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  s.pendingWebTitle.isEmpty ? 'Sign in' : s.pendingWebTitle,
                  style: TextStyle(
                      color: s.ink, fontSize: 17, fontWeight: FontWeight.w600),
                ),
                // WHERE YOU ARE. The host, live, so a page that wanders
                // off to another domain is seen doing it.
                if (_host.isNotEmpty)
                  Text(_host,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(color: s.inkDim, fontSize: 11.5)),
              ],
            ),
            actions: [
              // THE GOOGLE ESCAPE HATCH. Google refuses to run its sign-in
              // inside any app's embedded WebView (disallowed_useragent),
              // because a hosting app can read what is typed into its own
              // WebView -- a ban worth having. The hub never learns of the
              // sign-in from THIS screen anyway (it watches HA, or the
              // tailscale process), so the same URL in the real browser
              // completes the flow identically.
              IconButton(
                tooltip: 'Open in browser',
                icon: Icon(Icons.open_in_browser, color: s.inkDim),
                onPressed: () {
                  final url = widget.appState.pendingWebUrl;
                  if (url == null || url.isEmpty) return;
                  launchUrl(Uri.parse(url),
                      mode: LaunchMode.externalApplication);
                },
              ),
              IconButton(
                tooltip: 'Copy link',
                icon: Icon(Icons.link, color: s.inkDim),
                onPressed: _copyLink,
              ),
              IconButton(
                tooltip: 'Reload',
                icon: Icon(Icons.refresh, color: s.inkDim),
                onPressed: () {
                  setState(() => _error = null);
                  _controller?.reload();
                },
              ),
            ],
            bottom: (_progress < 100 && _error == null)
                ? PreferredSize(
                    preferredSize: const Size.fromHeight(2),
                    child: LinearProgressIndicator(
                      value: _progress <= 0 ? null : _progress / 100.0,
                      minHeight: 2,
                      color: s.accent,
                      backgroundColor: s.border,
                    ),
                  )
                : null,
          ),
          body: Column(
            children: [
              // Says what this page IS and who is waiting on it. Without it a
              // third-party login inside a home-automation app reads as a
              // phishing screen, which is a reasonable thing to think.
              Container(
                width: double.infinity,
                padding: const EdgeInsets.fromLTRB(16, 10, 16, 10),
                color: s.surface,
                child: Text(
                  s.pendingWebTitle.toLowerCase().contains('tailscale')
                      ? 'This page belongs to Tailscale, not to your hub. '
                          'Sign in and it will continue on its own.'
                      : 'This page belongs to the device maker, not to your '
                          'hub. Sign in and it will continue on its own.',
                  style: TextStyle(
                      color: s.inkDim, fontSize: 12.5, height: 1.4),
                ),
              ),
              // WHAT THIS PAGE NEEDS, SAID WHILE IT LOADS -- not after it
              // has failed (her call, 2026-08-27: "when the web screen is
              // loading and no internet just display the user has to
              // connect to the internet"). A page that needs the internet
              // on THIS phone, opened from a hub whose own Wi-Fi
              // deliberately has none, will spin and then fail with a
              // generic network error; by then the person is debugging
              // instead of switching networks. The reassurance matters as
              // much as the instruction -- switching does NOT drop the
              // hub, because the house Wi-Fi and the tailnet both reach
              // it too.
              if (s.pendingWebNote.isNotEmpty)
                Container(
                  width: double.infinity,
                  padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
                  decoration: BoxDecoration(
                    color: const Color(0xFFD29922).withValues(alpha: 0.10),
                    border: Border(
                        bottom: BorderSide(
                            color: const Color(0xFFD29922)
                                .withValues(alpha: 0.35))),
                  ),
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Icon(Icons.wifi_tethering,
                          size: 16, color: Color(0xFFD29922)),
                      const SizedBox(width: 10),
                      Expanded(
                        child: Text(
                          s.pendingWebNote,
                          style: TextStyle(
                              color: s.ink, fontSize: 12.5, height: 1.45),
                        ),
                      ),
                    ],
                  ),
                ),
              Expanded(
                child: _error != null
                    ? Center(
                        child: Padding(
                          padding: const EdgeInsets.all(24),
                          child: Column(
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              Icon(Icons.cloud_off,
                                  size: 40, color: s.inkFaint),
                              const SizedBox(height: 14),
                              Text(_error!,
                                  textAlign: TextAlign.center,
                                  style: TextStyle(
                                      color: s.inkDim,
                                      fontSize: 13.5,
                                      height: 1.5)),
                              const SizedBox(height: 18),
                              FilledButton(
                                style: FilledButton.styleFrom(
                                    backgroundColor: s.accent,
                                    foregroundColor: s.accentFg),
                                onPressed: () {
                                  setState(() => _error = null);
                                  _load();
                                },
                                child: const Text('Try again'),
                              ),
                              const SizedBox(height: 8),
                              TextButton(
                                onPressed: _copyLink,
                                child: Text('Copy the link instead',
                                    style: TextStyle(color: s.accent)),
                              ),
                            ],
                          ),
                        ),
                      )
                    : _controller == null
                        ? Center(
                            child: CircularProgressIndicator(
                                color: s.accent, backgroundColor: s.border))
                        : WebViewWidget(controller: _controller!),
              ),
            ],
          ),
        ),
      ),
    );
  }
}