/// HomeVoice's local plugin: Moonshine's newer models through Moonshine's
/// own Android library. The app talks to it through the method channel
/// below (audio/moonshine2.dart), not through an import of this file -- so
/// the app still builds, and uses its built-in Moonshine, when this plugin
/// is not in its pubspec.
library;

const String moonshine2Channel = 'homevoice/moonshine2';
