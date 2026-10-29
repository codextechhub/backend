"""The shared cache serves on when Redis is down (``core.cache``).

The Redis here is a port nothing listens on, on this machine, so every call
fails to connect the way a dead Redis does.
"""
from django.test import SimpleTestCase

from core.cache import ResilientRedisCache

DEAD = "redis://127.0.0.1:1/0"


class UnreachableRedisTests(SimpleTestCase):
    def setUp(self):
        self.cache = ResilientRedisCache(DEAD, {
            "OPTIONS": {"socket_connect_timeout": 0.2, "socket_timeout": 0.2},
        })

    def test_a_read_is_a_miss(self):
        with self.assertLogs("core.cache", "WARNING"):
            self.assertEqual(self.cache.get("throttle_login_1.2.3.4", []), [])
            self.assertEqual(self.cache.get_many(["a", "b"]), {})
            self.assertFalse(self.cache.has_key("a"))

    def test_a_write_is_dropped(self):
        with self.assertLogs("core.cache", "WARNING"):
            self.cache.set("throttle_login_1.2.3.4", [1.0], 60)
            self.cache.delete("throttle_login_1.2.3.4")

    def test_a_cooldown_lets_the_action_through(self):
        """A missing cooldown asks a provider again; a refused one would never ask."""
        with self.assertLogs("core.cache", "WARNING"):
            self.assertTrue(self.cache.add("payments:public-verify:ref", 1, 30))
