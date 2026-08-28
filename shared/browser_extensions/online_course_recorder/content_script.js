(() => {
  if (window.__mathProblemBankCourseRecorderInstalled) return;
  window.__mathProblemBankCourseRecorderInstalled = true;

  let lastCaption = "";
  let lastTimeEventAt = 0;
  let attachedVideo = null;
  let recordingIndicator = null;

  function videoElement() {
    const videos = [...document.querySelectorAll("video")];
    return videos.sort((a, b) => (b.clientWidth * b.clientHeight) - (a.clientWidth * a.clientHeight))[0] || null;
  }

  function videoId() {
    const url = new URL(location.href);
    if (url.hostname.includes("youtube")) return url.searchParams.get("v") || "";
    const match = url.pathname.match(/\/(BV[a-zA-Z0-9]+|av\d+)/i);
    return match ? match[1] : "";
  }

  function sourceIdentity() {
    const url = new URL(location.href);
    const nativeId = videoId();
    if (url.hostname.includes("bilibili")) {
      const parsedPart = Number.parseInt(url.searchParams.get("p") || "1", 10);
      const part = Number.isFinite(parsedPart) && parsedPart > 0 ? parsedPart : 1;
      return nativeId ? `bilibili:${nativeId.toLowerCase()}:p=${part}` : "";
    }
    if (url.hostname.includes("youtube")) {
      return nativeId ? `youtube:${nativeId.toLowerCase()}` : "";
    }
    return nativeId ? `video:${nativeId.toLowerCase()}` : "";
  }

  function metadata() {
    const video = videoElement();
    return {
      url: location.href,
      title: document.title.replace(/\s*[-_]\s*(YouTube|哔哩哔哩.*)$/i, "").trim(),
      video_id: videoId(),
      source_identity: sourceIdentity(),
      duration: Number.isFinite(video?.duration) ? video.duration : 0,
      current_time: video?.currentTime || 0,
      playback_rate: video?.playbackRate || 1,
      paused: video ? video.paused : true,
      muted: video ? video.muted : false,
      volume: video ? video.volume : 1,
      ready_state: video?.readyState || 0,
    };
  }

  function post(type, payload) {
    chrome.runtime.sendMessage({ type, payload }).catch(() => {});
  }

  function pauseSourceVideo(reason = "") {
    const video = videoElement();
    if (!video) {
      return { ok: false, paused: false, reason: "video_not_found" };
    }
    const alreadyPaused = video.paused;
    video.pause();
    return {
      ok: true,
      paused: video.paused,
      alreadyPaused,
      reason: String(reason || ""),
      current_time: video.currentTime || 0,
      source_identity: sourceIdentity(),
    };
  }

  function emitState(type) {
    const data = metadata();
    post("player-event", {
      type,
      state: type,
      url: data.url,
      title: data.title,
      video_id: data.video_id,
      source_identity: data.source_identity,
      duration: data.duration,
      current_time: data.current_time,
      playback_rate: data.playback_rate,
      paused: data.paused,
      muted: data.muted,
      volume: data.volume,
      ready_state: data.ready_state,
      wall_time: Date.now(),
    });
  }

  function attachVideo() {
    const video = videoElement();
    if (!video || video === attachedVideo) return;
    attachedVideo = video;
    for (const event of ["play", "pause", "playing", "waiting", "stalled", "seeking", "seeked", "ratechange", "volumechange", "ended", "emptied"]) {
      video.addEventListener(event, () => emitState(event), { passive: true });
    }
    video.addEventListener("timeupdate", () => {
      const now = Date.now();
      if (now - lastTimeEventAt >= 1000) {
        lastTimeEventAt = now;
        emitState(video.paused ? "paused" : "playing");
      }
    }, { passive: true });
  }

  function visibleText(selector) {
    return [...document.querySelectorAll(selector)]
      .filter((node) => node instanceof HTMLElement && node.offsetParent !== null)
      .map((node) => (node.innerText || node.textContent || "").trim())
      .filter(Boolean)
      .join(" ");
  }

  function scanCaption() {
    attachVideo();
    const selectors = [
      ".ytp-caption-segment",
      ".bpx-player-subtitle-panel-text",
      ".bpx-player-subtitle-panel-text-wrap",
      ".subtitle-item-text",
      "[class*='subtitle-item']",
    ];
    let text = "";
    for (const selector of selectors) {
      text = visibleText(selector).replace(/\s+/g, " ").trim();
      if (text) break;
    }
    if (!text || text === lastCaption || text.length > 1000) return;
    lastCaption = text;
    const data = metadata();
    const current = data.current_time;
    post("caption", {
      text,
      start_time: current,
      end_time: current + 3,
      url: data.url,
      video_id: data.video_id,
      source_identity: data.source_identity,
    });
  }

  function showRecordingIndicator(active) {
    if (active) {
      if (recordingIndicator) return;
      recordingIndicator = document.createElement("div");
      recordingIndicator.textContent = "● Math Problem Bank recording";
      Object.assign(recordingIndicator.style, {
        position: "fixed", right: "16px", top: "16px", zIndex: "2147483647",
        background: "rgba(20,32,48,.92)", color: "#7ef2b5", padding: "8px 12px",
        borderRadius: "9px", font: "600 12px/1.3 system-ui", boxShadow: "0 4px 18px rgba(0,0,0,.28)",
      });
      document.documentElement.appendChild(recordingIndicator);
    } else if (recordingIndicator) {
      recordingIndicator.remove();
      recordingIndicator = null;
    }
  }

  function showRecordingError(message) {
    showRecordingIndicator(false);
    const warning = document.createElement("div");
    warning.textContent = message;
    Object.assign(warning.style, {
      position: "fixed", right: "16px", top: "16px", zIndex: "2147483647",
      maxWidth: "440px", background: "rgba(130,20,20,.96)", color: "white",
      padding: "12px 15px", borderRadius: "9px", font: "600 13px/1.45 system-ui",
      boxShadow: "0 4px 18px rgba(0,0,0,.35)",
    });
    document.documentElement.appendChild(warning);
    setTimeout(() => warning.remove(), 20000);
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "collect-metadata") {
      sendResponse(metadata());
      return false;
    }
    if (message?.type === "pause-source-video") {
      sendResponse(pauseSourceVideo(message.reason));
      return false;
    }
    if (message?.type === "recording-state") {
      showRecordingIndicator(Boolean(message.active));
      sendResponse({ ok: true });
      return false;
    }
    if (message?.type === "recording-error") {
      showRecordingError(String(message.message || "The recording stopped because tab audio was silent."));
      sendResponse({ ok: true });
      return false;
    }
    return false;
  });

  attachVideo();
  document.addEventListener("yt-navigate-finish", () => {
    attachVideo();
    if (recordingIndicator) emitState("youtube-navigation");
  }, { passive: true });
  setInterval(scanCaption, 650);
  new MutationObserver(attachVideo).observe(document.documentElement, { childList: true, subtree: true });
})();
