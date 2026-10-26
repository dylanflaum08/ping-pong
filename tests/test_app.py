import os
import random
import sys
import tempfile
import unittest
from collections import Counter

TMP = tempfile.mkdtemp()
os.environ["PINGPONG_DB"] = os.path.join(TMP, "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import elo  # noqa: E402
import tournament as tm  # noqa: E402
from app import app  # noqa: E402


def new_tournament(conn, tables=2):
    cur = conn.execute("INSERT INTO tournaments (name, num_tables, status) VALUES ('T', ?, 'in_progress')", (tables,))
    return cur.lastrowid


class EloTests(unittest.TestCase):
    def test_even_match_is_base_points(self):
        self.assertEqual(elo.points_for_win(1100, 1100), 20)

    def test_upset_earns_slightly_more(self):
        upset = elo.points_for_win(1000, 1150)
        expected = elo.points_for_win(1150, 1000)
        self.assertGreater(upset, 20)
        self.assertLess(expected, 20)
        self.assertLessEqual(upset - expected, 10)

    def test_always_within_bounds(self):
        for a in range(600, 1700, 50):
            for b in range(600, 1700, 50):
                self.assertTrue(15 <= elo.points_for_win(a, b) <= 25)


class MatchmakingTests(unittest.TestCase):
    def test_everyone_reaches_min_games_fairly(self):
        rng = random.Random(42)
        with db.connect() as conn:
            tid = new_tournament(conn, tables=3)
            for i in range(13):  # odd number on purpose
                tm.add_player(conn, tid, f"P{i}", "Test", rng.randint(1, 4))

            for _ in range(200):
                pending = conn.execute(
                    "SELECT * FROM matches WHERE tournament_id = ? AND status = 'pending'", (tid,)
                ).fetchall()
                ids = [p for m in pending for p in (m["player1_id"], m["player2_id"])]
                self.assertEqual(len(ids), len(set(ids)), "player booked into two matches")
                self.assertLessEqual(len(pending), 3)

                games = [r[0] for r in conn.execute("SELECT games_played FROM players WHERE tournament_id = ?", (tid,))]
                if min(games) >= 3:
                    break
                m = rng.choice(pending)
                tm.record_result(conn, tid, m["id"], rng.choice([m["player1_id"], m["player2_id"]]))

            games = [r[0] for r in conn.execute("SELECT games_played FROM players WHERE tournament_id = ?", (tid,))]
            self.assertGreaterEqual(min(games), 3)
            self.assertLessEqual(max(games) - min(games), 2, games)

            # Zero-sum ratings
            total, initial = conn.execute(
                "SELECT SUM(elo), SUM(initial_elo) FROM players WHERE tournament_id = ?", (tid,)
            ).fetchone()
            self.assertEqual(total, initial)

            # Few repeat pairings
            pairs = Counter(
                frozenset((r[0], r[1])) for r in conn.execute(
                    "SELECT player1_id, player2_id FROM matches WHERE tournament_id = ? AND status = 'completed'", (tid,)
                )
            )
            self.assertLessEqual(max(pairs.values()), 2)

    def test_edit_result_replays_ratings(self):
        with db.connect() as conn:
            tid = new_tournament(conn, tables=1)
            a = tm.add_player(conn, tid, "A", "X", 1)
            b = tm.add_player(conn, tid, "B", "X", 4)
            m = conn.execute("SELECT * FROM matches WHERE tournament_id = ? AND status='pending'", (tid,)).fetchone()
            tm.record_result(conn, tid, m["id"], a)  # upset
            elo_a = conn.execute("SELECT elo FROM players WHERE id = ?", (a,)).fetchone()[0]
            self.assertEqual(elo_a, 1000 + elo.points_for_win(1000, 1150))

            tm.edit_result(conn, tid, m["id"], b)
            elo_a, elo_b = (conn.execute("SELECT elo FROM players WHERE id = ?", (p,)).fetchone()[0] for p in (a, b))
            change = elo.points_for_win(1150, 1000)
            self.assertEqual((elo_a, elo_b), (1000 - change, 1150 + change))

            tm.delete_result(conn, tid, m["id"])
            elo_a, elo_b = (conn.execute("SELECT elo FROM players WHERE id = ?", (p,)).fetchone()[0] for p in (a, b))
            self.assertEqual((elo_a, elo_b), (1000, 1150))

    def test_withdrawn_player_frees_table(self):
        with db.connect() as conn:
            tid = new_tournament(conn, tables=1)
            a = tm.add_player(conn, tid, "A", "Y", 2)
            tm.add_player(conn, tid, "B", "Y", 2)
            c = tm.add_player(conn, tid, "C", "Y", 2)
            tm.set_player_active(conn, tid, a, False)
            pending = conn.execute("SELECT * FROM matches WHERE tournament_id = ? AND status='pending'", (tid,)).fetchall()
            self.assertEqual(len(pending), 1)
            self.assertIn(c, (pending[0]["player1_id"], pending[0]["player2_id"]))


class WebFlowTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.admin = app.test_client()

    def test_full_flow(self):
        r = self.admin.post("/tournaments", data={"name": "Office Open", "num_tables": "2", "admin_pin": "1234"})
        self.assertEqual(r.status_code, 302)
        tid = int(r.headers["Location"].split("/")[2])

        # Players join from their phones
        for i, (first, last, lvl) in enumerate([("Ann", "Lee", 1), ("Bo", "O'Brien", 4), ("Cy", "Diaz", 2),
                                                ("Di", "Kim", 3), ("Ed", "Ng", 2)]):
            phone = app.test_client()
            r = phone.post(f"/t/{tid}/join", data={"first_name": first, "last_name": last, "level": str(lvl)})
            self.assertEqual(r.status_code, 302, r.data)
            self.assertIn(b"You're in", phone.get(f"/t/{tid}/me").data)

        dup = app.test_client().post(f"/t/{tid}/join", data={"first_name": "ann", "last_name": "lee", "level": "1"})
        self.assertIn(b"already signed up", dup.data)

        # Random phone can't reach admin
        stranger = app.test_client()
        self.assertEqual(stranger.get(f"/t/{tid}/admin").status_code, 302)
        self.assertEqual(stranger.post(f"/t/{tid}/admin/begin").status_code, 302)
        self.assertIn(b"Incorrect", stranger.post(f"/t/{tid}/login", data={"pin": "0000"}).data)

        self.admin.post(f"/t/{tid}/admin/begin")
        state = self.admin.get(f"/t/{tid}/api/state").get_json()
        self.assertEqual(len(state["current"]), 2)
        self.assertEqual(len(state["waiting"]), 1)

        # Enter a result via the admin page
        with db.connect() as conn:
            m = conn.execute("SELECT * FROM matches WHERE tournament_id = ? AND status='pending' ORDER BY table_no",
                             (tid,)).fetchone()
        self.admin.post(f"/t/{tid}/admin/result/{m['id']}",
                        data={"winner": m["player1_id"], "p1_score": "5", "p2_score": "11"})
        self.assertEqual(len(self.admin.get(f"/t/{tid}/api/state").get_json()["recent"]), 0, "contradicting score accepted")
        r = self.admin.post(f"/t/{tid}/admin/result/{m['id']}",
                            data={"winner": m["player1_id"], "p1_score": "11", "p2_score": "7"})
        self.assertEqual(r.status_code, 302)
        state = self.admin.get(f"/t/{tid}/api/state").get_json()
        self.assertEqual(len(state["recent"]), 1)
        self.assertEqual(state["recent"][0]["score"], "11–7")
        self.assertEqual(len(state["current"]), 2)  # table refilled

        for path in ("/", f"/t/{tid}/display", f"/t/{tid}/leaderboard", f"/t/{tid}/admin", f"/t/{tid}/qr.svg"):
            self.assertEqual(self.admin.get(path).status_code, 200, path)
        self.assertIn(b"O&#39;Brien", self.admin.get(f"/t/{tid}/admin").data)


if __name__ == "__main__":
    unittest.main()
