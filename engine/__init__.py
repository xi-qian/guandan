"""掼蛋规则引擎（纯逻辑，无 IO）。"""
from .cards import DECK, Card, build_deck
from .combos import Combo, beats, enumerate_combos, identify, legal_moves
from .game import Game, IllegalMove, team_of

__all__ = [
    "DECK",
    "Card",
    "build_deck",
    "Combo",
    "beats",
    "enumerate_combos",
    "identify",
    "legal_moves",
    "Game",
    "IllegalMove",
    "team_of",
]
