/// Foreground service scaffolding. On Android 12 a persistent notification
/// and a `microphone` foregroundServiceType are the price of a mic that
/// stays hot with the screen off -- the OS also shows the green mic
/// indicator the whole time, which is system behavior and not removable.
///
/// The service itself only keeps the process alive and visible; the actual
/// pipeline objects live in the main isolate and are started from main.dart.
library;

import 'package:flutter_foreground_task/flutter_foreground_task.dart';

Future<void> initForegroundService() async {
  FlutterForegroundTask.init(
    androidNotificationOptions: AndroidNotificationOptions(
      channelId: 'homevoice_listener',
      channelName: 'Omniscience listener',
      channelDescription: 'Listening for the wake word',
      channelImportance: NotificationChannelImportance.LOW,
      priority: NotificationPriority.LOW,
    ),
    iosNotificationOptions: const IOSNotificationOptions(),
    foregroundTaskOptions: ForegroundTaskOptions(
      eventAction: ForegroundTaskEventAction.nothing(),
      // NOT on boot: the pipeline lives in the main isolate and only
      // _startPipeline starts it, so a boot-started service would show
      // "Listening for the wake word" over a process listening to nothing.
      autoRunOnBoot: false,
      allowWifiLock: true, // the socket lives on Wi-Fi; don't let it doze
    ),
  );
}

Future<bool> startForegroundService() async {
  if (await FlutterForegroundTask.isRunningService) return true;
  final result = await FlutterForegroundTask.startService(
    notificationTitle: 'Omniscience',
    notificationText: 'Listening for the wake word',
  );
  return result is ServiceRequestSuccess;
}
