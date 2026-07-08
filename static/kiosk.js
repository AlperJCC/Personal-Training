(() => {
  "use strict";

  const RESET_DELAY_MS = 3000;
  const screen = document.getElementById("screen");
  const input = document.getElementById("scan-input");
  const manualToggle = document.getElementById("manual-entry-toggle");
  const manualForm = document.getElementById("manual-entry-form");
  const manualInput = document.getElementById("manual-entry-input");

  let buffer = "";
  let busy = false; // true while a scan is in flight or a result is showing
  let resetTimer = null;
  let manualEntryOpen = false;

  // --- Sound cues -------------------------------------------------------
  // Synthesized tones (no binary asset files to manage/deploy) — clearly
  // distinguishable success chime / soft double-beep / error buzz.
  let audioCtx = null;
  function getAudioCtx() {
    if (!audioCtx) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      audioCtx = new Ctx();
    }
    if (audioCtx.state === "suspended") audioCtx.resume();
    return audioCtx;
  }

  function tone(freq, startTime, duration, { gain = 0.2, type = "sine" } = {}) {
    const ctx = getAudioCtx();
    const osc = ctx.createOscillator();
    const g = ctx.createGain();
    osc.type = type;
    osc.frequency.value = freq;
    g.gain.setValueAtTime(0, startTime);
    g.gain.linearRampToValueAtTime(gain, startTime + 0.01);
    g.gain.linearRampToValueAtTime(0, startTime + duration);
    osc.connect(g).connect(ctx.destination);
    osc.start(startTime);
    osc.stop(startTime + duration + 0.02);
  }

  function playSuccessChime() {
    const ctx = getAudioCtx();
    const t = ctx.currentTime;
    tone(880, t, 0.12, { gain: 0.22 });
    tone(1318.5, t + 0.1, 0.18, { gain: 0.22 });
  }

  function playDeclinedBeep() {
    const ctx = getAudioCtx();
    const t = ctx.currentTime;
    tone(660, t, 0.09, { gain: 0.12, type: "sine" });
    tone(660, t + 0.16, 0.09, { gain: 0.12, type: "sine" });
  }

  function playErrorBuzz() {
    const ctx = getAudioCtx();
    const t = ctx.currentTime;
    tone(160, t, 0.35, { gain: 0.2, type: "sawtooth" });
  }

  // --- View state ---------------------------------------------------------
  function setState(state) {
    screen.className = `state-${state}`;
  }

  function scheduleReset() {
    clearTimeout(resetTimer);
    resetTimer = setTimeout(resetToIdle, RESET_DELAY_MS);
  }

  function resetToIdle() {
    clearTimeout(resetTimer);
    setState("idle");
    busy = false;
    buffer = "";
    closeManualEntry();
    focusInput();
  }

  function focusInput() {
    if (manualEntryOpen) return; // don't steal focus from the manual field
    input.value = "";
    input.focus();
  }

  // --- Manual barcode entry ------------------------------------------
  // Staff-only device (physical access to this kiosk is already the
  // access control), so this is a plain toggle — no PIN or extra
  // confirmation gate needed on top of that.
  function openManualEntry() {
    manualEntryOpen = true;
    manualForm.classList.remove("hidden");
    manualInput.value = "";
    manualInput.focus();
  }

  function closeManualEntry() {
    manualEntryOpen = false;
    manualForm.classList.add("hidden");
    manualInput.value = "";
  }

  manualToggle.addEventListener("click", () => {
    getAudioCtx(); // unlock audio on first real user interaction
    if (busy) return;
    openManualEntry();
  });

  manualForm.addEventListener("submit", (e) => {
    e.preventDefault();
    if (busy) return;
    const barcode = manualInput.value.trim();
    closeManualEntry();
    if (barcode) submitBarcode(barcode);
  });

  function fmtName(member) {
    if (!member) return "";
    return [member.first_name, member.last_name].filter(Boolean).join(" ");
  }

  function showRedeemed(data) {
    document.getElementById("redeemed-photo").src = data.member?.photo_url || "";
    document.getElementById("redeemed-photo").style.visibility =
      data.member?.photo_url ? "visible" : "hidden";
    document.getElementById("redeemed-name").textContent = fmtName(data.member);
    document.getElementById("redeemed-offering").textContent = data.offering_name || "";
    document.getElementById("redeemed-instructor").textContent =
      data.instructor_name ? `with ${data.instructor_name}` : "";
    document.getElementById("redeemed-remaining").textContent =
      `${data.remaining_instances} of ${data.total_instances} sessions remaining`;
    setState("redeemed");
    playSuccessChime();
  }

  function showDeclined(data) {
    document.getElementById("declined-photo").src = data.member?.photo_url || "";
    document.getElementById("declined-photo").style.visibility =
      data.member?.photo_url ? "visible" : "hidden";
    document.getElementById("declined-name").textContent = fmtName(data.member);
    document.getElementById("declined-message").textContent = data.message || "Already redeemed today";
    setState("declined");
    playDeclinedBeep();
  }

  function showError(data) {
    document.getElementById("error-message").textContent = data.message || "Something went wrong";
    setState("error");
    playErrorBuzz();
  }

  // --- Scan handling --------------------------------------------------
  async function submitBarcode(barcode) {
    busy = true;
    setState("loading");
    try {
      const resp = await fetch("/api/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ barcode }),
      });
      const data = await resp.json();

      if (data.status === "redeemed") {
        showRedeemed(data);
      } else if (data.status === "declined") {
        showDeclined(data);
      } else {
        showError(data);
      }
    } catch (err) {
      showError({ message: "Unable to reach server — see front desk" });
    }
    scheduleReset();
  }

  input.addEventListener("keydown", (e) => {
    getAudioCtx(); // unlock audio on first real hardware keystroke

    if (busy) {
      // Swallow keystrokes while a result is on screen / a scan is in
      // flight, so a stray second scan can't interleave with the first.
      e.preventDefault();
      return;
    }

    if (e.key === "Enter") {
      e.preventDefault();
      const barcode = buffer.trim();
      buffer = "";
      input.value = "";
      if (barcode) submitBarcode(barcode);
      return;
    }

    if (e.key.length === 1) {
      buffer += e.key;
    }
  });

  // Keep the capture input focused no matter what the operator/member taps
  // — except while the manual-entry form is open, where focus needs to
  // stay in that field instead.
  input.addEventListener("blur", () => setTimeout(focusInput, 50));
  document.addEventListener("click", (e) => {
    if (manualEntryOpen && (e.target === manualInput || manualForm.contains(e.target))) {
      return; // let clicks inside the manual-entry form keep their focus
    }
    focusInput();
  });

  resetToIdle();
})();
