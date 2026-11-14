/// The list of addresses this phone dials to find the hub, and the dialog
/// that edits it. Lives under Settings -> NETWORK -> Server addresses.
///
/// MOVED HERE from the home screen's drawer (2026-09-08), where it sat as
/// "Edit server addresses" under a CONNECTION heading -- a place people did
/// not look for it. Settings is where every other thing about this phone
/// is changed, so it is where this belongs. The drawer keeps only the
/// read-only "connected on ..." line.
///
/// One address per line, tried top-down; the first that answers is used.
/// Only `wss://` is accepted: a `ws://` line would be dialled in the clear,
/// PIN and all, so a bad line is named before anything is saved.
library;

import 'package:flutter/material.dart';

import '../app_state.dart';

/// The first line that is not `wss://host[:port]`, or null when all are.
String? firstBadCandidate(List<String> lines) {
  for (final raw in lines) {
    final line = raw.trim();
    if (line.isEmpty) continue;
    final u = Uri.tryParse(line);
    if (u == null || !u.isScheme('wss') || u.host.isEmpty) return line;
  }
  return null;
}

Future<void> showNetworkEditor(BuildContext context, AppState s) {
  final ctl = TextEditingController(text: s.candidates.join('\n'));
  String? bad;
  return showDialog<void>(
    context: context,
    builder: (dialogCtx) => StatefulBuilder(
      builder: (dialogCtx, setLocal) => AlertDialog(
        backgroundColor: s.surface,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        title: Text('Server addresses',
            style: TextStyle(
                color: s.ink, fontSize: 16, fontWeight: FontWeight.w600)),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // WHERE THIS PHONE IS RIGHT NOW, so an edit can be checked
            // against something true rather than remembered.
            if (s.connectedUrl != null)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Row(
                  children: [
                    Icon(Icons.check_circle, color: s.accent, size: 16),
                    const SizedBox(width: 6),
                    Expanded(
                      child: Text('Connected on ${s.connectedUrl}',
                          style: TextStyle(fontSize: 12, color: s.ink)),
                    ),
                  ],
                ),
              ),
            TextField(
              controller: ctl,
              maxLines: 6,
              autocorrect: false,
              enableIMEPersonalizedLearning: false,
              style: TextStyle(
                  color: s.ink, fontSize: 13, fontFamily: 'monospace'),
              decoration: InputDecoration(
                filled: true,
                fillColor: s.bg,
                // Two refusals, two sentences. A named line is a typo; an
                // EMPTY string means the box held no usable address at all
                // (all blank, or only whitespace), which used to close the
                // dialog as though it had saved.
                errorText: bad == null
                    ? null
                    : (bad!.isEmpty
                        ? 'Nothing to save — add at least one wss:// address'
                        : 'Not a wss:// address: $bad'),
                errorMaxLines: 3,
                border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(8),
                    borderSide: BorderSide(color: s.border)),
                focusedBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(8),
                    borderSide: BorderSide(color: s.accent)),
              ),
            ),
            const SizedBox(height: 8),
            Text(
              'One wss:// address per line, tried top-down. Put the address '
              'you use most first and your tailnet name last. Applies on '
              'the next connect.',
              style: TextStyle(fontSize: 12, color: s.inkFaint),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogCtx),
            // Every cancel wears the disconnect red (her rule, 2026-08-27).
            child: Text('Cancel', style: TextStyle(color: s.cancelInk)),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
                backgroundColor: s.accent, foregroundColor: s.accentFg),
            // AWAITED, and the dialog closes only on a real save (audit,
            // 2026-09-12). firstBadCandidate skips blank lines, so an
            // all-blank box passes validation; saveCandidates then finds
            // nothing valid and returns without writing. Popping on the
            // next line regardless made that look exactly like a
            // successful save, and its `status` line renders on the home
            // screen, two routes below this dialog.
            onPressed: () async {
              final lines = ctl.text.split('\n');
              final wrong = firstBadCandidate(lines);
              if (wrong != null) {
                setLocal(() => bad = wrong);
                return;
              }
              // THE HUB'S PORT when a line names none. "wss://hub.local" is
              // a valid URL, so it passed -- and was dialled on 443, where
              // nothing listens, on every sweep.
              final withPorts = [
                for (final raw in lines)
                  (() {
                    final u = Uri.tryParse(raw.trim());
                    return (u != null && u.host.isNotEmpty && !u.hasPort)
                        ? u.replace(port: 8765).toString()
                        : raw;
                  })()
              ];
              if (await s.saveCandidates(withPorts)) {
                if (dialogCtx.mounted) Navigator.pop(dialogCtx);
                return;
              }
              // Nothing was written. Say so HERE, where the person is --
              // unless the dialog was dismissed while the save ran.
              if (!dialogCtx.mounted) return;
              setLocal(() => bad = '');
            },
            child: const Text('Save'),
          ),
        ],
      ),
    ),
  // Disposed after the frame, not the moment the dialog's future resolves:
  // the route is still animating out and its field's focus loss, queued
  // behind this, writes to the controller.
  ).then((_) => WidgetsBinding.instance
      .addPostFrameCallback((_) => ctl.dispose()));
}