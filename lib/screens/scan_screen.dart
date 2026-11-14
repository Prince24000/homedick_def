/// scan_screen.dart — read the hub's pairing code with the phone's camera.
///
/// ONE JOB: open the camera, wait for a `homevoice://pair?...` QR (the one
/// `setup_manager.py --pair-qr` prints), hand the raw text back to the pair
/// screen, and close. Nothing is parsed or stored here; parsePairCode on the
/// pair screen stays the single authority on what the code means, exactly
/// as it is for a pasted code.
///
/// ON-DEVICE, NO NETWORK. `mobile_scanner` decodes with ML Kit's bundled
/// barcode model on Android and AVFoundation on iOS; frames never leave the
/// phone and no Play Services call is made. Any other QR that wanders into
/// view is ignored -- only our own scheme is accepted, so a random poster
/// cannot be poured into the key box.
///
/// KEPT IN ITS OWN FILE so the dependency is one import in one screen: if
/// the plugin ever has to go again, this file and the one button on the
/// pair screen are the whole footprint.
library;

import 'package:flutter/material.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

import '../app_state.dart';
import 'pair_screen.dart' show PairScreen;

class ScanScreen extends StatefulWidget {
  final AppState state;
  const ScanScreen({super.key, required this.state});

  @override
  State<ScanScreen> createState() => _ScanScreenState();
}

class _ScanScreenState extends State<ScanScreen>
    with SingleTickerProviderStateMixin {
  // DetectionSpeed.normal with our own once-only guard, not noDuplicates:
  // noDuplicates suppresses a code until a DIFFERENT one is seen, and on
  // some Android builds that suppression has swallowed the first report
  // outright ("the camera is not decoding", 2026-09-03). normal reports
  // every frame that has a code; _done makes the first one the only one
  // that matters, and the timeout keeps the stream from flooding.
  final MobileScannerController _controller = MobileScannerController(
    formats: const [BarcodeFormat.qrCode],
    detectionSpeed: DetectionSpeed.normal,
    detectionTimeoutMs: 250,
    torchEnabled: false,
  );

  /// Set the moment a good code is seen, so a second frame of the same QR
  /// cannot pop the route twice.
  bool _done = false;
  bool _torch = false;
  String? _hint;

  /// What the decoder last reported, so a screen that "does nothing" can
  /// say which of three things is happening: no code seen at all, a code
  /// seen that is not ours, or ours (which closes the screen).
  int _seen = 0;

  @override
  void dispose() {
    _sweep.dispose();
    _controller.dispose();
    super.dispose();
  }

  void _onDetect(BarcodeCapture capture) {
    // ONLY WHILE ON TOP. Cancel or Back starts this route's exit, and the
    // camera keeps decoding until dispose: a good code seen in those few
    // hundred milliseconds popped a SECOND route -- the pair screen.
    if (_done || !mounted || !(ModalRoute.of(context)?.isCurrent ?? false)) {
      return;
    }
    for (final code in capture.barcodes) {
      // rawValue is the decoded text; displayValue is ML Kit's tidied
      // copy, which is what some builds fill for URL-typed codes when
      // rawValue comes back empty.
      final raw = (code.rawValue ?? code.displayValue ?? '').trim();
      if (raw.isEmpty) continue;
      _seen++;
      debugPrint('[scan] code #$_seen: ${raw.length} chars, '
          'starts "${raw.substring(0, raw.length < 12 ? raw.length : 12)}"');
      // A PAIRING code, and a complete one: any other homevoice:// link,
      // or a pair code missing its key or with a broken port, used to
      // close the camera and land as an error on the pair screen. The pair
      // screen's own parser decides, so the two can never disagree.
      if (!raw.startsWith('homevoice://pair?') ||
          PairScreen.parsePairCode(raw) == null) {
        // A QR, but not ours. Say so rather than silently staring.
        if (mounted) {
          setState(() => _hint = 'That is not the hub\'s code');
        }
        continue;
      }
      _done = true;
      Navigator.of(context).pop<String>(raw);
      return;
    }
  }

  Future<void> _toggleTorch() async {
    try {
      await _controller.toggleTorch();
      if (mounted) setState(() => _torch = !_torch);
    } catch (_) {
      // No torch on this device; the button simply does nothing.
    }
  }

  // ── THE LOOK (her design, 2026-10-11) ───────────────────────────────
  // No words but "Scan the QR code" and Cancel. The screen is a dark grey
  // (not black) everywhere but the box, which shows the camera; bright
  // corners frame it and a glowing line sweeps it while it looks.
  static const Color _ground = Color(0xFF1B1B1E);
  static const Color _ivory = Color(0xFFFFFFF0);
  static const double _box = 240;

  late final AnimationController _sweep = AnimationController(
      vsync: this, duration: const Duration(milliseconds: 1200))
    ..repeat(reverse: true);

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: _ground,
      body: Stack(
        fit: StackFit.expand,
        children: [
          MobileScanner(
            controller: _controller,
            onDetect: _onDetect,
            fit: BoxFit.cover,
            // A camera that cannot open (permission refused, in use by
            // another app) must say so in words, not show grey.
            errorBuilder: (context, error) => const Center(
              child: Padding(
                padding: EdgeInsets.all(24),
                child: Text(
                  'The camera could not be opened. Allow camera access for '
                  'this app in system settings, or type the key instead.',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: Colors.white70),
                ),
              ),
            ),
          ),
          // Everything but the box, shaded.
          const IgnorePointer(
            child: CustomPaint(painter: _Shade(box: _box, color: _ground)),
          ),
          // The corners and the sweeping line.
          Center(
            child: IgnorePointer(
              child: SizedBox(
                width: _box + 16,
                height: _box + 16,
                child: Stack(children: [
                  const CustomPaint(
                      size: Size(_box + 16, _box + 16),
                      painter: _Corners(color: _ivory)),
                  AnimatedBuilder(
                    animation: _sweep,
                    builder: (_, __) => Positioned(
                      left: 22,
                      right: 22,
                      top: 16 + (_box - 16) * _sweep.value,
                      child: Container(
                        height: 2,
                        decoration: BoxDecoration(
                          color: _ivory,
                          borderRadius: BorderRadius.circular(2),
                          boxShadow: const [
                            BoxShadow(
                                color: Color(0x8CFFFFF0),
                                blurRadius: 14,
                                spreadRadius: 4),
                          ],
                        ),
                      ),
                    ),
                  ),
                ]),
              ),
            ),
          ),
          SafeArea(
            child: Column(
              children: [
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 8),
                  child: Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      IconButton(
                        tooltip: 'Back',
                        icon: const Icon(Icons.arrow_back, color: Colors.white),
                        onPressed: () =>
                            Navigator.of(context).pop<String>(null),
                      ),
                      IconButton(
                        tooltip: _torch ? 'Torch off' : 'Torch on',
                        icon: Icon(
                            _torch
                                ? Icons.flash_off_rounded
                                : Icons.flash_on_rounded,
                            color: Colors.white),
                        onPressed: _toggleTorch,
                      ),
                    ],
                  ),
                ),
                const Spacer(),
                // The one line: what to do -- or, when a code was seen that
                // is not the hub's, that it is not.
                Text(_hint ?? 'Scan the QR code',
                    textAlign: TextAlign.center,
                    style: const TextStyle(
                        color: Color(0xFFF7F3E8),
                        fontSize: 15,
                        fontWeight: FontWeight.w500)),
                const SizedBox(height: 20),
                OutlinedButton(
                  style: OutlinedButton.styleFrom(
                    foregroundColor: const Color(0xFFF85149),
                    side: const BorderSide(color: Color(0xFFF85149), width: 1.5),
                    minimumSize: const Size(140, 48),
                    shape: const StadiumBorder(),
                  ),
                  onPressed: () => Navigator.of(context).pop<String>(null),
                  child: const Text('Cancel',
                      style: TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
                ),
                const SizedBox(height: 40),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// The ground colour over the whole screen but a rounded square in the
/// middle: the camera shows only through the box.
class _Shade extends CustomPainter {
  const _Shade({required this.box, required this.color});
  final double box;
  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final hole = RRect.fromRectAndRadius(
        Rect.fromCenter(
            center: size.center(Offset.zero), width: box, height: box),
        const Radius.circular(18));
    final path = Path()
      ..fillType = PathFillType.evenOdd
      ..addRect(Offset.zero & size)
      ..addRRect(hole);
    canvas.drawPath(path, Paint()..color = color.withValues(alpha: 0.94));
  }

  @override
  bool shouldRepaint(_Shade old) => old.box != box || old.color != color;
}

/// Four bright rounded corners around the box.
class _Corners extends CustomPainter {
  const _Corners({required this.color});
  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    const len = 34.0, r = 22.0, w = 4.0;
    final p = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = w
      ..strokeCap = StrokeCap.round;
    const h = w / 2;
    final right = size.width - h, bottom = size.height - h;
    void corner(double x, double y, double dx, double dy) {
      final path = Path()
        ..moveTo(x, y + dy * len)
        ..lineTo(x, y + dy * r)
        ..arcToPoint(Offset(x + dx * r, y),
            radius: const Radius.circular(r), clockwise: dx * dy > 0)
        ..lineTo(x + dx * len, y);
      canvas.drawPath(path, p);
    }

    corner(h, h, 1, 1);
    corner(right, h, -1, 1);
    corner(h, bottom, 1, -1);
    corner(right, bottom, -1, -1);
  }

  @override
  bool shouldRepaint(_Corners old) => old.color != color;
}
