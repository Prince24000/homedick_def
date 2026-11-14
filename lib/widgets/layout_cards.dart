/// Layout cards — the Layouts section of the Devices screen.
///
/// ONE POINT OF TRUTH. Every connection lives here: a design abandoned
/// halfway, a finished plan that never reached its board, the wiring of a
/// board that is installed, and a plan you kept to reuse. Each card says
/// which, and names the ESP board it is on and the chip it fits.
///
/// A card that is a board's own wiring is edited THROUGH the board (the hub
/// keeps the two in step) and is not deleted or renamed on its own -- the
/// board owns its name and goes when the board is retired, after which its
/// wiring stays here as a plain plan. A plan is dormant: it controls nothing
/// until it is put on a board, and only its owner deletes it.
///
/// ONE DATA PATH: the `layouts` list in the `device_list` frame. Each record
/// carries its parts with their pins, so opening a card shows every
/// connection before you decide to edit, use or delete it.
library;

import 'package:flutter/material.dart';
import '../app_state.dart';

class LayoutCards {
  /// The cards, newest first as the hub sends them.
  static List<Widget> build(BuildContext context, AppState s) => [
        for (final l in s.layoutRecords) _card(context, s, l),
      ];

  /// "2026-10-01 03:21" -> "2026-10-01"; anything else is shown as is.
  static String _day(Object? stamp) {
    final t = (stamp ?? '').toString();
    return t.length >= 10 ? t.substring(0, 10) : t;
  }

  /// What the card says it is, in words, and whether it is the good state.
  static (String, bool) _status(Map<String, dynamic> l) {
    final board = (l['board_name'] ?? '').toString();
    switch ((l['status'] ?? 'saved').toString()) {
      case 'flashed':
        return ('On $board', true);
      case 'waiting':
        return ('On $board · not installed yet', false);
      case 'unfinished':
        return ('Unfinished', false);
      case 'not_sent':
        return ('Never reached its board', false);
      default:
        return ('Saved plan', false);
    }
  }

  static Widget _tag(AppState s, String text, bool good) {
    final c = good ? s.accent : s.inkDim;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        border: Border.all(color: c.withValues(alpha: 0.6)),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Text(text,
          style: TextStyle(
              fontSize: 11.5, fontWeight: FontWeight.w600, color: c)),
    );
  }

  static Widget _card(BuildContext context, AppState s, Map<String, dynamic> l) {
    final id = (l['id'] ?? '').toString();
    final name = (l['name'] ?? 'Layout').toString();
    final chip = (l['chip'] ?? '').toString();
    final boardLabel = (l['board_label'] ?? '').toString();
    final count = (l['count'] is num) ? (l['count'] as num).toInt() : 0;
    final source = (l['source'] ?? '').toString();
    final ownBoard = (l['on_board'] ?? '').toString(); // slug, or ''
    final (statusText, good) = _status(l);
    final parts = [
      for (final p in (l['parts'] as List? ?? const []))
        if (p is Map) p
    ];
    final sub = [
      boardLabel.isNotEmpty ? boardLabel : chip,
      '$count device${count == 1 ? '' : 's'}',
      if (_day(l['updated']).isNotEmpty) _day(l['updated']),
    ].where((x) => x.isNotEmpty).join(' · ');

    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      decoration: BoxDecoration(
        color: s.surface,
        border: Border.all(color: s.border),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Theme(
        // The tile's own divider lines fight the card's border.
        data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
        child: ExpansionTile(
          key: PageStorageKey<String>('layout:$id'),
          tilePadding: const EdgeInsets.symmetric(horizontal: 14),
          childrenPadding: const EdgeInsets.fromLTRB(14, 0, 14, 12),
          expandedCrossAxisAlignment: CrossAxisAlignment.start,
          iconColor: s.inkDim,
          collapsedIconColor: s.inkFaint,
          leading: Icon(
              ownBoard.isNotEmpty
                  ? Icons.developer_board
                  : Icons.dashboard_customize_outlined,
              size: 20,
              color: s.accent),
          title: Text(name,
              style: TextStyle(
                  fontSize: 15.5, fontWeight: FontWeight.w600, color: s.ink)),
          subtitle: Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                _tag(s, statusText, good),
                const SizedBox(height: 4),
                Text(sub, style: TextStyle(fontSize: 12.5, color: s.inkDim)),
              ],
            ),
          ),
          children: [
            if (source.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Text(source,
                    style: TextStyle(
                        fontSize: 12.5,
                        fontStyle: FontStyle.italic,
                        color: s.inkFaint)),
              ),
            Divider(color: s.border, height: 1),
            const SizedBox(height: 8),
            if (parts.isEmpty)
              Text('Nothing placed on it.',
                  style: TextStyle(fontSize: 13, color: s.inkDim)),
            for (final p in parts) _part(s, p),
            const SizedBox(height: 8),
            Divider(color: s.border, height: 1),
            const SizedBox(height: 4),
            Wrap(
              spacing: 4,
              runSpacing: 0,
              children: [
                _action(s, Icons.upload_outlined, 'Use on a board', s.accent,
                    () => s.useLayout(id)),
                _action(
                    s,
                    Icons.edit_outlined,
                    ownBoard.isNotEmpty ? 'Edit the board\'s wiring' : 'Edit',
                    s.inkDim,
                    () => s.editLayout(id)),
                // A board's wiring takes the board's name and goes with the
                // board; only a plain plan is renamed or deleted here.
                if (ownBoard.isEmpty) ...[
                  _action(s, Icons.drive_file_rename_outline, 'Rename',
                      s.inkDim, () => s.renameLayout(id)),
                  _action(s, Icons.delete_outline, 'Delete', s.cancelInk,
                      () => _confirmDelete(context, s, id, name)),
                ],
              ],
            ),
          ],
        ),
      ),
    );
  }

  /// One device of the layout: its name, the part, and every pin it uses.
  static Widget _part(AppState s, Map p) {
    final name = (p['name'] ?? '').toString();
    final part = (p['part'] ?? '').toString();
    final pins = [
      for (final r in (p['pins'] as List? ?? const []))
        if (r is Map) 'GPIO${r['gpio']}  (${(r['slot'] ?? '').toString()})'
    ];
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(Icons.subdirectory_arrow_right, size: 14, color: s.inkFaint),
          const SizedBox(width: 6),
          Expanded(
            child: Text(
              [
                if (name.isNotEmpty) name,
                if (part.isNotEmpty) '— $part',
                if (pins.isNotEmpty) '· ${pins.join(', ')}',
              ].join(' '),
              style: TextStyle(fontSize: 13, color: s.ink, height: 1.35),
            ),
          ),
        ],
      ),
    );
  }

  static Widget _action(AppState s, IconData icon, String label, Color color,
      VoidCallback onTap) {
    return TextButton.icon(
      onPressed: onTap,
      icon: Icon(icon, size: 16, color: color),
      label: Text(label, style: TextStyle(color: color, fontSize: 13.5)),
    );
  }

  /// Deleting is the ONE way a plan goes, and it is not undoable, so it is
  /// asked about. No board is touched: a layout is only a saved plan.
  static Future<void> _confirmDelete(
      BuildContext context, AppState s, String id, String name) async {
    final yes = await showDialog<bool>(
      context: context,
      builder: (dctx) => AlertDialog(
        backgroundColor: s.surface,
        surfaceTintColor: Colors.transparent,
        title:
            Text('Delete $name?', style: TextStyle(color: s.ink, fontSize: 17)),
        content: Text(
            'The saved plan goes for good. No board is changed — a layout '
            'only describes wiring, and boards already set up with it '
            'keep it.',
            style: TextStyle(color: s.inkDim, fontSize: 13.5)),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dctx, false),
            child: Text('Cancel', style: TextStyle(color: s.cancelInk)),
          ),
          TextButton(
            onPressed: () => Navigator.pop(dctx, true),
            child: Text('Delete',
                style: TextStyle(
                    color: s.cancelInk, fontWeight: FontWeight.w700)),
          ),
        ],
      ),
    );
    if (yes != true) return;
    s.removeLayout(id);
  }
}
