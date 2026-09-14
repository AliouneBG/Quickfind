import os
import sys
import threading
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qf import single


class TestSingleInstance(unittest.TestCase):
    def setUp(self):
        tag = uuid.uuid4().hex[:12]
        self.mutex = f"Local\\qf-test-mutex-{tag}"
        self.event = f"Local\\qf-test-event-{tag}"
        self.opened = []

    def tearDown(self):
        for instance in self.opened:
            instance.close()

    def make(self):
        instance = single.SingleInstance(self.mutex, self.event)
        self.opened.append(instance)
        return instance

    def test_first_instance_is_not_a_duplicate(self):
        self.assertFalse(self.make().already_running)

    def test_second_instance_detects_the_first(self):
        self.make()
        self.assertTrue(self.make().already_running)

    def test_third_instance_also_detects(self):
        self.make()
        self.make()
        self.assertTrue(self.make().already_running)

    def test_closing_releases_the_name(self):
        first = single.SingleInstance(self.mutex, self.event)
        first.close()
        second = self.make()
        self.assertFalse(second.already_running,
                         "a closed instance must not keep holding the mutex")

    def test_signal_reaches_the_listener(self):
        primary = self.make()
        woken = threading.Event()
        primary.listen(lambda: woken.set())

        second = self.make()
        self.assertTrue(second.already_running)
        self.assertTrue(second.signal_existing())
        self.assertTrue(woken.wait(timeout=5), "listener was never woken")

    def test_signal_can_repeat(self):
        primary = self.make()
        count = []
        done = threading.Event()

        def bump():
            count.append(1)
            if len(count) >= 2:
                done.set()

        primary.listen(bump)
        second = self.make()
        second.signal_existing()
        # The event auto-resets, so a second signal must wake the listener again.
        for _ in range(40):
            if count:
                break
            threading.Event().wait(0.05)
        second.signal_existing()
        self.assertTrue(done.wait(timeout=5), f"only {len(count)} wakeups")

    def test_signal_without_listener_is_harmless(self):
        instance = single.SingleInstance(self.mutex, self.event + "-absent")
        self.opened.append(instance)
        self.assertFalse(instance.signal_existing())

    def test_close_is_idempotent(self):
        instance = single.SingleInstance(self.mutex, self.event)
        instance.close()
        instance.close()

    def test_listener_stops_after_close(self):
        primary = self.make()
        hits = []
        primary.listen(lambda: hits.append(1))
        primary.close()
        threading.Event().wait(0.3)
        before = len(hits)
        threading.Event().wait(0.8)
        self.assertEqual(len(hits), before, "listener kept running after close")


if __name__ == "__main__":
    unittest.main(verbosity=2)
