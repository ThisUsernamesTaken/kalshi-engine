"""Commodity daily-ladder controller (Pyth-settled metals/energy).

The favorite-chase *scoring* core (Phase4CutpointsModel V13B + FavoriteChaseState)
is reused unchanged; what is new here is the **daily-window controller** — the
crypto entry window is "T+8..15 of a 15-min cycle", which does not exist for a
product that settles once a day at 5pm ET. ``window.py`` re-expresses the entry
window as *minutes before close*.
"""
