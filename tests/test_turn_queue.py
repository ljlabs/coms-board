import threading
import unittest

from coms.turn_queue import TurnQueue


class TurnQueueTests(unittest.TestCase):
    def test_capacity_and_review_priority_under_contention(self):
        queue = TurnQueue(1)
        occupied, release = threading.Event(), threading.Event()
        normal_waiting, review_waiting = threading.Event(), threading.Event()
        order = []
        def first():
            with queue.turn(2):
                occupied.set()
                release.wait(5)
        def waiting(label, priority, event):
            def cancelled():
                event.set()
                return False
            with queue.turn(priority, cancelled=cancelled):
                order.append(label)
        threads = [threading.Thread(target=first),
                   threading.Thread(target=waiting, args=('worker', 2, normal_waiting)),
                   threading.Thread(target=waiting, args=('review', 0, review_waiting))]
        threads[0].start()
        self.assertTrue(occupied.wait(2))
        threads[1].start()
        self.assertTrue(normal_waiting.wait(2))
        threads[2].start()
        self.assertTrue(review_waiting.wait(2))
        release.set()
        for thread in threads: thread.join(3)
        self.assertEqual(order, ['review', 'worker'])
        self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_cancelled_waiter_does_not_consume_future_capacity(self):
        queue = TurnQueue(1)
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            with queue.turn(0, cancelled=lambda: True):
                self.fail('Cancelled work started')
        with queue.turn(2): pass
