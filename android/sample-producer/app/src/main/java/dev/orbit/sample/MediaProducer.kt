package dev.orbit.sample

import android.app.Notification
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Shader
import android.media.MediaMetadata
import android.media.session.MediaSession
import android.media.session.PlaybackState
import android.os.SystemClock

/**
 * Silent fake music player: a real [MediaSession] whose PlaybackState is truthful (position
 * extrapolates at speed 1 while playing) plus a MediaStyle notification owned by [MediaService].
 * `previous` always goes to the previous track (no restart-current-track rule) so tests are deterministic.
 */
class MediaProducer(private val ctx: Context) : Producer {
    data class Track(val title: String, val artist: String, val album: String, val durationMs: Long, val color: Int)

    val tracks = listOf(
        Track("Neon Tide", "The Parallax", "Signal Bloom", 215_000, 0xFF1DB954.toInt()),
        Track("Paper Satellites", "Mira Kade", "Low Orbit", 242_000, 0xFFE8603C.toInt()),
        Track("Glass Harbor", "Northbound Echo", "Tidelines", 168_000, 0xFF3C7BE8.toInt()),
    )
    private val art = HashMap<Int, Bitmap>()

    override val name = "media"
    override val notifId = 1001
    override var active = false
        private set

    var session: MediaSession? = null
        private set
    var index = 0
        private set
    private var state = PlaybackState.STATE_NONE
    private var basePos = 0L
    private var baseAt = 0L

    private val track get() = tracks[index]
    private val playing get() = state == PlaybackState.STATE_PLAYING

    fun position(): Long =
        if (playing) (basePos + SystemClock.elapsedRealtime() - baseAt).coerceAtMost(track.durationMs) else basePos

    override fun handle(verb: String, value: Long?, text: String?): String {
        if (verb == "start") {
            if (active) return "ignored(already active)"
            start()
            return "ok"
        }
        if (!active) return "ignored(not active)"
        when (verb) {
            "play" -> if (playing) return "ignored(already playing)" else setState(PlaybackState.STATE_PLAYING, position())
            "pause" -> if (!playing) return "ignored(not playing)" else setState(PlaybackState.STATE_PAUSED, position())
            "next" -> selectTrack((index + 1) % tracks.size)
            "previous" -> selectTrack((index - 1 + tracks.size) % tracks.size)
            "seek" -> {
                val pos = value ?: return "error(missing value)"
                setState(state, pos.coerceIn(0, track.durationMs))
            }
            "stop" -> stop()
            else -> return "error(unknown verb)"
        }
        return "ok"
    }

    private fun start() {
        val s = MediaSession(ctx, "OrbitSample")
        s.setCallback(object : MediaSession.Callback() {
            override fun onPlay() = Producers.dispatch("media.play", source = "session")
            override fun onPause() = Producers.dispatch("media.pause", source = "session")
            override fun onSkipToNext() = Producers.dispatch("media.next", source = "session")
            override fun onSkipToPrevious() = Producers.dispatch("media.previous", source = "session")
            override fun onSeekTo(pos: Long) = Producers.dispatch("media.seek", pos, source = "session")
            override fun onStop() = Producers.dispatch("media.stop", source = "session")
        })
        s.setSessionActivity(Producers.openApp(ctx))
        s.isActive = true
        session = s
        active = true
        index = 0
        state = PlaybackState.STATE_PLAYING
        basePos = 0
        baseAt = SystemClock.elapsedRealtime()
        publish(metadata = true)
        ctx.startForegroundService(Intent(ctx, MediaService::class.java))
    }

    private fun stop() {
        state = PlaybackState.STATE_STOPPED
        basePos = 0
        baseAt = SystemClock.elapsedRealtime()
        session?.setPlaybackState(playbackState())
        session?.isActive = false
        session?.release()
        session = null
        active = false
        MediaService.instance?.dismiss()
        Producers.nm.cancel(notifId)
    }

    private fun setState(newState: Int, pos: Long) {
        state = newState
        basePos = pos
        baseAt = SystemClock.elapsedRealtime()
        publish(metadata = false)
    }

    private fun selectTrack(i: Int) {
        index = i
        basePos = 0
        baseAt = SystemClock.elapsedRealtime()
        publish(metadata = true)
    }

    override fun tick() {
        if (active && playing && position() >= track.durationMs) {
            Producers.dispatch("media.next", source = "track_end")
        }
    }

    private fun playbackState(): PlaybackState = PlaybackState.Builder()
        .setActions(
            PlaybackState.ACTION_PLAY or PlaybackState.ACTION_PAUSE or PlaybackState.ACTION_PLAY_PAUSE or
                PlaybackState.ACTION_SKIP_TO_NEXT or PlaybackState.ACTION_SKIP_TO_PREVIOUS or
                PlaybackState.ACTION_SEEK_TO or PlaybackState.ACTION_STOP
        )
        .setState(state, basePos, if (playing) 1f else 0f, baseAt)
        .build()

    private fun publish(metadata: Boolean) {
        val s = session ?: return
        if (metadata) {
            s.setMetadata(
                MediaMetadata.Builder()
                    .putString(MediaMetadata.METADATA_KEY_TITLE, track.title)
                    .putString(MediaMetadata.METADATA_KEY_ARTIST, track.artist)
                    .putString(MediaMetadata.METADATA_KEY_ALBUM, track.album)
                    .putLong(MediaMetadata.METADATA_KEY_DURATION, track.durationMs)
                    .putLong(MediaMetadata.METADATA_KEY_TRACK_NUMBER, index + 1L)
                    .putLong(MediaMetadata.METADATA_KEY_NUM_TRACKS, tracks.size.toLong())
                    .putBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART, artFor(index))
                    .build()
            )
        }
        s.setPlaybackState(playbackState())
        Producers.nm.notify(notifId, buildNotification())
    }

    override fun buildNotification(): Notification {
        val b = Notification.Builder(ctx, Producers.CH_MEDIA)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentTitle(track.title)
            .setContentText(track.artist)
            .setSubText(track.album)
            .setLargeIcon(artFor(index))
            .setColor(track.color)
            .setContentIntent(Producers.openApp(ctx))
            .setCategory(Notification.CATEGORY_TRANSPORT)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .setOngoing(true)
            .setShowWhen(false)
            .setOnlyAlertOnce(true)
            .addAction(Producers.action(ctx, android.R.drawable.ic_media_previous, "Previous", "media.previous"))
            .addAction(
                if (playing) Producers.action(ctx, android.R.drawable.ic_media_pause, "Pause", "media.pause")
                else Producers.action(ctx, android.R.drawable.ic_media_play, "Play", "media.play")
            )
            .addAction(Producers.action(ctx, android.R.drawable.ic_media_next, "Next", "media.next"))
            .addAction(Producers.action(ctx, android.R.drawable.ic_menu_close_clear_cancel, "Stop", "media.stop"))
        val style = Notification.MediaStyle().setShowActionsInCompactView(0, 1, 2)
        session?.let { style.setMediaSession(it.sessionToken) }
        return b.setStyle(style).build()
    }

    override fun describe(): String {
        if (!active) return "media{active=false}"
        val st = when (state) {
            PlaybackState.STATE_PLAYING -> "playing"
            PlaybackState.STATE_PAUSED -> "paused"
            PlaybackState.STATE_STOPPED -> "stopped"
            else -> "state$state"
        }
        return "media{state=$st track=${index + 1}/${tracks.size} title=\"${track.title}\" artist=\"${track.artist}\" " +
            "pos=${position()} dur=${track.durationMs} (${Producers.fmt(position())}/${Producers.fmt(track.durationMs)})}"
    }

    /** 256x256 album art: per-track gradient with concentric rings and the album initials. */
    private fun artFor(i: Int): Bitmap = art.getOrPut(i) {
        val t = tracks[i]
        val size = 256f
        val bmp = Bitmap.createBitmap(256, 256, Bitmap.Config.ARGB_8888)
        val c = Canvas(bmp)
        val dark = Color.rgb(Color.red(t.color) / 4, Color.green(t.color) / 4, Color.blue(t.color) / 4)
        val p = Paint(Paint.ANTI_ALIAS_FLAG)
        p.shader = LinearGradient(0f, 0f, size, size, t.color, dark, Shader.TileMode.CLAMP)
        c.drawRect(0f, 0f, size, size, p)
        p.shader = null
        p.style = Paint.Style.STROKE
        p.strokeWidth = 6f
        p.color = Color.argb(70, 255, 255, 255)
        for (r in 1..(3 + i)) c.drawCircle(size * 0.72f, size * 0.28f, r * 22f, p)
        p.style = Paint.Style.FILL
        p.color = Color.WHITE
        p.textSize = 88f
        p.isFakeBoldText = true
        val initials = t.album.split(' ').joinToString("") { it.take(1) }
        c.drawText(initials, 22f, size - 30f, p)
        bmp
    }
}
