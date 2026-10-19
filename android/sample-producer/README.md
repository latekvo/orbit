# Orbit sample producer (`dev.orbit.sample`)

A test app that acts like third-party apps posting live activities, so the Orbit companion
(NotificationListenerService + MediaSessionManager) can be tested end to end on an emulator.

| Producer | What it posts | Actions (`action` id) |
|---|---|---|
| `media` | `MediaSession` + MediaStyle notification (FGS `mediaPlayback`), 3 tracks with distinct 256x256 art | `media.play`, `media.pause`, `media.next`, `media.previous`, `media.seek` (`value` = ms), `media.stop` |
| `download` | Ongoing `progress` notification, 0 -> 100% over 60 s | `download.pause`, `download.resume`, `download.cancel` |
| `timer` | Ongoing count-down chronometer (`when` = end time), 5 min | `timer.pause`, `timer.resume`, `timer.add_minute` |
| `reply` | Ongoing message notification with a RemoteInput "Reply" action | `reply.send` (`text`) |

Every producer also takes `<name>.start` / `<name>.stop`. `timer.start` accepts an optional `value`
(duration in ms). `media.stop` ends the player: the session is released and the notification removed.
`media.previous` always goes to the previous track. When a track ends, playback moves to the next one.

All notification actions are broadcast PendingIntents to `ActionReceiver`, so a listener can fire them
with `PendingIntent.send()`. The media session's transport controls go through the same handlers.

## Build and install

```sh
export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
./gradlew assembleDebug lintDebug
adb install -r -g app/build/outputs/apk/debug/app-debug.apk   # -g grants POST_NOTIFICATIONS
```

## Automation

```sh
# start / stop producers (comma list or "all"); the activity route can start foreground services
adb shell am start -n dev.orbit.sample/.MainActivity --es start media,download,timer,reply
adb shell am start -n dev.orbit.sample/.MainActivity --es stop all

# fire any action without the UI (an explicit component is required for manifest receivers)
adb shell am broadcast -n dev.orbit.sample/.ActionReceiver --es action download.pause
adb shell am broadcast -n dev.orbit.sample/.ActionReceiver --es action media.seek --el value 200000
adb shell am broadcast -n dev.orbit.sample/.ActionReceiver --es action timer.start --el value 10000
adb shell am broadcast -n dev.orbit.sample/.ActionReceiver --es action reply.send --es text "'quoted for the device shell'"

# media keys reach the session while it is the system's media button session
adb shell cmd media_session dispatch play-pause   # also play, pause, next, previous, stop
adb shell dumpsys media_session                    # session "OrbitSample": state, position, metadata
```

The receiver also takes `--es start <list>` / `--es stop <list>`. That works from adb even with the app in
the background. A broadcast from another app may hit Android's foreground-service start restrictions
instead, logged as `result=error(...)`; the activity route always works.

## Logs

Each action produces one line (tag `OrbitSample`) with its source and the resulting state:

```
adb logcat -s OrbitSample
I OrbitSample: action=timer.add_minute source=broadcast result=ok state=timer{active=true running=true ... remaining=335114 (5:36) ...}
I OrbitSample: action=reply.send text="hi" source=remote_input result=ok state=reply{active=true count=1 last="hi"}
I OrbitSample: event=download.complete state=download{active=false paused=false finished=true progress=100%}
```

`source` is `ui`, `intent` (activity extras), `broadcast` (a notification action or adb), `remote_input`,
`session` (MediaSession transport controls / media keys) or `track_end`. `result` is `ok`,
`ignored(<reason>)` or `error(<reason>)`. The app screen shows the same state and the latest lines.

## Notes

- The player loops a silent `AudioTrack` while playing. Android routes media keys only to the session of
  the app that last produced audio, so without it `cmd media_session dispatch` would not reach the session.
- While download, timer or reply is active, `KeepAliveService` (FGS `dataSync`) keeps the process from
  being frozen in the background. It uses one producer's own notification, so no extra notification appears.
- Each producer's notification has its own group key, so Android does not bundle them under one
  system-generated summary (which separate real apps would never get).
- On Android 13+ the shade's media player draws its buttons from the session's PlaybackState actions;
  the notification's own actions are still there for listeners.
