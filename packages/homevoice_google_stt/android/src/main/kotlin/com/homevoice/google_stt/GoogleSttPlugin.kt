package com.homevoice.google_stt

// The phone's own speech recogniser (Google's, on most Android phones),
// fed with audio THE APP captured -- never opening the microphone itself.
//
// WHY FED, NOT LISTENING. The hub verifies the speaker from the raw audio
// of every command, so the app must own the microphone. A recogniser that
// opened the mic itself would leave the app with silence and every command
// would fall back to the PIN. Android 13 (API 33) added
// RecognizerIntent.EXTRA_AUDIO_SOURCE for exactly this: hand the recogniser
// the read end of a pipe and write our PCM into the other end.
//
// "DEPENDING ON THE RECOGNIZER IMPLEMENTATION, THIS VALUE MAY HAVE NO
// EFFECT" (Android's own documentation). So nothing here assumes it works:
// a pipe holds 64 KiB, and a recogniser that is reading it lets a longer
// clip through; one that ignores it leaves the writer stuck at 64 KiB.
// That stall is reported as `audio_source_ignored`, and the app falls back
// to its own model (Moonshine).
//
// ON-DEVICE FIRST. createOnDeviceSpeechRecognizer (API 31) keeps the audio
// on the phone. The ordinary recogniser -- which may send audio to its
// servers -- is used only when the app asks for it (network=true), which
// it does only when the person allowed online use (a setting the
// person turns on, labelled as such).
//
// Channel "homevoice/google_stt":
//   status(online)                 -> {sdk, available, onDevice, audioSource,
//                                      installed, pending, supported, online,
//                                      supportError}
//   download(locale)               -> bool (the system's download prompt)
//   transcribe(pcm, sampleRate, locale, network, timeoutMs, stallMs)
//                                  (network: the ordinary, server-capable
//                                   recogniser instead of the on-device one)
//                                  -> {text, confidence} or {error, code};
//                                     always with fed, fedAll, ms
//   cancel()

import android.annotation.TargetApi
import android.content.Context
import android.content.Intent
import android.media.AudioFormat
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.ParcelFileDescriptor
import android.speech.RecognitionListener
import android.speech.RecognitionSupport
import android.speech.RecognitionSupportCallback
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import io.flutter.embedding.engine.plugins.FlutterPlugin
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.io.IOException
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

class GoogleSttPlugin : FlutterPlugin, MethodChannel.MethodCallHandler {

    companion object {
        const val CHANNEL = "homevoice/google_stt"

        /** Linux's default pipe capacity. More than this written means read. */
        const val PIPE_BYTES = 65536L
    }

    private lateinit var channel: MethodChannel
    private lateinit var context: Context
    private val main = Handler(Looper.getMainLooper())
    private var active: Session? = null

    override fun onAttachedToEngine(binding: FlutterPlugin.FlutterPluginBinding) {
        context = binding.applicationContext
        channel = MethodChannel(binding.binaryMessenger, CHANNEL)
        channel.setMethodCallHandler(this)
    }

    override fun onDetachedFromEngine(binding: FlutterPlugin.FlutterPluginBinding) {
        channel.setMethodCallHandler(null)
        active?.finish(mapOf("error" to "detached"))
        active = null
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "status" -> status(call.argument<Boolean>("online") ?: false, result)
            "download" -> download(call.argument<String>("locale"), result)
            "transcribe" -> {
                val pcm = call.argument<ByteArray>("pcm")
                if (pcm == null) {
                    result.success(mapOf("error" to "no_audio"))
                    return
                }
                transcribe(
                    pcm,
                    call.argument<Int>("sampleRate") ?: 16000,
                    call.argument<String>("locale"),
                    call.argument<Boolean>("network") ?: false,
                    (call.argument<Int>("timeoutMs") ?: 15000).toLong(),
                    (call.argument<Int>("stallMs") ?: 5000).toLong(),
                    result,
                )
            }
            "cancel" -> {
                active?.finish(mapOf("error" to "cancelled"))
                active = null
                result.success(null)
            }
            else -> result.notImplemented()
        }
    }

    // ── which recogniser ────────────────────────────────────────────────

    private fun onDeviceAvailable(): Boolean =
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
            SpeechRecognizer.isOnDeviceRecognitionAvailable(context)

    /** For STATUS: the on-device recogniser when the phone has one; the
     *  ordinary one only when online use was allowed; else none. */
    private fun newRecognizer(online: Boolean): SpeechRecognizer? {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
            SpeechRecognizer.isOnDeviceRecognitionAvailable(context)) {
            return SpeechRecognizer.createOnDeviceSpeechRecognizer(context)
        }
        if (online && SpeechRecognizer.isRecognitionAvailable(context)) {
            return SpeechRecognizer.createSpeechRecognizer(context)
        }
        return null
    }

    /** For TRANSCRIBE: exactly the recogniser the app chose -- the app
     *  knows which one has the language (see the status call). */
    private fun chosenRecognizer(network: Boolean): SpeechRecognizer? {
        if (network) {
            return if (SpeechRecognizer.isRecognitionAvailable(context)) {
                SpeechRecognizer.createSpeechRecognizer(context)
            } else {
                null
            }
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
            SpeechRecognizer.isOnDeviceRecognitionAvailable(context)) {
            return SpeechRecognizer.createOnDeviceSpeechRecognizer(context)
        }
        return null
    }

    private fun baseIntent(locale: String?): Intent =
        Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, context.packageName)
            if (!locale.isNullOrEmpty()) {
                putExtra(RecognizerIntent.EXTRA_LANGUAGE, locale)
            }
        }

    // ── status ──────────────────────────────────────────────────────────

    private fun status(online: Boolean, result: MethodChannel.Result) {
        val out = HashMap<String, Any?>()
        out["sdk"] = Build.VERSION.SDK_INT
        out["available"] = SpeechRecognizer.isRecognitionAvailable(context)
        out["onDevice"] = onDeviceAvailable()
        out["audioSource"] = Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            statusLanguages(online, out, result)
        } else {
            result.success(out)
        }
    }

    /** Which languages the recogniser has installed, is downloading, or
     *  could download -- so the app can pick English and offer the pack. */
    @TargetApi(Build.VERSION_CODES.TIRAMISU)
    private fun statusLanguages(
        online: Boolean,
        out: HashMap<String, Any?>,
        result: MethodChannel.Result,
    ) {
        val rec = newRecognizer(online)
        if (rec == null) {
            result.success(out)
            return
        }
        val replied = AtomicBoolean(false)
        val reply = {
            if (replied.compareAndSet(false, true)) {
                try {
                    rec.destroy()
                } catch (e: Exception) {
                    // already gone
                }
                result.success(out)
            }
        }
        try {
            rec.checkRecognitionSupport(baseIntent(null), context.mainExecutor,
                object : RecognitionSupportCallback {
                    override fun onSupportResult(support: RecognitionSupport) {
                        out["installed"] = ArrayList(support.installedOnDeviceLanguages)
                        out["pending"] = ArrayList(support.pendingOnDeviceLanguages)
                        out["supported"] = ArrayList(support.supportedOnDeviceLanguages)
                        out["online"] = ArrayList(support.onlineLanguages)
                        reply()
                    }

                    override fun onError(error: Int) {
                        out["supportError"] = error
                        reply()
                    }
                })
        } catch (e: Exception) {
            out["supportError"] = e.toString()
            reply()
            return
        }
        // A recogniser that never answers must not hang the app.
        main.postDelayed({
            if (!replied.get()) {
                out["supportError"] = "timeout"
                reply()
            }
        }, 5000)
    }

    // ── download ────────────────────────────────────────────────────────

    private fun download(locale: String?, result: MethodChannel.Result) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU && onDeviceAvailable()) {
            downloadModel(locale, result)
        } else {
            result.success(false)
        }
    }

    /** Ask the phone to fetch its offline speech pack for `locale`. The
     *  phone shows its own prompt/notification; this returns at once. */
    @TargetApi(Build.VERSION_CODES.TIRAMISU)
    private fun downloadModel(locale: String?, result: MethodChannel.Result) {
        val rec = SpeechRecognizer.createOnDeviceSpeechRecognizer(context)
        try {
            rec.triggerModelDownload(baseIntent(locale))
            result.success(true)
        } catch (e: Exception) {
            result.success(false)
        } finally {
            // Not at once: the request is still being handed over.
            main.postDelayed({
                try {
                    rec.destroy()
                } catch (e: Exception) {
                    // already gone
                }
            }, 3000)
        }
    }

    // ── transcribe ──────────────────────────────────────────────────────

    private fun transcribe(
        pcm: ByteArray,
        sampleRate: Int,
        locale: String?,
        network: Boolean,
        timeoutMs: Long,
        stallMs: Long,
        result: MethodChannel.Result,
    ) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            result.success(mapOf("error" to "android_too_old"))
            return
        }
        transcribeFed(pcm, sampleRate, locale, network, timeoutMs, stallMs, result)
    }

    @TargetApi(Build.VERSION_CODES.TIRAMISU)
    private fun transcribeFed(
        pcm: ByteArray,
        sampleRate: Int,
        locale: String?,
        network: Boolean,
        timeoutMs: Long,
        stallMs: Long,
        result: MethodChannel.Result,
    ) {
        // One at a time: a newer request replaces an older one.
        active?.finish(mapOf("error" to "superseded"))
        active = null

        val rec = chosenRecognizer(network)
        if (rec == null) {
            result.success(mapOf("error" to "no_recognizer"))
            return
        }
        val pipe = try {
            ParcelFileDescriptor.createPipe()
        } catch (e: IOException) {
            rec.destroy()
            result.success(mapOf("error" to "pipe"))
            return
        }
        val session = Session(rec, pipe[0], pipe[1], result)
        active = session
        rec.setRecognitionListener(session)

        val intent = baseIntent(locale).apply {
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, false)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, !network)
            putExtra(RecognizerIntent.EXTRA_AUDIO_SOURCE, pipe[0])
            putExtra(RecognizerIntent.EXTRA_AUDIO_SOURCE_CHANNEL_COUNT, 1)
            putExtra(RecognizerIntent.EXTRA_AUDIO_SOURCE_ENCODING,
                AudioFormat.ENCODING_PCM_16BIT)
            putExtra(RecognizerIntent.EXTRA_AUDIO_SOURCE_SAMPLING_RATE, sampleRate)
        }
        try {
            rec.startListening(intent)
        } catch (e: Exception) {
            session.finish(mapOf("error" to "start_failed"))
            return
        }
        Thread({ session.feed(pcm, sampleRate) }, "google-stt-feed").start()
        main.postDelayed({ session.checkStall() }, stallMs)
        main.postDelayed({ session.finish(mapOf("error" to "timeout")) }, timeoutMs)
    }

    /** One recognition: the recogniser, the pipe, the reply owed to Dart. */
    private inner class Session(
        private val rec: SpeechRecognizer,
        private val readSide: ParcelFileDescriptor,
        private val writeSide: ParcelFileDescriptor,
        private val result: MethodChannel.Result,
    ) : RecognitionListener {

        private val done = AtomicBoolean(false)
        private val fed = AtomicLong(0)
        @Volatile private var fedAll = false
        private val started = System.nanoTime()

        /** On a worker thread: the clip in 100 ms pieces, then 400 ms of
         *  silence so the recogniser's end-pointer closes cleanly, then
         *  end-of-stream. A write blocks while the pipe is full, which is
         *  what makes a recogniser that never reads visible. */
        fun feed(pcm: ByteArray, sampleRate: Int) {
            try {
                ParcelFileDescriptor.AutoCloseOutputStream(writeSide).use { out ->
                    val chunk = sampleRate / 10 * 2
                    var off = 0
                    while (off < pcm.size && !done.get()) {
                        val n = minOf(chunk, pcm.size - off)
                        out.write(pcm, off, n)
                        off += n
                        fed.set(off.toLong())
                    }
                    if (!done.get()) {
                        val tail = ByteArray(sampleRate * 2 * 4 / 10)
                        out.write(tail)
                        fed.addAndGet(tail.size.toLong())
                    }
                }
                fedAll = true
            } catch (e: IOException) {
                // The reader went away: finished, cancelled, or never there.
            }
        }

        /** Nobody has read past the pipe's own buffer: the recogniser is
         *  not listening to our audio. Only decidable for a clip longer
         *  than the buffer; a short one fits without anyone reading. */
        fun checkStall() {
            if (done.get() || fedAll) return
            if (fed.get() <= PIPE_BYTES) {
                finish(mapOf("error" to "audio_source_ignored"))
            }
        }

        fun finish(payload: Map<String, Any?>) {
            if (!done.compareAndSet(false, true)) return
            main.post {
                try {
                    rec.cancel()
                } catch (e: Exception) {
                    // already stopped
                }
                try {
                    rec.destroy()
                } catch (e: Exception) {
                    // already gone
                }
                // Closing our read end (the recogniser's copy is gone with
                // it) makes a blocked writer fail out instead of hanging.
                try {
                    readSide.close()
                } catch (e: IOException) {
                    // already closed
                }
                try {
                    writeSide.close()
                } catch (e: IOException) {
                    // already closed
                }
                if (active === this) active = null
                val out = HashMap<String, Any?>(payload)
                out["fed"] = fed.get()
                out["fedAll"] = fedAll
                out["ms"] = (System.nanoTime() - started) / 1_000_000
                result.success(out)
            }
        }

        override fun onResults(results: Bundle?) {
            val texts = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
            val scores = results?.getFloatArray(SpeechRecognizer.CONFIDENCE_SCORES)
            finish(mapOf(
                "text" to (texts?.firstOrNull() ?: ""),
                "confidence" to scores?.firstOrNull()?.toDouble(),
            ))
        }

        override fun onError(error: Int) {
            finish(mapOf("error" to "recognizer", "code" to error))
        }

        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}
        override fun onPartialResults(partialResults: Bundle?) {}
        override fun onEvent(eventType: Int, params: Bundle?) {}
    }
}
