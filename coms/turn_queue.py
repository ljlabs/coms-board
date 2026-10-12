"""Bound concurrent local-model prompt turns, prioritizing peer decisions."""
from contextlib import contextmanager
import threading
import time


class TurnQueue:
    def __init__(self, capacity=2):
        self.capacity = max(1, capacity)
        self.condition = threading.Condition()
        self.active = 0
        self.waiters = []
        self.sequence = 0

    @contextmanager
    def turn(self, priority, *, cancelled=lambda: False, timeout=900):
        deadline = time.monotonic() + timeout
        claimed = False
        with self.condition:
            self.sequence += 1
            key = (priority, self.sequence)
            self.waiters.append(key)
            try:
                while True:
                    if cancelled(): raise RuntimeError('Queued model turn cancelled')
                    if self.active < self.capacity and key == min(self.waiters):
                        self.waiters.remove(key)
                        self.active += 1
                        claimed = True
                        break
                    if time.monotonic() >= deadline: raise RuntimeError('Timed out waiting for local-model turn capacity')
                    self.condition.wait(.25)
            finally:
                if not claimed:
                    self.waiters.remove(key)
                    self.condition.notify_all()
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()
