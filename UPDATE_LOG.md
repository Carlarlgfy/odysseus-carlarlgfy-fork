# Update Log

## Sunday, June 28, 2026

### Part Two: Continuous local voice conversation recovery fixes

I wrote and applied the code changes for the continuous local voice conversation loop after investigating why the first spoken turn worked but later turns could be silently ignored. The work stayed inside `/Users/carlmarquis-olson/Documents/code/odysseus-voice-test` and touched only the voice-loop frontend files:

- `static/js/voiceRecorder.js`
- `static/app.js`
- `static/js/tts-ai.js`

No cloud STT or TTS was added. The feature still uses local faster-whisper for transcription through `/api/stt/transcribe` and local Piper for text-to-speech.

#### Why this work was needed

The failure pattern pointed to the voice loop stalling before the user's second utterance could become a chat message. The most likely failure points were:

- VAD threshold drift: the adaptive noise floor could raise the speech threshold after the first turn, making normal second-turn speech fail to count as speech.
- Long VAD suppression: `odysseus:tts-start` suppressed VAD for up to 60 seconds and depended on `odysseus:tts-idle` to recover sooner.
- TTS playback hangs: Safari can be fragile around `audio.play()`, and a permanently pending playback promise could prevent the TTS queue from ever dispatching idle.
- Lost prelude audio: clearing the rolling audio buffer after every utterance made the next utterance easier to clip, especially if the user started speaking soon after TTS ended.
- MediaRecorder tick stalls: the 500ms rotating recorder depended on `onstop` firing every cycle. If Safari failed to fire it, the UI could remain active while no new audio chunks were captured.
- Queue deadlocks: transcripts could remain queued if `chatBusy`, streaming button state, or missing completion events prevented `processTranscriptQueue()` from running again.

The fixes were intentionally small and targeted rather than a broad rewrite of the voice system.

#### `static/js/voiceRecorder.js`

I capped the adaptive VAD threshold. Previously, the threshold was calculated as the larger of `minThreshold` and `noiseFloor * thresholdMultiplier`. That meant the first utterance could trigger easily, but after the room noise floor adapted upward, later speech might never cross the new threshold. I added a configurable `maxSilenceThreshold` with a default of `0.055`, then bounded the adaptive threshold between the minimum and that ceiling. This keeps the threshold from drifting into a range where normal laptop-mic speech is ignored.

I preserved the rolling prelude buffer across utterances. `_finalizeContinuousUtterance()` used to clear both `utteranceChunks` and `rollingChunks`. I removed the `rollingChunks` reset so the next utterance still has recent audio available as prelude. The existing sequence-number filtering still prevents duplicate chunks from being prepended when speech starts.

I added tick-capture liveness tracking for the Safari MediaRecorder loop. Continuous VAD mode captures audio with rotating 500ms recorders. If a recorder's `onstop` callback never fires, audio capture can silently die while VAD still appears active. I added `tickMs`, `lastTickAt`, and `tickRestarting` to the continuous state, update `lastTickAt` when ticks start and stop, and check from the VAD interval whether a tick has gone stale for more than 2500ms. If so, the code attempts a guarded restart.

I also tightened the restart logic so an old recorder that finally fires `onstop` after a watchdog restart does not spawn a duplicate recorder. The `onstop` handler now only starts the next tick if that recorder is still the active `st.recorder`.

#### `static/app.js`

I added explicit TTS busy state to the voice loop. `processTranscriptQueue()` now refuses to submit queued voice transcripts while TTS is active. This prevents the loop from submitting accidental transcripts while the assistant's spoken response is still playing or while the TTS system is still considered busy.

I added a TTS suppression watchdog. On `odysseus:tts-start`, the voice loop now marks `ttsBusy = true`, suppresses VAD for a bounded 30 seconds, and starts a watchdog timer. If `odysseus:tts-idle` does not arrive in time, the watchdog clears `ttsBusy`, unsuppresses VAD, and attempts to drain any queued transcript. This prevents the old "suppressed for 60 seconds and maybe never naturally recovers" behavior.

I updated the `odysseus:tts-idle` path to clear the watchdog, mark TTS idle, apply the normal 600ms post-TTS cooldown, clear `chatBusy`, and drain the transcript queue after the cooldown. This keeps the happy path natural while adding a recovery path for missed or delayed idle events.

I added a queue drain interval while continuous voice mode is active. Every second, the loop checks whether there are queued transcripts and tries to process them. The actual processing still respects `active`, `chatBusy`, `ttsBusy`, and the send button's streaming state, so the interval is a safety retry rather than a bypass.

I added a `chatBusy` timeout safety. When the voice loop submits a transcript, it records `chatBusySince`. If `chatBusy` remains true for more than 60 seconds and the send button is no longer streaming, the safety drain clears `chatBusy` so the queue can recover. This targets cases where a chat completion event is skipped or a background/session edge case prevents the normal completion path from firing.

I changed the VAD tuning passed from the app:

- `minSpeechMs` changed to `200`
- `thresholdMultiplier` changed to `2.4`
- `maxSilenceThreshold` added as `0.055`
- `silenceMs` remains the shorter continuous-conversation value of `2500`

The reasoning is that the prelude buffer already protects the beginning of speech, so requiring a full 300ms of loudness after VAD resumes was more likely to clip natural speech. Lowering the multiplier and adding the cap makes later utterances behave more like the first utterance instead of becoming much harder to trigger.

I also added optional VAD debug logging behind:

```js
localStorage.setItem('odysseus_voice_debug', '1')
```

When enabled, it logs throttled `rms`, `threshold`, `loud`, and `speaking` values once per second. It is disabled by default, so normal browser sessions stay quiet.

#### `static/js/tts-ai.js`

I made queued audio playback timeout-safe. `_playQueueItem()` previously awaited a promise that could remain pending if Safari never resolved or rejected `audio.play()`, or if playback never reached `ended`/`error`. That could stall `_processQueue()` and prevent `odysseus:tts-idle` from firing.

I wrapped the queued playback promise with a 30 second timeout. On timeout, the code pauses the audio if possible, clears `currentAudio`, marks `isPlaying = false`, resolves the queue item, and lets `_processQueue()` continue to the next item or dispatch `odysseus:tts-idle`. I also wrapped the `audio.play()` call itself in `try/catch` so a synchronous browser playback exception cleans up correctly.

This is intentionally recovery-oriented: a bad or stuck TTS item should not permanently kill continuous voice mode.

#### Important existing work preserved

The touched files already had unrelated local changes before this pass, including TTS volume controls and hardware volume key handling. I did not revert those. I layered the continuous voice recovery changes on top of the existing worktree state.

#### Verification performed

I ran JavaScript parser checks on the modified files:

```sh
node --check static/js/voiceRecorder.js
node --check static/app.js
node --check static/js/tts-ai.js
```

I also ran whitespace/diff validation:

```sh
git diff --check -- static/js/voiceRecorder.js static/app.js static/js/tts-ai.js
```

All checks passed.

#### What still needs manual verification

The meaningful end-to-end test requires Safari, microphone permission, local faster-whisper, and local Piper on the running app at port `7861`. The manual validation should confirm:

- Voice mode starts and captures the first utterance.
- The first transcript submits to chat.
- The assistant response is spoken by Piper.
- VAD resumes after TTS.
- Second and later utterances are detected at normal laptop distance.
- Speaking shortly after TTS ends does not clip the first word.
- Stopping voice mode during TTS does not submit stale queued transcripts.
- Long or failed TTS playback does not leave VAD suppressed indefinitely.

The code was written to fix the most likely "works once, then ignores later speech" causes without changing the chat streaming architecture or adding cloud dependencies.
