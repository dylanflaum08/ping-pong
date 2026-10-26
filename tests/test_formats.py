import os
import random
import sqlite3
import sys
import tempfile
import unittest

if "PINGPONG_DB" not in os.environ:
    os.environ["PINGPONG_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bracket  # noqa: E402
import db  # noqa: E402
import tournament as tm  # noqa: E402
from app import app  # noqa: E402


def make(conn, kind, players, tables=2, rounds=3, rng=None):
    rng = rng or random.Random(1)
    tid = conn.execute(
        "INSERT INTO tournaments (name, type, num_tables, rounds) VALUES ('T', ?, ?, ?)", (kind, tables, rounds)
    ).lastrowid
    ids = [tm.add_player(conn, tid, f"P{i}", kind, rng.randint(1, 4)) for i in range(players)]
    return tid, ids


def on_table(conn, tid):
    return conn.execute(
        "SELECT * FROM matches WHERE tournament_id = ? AND status = 'pending' AND table_no > 0 ORDER BY table_no", (tid,)
    ).fetchall()


def play_round(conn, tid, round_no, rng):
    """Play only the given round's games (the next round is paired automatically afterwards)."""
    while True:
        m = conn.execute(
            "SELECT * FROM matches WHERE tournament_id = ? AND status = 'pending' AND round = ? AND table_no > 0 "
            "ORDER BY table_no", (tid, round_no)
        ).fetchone()
        if m is None:
            return
        tm.record_result(conn, tid, m["id"], rng.choice([m["player1_id"], m["player2_id"]]))


def play_out(conn, tid, rng, limit=500):
    for _ in range(limit):
        tables = on_table(conn, tid)
        if not tables:
            return
        m = tables[0]
        tm.record_result(conn, tid, m["id"], rng.choice([m["player1_id"], m["player2_id"]]))
    raise AssertionError("tournament never finished")


class BracketTests(unittest.TestCase):
    def test_random_draw_is_always_valid(self):
        rng = random.Random(7)
        for n in range(2, 34):
            ids = list(range(1, n + 1))
            slots = bracket.random_slots(ids, rng)
            self.assertEqual(len(slots), bracket.bracket_size(n))
            self.assertIsNone(bracket.validate_slots(slots, ids), n)

    def test_validation_catches_mistakes(self):
        self.assertIn("more than once", bracket.validate_slots([1, 1, 2, 3], [1, 2, 3]))
        self.assertIn("no players", bracket.validate_slots([1, 2, None, None], [1, 2]))
        self.assertIn("aren't in the bracket", bracket.validate_slots([1, 2, 3, None], [1, 2, 3, 4]))

    def test_plays_through_to_a_champion(self):
        rng = random.Random(2)
        for n in (2, 3, 5, 8, 11):
            with db.connect() as conn:
                tid, ids = make(conn, "bracket", n, tables=3)
                tm.start_bracket(conn, tid, bracket.random_slots(ids, rng))
                play_out(conn, tid, rng)
                view = tm.bracket_view(conn, tid)
                self.assertIsNotNone(view["champion"], n)
                games = conn.execute(
                    "SELECT COUNT(*) FROM matches WHERE tournament_id = ? AND status = 'completed'", (tid,)
                ).fetchone()[0]
                self.assertEqual(games, n - 1)  # single elimination: everyone but the champion loses once
                rows = tm.standings(conn, tid)
                self.assertEqual(rows[0]["bracket_status"], "Champion")
                self.assertEqual(rows[1]["bracket_status"], "Runner-up")

    def test_manual_draw_is_respected(self):
        with db.connect() as conn:
            tid, (a, b, c, d) = make(conn, "bracket", 4)
            tm.start_bracket(conn, tid, [a, d, b, c])
            pairs = {frozenset((m["player1_id"], m["player2_id"])) for m in on_table(conn, tid)}
            self.assertEqual(pairs, {frozenset((a, d)), frozenset((b, c))})

    def test_changing_a_result_respects_later_rounds(self):
        with db.connect() as conn:
            tid, (a, b, c, d) = make(conn, "bracket", 4)
            tm.start_bracket(conn, tid, [a, b, c, d])
            m1, m2 = on_table(conn, tid)
            tm.record_result(conn, tid, m1["id"], a)
            tm.record_result(conn, tid, m2["id"], c)
            final = on_table(conn, tid)[0]
            self.assertEqual({final["player1_id"], final["player2_id"]}, {a, c})

            # Final not played yet: flipping a semifinal re-pairs the final.
            self.assertIsNone(tm.edit_result(conn, tid, m1["id"], b))
            final = on_table(conn, tid)[0]
            self.assertEqual({final["player1_id"], final["player2_id"]}, {b, c})

            # Once the final is played, the semifinal is locked.
            tm.record_result(conn, tid, final["id"], c)
            self.assertIn("already been played", tm.edit_result(conn, tid, m1["id"], a))
            self.assertEqual(tm.bracket_view(conn, tid)["champion"], "P2 bracket")

    def test_withdrawal_is_a_forfeit(self):
        with db.connect() as conn:
            tid, (a, b, c, d) = make(conn, "bracket", 4)
            tm.start_bracket(conn, tid, [a, b, c, d])
            tm.set_player_active(conn, tid, b, False)
            m = conn.execute(
                "SELECT * FROM matches WHERE tournament_id = ? AND walkover = 1", (tid,)
            ).fetchone()
            self.assertEqual(m["winner_id"], a)
            elo_a = conn.execute("SELECT elo, initial_elo FROM players WHERE id = ?", (a,)).fetchone()
            self.assertEqual(elo_a[0], elo_a[1], "forfeit must not change ratings")

    def test_tables_limit_and_queue(self):
        with db.connect() as conn:
            tid, ids = make(conn, "bracket", 8, tables=1)
            tm.start_bracket(conn, tid, ids)
            self.assertEqual(len(on_table(conn, tid)), 1)
            queued = conn.execute(
                "SELECT COUNT(*) FROM matches WHERE tournament_id = ? AND status = 'pending' AND table_no = 0", (tid,)
            ).fetchone()[0]
            self.assertEqual(queued, 3)


class SwissTests(unittest.TestCase):
    def run_swiss(self, players, rounds, seed):
        rng = random.Random(seed)
        with db.connect() as conn:
            tid, ids = make(conn, "swiss", players, tables=3, rounds=rounds, rng=rng)
            conn.execute("UPDATE tournaments SET status = 'in_progress' WHERE id = ?", (tid,))
            tm.generate_matches(conn, tid)
            mismatched = 0
            for r in range(1, rounds + 1):
                rec = {p["id"]: p["wins"] for p in conn.execute("SELECT * FROM players WHERE tournament_id = ?", (tid,))}
                ms = conn.execute("SELECT * FROM matches WHERE tournament_id = ? AND round = ?", (tid, r)).fetchall()
                self.assertEqual(len(ms), players // 2, f"round {r}")
                mismatched += sum(1 for m in ms if rec[m["player1_id"]] != rec[m["player2_id"]])
                # Within a round at most one player per record group has to play down a group.
                self.assertLessEqual(sum(1 for m in ms if rec[m["player1_id"]] != rec[m["player2_id"]]),
                                     len(set(rec.values())) - 1, f"round {r}")
                play_round(conn, tid, r, rng)
            pairs = [frozenset((m["player1_id"], m["player2_id"]))
                     for m in conn.execute("SELECT * FROM matches WHERE tournament_id = ?", (tid,))]
            byes = conn.execute("SELECT player_id FROM byes WHERE tournament_id = ?", (tid,)).fetchall()
            return tid, pairs, byes, mismatched

    def test_same_record_pairing_no_rematches(self):
        for players, rounds, seed in ((8, 3, 1), (11, 4, 2), (16, 4, 3), (7, 3, 4)):
            tid, pairs, byes, mismatched = self.run_swiss(players, rounds, seed)
            self.assertEqual(len(pairs), len(set(pairs)), "rematch")
            if players % 2:
                ids = [b[0] for b in byes]
                self.assertEqual(len(ids), rounds)
                self.assertEqual(len(ids), len(set(ids)), "someone got two byes")
            else:
                self.assertEqual(byes, [])

    def test_bye_counts_as_a_win(self):
        with db.connect() as conn:
            tid, ids = make(conn, "swiss", 3, rounds=1)
            conn.execute("UPDATE tournaments SET status = 'in_progress' WHERE id = ?", (tid,))
            tm.generate_matches(conn, tid)
            bye = conn.execute("SELECT player_id FROM byes WHERE tournament_id = ?", (tid,)).fetchone()[0]
            wins = conn.execute("SELECT wins FROM players WHERE id = ?", (bye,)).fetchone()[0]
            self.assertEqual(wins, 1)

    def test_stops_after_last_round(self):
        rng = random.Random(9)
        with db.connect() as conn:
            tid, ids = make(conn, "swiss", 6, rounds=2, rng=rng)
            conn.execute("UPDATE tournaments SET status = 'in_progress' WHERE id = ?", (tid,))
            tm.generate_matches(conn, tid)
            play_out(conn, tid, rng)
            self.assertEqual(tm.get_tournament(conn, tid)["current_round"], 2)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM matches WHERE tournament_id = ?", (tid,)).fetchone()[0], 6)
            r1 = conn.execute("SELECT id FROM matches WHERE tournament_id = ? AND round = 1", (tid,)).fetchone()[0]
            self.assertIn("already been paired", tm.delete_result(conn, tid, r1))


class MigrationTests(unittest.TestCase):
    def test_old_database_is_upgraded(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE tournaments (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'registration', num_tables INTEGER NOT NULL DEFAULT 1,
                min_games INTEGER NOT NULL DEFAULT 3, qualify_count INTEGER NOT NULL DEFAULT 8, admin_pin TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')));
            CREATE TABLE matches (id INTEGER PRIMARY KEY AUTOINCREMENT, tournament_id INTEGER NOT NULL,
                table_no INTEGER NOT NULL, player1_id INTEGER NOT NULL, player2_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', winner_id INTEGER, p1_score INTEGER, p2_score INTEGER,
                p1_elo_before INTEGER, p2_elo_before INTEGER, elo_change INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')), completed_at TEXT, completed_seq INTEGER);
            INSERT INTO tournaments (name, status) VALUES ('Old one', 'completed');
        """)
        old.commit()
        old.close()
        saved = db.DB_PATH
        db.DB_PATH = path
        try:
            db.init_db()
            with db.connect() as conn:
                t = conn.execute("SELECT * FROM tournaments").fetchone()
                self.assertEqual((t["name"], t["type"], t["rounds"]), ("Old one", "elo", 3))
                cols = {r["name"] for r in conn.execute("PRAGMA table_info(matches)")}
                self.assertTrue({"round", "slot", "walkover"} <= cols)
        finally:
            db.DB_PATH = saved


class WebFormatTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.admin = app.test_client()

    def create(self, kind, **extra):
        r = self.admin.post("/tournaments", data={"name": f"{kind} cup", "num_tables": "2", "type": kind, **extra})
        return int(r.headers["Location"].split("/")[2])

    def join(self, tid, n):
        for i in range(n):
            r = app.test_client().post(f"/t/{tid}/join", data={"first_name": f"Pl{i}", "last_name": "Web", "level": "2"})
            self.assertEqual(r.status_code, 302)

    def test_bracket_flow(self):
        tid = self.create("bracket")
        self.join(tid, 5)
        # Begin play goes to setup rather than starting
        r = self.admin.post(f"/t/{tid}/admin/begin")
        self.assertIn("/admin/bracket", r.headers["Location"])
        page = self.admin.get(f"/t/{tid}/admin/bracket").data
        self.assertIn(b"8-player bracket", page)

        # Manual draw missing a player is rejected
        with db.connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM players WHERE tournament_id = ? ORDER BY id", (tid,))]
        form = {"size": "8", "slot0": ids[0], "slot1": ids[1], "slot2": ids[2], "slot4": ids[3]}
        self.admin.post(f"/t/{tid}/admin/bracket-start", data=form)
        self.assertEqual(self.admin.get(f"/t/{tid}/api/state").get_json()["tournament"]["status"], "registration")

        # Randomize then start with the saved draw
        self.admin.post(f"/t/{tid}/admin/bracket-randomize")
        with db.connect() as conn:
            slots = bracket.get_slots(conn, tid)
        form = {"size": "8", **{f"slot{i}": (pid or "") for i, pid in enumerate(slots)}}
        self.admin.post(f"/t/{tid}/admin/bracket-start", data=form)
        state = self.admin.get(f"/t/{tid}/api/state").get_json()
        self.assertEqual(state["tournament"]["status"], "in_progress")
        self.assertFalse(state["signup_open"])
        self.assertEqual(state["current"][0]["round"], "Quarterfinals")
        self.assertEqual(len(state["bracket"]["rounds"]), 3)

        late = app.test_client().get(f"/t/{tid}/join").data
        self.assertIn(b"closed", late)
        for path in (f"/t/{tid}/display", f"/t/{tid}/leaderboard", f"/t/{tid}/admin"):
            self.assertEqual(self.admin.get(path).status_code, 200, path)

    def test_levels_only_asked_for_elo(self):
        for kind, asks in (("elo", True), ("bracket", False), ("swiss", False)):
            tid = self.create(kind)
            page = app.test_client().get(f"/t/{tid}/join").data
            self.assertEqual(b"Your ping pong level" in page, asks, kind)

            phone = app.test_client()
            r = phone.post(f"/t/{tid}/join", data={"first_name": "Sam", "last_name": "Smith"})
            if asks:
                self.assertIn(b"Please pick your level", r.data)
            else:
                self.assertEqual(r.status_code, 302, kind)
                # A level sent anyway is ignored: everyone starts level with each other.
                app.test_client().post(f"/t/{tid}/join", data={"first_name": "Says", "last_name": "Four", "level": "4"})
                with db.connect() as conn:
                    starts = {r[0] for r in conn.execute("SELECT elo FROM players WHERE tournament_id = ?", (tid,))}
                self.assertEqual(starts, {1000}, kind)
                self.assertNotIn(b"Level", phone.get(f"/t/{tid}/me").data, kind)
                admin_page = self.admin.get(f"/t/{tid}/admin").data
                self.assertNotIn(b'name="level"', admin_page, kind)

    def test_swiss_flow(self):
        tid = self.create("swiss", rounds="2")
        self.join(tid, 5)
        self.admin.post(f"/t/{tid}/admin/begin")
        state = self.admin.get(f"/t/{tid}/api/state").get_json()
        self.assertEqual(state["tournament"]["current_round"], 1)
        self.assertEqual(len(state["current"]), 2)
        self.assertIsNotNone(state["bye"])
        self.assertTrue(state["signup_open"])
        for path in (f"/t/{tid}/display", f"/t/{tid}/leaderboard", f"/t/{tid}/admin", "/"):
            self.assertEqual(self.admin.get(path).status_code, 200, path)
        self.assertIn(b"Round 1 of 2", self.admin.get(f"/t/{tid}/admin").data)


if __name__ == "__main__":
    unittest.main()
