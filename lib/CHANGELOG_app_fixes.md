# HomeVoice app — fix pass (2 Sep 2026)

This is the app's `lib/` folder with every audit fix applied. Copy it over your project's `lib/` (delete `lib/screens/esphome_screen.dart` by hand — it was removed and unzipping will not delete it). It must be used with the fixed hub (`homevoice_fixed.zip`, rebuilt today): five hub changes were made for the app and the wire contract only lines up with both sides updated. Finding numbers (A1–A46) refer to the app audit page. Not built here (no Dart toolchain); syntax-checked with a Dart parser after every edit and reviewed once more by hand for compile errors — expect the first real build to be the final check, especially on three plugin APIs: `webview_flutter` ≥ 4.4 (`onUrlChange`, `WebViewCookieManager`), `flutter_tts` ≥ 3.3 (`getDefaultVoice`), and `flutter_foreground_task` (unchanged parameter names).

## Hub-side changes made for the app (already in homevoice_fixed.zip)

- Login PIN prompt treats a cancel word as a cancel, never as a PIN guess (A3).
- The 15-second home-tile feed never includes a secure device (A7).
- Board credentials frame carries passwords only in a masked `secrets` list; `items` holds username/topic only (A46).
- `device_list.on_home` for a switchable device now means "asked for", matching what the home screen draws (A41).
- `own_spki_fingerprint` docstring states the real binding property: local.crt on every listener; pairing only on the access point (A10).

## What changed in the app

**Privacy**
- The recording harness is debug-only, command-role-only, capped at 20 pairs. Release builds write nothing to external storage (A1).
- The recovery phrase is never spoken; the phone says "Your recovery words are on the screen. Write them down." (A9). Only text beginning `RECOVERY PHRASE` opens the keepsafe dialog; other important lines are a banner (A8).
- Text-to-speech prefers an installed voice that does not need the network, when the platform says which (A18).
- The API key is wiped from the clipboard after a successful pairing (A32).
- Every field that answers the hub has autocorrect and suggestions off; secret fields are masked with an eye toggle (A6, A20, A21, A43).

**Transport**
- `wss://` is enforced in the address dialog, in `saveCandidates`, and in the socket client itself; a `ws://` line is refused, never dialled (A2).
- Bounded reconnect (2 s, 5 s, 10 s; only after one successful authentication) and a 20-second ping so a dead link is noticed; every reconnect re-runs the full challenge (A27).
- `forgetPin()` really forgets; candidate list copied before a probe sweep; key generation memoised (A26, A28, A29).
- Pairing refuses non-wss URLs and a missing certificate with a clear error; the pair screen says pairing happens on the hub's own Wi-Fi (A10).
- Foreground service no longer auto-starts on boot (A30).

**Flow and state**
- Cancelling a secret prompt sends an empty answer (A3).
- Real pipeline teardown before a restart, on dispose, and on unpair; "Unpair this phone" added to Settings (A11, A37).
- Concurrent refusals (`busy`, `server_busy`, `setup_in_progress`, `not_enrolled`, `not_bound`) leave a running flow untouched; notices cannot fire a menu action while busy (A12).
- Frames handled one at a time; speech never restores a stale state (A13).
- Prompts answered by voice clear their screen, arm the watchdog, and `view` frames wake the flow (A14).
- Checkpointed and failed results reach the banner/popup for every flow (A15).
- Boot failures land in a visible problem with a Retry button; the boot runs behind the first frame (A17).
- NaN-safe audio cleaner; the flow returns to idle on any capture failure (A19).
- `hide_ha` and the "Home Assistant things" button removed (A16); enroll-view sentence only for an unbound phone (A25); "Done." only for command results (A40); route notices allow-listed (A36); still-there and next-step dialogs own their routes (A35); pair chip survives unrelated errors (A34); oversized challenge clips keep the question (A39); `_pendingEditId` cleared (A33); "No response" no longer fires during a gated action (A38).

**Screens**
- "falls below" rules reopen and save as "falls below" (A4).
- Pin map sends `confirmed` for overrides; GPIO collisions and duplicate names refused at entry (A5).
- Forms honour `secret` (A6); PIN boxes are a real password field (A21); PIN keypad only for PIN prompts (A20).
- Profile: other people shown neutrally, your other handsets listed, "re-set your PIN to upgrade your recovery words" hint (A22).
- WebView: http(s)-only navigation, host shown in the bar, cookies cleared on close; the initial page must be https unless the host is private (A23).
- Login screen tells the hub when it is dismissed (A24). Secure devices with withheld state read "verify to see" (A41). Per-question state keyed on a serial, not the question text (A42). `esphome_screen.dart` deleted (A44). Orb animations run only when active (A45).

## Left as is
- A10 — per-binding channel binding on the tailnet would need both sides to read the presented certificate on every connection; documented instead. Pairing on the AP is the intended path.
- A31 — the recording package exposes no sample-rate readback; 16 kHz is documented as an assumption.

## Added: in-app QR scanner (your request, 2 Sep 2026)

`screens/scan_screen.dart` (new) opens the camera, waits for a `homevoice://pair?...` code and hands the raw text back to the pair screen, which parses it with `parsePairCode`. Only our own scheme is accepted; any other QR is ignored with a hint. Decoding is on-device (ML Kit's bundled model on Android, AVFoundation on iOS) — no frames and no network calls leave the phone. **The paste button is gone** (your call); the key can still be typed by hand. The voice dock is hidden over the scanner.

**Pairing-window countdown.** The pair screen now shows a chip — "Pairing is open on the hub · 4:32" counting down, turning to "Pairing has closed on the hub — run --pair again and rescan" at zero. It knows the deadline from two sources: the scanned code (the hub's `--pair` now prints a code that carries `until=<expiry>`), and the hub itself the moment a pairing attempt is answered (`pair_window_seconds`), which overrides the code. A stale printed code reads as closed, never as open.

**You must add the dependency and permissions yourself (no pubspec was supplied):**

1. `pubspec.yaml`, under `dependencies`:
   ```yaml
   mobile_scanner: ^3.5.7
   ```
   3.5.x is the last line that builds with AGP 7 / Kotlin 1.7 — the 5.x/6.x line is what forced the Gradle upgrade last time. If your `android/build.gradle` already has AGP ≥ 8.1 and Kotlin ≥ 1.9, `^5.2.3` is fine too; the Dart API used here (`MobileScannerController(formats:, detectionSpeed:, torchEnabled:)`, `MobileScanner(controller:, onDetect:, errorBuilder:)`, `toggleTorch()`) is the same in both.
2. `android/app/src/main/AndroidManifest.xml`, inside `<manifest>`:
   ```xml
   <uses-permission android:name="android.permission.CAMERA" />
   ```
   `minSdkVersion` must be ≥ 21. The plugin asks for camera permission at runtime the first time the screen opens.
3. iOS only — `ios/Runner/Info.plist`:
   ```xml
   <key>NSCameraUsageDescription</key>
   <string>Scan the hub's pairing code.</string>
   ```

On the hub, `python setup_manager.py --pair` now opens the window AND draws the code in the terminal (needs `pip install qrcode`); `--pair-qr` still writes a printable `pair.png` without an expiry. Open the app → Pair → tap the camera icon in the key field.

## Merged: your 2 Sep additions (3 Sep 2026)

You sent your current `app_state.dart` and `screens/home_screen.dart`. Diffed against the archive you first gave me, the only differences were two additions of yours, and both are now in the fixed files with every fix kept:

- `app_state.dart` — `toggleOnHome` flips `on_home` in `deviceRecords` and notifies before sending `homeflag:<id>`, so the switch moves on the tap; the hub's `device_list` refresh still settles it either way.
- `screens/home_screen.dart` — reading tiles: 12 px grid gaps, aspect 1.78, 14/13 padding, radius 14, 5 px room gap, and a value with `reporting == false` is drawn in `inkFaint` (the hub already sends `reporting` per row, so this lights up straight away).

Nothing else in your two files was newer than the archive, so no fix was reverted. The scanner button on the pair screen uses `Icons.qr_code_scanner_rounded` (the QR-in-a-viewfinder glyph): the button opens a scanner for a QR code, and a plain camera icon would promise a photo.
