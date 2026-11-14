import 'dart:math';
import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';
import '../app_state.dart';
import '../flow/tts.dart' show Voice;

/// THE ORB, IN FIVE MOVES (her design, 2026-10-08 -- orb_flow_final.html):
///
///   idle       the solid orb, bobbing, as it always was;
///   listening  it opens into a turning globe of dots that ripples;
///   thinking   the globe shrinks and square circuit lines grow out of it,
///              pulses running in along them;
///   speaking   the dots rush into the orb and soft filled waves go out
///              from it, one per word as the voice says it (her design,
///              2026-10-11: "the circular waves it was before... the
///              waves follow how it speaks"). Filled, never rings round a
///              dark middle: that read as an eye.
///
/// Behind it all, a soft glow in the orb's own colour, in both modes.
///   done       the small orb grows back into the idle orb.
///
/// One set of dots is moved between those shapes, so every change is a
/// morph and never a cut. Drawn by one painter off one ticker.
///
/// `isActive` alone (the enrolment screen) still means "a capture is open"
/// and draws the globe; `isSpeaking` picks the speaking shape.
class Orb extends StatefulWidget {
  final AppState appState;
  final bool showCancel;
  final VoidCallback? onCancel;
  final String? phrase;
  final bool isThinking;
  final bool isActive;
  final bool isSpeaking;
  final VoidCallback? onTap;

  /// Its width and height. 140 beside the home cards; 170 when they are
  /// hidden and it has the middle of the screen (her design, 2026-10-11).
  final double size;

  const Orb({
    super.key,
    required this.appState,
    this.showCancel = false,
    this.onCancel,
    this.phrase,
    this.isThinking = false,
    this.isActive = false,
    this.isSpeaking = false,
    this.onTap,
    this.size = 140,
  });

  @override
  State<Orb> createState() => _OrbState();
}

enum _Mode { idle, listening, thinking, speaking }

class _Dot {
  _Dot(this.gx, this.gy, this.gz, this.x, this.y);
  final double gx, gy, gz; // its place on the unit globe
  double x, y; // where it is drawn now
  double a = 0; // how visible
  bool front = true;
}

class _Trace {
  _Trace(this.angle, this.horiz, this.l1, this.l2, this.l3, this.phase);
  final double angle, l1, l2, l3, phase;
  final bool horiz;
}

/// Everything that moves. The painter only reads it.
class _OrbSim extends ChangeNotifier {
  static const double box = 140, cx = 70, cy = 70, orb = 40;

  /// Dots on the globe. Dropped to [_fewDots] by itself on a phone that
  /// cannot keep the frame rate with all of them.
  static const int _allDots = 200, _fewDots = 120;
  final List<_Dot> dots = [];
  final List<_Trace> traces = [];
  final Random _rnd = Random(7);

  _Mode mode = _Mode.idle;
  double modeT = 0; // seconds in this mode
  double now = 0; // seconds since start
  double spin = 0, speed = 0.4;
  double orbA = 1, orbR = orb;
  double traceA = 0;
  int dotCount = _allDots;

  /// Waves going out while it speaks: [born (s), strength 0..1].
  final List<List<double>> waves = [];
  double _lastBeat = -9;
  static const double waveLife = 1.2;

  /// A word began: one wave, stronger for a longer word.
  void beat(int letters) {
    if (mode != _Mode.speaking) return;
    _lastBeat = now;
    waves.add([now, 0.55 + 0.45 * min(1.0, letters / 8)]);
  }

  _OrbSim() {
    for (var i = 0; i < _allDots; i++) {
      final y = 1 - (i / (_allDots - 1)) * 2;
      final r = sqrt(max(0.0, 1 - y * y));
      final th = i * 2.39996;
      final ang = _rnd.nextDouble() * pi * 2;
      dots.add(_Dot(cos(th) * r, y, sin(th) * r, cx + cos(ang) * orb * 0.3,
          cy + sin(ang) * orb * 0.3));
    }
    // Square circuit lines: straight out along the nearer axis, a
    // 90-degree step, straight out again -- traces leaving a chip.
    for (var i = 0; i < 14; i++) {
      final a = i / 14 * pi * 2 + 0.11;
      traces.add(_Trace(a, cos(a).abs() > sin(a).abs(), 7.0 + (i * 7) % 10,
          5.0 + (i * 5) % 9, 8.0 + (i * 11) % 12, (i * 0.37) % 1));
    }
  }

  /// The voice, as the animation moves to it. Shaped like speech -- bursts
  /// and gaps -- rather than read from the microphone.
  double get voice =>
      0.5 + 0.5 * (sin(now / 0.38)).abs() * (sin(now / 0.15)).abs();
  double get talk =>
      0.35 + 0.65 * (sin(now / 0.17)).abs() * (sin(now / 0.43)).abs();

  /// `k` per 60 Hz frame, made the same per second at any frame rate.
  static double _ease(double k, double dt) => 1 - pow(1 - k, dt * 60).toDouble();

  void step(double dt, _Mode m) {
    now += dt;
    if (m != mode) {
      mode = m;
      modeT = 0;
    } else {
      modeT += dt;
    }

    final speaking = mode == _Mode.speaking;
    final wantOrbA = speaking
        ? ((modeT - 0.15) / 0.35).clamp(0.0, 1.0)
        : mode == _Mode.idle
            ? 1.0
            : mode == _Mode.listening
                ? 0.12
                : 0.0;
    final wantOrbR = speaking
        ? orb * 0.8
        : (mode == _Mode.listening || mode == _Mode.thinking)
            ? orb * 0.5
            : orb;
    orbA += (wantOrbA - orbA) * _ease(speaking ? 0.25 : 0.08, dt);
    orbR += (wantOrbR - orbR) * _ease(mode == _Mode.idle ? 0.06 : 0.1, dt);

    traceA += ((mode == _Mode.thinking ? 1.0 : 0.0) - traceA) * _ease(0.1, dt);

    // An engine that reports no words still speaks in waves: a speech-
    // shaped beat while none has come for a moment.
    if (speaking && modeT > 0.3 && now - _lastBeat > 0.45) {
      _lastBeat = now;
      waves.add([now, 0.35 + 0.5 * talk]);
    }
    waves.removeWhere((w) => now - w[0] > waveLife);

    final wantSpeed = mode == _Mode.listening
        ? 0.55
        : mode == _Mode.thinking
            ? 1.6
            : 0.2;
    speed += (wantSpeed - speed) * _ease(0.05, dt);
    spin += speed * 0.02 * dt * 60;

    final v = voice;
    final scan = sin(now / 0.26);
    final pull = _ease(speaking ? 0.28 : 0.1, dt);
    final fade = _ease(speaking ? 0.3 : 0.1, dt);
    for (var i = 0; i < dotCount; i++) {
      final p = dots[i];
      double tx, ty, ta;
      if (mode == _Mode.listening || mode == _Mode.thinking) {
        final x = p.gx * cos(spin) + p.gz * sin(spin);
        final z = -p.gx * sin(spin) + p.gz * cos(spin);
        final r = mode == _Mode.listening
            ? orb * 1.55 * (1 + 0.07 * v * sin(p.gy * 7 - now / 0.12))
            : orb * 0.9;
        tx = cx + x * r;
        ty = cy + p.gy * r;
        ta = 0.25 + 0.75 * (z + 1) / 2;
        if (mode == _Mode.thinking && (p.gy - scan).abs() < 0.07) ta = 1;
        p.front = z > 0;
      } else if (speaking) {
        // Every dot rushes into the small orb, and is gone as it arrives.
        tx = cx + p.gx * orb * 0.35;
        ty = cy + p.gy * orb * 0.35;
        final away = sqrt(pow(p.x - cx, 2) + pow(p.y - cy, 2));
        ta = away > orb * 0.45 && modeT < 0.6 ? 1 : 0;
        p.front = true;
      } else {
        tx = cx + p.gx * orb * 0.3;
        ty = cy + p.gy * orb * 0.3;
        ta = 0;
        p.front = true;
      }
      p.x += (tx - p.x) * pull;
      p.y += (ty - p.y) * pull;
      p.a += (ta - p.a) * fade;
    }
    notifyListeners();
  }

  // ── the frame-rate guard ──
  double _slowFor = 0;
  void watch(double dt) {
    if (dotCount == _fewDots || mode == _Mode.idle) return;
    // 26 ms is under 40 frames a second: count how long it stays there.
    _slowFor = dt > 0.026 ? _slowFor + dt : max(0.0, _slowFor - dt);
    if (_slowFor > 2.0) {
      dotCount = _fewDots;
      debugPrint('[orb] frames are slow on this phone -- '
          'drawing $_fewDots dots instead of $_allDots');
    }
  }

  List<Offset> tracePoints(_Trace t, double rg) {
    final x0 = cx + cos(t.angle) * rg, y0 = cy + sin(t.angle) * rg;
    final sx = cos(t.angle) >= 0 ? 1.0 : -1.0;
    final sy = sin(t.angle) >= 0 ? 1.0 : -1.0;
    if (t.horiz) {
      final x1 = x0 + sx * t.l1, y2 = y0 + sy * t.l2;
      return [Offset(x0, y0), Offset(x1, y0), Offset(x1, y2),
        Offset(x1 + sx * t.l3, y2)];
    }
    final y1 = y0 + sy * t.l1, x2 = x0 + sx * t.l2;
    return [Offset(x0, y0), Offset(x0, y1), Offset(x2, y1),
      Offset(x2, y1 + sy * t.l3)];
  }

  /// The first `u` (0..1) of a trace, as a path.
  static Path partial(List<Offset> pts, double u) {
    final path = Path()..moveTo(pts[0].dx, pts[0].dy);
    final seg = <double>[];
    var total = 0.0;
    for (var i = 1; i < pts.length; i++) {
      final l = (pts[i] - pts[i - 1]).distance;
      seg.add(l);
      total += l;
    }
    var d = u.clamp(0.0, 1.0) * total;
    for (var i = 0; i < seg.length; i++) {
      if (d >= seg[i]) {
        path.lineTo(pts[i + 1].dx, pts[i + 1].dy);
        d -= seg[i];
        continue;
      }
      final e = Offset.lerp(pts[i], pts[i + 1], seg[i] == 0 ? 0.0 : d / seg[i])!;
      path.lineTo(e.dx, e.dy);
      break;
    }
    return path;
  }

  static Offset along(List<Offset> pts, double u) {
    final seg = <double>[];
    var total = 0.0;
    for (var i = 1; i < pts.length; i++) {
      final l = (pts[i] - pts[i - 1]).distance;
      seg.add(l);
      total += l;
    }
    var d = u.clamp(0.0, 1.0) * total;
    for (var i = 0; i < seg.length; i++) {
      if (d <= seg[i] || i == seg.length - 1) {
        final f = seg[i] == 0 ? 0.0 : (d / seg[i]).clamp(0.0, 1.0);
        return Offset.lerp(pts[i], pts[i + 1], f)!;
      }
      d -= seg[i];
    }
    return pts.last;
  }
}

class _OrbPainter extends CustomPainter {
  _OrbPainter(this.sim, this.color, this.glow) : super(repaint: sim);
  final _OrbSim sim;
  final Color color;

  /// How strong the glow is: softer on a white page than on a dark one.
  final double glow;

  @override
  void paint(Canvas canvas, Size size) {
    // The simulation is laid out in a 140 x 140 box; scale to what we got.
    final k = size.width / _OrbSim.box;
    canvas.save();
    canvas.scale(k, k);
    const cx = _OrbSim.cx, cy = _OrbSim.cy, orb = _OrbSim.orb;
    final mode = sim.mode;

    // THINKING: square circuit lines grow out of the globe, pulses run in.
    if (sim.traceA > 0.02) {
      final grow = mode == _Mode.thinking
          ? (sim.modeT / 0.7).clamp(0.0, 1.0)
          : 1.0;
      final line = Paint()
        ..color = color.withValues(alpha: 0.5 * sim.traceA)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.1
        ..strokeJoin = StrokeJoin.miter;
      final pad = Paint()..color = color.withValues(alpha: 0.8 * sim.traceA);
      for (final t in sim.traces) {
        final pts = sim.tracePoints(t, orb * 0.95);
        // Drawn only as far as it has grown.
        final path = _OrbSim.partial(pts, grow);
        final end = _OrbSim.along(pts, grow);
        canvas.drawPath(path, line);
        canvas.drawCircle(end, 2.0, pad);
        if (mode == _Mode.thinking && grow >= 1) {
          final ph = (sim.now / 1.1 + t.phase) % 1;
          for (var j = 0; j < 6; j++) {
            // `1.0 -`, and max<double>: `1 - max(...)` is inferred as num.
            final double u = 1.0 - max<double>(0.0, ph - j * 0.015);
            canvas.drawCircle(
                _OrbSim.along(pts, u),
                2.0 - j * 0.25,
                Paint()..color = color.withValues(alpha: (1 - j / 6) * 0.95));
          }
        }
      }
    }

    // THE DOTS -- the globe, its turn, the rush into the small orb.
    final front = Paint();
    final back = Paint();
    for (var i = 0; i < sim.dotCount; i++) {
      final p = sim.dots[i];
      if (p.a < 0.02) continue;
      if (p.front) {
        front.color = color.withValues(alpha: p.a);
        canvas.drawCircle(Offset(p.x, p.y), 1.5, front);
      } else {
        back.color = color.withValues(alpha: p.a * 0.4);
        canvas.drawCircle(Offset(p.x, p.y), 1.0, back);
      }
    }

    // SPEAKING: soft filled waves going out from the orb, one per word.
    if (sim.orbA > 0.02) {
      final wave = Paint();
      for (final w in sim.waves) {
        final p = ((sim.now - w[0]) / _OrbSim.waveLife).clamp(0.0, 1.0);
        final r = sim.orbR + p * (18 + 14 * w[1]);
        wave.color = color.withValues(alpha: (1 - p) * 0.32 * w[1] * sim.orbA);
        canvas.drawCircle(const Offset(cx, cy), r, wave);
      }
    }

    // THE ORB: full when idle, a faint core while listening, gone while
    // thinking, small while speaking, growing back afterwards.
    if (sim.orbA > 0.01) {
      final bob = mode == _Mode.idle ? sin(sim.now / 0.4) * 4 : 0.0;
      // THE GLOW behind it, in its own colour, fading with it.
      canvas.drawCircle(
          Offset(cx, cy + bob),
          sim.orbR + 4,
          Paint()
            ..color = color.withValues(alpha: glow * sim.orbA)
            ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 12));
      canvas.drawCircle(Offset(cx, cy + bob), sim.orbR,
          Paint()..color = color.withValues(alpha: sim.orbA));
    }
    canvas.restore();
  }

  @override
  bool shouldRepaint(_OrbPainter old) =>
      old.color != color || old.sim != sim || old.glow != glow;
}

class _OrbState extends State<Orb> with SingleTickerProviderStateMixin {
  final _OrbSim _sim = _OrbSim();
  late final Ticker _ticker;
  Duration _last = Duration.zero;

  @override
  void initState() {
    super.initState();
    _ticker = createTicker(_tick)..start();
    Voice.wordStarts.addListener(_onWord);
  }

  void _onWord() => _sim.beat(Voice.lastWordLength);

  _Mode get _mode {
    if (widget.isThinking) return _Mode.thinking;
    if (widget.isSpeaking) return _Mode.speaking;
    if (widget.isActive) return _Mode.listening;
    return _Mode.idle;
  }

  void _tick(Duration elapsed) {
    // Capped, so a frame after a pause (the app in the background) does
    // not jump every dot to the end of its move.
    final dt = ((elapsed - _last).inMicroseconds / 1e6).clamp(0.0, 0.05);
    _last = elapsed;
    _sim.watch(dt);
    _sim.step(dt, _mode);
  }

  @override
  void dispose() {
    Voice.wordStarts.removeListener(_onWord);
    _ticker.dispose();
    _sim.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final color = widget.appState.orbColor;

    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (widget.phrase != null) ...[
          Text(
            widget.phrase!,
            textAlign: TextAlign.center,
            style: TextStyle(
              color: widget.appState.ink,
              fontSize: 20,
              fontWeight: FontWeight.w600,
              height: 1.3,
            ),
          ),
          const SizedBox(height: 6),
          // Seen, not only heard: cancel can be said here too.
          Text('say it, or say "cancel"',
              style: TextStyle(
                  color: widget.appState.inkDim, fontSize: 12.5)),
          const SizedBox(height: 18),
        ],
        Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            GestureDetector(
              onTap: widget.onTap,
              // A new size GLIDES (the cards folding, speaking): never a jump.
              child: TweenAnimationBuilder<double>(
                tween: Tween<double>(end: widget.size),
                duration: const Duration(milliseconds: 320),
                curve: Curves.easeInOutCubic,
                builder: (_, size, child) =>
                    SizedBox(width: size, height: size, child: child),
                child: RepaintBoundary(
                  child: CustomPaint(
                    painter: _OrbPainter(
                        _sim, color, widget.appState.isLight ? 0.22 : 0.34),
                  ),
                ),
              ),
            ),
            if (widget.showCancel) ...[
              const SizedBox(width: 20),
              _buildCancel(),
            ],
          ],
        ),
      ],
    );
  }

  Widget _buildCancel() {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        onTap: widget.onCancel,
        borderRadius: BorderRadius.circular(28),
        child: Container(
          width: 56,
          height: 56,
          decoration: const BoxDecoration(
            color: Color(0xFFF85149),
            shape: BoxShape.circle,
          ),
          child: const Icon(Icons.close, color: Colors.white, size: 28),
        ),
      ),
    );
  }
}
