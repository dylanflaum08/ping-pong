"""Round robin by record (Swiss system).

Play happens in rounds. At the start of each round every active player is
paired with someone on the same record (1-0 plays 1-0, 0-1 plays 0-1, ...),
never with someone they've already played if it can be avoided. When a record
group has an odd number of players, one of them plays someone from the next
group down. With an odd number of players overall, the lowest-ranked player who
hasn't had one yet gets a bye, which counts as a win.
"""
import random
from collections import Counter

SEARCH_BUDGET = 20000  # backtracking steps before falling back to allowing rematches


def opponents(conn, tid):
    """{player_id: Counter(opponent_id -> games)} from completed matches."""
    played = {}
    for m in conn.execute(
        "SELECT player1_id, player2_id FROM matches WHERE tournament_id = ? AND status = 'completed'", (tid,)
    ):
        a, b = m["player1_id"], m["player2_id"]
        played.setdefault(a, Counter())[b] += 1
        played.setdefault(b, Counter())[a] += 1
    return played


def pair_players(order, played):
    """Pair a ranked list top-down, avoiding rematches; returns [(a, b), ...]."""
    steps = 0

    def search(remaining):
        nonlocal steps
        if not remaining:
            return []
        steps += 1
        if steps > SEARCH_BUDGET:
            return None
        a, rest = remaining[0], remaining[1:]
        for i, b in enumerate(rest):
            if played.get(a, {}).get(b):
                continue
            tail = search(rest[:i] + rest[i + 1:])
            if tail is not None:
                return [(a, b)] + tail
        return None

    pairs = search(list(order))
    if pairs is None:  # everyone has played everyone nearby: allow rematches
        pairs = [(order[i], order[i + 1]) for i in range(0, len(order) - 1, 2)]
    return pairs


def advance(conn, t, rng=random):
    """Start the next round once every game in the current round is finished."""
    tid = t["id"]
    if t["current_round"] >= t["rounds"]:
        return
    unfinished = conn.execute(
        "SELECT COUNT(*) FROM matches WHERE tournament_id = ? AND status = 'pending'", (tid,)
    ).fetchone()[0]
    if unfinished:
        return

    players = [dict(p) for p in conn.execute("SELECT * FROM players WHERE tournament_id = ? AND active = 1", (tid,))]
    if len(players) < 2:
        return
    round_no = t["current_round"] + 1

    # Rank: most wins first; random order within a record so pairings vary.
    for p in players:
        p["_tiebreak"] = rng.random()
    players.sort(key=lambda p: (-p["wins"], p["losses"], p["_tiebreak"]))
    order = [p["id"] for p in players]

    if len(order) % 2:
        had_bye = {r["player_id"] for r in conn.execute("SELECT player_id FROM byes WHERE tournament_id = ?", (tid,))}
        bye = next((pid for pid in reversed(order) if pid not in had_bye), order[-1])
        order.remove(bye)
        conn.execute("INSERT INTO byes (tournament_id, round, player_id) VALUES (?, ?, ?)", (tid, round_no, bye))

    for slot, (a, b) in enumerate(pair_players(order, opponents(conn, tid))):
        conn.execute(
            "INSERT INTO matches (tournament_id, table_no, player1_id, player2_id, round, slot) VALUES (?, 0, ?, ?, ?, ?)",
            (tid, a, b, round_no, slot),
        )
    conn.execute("UPDATE tournaments SET current_round = ? WHERE id = ?", (round_no, tid))


def byes(conn, tid):
    return {r["round"]: r["player_id"] for r in conn.execute("SELECT * FROM byes WHERE tournament_id = ?", (tid,))}


def standings(conn, t, players):
    """Rank by wins, then strength of schedule (opponents' total wins), then rating."""
    wins = {p["id"]: p["wins"] for p in players}
    played = opponents(conn, t["id"])
    for p in players:
        p["opp_wins"] = sum(wins.get(o, 0) * n for o, n in played.get(p["id"], {}).items())
    players.sort(key=lambda p: (not p["active"], -p["wins"], p["losses"], -p["opp_wins"], -p["elo"]))
    return players
