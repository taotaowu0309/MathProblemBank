const course = document.getElementById("course");
const status = document.getElementById("status");
const start = document.getElementById("start");
const stop = document.getElementById("stop");
const reload = document.getElementById("reload");

function request(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (response) => {
      if (chrome.runtime.lastError) reject(chrome.runtime.lastError);
      else if (!response?.ok) reject(new Error(response?.error || "Unknown recorder error"));
      else resolve(response);
    });
  });
}

async function refresh() {
  try {
    const response = await request({ type: "popup-status" });
    const selected = response.receiver.course;
    const extensionProtocol = Number(response.extensionProtocolVersion || 0);
    const receiverProtocol = Number(response.receiver.required_recorder_protocol_version || 0);
    const protocolMismatch = !extensionProtocol || extensionProtocol !== receiverProtocol;
    reload.hidden = !protocolMismatch;
    if (protocolMismatch) {
      course.textContent = "Recorder update required";
      status.textContent =
        `Extension protocol ${extensionProtocol || "unknown"}; control center protocol ${receiverProtocol || "unknown"}.\n` +
        "Reload the recorder here, then open it again. Your course data is not changed.";
      start.hidden = true;
      stop.hidden = true;
      return;
    }
    course.textContent = selected ? `${selected.course_code}  ${selected.course_title}` : "No course is prepared";
    const awake =
      `Recorder ${response.extensionVersion || ""} background woke successfully. ` +
      "Chrome may show Service Worker (Inactive) again while idle; that is normal.";
    if (response.active) {
      status.textContent = awake + "\n\nRecording is active. The lecture is replayed to your Windows output, while only this browser tab is recorded; microphone is never used.";
      start.hidden = true; stop.hidden = false;
    } else {
      status.textContent = awake + "\n\n" + (selected
        ? "Open the desired YouTube or Bilibili episode, then start recording. Keep Windows output audible to listen; only tab audio is captured, and microphone audio is excluded."
        : "Open Math Problem Bank → Web Course Lectures and prepare a course first.");
      start.hidden = false; stop.hidden = true; start.disabled = !selected;
    }
  } catch (error) {
    course.textContent = "Local receiver is unavailable";
    status.textContent =
      "The extension background woke successfully; only the local control-center receiver is unavailable.\n" +
      "Open Math Problem Bank and visit the Web Course Lectures page.\n" + error.message;
    start.disabled = true;
  }
}

start.addEventListener("click", async () => {
  start.disabled = true; status.textContent = "Starting capture…";
  try { await request({ type: "popup-start" }); await refresh(); }
  catch (error) { status.textContent = error.message; start.disabled = false; }
});

stop.addEventListener("click", async () => {
  stop.disabled = true; status.textContent = "Saving the final chunk…";
  try { await request({ type: "popup-stop" }); await refresh(); }
  catch (error) { status.textContent = error.message; stop.disabled = false; }
});

reload.addEventListener("click", () => {
  reload.disabled = true;
  status.textContent = "Reloading the recorder… Open the popup again when it closes.";
  chrome.runtime.reload();
});

refresh();
