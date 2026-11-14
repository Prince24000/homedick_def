# homevoice_google_stt

The phone's own speech recogniser, fed with audio the app captured
(`RecognizerIntent.EXTRA_AUDIO_SOURCE`, Android 13+), so the app keeps the
microphone and the hub still receives the raw audio for voice checks.

## Adding it to the app

1. Copy this folder to `packages/homevoice_google_stt` in the Flutter project
   (beside `lib/`).
2. In the app's `pubspec.yaml`, under `dependencies:`

   ```yaml
   homevoice_google_stt:
     path: packages/homevoice_google_stt
   ```

3. `flutter pub get`, then build as usual.

Without step 2 the app still builds; Settings -> Speech to text then says the
Google engine is not in this build and Moonshine does everything.
