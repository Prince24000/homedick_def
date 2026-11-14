# homevoice_moonshine2

Moonshine's newer speech models (Tiny, Small, Medium "streaming") through
Moonshine's own Android library, `ai.moonshine:moonshine-voice` 0.1.5. The
app hands it a finished recording and gets words back, so the app keeps the
microphone and the hub still receives the raw audio for voice checks. Works
from Android 8; needs a 64-bit phone.

The app's built-in Moonshine (version 1, through sherpa-onnx) is untouched
and is what everything falls back to.

NOT BUILT OR RUN BY ITS AUTHOR: it was written against a published app that
uses the same library version, without a compiler. Expect to send a build
error back at least once.

## Adding it to the app

1. Copy this folder to `packages/homevoice_moonshine2` in the Flutter
   project (beside `lib/`).
2. In the app's `pubspec.yaml`, under `dependencies:`

   ```yaml
   homevoice_moonshine2:
     path: packages/homevoice_moonshine2
   ```

3. In `android/app/build.gradle`, `minSdk` must be 26 or higher (Android 8):

   ```gradle
   defaultConfig {
       minSdk = 26
   }
   ```

4. `flutter pub get`, then build as usual.

Without step 2 the app still builds; Settings -> Speech to text then says the
new Moonshine is "not in this build of the app" and the built-in does
everything.

## Using it

Settings -> Speech to text -> **New Moonshine**, then pick a size. The first
time, the model is downloaded from Moonshine's servers (Tiny 34 MB, Small
123 MB, Medium 245 MB): the phone needs internet for that once, and the app
should stay open. Until it says "ready", the built-in recogniser hears
everything.

The same sheet shows who heard the last sentence and how long it took, which
is the number to judge a size by on a given phone.

## Keeping your own copy of the model (no internet on the phone)

A model the app downloads is kept in the app's PRIVATE storage
(`no_backup/moonshine-models`). It stays through an update of the app
(installing a new build over the old one) and is deleted when the app is
uninstalled or its data is cleared. A file manager cannot see it.

AFTER A DOWNLOAD THE PLUGIN SAVES A COPY where a computer can reach it (the
folder below), once. Connect the phone by USB, copy that folder to the
laptop, and keep it: after a fresh install, copy it back and nothing is
downloaded again. Settings shows where the copy went.

To hold a copy yourself, the plugin looks FIRST in the app's ordinary files
folder, the one a computer sees over USB and where the other models already
live:

    Android/data/<your app id>/files/models/moonshine2/small/
    Android/data/<your app id>/files/models/moonshine2/medium/
    Android/data/<your app id>/files/models/moonshine2/tiny/

Put the model's files (`encoder.ort`, `adapter.ort`, `cross_kv.ort`,
`decoder_kv.ort`, `frontend.model.ort`, `frontend.weights.ort`,
`streaming_config.json`, `tokenizer.bin`, ...) straight into that folder.
Settings then says "ready: small (..., from your models folder)".

The download happens ONLY when that folder is missing or empty. If your copy
is there and will not load, nothing is downloaded: Settings shows the reason
and the built-in recogniser carries on.

Getting the files onto a computer, from Moonshine's documentation:

    pip install moonshine-voice
    moonshine-voice download --stt --language en --model-arch 4

`--model-arch 4` is Small and `5` is Medium (2 is Tiny), going by the sample
app this plugin was written from; the command prints the folder it saved to.

NOT VERIFIED: the library call used to load from a folder was written from
memory and is looked up by name when the app runs. If Settings says your
copy was not loaded, send the reason it shows.

The app's files folder is removed when the app is UNINSTALLED, like the
other models in it: copy the folder back after a fresh install.

## If the build fails

| Error mentions | What to do |
|---|---|
| `Manifest merger failed ... minSdkVersion` | Step 3 was missed. |
| `2 files found with path 'lib/arm64-v8a/libonnxruntime.so'` | Both speech libraries carry their own copy of the model runtime. In `android/app/build.gradle`, inside `android { }`, add `packaging { jniLibs { pickFirsts += ["lib/**/libonnxruntime.so"] } }`. If the app then crashes when a model loads, the two copies are different versions and cannot share: say so, with the crash text. |
| `Unresolved reference` in `Moonshine2Plugin.kt` | The library's names differ from what was assumed. Send the lines of the error. |

## If it will not load on a phone

Settings shows the reason. "this phone is 32-bit" means the phone's Android
is a 32-bit build, which the library does not support. `OutOfMemoryError`
means the size chosen is too big for the phone: pick a smaller one.
