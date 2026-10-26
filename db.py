import os
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get("PINGPONG_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "pingpong.db"))

# Single-process app: one lock serialises every write so two results submitted
# at the same moment can never double-book a player into two matches.
write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tournaments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'registration',  -- registration | in_progress | completed
    num_tables    INTEGER NOT NULL DEFAULT 1,
    min_games     INTEGER NOT NULL DEFAULT 3,
    qualify_count INTEGER NOT NULL DEFAULT 8,
    admin_pin     TEXT,
    type          TEXT    NOT NULL DEFAULT 'elo',            -- elo | bracket | swiss
    rounds        INTEGER NOT NULL DEFAULT 3,                -- swiss: number of rounds
    current_round INTEGER NOT NULL DEFAULT 0,                -- swiss: round in progress
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS players (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id  INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    first_name     TEXT    NOT NULL,
    last_name      TEXT    NOT NULL,
    level          INTEGER NOT NULL CHECK (level BETWEEN 1 AND 4),
    initial_elo    INTEGER NOT NULL,
    elo            INTEGER NOT NULL,
    games_played   INTEGER NOT NULL DEFAULT 0,
    wins           INTEGER NOT NULL DEFAULT 0,
    losses         INTEGER NOT NULL DEFAULT 0,
    active         INTEGER NOT NULL DEFAULT 1,
    joined_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_played_at TEXT
);

CREATE TABLE IF NOT EXISTS matches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id  INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    table_no       INTEGER NOT NULL,  -- 0 = queued, waiting for a free table
    player1_id     INTEGER NOT NULL REFERENCES players(id),
    player2_id     INTEGER NOT NULL REFERENCES players(id),
    status         TEXT    NOT NULL DEFAULT 'pending',  -- pending | completed | cancelled
    winner_id      INTEGER REFERENCES players(id),
    p1_score       INTEGER,
    p2_score       INTEGER,
    p1_elo_before  INTEGER,
    p2_elo_before  INTEGER,
    elo_change     INTEGER,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    completed_at   TEXT,
    completed_seq  INTEGER, -- order results were entered; used to replay ratings
    round          INTEGER, -- bracket / swiss round number
    slot           INTEGER, -- bracket: position within the round
    walkover       INTEGER NOT NULL DEFAULT 0  -- won by forfeit: counts in the record, not the rating
);

-- Bracket: first-round positions. Pairs are (0,1), (2,3), ...; NULL = bye.
CREATE TABLE IF NOT EXISTS bracket_slots (
    tournament_id  INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    position       INTEGER NOT NULL,
    player_id      INTEGER REFERENCES players(id),
    PRIMARY KEY (tournament_id, position)
);

-- Swiss: a player who sat out a round with an odd number of players (counts as a win).
CREATE TABLE IF NOT EXISTS byes (
    tournament_id  INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    round          INTEGER NOT NULL,
    player_id      INTEGER NOT NULL REFERENCES players(id),
    PRIMARY KEY (tournament_id, round)
);

CREATE INDEX IF NOT EXISTS idx_players_t ON players(tournament_id);
CREATE INDEX IF NOT EXISTS idx_matches_t ON matches(tournament_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_players_name
    ON players(tournament_id, lower(first_name), lower(last_name));
"""


@contextmanager
def connect():
    """Open a connection; commit on success, roll back on error, always close."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns added after the first release; init_db adds any that an older database lacks.
MIGRATIONS = {
    "tournaments": {
        "type": "TEXT NOT NULL DEFAULT 'elo'",
        "rounds": "INTEGER NOT NULL DEFAULT 3",
        "current_round": "INTEGER NOT NULL DEFAULT 0",
    },
    "matches": {
        "round": "INTEGER",
        "slot": "INTEGER",
        "walkover": "INTEGER NOT NULL DEFAULT 0",
    },
}


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
