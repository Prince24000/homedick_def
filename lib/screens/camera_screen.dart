/// camera_screen.dart — one camera's live feed, from Home Assistant.
///
/// THE FEED NEVER TOUCHES THE HUB. The hub reads the camera's own rotating
/// stream token from Home Assistant and sends `open camera` with two URLs:
/// an MJPEG stream and a JPEG snapshot, both on HA's camera proxy and both
/// scoped to that one camera for a while. This screen shows the stream in
/// a WebView -- the same engine WebScreen uses for sign-in pages, under
/// the same policy (https anywhere, plain http only inside the house;
/// AppState._handleOpen refuses anything else before this screen ever
/// sees it) -- wrapped in a black page so the picture, not a browser
/// default, is what you look at.
///
/// WHEN THE TOKEN ROTATES the stream stops; the page reports the broken
/// image through a JavaScript channel and the screen offers Reconnect,
/// which asks the hub for a fresh URL (a menu action, gated as a secure
/// device when the camera was registered as one). Nothing is cached and
/// nothing is stored: leaving the screen forgets the URLs.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:webview_flutter/webview_flutter.dart';

import '../app_state.dart';

class CameraScreen extends StatefulWidget {
  final AppState appState;
  const CameraScreen({super.key, required this.appState});

  @override
  State<CameraScreen> createState() => _CameraScreenState();
}

class _CameraScreenState extends State<CameraScreen> {
  WebViewController? _controller;
  int _shownSeq = -1;
  bool _live = true;
  bool _stopped = false;
  bool _asking = false;

  @override
  void initState() {
    super.initState();
    widget.appState.addListener(_onState);
    _load();
  }

  @override
  void dispose() {
    widget.appState.removeListener(_onState);
    // Drop the MJPEG connection now rather than when the platform view is
    // collected, and forget the URLs whichever way the screen was left.
    _controller
        ?.loadHtmlString('<html><body style="background:#000"></body></html>')
        .catchError((_) {}); // the platform view may already be gone
    widget.appState.pendingCamera = null;
    super.dispose();
  }

  Map<String, String>? get _cam => widget.appState.pendingCamera;

  /// A fresh `open camera` from the hub (a Reconnect answered) swaps the
  /// feed in place.
  void _onState() {
    if (!mounted) return;
    if (_cam != null && widget.appState.cameraSeq != _shownSeq) {
      _asking = false;
      _load();
    } else if (_asking && !widget.appState.menuBusy) {
      // The hub answered without a new feed (refused, or HA down); the
      // status line carries its words. Stop spinning.
      setState(() => _asking = false);
    }
  }

  void _load() {
    final cam = _cam;
    if (cam == null) return;
    final url = _live ? cam['url']! : _snapshotUrl(cam);
    if (url.isEmpty) return;
    final controller = _controller ?? _newController();
    controller.loadHtmlString(_page(url));
    setState(() {
      _controller = controller;
      _shownSeq = widget.appState.cameraSeq;
      _stopped = false;
    });
  }

  String _snapshotUrl(Map<String, String> cam) {
    final snap = cam['snapshot'] ?? '';
    if (snap.isEmpty) return '';
    // A cache-buster, so "Snapshot" pressed twice shows two moments.
    final sep = snap.contains('?') ? '&' : '?';
    return '$snap${sep}t=${DateTime.now().millisecondsSinceEpoch}';
  }

  WebViewController _newController() {
    final c = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setBackgroundColor(Colors.black)
      ..addJavaScriptChannel('hv', onMessageReceived: (msg) {
        if (msg.message == 'error' && mounted) {
          setState(() => _stopped = true);
        }
      })
      ..setNavigationDelegate(NavigationDelegate(
        // NOTHING NAVIGATES. This page is one image; a stream URL that
        // tried to send the WebView somewhere else is refused outright.
        onNavigationRequest: (req) =>
            (req.url.startsWith('about:') || req.url.startsWith('data:'))
                ? NavigationDecision.navigate
                : NavigationDecision.prevent,
        // Main frame only: on Android a replaced <img> reports itself
        // as a cancelled subresource AFTER the new page has loaded, and
        // that would flip a healthy feed to "stopped". The <img onerror>
        // channel already covers every way the picture itself can fail.
        onWebResourceError: (e) {
          if (e.isForMainFrame == true && mounted) {
            setState(() => _stopped = true);
          }
        },
      ));
    return c;
  }

  /// The wrapper page: black, the picture fitted to the screen, and an
  /// onerror that tells Dart the stream died. The URL is placed as an
  /// attribute with the only two characters that could break out of it
  /// escaped, so a URL from the hub cannot inject script here.
  static String _page(String url) {
    final safe = url
        .replaceAll('&', '&amp;')
        .replaceAll('"', '&quot;')
        .replaceAll('<', '&lt;');
    return '''
<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<style>html,body{margin:0;height:100%;background:#000;overflow:hidden}
img{width:100%;height:100%;object-fit:contain;display:block}</style></head>
<body><img src="$safe" onerror="hv.postMessage('error')"></body></html>''';
  }

  void _reconnect() {
    final did = _cam?['device_id'] ?? '';
    if (did.isEmpty) return;
    // Only spin when a request actually went out; a hub that is busy
    // answers with a toast, not a frame, and nothing would clear it.
    if (widget.appState.viewCamera(did)) setState(() => _asking = true);
  }

  void _toggleLive() {
    setState(() => _live = !_live);
    _load();
  }

  void _close() {
    widget.appState.closeCamera();
    Navigator.of(context).maybePop();
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.appState;
    final cam = _cam;
    final title = cam?['title'] ?? 'Camera';
    final state = cam?['state'] ?? '';
    final hasSnapshot = (cam?['snapshot'] ?? '').isNotEmpty;
    return AnnotatedRegion<SystemUiOverlayStyle>(
      value: SystemUiOverlayStyle.light.copyWith(
        statusBarColor: Colors.black,
        systemNavigationBarColor: Colors.black,
      ),
      // System back leaves the same way the arrow does.
      child: PopScope(
        onPopInvokedWithResult: (didPop, _) {
          if (didPop) widget.appState.closeCamera();
        },
        child: Scaffold(
        backgroundColor: Colors.black,
        appBar: AppBar(
          backgroundColor: Colors.black,
          foregroundColor: Colors.white,
          elevation: 0,
          leading: IconButton(
            icon: const Icon(Icons.arrow_back),
            onPressed: _close,
          ),
          title: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(title,
                  style: const TextStyle(
                      fontSize: 16, fontWeight: FontWeight.w600)),
              Text(
                _stopped
                    ? 'stream stopped'
                    : (_live ? 'live' : 'snapshot') +
                        (state.isNotEmpty ? ' · $state' : ''),
                style: TextStyle(
                    fontSize: 11,
                    color: _stopped ? s.cancelInk : Colors.white54),
              ),
            ],
          ),
          actions: [
            if (hasSnapshot)
              IconButton(
                tooltip: _live ? 'Take a snapshot' : 'Back to live',
                icon: Icon(_live
                    ? Icons.photo_camera_outlined
                    : Icons.play_circle_outline),
                onPressed: cam == null ? null : _toggleLive,
              ),
            IconButton(
              tooltip: 'Reconnect',
              icon: const Icon(Icons.refresh),
              onPressed: cam == null || _asking ? null : _reconnect,
            ),
          ],
        ),
        body: cam == null
            ? const Center(
                child: Text('No camera to show.',
                    style: TextStyle(color: Colors.white70)))
            : Stack(
                fit: StackFit.expand,
                children: [
                  if (_controller != null)
                    WebViewWidget(controller: _controller!),
                  if (_stopped || _asking)
                    Container(
                      color: Colors.black87,
                      alignment: Alignment.center,
                      padding: const EdgeInsets.all(28),
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          if (_asking)
                            const SizedBox(
                              width: 28,
                              height: 28,
                              child: CircularProgressIndicator(
                                  strokeWidth: 2.5, color: Colors.white70),
                            )
                          else
                            const Icon(Icons.videocam_off_outlined,
                                color: Colors.white70, size: 40),
                          const SizedBox(height: 14),
                          Text(
                            _asking
                                ? 'Asking the hub for a fresh feed…'
                                : 'The stream stopped.\nIts key has probably '
                                    'rotated, or this phone is not on the '
                                    'house network.',
                            textAlign: TextAlign.center,
                            style: const TextStyle(
                                color: Colors.white70, fontSize: 13.5,
                                height: 1.4),
                          ),
                          if (!_asking) ...[
                            const SizedBox(height: 16),
                            FilledButton.icon(
                              onPressed: _reconnect,
                              icon: const Icon(Icons.refresh),
                              label: const Text('Reconnect'),
                            ),
                          ],
                        ],
                      ),
                    ),
                ],
              ),
        ),
      ),
    );
  }
}