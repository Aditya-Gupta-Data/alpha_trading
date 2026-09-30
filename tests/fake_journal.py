# TEST INFRA — the in-memory journal the tracker/rotation tests swap in.
"""The journal's full write interface (decision #122) over a test's own
`read_all` / `rewrite_all`: `locked()`, `row_key`, `get_entry`,
`update_matching`, `update_entry`, `mutate_all`, `log`. A test's fake
subclasses this and keeps its own `read_all` (and, if it captures writes,
`rewrite_all`) — every helper routes through those two, so what the test
captured before #122 it still captures."""
from contextlib import nullcontext

from src import journal as _journal


class FakeJournalBase:
    row_key = staticmethod(_journal.row_key)

    def locked(self, timeout=None):
        return nullcontext()

    def lock_held(self):
        return False

    def rewrite_all(self, entries):
        self.rewritten = entries

    def get_entry(self, key):
        return next((e for e in self.read_all() if _journal.row_key(e) == key), None)

    def update_matching(self, match_fn, mutate_fn):
        entries = self.read_all()
        target = next((e for e in entries if match_fn(e)), None)
        if target is None or mutate_fn(target) is False:
            return None
        self.rewrite_all(entries)
        return target

    def update_entry(self, short_id, mutate_fn):
        return self.update_matching(lambda e: e.get("short_id") == short_id, mutate_fn)

    def mutate_all(self, fn):
        entries = self.read_all()
        result = fn(entries)
        if result:
            self.rewrite_all(entries)
        return result

    def log(self, entry):
        self.read_all().append(entry)
