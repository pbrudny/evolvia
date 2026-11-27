// Live build progress: while any idea is accepted or being built, poll its status every 4 seconds and update the card.
(() => {
  const section = document.getElementById("builds");
  if (!section) return;
  const game = section.dataset.game;
  const busy = (st) => st === "accepted" || st === "building";
  const cards = () => [...section.querySelectorAll("[data-build]")];
  const anyBusy = () => cards().some((li) => busy(li.querySelector(".pill").textContent.trim()));

  async function poll() {
    let rows;
    try { rows = await fetch(`/api/games/${encodeURIComponent(game)}/builds`).then((r) => r.json()); } catch { return schedule(); }
    for (const b of rows) {
      const li = section.querySelector(`[data-build="${b.id}"]`);
      if (!li) { if (busy(b.status)) location.reload(); continue; }  // a new idea went into building: show its card
      const pill = li.querySelector(".pill"), note = li.querySelector(".note"), link = li.querySelector(".new-version");
      pill.textContent = b.status;
      pill.className = `pill status-${b.status}`;
      note.textContent = b.build_note || "";
      if (busy(b.status)) { const sp = document.createElement("span"); sp.className = "spinner"; sp.setAttribute("aria-hidden", "true"); note.prepend(sp); }
      if (b.result_version) { link.href = `/g/${game}/v/${b.result_version}`; link.hidden = false; }
    }
    schedule();
  }
  function schedule() { if (anyBusy()) setTimeout(poll, 4000); }
  schedule();
})();
