package dev.orbit.companion

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.drawable.BitmapDrawable
import android.graphics.drawable.Drawable
import android.graphics.drawable.Icon
import dev.orbit.protocol.Art
import java.io.ByteArrayOutputStream

/** Converts icons and artwork to [Art] (at most 256x256; PNG for icons, JPEG for art). */
class ArtCache(private val context: Context) {
    private val appIcons = HashMap<String, Art?>()

    fun appIcon(pkg: String): Art? = synchronized(appIcons) {
        appIcons.getOrPut(pkg) {
            runCatching { encode(drawableToBitmap(context.packageManager.getApplicationIcon(pkg), 128), png = true) }.getOrNull()
        }
    }

    fun art(bitmap: Bitmap?): Art? = bitmap?.let { runCatching { encode(downscale(it), png = false) }.getOrNull() }

    fun art(icon: Icon?): Art? = icon?.let { i ->
        runCatching { i.loadDrawable(context)?.let { encode(drawableToBitmap(it, MAX), png = false) } }.getOrNull()
    }

    private fun encode(bmp: Bitmap, png: Boolean): Art {
        val out = ByteArrayOutputStream()
        if (png) {
            bmp.compress(Bitmap.CompressFormat.PNG, 100, out)
        } else {
            // JPEG has no alpha: flatten onto white.
            val flat = Bitmap.createBitmap(bmp.width, bmp.height, Bitmap.Config.ARGB_8888)
            Canvas(flat).apply { drawColor(android.graphics.Color.WHITE); drawBitmap(bmp, 0f, 0f, null) }
            flat.compress(Bitmap.CompressFormat.JPEG, 85, out)
        }
        return Art(if (png) "image/png" else "image/jpeg", out.toByteArray())
    }

    private fun downscale(bmp: Bitmap): Bitmap {
        val src = if (bmp.config == Bitmap.Config.HARDWARE) bmp.copy(Bitmap.Config.ARGB_8888, false) else bmp
        val scale = minOf(1f, MAX.toFloat() / maxOf(src.width, src.height))
        if (scale >= 1f) return src
        return Bitmap.createScaledBitmap(src, maxOf(1, (src.width * scale).toInt()), maxOf(1, (src.height * scale).toInt()), true)
    }

    private fun drawableToBitmap(d: Drawable, max: Int): Bitmap {
        if (d is BitmapDrawable && d.bitmap != null) return downscale(d.bitmap)
        val w = d.intrinsicWidth.takeIf { it > 0 } ?: max
        val h = d.intrinsicHeight.takeIf { it > 0 } ?: max
        val scale = minOf(1f, max.toFloat() / maxOf(w, h))
        val bmp = Bitmap.createBitmap(maxOf(1, (w * scale).toInt()), maxOf(1, (h * scale).toInt()), Bitmap.Config.ARGB_8888)
        d.setBounds(0, 0, bmp.width, bmp.height)
        d.draw(Canvas(bmp))
        return bmp
    }

    companion object {
        const val MAX = 256
    }
}
