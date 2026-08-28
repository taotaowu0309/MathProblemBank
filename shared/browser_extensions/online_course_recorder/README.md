# Web Course Recorder

This unpacked Chrome extension records the active YouTube or Bilibili tab into
the course prepared in Math Problem Bank. Captured chunks and captions,
LaTeX sources, and PDFs are stored below the configured course root
(`$MATH_PROBLEM_BANK_COURSE_ROOT` in a public installation).

Audio is captured exclusively from the selected browser tab through
`chrome.tabCapture`. The extension does not request microphone permission,
does not mix a microphone track, and never falls back to microphone input. If
Chrome cannot provide exactly one tab-audio track, recording is cancelled and
the newly opened local session is closed instead of creating a silent or
environment-noise recording.

Before a local recording session is created, the receiver sends a text-only
Agent API preflight. A blocked or unavailable model route therefore rejects the
Start action before any course evidence is accumulated. The extension also
rejects a muted web player or volume 0. While the video is playing it measures
the actual captured tab-audio signal; 15 continuous minutes of source-video time
without valid captured tab audio stops the recording with an `ERR` badge and
visible page warning. Short or intermittent silence resets when valid audio
returns. The receiver repeats
the level check before ASR so digital silence is never sent to Whisper.
Because `chrome.tabCapture` removes the tab from Chrome's ordinary audio output,
the extension replays the captured tab track to the default output device while
recording. This lets the lecturer remain audible when Windows output and the web
player are unmuted. The replay branch does not enter the recorded stream and does
not request or mix microphone audio.

Installation for the first test:

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Choose **Load unpacked**.
4. Select this directory.
5. In Math Problem Bank, select a project, open **网课讲义**, create a course,
   and select it. The recorder target is synchronized automatically.
6. Open a YouTube or Bilibili episode and use the extension popup to start and
   stop recording.
7. Chrome may show **Service Worker (Inactive)** whenever the recorder is idle.
   This is the normal Manifest V3 lifecycle, not an extension failure; opening
   the popup wakes it automatically. After a genuinely incompatible recorder
   update, the popup shows **Reload updated recorder**. Use that button once;
   ordinary Math Problem Bank code changes do not require an extension reload.

The recorder accepts pauses, seeks, replays, buffering, and playback-rate
changes. It saves independent 12-second WebM chunks so a browser or network
failure does not discard the entire session. In parallel, one bounded browser
analysis stream sends formula-preserving JPEG frames at up to 1024 pixels wide.
The normal path uses about 2 fps while stable and up to about 5 fps while a
change is settling. Each frame carries the authoritative player timestamp.

When a YouTube playlist or Bilibili multipart video autoplays or navigates to
the next native video identity, the recorder freezes the completed episode
boundary, pauses the source webpage's video player, flushes the current WebM
chunk, ends the recording automatically, and asks Math Problem Bank to open the
lecture-notes page for the recorded course. A player-pause failure is reported
in the extension console but never blocks recording cleanup. The next episode
is not recorded until the user explicitly starts a new recording.

The Python receiver performs page-level and grid-local mathematical-stroke
change detection online, waits for a stable clear frame, and atomically saves
only candidate states plus compact change-region metadata. It keeps constant
memory and finalizes the tail when recording stops. WebM decoding and the old
whole-session look-around are not part of the normal candidate path; the WebM
chunks remain the recoverable source. A missing live-frame manifest stops
material generation with an explicit extension-reload error. Only concrete gap
ranges recorded by a finalized manifest may decode a bounded subset of WebM
chunks (at most 8 chunks and 90 seconds). Audio chunks are transcribed by two
ASR workers independently of visual analysis.

Keep Math Problem Bank open while recording so the loopback receiver remains
available. An Internet outage only pauses the website video: already completed
chunks stay on disk, frozen intervals are excluded from transcription, and
binary writes are retried locally. After the video resumes, recording continues
in the same episode. Stop the extension before closing the control center so the
last chunk is fully written.
