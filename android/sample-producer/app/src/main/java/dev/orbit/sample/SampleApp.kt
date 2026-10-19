package dev.orbit.sample

import android.app.Application

class SampleApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Producers.init(this)
    }
}
