// Passes messages from the sandboxed game frame to the platform API.
// Only messages from our own iframe count (event.source); the frame has an opaque origin, so we can't check origin.
(() => {
  const frame = document.getElementById("game");
  if (!frame) return;
  const version = frame.dataset.version;
  const signedIn = frame.dataset.signedIn === "yes";
  const status = document.getElementById("status");
  let session = null;

  const post = (url, body) =>
    fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), credentials: "same-origin" })
      .then((r) => r.json().then((j) => ({ ok: r.ok, ...j })));
  const say = (text) => { if (status) status.textContent = text; };

  window.addEventListener("message", async (event) => {
    if (event.source !== frame.contentWindow) return;
    const msg = event.data;
    if (!msg || msg.source !== "evolvia-game" || !signedIn) return;

    if (msg.event === "session-start") {
      const r = await post("/api/sessions", { version, seed: msg.seed });
      session = r.session;
    } else if (msg.event === "milestone" && session) {
      const r = await post(`/api/sessions/${session}/milestone`, { id: String(msg.id) });
      if (r.ok && r.new) {
        const li = document.querySelector(`[data-milestone="${CSS.escape(String(msg.id))}"]`);
        if (li) { li.classList.add("done"); li.textContent = "✓ " + r.name; }
        const locked = document.getElementById("locked");
        if (locked && r.eligible) {
          locked.innerHTML = "";
          const a = document.createElement("a");
          a.href = location.pathname + "#whatif"; a.textContent = "Unlocked! Reload the page to suggest a change.";
          a.addEventListener("click", (e) => { e.preventDefault(); location.reload(); });
          locked.append(a);
        }
        say(`Milestone reached: ${r.name}`);
      } else if (!r.ok && r.reason === "too fast") {
        say("That milestone came faster than the game allows, so it was not recorded.");
      }
    } else if (msg.event === "session-end" && session) {
      await post(`/api/sessions/${session}/end`, { score: msg.score });
      session = null;
    }
  });

  frame.addEventListener("load", () => frame.focus());
})();
