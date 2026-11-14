package com.example.homevoice

import android.content.Context
import android.net.wifi.WifiManager
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        // FIRST: this is what registers every plugin the app uses.
        super.configureFlutterEngine(flutterEngine)

        // THE WI-FI SIGNAL, for the connection bars (app_state.dart,
        // _readWifi). One method, "rssi": the signal in dBm, or null when
        // this phone is not on Wi-Fi.
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "homevoice/wifi")
            .setMethodCallHandler { call, result ->
                if (call.method == "rssi") result.success(wifiRssi())
                else result.notImplemented()
            }
    }

    /** The Wi-Fi signal in dBm (about -40 strong to -90 nearly gone), or
     *  null: Wi-Fi off, not joined to a network, or this phone is the
     *  hotspot. Needs only ACCESS_WIFI_STATE -- the network's NAME would
     *  need location, and is not asked for. */
    @Suppress("DEPRECATION")
    private fun wifiRssi(): Int? {
        return try {
            val wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            if (!wifi.isWifiEnabled) return null
            val rssi = wifi.connectionInfo?.rssi ?: return null
            // -127 is Android's "no reading": not joined to anything.
            if (rssi <= -127 || rssi >= 0) null else rssi
        } catch (e: Exception) {
            null
        }
    }
}