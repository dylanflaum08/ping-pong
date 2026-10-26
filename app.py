import io
import os
import secrets
import socket
import sqlite3
from functools import wraps

import segno
from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, session, url_for

import bracket
import db
import elo
import tournament as tm

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", 5000))

app = Flask(__name__)


def _secret_key():
    path = os.path.join(BASE_DIR, "secret_key.txt")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app.secret_key = _secret_key()
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30


@app.context_processor
def inject_rating_rules():
    return {"elo": elo, "TYPES": tm.TYPES}


# --------------------------------------------------------------------------- helpers

def lan_ip():
    """Best guess at this machine's address on the local network."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packets are sent for UDP connect
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def public_base_url():
    override = os.environ.get("PUBLIC_URL")
    if override:
        return override.rstrip("/")
    return f"http://{lan_ip()}:{PORT}"


def join_url(tid):
    return f"{public_base_url()}{url_for('join', tid=tid)}"


def clean_name(value):
    value = " ".join((value or "").split())[:40]
    return value[:1].upper() + value[1:]


def parse_score(value):
    try:
        score = int(value)
        return score if 0 <= score <= 99 else None
    except (TypeError, ValueError):
        return None


def scores_from_form(m_row, winner_id):
    """Optional scores from the form; returns (p1, p2, error)."""
    p1, p2 = parse_score(request.form.get("p1_score")), parse_score(request.form.get("p2_score"))
    if m_row is not None and p1 is not None and p2 is not None:
        winner_score, loser_score = (p1, p2) if winner_id == m_row["player1_id"] else (p2, p1)
        if winner_score <= loser_score:
            return p1, p2, "The score doesn't match the winner you picked — please check and try again."
    return p1, p2, None


def parse_int(value, default, lo, hi):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def load_tournament(conn, tid):
    t = tm.get_tournament(conn, tid)
    if t is None:
        abort(404)
    return t


def is_admin(t):
    return not t["admin_pin"] or t["id"] in session.get("admin_for", [])


def grant_admin(tid):
    session.permanent = True
    session["admin_for"] = list(set(session.get("admin_for", [])) | {tid})


def admin_required(view):
    @wraps(view)
    def wrapper(tid, *args, **kwargs):
        with db.connect() as conn:
            t = load_tournament(conn, tid)
        if not is_admin(t):
            return redirect(url_for("login", tid=tid, next=request.path))
        return view(tid, *args, **kwargs)
    return wrapper


def signup_open(t):
    """Bracket signups close once the draw is made; other formats take late arrivals."""
    if t["status"] == "completed":
        return False
    return t["type"] != "bracket" or t["status"] == "registration"


def round_labeler(conn, t):
    """Function giving a display name for a match's round ("Semifinals", "Round 2")."""
    if t["type"] == "bracket":
        total = max(1, len(bracket.get_slots(conn, t["id"])).bit_length() - 1)
        return lambda r: bracket.round_name(r, total) if r else ""
    if t["type"] == "swiss":
        return lambda r: f"Round {r}" if r else ""
    return lambda r: ""


def player_map(conn, tid):
    return {
        p["id"]: dict(p, name=f"{p['first_name']} {p['last_name']}")
        for p in conn.execute("SELECT * FROM players WHERE tournament_id = ?", (tid,))
    }


def match_views(conn, tid, status, order="table_no = 0, table_no, round, slot, id", limit=None):
    players = player_map(conn, tid)
    sql = f"SELECT * FROM matches WHERE tournament_id = ? AND status = ? ORDER BY {order}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = []
    for m in conn.execute(sql, (tid, status)):
        rows.append(dict(m, p1=players[m["player1_id"]], p2=players[m["player2_id"]]))
    return rows


# --------------------------------------------------------------------------- home

@app.route("/")
def home():
    with db.connect() as conn:
        tournaments = conn.execute(
            "SELECT t.*, (SELECT COUNT(*) FROM players p WHERE p.tournament_id = t.id) AS player_count, "
            "(SELECT COUNT(*) FROM matches m WHERE m.tournament_id = t.id AND m.status = 'completed') AS match_count "
            "FROM tournaments t ORDER BY t.id DESC"
        ).fetchall()
    return render_template("home.html", tournaments=tournaments)


@app.post("/tournaments")
def create_tournament():
    name = " ".join(request.form.get("name", "").split())[:60] or "Ping Pong Tournament"
    tables = parse_int(request.form.get("num_tables"), 1, 1, 20)
    pin = request.form.get("admin_pin", "").strip()[:20] or None
    kind = request.form.get("type") if request.form.get("type") in tm.TYPES else "elo"
    rounds = parse_int(request.form.get("rounds"), 3, 1, 20)
    with db.write_lock, db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO tournaments (name, num_tables, admin_pin, type, rounds) VALUES (?, ?, ?, ?, ?)",
            (name, tables, pin, kind, rounds),
        )
        tid = cur.lastrowid
    grant_admin(tid)
    return redirect(url_for("admin", tid=tid))


# --------------------------------------------------------------------------- public pages

@app.route("/t/<int:tid>/display")
def display(tid):
    with db.connect() as conn:
        t = load_tournament(conn, tid)
    return render_template("display.html", t=t, join_url=join_url(tid))


@app.route("/t/<int:tid>/api/state")
def api_state(tid):
    with db.connect() as conn:
        t = load_tournament(conn, tid)
        standings = tm.standings(conn, tid)
        pending = match_views(conn, tid, "pending")
        recent = match_views(conn, tid, "completed", order="completed_seq DESC", limit=6)
        bracket_data = tm.bracket_view(conn, tid) if t["type"] == "bracket" else None
        bye_row = conn.execute(
            "SELECT player_id FROM byes WHERE tournament_id = ? AND round = ?", (tid, t["current_round"])
        ).fetchone()
        label = round_labeler(conn, t)
    current = [m for m in pending if m["table_no"]]
    queued = [m for m in pending if not m["table_no"]]
    busy = {m["player1_id"] for m in pending} | {m["player2_id"] for m in pending}
    names = {p["id"]: p["name"] for p in standings}

    def pv(p):
        return {"id": p["id"], "name": p["name"], "elo": p["elo"], "level": p["level"], "games": p["games_played"],
                "record": f"{p['wins']}–{p['losses']}"}

    waiting = sorted(
        (p for p in standings if p["active"] and p["id"] not in busy),
        key=lambda p: (p["games_played"], p["last_played_at"] or "", p["joined_at"]),
    )
    leaders = standings if t["type"] == "bracket" else [p for p in standings if p["active"]]
    return jsonify(
        tournament={"name": t["name"], "status": t["status"], "type": t["type"], "type_label": tm.TYPES[t["type"]],
                    "min_games": t["min_games"], "qualify_count": t["qualify_count"],
                    "num_tables": t["num_tables"], "rounds": t["rounds"], "current_round": t["current_round"]},
        signup_open=signup_open(t),
        current=[{"table": m["table_no"], "round": label(m["round"]), "p1": pv(m["p1"]), "p2": pv(m["p2"])}
                 for m in current],
        queued=[{"round": label(m["round"]), "p1": pv(m["p1"]), "p2": pv(m["p2"])} for m in queued],
        bye=names.get(bye_row[0]) if bye_row else None,
        bracket=bracket_data,
        recent=[{
            "table": m["table_no"],
            "winner": m["p1"]["name"] if m["winner_id"] == m["player1_id"] else m["p2"]["name"],
            "loser": m["p2"]["name"] if m["winner_id"] == m["player1_id"] else m["p1"]["name"],
            "score": (f"{max(m['p1_score'], m['p2_score'])}–{min(m['p1_score'], m['p2_score'])}"
                      if m["p1_score"] is not None and m["p2_score"] is not None else None),
            "change": m["elo_change"],
            "walkover": bool(m["walkover"]),
        } for m in recent],
        waiting=[pv(p) for p in waiting],
        leaders=[{"seed": p["seed"], "rank": p["rank"], "name": p["name"], "elo": p["elo"],
                  "wins": p["wins"], "losses": p["losses"], "games_needed": p["games_needed"],
                  "active": p["active"], "bracket_status": p.get("bracket_status"),
                  "opp_wins": p.get("opp_wins")} for p in leaders][:12],
        player_count=sum(1 for p in standings if p["active"]),
        all_min_met=bool(standings) and all(p["games_needed"] == 0 for p in standings if p["active"]),
    )


@app.route("/t/<int:tid>/qr.svg")
def qr(tid):
    with db.connect() as conn:
        load_tournament(conn, tid)
    buf = io.BytesIO()
    segno.make(join_url(tid), error="m").save(buf, kind="svg", scale=10, border=2, dark="#0b1220", light="#ffffff")
    return Response(buf.getvalue(), mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.route("/t/<int:tid>/join", methods=["GET", "POST"])
def join(tid):
    with db.connect() as conn:
        t = load_tournament(conn, tid)
    if not signup_open(t):
        return render_template("join.html", t=t, closed=True)

    error = None
    form = {"first_name": "", "last_name": "", "level": ""}
    if request.method == "POST":
        form = {k: request.form.get(k, "") for k in form}
        first, last = clean_name(form["first_name"]), clean_name(form["last_name"])
        level = parse_int(form["level"], 0, 0, 4) if tm.uses_levels(t) else tm.NO_LEVEL
        if not first or not last:
            error = "Please enter your first and last name."
        elif level not in (1, 2, 3, 4):
            error = "Please pick your level."
        else:
            try:
                with db.write_lock, db.connect() as conn:
                    pid = tm.add_player(conn, tid, first, last, level)
            except sqlite3.IntegrityError:
                error = f"{first} {last} is already signed up for this tournament."
            else:
                session.permanent = True
                session[f"player_{tid}"] = pid
                return redirect(url_for("me", tid=tid))
    return render_template("join.html", t=t, error=error, form=form, closed=False)


@app.route("/t/<int:tid>/me")
def me(tid):
    pid = session.get(f"player_{tid}")
    with db.connect() as conn:
        t = load_tournament(conn, tid)
        if not pid:
            return redirect(url_for("join", tid=tid))
        standings = tm.standings(conn, tid)
        player = next((p for p in standings if p["id"] == pid), None)
        if player is None:
            session.pop(f"player_{tid}", None)
            return redirect(url_for("join", tid=tid))
        current = next(
            (m for m in match_views(conn, tid, "pending") if pid in (m["player1_id"], m["player2_id"])), None
        )
        history = [
            m for m in match_views(conn, tid, "completed", order="completed_seq DESC")
            if pid in (m["player1_id"], m["player2_id"])
        ]
        round_label = round_labeler(conn, t)
        has_bye = conn.execute(
            "SELECT 1 FROM byes WHERE tournament_id = ? AND round = ? AND player_id = ?",
            (tid, t["current_round"], pid),
        ).fetchone() is not None
    return render_template("me.html", t=t, player=player, current=current, history=history,
                           round_label=round_label, has_bye=has_bye)


@app.post("/t/<int:tid>/me/forget")
def forget_me(tid):
    session.pop(f"player_{tid}", None)
    return redirect(url_for("join", tid=tid))


@app.route("/t/<int:tid>/leaderboard")
def leaderboard(tid):
    with db.connect() as conn:
        t = load_tournament(conn, tid)
        standings = tm.standings(conn, tid)
        bracket_data = tm.bracket_view(conn, tid) if t["type"] == "bracket" else None
    return render_template("leaderboard.html", t=t, standings=standings, admin=is_admin(t), bracket=bracket_data)


# --------------------------------------------------------------------------- admin

@app.route("/t/<int:tid>/login", methods=["GET", "POST"])
def login(tid):
    with db.connect() as conn:
        t = load_tournament(conn, tid)
    error = None
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("pin", ""), t["admin_pin"] or ""):
            grant_admin(tid)
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("admin", tid=tid))
        error = "Incorrect PIN."
    return render_template("login.html", t=t, error=error)


@app.route("/t/<int:tid>/admin")
@admin_required
def admin(tid):
    with db.connect() as conn:
        t = load_tournament(conn, tid)
        pending = match_views(conn, tid, "pending")
        completed = match_views(conn, tid, "completed", order="completed_seq DESC")
        standings = tm.standings(conn, tid)
        round_label = round_labeler(conn, t)
        bye_row = conn.execute(
            "SELECT player_id FROM byes WHERE tournament_id = ? AND round = ?", (tid, t["current_round"])
        ).fetchone()
    bye = next((p["name"] for p in standings if bye_row and p["id"] == bye_row[0]), None)
    players = sorted(standings, key=lambda p: (not p["active"], p["last_name"].lower(), p["first_name"].lower()))
    busy = {m["player1_id"] for m in pending} | {m["player2_id"] for m in pending}
    open_tables = t["num_tables"] - sum(1 for m in pending if m["table_no"])
    idle = sum(1 for p in standings if p["active"] and p["id"] not in busy)
    can_reset_bracket = (t["type"] == "bracket" and t["status"] == "in_progress"
                         and not any(not m["walkover"] for m in completed))
    return render_template(
        "admin.html", t=t, pending=pending, completed=completed, players=players,
        open_tables=open_tables, idle=idle, join_url=join_url(tid), can_reset_bracket=can_reset_bracket,
        round_label=round_label, bye=bye, standings=standings,
    )


@app.route("/t/<int:tid>/admin/bracket")
@admin_required
def bracket_setup(tid):
    with db.connect() as conn:
        t = load_tournament(conn, tid)
        if t["type"] != "bracket" or t["status"] != "registration":
            return redirect(url_for("admin", tid=tid))
        players = [dict(p, name=f"{p['first_name']} {p['last_name']}") for p in conn.execute(
            "SELECT * FROM players WHERE tournament_id = ? AND active = 1 ORDER BY first_name, last_name", (tid,)
        )]
        saved = bracket.get_slots(conn, tid)
    size = bracket.bracket_size(max(len(players), 2))
    valid = {p["id"] for p in players}
    slots = [pid if pid in valid else None for pid in saved[:size]] + [None] * max(0, size - len(saved))
    return render_template("bracket_setup.html", t=t, players=players, slots=slots, size=size,
                           round_name=bracket.round_name(1, size.bit_length() - 1))


def admin_post(rule):
    """Register a POST-only admin endpoint that runs inside the write lock.

    The wrapped function receives an open connection; unless it returns its own
    response, the user is sent back to the admin page.
    """
    def deco(fn):
        @wraps(fn)
        def wrapper(tid, **kwargs):
            with db.write_lock, db.connect() as conn:
                load_tournament(conn, tid)
                response = fn(conn, tid, **kwargs)
            return response or redirect(url_for("admin", tid=tid))
        app.add_url_rule(f"/t/<int:tid>/admin/{rule}", endpoint=f"admin_{fn.__name__}",
                         view_func=admin_required(wrapper), methods=["POST"])
        return fn
    return deco


@admin_post("begin")
def begin(conn, tid):
    if tm.get_tournament(conn, tid)["type"] == "bracket":
        return redirect(url_for("bracket_setup", tid=tid))
    conn.execute("UPDATE tournaments SET status = 'in_progress' WHERE id = ?", (tid,))
    created = tm.generate_matches(conn, tid)
    flash(f"Play started — {len(created)} match(es) created." if created else "Play started. Waiting for players.")


@admin_post("finish")
def finish(conn, tid):
    conn.execute("UPDATE tournaments SET status = 'completed' WHERE id = ?", (tid,))
    flash("Tournament ended. No new matchups will be created; you can still enter results for games in progress.")


@admin_post("reopen")
def reopen(conn, tid):
    conn.execute("UPDATE tournaments SET status = 'in_progress' WHERE id = ?", (tid,))
    tm.generate_matches(conn, tid)
    flash("Tournament reopened.")


@admin_post("settings")
def settings(conn, tid):
    t = tm.get_tournament(conn, tid)
    name = " ".join(request.form.get("name", "").split())[:60] or t["name"]
    tables = parse_int(request.form.get("num_tables"), t["num_tables"], 1, 20)
    min_games = parse_int(request.form.get("min_games"), t["min_games"], 0, 50)
    qualify = parse_int(request.form.get("qualify_count"), t["qualify_count"], 1, 128)
    rounds = parse_int(request.form.get("rounds"), t["rounds"], max(1, t["current_round"]), 20)
    pin = request.form.get("admin_pin", "").strip()[:20] or None
    kind = t["type"]
    if t["status"] == "registration" and request.form.get("type") in tm.TYPES:
        kind = request.form.get("type")  # the format can only change before play starts
    conn.execute(
        "UPDATE tournaments SET name = ?, num_tables = ?, min_games = ?, qualify_count = ?, admin_pin = ?, "
        "rounds = ?, type = ? WHERE id = ?",
        (name, tables, min_games, qualify, pin, rounds, kind, tid),
    )
    if tables < t["num_tables"]:
        # Tables were removed: elo pairings on them are dropped; fixed pairings go back in the queue.
        if kind == "elo":
            conn.execute(
                "UPDATE matches SET status = 'cancelled' WHERE tournament_id = ? AND status = 'pending' "
                "AND table_no > ?",
                (tid, tables),
            )
        else:
            conn.execute(
                "UPDATE matches SET table_no = 0 WHERE tournament_id = ? AND status = 'pending' AND table_no > ?",
                (tid, tables),
            )
    tm.recompute_ratings(conn, tid)  # the format decides whether levels set starting ratings
    tm.generate_matches(conn, tid)
    flash("Settings saved.")
    has_players = conn.execute("SELECT 1 FROM players WHERE tournament_id = ?", (tid,)).fetchone()
    if kind == "elo" and t["type"] != "elo" and has_players:
        flash("Players who signed up without a level are set to level 1 — adjust them in the Players list.")


@admin_post("generate")
def generate(conn, tid):
    created = tm.generate_matches(conn, tid)
    flash(f"Created {len(created)} new match(es)." if created
          else "No new matches — tables are full or there aren't enough free players.")


@admin_post("result/<int:mid>")
def result(conn, tid, mid):
    winner = parse_int(request.form.get("winner"), 0, 0, 10**9)
    m = conn.execute("SELECT * FROM matches WHERE id = ?", (mid,)).fetchone()
    p1, p2, error = scores_from_form(m, winner)
    if error:
        flash(error)
        return
    ok = tm.record_result(conn, tid, mid, winner, p1, p2)
    flash("Result saved." if ok else "That match was already recorded or no longer exists.")


@admin_post("edit/<int:mid>")
def edit(conn, tid, mid):
    winner = parse_int(request.form.get("winner"), 0, 0, 10**9)
    m = conn.execute("SELECT * FROM matches WHERE id = ?", (mid,)).fetchone()
    p1, p2, error = scores_from_form(m, winner)
    if error:
        flash(error)
        return
    error = tm.edit_result(conn, tid, mid, winner, p1, p2)
    flash(error or "Result updated and ratings recalculated.")


@admin_post("delete/<int:mid>")
def delete(conn, tid, mid):
    error = tm.delete_result(conn, tid, mid)
    if error:
        flash(error)
    elif tm.get_tournament(conn, tid)["type"] == "elo":
        flash("Result deleted and ratings recalculated.")
    else:
        flash("Result cleared — the match is back in the queue to be replayed.")


@admin_post("cancel/<int:mid>")
def cancel(conn, tid, mid):
    if tm.cancel_match(conn, tid, mid):
        flash("Match cancelled. Use “Fill open tables” to create a new pairing.")
    else:
        flash("Bracket and round-robin pairings can't be cancelled — withdraw a player to forfeit instead.")


@admin_post("add-player")
def add_player(conn, tid):
    first, last = clean_name(request.form.get("first_name")), clean_name(request.form.get("last_name"))
    level = parse_int(request.form.get("level"), None, 1, 4)
    if not first or not last:
        flash("First and last name are required.")
        return
    try:
        tm.add_player(conn, tid, first, last, level)
        flash(f"Added {first} {last}.")
    except sqlite3.IntegrityError:
        flash(f"{first} {last} is already registered.")


@admin_post("update-player/<int:pid>")
def update_player(conn, tid, pid):
    first, last = clean_name(request.form.get("first_name")), clean_name(request.form.get("last_name"))
    level = parse_int(request.form.get("level"), None, 1, 4)
    if not first or not last:
        flash("First and last name are required.")
        return
    try:
        tm.update_player(conn, tid, pid, first, last, level)
        flash(f"Updated {first} {last}.")
    except sqlite3.IntegrityError:
        flash(f"Another player is already named {first} {last}.")


@admin_post("toggle-player/<int:pid>")
def toggle_player(conn, tid, pid):
    p = conn.execute("SELECT * FROM players WHERE id = ? AND tournament_id = ?", (pid, tid)).fetchone()
    if p:
        tm.set_player_active(conn, tid, pid, not p["active"])
        flash(f"{p['first_name']} {p['last_name']} {'removed from' if p['active'] else 'returned to'} the rotation.")


@admin_post("bracket-randomize")
def bracket_randomize(conn, tid):
    ids = [r[0] for r in conn.execute("SELECT id FROM players WHERE tournament_id = ? AND active = 1", (tid,))]
    if len(ids) < 2:
        flash("You need at least two players for a bracket.")
    else:
        bracket.save_slots(conn, tid, bracket.random_slots(ids))
        flash("Bracket randomized. Swap any slots you like, then start the bracket.")
    return redirect(url_for("bracket_setup", tid=tid))


@admin_post("bracket-clear")
def bracket_clear(conn, tid):
    bracket.save_slots(conn, tid, [])
    return redirect(url_for("bracket_setup", tid=tid))


@admin_post("bracket-start")
def bracket_start(conn, tid):
    t = tm.get_tournament(conn, tid)
    if t["type"] != "bracket" or t["status"] != "registration":
        return None
    active = [r[0] for r in conn.execute("SELECT id FROM players WHERE tournament_id = ? AND active = 1", (tid,))]
    size = parse_int(request.form.get("size"), 0, 0, 1024)
    slots = [parse_int(request.form.get(f"slot{i}"), 0, 0, 10**9) or None for i in range(size)]
    slots = [pid if pid in active else None for pid in slots]
    bracket.save_slots(conn, tid, slots)  # keep the admin's work even if it isn't valid yet
    if size != bracket.bracket_size(max(len(active), 2)):
        error = "The player list changed while you were editing — please review the bracket again."
    else:
        error = bracket.validate_slots(slots, active)
    if error:
        flash(error)
        return redirect(url_for("bracket_setup", tid=tid))
    created = tm.start_bracket(conn, tid, slots)
    flash(f"Bracket started — {len(created)} match(es) on the tables.")


@admin_post("bracket-reset")
def bracket_reset(conn, tid):
    if tm.reset_bracket(conn, tid):
        flash("Bracket reset. Signups are open again and your draw has been kept.")
        return redirect(url_for("bracket_setup", tid=tid))
    flash("Games have already been played, so the bracket can't be reset.")


@admin_post("delete-tournament")
def delete_tournament(conn, tid):
    if request.form.get("confirm_name", "").strip() != tm.get_tournament(conn, tid)["name"]:
        flash("Type the tournament name exactly to confirm deletion.")
        return
    for table in ("matches", "bracket_slots", "byes", "players"):
        conn.execute(f"DELETE FROM {table} WHERE tournament_id = ?", (tid,))
    conn.execute("DELETE FROM tournaments WHERE id = ?", (tid,))
    flash("Tournament deleted.")
    return redirect(url_for("home"))


# --------------------------------------------------------------------------- main

db.init_db()

if __name__ == "__main__":
    print("\n  Ping Pong Tournament is running")
    print(f"    On this computer:   http://localhost:{PORT}")
    print(f"    On the network:     {public_base_url()}")
    print("  (Phones must be on the same network. Press Ctrl+C to stop.)\n")
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=PORT, threads=16)
    except ImportError:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
