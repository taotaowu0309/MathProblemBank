const RECEIVER = "http://127.0.0.1:8765";
const ACTIVE_KEY = "activeRecording";
const RECOVERY_KEY = "activeRecordingRecovery";
const RECORDER_PROTOCOL_VERSION = 8;
const ANALYSIS_STREAM_VERSION = 1;
let stopCaptureInFlight = null;
let startCaptureInFlight = null;
let reconciliationInFlight = null;
const OFFSCREEN_RETRY_DELAYS_MS = [0, 100, 250, 500, 1000];

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function sendOffscreen(message) {
  let lastError = null;
  for (const wait of OFFSCREEN_RETRY_DELAYS_MS) {
    if (wait) await delay(wait);
    try {
      const response = await chrome.runtime.sendMessage({ target: "offscreen", ...message });
      if (!response) throw new Error("The recorder offscreen page did not respond.");
      return response;
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error("The recorder offscreen page did not respond.");
}

async function getTabCaptureStreamId(tabId) {
  let lastError = null;
  for (const wait of OFFSCREEN_RETRY_DELAYS_MS) {
    if (wait) await delay(wait);
    try {
      return await chrome.tabCapture.getMediaStreamId({ targetTabId: tabId });
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error("Chrome did not provide a tab capture stream.");
}

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
  if (value) {
    await chrome.storage.session.set({ [ACTIVE_KEY]: value });
    // session storage is intentionally cleared by Chrome when an extension is
    // reloaded. Keep a private recovery copy so the receiver can be finalized
    // cleanly after a reload or browser restart; it is never used as a live
    // capture indicator.
    await chrome.storage.local.set({ [RECOVERY_KEY]: value });
  } else {
    await chrome.storage.session.remove(ACTIVE_KEY);
    await chrome.storage.local.remove(RECOVERY_KEY);
  }
}

async function persistedActiveRecording() {
  return (await chrome.storage.local.get(RECOVERY_KEY))[RECOVERY_KEY] || null;
}

async function hasOffscreenContext() {
  try {
    const contexts = await chrome.runtime.getContexts({
      contextTypes: ["OFFSCREEN_DOCUMENT"],
      documentUrls: [chrome.runtime.getURL("offscreen.html")],
    });
    return contexts.length > 0;
  } catch (_error) {
    return false;
  }
}

async function reconcileInterruptedSession(reason = "startup") {
  if (reconciliationInFlight) return reconciliationInFlight;
  reconciliationInFlight = (async () => {
    const persisted = await persistedActiveRecording();
    if (!persisted?.session_id) return null;

    // A normal MV3 service-worker wake leaves the offscreen document alive.
    // Only treat the session as interrupted after the offscreen context is
    // definitely gone, which is what happens when Chrome reloads/updates the
    // unpacked extension or restarts the browser.
    for (const wait of [0, 100, 300, 800]) {
      if (wait) await delay(wait);
      if (await hasOffscreenContext()) return null;
    }

    let status;
    try {
      status = await receiverJson("/status");
    } catch (_error) {
      // Keep the recovery record. A later popup or startup event can retry
      // once the local receiver is available again.
      return null;
    }
    const remote = await remoteActiveRecording(status);
    if (!remote || String(remote.session_id || "") !== String(persisted.session_id)) {
      if (!remote) await setActive(null);
      return null;
    }

    const metadata = {
      ...(persisted.metadata || {}),
      title: remote.title || persisted.metadata?.title || "",
      source_identity: remote.source_identity || persisted.metadata?.source_identity || "",
      current_time: Number(remote.last_video_time || persisted.metadata?.current_time || 0),
      playback_rate: Number(remote.playback_rate || persisted.metadata?.playback_rate || 1),
      extension_lifecycle_interrupted: true,
      recovery_reason: String(reason || "startup"),
    };
    try {
      await receiverJson("/session/stop", {
        method: "POST",
        body: JSON.stringify({ session_id: remote.session_id, ...metadata }),
      });
      await setActive(null);
      return { session_id: remote.session_id, interrupted: true };
    } catch (_error) {
      // Do not clear the recovery record until the receiver confirms the stop.
      return null;
    }
  })().finally(() => {
    reconciliationInFlight = null;
  });
  return reconciliationInFlight;
}

async function sendTabMessage(tabId, message) {
  try {
    return await chrome.tabs.sendMessage(tabId, message);
  } catch (firstError) {
    try {
      await chrome.scripting.executeScript({ target: { tabId }, files: ["content_script.js"] });
      return await chrome.tabs.sendMessage(tabId, message);
    } catch (_secondError) {
      throw firstError;
    }
  }
}

async function updateBadge(tabId, { text, color, title } = {}) {
  try {
    if (color) await chrome.action.setBadgeBackgroundColor({ color });
    if (text !== undefined) await chrome.action.setBadgeText({ text, tabId });
    if (title) await chrome.action.setTitle({ title, tabId });
  } catch (error) {
    // Closing a tab while a capture is finishing is harmless. Do not turn
    // badge cleanup into a failed recording operation.
    console.warn("Could not update the recorder badge", error);
  }
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
  for (const wait of [0, 50, 100, 200, 400]) {
    if (wait) await delay(wait);
    const ready = await chrome.runtime.getContexts({
      contextTypes: ["OFFSCREEN_DOCUMENT"],
      documentUrls: [url],
    });
    if (ready.length) return;
  }
  throw new Error("The recorder background page did not finish starting.");
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
  return sendTabMessage(tabId, { type: "collect-metadata" });
}

async function recordingMetadata(tabId) {
  let latest = null;
  let lastError = null;
  for (const wait of [0, 150, 300, 600, 1000, 1500]) {
    if (wait) await delay(wait);
    try {
      latest = await pageMetadata(tabId);
      if (
        latest
        && String(latest.source_identity || "").trim()
        && String(latest.video_id || "").trim()
        && Number(latest.ready_state || 0) >= 2
      ) {
        return latest;
      }
    } catch (error) {
      lastError = error;
    }
  }
  if (lastError && !latest) throw lastError;
  throw new Error(
    "The course video is still loading. Wait until the player picture appears, then start recording again."
  );
}

async function remoteActiveRecording(status = null) {
  const value = status || await receiverJson("/status");
  return value?.active_session || (value?.active_sessions || [])[0] || null;
}

async function pauseSourceVideo(tabId, reason) {
  let lastError = null;
  for (const wait of [0, 50, 150, 300, 600, 1000]) {
    if (wait) await delay(wait);
    try {
      const response = await chrome.tabs.sendMessage(tabId, {
        type: "pause-source-video",
        reason: String(reason || ""),
        hold: true,
      });
      if (response?.ok && response.paused === true) return response;
      throw new Error(response?.reason || "The course video did not report a paused state.");
    } catch (error) {
      lastError = error;
      // A full Bilibili navigation can replace the content-script context
      // between the route change and this request. Reinstall it before the
      // next attempt; the content script keeps the pause lock alive locally.
      try {
        await chrome.scripting.executeScript({ target: { tabId }, files: ["content_script.js"] });
      } catch (_injectError) {}
    }
  }
  console.warn("Could not pause the source video at the automatic episode boundary", lastError);
  return { ok: false, paused: false, error: String(lastError?.message || lastError || "unknown_error") };
}

async function performStartCapture() {
  await reconcileInterruptedSession("popup_start");
  if (await activeRecording()) throw new Error("A web-course recording is already active.");
  const status = await receiverJson("/status");
  if (!status.armed) throw new Error("Select a course in Math Problem Bank first; the recorder target syncs automatically.");
  const remoteActive = await remoteActiveRecording(status);
  if (remoteActive) {
    throw new Error(
      `A previous recording session is still open (episode ${remoteActive.episode_number || "unknown"}). ` +
      "Stop or recover it before starting another episode."
    );
  }
  if (Number(status.required_recorder_protocol_version || 0) !== RECORDER_PROTOCOL_VERSION) {
    throw new Error(
      `Recorder protocol mismatch. Reload this extension (extension ${RECORDER_PROTOCOL_VERSION}, receiver ${status.required_recorder_protocol_version || "unknown"}).`
    );
  }
  const tab = await activeTab();
  const metadata = {
    ...(await recordingMetadata(tab.id)),
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
    const streamId = await getTabCaptureStreamId(tab.id);
    const response = await sendOffscreen({
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
    try { await sendOffscreen({ type: "stop-capture" }); } catch (_stopError) {}
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
  await updateBadge(tab.id, { color: "#20a66a", text: "REC" });
  try {
    await sendTabMessage(tab.id, { type: "recording-state", active: true });
  } catch (error) {
    // Capture is already running. A content-script navigation race must not
    // make the popup report a failed start or lose the receiver session.
    console.warn("Could not mark the course tab as recording", error);
  }
  return active;
}

async function startCapture() {
  if (startCaptureInFlight) return startCaptureInFlight;
  startCaptureInFlight = performStartCapture();
  try {
    return await startCaptureInFlight;
  } finally {
    startCaptureInFlight = null;
  }
}

async function performStopCapture(options = {}) {
  let active = await activeRecording();
  let recoveredRemoteSession = false;
  if (!active) {
    const remote = await remoteActiveRecording();
    if (!remote) return null;
    const tab = await activeTab();
    active = { ...remote, tabId: tab.id, metadata: {} };
    recoveredRemoteSession = true;
  }
  let metadata = options.metadata ? { ...options.metadata } : (active.metadata || {});
  if (!options.metadata && recoveredRemoteSession) {
    metadata = {
      title: active.title || "",
      source_identity: active.source_identity || "",
      current_time: Number(active.last_video_time || active.started_video_time || 0),
      playback_rate: Number(active.playback_rate || 1),
      recovered_remote_session: true,
    };
  } else if (!options.metadata) {
    try { metadata = await pageMetadata(active.tabId); } catch (_error) {}
  }
  let offscreenError = null;
  let receiverError = null;
  try {
    await sendOffscreen({ type: "stop-capture" });
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
  }
  if (!receiverError) {
    await updateBadge(active.tabId, { text: "" });
    try { await sendTabMessage(active.tabId, { type: "recording-state", active: false }); } catch (_error) {}
    await setActive(null);
  }
  if (receiverError) throw receiverError;
  if (offscreenError) console.warn("Offscreen recorder cleanup failed", offscreenError);
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

function scheduleReconciliation(reason) {
  void reconcileInterruptedSession(reason).catch((error) => {
    console.warn("Could not reconcile an interrupted recorder session", error);
  });
}

chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason === "update") scheduleReconciliation("extension_update");
});

chrome.runtime.onStartup.addListener(() => {
  scheduleReconciliation("browser_startup");
});

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
      await updateBadge(active.tabId, { color: "#c43d3d", text: "ERR", title: errorMessage });
      try {
        await sendTabMessage(active.tabId, { type: "recording-error", message: errorMessage });
      } catch (_error) {}
      sendResponse({ ok: true, stopped, audioSilence: true });
      return;
    }
    if (message?.type === "popup-status") {
      const recovered = await reconcileInterruptedSession("popup_status");
      const local = await receiverJson("/status");
      sendResponse({
        ok: true,
        receiver: local,
        active: (await activeRecording()) || await remoteActiveRecording(local),
        interrupted: Boolean(recovered?.interrupted),
        extensionProtocolVersion: RECORDER_PROTOCOL_VERSION,
        extensionVersion: chrome.runtime.getManifest().version,
      });
      return;
    }
    if (message?.type === "content-script-ready") {
      await reconcileInterruptedSession("content_script_ready");
      const active = await activeRecording();
      if (active && sender.tab?.id === active.tabId) {
        const payload = message.payload || {};
        const previousIdentity = String(
          active.source_identity || active.metadata?.source_identity || ""
        ).trim().toLowerCase();
        const observedIdentity = String(payload.source_identity || "").trim().toLowerCase();
        const sourceChanged = Boolean(
          previousIdentity && observedIdentity && previousIdentity !== observedIdentity
        );
        if (sourceChanged) {
          // A full navigation can replace the old content-script context
          // before it emits player-event. Treat the new document itself as
          // the boundary, pause it with the same hold used by the SPA path,
          // and finalize the old episode immediately.
          await pauseSourceVideo(active.tabId, "content_script_ready_source_changed");
          const saved = await receiverJson("/session/event", {
            method: "POST",
            body: JSON.stringify({
              session_id: active.session_id,
              ...payload,
              type: "episode-boundary",
              state: "episode-boundary",
              wall_time: Date.now(),
              paused: true,
            }),
          });
          try {
            await sendOffscreen({
              type: "player-state",
              payload: {
                ...payload,
                type: "episode-boundary",
                state: "episode-boundary",
                paused: true,
              },
            });
          } catch (error) {
            // The receiver boundary is authoritative. A lost offscreen
            // context must not prevent the old episode from being finalized.
            console.warn("Could not forward the navigation boundary to the offscreen recorder", error);
          }
          const instruction = saved.data || {};
          if (instruction.auto_stop_recording) {
            await pauseSourceVideo(active.tabId, "content_script_ready_auto_episode_boundary");
            const stopMetadata = {
              ...(active.metadata || {}),
              current_time: Number(instruction.boundary_video_time ?? active.metadata?.current_time ?? 0),
              playback_rate: Number(payload.playback_rate || active.metadata?.playback_rate || 1),
              source_identity: String(instruction.source_identity || active.source_identity || active.metadata?.source_identity || ""),
              auto_episode_boundary: true,
              boundary_type: String(instruction.boundary_type || "autoplay_next_episode"),
              next_source_identity: String(instruction.next_source_identity || observedIdentity),
            };
            const stopOptions = { metadata: stopMetadata };
            const stopped = await stopCapture(stopOptions);
            sendResponse({ ok: true, active: false, autoStopped: true, stopped, boundary: instruction });
            return;
          }
        }
        try { await sendTabMessage(active.tabId, { type: "recording-state", active: true }); } catch (_error) {}
      }
      sendResponse({ ok: true, active: Boolean(active) });
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
        // Pause before the receiver round-trip.  On autoplay pages the next
        // episode can begin between the DOM event and the HTTP response; the
        // old ordering therefore sometimes paused too late (or paused the
        // newly-started episode instead of freezing the boundary).
        const previousIdentity = String(
          active.source_identity || active.metadata?.source_identity || ""
        ).trim().toLowerCase();
        const observedIdentity = String(
          message.payload?.source_identity || ""
        ).trim().toLowerCase();
        const sourceChanged = Boolean(
          previousIdentity && observedIdentity && previousIdentity !== observedIdentity
        );
        if (sourceChanged) {
          await pauseSourceVideo(active.tabId, "source_identity_changed");
        }
        const saved = await receiverJson("/session/event", {
          method: "POST",
          body: JSON.stringify({ session_id: active.session_id, ...message.payload }),
        });
        try {
          await sendOffscreen({ type: "player-state", payload: message.payload });
        } catch (error) {
          // Player telemetry is useful but must not prevent the receiver from
          // finalizing an automatic episode boundary.
          console.warn("Could not forward player state to the offscreen recorder", error);
        }
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
    try {
      await stopCapture();
    } catch (error) {
      // Keep the local handle when the receiver could not be finalized.  The
      // popup can retry the same session instead of silently orphaning it.
      console.warn("Could not finalize the recording after its tab closed", error);
    }
  }
});
