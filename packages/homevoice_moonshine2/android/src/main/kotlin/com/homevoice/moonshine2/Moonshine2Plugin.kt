// HomeVoice -- Moonshine's newer models, fed with the app's own audio.
//
// WHY THIS EXISTS. The app's built-in recogniser is Moonshine version 1
// (base), through sherpa-onnx, and that library does not carry the newer
// Small and Medium models -- the ones with the real gain in accuracy. They
// come through Moonshine's own Android library, which is what this wraps.
//
// THE APP KEEPS THE MICROPHONE. Nothing here records: the app hands over a
// finished capture (16-bit mono PCM) and gets words back, so the hub still
// receives the same audio for its voice check. That is the whole reason
// this works on Android 12 and below, where the phone's own recogniser
// cannot be used (it would take the microphone for itself).
//
// The calls into the library are the ones a published app makes with this
// same version (0.1.5): MicTranscriber(context).language(..).modelArch(..),
// load() -- which downloads the model the first time -- and
// transcribeWithoutStreaming(floats, 16000).text(). MicTranscriber is used
// only because it is the class that knows how to fetch a model; its
// microphone is never started.
//
// Every call answers with a map and never throws across the channel: a
// phone this cannot run on must leave the app exactly as it was, on its
// built-in recogniser.
package com.homevoice.moonshine2

import ai.moonshine.voice.JNI
import ai.moonshine.voice.MicTranscriber
import ai.moonshine.voice.ModelCache
import android.content.Context
import android.os.Build
import android.os.Handler
import android.os.Looper
import io.flutter.embedding.engine.plugins.FlutterPlugin
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.io.File
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

class Moonshine2Plugin : FlutterPlugin, MethodChannel.MethodCallHandler {

    companion object {
        const val CHANNEL = "homevoice/moonshine2"
    }

    private lateinit var channel: MethodChannel
    private lateinit var context: Context
    private val main = Handler(Looper.getMainLooper())

    // ONE thread for the model: loading and reading are never run side by
    // side, and neither is ever run on the UI thread (a load takes seconds,
    // or minutes when it has to download first).
    private val worker: ExecutorService = Executors.newSingleThreadExecutor { r ->
        // A daemon: it must never be what keeps the app's process alive.
        Thread(r, "homevoice-moonshine2").apply { isDaemon = true }
    }

    @Volatile private var transcriber: MicTranscriber? = null
    @Volatile private var loadedModel: String? = null
    @Volatile private var loading: String? = null
    @Volatile private var progress: Int = 0
    @Volatile private var lastError: String = ""
    @Volatile private var folderNote: String = ""
    @Volatile private var copyNote: String = ""

    override fun onAttachedToEngine(binding: FlutterPlugin.FlutterPluginBinding) {
        context = binding.applicationContext
        channel = MethodChannel(binding.binaryMessenger, CHANNEL)
        channel.setMethodCallHandler(this)
    }

    override fun onDetachedFromEngine(binding: FlutterPlugin.FlutterPluginBinding) {
        channel.setMethodCallHandler(null)
        worker.execute { closeQuietly() }
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "status" -> result.success(status())
            "load" -> load(call.argument<String>("model") ?: "small", result)
            "transcribe" -> {
                val pcm = call.argument<ByteArray>("pcm")
                if (pcm == null) {
                    result.success(mapOf("error" to "no_audio"))
                    return
                }
                transcribe(pcm, call.argument<Int>("sampleRate") ?: 16000, result)
            }
            "unload" -> worker.execute {
                closeQuietly()
                main.post { result.success(null) }
            }
            else -> result.notImplemented()
        }
    }

    private fun status(): Map<String, Any?> = mapOf(
        "sdk" to Build.VERSION.SDK_INT,
        // A 32-bit phone lists no arm64-v8a here; the library is built for
        // 64-bit, so "why will it not load" can be read off this line.
        "abis" to Build.SUPPORTED_ABIS.toList(),
        "loaded" to loadedModel,
        "loading" to loading,
        "progress" to progress,
        "error" to lastError,
        // Where the library keeps a model it downloaded itself (private to
        // the app: kept across an update, gone on uninstall), and what
        // happened with a folder the person supplied.
        "cache" to File(context.noBackupFilesDir, "moonshine-models").absolutePath,
        "folder" to (folderFor(loadedModel ?: loading ?: "small")?.absolutePath ?: ""),
        "folderNote" to folderNote,
    )

    private fun archFor(model: String): Int = when (model) {
        "tiny" -> JNI.MOONSHINE_MODEL_ARCH_TINY_STREAMING
        "base" -> JNI.MOONSHINE_MODEL_ARCH_BASE_STREAMING
        "medium" -> JNI.MOONSHINE_MODEL_ARCH_MEDIUM_STREAMING
        else -> JNI.MOONSHINE_MODEL_ARCH_SMALL_STREAMING
    }

    /** Where a model the person copied over is looked for: the app's own
     *  files folder that a computer can see over USB, beside the other
     *  models -- .../files/models/moonshine2/<size>/. */
    private fun folderFor(model: String): File? {
        val root = context.getExternalFilesDir(null) ?: return null
        return File(root, "models/moonshine2/$model")
    }

    /** The library's own folder for `model` (private to the app), when it
     *  has downloaded it: .../no_backup/moonshine-models/stt-en-<number>. */
    private fun libraryCopy(model: String): File? = try {
        val arch = archFor(model)
        ModelCache.defaultRoot(context).listFiles()?.firstOrNull {
            it.isDirectory && it.name.endsWith("-$arch") &&
                !it.list().isNullOrEmpty()
        }
    } catch (_: Throwable) {
        null
    }

    /** Copy the library's downloaded model into the reachable folder, when
     *  that folder is still empty. A failure here is only a note: the model
     *  itself is loaded and working. */
    private fun saveACopy(model: String) {
        copyNote = try {
            val from = libraryCopy(model)
            val to = folderFor(model)
            when {
                from == null -> "no copy saved: the downloaded files were not found"
                to == null -> "no copy saved: the phone's files folder is not available"
                folderHasFiles(model) -> "a copy is already in ${to.absolutePath}"
                else -> {
                    from.copyRecursively(to, overwrite = true)
                    "a copy was saved to ${to.absolutePath}"
                }
            }
        } catch (e: Throwable) {
            "no copy saved: ${e.javaClass.simpleName}: ${e.message ?: "no detail"}"
        }
    }

    private fun folderHasFiles(model: String): Boolean {
        val dir = folderFor(model) ?: return false
        return dir.isDirectory && !dir.list().isNullOrEmpty()
    }

    /** Point the library at that folder when it is there, so that load()
     *  opens those files and downloads nothing. False when there is no
     *  such folder, or this version of the library has no way to be
     *  pointed at one.
     *
     *  The call is `modelsFrom(path)` (Moonshine's documentation: "for
     *  fully offline builds ... point the engine at them with
     *  modelsFrom(path)"). An earlier version of this looked for
     *  `loadFromFiles`, which this library does not have ("this library
     *  version cannot load from a folder", field test 2026-10-07).
     *
     *  BY NAME, AT RUN TIME: it could not be compiled against here, and
     *  looked up this way a wrong guess costs a line in `folderNote` --
     *  which then lists what the library does have -- not a build that
     *  will not compile. */
    private fun pointAtFolder(mic: MicTranscriber, model: String): Boolean {
        val dir = folderFor(model)
        if (dir == null || !dir.isDirectory || dir.list().isNullOrEmpty()) {
            folderNote = "no folder at ${dir?.absolutePath}"
            return false
        }
        return try {
            val method = mic.javaClass.methods.firstOrNull {
                it.name == "modelsFrom" && it.parameterTypes.size == 1 &&
                    (it.parameterTypes[0] == String::class.java ||
                        it.parameterTypes[0] == File::class.java)
            }
            if (method == null) {
                val has = mic.javaClass.methods.map { it.name }.distinct()
                    .filter { n ->
                        listOf("model", "load", "from", "path", "asset", "file")
                            .any { n.contains(it, ignoreCase = true) }
                    }.sorted().joinToString(", ")
                folderNote = "this library has no modelsFrom; it has: $has"
                false
            } else {
                if (method.parameterTypes[0] == File::class.java) {
                    method.invoke(mic, dir)
                } else {
                    method.invoke(mic, dir.absolutePath)
                }
                folderNote = "pointed at ${dir.absolutePath}"
                true
            }
        } catch (e: Throwable) {
            val cause = e.cause ?: e
            folderNote = "the folder could not be used: " +
                "${cause.javaClass.simpleName}: ${cause.message ?: "no detail"}"
            false
        }
    }

    /** An error with what caused it, as far down as it goes: the library's
     *  own message is one line ("Failed to load the speech-to-text model")
     *  and the reason is in what it wraps. */
    private fun whole(e: Throwable): String {
        val parts = ArrayList<String>()
        var at: Throwable? = e
        while (at != null && parts.size < 4) {
            parts.add("${at.javaClass.simpleName}: ${at.message ?: "no detail"}")
            at = at.cause
        }
        return parts.joinToString(" <- ")
    }

    private fun closeQuietly() {
        try {
            transcriber?.close()
        } catch (_: Throwable) {
        }
        transcriber = null
        loadedModel = null
    }

    /** Load `model` (downloading it the first time). Answers when it is
     *  ready or has failed: {ok, model, ms} or {error}. */
    private fun load(model: String, result: MethodChannel.Result) {
        worker.execute {
            val answer: Map<String, Any?> = try {
                if (loadedModel == model && transcriber?.isLoaded == true) {
                    mapOf("ok" to true, "model" to model, "ms" to 0)
                } else {
                    closeQuietly()
                    loading = model
                    progress = 0
                    lastError = ""
                    val began = System.currentTimeMillis()
                    // Not chained into the declaration: whatever type the
                    // two setters return, `mic` stays a MicTranscriber.
                    val mic = MicTranscriber(context)
                    mic.language("en").modelArch(archFor(model))
                    mic.onProgress { fraction, _ ->
                        progress = (fraction * 100).toInt()
                    }
                    // A COPY THE PERSON PUT ON THE PHONE comes first: no
                    // internet needed, and it survives a reinstall.
                    val pointed = pointAtFolder(mic, model)
                    // HER COPY IS THERE AND CANNOT BE USED: stop and say so.
                    // Never a silent download behind it -- the copy exists
                    // precisely so that data is not spent twice.
                    // (Unless the library already holds this model itself:
                    // loading that costs no data either.)
                    if (!pointed && folderHasFiles(model) &&
                        libraryCopy(model) == null
                    ) {
                        try {
                            mic.close()
                        } catch (_: Throwable) {
                        }
                        throw IllegalStateException(
                            "your copy in models/moonshine2/$model was not " +
                                "used ($folderNote) -- nothing was downloaded")
                    }
                    try {
                        mic.load()
                    } catch (e: Throwable) {
                        try {
                            mic.close()
                        } catch (_: Throwable) {
                        }
                        if (pointed) {
                            throw IllegalStateException(
                                "your copy in models/moonshine2/$model was " +
                                    "found and would not load: ${whole(e)}")
                        }
                        throw IllegalStateException(whole(e))
                    }
                    val from = if (pointed) "folder" else "download"
                    transcriber = mic
                    loadedModel = model
                    // WHAT WAS DOWNLOADED IS PUT WHERE SHE CAN REACH IT, once:
                    // the app's files folder, which a computer sees over
                    // USB. From there it can be kept on a laptop and copied
                    // back after a fresh install, with no second download.
                    if (from == "download") saveACopy(model)
                    mapOf(
                        "ok" to true,
                        "model" to model,
                        "from" to from,
                        "saved" to copyNote,
                        "ms" to (System.currentTimeMillis() - began).toInt(),
                    )
                }
            } catch (e: Throwable) {
                // Throwable, not Exception: a phone whose processor the
                // library was not built for fails with UnsatisfiedLinkError,
                // and a model too big for its memory with OutOfMemoryError.
                lastError = "${e.javaClass.simpleName}: ${e.message ?: "no detail"}"
                closeQuietly()
                mapOf("error" to lastError)
            } finally {
                loading = null
            }
            main.post { result.success(answer) }
        }
    }

    /** One finished capture -> words: {text, ms} or {error}. */
    private fun transcribe(pcm: ByteArray, sampleRate: Int, result: MethodChannel.Result) {
        worker.execute {
            val answer: Map<String, Any?> = try {
                val t = transcriber
                if (t == null || loadedModel == null) {
                    mapOf("error" to "not_loaded")
                } else {
                    // 16-bit little-endian mono -> floats in [-1, 1].
                    val floats = FloatArray(pcm.size / 2)
                    for (i in floats.indices) {
                        val lo = pcm[2 * i].toInt() and 0xFF
                        val hi = pcm[2 * i + 1].toInt()
                        floats[i] = ((hi shl 8) or lo) / 32768f
                    }
                    val began = System.currentTimeMillis()
                    val transcript = t.transcribeWithoutStreaming(floats, sampleRate)
                    val text = (transcript.text() ?: "").trim()
                    mapOf(
                        "text" to text,
                        "ms" to (System.currentTimeMillis() - began).toInt(),
                    )
                }
            } catch (e: Throwable) {
                lastError = "${e.javaClass.simpleName}: ${e.message ?: "no detail"}"
                if (e is OutOfMemoryError) closeQuietly()
                mapOf("error" to lastError)
            }
            main.post { result.success(answer) }
        }
    }
}
