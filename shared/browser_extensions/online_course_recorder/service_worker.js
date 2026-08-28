const RECEIVER = "http://127.0.0.1:8765";
const ACTIVE_KEY = "activeRecording";
const RECORDER_PROTOCOL_VERSION = 8;
const ANALYSIS_STREAM_VERSION = 1;
let stopCaptureInFlight = null;

async function receiverJson(path, options = {}) {
  const response = await fetch(RECEIVER + path, {
    cache: "no-store",
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const payload = await response.json();
  if (!response.ok || !payload.ok) throw new Error(payload.error || `Receiver HTTP ${response.status}`);
  return payload;
}

async function activeRecording() {
  return (await chrome.storage.session.get(ACTIVE_KEY))[ACTIVE_KEY] || null;
}

async function setActive(value) {
  if (value) await chrome.storage.session.set({ [ACTIVE_KEY]: value });
  else await chrome.storage.session.remove(ACTIVE_KEY);
}

async function ensureOffscreen() {
  const url = chrome.runtime.getURL("offscreen.html");
  const contexts = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
    documentUrls: [url],
  });
  if (!contexts.length) {
    await chrome.offscreen.createDocument({
      url: "offscreen.html",
      reasons: ["USER_MEDIA"],
      justification: "Capture the user-selected course tab and save recoverable local chunks.",
    });
  }
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) throw new Error("No active browser tab was found.");
  if (!/^https:\/\/(?:www\.)?(?:youtube\.com|bilibili\.com)\//i.test(tab.url || "")) {
    throw new Error("Open a YouTube or Bilibili video page first.");
  }
  return tab;
}

async function pageMetadata(tabId) {
  try {
    return await chrome.tabs.sendMessage(tabId, { type: "collect-metadata" });
  } catch (_error) {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content_script.js"] });
    return chrome.tabs.sendMessage(tabId, { type: "collect-metadata" });
  }
}

async function pauseSourceVideo(tabId, reason) {
  try {
    const response = await chrome.tabs.sendMessage(tabId, {
      type: "pause-source-video",
      reason: String(reason || ""),
    });
    if (!response?.ok || response.paused !== true) {
      throw new Error(response?.reason || "The course video did not report a paused state.");
    }
    return response;
  } catch (error) {
    console.error("Could not pause the source video at the automatic episode boundary", error);
    return { ok: false, paused: false, error: String(error?.message || error) };
  }
}

async function startCapture() {
  if (await activeRecording()) throw new Error("A web-course recording is already active.");
  const status = await receiverJson("/status");
  if (!status.armed) throw new Error("Select a course in Math Problem Bank first; the recorder target syncs automatically.");
  if (Number(status.required_recorder_protocol_version || 0) !== RECORDER_PROTOCOL_VERSION) {
    throw new Error(
      `Recorder protocol mismatch. Reload this extension (extension ${RECORDER_PROTOCOL_VERSION}, receiver ${status.required_recorder_protocol_version || "unknown"}).`
    );
  }
  const tab = await activeTab();
  const metadata = {
    ...(await pageMetadata(tab.id)),
    recorder_protocol_version: RECORDER_PROTOCOL_VERSION,
    analysis_stream_version: ANALYSIS_STREAM_VERSION,
  };
  if (metadata.muted || Number(metadata.volume ?? 1) <= 0) {
    throw new Error(
      "The web course player itself is muted or its volume is 0. Unmute the player before recording; " +
      "keep Windows output audible if you want to hear the lecture. No microphone is used."
    );
  }
  const started = await receiverJson("/session/start", { method: "POST", body: JSON.stringify(metadata) });
  const session = started.data;
  let captureResult = null;
  try {
    await ensureOffscreen();
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
    const response = await chrome.runtime.sendMessage({
      target: "offscreen",
      type: "start-capture",
      streamId,
      sessionId: session.session_id,
      metadata,
    });
    if (!response?.ok) throw new Error(response?.error || "Tab capture did not start.");
    captureResult = response.data || {};
    if (
      captureResult.captureMode !== "tab-audio-only-no-microphone"
      || captureResult.audioTrackCount !== 1
      || captureResult.localPlaybackMonitored !== true
    ) {
      throw new Error(
        "Internal tab-audio or local-playback validation failed; microphone fallback is forbidden."
      );
    }
  } catch (error) {
    try { await chrome.runtime.sendMessage({ target: "offscreen", type: "stop-capture" }); } catch (_stopError) {}
    try {
      await receiverJson("/session/stop", {
        method: "POST",
        body: JSON.stringify({ session_id: session.session_id, ...metadata, capture_start_failed: true }),
      });
    } catch (_receiverError) {}
    throw error;
  }
  const active = {
    ...session,
    tabId: tab.id,
    startedAt: Date.now(),
    metadata,
    captureMode: captureResult.captureMode,
    audioTrackLabel: captureResult.audioTrackLabel || "captured tab audio",
  };
  await setActive(active);
  await chrome.action.setBadgeBackgroundColor({ color: "#20a66a" });
  await chrome.action.setBadgeText({ text: "REC", tabId: tab.id });
  await chrome.tabs.sendMessage(tab.id, { type: "recording-state", active: true });
  return active;
}

async function performStopCapture(options = {}) {
  const active = await activeRecording();
  if (!active) return null;
  let metadata = options.metadata ? { ...options.metadata } : (active.metadata || {});
  if (!options.metadata) {
    try { metadata = await pageMetadata(active.tabId); } catch (_error) {}
  }
  let offscreenError = null;
  let receiverError = null;
  try {
    await chrome.runtime.sendMessage({ target: "offscreen", type: "stop-capture" });
  } catch (error) {
    offscreenError = error;
  }
  try {
    await receiverJson("/session/stop", {
      method: "POST",
      body: JSON.stringify({ session_id: active.session_id, ...metadata }),
    });
  } catch (error) {
    receiverError = error;
  } finally {
    await chrome.action.setBadgeText({ text: "", tabId: active.tabId });
    try { await chrome.tabs.sendMessage(active.tabId, { type: "recording-state", active: false }); } catch (_error) {}
    await setActive(null);
  }
  if (receiverError) throw receiverError;
  if (offscreenError) console.error("Offscreen recorder cleanup failed", offscreenError);
  return { ...active, stoppedAt: Date.now(), metadata };
}

async function stopCapture(options = {}) {
  if (stopCaptureInFlight) return stopCaptureInFlight;
  stopCaptureInFlight = performStopCapture(options);
  try {
    return await stopCaptureInFlight;
  } finally {
    stopCaptureInFlight = null;
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.target === "offscreen") return false;
  (async () => {
    if (message?.type === "capture-audio-silence") {
      const active = await activeRecording();
      if (!active) {
        sendResponse({ ok: true, ignored: true });
        return;
      }
      const detail = message.payload || {};
      const errorMessage =
        "Recording stopped after 15 continuous minutes of source-video time (15 minutes at 1x) without valid captured tab audio. " +
        "Short or intermittent silent intervals are allowed and reset as soon as valid audio returns. " +
        "Check that the web player is unmuted and its own volume is above 0 before retrying. " +
        "Keep Windows output audible if you want to hear the lecture; do not enable a microphone.";
      const stopped = await stopCapture({
        metadata: {
          ...(active.metadata || {}),
          current_time: Number(detail.current_time || active.metadata?.current_time || 0),
          audio_capture_failed: true,
          audio_capture_error: errorMessage,
          audio_peak: Number(detail.audio_peak || 0),
          consecutive_silent_video_seconds: Number(
            detail.consecutive_silent_video_seconds || 0
          ),
          silence_limit_video_seconds: Number(detail.silence_limit_video_seconds || 15 * 60),
        },
      });
      await chrome.action.setBadgeBackgroundColor({ color: "#c43d3d" });
      await chrome.action.setBadgeText({ text: "ERR", tabId: active.tabId });
      await chrome.action.setTitle({ title: errorMessage, tabId: active.tabId });
      try {
        await chrome.tabs.sendMessage(active.tabId, { type: "recording-error", message: errorMessage });
      } catch (_error) {}
      sendResponse({ ok: true, stopped, audioSilence: true });
      return;
    }
    if (message?.type === "popup-status") {
      const local = await receiverJson("/status");
      sendResponse({
        ok: true,
        receiver: local,
        active: await activeRecording(),
        extensionProtocolVersion: RECORDER_PROTOCOL_VERSION,
        extensionVersion: chrome.runtime.getManifest().version,
      });
      return;
    }
    if (message?.type === "popup-start") {
      sendResponse({ ok: true, active: await startCapture() });
      return;
    }
    if (message?.type === "popup-stop") {
      sendResponse({ ok: true, stopped: await stopCapture() });
      return;
    }
    if (message?.type === "player-event" || message?.type === "caption") {
      const active = await activeRecording();
      if (!active || sender.tab?.id !== active.tabId) {
        sendResponse({ ok: true, ignored: true });
        return;
      }
      if (message.type === "player-event") {
        const saved = await receiverJson("/session/event", {
          method: "POST",
          body: JSON.stringify({ session_id: active.session_id, ...message.payload }),
        });
        await chrome.runtime.sendMessage({ target: "offscreen", type: "player-state", payload: message.payload });
        const instruction = saved.data || {};
        if (instruction.auto_stop_recording) {
          await pauseSourceVideo(active.tabId, "auto_episode_boundary");
          const stopMetadata = {
            ...(active.metadata || {}),
            current_time: Number(instruction.boundary_video_time ?? active.metadata?.current_time ?? 0),
            playback_rate: Number(message.payload?.playback_rate || active.metadata?.playback_rate || 1),
            source_identity: String(instruction.source_identity || active.source_identity || active.metadata?.source_identity || ""),
            auto_episode_boundary: true,
            boundary_type: String(instruction.boundary_type || "autoplay_next_episode"),
            next_source_identity: String(instruction.next_source_identity || ""),
          };
          const stopped = await stopCapture({ metadata: stopMetadata });
          sendResponse({ ok: true, autoStopped: true, stopped, boundary: instruction });
          return;
        }
      } else {
        await receiverJson("/session/caption", {
          method: "POST",
          body: JSON.stringify({ session_id: active.session_id, ...message.payload }),
        });
      }
      sendResponse({ ok: true });
      return;
    }
    sendResponse({ ok: false, error: "Unknown message." });
  })().catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
  return true;
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const active = await activeRecording();
  if (active?.tabId === tabId) {
    try { await stopCapture(); } catch (_error) { await setActive(null); }
  }
});
