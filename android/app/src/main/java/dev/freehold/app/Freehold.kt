package dev.freehold.app

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.BatteryManager
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONObject

/**
 * The only place Kotlin talks to Python.
 *
 * Everything goes through one function, `handle(json) -> json`, so there is
 * a single thing to keep in step across the two languages. Chunking,
 * deduplication and encryption are the same code the desktop runs; nothing
 * is reimplemented here.
 */
object Freehold {

    fun start(context: Context) {
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(context))
        }
    }

    private fun bridge() = Python.getInstance().getModule("freehold.mobile.bridge")

    /** The app's private directory. Not world-readable, no permission needed. */
    fun homeDir(context: Context): String =
        context.filesDir.resolve("freehold").apply { mkdirs() }.absolutePath

    private fun call(context: Context, request: JSONObject): JSONObject {
        start(context)
        request.put("home", homeDir(context))
        val reply = bridge().callAttr("handle", request.toString()).toString()
        return JSONObject(reply)
    }

    fun isEnrolled(context: Context): Boolean =
        java.io.File(homeDir(context), "key.json").exists()

    /** Join an existing Freehold using the key from the desktop. */
    fun enrol(context: Context, keyHex: String, device: String): JSONObject =
        call(context, JSONObject().put("cmd", "enrol")
            .put("key", keyHex).put("device", device))

    fun status(context: Context): JSONObject =
        call(context, JSONObject().put("cmd", "status"))

    fun addFolder(context: Context, path: String): JSONObject =
        call(context, JSONObject().put("cmd", "add_folder").put("path", path))

    fun addBackend(context: Context, name: String, driver: String,
                   options: JSONObject): JSONObject =
        call(context, JSONObject().put("cmd", "add_backend")
            .put("name", name).put("driver", driver).put("options", options))

    /** Store a token in the app's private directory. There is no shell
     *  environment on a phone to read one from. */
    fun setCredential(context: Context, name: String, value: String): JSONObject =
        call(context, JSONObject().put("cmd", "set_credential")
            .put("name", name).put("value", value))

    /**
     * Back up. The platform reports the things Python cannot see - what the
     * radio is doing, whether we are charging - and Python decides.
     */
    fun sync(context: Context, budgetSeconds: Int = 240): JSONObject =
        call(context, JSONObject()
            .put("cmd", "sync")
            .put("budget_seconds", budgetSeconds)
            .put("conditions", conditions(context)))

    fun shouldSync(context: Context): JSONObject =
        call(context, JSONObject().put("cmd", "should_sync")
            .put("conditions", conditions(context)))

    fun snapshots(context: Context): JSONObject =
        call(context, JSONObject().put("cmd", "snapshots"))

    fun restore(context: Context, snapshot: Int, dest: String): JSONObject =
        call(context, JSONObject().put("cmd", "restore")
            .put("snapshot", snapshot).put("dest", dest))

    // -- what the OS knows and Python does not -----------------------------
    private fun conditions(context: Context): JSONObject {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE)
                as ConnectivityManager
        val caps = cm.getNetworkCapabilities(cm.activeNetwork)
        val network = when {
            caps == null -> "offline"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "ethernet"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
            else -> "unknown"
        }
        val bm = context.getSystemService(Context.BATTERY_SERVICE)
                as BatteryManager
        return JSONObject()
            .put("network", network)
            .put("charging", bm.isCharging)
            .put("battery_pct",
                 bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY))
    }
}
