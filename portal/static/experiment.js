// experiment.js
// 依 ground_truth.csv 固定序列在瀏覽器內產生流量。
// 按鈕開始後變成「Stop Experiment」；手動停止走 /experiment/abort（不留紀錄）；
// 自動跑完走 /experiment/stop（正常存 infer log）。
//
// ground_truth.csv 序列（offset_sec, label）：
//   0   BROWSING
//   45  FT
//   115 P2P
//   170 FT
//   205 BROWSING
//   285 P2P
//   325 BROWSING
//   385 FT
//   435 P2P
//   510 END

"use strict";

const GROUND_TRUTH = [
  { offset: 0,   label: "BROWSING" },
  { offset: 45,  label: "FT"       },
  { offset: 115, label: "P2P"      },
  { offset: 170, label: "FT"       },
  { offset: 205, label: "BROWSING" },
  { offset: 285, label: "P2P"      },
  { offset: 325, label: "BROWSING" },
  { offset: 385, label: "FT"       },
  { offset: 435, label: "P2P"      },
  { offset: 510, label: "END"      },
];

// ── 流量產生參數 ──────────────────────────────────────────────────────
const BROWSING_INTERVAL_MS = 4000;
const P2P_INTERVAL_MS      = 8000;
const FT_CHUNK_DELAY_MS    = 200;

// ── 狀態 ─────────────────────────────────────────────────────────────
let _running = false;
let _timers  = [];   // setTimeout / setInterval handles
let _ftFlags = [];   // FT loop 的 active flag objects

// ── UI ────────────────────────────────────────────────────────────────
const btn       = document.getElementById("exp-btn");
const statusDiv = document.getElementById("exp-status");

function setStatus(html) {
  statusDiv.innerHTML = html;
}

function setPhase(label, remainSec) {
  const remain = remainSec > 0
    ? ` — <span>${remainSec}s remaining</span>`
    : "";
  setStatus(`Phase: <span class="phase">${label}</span>${remain}`);
}

// ── 流量產生函式 ──────────────────────────────────────────────────────

function startBrowsing(durationMs) {
  const h = setInterval(() => {
    fetch("/", { method: "GET", cache: "no-store" }).catch(() => {});
  }, BROWSING_INTERVAL_MS);
  _timers.push(h);
  setTimeout(() => clearInterval(h), durationMs);
}

function startFT(durationMs) {
  const flag = { active: true };
  _ftFlags.push(flag);

  async function loop() {
    while (flag.active) {
      try {
        const res = await fetch("/static/testfile.bin", { cache: "no-store" });
        await res.arrayBuffer();
      } catch (_) {}
      if (flag.active) await sleep(FT_CHUNK_DELAY_MS);
    }
  }
  loop();
  setTimeout(() => { flag.active = false; }, durationMs);
}

function startP2P(durationMs) {
  const h = setInterval(() => {
    fetch("/static/testfile.bin", {
      headers: { Range: "bytes=0-32767" },
      cache: "no-store",
    }).catch(() => {});
  }, P2P_INTERVAL_MS);
  _timers.push(h);
  setTimeout(() => clearInterval(h), durationMs);
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// ── 清除所有 timer ────────────────────────────────────────────────────
function clearAll() {
  for (const h of _timers) {
    clearTimeout(h);
    clearInterval(h);
  }
  _timers = [];
  for (const f of _ftFlags) f.active = false;
  _ftFlags = [];
}

// ── UI reset ──────────────────────────────────────────────────────────
function resetUI() {
  _running = false;
  btn.textContent = "Start Experiment";
  btn.classList.remove("btn-stop");
  btn.disabled = false;
}

// ── 主流程 ────────────────────────────────────────────────────────────
async function runExperiment() {
  if (_running) return;
  _running = true;
  btn.textContent = "Stop Experiment";
  btn.classList.add("btn-stop");
  setStatus("Starting…");

  // 1. 通知 Portal 實驗開始
  try {
    const res = await fetch("/experiment/start", { method: "POST" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
  } catch (e) {
    setStatus(`<span class="err">Failed to start: ${e.message}</span>`);
    resetUI();
    return;
  }

  const experimentStart = Date.now();
  const totalSec = GROUND_TRUTH[GROUND_TRUTH.length - 1].offset;

  // 2. 排程各流量階段
  for (let i = 0; i < GROUND_TRUTH.length - 1; i++) {
    const cur      = GROUND_TRUTH[i];
    const next     = GROUND_TRUTH[i + 1];
    const startMs  = cur.offset * 1000;
    const durMs    = (next.offset - cur.offset) * 1000;

    const h = setTimeout(() => {
      if (!_running) return;
      if (cur.label === "BROWSING") startBrowsing(durMs);
      else if (cur.label === "FT")  startFT(durMs);
      else if (cur.label === "P2P") startP2P(durMs);
    }, startMs);
    _timers.push(h);
  }

  // 3. 每秒更新 phase + 倒數
  const countdownH = setInterval(() => {
    if (!_running) { clearInterval(countdownH); return; }
    const elapsed = Math.floor((Date.now() - experimentStart) / 1000);
    const remain  = totalSec - elapsed;
    if (remain <= 0) { clearInterval(countdownH); return; }
    let curLabel = GROUND_TRUTH[0].label;
    for (const step of GROUND_TRUTH) {
      if (elapsed >= step.offset) curLabel = step.label;
    }
    if (curLabel !== "END") setPhase(curLabel, remain);
  }, 1000);
  _timers.push(countdownH);

  // 4. 實驗自然結束
  const endH = setTimeout(async () => {
    clearAll();
    setStatus("Finishing…");
    btn.disabled = true;

    try {
      const res  = await fetch("/experiment/stop", { method: "POST" });
      const data = await res.json();
      if (res.ok) {
        setStatus(
          `<span class="done">✓ Done!</span> ` +
          `Model: <b>${data.model}</b> · ` +
          `${data.rows} ticks recorded`
        );
      } else {
        setStatus(`<span class="err">Error: ${data.message}</span>`);
      }
    } catch (e) {
      setStatus(`<span class="err">Stop failed: ${e.message}</span>`);
    }
    resetUI();
  }, totalSec * 1000);
  _timers.push(endH);
}

// ── 手動中斷 ──────────────────────────────────────────────────────────
async function abortExperiment() {
  clearAll();
  _running = false;               // 先關掉，避免 clearAll 後的 callback 誤判
  btn.disabled = true;
  setStatus("Aborting…");

  try {
    await fetch("/experiment/abort", { method: "POST" });
  } catch (_) {}

  setStatus("Experiment aborted.");
  resetUI();
}

// ── 按鈕綁定 ─────────────────────────────────────────────────────────
btn.addEventListener("click", () => {
  if (_running) abortExperiment();
  else          runExperiment();
});