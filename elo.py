"""Rating algorithm for the tournament.

A dampened Elo variant: every decided game moves BASE_POINTS between the two
players, nudged by at most +/- SWING_POINTS depending on how surprising the
result was. Beating a stronger player earns a little more, beating a weaker
player earns a little less, but the difference is intentionally small.

With the defaults below a win is always worth between 15 and 25 points:
    evenly matched            -> 20
    winner 150 pts stronger   -> 18
    winner 150 pts weaker     -> 22
    huge mismatch (either way)-> approaches 15 / 25

The system is zero-sum: the loser drops exactly what the winner gains.
"""

# Starting rating by self-reported level (1 = beginner ... 4 = advanced).
# Levels are deliberately close together (one level ~= 2-3 wins) so that
# results on the day matter more than the number people pick at signup.
STARTING_ELO = {1: 1000, 2: 1050, 3: 1100, 4: 1150}

BASE_POINTS = 20
SWING_POINTS = 5
SCALE = 400  # standard Elo scale for the expected-score curve


def starting_elo(level: int) -> int:
    return STARTING_ELO.get(int(level), STARTING_ELO[1])


def expected_score(rating: float, opponent_rating: float) -> float:
    """Probability (0-1) that `rating` beats `opponent_rating`."""
    return 1.0 / (1.0 + 10 ** ((opponent_rating - rating) / SCALE))


def points_for_win(winner_rating: float, loser_rating: float) -> int:
    """Points transferred from loser to winner for a single game."""
    expected = expected_score(winner_rating, loser_rating)
    # expected = 0.5 -> BASE; expected -> 0 (upset) -> BASE + SWING;
    # expected -> 1 (favourite won) -> BASE - SWING
    return round(BASE_POINTS + SWING_POINTS * (1 - 2 * expected))
