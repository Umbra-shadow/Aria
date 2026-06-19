// Aria — interactive dream-world client (turn-based; chat + generated scene images).
// The DashScope key lives server-side in .env (it powers both the brain and the
// Wan image generation), so there is no key field here.
const $ = s => document.querySelector(s);
let runId = null, guide = "Lyra", busy = false;
const JSON_HEADERS = { "Content-Type": "application/json" };

const esc = s => (s||"").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const EMOS = ["warm","wonder","joy","grand","tender","calm"];

function addBubble(cls, who, text){
  const d = document.createElement("div"); d.className = "bubble " + cls;
  d.innerHTML = `<div class="who">${esc(who)}</div><div class="line"></div>`;
  $("#chatlog").appendChild(d); $("#chatlog").scrollTop = $("#chatlog").scrollHeight;
  return d.querySelector(".line");
}
// typewriter — feels like live generation
async function type(el, text){
  el.textContent = "";
  for (let i = 0; i < text.length; i++){
    el.textContent += text[i];
    if (i % 2 === 0){ $("#chatlog").scrollTop = $("#chatlog").scrollHeight; await new Promise(r => setTimeout(r, 12)); }
  }
}

function setScene(beat){
  const sc = $("#scene");
  EMOS.forEach(e => sc.classList.remove("emo-" + e));
  sc.classList.add("emo-" + (beat.emotion || "warm"));
  // clear the previous beat's media; the new video is polled for below
  sc.querySelectorAll("video,img.scene-img").forEach(el => el.remove());
  $("#videoBadge").textContent = "generating video…";
  $("#sceneState").textContent = "";
  // mirror moment — reflect what you told the guide
  if (beat.special === "mirror"){
    const f = beat.facts || {};
    $("#mirror").style.display = "";
    $("#faceOrb").textContent = [f.name, f.gender, f.appearance].filter(Boolean).join(" · ") || "your reflection";
  } else { $("#mirror").style.display = "none"; }
  $("#caption").style.display = "";
  $("#cwho").textContent = guide;
  $("#ctext").textContent = beat.reply;
  $("#sceneMeta").textContent = "the guide is speaking this scene into being…";
}

function showMedia(clip){
  const sc = $("#scene");
  sc.querySelectorAll("video,img.scene-img").forEach(el => el.remove());
  if (clip.kind === "video" && clip.url){
    $("#videoBadge").textContent = "happyhorse";
    const v = document.createElement("video");
    v.src = clip.url; v.autoplay = true; v.playsInline = true; v.controls = true; v.muted = false;
    sc.prepend(v);
    // honour browser autoplay policy: if sound-autoplay is blocked, mute & retry
    v.play().catch(() => { v.muted = true; v.play().catch(() => {}); });
    $("#sceneMeta").textContent = clip.scene ? clip.scene.slice(0, 160) : "";
  } else {
    $("#videoBadge").textContent = "scene";
    $("#sceneState").textContent = clip.meta?.reason || "add a DashScope key to generate video";
  }
}

async function pollClip(idx){
  $("#videoBadge").textContent = "generating video…";
  for (let i = 0; i < 100; i++){           // ~5 min ceiling (3s × 100)
    try{
      const c = await (await fetch(`/api/aria/${runId}/clip/${idx}`)).json();
      if (c.status === "pending"){
        $("#videoBadge").textContent = `generating video… ${i * 3}s`;
        await new Promise(r => setTimeout(r, 3000));
        continue;
      }
      showMedia(c); return;
    } catch(_){ await new Promise(r => setTimeout(r, 3000)); }
  }
  $("#videoBadge").textContent = "scene";
  $("#sceneState").textContent = "video took too long — the story continues";
}

async function renderBeat(beat){
  guide = beat.guide || guide;
  setScene(beat);
  const line = addBubble("guide", guide, "");
  await type(line, beat.reply);
  if (beat.asks){
    const a = document.createElement("div"); a.className = "asks"; a.textContent = "“" + beat.asks + "”";
    line.parentNode.appendChild(a);
  }
  $("#chatlog").scrollTop = $("#chatlog").scrollHeight;
  refreshMemory();
  // the talking video renders in the background — poll for it without blocking chat
  pollClip(beat.turn_no);
  if (beat.done){
    $("#say").disabled = true; $("#sayBtn").disabled = true;
    $("#say").placeholder = "✦ the dream is complete — thank you for following";
  }
}

async function begin(){
  $("#begin").disabled = true; $("#obErr").textContent = "";
  try{
    const r = await fetch("/api/aria/start", { method:"POST", headers: JSON_HEADERS });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const beat = await r.json(); runId = beat.run_id;
    $("#onboard").classList.add("hidden");
    $("#say").disabled = false; $("#sayBtn").disabled = false; $("#say").focus();
    await renderBeat(beat);
  } catch(e){ $("#obErr").textContent = e.message; $("#begin").disabled = false; }
}

async function say(){
  const text = $("#say").value.trim();
  if (busy || !runId) return;
  $("#say").value = "";
  if (text){ const l = addBubble("you", "You", ""); l.textContent = text; }
  busy = true; $("#sayBtn").disabled = true;
  const thinking = addBubble("guide", guide, "");
  thinking.innerHTML = '<span class="spin"></span>' + guide + " is with you…";
  try{
    const r = await fetch(`/api/aria/${runId}/say`, {
      method:"POST", headers: JSON_HEADERS, body: JSON.stringify({ text })
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const beat = await r.json();
    thinking.parentNode.remove();
    await renderBeat(beat);
  } catch(e){ thinking.textContent = "… something broke: " + e.message; }
  finally { busy = false; $("#sayBtn").disabled = false; }
}

async function refreshMemory(){
  if (!runId) return;
  try{
    const j = await (await fetch(`/api/aria/${runId}/memory`)).json();
    $("#memCount").textContent = (j.total || 0);
    const feed = $("#memFeed");
    if (!(j.engrams||[]).length){ feed.innerHTML = '<div class="mem-empty">your world will be remembered here</div>'; return; }
    feed.innerHTML = j.engrams.map(e =>
      `<div class="mem-row"><div class="mem-dot"></div><div class="mem-text" title="${esc(e.meaning||e.message||"")}">${esc(e.meaning||e.message||"")}</div></div>`
    ).join("");
  } catch(_){}
}

$("#begin").onclick = begin;
$("#sayBtn").onclick = say;
$("#say").addEventListener("keydown", e => { if (e.key === "Enter") say(); });
