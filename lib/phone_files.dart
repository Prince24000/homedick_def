/// Files picked on this phone and sent to the hub when it asks
/// (`file_request`): firmware images for "Install firmware on hub".
///
/// Nothing is stored on the phone and nothing is executed; the files are
/// only read from the picker and sent. Every name is reduced to a bare,
/// plain file name first.
library;

import 'dart:convert' show base64Encode;
import 'dart:io';
import 'dart:typed_data';

import 'package:file_picker/file_picker.dart';

/// The hub's `file_request` question.
class FileRequestFrame {
  final String text;

  /// What the files are for ('firmware').
  final String purpose;

  /// Lower-case extensions with their dot (".ino"). Empty means any.
  final List<String> accept;
  final bool multiple;

  /// The TOTAL of all files, before base64. 0 means the hub named none.
  final int maxBytes;

  /// For purpose `firmware`: the one chip family wanted ("esp32c3"), or ''
  /// for any. Firmware files of other families are left out.
  final String family;

  const FileRequestFrame({
    required this.text,
    required this.purpose,
    required this.accept,
    required this.multiple,
    required this.maxBytes,
    this.family = '',
  });

  factory FileRequestFrame.fromJson(Map<String, dynamic> m) {
    final accept = <String>[];
    for (final a in (m['accept'] as List? ?? const [])) {
      var e = a.toString().trim().toLowerCase();
      if (e.isEmpty) continue;
      if (!e.startsWith('.')) e = '.$e';
      accept.add(e);
    }
    return FileRequestFrame(
      text: (m['text'] as String? ?? '').trim(),
      purpose: (m['purpose'] as String? ?? 'firmware').trim(),
      accept: accept,
      multiple: m['multiple'] == true,
      maxBytes: (m['max_bytes'] as num?)?.toInt() ?? 0,
      family: (m['family'] as String? ?? '').trim().toLowerCase(),
    );
  }

  bool accepts(String name) =>
      (accept.isEmpty || accept.contains(PhoneFiles.extOf(name))) &&
      (family.isEmpty ||
          PhoneFiles.extOf(name) != '.bin' ||
          PhoneFiles.familyOf(name) == family);
}

/// A file in memory, on its way to the hub.
class PickedFile {
  final String name;
  final Uint8List bytes;
  const PickedFile(this.name, this.bytes);
}

/// What the system picker gave back.
class PickOutcome {
  /// True when the person closed the picker without choosing.
  final bool cancelled;
  final List<PickedFile> files;

  /// Names left out, each with why ("not .bin", "could not be read").
  final List<String> skipped;
  const PickOutcome(
      {this.cancelled = false,
      this.files = const [],
      this.skipped = const []});
}

class PhoneFiles {
  PhoneFiles._();

  /// Letters, digits and the punctuation real file names use. No slashes,
  /// so a name can never climb out of its folder.
  static final _plain = RegExp(r'^[A-Za-z0-9 ._()+\-]+$');

  /// The lower-case extension with its dot, or '' for none.
  static String extOf(String name) {
    final i = name.lastIndexOf('.');
    return i <= 0 ? '' : name.substring(i).toLowerCase();
  }

  /// A bare, safe file name, or null. Any folder part is dropped (the
  /// contract sends bare names; a picker may hand a path).
  static String? safeName(String raw) {
    final parts = raw.replaceAll('\\', '/').split('/');
    final n = parts.isEmpty ? '' : parts.last.trim();
    if (n.isEmpty || n.length > 120 || n.startsWith('.')) return null;
    if (!_plain.hasMatch(n)) return null;
    return n;
  }

  /// "12 KB", "1.4 MB".
  static String sizeLabel(int bytes) {
    if (bytes < 1024) return '$bytes B';
    if (bytes < 1024 * 1024) return '${(bytes / 1024).round()} KB';
    return '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';
  }

  /// The chip family a firmware file is for, from Tasmota's own naming
  /// (tasmota32.bin -> "esp32", tasmota32c3.factory.bin -> "esp32c3"), or
  /// null when the name does not say.
  static String? familyOf(String name) {
    final m = RegExp(r'^tasmota32(s2|s3|c2|c3|c5|c6|h2|p4)?(?![a-z0-9])')
        .firstMatch(name.toLowerCase());
    if (m == null) return null;
    return 'esp32${m.group(1) ?? ''}';
  }

  /// Open the system picker. Android cannot filter extensions it does not
  /// know (.ino), so it opens on everything and the filter is applied here;
  /// [accept] empty keeps every file.
  static Future<PickOutcome> pick(
      {required bool multiple,
      List<String> accept = const [],
      String family = ''}) async {
    final result = await FilePicker.platform.pickFiles(
      allowMultiple: multiple,
      type: FileType.any,
      withData: true,
    );
    if (result == null || result.files.isEmpty) {
      return const PickOutcome(cancelled: true);
    }
    final files = <PickedFile>[];
    final skipped = <String>[];
    for (final f in result.files) {
      final name = safeName(f.name);
      if (name == null) {
        skipped.add('${f.name} (unusable name)');
        continue;
      }
      if (accept.isNotEmpty && !accept.contains(extOf(name))) {
        skipped.add('$name (not ${accept.join('/')})');
        continue;
      }
      if (family.isNotEmpty && extOf(name) == '.bin' &&
          familyOf(name) != family) {
        skipped.add('$name (not firmware for $family)');
        continue;
      }
      // Large files can come back without bytes on some platforms; the
      // path is then the way to them.
      var bytes = f.bytes;
      final path = f.path;
      if (bytes == null && path != null) {
        try {
          bytes = await File(path).readAsBytes();
        } catch (_) {
          bytes = null;
        }
      }
      if (bytes == null) {
        skipped.add('$name (could not be read)');
        continue;
      }
      files.add(PickedFile(name, bytes));
    }
    return PickOutcome(files: files, skipped: skipped);
  }

  static int totalBytes(List<PickedFile> files) =>
      files.fold<int>(0, (sum, f) => sum + f.bytes.length);

  /// The `files` list of a file_response.
  static List<Map<String, String>> wireFiles(List<PickedFile> files) => [
        for (final f in files) {'name': f.name, 'data': base64Encode(f.bytes)}
      ];
}
