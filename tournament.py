"""Tournament rules: rating replay, matchmaking, and standings.

Three formats share the same players, results and ratings:
  elo     - open play; the matchmaker pairs whoever has played least (below)
  bracket - single elimination (bracket.py)
  swiss   - "round robin by record": rounds where equal records meet (swiss.py)
"""
from collections import Counter

import bracket
import elo
import swiss

TYPES = {
    "elo": "Elo based",
    "bracket": "Bracket",
    "swiss": "Round robin (by record)",
}

# Bracket and round robin don't ask for a level: everyone is stored at this level and
# starts at the same rating, so a self-reported level can't give anyone a head start.
NO_LEVEL = 1

# A rematch counts like this many extra games when choosing an opponent, so the
# matchmaker strongly prefers fresh pairings but will still allow a rematch
# rather than make someone sit out.
REMATCH_PENALTY = 2


def get_tournament(conn, tid):
    return conn.execute("SELECT * FROM tournaments WHERE id = ?", (tid,)).fetchone()


def uses_levels(t):
    """Only the Elo format asks players for a level (it sets their starting rating)."""
    return t["type"] == "elo"


def next_seq(conn, tid):
    return conn.execute(
        "SELECT COALESCE(MAX(completed_seq), 0) + 1 FROM matches WHERE tournament_id = ?", (tid,)
    ).fetchone()[0]


def recompute_ratings(conn, tid):
    """Replay every completed match in the order results were entered.

    Ratings are always derived from scratch, so editing or deleting an old
    result produces exactly the same numbers as if it had been right first time.
    """
    level_based = uses_levels(get_tournament(conn, tid))
    flat_start = elo.starting_elo(NO_LEVEL)
    players = {
        p["id"]: {"elo": p["initial_elo"] if level_based else flat_start,
                  "games": 0, "wins": 0, "losses": 0, "last": None}
        for p in conn.execute("SELECT id, initial_elo FROM players WHERE tournament_id = ?", (tid,))
    }
    matches = conn.execute(
        "SELECT * FROM matches WHERE tournament_id = ? AND status = 'completed' ORDER BY completed_seq, id",
        (tid,),
    ).fetchall()

    for m in matches:
        p1, p2 = players[m["player1_id"]], players[m["player2_id"]]
        winner, loser = (p1, p2) if m["winner_id"] == m["player1_id"] else (p2, p1)
        if m["walkover"]:
            # Forfeit: goes on the record, but no game was played so ratings don't move.
            conn.execute("UPDATE matches SET elo_change = 0 WHERE id = ?", (m["id"],))
            winner["wins"] += 1
            loser["losses"] += 1
            continue
        change = elo.points_for_win(winner["elo"], loser["elo"])
        conn.execute(
            "UPDATE matches SET p1_elo_before = ?, p2_elo_before = ?, elo_change = ? WHERE id = ?",
            (p1["elo"], p2["elo"], change, m["id"]),
        )
        winner["elo"] += change
        loser["elo"] -= change
        winner["wins"] += 1
        loser["losses"] += 1
        for p in (p1, p2):
            p["games"] += 1
            p["last"] = m["completed_at"]

    for b in conn.execute("SELECT player_id FROM byes WHERE tournament_id = ?", (tid,)):
        players[b["player_id"]]["wins"] += 1

    for pid, p in players.items():
        conn.execute(
            "UPDATE players SET elo = ?, games_played = ?, wins = ?, losses = ?, last_played_at = ? WHERE id = ?",
            (p["elo"], p["games"], p["wins"], p["losses"], p["last"], pid),
        )


def generate_matches(conn, tid):
    """Create whatever matches the format calls for and put them on free tables.

    Returns the ids of matches that are newly on a table.
    """
    t = get_tournament(conn, tid)
    if t is None or t["status"] != "in_progress":
        return []
    if t["type"] == "elo":
        return _generate_elo(conn, t)
    before = _on_table(conn, tid)
    if t["type"] == "bracket":
        bracket.advance(conn, t)
    else:
        swiss.advance(conn, t)
        recompute_ratings(conn, tid)  # a new round may have handed out a bye, which counts as a win
    assign_tables(conn, tid)
    return sorted(_on_table(conn, tid) - before)


def _on_table(conn, tid):
    return {r[0] for r in conn.execute(
        "SELECT id FROM matches WHERE tournament_id = ? AND status = 'pending' AND table_no > 0", (tid,)
    )}


def assign_tables(conn, tid):
    """Move queued matches (table 0) onto free tables, earliest round first."""
    t = get_tournament(conn, tid)
    used = {r[0] for r in conn.execute(
        "SELECT table_no FROM matches WHERE tournament_id = ? AND status = 'pending' AND table_no > 0", (tid,)
    )}
    free = [n for n in range(1, t["num_tables"] + 1) if n not in used]
    queued = conn.execute(
        "SELECT id FROM matches WHERE tournament_id = ? AND status = 'pending' AND table_no = 0 "
        "ORDER BY round, slot, id",
        (tid,),
    ).fetchall()
    for table, m in zip(free, queued):
        conn.execute("UPDATE matches SET table_no = ? WHERE id = ?", (table, m["id"]))


def _generate_elo(conn, t):
    """Fill every open table with a new matchup.

    Priority for who plays next:
      1. fewest games played (so everyone reaches the minimum)
      2. longest time since their last game
      3. order they joined
    Their opponent is the available player with the lowest
    games_played + REMATCH_PENALTY * times_already_played_each_other,
    avoiding an immediate back-to-back rematch, then closest rating.
    """
    tid = t["id"]
    pending = conn.execute(
        "SELECT * FROM matches WHERE tournament_id = ? AND status = 'pending'", (tid,)
    ).fetchall()
    busy = {m["player1_id"] for m in pending} | {m["player2_id"] for m in pending}
    used_tables = {m["table_no"] for m in pending}
    free_tables = [n for n in range(1, t["num_tables"] + 1) if n not in used_tables]

    available = [
        dict(p)
        for p in conn.execute("SELECT * FROM players WHERE tournament_id = ? AND active = 1", (tid,))
        if p["id"] not in busy
    ]

    head_to_head = Counter()
    last_opponent = {}
    for m in conn.execute(
        "SELECT player1_id, player2_id FROM matches WHERE tournament_id = ? AND status = 'completed' "
        "ORDER BY completed_seq, id",
        (tid,),
    ):
        a, b = m["player1_id"], m["player2_id"]
        head_to_head[frozenset((a, b))] += 1
        last_opponent[a], last_opponent[b] = b, a

    created = []
    while free_tables and len(available) >= 2:
        available.sort(key=lambda p: (p["games_played"], p["last_played_at"] or "", p["joined_at"], p["id"]))
        p1 = available[0]

        def opponent_cost(o):
            return (
                o["games_played"] + REMATCH_PENALTY * head_to_head[frozenset((p1["id"], o["id"]))],
                1 if last_opponent.get(p1["id"]) == o["id"] else 0,
                abs(o["elo"] - p1["elo"]),
                o["last_played_at"] or "",
            )

        p2 = min(available[1:], key=opponent_cost)
        table = free_tables.pop(0)
        cur = conn.execute(
            "INSERT INTO matches (tournament_id, table_no, player1_id, player2_id) VALUES (?, ?, ?, ?)",
            (tid, table, p1["id"], p2["id"]),
        )
        created.append(cur.lastrowid)
        available = [p for p in available if p["id"] not in (p1["id"], p2["id"])]
    return created


def start_bracket(conn, tid, slots):
    bracket.save_slots(conn, tid, slots)
    conn.execute("UPDATE tournaments SET status = 'in_progress' WHERE id = ?", (tid,))
    return generate_matches(conn, tid)


def reset_bracket(conn, tid):
    """Back to setup, keeping the draw. Only allowed before any game is played."""
    played = conn.execute(
        "SELECT COUNT(*) FROM matches WHERE tournament_id = ? AND status = 'completed' AND walkover = 0", (tid,)
    ).fetchone()[0]
    if played:
        return False
    conn.execute("DELETE FROM matches WHERE tournament_id = ?", (tid,))
    conn.execute("UPDATE tournaments SET status = 'registration' WHERE id = ?", (tid,))
    recompute_ratings(conn, tid)
    return True


def record_result(conn, tid, match_id, winner_id, p1_score=None, p2_score=None):
    m = conn.execute(
        "SELECT * FROM matches WHERE id = ? AND tournament_id = ?", (match_id, tid)
    ).fetchone()
    if m is None or m["status"] != "pending" or winner_id not in (m["player1_id"], m["player2_id"]):
        return False
    conn.execute(
        "UPDATE matches SET status = 'completed', winner_id = ?, p1_score = ?, p2_score = ?, "
        "completed_at = datetime('now', 'localtime'), completed_seq = ? WHERE id = ?",
        (winner_id, p1_score, p2_score, next_seq(conn, tid), match_id),
    )
    recompute_ratings(conn, tid)
    generate_matches(conn, tid)
    return True


def edit_result(conn, tid, match_id, winner_id, p1_score=None, p2_score=None):
    """Change a recorded result. Returns an error message, or None on success."""
    t = get_tournament(conn, tid)
    m = conn.execute(
        "SELECT * FROM matches WHERE id = ? AND tournament_id = ?", (match_id, tid)
    ).fetchone()
    if m is None or m["status"] != "completed" or winner_id not in (m["player1_id"], m["player2_id"]):
        return "Could not update that result."
    if t["type"] == "bracket" and winner_id != m["winner_id"]:
        error = bracket.release_downstream(conn, tid, m)
        if error:
            return error
    conn.execute(
        "UPDATE matches SET winner_id = ?, p1_score = ?, p2_score = ?, walkover = 0 WHERE id = ?",
        (winner_id, p1_score, p2_score, match_id),
    )
    recompute_ratings(conn, tid)
    generate_matches(conn, tid)
    return None


def delete_result(conn, tid, match_id):
    """Remove a result. Returns an error message, or None on success.

    Elo: the game is simply dropped. Bracket / round robin: the pairing is part
    of the draw, so the match goes back to unplayed instead.
    """
    t = get_tournament(conn, tid)
    m = conn.execute(
        "SELECT * FROM matches WHERE id = ? AND tournament_id = ? AND status = 'completed'", (match_id, tid)
    ).fetchone()
    if m is None:
        return "That result no longer exists."
    if t["type"] == "elo":
        conn.execute("UPDATE matches SET status = 'cancelled' WHERE id = ?", (match_id,))
    else:
        if t["type"] == "bracket":
            error = bracket.release_downstream(conn, tid, m)
            if error:
                return error
        elif m["round"] < t["current_round"]:
            return f"Round {m['round'] + 1} has already been paired — edit this result instead of resetting it."
        conn.execute(
            "UPDATE matches SET status = 'pending', table_no = 0, winner_id = NULL, p1_score = NULL, "
            "p2_score = NULL, walkover = 0, completed_at = NULL, completed_seq = NULL, elo_change = NULL "
            "WHERE id = ?",
            (match_id,),
        )
    recompute_ratings(conn, tid)
    generate_matches(conn, tid)
    return None


def cancel_match(conn, tid, match_id):
    """Elo only: drop an unplayed pairing. Bracket and round-robin pairings are fixed."""
    if get_tournament(conn, tid)["type"] != "elo":
        return False
    conn.execute(
        "UPDATE matches SET status = 'cancelled' WHERE id = ? AND tournament_id = ? AND status = 'pending'",
        (match_id, tid),
    )
    return True


def set_player_active(conn, tid, pid, active):
    conn.execute(
        "UPDATE players SET active = ? WHERE id = ? AND tournament_id = ?", (1 if active else 0, pid, tid)
    )
    if not active:
        if get_tournament(conn, tid)["type"] == "elo":
            # Free up their table; the opponent goes back into the pool.
            conn.execute(
                "UPDATE matches SET status = 'cancelled' WHERE tournament_id = ? AND status = 'pending' "
                "AND (player1_id = ? OR player2_id = ?)",
                (tid, pid, pid),
            )
        else:
            # Fixed pairing: their opponent wins by forfeit.
            for m in conn.execute(
                "SELECT * FROM matches WHERE tournament_id = ? AND status = 'pending' "
                "AND (player1_id = ? OR player2_id = ?)",
                (tid, pid, pid),
            ).fetchall():
                winner = m["player2_id"] if m["player1_id"] == pid else m["player1_id"]
                conn.execute(
                    "UPDATE matches SET status = 'completed', winner_id = ?, walkover = 1, "
                    "completed_at = datetime('now', 'localtime'), completed_seq = ? WHERE id = ?",
                    (winner, next_seq(conn, tid), m["id"]),
                )
            recompute_ratings(conn, tid)
    generate_matches(conn, tid)


def add_player(conn, tid, first, last, level=None):
    if level is None or not uses_levels(get_tournament(conn, tid)):
        level = NO_LEVEL
    start = elo.starting_elo(level)
    cur = conn.execute(
        "INSERT INTO players (tournament_id, first_name, last_name, level, initial_elo, elo) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tid, first, last, level, start, start),
    )
    generate_matches(conn, tid)
    return cur.lastrowid


def update_player(conn, tid, pid, first, last, level=None):
    if level is None or not uses_levels(get_tournament(conn, tid)):
        level = conn.execute("SELECT level FROM players WHERE id = ?", (pid,)).fetchone()[0]
    conn.execute(
        "UPDATE players SET first_name = ?, last_name = ?, level = ?, initial_elo = ? "
        "WHERE id = ? AND tournament_id = ?",
        (first, last, level, elo.starting_elo(level), pid, tid),
    )
    recompute_ratings(conn, tid)


def standings(conn, tid):
    """All players ranked for the tournament's format.

    Every row gets rank, name, games_needed and seed (position among those who
    advance, or None). Bracket rows also get bracket_status; round-robin rows
    get opp_wins (strength of schedule).
    """
    t = get_tournament(conn, tid)
    rows = [
        dict(p)
        for p in conn.execute(
            "SELECT * FROM players WHERE tournament_id = ? "
            "ORDER BY elo DESC, wins DESC, games_played DESC, last_name, first_name",
            (tid,),
        )
    ]
    if t["type"] == "swiss":
        rows = swiss.standings(conn, t, rows)
    elif t["type"] == "bracket":
        rows = bracket.standings(conn, t, rows)[0]

    seed = 0
    for rank, p in enumerate(rows, start=1):
        p["rank"] = rank
        p["name"] = f"{p['first_name']} {p['last_name']}"
        p["start_elo"] = p["initial_elo"] if uses_levels(t) else elo.starting_elo(NO_LEVEL)
        p["games_needed"] = max(0, t["min_games"] - p["games_played"]) if t["type"] == "elo" else 0
        p["seed"] = None
        if t["type"] != "bracket" and p["active"] and p["games_needed"] == 0 and seed < t["qualify_count"]:
            seed += 1
            p["seed"] = seed
    return rows


def bracket_view(conn, tid):
    """Bracket rounds with player names filled in, for display."""
    rounds = bracket.tree(conn, tid)
    names = {p["id"]: f"{p['first_name']} {p['last_name']}"
             for p in conn.execute("SELECT * FROM players WHERE tournament_id = ?", (tid,))}
    total = len(rounds)
    view = []
    for nodes in rounds:
        matches = []
        for n in nodes:
            m = n["match"]
            matches.append({
                "p1": names.get(n["p1"]),
                "p2": names.get(n["p2"]),
                "winner": names.get(n["winner"]),
                "state": n["state"],
                "table": m["table_no"] if m and n["state"] == "playing" and m["table_no"] else None,
                "score": (f"{m['p1_score']}–{m['p2_score']}"
                          if m and m["p1_score"] is not None and m["p2_score"] is not None else None),
                "walkover": bool(m and m["walkover"]),
            })
        view.append({"name": bracket.round_name(nodes[0]["round"], total), "matches": matches})
    champion = names.get(rounds[-1][0]["winner"]) if rounds else None
    return {"rounds": view, "champion": champion}
