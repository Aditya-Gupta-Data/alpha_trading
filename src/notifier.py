"""
Sends alerts.

Right now it just prints to the screen, which is enough to prove the whole
pipeline works end to end. Telegram (or email) delivery will plug in right here
in Phase 1, step 2 — the rest of the program won't need to change.
"""


def send_alert(message: str) -> None:
    print(f"  [ALERT] {message}")
