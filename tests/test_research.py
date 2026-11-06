import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch
from penny.research import merge, research
from penny.service import Model
from penny.store import normalize, unit_price, product_line, StateError
import penny.store as store_state


def result(name='Порошок 2,4кг'):
    return normalize({'items': [{'id': 1, 'name': name, 'price': 39999}]}, 'порошок')


def magnit_result(name='Порошок 2,4кг'):
    return normalize({'items': [{'id': 5, 'name': name, 'price': 44999}]}, 'порошок', 'magnit')


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._cooldowns = (
            store_state._dixy_captcha_until, store_state._dixy_fail_streak,
            dict(store_state._site_cooldown_until), dict(store_state._site_fail_streaks),
        )
        for site in store_state.DEFAULT_SITES:
            store_state.reset_site_cooldown(site)

    def tearDown(self):
        store_state._dixy_captcha_until, store_state._dixy_fail_streak = self._cooldowns[:2]
        store_state._site_cooldown_until.clear()
        store_state._site_cooldown_until.update(self._cooldowns[2])
        store_state._site_fail_streaks.clear()
        store_state._site_fail_streaks.update(self._cooldowns[3])

    async def test_cancelling_research_cleans_up_every_store(self):
        started, cancelled = set(), set()
        ready = asyncio.Event()
        async def execute(site, call, *, timeout):
            started.add(site)
            if len(started) == len(store_state.DEFAULT_SITES):
                ready.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.add(site)
        model = AsyncMock()
        with patch('penny.research.run_store', side_effect=execute):
            task = asyncio.create_task(research(model, 'query', 'request', 'proxy'))
            await asyncio.wait_for(ready.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(cancelled, set(store_state.DEFAULT_SITES))
        model.followups.assert_not_awaited()

    async def test_round_timeout_preserves_completed_store_and_cleans_up(self):
        cancelled = set()
        async def execute(site, call, *, timeout):
            if site == 'cosmetic':
                return result()
            try:
                await asyncio.Future()
            finally:
                cancelled.add(site)
        model = AsyncMock()
        model.followups.return_value = []
        with patch('penny.research.run_store', side_effect=execute), \
                patch('penny.research.STORE_DEADLINE_S', .02), \
                self.assertLogs('penny', level='ERROR'):
            combined = await research(model, 'query', 'request', 'proxy')
        self.assertEqual(combined['sites'], ['cosmetic'])
        self.assertTrue(combined['incomplete'])
        self.assertEqual(cancelled, set(store_state.DEFAULT_SITES) - {'cosmetic'})
        self.assertEqual(len(combined['notes']), 4)

    def test_units_and_ambiguity(self):
        self.assertEqual(str(round(unit_price(result()['products'][0])[0], 2)), '166.66')
        self.assertIsNone(unit_price(result('Порошок 2 x 400г')['products'][0]))
        self.assertIsNone(unit_price(result('Порошок 500г + 100г')['products'][0]))
        self.assertIn('20,00 ₽/стирку', product_line(result('Концентрат 500г 20 стирок')['products'][0]))

    def test_merge_keeps_both_stores(self):
        first, second = result(), magnit_result()
        merged = merge([{'cosmetic': first, 'magnit': second}])
        self.assertEqual(len(merged['products']), 2)
        self.assertEqual([p['store'] for p in merged['products']], ['М.Косметик', 'Магнит'])
        self.assertEqual(merged['sites'], ['cosmetic', 'magnit'])
        self.assertEqual(merged['site_urls']['magnit'], second['url'])

    def test_merge_deduplicates_within_site(self):
        first, second = result(), result()
        first['checked'] = 'first'
        second['checked'] = 'second'
        merged = merge([{'cosmetic': first}, {'cosmetic': second}])
        self.assertEqual(len(merged['products']), 1)
        self.assertEqual(merged['products'][0]['checked'], 'first')

    def test_merge_interleaves_stores_for_the_cap(self):
        cosmetic = [normalize({'items': [{'id': 1, 'name': 'A', 'price': 39999}]}, 'порошок'),
                    normalize({'items': [{'id': 2, 'name': 'B', 'price': 39999}]}, 'порошок'),
                    normalize({'items': [{'id': 3, 'name': 'C', 'price': 39999}]}, 'порошок')]
        magnit = [normalize({'items': [{'id': 5, 'name': 'M1', 'price': 44999}]}, 'порошок', 'magnit'),
                  normalize({'items': [{'id': 6, 'name': 'M2', 'price': 44999}]}, 'порошок', 'magnit')]
        merged = merge([{'cosmetic': cosmetic[0], 'magnit': magnit[0]},
                        {'cosmetic': cosmetic[1]},
                        {'cosmetic': cosmetic[2], 'magnit': magnit[1]}])
        self.assertEqual([p['store'] for p in merged['products']],
                         ['М.Косметик', 'Магнит', 'М.Косметик', 'М.Косметик', 'Магнит'])

    async def test_plan_limit_and_duplicates(self):
        model = Model('fake', 'fake')
        model.ask = AsyncMock(return_value=({'queries': ['порошок', 'Tide']}, 'stop'))
        self.assertEqual(await model.followups('порошок', 'порошок', result()), ['Tide'])
        model.ask.return_value = ({'queries': ['a', 'b', 'c']}, 'stop')
        with self.assertRaises(ValueError):
            await model.followups('порошок', 'порошок', result())

    async def test_followup_failure_preserves_results(self):
        model = Model('fake', 'fake')
        model.followups = AsyncMock(return_value=['Tide'])
        calls = []
        def lookup(site, query, proxy, sort='popularity'):
            calls.append((site, query))
            if query == 'Tide':
                raise ValueError('failed')
            return result()
        async def inline(fn, *args):
            return fn(*args)
        with patch('penny.research.asyncio.to_thread', side_effect=inline), self.assertLogs('penny', level='ERROR'):
            combined = await research(model, 'порошок', 'порошок', 'fake', lookup)
        self.assertCountEqual(calls[:5],
                              [(site, 'порошок') for site in ('cosmetic', 'magnit', 'dixy', '5ka', 'lenta')])
        self.assertCountEqual(calls[5:],
                              [(site, 'Tide') for site in ('cosmetic', 'magnit', 'dixy')])
        self.assertTrue(combined['incomplete'])
        self.assertEqual(len(combined['products']), 5)
        self.assertEqual(len(combined['notes']), 3)

    async def test_magnit_state_error_is_graceful(self):
        model = Model('fake', 'fake')
        model.followups = AsyncMock(return_value=[])
        def lookup(site, query, proxy, sort='popularity'):
            if site == 'magnit':
                raise StateError('Файл cookie не найден')
            return result()
        async def inline(fn, *args):
            return fn(*args)
        with patch('penny.research.asyncio.to_thread', side_effect=inline), self.assertLogs('penny', level='ERROR'):
            combined = await research(model, 'порошок', 'порошок', 'fake', lookup)
        self.assertEqual(combined['sites'], ['cosmetic', 'dixy', '5ka', 'lenta'])
        self.assertEqual([n.split(':')[0] for n in combined['notes']], ['Магнит'])
        self.assertEqual(len(combined['products']), 4)
        self.assertTrue(combined['incomplete'])
        self.assertIn('Магнит', combined['notes'][0])
        self.assertIn('cookie', combined['notes'][0])

    async def test_all_stores_failing_raises(self):
        model = Model('fake', 'fake')
        model.followups = AsyncMock(return_value=[])
        def lookup(site, query, proxy, sort='popularity'):
            raise ValueError('down')
        async def inline(fn, *args):
            return fn(*args)
        with patch('penny.research.asyncio.to_thread', side_effect=inline), self.assertLogs('penny', level='ERROR'):
            with self.assertRaises(ValueError):
                await research(model, 'порошок', 'порошок', 'fake', lookup)

    async def test_failed_store_notes_deduplicate_across_rounds(self):
        model = Model('fake', 'fake')
        model.followups = AsyncMock(return_value=['Tide'])
        def lookup(site, query, proxy, sort='popularity'):
            if site == 'dixy':
                raise ValueError('down')
            return result()
        async def inline(fn, *args):
            return fn(*args)
        with patch('penny.research.asyncio.to_thread', side_effect=inline), self.assertLogs('penny', level='ERROR'):
            combined = await research(model, 'порошок', 'порошок', 'fake', lookup)
        self.assertEqual([n.split(':')[0] for n in combined['notes']], ['Дикси'])

    async def test_store_in_cooldown_is_explained_not_hidden(self):
        import penny.store as store
        saved = (dict(store._site_cooldown_until), dict(store._site_fail_streaks),
                 store._dixy_captcha_until, store._dixy_fail_streak)
        try:
            store._site_cooldown_until['5ka'] = time.time() + 600
            model = Model('fake', 'fake')
            model.followups = AsyncMock(return_value=[])
            def lookup(site, query, proxy, sort='popularity'):
                return result()
            async def inline(fn, *args):
                return fn(*args)
            with patch('penny.research.asyncio.to_thread', side_effect=inline):
                combined = await research(model, 'порошок', 'порошок', 'fake', lookup)
            self.assertNotIn('5ka', combined['sites'])
            self.assertIn('Пятёрочка', combined['notes'][0])
            self.assertIn('пауза', combined['notes'][0])
            self.assertTrue(combined['incomplete'])
            self.assertEqual(combined['captcha_sites'], [])
        finally:
            store._site_cooldown_until.clear()
            store._site_cooldown_until.update(saved[0])
            store._site_fail_streaks.clear()
            store._site_fail_streaks.update(saved[1])
            store._dixy_captcha_until, store._dixy_fail_streak = saved[2], saved[3]

    async def test_captcha_block_is_flagged_for_the_chat_button(self):
        import penny.store as store
        from penny.store import CaptchaBlocked
        saved = (store._dixy_captcha_until, store._dixy_fail_streak,
                 dict(store._site_cooldown_until))
        try:
            model = Model('fake', 'fake')
            model.followups = AsyncMock(return_value=[])
            def lookup(site, query, proxy, sort='popularity'):
                if site == 'dixy':
                    raise CaptchaBlocked('капча')
                return result()
            async def inline(fn, *args):
                return fn(*args)
            with patch('penny.research.asyncio.to_thread', side_effect=inline), \
                    self.assertLogs('penny', level='ERROR'):
                combined = await research(model, 'порошок', 'порошок', 'fake', lookup)
            self.assertEqual(combined['captcha_sites'], ['dixy'])
            self.assertIn('Дикси', combined['notes'][0])
        finally:
            store._dixy_captcha_until, store._dixy_fail_streak = saved[0], saved[1]
            store._site_cooldown_until.clear()
            store._site_cooldown_until.update(saved[2])

    async def test_service_block_is_explained_and_pauses_the_store(self):
        import penny.store as store
        from penny.store import ServiceBlocked
        model = Model('fake', 'fake')
        model.followups = AsyncMock(return_value=[])
        def lookup(site, query, proxy, sort='popularity'):
            if site == '5ka':
                raise ServiceBlocked('block page')
            return result()
        async def inline(fn, *args):
            return fn(*args)
        with patch('penny.research.asyncio.to_thread', side_effect=inline), \
                self.assertLogs('penny', level='ERROR'):
            combined = await research(model, 'порошок', 'порошок', 'fake', lookup)
        self.assertTrue(store.site_cooldown_active('5ka'))
        self.assertIn('блокирует', combined['notes'][0])
        self.assertNotIn('5ka', combined['sites'])

    async def test_lenta_block_is_explained_without_immediate_cooldown(self):
        import penny.store as store
        from penny.store import LentaBlocked
        model = Model('fake', 'fake')
        model.followups = AsyncMock(return_value=[])

        def lookup(site, query, proxy, sort='popularity'):
            if site == 'lenta':
                raise LentaBlocked('blocked', 403)
            return result()

        async def inline(fn, *args):
            return fn(*args)

        with patch('penny.research.asyncio.to_thread', side_effect=inline), \
                self.assertLogs('penny', level='ERROR'):
            combined = await research(model, 'порошок', 'порошок', 'fake', lookup)
        self.assertFalse(store.site_cooldown_active('lenta'))
        self.assertIn('ограничил', combined['notes'][0])
        self.assertNotIn('lenta', combined['sites'])

    async def test_connect_timeout_is_retried_once(self):
        model = Model('fake', 'fake')
        model.followups = AsyncMock(return_value=[])
        class ConnectTimeout(Exception):
            pass
        calls = {}
        def lookup(site, query, proxy, sort='popularity'):
            calls[site] = calls.get(site, 0) + 1
            if site == 'cosmetic' and calls[site] == 1:
                raise ConnectTimeout()
            return result()
        async def inline(fn, *args):
            return fn(*args)
        with patch('penny.research.asyncio.to_thread', side_effect=inline), \
                patch('penny.research.time.sleep'), self.assertLogs('penny'):
            combined = await research(model, 'порошок', 'порошок', 'fake', lookup)
        self.assertEqual(calls, {'cosmetic': 2, 'magnit': 1, 'dixy': 1, '5ka': 1, 'lenta': 1})
        self.assertEqual(len(combined['products']), 5)

    async def test_no_regex_postfilter_model_decides(self):
        model = Model('fake', 'fake')
        model.ask = AsyncMock(return_value=({'items': [
            {'show': True, 'section': 'Похожие'},
            {'show': True, 'section': 'Гипоаллергенные'}], 'conclusion': '', 'strict': False}, 'stop'))
        found = normalize({'items': [
            {'name': 'Порошок для чувствительной кожи 3кг', 'price': 44999},
            {'name': 'Порошок гипоаллергенный 2.4кг', 'price': 39999}]}, 'порошок')
        answer = await model.select('порошок', found, 'Гипоаллергенный порошок')
        # Code preserves model sections verbatim; prompt guides sensitive->later section, gel->other section.
        self.assertIn('для чувствительной кожи', answer)
        self.assertIn('Порошок гипоаллергенный', answer)
