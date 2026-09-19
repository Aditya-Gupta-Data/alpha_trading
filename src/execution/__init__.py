"""src/execution — the Phase M2 execution layer (decision #101).

Only PAPER venues live here. Nothing in this package imports a broker, and
Rule 7 (no order-placement path in src/) is unchanged: a venue is what a
caller explicitly drives an Order Ticket through, never something that
reaches a real exchange.
"""
