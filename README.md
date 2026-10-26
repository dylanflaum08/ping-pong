# Ping Pong Tournament

A small local web app for running a walk-up ping pong tournament: QR-code signup,
automatic matchmaking, Elo-style ratings, a big-screen display, and a leaderboard.
Three formats: **Elo based**, **Bracket** (single elimination) and **Round robin**
(rounds where players with the same record meet).
Everything runs on one laptop; data is stored in a local SQLite file (`pingpong.db`).

## Running it

```
start.bat            (or:  pip install -r requirements.txt  &&  python app.py)
```

The console prints two addresses:

- `http://localhost:5000` — use this on the laptop.
- `http://<laptop-ip>:5000` — this is what the QR code points to. Phones must be on
  the **same network** as the laptop and able to reach it. If Windows asks whether to
  allow Python through the firewall, allow it on the network you are using.

If the detected address is wrong (e.g. the laptop has several network adapters), set it
explicitly: `set PUBLIC_URL=http://192.168.1.50:5000` before `python app.py`.
`PORT` changes the port.

## On the day

1. **Home page → Start tournament.** Pick the type, give it a name, number of tables, and
   an optional admin PIN (stops players entering results from their phones).
2. **Open main screen** on the TV/projector. It shows the QR code, the current matchups,
   who's waiting, recent results, and the leaderboard, and updates every 3 seconds.
3. People scan the QR code and enter first name and last name — plus, for **Elo based**
   only, their level (1 = beginner, 4 = advanced), which sets their starting rating. In
   Bracket and Round robin nobody is asked for a level and everyone starts at 1000.
   Their phone then shows their own status, current table, and record.
4. On the admin page press **Begin play** (Bracket: **Set up bracket**, see below).
5. When a game ends, tap the winner on the admin page (scores optional). The table is
   immediately refilled with the next matchup.
6. The **Leaderboard** shows the current top 8 among players with at least 3 games.
   Press **End tournament** to stop new matchups.

Admin page also lets you add players by hand, edit names/levels, withdraw a player who
leaves (their current match is cancelled and the table re-paired), cancel a match, and
edit or delete past results. Any change replays all ratings from scratch.

## Tournament types

| Type | How matchups are made | Who wins |
|---|---|---|
| **Elo based** | Continuously: whoever has played the fewest games plays next (below). | Top 8 by rating among players with 3+ games. |
| **Bracket** | Single elimination. The admin randomizes the draw or picks every first-round matchup by hand. | Last one standing. |
| **Round robin** | In rounds (3 by default). Each round pairs players with the same record — 1-0 vs 1-0, 0-1 vs 0-1 — and avoids rematches. | Best record; ties broken by opponents' total wins, then rating. |

**Bracket details** (`bracket.py`)
- Signups close when the bracket starts. If the player count isn't a power of 2, the
  bracket gets byes — a player facing "Bye" goes straight to the next round.
- Every match whose two players are known is queued; queued matches move onto tables as
  they free up ("On deck" on the main screen).
- Withdrawing a player gives their opponent a forfeit win (no rating change).
- A result can be changed until the next-round match it feeds has been played.
- **Redo bracket** returns to setup as long as no game has been played.

**Round robin details** (`swiss.py`) — technically a Swiss-system tournament
- The next round is paired automatically as soon as the last game of the current round
  is entered. When a record group has an odd number of players, one plays someone from
  the next group down.
- With an odd number of players, the lowest-ranked player who hasn't had one gets a
  **bye**, which counts as a win.
- Late arrivals can still sign up and join from the next round.
- Withdrawing a player mid-round gives their opponent a forfeit win.

Ratings update in every format, so everyone can see their rating move.

## Ratings (`elo.py`)

- Starting rating by level: 1 → 1000, 2 → 1050, 3 → 1100, 4 → 1150.
- Each win moves points from loser to winner (zero-sum):
  `points = 20 + 5 × (1 − 2 × expected)`, where `expected` is the standard Elo win
  probability. That is 20 for an even match, up to 25 for a big upset, down to 15 for
  beating a much weaker player. 150 rating points apart gives 22 vs 18.

All of these numbers are constants at the top of `elo.py`.

## Elo-based matchmaking (`tournament.py`)

Whenever a table is free:

1. The free player with the **fewest games** goes next (ties: longest wait, then signup order).
2. Their opponent is the free player with the lowest
   `games played + 2 × times they've already played each other`, avoiding an immediate
   rematch, then the closest rating.

This keeps everyone's game count within a game or two of each other until all have
reached the minimum (3 by default, configurable in Settings along with tables and the
number who advance).

## Tests

```
python -m unittest discover -s tests
```
