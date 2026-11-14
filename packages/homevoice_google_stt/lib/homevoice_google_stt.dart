/// HomeVoice's local plugin: the phone's speech recogniser, fed with the
/// app's own audio. The app talks to it through the method channel below
/// (audio/google_stt.dart), not through an import of this file -- so the
/// app still builds, and falls back to Moonshine, when this plugin is not
/// in its pubspec.
library;

const String googleSttChannel = 'homevoice/google_stt';
