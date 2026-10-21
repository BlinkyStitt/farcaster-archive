"use strict";

const $ = (id) => document.getElementById(id);
const titles = {own: "Your casts", all: "All saved casts", reactions: "Likes & recasts", quotes: "Quoted casts", media: "Media & links", contacts: "People & contacts", exports: "Export files & gaps", thread: "Conversation"};
const descriptions = {own: "Your casts and replies in time order. Open any conversation to read the full thread.", all: "Every obtainable cast saved with your archive, including other authors’ conversation context.", reactions: "Saved reaction targets, including retained like, recast, and removal history. Dates below are cast dates.", quotes: "Obtainable casts you quoted. Missing targets remain listed below.", media: "Saved images, videos, and files, plus every original URL and recorded download gap.", contacts: "Public names, bios, social accounts, links, and wallets. Contact details vary by person.", exports: "Original machine-readable exports, readable archives, and the recorded content gaps.", thread: "Replies sit beneath their parents. Each branch runs oldest to newest."};
let summary, view = "own", generation = 0, next = 0, loading = false, total = 0, shown = 0, lastMonth = "", currentParams = new URLSearchParams();

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function safeURL(value) {
  try {
    const u = new URL(value);
    return ["https:", "http:", "mailto:", "tel:", "xmpp:", "matrix:", "nostr:"].includes(u.protocol) ? u.href : null;
  } catch { return null; }
}
function external(text, url) {
  const href = safeURL(url);
  if (!href) return el("span", "", text);
  const a = el("a", "", text);
  a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer";
  return a;
}
function button(text, action, className = "") {
  const b = el("button", className, text); b.type = "button";
  b.addEventListener("click", action); return b;
}
function internal(text, hash, className = "") {
  const a = el("a", className, text); a.href = hash; return a;
}
function formatDate(stamp) {
  return new Date(stamp * 1000).toLocaleString(undefined, {year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
}
function dateInput(stamp) {
  const date = new Date(stamp * 1000);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
function localDay(value, nextDay = false) {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day + Number(nextDay)).getTime() / 1000;
}
function number(n) { return Number(n).toLocaleString(); }
function bytes(n) { return n >= 1024 ** 3 ? (n / 1024 ** 3).toFixed(2) + " GB" : n >= 1024 ** 2 ? (n / 1024 ** 2).toFixed(1) + " MB" : n >= 1024 ? (n / 1024).toFixed(1) + " KB" : n + " B"; }
function textWithLinks(text) {
  const node = el("div", "cast-text");
  let start = 0;
  for (const m of text.matchAll(/https?:\/\/[^\s<>"']+/g)) {
    node.append(document.createTextNode(text.slice(start, m.index)));
    node.append(external(m[0], m[0])); start = m.index + m[0].length;
  }
  node.append(document.createTextNode(text.slice(start))); return node;
}
async function api(route, params = {}) {
  const response = await fetch(route + "?" + new URLSearchParams(params));
  if (!response.ok) throw new Error("The local viewer could not load this page (HTTP " + response.status + ").");
  return response.json();
}
function report(error) { $("error").hidden = false; $("error").textContent = error.message || String(error); }
function savedURL(path) { return "/saved/" + path.split("/").map(encodeURIComponent).join("/"); }
function unavailable(hash, message = "This referenced cast was unavailable when the archive was made. Its absence does not prove deletion.") {
  const box = el("div", "missing"); box.append(el("strong", "", "Unavailable cast"), el("div", "", message), el("div", "cast-hash", hash)); return box;
}
function attachment(item) {
  const box = el("div", "attachment");
  const kind = item.detected_content_type || item.content_type || "";
  if (item.status === "downloaded" && item.path) {
    const url = savedURL(item.path);
    if (kind.startsWith("image/") && kind !== "image/svg+xml") {
      const image = el("img"); image.src = url; image.loading = "lazy"; image.alt = "Saved image from cast";
      image.addEventListener("error", () => { image.replaceWith(el("p", "muted", "Image preview unavailable. Open the saved file below.")); }, {once: true});
      box.append(image);
    } else if (kind.startsWith("video/") || item.format === "hls-vod") {
      const video = el("video"); video.controls = true; video.preload = "none"; video.playsInline = true;
      if (item.format !== "hls-vod" || video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = url; box.append(video);
      } else box.append(el("p", "muted", "Saved HLS video. Open the playlist in Safari or a compatible player."));
    } else if (kind.startsWith("audio/")) {
      const audio = el("audio"); audio.controls = true; audio.preload = "none"; audio.src = url; box.append(audio);
    }
  }
  const info = el("div", "attachment-info");
  info.append(el("span", "status", item.status === "downloaded" ? "Saved locally · " + bytes(item.bundle_bytes || item.bytes || 0) : item.status === "non-media-link" ? "Web link · original URL saved" : "Not downloaded · original URL saved"));
  if (item.status === "downloaded" && item.path) {
    const a = el("a", "", item.format === "hls-vod" ? "Open saved video playlist" : "Open saved file");
    a.href = savedURL(item.path); a.target = "_blank"; a.rel = "noopener"; info.append(a, document.createTextNode(" · "));
  }
  info.append(external(item.url, item.url));
  if (item.detail) info.append(el("div", "detail", item.detail));
  box.append(info); return box;
}
function castCard(cast, compact = false) {
  if (!cast || cast.unavailable) return unavailable(cast?.hash || "Unknown hash");
  const card = el("article", "cast" + (cast.fid === summary.fid ? " own" : "")); card.dataset.hash = cast.hash; card.dataset.timestamp = cast.timestamp;
  const head = el("div", "cast-head"); head.append(el("div", "avatar", cast.author.slice(0, 1).toUpperCase()));
  const who = el("div"); const name = el("div", "author", cast.author);
  name.append(el("span", "handle", cast.username ? "@" + cast.username : "FID " + cast.fid));
  const date = el("time", "", formatDate(cast.timestamp)); date.dateTime = cast.published;
  who.append(name, date); head.append(who);
  if (cast.fid === summary.fid) head.append(el("span", "badge", "Your cast"));
  card.append(head);
  if (cast.parent && !compact) {
    const context = el("div", "parent-preview"); const p = cast.parent_preview;
    const label = p ? "Reply to " + p.author + ": “" + p.text.slice(0, 160) + (p.text.length > 160 ? "…" : "") + "”" : "Reply to an unavailable parent · show saved context";
    context.append(button(label, async () => {
      if (context.querySelector(".context")) { context.querySelector(".context").remove(); return; }
      const target = el("div", "context", "Loading context…"); context.append(target);
      try {
        const data = await api("/api/cast", {hash: cast.hash}); target.replaceChildren();
        for (const parent of data.ancestors) target.append(castCard(parent, true));
      } catch (e) { target.textContent = e.message; }
    })); card.append(context);
  }
  card.append(textWithLinks(cast.text));
  if (cast.media?.length) {
    const media = el("div", "media-grid");
    for (const item of cast.media) media.append(attachment(item)); card.append(media);
  }
  for (const h of cast.quotes || []) card.append(internal("Read quoted cast →", "#thread?hash=" + encodeURIComponent(h), "quote-link"));
  const actions = el("div", "cast-actions");
  actions.append(internal("Open full thread" + (cast.replies ? " · " + number(cast.replies) + " direct replies" : ""), "#thread?hash=" + encodeURIComponent(cast.hash)));
  actions.append(external("Original cast ↗", "https://farcaster.xyz/~/conversations/" + cast.hash));
  if (cast.channel) actions.append(el("span", "", "/" + cast.channel));
  if (cast.labels?.length) actions.append(el("span", "", cast.labels.join(" · ")));
  card.append(actions);
  const metadata = el("details", "metadata"); metadata.append(el("summary", "", "Hash & original metadata"));
  const hash = el("div", "cast-hash", cast.hash); metadata.append(hash);
  metadata.addEventListener("toggle", async () => {
    if (!metadata.open || metadata.dataset.loaded) return;
    metadata.dataset.loaded = "1"; const pre = el("pre", "", "Loading…"); metadata.append(pre);
    try { pre.textContent = JSON.stringify(await api("/api/cast", {hash: cast.hash, raw: 1}), null, 2); }
    catch (e) { pre.textContent = e.message; delete metadata.dataset.loaded; }
  }); card.append(metadata); return card;
}
function contactCard(person) {
  const card = el("article", "contact");
  card.append(el("h2", "", person.display_name || "FID " + person.fid), el("div", "muted", "@" + person.farcaster_username + " · FID " + person.fid + " · " + person.relationships.map(r => r === "following" ? "You follow" : "Follows you").join(" · ")));
  card.append(el("p", "bio", person.bio));
  const links = el("div", "contact-links"); links.append(external("Farcaster profile", person.farcaster_url));
  const seen = new Set();
  for (const endpoint of person.endpoints) {
    const key = endpoint.kind + endpoint.value;
    if (seen.has(key)) continue; seen.add(key);
    const link = external(endpoint.kind + ": " + endpoint.value, endpoint.url);
    link.title = endpoint.verification + " · " + endpoint.source_field; links.append(link);
  }
  card.append(links);
  if (person.verified_eth_addresses.length || person.verified_sol_addresses.length) {
    const details = el("details"); details.append(el("summary", "", "Verified public wallet addresses"));
    for (const a of [...person.verified_eth_addresses, ...person.verified_sol_addresses]) details.append(el("span", "wallet", a)); card.append(details);
  }
  return card;
}
function filePanel(label, prefix) {
  const panel = el("section", "panel"); panel.append(el("h2", "", label)); const list = el("div", "file-list");
  for (const file of summary.exports.filter(f => f.key.startsWith(prefix + "/"))) {
    const row = el("div", "file-row"); const a = el("a", "", file.key.split("/").at(-1));
    a.href = "/files/" + file.key; a.download = ""; row.append(a, el("span", "", bytes(file.bytes))); list.append(row);
  }
  panel.append(list); return panel;
}
function renderExports() {
  const panel = el("section", "panel"); panel.append(el("h2", "", "What is saved"));
  panel.append(el("p", "", number(summary.authored) + " of your surviving casts and replies; " + number(summary.all_casts) + " total casts with context; " + number(summary.media_downloaded) + " downloaded media items; " + number(summary.contacts.unique_accounts) + " unique contact profiles."));
  panel.append(el("h2", "", "Recorded gaps"));
  panel.append(el("p", "", summary.complete ? "The archive has no recorded content gaps. Original URLs remain saved." : "The archive remains marked incomplete. Original URLs and unavailable references remain saved. Missing records can be deleted, pruned, or unavailable from the sources; the viewer does not assume the cause."));
  for (const [kind, count] of Object.entries(summary.issues)) panel.append(el("div", "file-row", kind + ": " + number(count) + " issue records"));
  panel.append(el("p", "", number(summary.authored_embed_unresolved) + " unresolved embed URLs appear in your own casts. Multiple issue records can refer to one unavailable cast."));
  $("feed").append(panel, filePanel("Cast archive exports", "casts"), filePanel("Contact exports", "contacts"));
  if (summary.exports.some(f => f.key.startsWith("reports/"))) $("feed").append(filePanel("Media recovery reports", "reports"));
  $("results-label").textContent = "Downloads come directly from the saved snapshots. No files are changed.";
}
async function renderThread(params, token) {
  const data = await api("/api/thread", {hash: params.get("hash") || ""});
  if (token !== generation) return;
  const nodes = new Map(data.items.map(c => [c.hash, c])), children = new Map(), path = new Set();
  let focus = nodes.get(data.focus);
  while (focus && !path.has(focus.hash)) { path.add(focus.hash); focus = nodes.get(focus.parent); }
  for (const cast of data.items) {
    const key = cast.parent && nodes.has(cast.parent) ? cast.parent : "root";
    if (!children.has(key)) children.set(key, []); children.get(key).push(cast);
  }
  const heading = el("div", "thread-heading"); heading.append(button("← Back", () => history.length > 1 ? history.back() : location.assign("#own"), "back"));
  heading.append(el("p", "", number(data.items.length) + " saved casts · replies grouped by parent")); $("feed").append(heading);
  if (!nodes.has(data.root)) $("feed").append(unavailable(data.root, "The thread root is unavailable. Obtainable replies appear below."));
  if (!data.items.length) return;
  function branch(cast, depth = 0) {
    const section = el("section", "thread-node" + (cast.hash === data.focus ? " focus" : "")); section.id = "cast-" + cast.hash;
    if (cast.parent && !nodes.has(cast.parent) && cast.parent !== data.root) section.append(unavailable(cast.parent, "The immediate parent is unavailable. This branch stays attached to its known conversation."));
    section.append(castCard(cast, true));
    const replies = children.get(cast.hash) || [];
    if (replies.length) {
      const container = el("div", depth >= 4 ? "thread-children deep-branch" : "thread-children"); let cursor = 0;
      const rendered = new Map();
      const more = button("Show replies (" + replies.length + ")", append, "thread-more");
      function insert(index) {
        if (rendered.has(index)) return;
        const node = branch(replies[index], depth + 1);
        const after = [...rendered.keys()].filter(i => i > index).sort((a,b) => a-b)[0];
        container.insertBefore(node, after === undefined ? more : rendered.get(after));
        rendered.set(index, node);
        more.hidden = rendered.size >= replies.length;
        more.textContent = "Show more replies (" + (replies.length - rendered.size) + " hidden)";
      }
      function append() {
        const end = Math.min(cursor + 20, replies.length);
        for (; cursor < end; cursor++) insert(cursor);
      }
      container.append(more); section.append(container);
      if (depth < 1 || path.has(cast.hash)) {
        append();
        const needed = replies.findIndex(c => path.has(c.hash));
        if (needed >= cursor) insert(needed);
      }
    }
    return section;
  }
  for (const cast of children.get("root") || []) $("feed").append(branch(cast));
  $("results-label").textContent = "All saved thread records loaded. Expand branches to read more replies.";
  requestAnimationFrame(() => document.getElementById("cast-" + data.focus)?.scrollIntoView({block: "center"}));
}
async function loadMore(token = generation) {
  if (loading || next === null || ["thread", "exports"].includes(view)) return;
  loading = true; $("more").disabled = true; $("more").textContent = "Loading…";
  const params = Object.fromEntries(currentParams); params.offset = next; params.limit = 30;
  if (!["contacts", "media"].includes(view)) params.view = view;
  const route = view === "contacts" ? "/api/contacts" : view === "media" ? "/api/media" : "/api/casts";
  try {
    const data = await api(route, params);
    if (token !== generation) return;
    total = data.total; next = data.next;
    for (const item of data.items) {
      if (!["media", "contacts"].includes(view)) {
        const month = new Date(item.timestamp * 1000).toLocaleString(undefined, {month: "long", year: "numeric"});
        if (lastMonth !== month) { $("feed").append(el("div", "date-separator", month)); lastMonth = month; }
        $("feed").append(castCard(item));
      } else if (view === "contacts") $("feed").append(contactCard(item));
      else {
        const card = el("article", "media-card"); card.append(attachment(item));
        const hashes = item.authored_casts?.length ? item.authored_casts : item.referenced_by;
        if (hashes?.length) card.append(internal("Read referencing cast →", "#thread?hash=" + hashes[0], "quote-link"));
        $("feed").append(card);
      }
    }
    shown += data.items.length;
    $("results-label").textContent = "Showing " + number(shown) + " of " + number(total) + (view === "contacts" ? " people" : view === "media" ? " URLs" : " casts") + (params.order === "desc" ? " · newest first" : ["contacts", "media"].includes(view) ? "" : " · oldest first") + ".";
    if (!total) $("feed").append(el("div", "empty", "No saved records match these filters."));
    $("more").hidden = next === null;
  } catch (e) { if (token === generation) report(e); }
  finally { if (token === generation) { loading = false; $("more").disabled = false; $("more").textContent = "Load more"; requestAnimationFrame(maybeLoad); } }
}
function maybeLoad() {
  if (!loading && next !== null && shown > 0 && $("sentinel").getBoundingClientRect().top < innerHeight + 500) loadMore();
}
async function route() {
  if (!summary) return;
  const [target, query = ""] = location.hash.slice(1).split("?"); view = titles[target] ? target : "own";
  const token = ++generation; currentParams = new URLSearchParams(query);
  next = 0; shown = 0; loading = false; lastMonth = "";
  $("feed").replaceChildren(); $("error").hidden = true; $("more").hidden = true;
  $("results-label").textContent = "Loading saved records…";
  document.body.dataset.view = view; $("title").textContent = titles[view]; $("subtitle").textContent = descriptions[view];
  for (const a of document.querySelectorAll("nav a")) a.toggleAttribute("aria-current", a.dataset.view === view);
  for (const a of document.querySelectorAll("nav a[aria-current]")) a.setAttribute("aria-current", "page");
  const casts = ["own", "all", "reactions", "quotes"].includes(view);
  $("filters").hidden = ["thread", "exports"].includes(view); $("overview").hidden = !["own", "exports"].includes(view);
  for (const name of ["order", "from", "to"]) $(name + "-label").hidden = !casts;
  $("relationship-label").hidden = view !== "contacts"; $("status-label").hidden = view !== "media"; $("own-media-label").hidden = view !== "media";
  $("search").value = currentParams.get("q") || ""; $("order").value = currentParams.get("order") || "asc";
  $("relationship").value = currentParams.get("relationship") || ""; $("status").value = currentParams.get("status") || ""; $("own-media").checked = currentParams.get("own") === "1";
  $("from").value = currentParams.has("from") ? dateInput(Number(currentParams.get("from"))) : "";
  $("to").value = currentParams.has("to") ? dateInput(Number(currentParams.get("to")) - 1) : "";
  window.scrollTo(0, 0);
  try {
    if (view === "exports") renderExports();
    else if (view === "thread") await renderThread(currentParams, token);
    else {
      await loadMore(token);
      if (["reactions", "quotes"].includes(view)) {
        const missing = await api("/api/missing-targets");
        if (token !== generation) return;
        const subset = missing.filter(t => view === "reactions" ? JSON.parse(t.labels).some(label => label !== "Quoted") : JSON.parse(t.labels).includes("Quoted"));
        if (subset.length) {
          const details = el("details", "panel"); details.append(el("summary", "", number(subset.length) + " unavailable referenced casts · show hashes"));
          for (const t of subset) details.append(unavailable(t.hash, JSON.parse(t.labels).join(" · ") + " · FID " + t.fid));
          $("feed").prepend(details);
        }
      }
    }
  } catch (e) { if (token === generation) report(e); }
}
$("filters").addEventListener("submit", (event) => {
  event.preventDefault(); const params = new URLSearchParams();
  if ($("search").value.trim()) params.set("q", $("search").value.trim());
  if (["own", "all", "reactions", "quotes"].includes(view)) {
    params.set("order", $("order").value);
    if ($("from").value) params.set("from", localDay($("from").value));
    if ($("to").value) params.set("to", localDay($("to").value, true));
  }
  if (view === "contacts" && $("relationship").value) params.set("relationship", $("relationship").value);
  if (view === "media") { if ($("status").value) params.set("status", $("status").value); if ($("own-media").checked) params.set("own", "1"); }
  const hash = "#" + view + "?" + params; if (location.hash === hash) route(); else location.hash = hash;
});
$("more").addEventListener("click", () => loadMore());
document.querySelector(".skip").addEventListener("click", e => { e.preventDefault(); $("content").focus(); });
const scrollObserver = new IntersectionObserver(entries => { if (entries.some(e => e.isIntersecting)) maybeLoad(); }, {rootMargin: "500px"});
scrollObserver.observe($("sentinel"));
window.addEventListener("hashchange", route);
(async () => {
  try {
    summary = await api("/api/summary");
    $("account").textContent = "FID " + summary.fid;
    $("own-count").textContent = number(summary.authored); $("all-count").textContent = number(summary.all_casts);
    for (const [n, label] of [[summary.authored, "Your casts & replies"], [summary.media_downloaded, "Saved media items"], [summary.contacts.unique_accounts, "Contact profiles"]]) {
      const metric = el("div", "", label); metric.prepend(el("strong", "", number(n))); $("overview").append(metric);
    }
    await route();
  } catch (e) { report(e); }
})();
