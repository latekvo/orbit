package dev.orbit.companion

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.util.Log

/** DNS-SD registration of `_orbit._tcp` (spec 1.1). */
class NsdAdvertiser(context: Context, private val onStatus: (String) -> Unit) {
    private val nsd = context.getSystemService(NsdManager::class.java)
    private var listener: NsdManager.RegistrationListener? = null

    fun start(name: String, port: Int, deviceId: String) {
        val info = NsdServiceInfo().apply {
            serviceName = name
            serviceType = SERVICE_TYPE
            setPort(port)
            setAttribute("v", "1")
            setAttribute("id", deviceId)
            setAttribute("pf", "android")
        }
        val l = object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(info: NsdServiceInfo) {
                Log.i(TAG, "nsd registered as ${info.serviceName}")
                onStatus("registered as \"${info.serviceName}\"")
            }
            override fun onRegistrationFailed(info: NsdServiceInfo, errorCode: Int) {
                Log.w(TAG, "nsd registration failed: $errorCode")
                onStatus("registration failed ($errorCode)")
            }
            override fun onServiceUnregistered(info: NsdServiceInfo) = onStatus("stopped")
            override fun onUnregistrationFailed(info: NsdServiceInfo, errorCode: Int) {}
        }
        listener = l
        try {
            nsd.registerService(info, NsdManager.PROTOCOL_DNS_SD, l)
            onStatus("registering")
        } catch (e: RuntimeException) {
            Log.w(TAG, "nsd unavailable", e)
            listener = null
            onStatus("unavailable: ${e.message}")
        }
    }

    fun stop() {
        listener?.let { runCatching { nsd.unregisterService(it) } }
        listener = null
    }

    companion object {
        const val SERVICE_TYPE = "_orbit._tcp"
    }
}
