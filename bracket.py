"""Single-elimination bracket.

The first round is stored as a list of positions (bracket_slots); positions
(0,1), (2,3), ... meet in round 1 and an empty position is a bye. Everything
after that is derived: the winner of round r, slot k plays in round r+1,
slot k // 2. Matches are created as soon as both players for a slot are known.
"""
import random


def bracket_size(n):
    size = 2
    while size < n:
        size *= 2
    return size


def round_name(round_no, total_rounds):
    remaining = 2 ** (total_rounds - round_no + 1)
    return {2: "Final", 4: "Semifinals", 8: "Quarterfinals"}.get(remaining, f"Round of {remaining}")


def random_slots(player_ids, rng=random):
    """Random draw with byes spread out so no first-round pair is bye-vs-bye."""
    ids = list(player_ids)
    rng.shuffle(ids)
    size = bracket_size(len(ids))
    pairs = size // 2
    slots = [None] * size
    for k in range(pairs):
        slots[2 * k] = ids[k]
    for k, pid in zip(rng.sample(range(pairs), len(ids) - pairs), ids[pairs:]):
        slots[2 * k + 1] = pid
    return slots


def validate_slots(slots, active_ids):
    """Return an error message, or None if the draw is usable."""
    placed = [p for p in slots if p is not None]
    if len(placed) < 2:
        return "Place at least two players in the bracket."
    if len(placed) != len(set(placed)):
        return "A player is in the bracket more than once."
    for k in range(0, len(slots), 2):
        if slots[k] is None and slots[k + 1] is None:
            return f"Match {k // 2 + 1} has no players — every first-round match needs at least one."
    missing = set(active_ids) - set(placed)
    if missing:
        return f"{len(missing)} registered player(s) aren't in the bracket. Place them, or withdraw them first."
    return None


def get_slots(conn, tid):
    return [r["player_id"] for r in conn.execute(
        "SELECT player_id FROM bracket_slots WHERE tournament_id = ? ORDER BY position", (tid,)
    )]


def save_slots(conn, tid, slots):
    conn.execute("DELETE FROM bracket_slots WHERE tournament_id = ?", (tid,))
    conn.executemany(
        "INSERT INTO bracket_slots (tournament_id, position, player_id) VALUES (?, ?, ?)",
        [(tid, i, pid) for i, pid in enumerate(slots)],
    )


def tree(conn, tid):
    """The whole bracket as rounds -> nodes.

    Each node: round, slot, p1, p2 (player id or None), winner (id or None),
    state ('done' | 'bye' | 'playing' | 'ready' | 'waiting' | 'empty'), match row.
    """
    slots = get_slots(conn, tid)
    if not slots:
        return []
    matches = {
        (m["round"], m["slot"]): m
        for m in conn.execute(
            "SELECT * FROM matches WHERE tournament_id = ? AND status IN ('pending', 'completed') "
            "AND round IS NOT NULL ORDER BY id",
            (tid,),
        )
    }
    total_rounds = len(slots).bit_length() - 1
    rounds = []
    for r in range(1, total_rounds + 1):
        nodes = []
        count = len(slots) // 2 ** r
        for k in range(count):
            if r == 1:
                sides = [("player", slots[2 * k]) if slots[2 * k] else ("empty", None),
                         ("player", slots[2 * k + 1]) if slots[2 * k + 1] else ("empty", None)]
            else:
                sides = []
                for child in (rounds[-1][2 * k], rounds[-1][2 * k + 1]):
                    if child["winner"]:
                        sides.append(("player", child["winner"]))
                    elif child["state"] == "empty":
                        sides.append(("empty", None))
                    else:
                        sides.append(("tbd", None))
            node = {"round": r, "slot": k, "p1": sides[0][1], "p2": sides[1][1],
                    "winner": None, "match": matches.get((r, k)), "state": "waiting"}
            kinds = (sides[0][0], sides[1][0])
            if kinds == ("empty", "empty"):
                node["state"] = "empty"
            elif "tbd" in kinds:
                node["state"] = "waiting"
            elif "empty" in kinds:
                node["state"] = "bye"
                node["winner"] = node["p1"] or node["p2"]
            elif node["match"] is None:
                node["state"] = "ready"
            elif node["match"]["status"] == "completed":
                node["state"] = "done"
                node["winner"] = node["match"]["winner_id"]
            else:
                node["state"] = "playing"
            nodes.append(node)
        rounds.append(nodes)
    return rounds


def advance(conn, t):
    """Create matches for every bracket slot whose two players are now known.

    A slot where one player has withdrawn is settled immediately as a walkover.
    """
    tid = t["id"]
    active = {r["id"]: r["active"] for r in conn.execute("SELECT id, active FROM players WHERE tournament_id = ?", (tid,))}
    while True:
        changed = False
        for nodes in tree(conn, tid):
            for n in nodes:
                if n["state"] != "ready":
                    continue
                p1_in, p2_in = active.get(n["p1"]), active.get(n["p2"])
                if p1_in and p2_in:
                    conn.execute(
                        "INSERT INTO matches (tournament_id, table_no, player1_id, player2_id, round, slot) "
                        "VALUES (?, 0, ?, ?, ?, ?)",
                        (tid, n["p1"], n["p2"], n["round"], n["slot"]),
                    )
                else:
                    winner = n["p2"] if p2_in and not p1_in else n["p1"]
                    record_walkover(conn, tid, n["p1"], n["p2"], winner, n["round"], n["slot"])
                    changed = True
        if not changed:
            return


def record_walkover(conn, tid, p1, p2, winner, round_no, slot):
    seq = conn.execute(
        "SELECT COALESCE(MAX(completed_seq), 0) + 1 FROM matches WHERE tournament_id = ?", (tid,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO matches (tournament_id, table_no, player1_id, player2_id, round, slot, status, winner_id, "
        "walkover, completed_at, completed_seq) VALUES (?, 0, ?, ?, ?, ?, 'completed', ?, 1, "
        "datetime('now', 'localtime'), ?)",
        (tid, p1, p2, round_no, slot, winner, seq),
    )


def downstream_match(conn, tid, match):
    """The next-round match fed by this match's winner, if it exists yet."""
    return conn.execute(
        "SELECT * FROM matches WHERE tournament_id = ? AND round = ? AND slot = ? "
        "AND status IN ('pending', 'completed')",
        (tid, match["round"] + 1, match["slot"] // 2),
    ).fetchone()


def release_downstream(conn, tid, match):
    """Before changing a result: cancel the next-round match if it hasn't been played.

    Returns an error message if the next round has already been decided.
    """
    nxt = downstream_match(conn, tid, match)
    if nxt is None:
        return None
    if nxt["status"] == "completed":
        return "The next-round match has already been played. Change or reset that result first."
    conn.execute("UPDATE matches SET status = 'cancelled' WHERE id = ?", (nxt["id"],))
    return None


def standings(conn, t, players):
    """Rank by how far each player got: champion, runner-up, then by round reached."""
    rounds = tree(conn, t["id"])
    total = len(rounds)
    reached, out_in = {}, {}
    for nodes in rounds:
        for n in nodes:
            for pid in (n["p1"], n["p2"]):
                if pid:
                    reached[pid] = n["round"]
            if n["winner"] and n["state"] == "done":
                loser = n["p2"] if n["winner"] == n["p1"] else n["p1"]
                out_in[loser] = n["round"]
    champion = rounds[-1][0]["winner"] if rounds else None

    for p in players:
        pid = p["id"]
        if pid not in reached:
            p["bracket_status"], depth = "Not in bracket", -1
        elif pid == champion:
            p["bracket_status"], depth = "Champion", total + 2
        elif pid in out_in:
            depth = out_in[pid]
            p["bracket_status"] = "Runner-up" if depth == total else f"Out in {round_name(depth, total)}"
        else:
            p["bracket_status"] = "Still in" if p["active"] else "Withdrawn"
            depth = total + 1 if p["active"] else reached[pid]
        p["_depth"] = depth
    players.sort(key=lambda p: (-p["_depth"], -p["elo"]))
    return players, rounds, champion
