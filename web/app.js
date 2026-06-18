// Aria — split-screen live dialogue client.
const $ = s => document.querySelector(s);
const KEY = "aria_qwen_key";
let convId = null, es = null;

// ── API key (per-tab, never persisted server-side) ──────────────────
const getKey = () => sessionStorage.getItem(KEY) || "";
const keyHeaders = () => { const k = getKey(); return k ? { "X-Qwen-Key": k } : {}; };
function reflectKey(){
  const k = getKey();
  $("#keyState").textContent = k ? ("set ✓ " + k.slice(0,3) + "…") : "no key";
  $("#keyState").className = "keystate" + (k ? " set" : "");
  if (k && !$("#key").value) $("#key").value = k;
}
function saveKey(){
  const v = $("#key").value.trim();
  if (v) sessionStorage.setItem(KEY, v); else sessionStorage.removeItem(KEY);
  reflectKey();
}
$("#saveKey").onclick = saveKey;
$("#key").addEventListener("keydown", e => { if (e.key === "Enter") saveKey(); });
reflectKey();

$("#examples").addEventListener("click", e => {
  if (e.target.classList.contains("chip")){ $("#topic").value = e.target.textContent; $("#topic").focus(); }
});

const esc = s => (s||"").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));

function addBubble(speaker, line, recalled){
  $("#empty")?.remove();
  const d = document.createElement("div");
  d.className = "bubble " + speaker;
  const who = speaker === "a" ? "Theo" : "Mara";
  d.innerHTML = `<div class="who">${who}</div><div class="line">${esc(line)}</div>` +
    (recalled != null ? `<div class="recall">↺ ${recalled} memories recalled</div>` : "");
  $("#stream").appendChild(d);
  $("#stream").scrollTop = $("#stream").scrollHeight;
}

function setThinking(speaker, on){
  let t = $("#thinkRow");
  if (on){
    if (!t){ t = document.createElement("div"); t.id = "thinkRow"; t.className = "thinking"; $("#stream").appendChild(t); }
    t.innerHTML = `<span class="spin"></span>${speaker === "a" ? "Theo" : "Mara"} is thinking…`;
    $("#stream").scrollTop = $("#stream").scrollHeight;
  } else { t?.remove(); }
}

function showVideo(ev){
  $("#videoBadge").textContent = ev.kind === "video" ? "happyhorse" : "placeholder";
  const card = $("#card");
  card.querySelectorAll("video").forEach(v => v.remove());
  if (ev.kind === "video" && ev.url){
    const v = document.createElement("video");
    v.src = ev.url; v.autoplay = true; v.muted = !ev.audio_url; v.playsInline = true; v.controls = false;
    card.prepend(v);
  }
  $("#caption").style.display = "";
  $("#cwho").textContent = ev.speaker === "a" ? "Theo" : "Mara";
  $("#ctext").textContent = ev.caption;
  $("#sceneMeta").textContent = ev.scene || "";
}

async function start(){
  const topic = $("#topic").value.trim();
  if (topic.length < 2){ $("#topic").focus(); return; }
  // Key is optional: a UI key (X-Qwen-Key) overrides the server's default brain,
  // but the server may already have one (Qwen in .env, or a Gemini .env.local).
  $("#go").disabled = true;
  $("#stream").innerHTML = "";
  try{
    const r = await fetch("/api/aria/start", {
      method: "POST", headers: { "Content-Type": "application/json", ...keyHeaders() },
      body: JSON.stringify({ topic, turns: 6 })
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const j = await r.json(); convId = j.conv_id;
    listen();
  } catch(e){
    addBubble("a", "Could not start: " + e.message);
    $("#go").disabled = false;
  }
}

function listen(){
  if (es) es.close();
  es = new EventSource(`/api/aria/${convId}/stream`);
  es.onmessage = ev => {
    const e = JSON.parse(ev.data);
    if (e.stage === "thinking") setThinking(e.speaker, true);
    else if (e.stage === "spoke"){ setThinking(e.speaker, false); addBubble(e.speaker, e.line, e.recalled); }
    else if (e.stage === "video") showVideo(e);
    else if (e.stage === "done"){ es.close(); $("#go").disabled = false; }
    else if (e.stage === "error"){ es.close(); $("#go").disabled = false; addBubble("a", "Error: " + (e.error || "unknown")); }
  };
}

$("#go").onclick = start;
$("#topic").addEventListener("keydown", e => { if (e.key === "Enter") start(); });
