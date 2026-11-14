/// The connection's watchdog: WHEN to dial again, and what to tell the
/// person about remote access. One place for both (2026-09-28).
///
/// The socket itself (socket_client.dart) keeps the heartbeat -- a ping
/// every 20 s, a missed pong closes it -- and the retry ladder after a drop
/// (2, 5, 10 s, then every 30 s until Disconnect). This file only decides
/// the EXTRA dial when the app comes back to the front -- only while a
/// connection is wanted and is not up.
library;

import 'socket_client.dart';

class ConnectionWatch {
  final Set<String> _told = {};

  /// Should an extra dial happen now? Never after Disconnect, never over a
  /// live connection.
  bool shouldDial(SocketClient? sock, ConnState state) =>
      sock != null && sock.wantConnected && state != ConnState.ready;

  /// What to tell the person about remote access, once per hub per app run
  /// -- or null. ONLY a fact the hub itself reported: it sent no Tailscale
  /// name or address. (The "phone not on Tailscale" and probe checks were
  /// removed 2026-09-29: both warned on phones connected THROUGH Tailscale.)
  Future<String?> tailnetNotice(
      {required String hubId,
      required String hubLabel,
      required String name,
      required String ip,
      int port = 8765}) async {
    if (name.isNotEmpty || ip.isNotEmpty) {
      _told.removeWhere((k) => k.startsWith('$hubId:'));
      return null;
    }
    final who = hubLabel.isEmpty ? 'This hub' : hubLabel;
    return _once(hubId, 'none',
        '$who has no Tailscale, so it can only be reached on the same '
        'network. Set up remote access on the hub to use it away from home.');
  }

  String? _once(String hubId, String kind, String text) =>
      _told.add('$hubId:$kind') ? text : null;
}
