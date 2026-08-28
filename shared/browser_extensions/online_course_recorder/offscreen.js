const RECEIVER = "http://127.0.0.1:8765";
const ANALYSIS_MAX_WIDTH = 1024;
const ANALYSIS_DEFAULT_INTERVAL_MS = 500;
const CONTINUOUS_SILENCE_STOP_VIDEO_SECONDS = 15 * 60;
const DIGITAL_SILENCE_PEAK_THRESHOLD = 0.0001;
const MIN_AUDIO_LEVEL_SAMPLES_PER_CHUNK = 30;
let capture = null;

function recorderMimeType() {
  for (const value of ["video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus", "video/webm"]) {
    if (MediaRecorder.isTypeSupported(value)) return value;
  }
  return "";
}

async function uploadBinary(path, sessionId, metadata, blob) {
  let lastError = null;
  // Local receiver requests can briefly race with the recorder worker or a
  // Windows sleep/wake transition.  Keep retrying the same frame/chunk long
  // enough that a transient failure does not create a permanent analysis gap.
  for (const delay of [0, 400, 1200, 2500, 5000, 9000, 15000]) {
    if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
    try {
      const response = await fetch(RECEIVER + path, {
        method: "POST",
        headers: {
          "Content-Type": blob.type || "application/octet-stream",
          "X-Session-Id": sessionId,
          "X-Recorder-Metadata": encodeURIComponent(JSON.stringify(metadata)),
        },
        body: blob,
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || `Receiver HTTP ${response.status}`);
      return payload.data || null;
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error("Local receiver upload failed.");
}

function estimatedVideoTime(current) {
  const player = current.player || {};
  const base = Number(player.current_time || 0);
  if (player.paused || ["waiting", "stalled", "seeking", "ended"].includes(player.state)) return base;
  const elapsed = Math.max(0, performance.now() - current.playerUpdatedAt) / 1000;
  return base + elapsed * Math.max(0.1, Number(player.playback_rate || 1));
}

async function captureAnalysisFrame(current, force = false) {
  if (!current || current.analysisBusy || (!force && !current.running)) return;
  if (current.episodeBoundary) return;
  if (!current.video || current.video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return;
  const videoTime = estimatedVideoTime(current);
  const playerState = String(current.player?.state || (current.player?.paused ? "paused" : "playing")).toLowerCase();
  const playerReadyState = Number(current.player?.ready_state ?? 0);
  const duration = Math.max(0, Number(current.player?.duration || 0));
  const priorVideoTime = current.lastAnalysisVideoTime;
  const nearMediaEnd = priorVideoTime !== null && duration > 0 && priorVideoTime >= duration - Math.max(5, duration * 0.02);
  const terminalTimeReset = priorVideoTime !== null
    && videoTime <= 1
    && priorVideoTime - videoTime > Math.max(4, 2.5 * Math.max(0.1, Number(current.player?.playback_rate || 1)))
    && (["ended", "emptied"].includes(playerState)
      || playerReadyState === 0
      || (nearMediaEnd && ["waiting", "stalled", "paused", "playing", "play"].includes(playerState)));
  if (terminalTimeReset) return;
  if (!force && current.lastAnalysisVideoTime !== null && Math.abs(videoTime - current.lastAnalysisVideoTime) < 0.08) return;
  current.analysisBusy = true;
  try {
    const sourceWidth = Math.max(2, current.video.videoWidth || 1280);
    const sourceHeight = Math.max(2, current.video.videoHeight || 720);
    const scale = Math.min(1, ANALYSIS_MAX_WIDTH / sourceWidth);
    const width = Math.max(2, Math.round(sourceWidth * scale));
    const height = Math.max(2, Math.round(sourceHeight * scale));
    if (current.analysisCanvas.width !== width) current.analysisCanvas.width = width;
    if (current.analysisCanvas.height !== height) current.analysisCanvas.height = height;
    current.analysisContext.drawImage(current.video, 0, 0, width, height);
    const blob = await new Promise((resolve) => current.analysisCanvas.toBlob(resolve, "image/jpeg", 0.80));
    if (!blob) throw new Error("The browser could not encode the live analysis frame.");
    const sequence = current.analysisSequence++;
    const upload = uploadBinary("/session/analysis-frame", current.sessionId, {
      sequence,
      video_time: videoTime,
      playback_rate: Math.max(0.1, Number(current.player?.playback_rate || 1)),
      player_state: playerState,
      paused: Boolean(current.player?.paused),
      ready_state: playerReadyState,
      duration,
      url: String(current.player?.url || ""),
      video_id: String(current.player?.video_id || ""),
      source_identity: String(current.player?.source_identity || ""),
      width,
      height,
      monotonic_ms: performance.now(),
      wall_time: Date.now(),
    }, blob);
    current.analysisUpload = upload;
    const result = await upload;
    current.analysisIntervalMs = Math.max(180, Math.min(1000, Number(result?.sample_interval_ms || ANALYSIS_DEFAULT_INTERVAL_MS)));
    current.lastAnalysisVideoTime = videoTime;
  } catch (error) {
    console.error("Course analysis frame upload failed", error);
  } finally {
    current.analysisUpload = null;
    current.analysisBusy = false;
  }
}

function scheduleAnalysis(current) {
  clearTimeout(current.analysisTimer);
  if (!current.running) return;
  current.analysisTimer = setTimeout(async () => {
    await captureAnalysisFrame(current);
    scheduleAnalysis(current);
  }, current.analysisIntervalMs);
}

function resetContinuousSilence(current) {
  current.consecutiveSilentVideoSeconds = 0;
  current.lastSilentVideoTime = null;
}

function updateContinuousSilenceState(current, snapshot) {
  const sampleCount = Number(snapshot.audioSampleCount || 0);
  const audioPeak = Number(snapshot.audioPeak || 0);
  if (sampleCount < MIN_AUDIO_LEVEL_SAMPLES_PER_CHUNK) {
    return { digitalSilence: false, limitReached: false, silentVideoSeconds: 0 };
  }
  if (audioPeak >= DIGITAL_SILENCE_PEAK_THRESHOLD) {
    resetContinuousSilence(current);
    return { digitalSilence: false, limitReached: false, silentVideoSeconds: 0 };
  }
  if (!snapshot.playing) {
    // Pauses and buffering neither consume nor reset the video-time budget.
    return {
      digitalSilence: false,
      limitReached: false,
      silentVideoSeconds: Number(current.consecutiveSilentVideoSeconds || 0),
    };
  }

  const startTime = Math.max(0, Number(snapshot.startTime || 0));
  const endTime = Math.max(startTime, Number(snapshot.endTime || startTime));
  const playbackRate = Math.max(0.1, Number(snapshot.playbackRate || 1));
  const wallSeconds = Math.max(0, Number(snapshot.wallSeconds || 0));
  const videoDelta = Math.max(0, endTime - startTime);
  const expectedVideoDelta = wallSeconds * playbackRate;
  const lastSilentVideoTime = Number(current.lastSilentVideoTime);
  const hasPriorSilentTime = current.lastSilentVideoTime !== null;
  const timelineGap = hasPriorSilentTime
    ? Math.abs(startTime - lastSilentVideoTime)
    : 0;
  const continuousTimeline = videoDelta > 0
    && videoDelta <= Math.max(3, expectedVideoDelta + 3)
    && (!hasPriorSilentTime || timelineGap <= Math.max(3, playbackRate * 3));
  if (!continuousTimeline) resetContinuousSilence(current);

  const addedVideoSeconds = continuousTimeline
    ? Math.min(videoDelta, expectedVideoDelta + 1)
    : 0;
  current.consecutiveSilentVideoSeconds = Math.max(
    0,
    Number(current.consecutiveSilentVideoSeconds || 0) + addedVideoSeconds,
  );
  current.lastSilentVideoTime = endTime;
  return {
    digitalSilence: true,
    limitReached:
      current.consecutiveSilentVideoSeconds >= CONTINUOUS_SILENCE_STOP_VIDEO_SECONDS,
    silentVideoSeconds: current.consecutiveSilentVideoSeconds,
  };
}

async function startRecorder() {
  if (!capture?.running || !capture.stream.active) return;
  const mimeType = recorderMimeType();
  const recorder = new MediaRecorder(capture.stream, mimeType ? { mimeType } : undefined);
  capture.recorder = recorder;
  const sequence = capture.sequence++;
  const startTime = estimatedVideoTime(capture);
  const rate = capture.player.playback_rate || 1;
  const chunkStartedAt = performance.now();
  const blobs = [];
  capture.chunkAudioPeak = 0;
  capture.chunkAudioSampleCount = 0;
  recorder.ondataavailable = (event) => { if (event.data?.size) blobs.push(event.data); };
  recorder.onstop = async () => {
    const current = capture;
    const chunkStoppedAt = performance.now();
    const endTime = current ? estimatedVideoTime(current) : startTime;
    const sessionId = current?.sessionId;
    const playerState = String(current?.player?.state || "").toLowerCase();
    const playing = Boolean(current) && !current.player?.paused
      && !["waiting", "stalled", "seeking", "ended"].includes(playerState);
    const silenceState = current
      ? updateContinuousSilenceState(current, {
          playing,
          startTime,
          endTime,
          playbackRate: rate,
          wallSeconds: Math.max(0, chunkStoppedAt - chunkStartedAt) / 1000,
          audioPeak: Number(current.chunkAudioPeak || 0),
          audioSampleCount: Number(current.chunkAudioSampleCount || 0),
        })
      : { digitalSilence: false, limitReached: false, silentVideoSeconds: 0 };
    const upload = (async () => {
    try {
      if (sessionId && blobs.length) {
        await uploadBinary("/session/chunk", sessionId, {
          sequence, start_time: startTime, end_time: Math.max(startTime, endTime), playback_rate: rate,
          audio_peak: Number(current?.chunkAudioPeak || 0),
          audio_sample_count: Number(current?.chunkAudioSampleCount || 0),
          url: String(current?.player?.url || ""),
          video_id: String(current?.player?.video_id || ""),
          source_identity: String(current?.player?.source_identity || ""),
        }, new Blob(blobs, { type: recorder.mimeType || "video/webm" }));
      }
    } catch (error) {
      console.error("Course chunk upload failed", error);
    }
    })();
    current?.pendingUploads.add(upload);
    try { await upload; } finally { current?.pendingUploads.delete(upload); }
    if (silenceState.limitReached && capture === current) {
      current.running = false;
      clearTimeout(current.analysisTimer);
      chrome.runtime.sendMessage({
        type: "capture-audio-silence",
        payload: {
          sequence,
          current_time: Math.max(startTime, endTime),
          audio_peak: Number(current.chunkAudioPeak || 0),
          consecutive_silent_video_seconds: Number(silenceState.silentVideoSeconds || 0),
          silence_limit_video_seconds: CONTINUOUS_SILENCE_STOP_VIDEO_SECONDS,
          message: "No valid captured tab audio was detected for 15 continuous minutes of source-video time (15 minutes at 1x).",
        },
      }).catch(console.error);
    }
    if (capture === current && current?.running) startRecorder().catch(console.error);
  };
  recorder.start();
  capture.rotateTimer = setTimeout(() => {
    if (recorder.state === "recording") recorder.stop();
  }, 12000);
}

async function startCapture(message) {
  if (capture) await stopCapture();
  const tabStream = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: message.streamId } },
    video: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: message.streamId } },
  });
  const audioTracks = tabStream.getAudioTracks();
  const videoTracks = tabStream.getVideoTracks();
  if (audioTracks.length !== 1) {
    tabStream.getTracks().forEach((track) => track.stop());
    throw new Error(
      "The selected tab did not provide exactly one internal audio track. " +
      "Recording was cancelled; microphone fallback is forbidden."
    );
  }
  if (!videoTracks.length) {
    tabStream.getTracks().forEach((track) => track.stop());
    throw new Error("The selected tab did not provide a video track for keyframe extraction.");
  }
  // Build the recorder stream only from tracks returned by chrome.tabCapture.
  // No microphone device is requested, mixed, or accepted as a fallback.
  const stream = new MediaStream([...videoTracks, audioTracks[0]]);
  const video = document.createElement("video");
  video.srcObject = stream;
  video.muted = true;
  await video.play();
  const audioContext = new AudioContext();
  const source = audioContext.createMediaStreamSource(stream);
  const audioAnalyser = audioContext.createAnalyser();
  const monitorGain = audioContext.createGain();
  audioAnalyser.fftSize = 2048;
  monitorGain.gain.value = 1;
  source.connect(audioAnalyser);
  // chrome.tabCapture removes the tab from Chrome's ordinary audio output.
  // Replay that captured tab track to the default output so the lecturer stays
  // audible during recording. MediaRecorder still consumes the original tab
  // stream, and this monitoring branch never requests or mixes a microphone.
  audioAnalyser.connect(monitorGain);
  monitorGain.connect(audioContext.destination);
  await audioContext.resume();
  capture = {
    running: true, stream, video, audioContext, source, audioAnalyser, monitorGain, sessionId: message.sessionId,
    captureMode: "tab-audio-only-no-microphone",
    audioTrackLabel: audioTracks[0].label || "captured tab audio",
    sequence: 0, player: message.metadata || {},
    rotateTimer: null, recorder: null, pendingUploads: new Set(),
    playerUpdatedAt: performance.now(), analysisSequence: 0,
    analysisCanvas: document.createElement("canvas"), analysisContext: null,
    analysisTimer: null, analysisUpload: null, analysisBusy: false,
    analysisIntervalMs: ANALYSIS_DEFAULT_INTERVAL_MS, lastAnalysisVideoTime: null,
    episodeBoundary: null,
    audioLevelTimer: null, chunkAudioPeak: 0, chunkAudioSampleCount: 0,
    consecutiveSilentVideoSeconds: 0, lastSilentVideoTime: null,
  };
  const audioSamples = new Float32Array(audioAnalyser.fftSize);
  capture.audioLevelTimer = setInterval(() => {
    if (!capture?.running) return;
    audioAnalyser.getFloatTimeDomainData(audioSamples);
    let peak = 0;
    for (const value of audioSamples) peak = Math.max(peak, Math.abs(value));
    capture.chunkAudioPeak = Math.max(capture.chunkAudioPeak, peak);
    capture.chunkAudioSampleCount += 1;
  }, 100);
  capture.analysisContext = capture.analysisCanvas.getContext("2d", { alpha: false });
  if (!capture.analysisContext) throw new Error("The browser could not create the live analysis canvas.");
  await startRecorder();
  await captureAnalysisFrame(capture, true);
  scheduleAnalysis(capture);
  return {
    captureMode: capture.captureMode,
    audioTrackCount: audioTracks.length,
    videoTrackCount: videoTracks.length,
    audioTrackLabel: capture.audioTrackLabel,
    localPlaybackMonitored: true,
  };
}

async function stopCapture() {
  const current = capture;
  if (!current) return;
  clearTimeout(current.analysisTimer);
  clearInterval(current.audioLevelTimer);
  current.running = false;
  await captureAnalysisFrame(current, true);
  if (current.analysisUpload) await current.analysisUpload.catch(() => {});
  clearTimeout(current.rotateTimer);
  if (current.recorder?.state === "recording") {
    const stopped = new Promise((resolve) => current.recorder.addEventListener("stop", resolve, { once: true }));
    current.recorder.stop();
    await stopped;
  }
  await Promise.allSettled([...current.pendingUploads]);
  current.stream.getTracks().forEach((track) => track.stop());
  await current.audioContext.close().catch(() => {});
  capture = null;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.target !== "offscreen") return false;
  (async () => {
    let data = null;
    if (message.type === "start-capture") data = await startCapture(message);
    else if (message.type === "stop-capture") await stopCapture();
    else if (message.type === "player-state" && capture) {
      const payload = message.payload || {};
      const previousPlayer = { ...(capture.player || {}) };
      const priorVideoTime = Math.max(
        Number(capture.lastAnalysisVideoTime || 0),
        Number(capture.player?.current_time || 0),
      );
      const nextVideoTime = Math.max(0, Number(payload.current_time || 0));
      const duration = Math.max(0, Number(payload.duration || capture.player?.duration || 0));
      const playerState = String(payload.state || payload.type || "").toLowerCase();
      const readyState = Number(payload.ready_state ?? capture.player?.ready_state ?? 0);
      const playbackRate = Math.max(0.1, Number(payload.playback_rate || capture.player?.playback_rate || 1));
      const previousIdentity = String(capture.player?.source_identity || "").toLowerCase();
      const nextIdentity = String(payload.source_identity || "").toLowerCase();
      const nativeIdentityChanged = Boolean(
        previousIdentity && nextIdentity && previousIdentity !== nextIdentity
      );
      const nearMediaEnd = duration > 0 && priorVideoTime >= duration - Math.max(5, duration * 0.02);
      const legacyTimeReset = !previousIdentity && !nextIdentity
        && nearMediaEnd
        && nextVideoTime <= 1
        && priorVideoTime - nextVideoTime > Math.max(4, 2.5 * playbackRate)
        && (["ended", "emptied", "waiting", "stalled", "paused", "playing", "play"].includes(playerState)
          || readyState === 0);
      const autoplayBoundary = !capture.episodeBoundary
        && (nativeIdentityChanged || legacyTimeReset);
      capture.player = { ...capture.player, ...payload };
      capture.playerUpdatedAt = performance.now();
      if (autoplayBoundary) {
        capture.episodeBoundary = {
          type: nativeIdentityChanged ? "native_episode_identity_change" : "legacy_terminal_time_reset",
          boundaryVideoTime: priorVideoTime,
          nextEpisodeVideoTime: nextVideoTime,
          sourceIdentity: previousIdentity,
          nextSourceIdentity: nextIdentity,
        };
        capture.player = {
          ...previousPlayer,
          current_time: priorVideoTime,
          paused: true,
          state: "episode-boundary",
        };
        capture.running = false;
        clearTimeout(capture.analysisTimer);
        clearTimeout(capture.rotateTimer);
      }
    }
    sendResponse({ ok: true, data });
  })().catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
  return true;
});
