import 'package:flutter/material.dart';
import 'app_state.dart';
import 'widgets/busy_card.dart';
// FIX: initForegroundService() is called here but was never imported.
import 'service/foreground.dart';
import 'screens/home_screen.dart';
import 'screens/enroll_screen.dart';
import 'screens/prompt_screen.dart';
import 'screens/choice_screen.dart';
import 'screens/devices_screen.dart';
import 'screens/form_screen.dart';
import 'screens/notifications_screen.dart';
import 'screens/pin_map_screen.dart';
import 'screens/profile_screen.dart';
import 'screens/scenes_screen.dart';
import 'screens/settings_screen.dart';
import 'screens/statistics_screen.dart';
import 'screens/auth_screen.dart';
import 'screens/automations_screen.dart';
import 'screens/working_screen.dart';
import 'screens/web_screen.dart';
import 'screens/camera_screen.dart';
import 'screens/registration_screen.dart';
import 'widgets/voice_dock.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await initForegroundService();

  final appState = AppState();
  // A notice's Details opens the Remote access sheet where the person is.
  appState.remoteAccessOpener = () {
    final ctx = appState.navigatorKey.currentContext;
    if (ctx != null) showRemoteAccess(ctx, appState);
  };
  // FIX: Don't let an init exception kill the app silently.
  try {
    await appState.init();
  } catch (e, st) {
    debugPrint('AppState.init failed: $e\n$st');
  }

  runApp(HomeVoiceApp(appState: appState));
}

class HomeVoiceApp extends StatefulWidget {
  final AppState appState;
  const HomeVoiceApp({super.key, required this.appState});

  @override
  State<HomeVoiceApp> createState() => _HomeVoiceAppState();
}

class _HomeVoiceAppState extends State<HomeVoiceApp> {
  // ONE THEME PER MODE, made once (2026-11-07). It was rebuilt on every
  // change of state -- many times a second while the voice dock listens --
  // and each new theme re-ran the theme animation under every open text
  // field while it was being typed in.
  ThemeData? _theme;
  bool? _themeLight;
  ThemeData _themeFor(AppState s) {
    final cached = _theme;
    if (cached != null && _themeLight == s.isLight) return cached;
    _themeLight = s.isLight;
    return _theme = ThemeData(
      brightness: s.isLight ? Brightness.light : Brightness.dark,
      useMaterial3: true,
      colorScheme: ColorScheme.fromSeed(
        // The teal seeds BOTH modes: seeding dark with the ivory
        // accent would wash every derived tone to grey. Explicit
        // accent colours on the widgets stay exactly as they are.
        seedColor: const Color(0xFF00695C),
        brightness: s.isLight ? Brightness.light : Brightness.dark,
      ),
      textSelectionTheme: TextSelectionThemeData(
        cursorColor: s.accent,
        selectionColor: s.accent.withValues(alpha: 0.30),
        selectionHandleColor: s.accent,
      ),
    );
  }

  // ONE observer instance for the app's lifetime — a NavigatorObserver may
  // not be shared, and recreating it per rebuild would churn attachment.
  late final _RouteTracker _routeTracker = _RouteTracker(widget.appState);

  @override
  void dispose() {
    widget.appState.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.appState;
    return ListenableBuilder(
      listenable: widget.appState,
      builder: (context, _) => MaterialApp(
        title: 'Omniscience',
        debugShowCheckedModeBanner: false,
        // SEEDED FROM HER PALETTE, BOTH MODES. The bare
        // ThemeData(brightness: dark) left every unstyled Material control
        // on the M3 default blue/purple — the pairing overlay's blue
        // button, blue text cursors and selection handles in every field
        // ("the pair phone should carry the theme not blue botton", field
        // test 2026-08-27). Screens still paint their own colours; this is
        // the net under them, so nothing that slips through ever reads as
        // someone else's app. Brightness follows the mode switch too — the
        // old hardcoded dark meant M3 defaults ignored light mode.
        theme: _themeFor(s),
        navigatorKey: widget.appState.navigatorKey,
        // Toasts ("Done -- kitchen is settled.") need a messenger that
        // outlives whichever screen asked for them.
        scaffoldMessengerKey: widget.appState.scaffoldMessengerKey,
        navigatorObservers: [_routeTracker],
        initialRoute: '/',
        routes: {
          '/': (context) => HomeScreen(appState: widget.appState),
          '/enroll': (context) => EnrollScreen(appState: widget.appState),
          '/prompt': (context) => PromptScreen(appState: widget.appState),
          '/choice': (context) => ChoiceScreen(appState: widget.appState),
          '/devices': (context) => DevicesScreen(appState: widget.appState),
          '/scenes': (context) => ScenesScreen(appState: widget.appState),
          '/form': (context) => FormScreen(appState: widget.appState),
          // The registration screen (add/edit, board or HA).
          '/registration': (context) =>
              RegistrationScreen(appState: widget.appState),
          '/notifications': (context) =>
              NotificationsScreen(appState: widget.appState),
          '/pins': (context) => PinMapScreen(appState: widget.appState),
          '/web': (context) => WebScreen(appState: widget.appState),
          '/camera': (context) => CameraScreen(appState: widget.appState),
          '/profile': (context) => ProfileScreen(appState: widget.appState),
          '/settings': (context) =>
              SettingsScreen(appState: widget.appState),
          '/stats': (context) =>
              StatisticsScreen(appState: widget.appState),
          '/auth': (context) => AuthScreen(appState: widget.appState),
          // ONE ROUTE for both faces (her merge, 2026-08-27): the list
          // and the blocks are the same screen, so there is no '/builder'
          // to push and nothing to go back through between them.
          '/automations': (context) =>
              AutomationsScreen(appState: widget.appState),
          '/working': (context) =>
              WorkingScreen(appState: widget.appState),
        },
        // The voice dock lives ABOVE the Navigator: rendered once, follows
        // every route, and no screen can forget to include it.
        //
        // The busy overlay is here for the same reason. The hub can start
        // waiting during ANY flow, so a per-screen spinner would mean every
        // future screen has to remember to draw one. One place, above the
        // Navigator, and none of them can forget.
        builder: (context, child) => Stack(
          children: [
            // The GROUND behind everything is the theme's own background.
            // Without it, the dock's reserved strip showed whatever
            // Flutter paints behind the Navigator -- a slightly different
            // black, with a visible seam where the screen ended ("there
            // shouldnt be any demacation", field test 2026-08-25). One
            // ground colour, no seam.
            Positioned.fill(child: ColoredBox(color: widget.appState.bg)),
            // The dock's strip is RESERVED, not overlaid: screens end
            // where the dock begins, so it can never hover over the last
            // rows of a list ("don't let it hover any info" -- field
            // test 2026-08-25). On routes without the dock the padding
            // collapses and the screen gets the full height back.
            if (child != null)
              AnimatedPadding(
                duration: const Duration(milliseconds: 150),
                padding: EdgeInsets.only(
                    bottom: VoiceDock.reservesSpace(widget.appState)
                        ? VoiceDock.reservedHeight
                        : 0),
                child: child,
              ),
            // NOT on the Working screen or the Question screen: both
            // render this same card inline as their own body, and a
            // popup over a copy of itself -- over a scroll of esptool
            // chatter -- is the screenshot this fixed. Everywhere else,
            // the overlay is the one place.
            // NEVER OVER A LIVE QUESTION: the card absorbs taps, so a
            // question the hub asks mid-job (a port, the Wi-Fi) could
            // not be answered under it.
            if (widget.appState.busyText != null &&
                !widget.appState.questionLive &&
                widget.appState.topRoute != '/prompt' &&
                widget.appState.topRoute != '/working')
              _BusyOverlay(appState: widget.appState),
            VoiceDock(appState: widget.appState),
          ],
        ),
      ),
    );
  }
}

/// "The hub is waiting, and here is what for."
///
/// A DETERMINATE BAR when the hub says how long, an indeterminate one when
/// it does not. That distinction is the whole point of the widget: a bounded
/// wait rendered as a spinner looks identical at second one and second
/// twenty, which is how twenty-five honest seconds come to feel like a hang.
/// The bar fills locally from a duration sent once, so nothing streams over
/// the socket to move it.
///
/// It absorbs taps, and that is not a UI opinion — while this is up, the hub
/// is holding its shell lock, so anything you started would come back
/// "busy". Blocking here reflects what is actually true rather than letting
/// you discover it by being refused.
///
/// It is NOT dismissible. It goes when the hub says so, or when the
/// connection drops and _clearPending takes it down. A spinner the user can
/// dismiss while the server carries on is a lie about who is in control.
///
/// The card itself lives in busy_card.dart, shared with the Working screen
/// -- one card, drawn in the app's own palette, wrapped in the Material
/// that keeps its text from rendering in Flutter's yellow-double-underline
/// error style (which is what "no Material ancestor" looks like, and what
/// the first live flash screen showed).
class _BusyOverlay extends StatelessWidget {
  final AppState appState;
  const _BusyOverlay({required this.appState});

  @override
  Widget build(BuildContext context) {
    return Positioned.fill(
      child: AbsorbPointer(
        child: Container(
          color: Colors.black.withValues(alpha: 0.55),
          alignment: Alignment.center,
          child: Container(
            margin: const EdgeInsets.symmetric(horizontal: 36),
            child: BusyCard(appState: appState),
          ),
        ),
      ),
    );
  }
}

/// Keeps AppState.topRoute in sync so the dock knows when the big orb owns
/// the screen ('/' and '/enroll') and hides itself.
class _RouteTracker extends NavigatorObserver {
  _RouteTracker(this.appState);
  final AppState appState;

  /// A ROUTE WITH NO NAME IS A MODAL, not a screen. showModalBottomSheet and
  /// showDialog push routes whose settings.name is null, and `?? '/'` turned
  /// every one of them into "we are at home". Two consequences, both real:
  /// the voice dock hides on '/', so opening the device sheet on the pin
  /// screen made the dock vanish; and every topRoute guard in AppState --
  /// the working-screen check, the question-route checks -- briefly believed
  /// it was somewhere it was not. A sheet ON a screen does not change which
  /// screen you are on, so the last NAMED route stands.
  void _sync(Route<dynamic>? route) {
    final name = route?.settings.name;
    if (name == null || name.isEmpty) return;
    appState.setTopRoute(name);
  }

  /// The stack AppState reads to answer "is that screen already open
  /// further down". Unnamed routes (every dialog and sheet) are not
  /// screens and are not tracked, exactly as _sync ignores them.
  static String? _name(Route<dynamic>? route) {
    final n = route?.settings.name;
    return (n == null || n.isEmpty || n.startsWith('/dialog/')) ? null : n;
  }

  @override
  void didPush(Route<dynamic> route, Route<dynamic>? previousRoute) {
    final n = _name(route);
    if (n != null) appState.noteRoutePushed(n);
    _sync(route);
  }

  @override
  void didPop(Route<dynamic> route, Route<dynamic>? previousRoute) {
    final n = _name(route);
    if (n != null) appState.noteRouteRemoved(n);
    _sync(previousRoute);
    // SAFETY NET: previousRoute == null means the navigator just went
    // EMPTY — the last route was popped, the screen is pure black, and
    // nothing on it can ever recover. Whatever bug caused the stray pop,
    // the user must never be stranded: restore the home route next frame.
    if (previousRoute == null) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        final nav = navigator;
        if (nav != null && !nav.canPop()) {
          nav.pushNamed('/');
        }
      });
    }
  }

  @override
  void didRemove(Route<dynamic> route, Route<dynamic>? previousRoute) {
    final n = _name(route);
    if (n != null) appState.noteRouteRemoved(n);
    _sync(previousRoute);
  }

  @override
  void didReplace({Route<dynamic>? newRoute, Route<dynamic>? oldRoute}) {
    final gone = _name(oldRoute);
    if (gone != null) appState.noteRouteRemoved(gone);
    final added = _name(newRoute);
    if (added != null) appState.noteRoutePushed(added);
    _sync(newRoute);
  }
}