// Draws a bracket (from /api/state -> bracket) into a container element.
(function () {
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

  function side(name, m) {
    if (!name) {
      const label = m.state === "bye" || m.state === "empty" ? "Bye" : "TBD";
      return `<div class="b-side b-empty">${label}</div>`;
    }
    const cls = m.winner ? (m.winner === name ? "b-win" : "b-lose") : "";
    return `<div class="b-side ${cls}">${esc(name)}</div>`;
  }

  window.renderBracket = function (el, bracket) {
    if (!bracket || !bracket.rounds.length) {
      el.innerHTML = `<p class="muted">The bracket hasn't been drawn yet.</p>`;
      return;
    }
    const champ = bracket.champion
      ? `<div class="b-champion">🏆 Champion: <b>${esc(bracket.champion)}</b></div>` : "";
    const compact = bracket.rounds[0].matches.length > 8 ? " b-compact" : "";
    el.innerHTML = champ + `<div class="bracket${compact}">${bracket.rounds.map(r => `
      <div class="b-round">
        <div class="b-round-name">${esc(r.name)}</div>
        <div class="b-matches">${r.matches.map(m => `
          <div class="b-match b-${m.state}">
            ${side(m.p1, m)}${side(m.p2, m)}
            ${m.state === "playing" && m.table ? `<div class="b-tag b-live">Table ${m.table}</div>` : ""}
            ${m.state === "playing" && !m.table ? `<div class="b-tag">On deck</div>` : ""}
            ${m.walkover ? `<div class="b-tag">Forfeit</div>` : (m.score ? `<div class="b-tag">${esc(m.score)}</div>` : "")}
          </div>`).join("")}
        </div>
      </div>`).join("")}</div>`;
  };
})();
