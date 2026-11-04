import os
import tempfile
import time
import unittest

from penny.memory import Memory
from penny.watches import check_watches, find_watch_product


def product(price="99.90", site="magnit", pid="42", name="Молоко 1 л"):
    return {"site": site, "id": pid, "name": name, "price": price, "store": "Магнит",
            "quantity": 3, "card": False, "checked": "01.11.2026 03:00 МСК"}


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, user_id, text, **kwargs):
        self.sent.append((user_id, text))


class WatchCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import penny.store as store
        self.saved_cooldowns = dict(store._site_cooldown_until)
        store._site_cooldown_until.clear()
        self.folder = tempfile.TemporaryDirectory()
        self.memory = Memory(os.path.join(self.folder.name, "penny.sqlite3"))

    def tearDown(self):
        import penny.store as store
        store._site_cooldown_until.clear()
        store._site_cooldown_until.update(self.saved_cooldowns)
        self.folder.cleanup()

    def test_find_watch_product_matches_id_and_name(self):
        self.assertEqual(find_watch_product([product()],
                                            {"site": "magnit", "product_key": "42"})["id"], "42")
        dixy = {"site": "dixy", "id": None, "name": "Вино", "price": "1.00"}
        watch = {"site": "dixy", "product_key": "вино"}
        self.assertIs(find_watch_product([dixy], watch), dixy)
        self.assertIsNone(find_watch_product([product(pid="999")],
                                             {"site": "magnit", "product_key": "42"}))

    async def test_check_watches_alerts_each_watcher_once(self):
        self.memory.save_watch(7, product())
        self.memory.save_watch(8, product())
        bot = FakeBot()

        async def lookup(site, query, proxy):
            return {"products": [product("89.90")]}

        first = await check_watches(bot, self.memory, "proxy", lookup=lookup)
        self.assertEqual(first, {"checked": 1, "drops": 2})
        self.assertEqual(len(bot.sent), 2)
        self.assertIn("89,90", bot.sent[0][1])
        self.assertIn("минимум за всё время", bot.sent[0][1])
        second = await check_watches(bot, self.memory, "proxy", lookup=lookup)
        self.assertEqual(second["drops"], 0)
        self.assertEqual(len(self.memory.history("magnit", "42")), 2)

    async def test_check_watches_skips_cooling_store(self):
        import penny.store as store
        store._site_cooldown_until["magnit"] = time.time() + 600
        self.memory.save_watch(7, product())
        bot = FakeBot()
        called = []

        async def lookup(site, query, proxy):
            called.append(site)
            return {"products": []}

        result = await check_watches(bot, self.memory, "proxy", lookup=lookup)
        self.assertEqual(result, {"checked": 0, "drops": 0})
        self.assertEqual(called, [])

    async def test_check_watches_not_found_records_nothing(self):
        self.memory.save_watch(7, product())
        bot = FakeBot()

        async def lookup(site, query, proxy):
            return {"products": [product(pid="999")]}

        result = await check_watches(bot, self.memory, "proxy", lookup=lookup)
        self.assertEqual(result, {"checked": 1, "drops": 0})
        self.assertEqual(bot.sent, [])
        self.assertEqual(self.memory.history("magnit", "42"), [])
        self.assertIsNotNone(self.memory.all_watches()[0]["checked_at"])


if __name__ == "__main__":
    unittest.main()
