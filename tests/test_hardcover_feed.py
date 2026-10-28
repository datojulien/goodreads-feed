import copy
import datetime as dt
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET

import hardcover_to_ifttt as h

ACCOUNT = {'id': 190580, 'username': 'julienpierre'}
BASE = dt.datetime(2026, 10, 28, 11, tzinfo=h.UTC)
LATER = BASE + dt.timedelta(hours=1)
REPO = Path(__file__).resolve().parents[1]


def book(book_id=42, privacy=1):
    return {'id': 10, 'book_id': book_id, 'privacy_setting_id': privacy, 'rating': 4.5,
            'review_has_spoilers': False, 'user_book_reads': [],
            'book': {'id': book_id, 'title': 'Example & Book', 'slug': 'example-book',
                     'cached_contributors': [{'author': {'name': 'Example Author'}, 'contributor_role_id': 1}]},
            'edition': {'title': 'Example & Book', 'pages': 320, 'audio_seconds': 3600}}


def journal(event, row_id=1, when=LATER, metadata=None, book_id=42, privacy=1):
    return {'id': row_id, 'user_id': ACCOUNT['id'], 'book_id': book_id, 'event': event,
            'privacy_setting_id': privacy, 'action_at': h.iso(when),
            'created_at': h.sql_time(when), 'updated_at': h.sql_time(when),
            'journal_date': None, 'metadata': metadata or {}}


def initial(rows=None):
    return h.plan_update(None, rows or [], [book()], ACCOUNT, BASE)[0]


class FakeClient:
    def __init__(self, rows=None, library=None, error=False):
        self.rows, self.library, self.error = rows or [], library or [book()], error

    def account(self):
        return ACCOUNT

    def snapshot(self, account, state, now):
        if self.error:
            raise h.FeedError('A simulated API failure')
        return self.library, self.rows


class EventsTest(unittest.TestCase):
    def test_baseline_suppresses_all_existing_history(self):
        rows = [journal('reviewed', metadata={'review_raw': 'Old review'}), journal('progress_updated', 2, metadata={'progress': 4})]
        state, events = h.plan_update(None, rows, [book()], ACCOUNT, BASE)
        self.assertEqual(events, [])
        self.assertEqual(len(state['observed']), 2)

    def test_all_four_required_event_types(self):
        rows = [journal('status_currently_reading', 1),
                journal('progress_updated', 2, metadata={'progress': 58}),
                journal('user_book_read_finished', 3, metadata={'finished_at': '2026-10-28'}),
                journal('reviewed', 4, metadata={'review_raw': 'A clear review <without> unsafe markup.'})]
        state, events = h.plan_update(initial(), rows, [book()], ACCOUNT, LATER)
        self.assertEqual([e['kind'] for e in events], ['started', 'progress', 'finished', 'reviewed'])
        self.assertEqual(events[1]['percent'], 58)
        for event in events:
            for limit in (280, 500):
                post = h.format_post(event, limit)
                self.assertLessEqual(len(post), limit)
                self.assertIn(event['url'], post)
        self.assertIn('⭐️ Rated: 4.5/5', h.format_post(events[3], 500))

    def test_duplicate_poll_emits_nothing(self):
        rows = [journal('status_currently_reading')]
        state, first = h.plan_update(initial(), rows, [book()], ACCOUNT, LATER)
        again, second = h.plan_update(state, rows, [book()], ACCOUNT, LATER + dt.timedelta(hours=1))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(state['published'], again['published'])

    def test_recent_import_with_historical_action_is_not_reposted(self):
        row = journal('reviewed', when=BASE - dt.timedelta(days=300), metadata={'review_raw': 'Imported'})
        row['updated_at'] = h.sql_time(LATER)
        _, events = h.plan_update(initial(), [row], [book()], ACCOUNT, LATER)
        self.assertEqual(events, [])

    def test_backdated_finish_correction_is_not_reposted(self):
        row = journal('user_book_read_finished', metadata={'finished_at': '2014-04-02'})
        _, events = h.plan_update(initial(), [row], [book()], ACCOUNT, LATER)
        self.assertEqual(events, [])

    def test_daily_progress_revision_after_baseline(self):
        row = journal('progress_updated', when=BASE - dt.timedelta(minutes=10), metadata={'progress': 4, 'progress_pages': 15})
        row['journal_date'] = '2026-10-28'
        state = initial([row])
        row['metadata'] = {'progress': 9, 'progress_pages': 30}
        row['updated_at'] = h.sql_time(LATER)
        state, events = h.plan_update(state, [row], [book()], ACCOUNT, LATER)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['percent'], 9)
        self.assertEqual(events[0]['when'], h.iso(LATER))
        row['updated_at'] = h.sql_time(LATER + dt.timedelta(minutes=1))
        self.assertEqual(h.plan_update(state, [row], [book()], ACCOUNT, LATER)[1], [])

    def test_edited_old_progress_is_not_reposted(self):
        row = journal('progress_updated', when=BASE - dt.timedelta(days=1), metadata={'progress': 4})
        row['updated_at'] = h.sql_time(LATER)
        _, events = h.plan_update(initial(), [row], [book()], ACCOUNT, LATER)
        self.assertEqual(events, [])

    def test_private_rows_books_and_other_users_are_excluded(self):
        rows = [journal('reviewed', 1, privacy=3), journal('reviewed', 2, book_id=43), journal('reviewed', 3)]
        rows[2]['user_id'] = 999
        _, events = h.plan_update(initial(), rows, [book(), book(43, 3)], ACCOUNT, LATER)
        self.assertEqual(events, [])

    def test_spoiler_text_is_not_disclosed(self):
        record = book(); record['review_has_spoilers'] = True
        _, events = h.plan_update(initial(), [journal('reviewed', metadata={'review_raw': 'SECRET SPOILER'})], [record], ACCOUNT, LATER)
        self.assertNotIn('SECRET SPOILER', h.format_post(events[0], 500))

    def test_status_and_read_finish_create_one_announcement(self):
        rows = [journal('status_read', 1), journal('user_book_read_finished', 2)]
        self.assertEqual(len(h.plan_update(initial(), rows, [book()], ACCOUNT, LATER)[1]), 1)

    def test_rating_and_review_in_same_poll_are_combined(self):
        rows = [journal('rated', 1, metadata={'rating': 4.5}), journal('reviewed', 2, metadata={'review_raw': 'Good book'})]
        events = h.plan_update(initial(), rows, [book()], ACCOUNT, LATER)[1]
        self.assertEqual(len(events), 1)
        self.assertIn('Good book', h.format_post(events[0], 500))

    def test_pages_fallback_and_zero_and_complete_suppression(self):
        rows = [journal('progress_updated', 1, metadata={'progress_pages': 160}),
                journal('progress_updated', 2, metadata={'progress': 0}),
                journal('progress_updated', 3, metadata={'progress': 100})]
        events = h.plan_update(initial(), rows, [book()], ACCOUNT, LATER)[1]
        self.assertEqual([e['percent'] for e in events], [50])

    def test_bad_progress_fails_instead_of_silently_losing_event(self):
        with self.assertRaises(h.FeedError):
            h.plan_update(initial(), [journal('progress_updated', metadata={'progress': 'invalid'})], [book()], ACCOUNT, LATER)

    def test_long_unicode_titles_and_reviews_fit_both_limits(self):
        record = book(); record['edition']['title'] = 'Étrange title ' * 100
        record['book']['cached_contributors'][0]['author']['name'] = 'Very Long Author Name ' * 40
        events = h.plan_update(initial(), [journal('reviewed', metadata={'review_raw': 'A review ' * 200})], [record], ACCOUNT, LATER)[1]
        for limit in (280, 500):
            self.assertLessEqual(len(h.format_post(events[0], limit)), limit)
            self.assertIn('https://hardcover.app/books/example-book', h.format_post(events[0], limit))

    def test_wrong_account_state_fails(self):
        state = initial(); state['account']['id'] = 999
        with self.assertRaises(h.FeedError):
            h.plan_update(state, [], [book()], ACCOUNT, LATER)


class FileAndApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for filename, _ in h.OUTPUTS.values():
            shutil.copyfile(REPO / filename, self.root / filename)
        self.before = {p.name: p.read_bytes() for p in self.root.iterdir()}

    def tearDown(self):
        self.temp.cleanup()

    def test_bootstrap_preserves_real_legacy_feeds_byte_for_byte(self):
        report = h.run(FakeClient([journal('reviewed')]), self.root, self.root, now=BASE)
        self.assertTrue(report['baseline_created'])
        self.assertEqual(report['new_events'], 0)
        for filename, _ in h.OUTPUTS.values():
            self.assertEqual((self.root / filename).read_bytes(), self.before[filename])

    def test_preview_does_not_modify_repository(self):
        out = self.root / 'preview'
        h.run(FakeClient(), self.root, out, now=BASE)
        for filename, content in self.before.items():
            self.assertEqual((self.root / filename).read_bytes(), content)
        self.assertFalse((self.root / h.STATE_FILE).exists())
        self.assertTrue((out / h.STATE_FILE).exists())

    def test_api_error_leaves_outputs_and_state_unchanged(self):
        h.run(FakeClient(), self.root, self.root, now=BASE)
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with self.assertRaises(h.FeedError):
            h.run(FakeClient(error=True), self.root, self.root, now=LATER)
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_legacy_ids_and_feed_identity_survive_new_events(self):
        original = self.before[h.OUTPUTS['tw'][0]]
        events = h.plan_update(initial(), [journal('status_currently_reading')], [book()], ACCOUNT, LATER)[1]
        result = ET.fromstring(h.feed_bytes(original, events, 'tw'))
        old = ET.fromstring(original)
        self.assertEqual(result.findtext(f'{{{h.ATOM}}}id'), old.findtext(f'{{{h.ATOM}}}id'))
        old_ids = {e.findtext(f'{{{h.ATOM}}}id') for e in old.findall(f'{{{h.ATOM}}}entry')}
        new_ids = {e.findtext(f'{{{h.ATOM}}}id') for e in result.findall(f'{{{h.ATOM}}}entry')}
        self.assertTrue(old_ids < new_ids)
        once = h.feed_bytes(original, events, 'tw')
        self.assertEqual(h.feed_bytes(once, events, 'tw'), once)

    def test_invalid_existing_or_generated_xml_fails(self):
        with self.assertRaises(h.FeedError):
            h.feed_bytes(b'<broken>', [], 'tw')
        events = h.plan_update(initial(), [journal('status_currently_reading')], [book()], ACCOUNT, LATER)[1]
        events[0]['title'] = 'bad\x00title'
        with self.assertRaises(h.FeedError):
            h.feed_bytes(self.before[h.OUTPUTS['tw'][0]], events, 'tw')

    def test_server_row_cap_does_not_truncate_pagination(self):
        class CappedClient(h.Client):
            def __init__(self): self.calls = []
            def query(self, text, variables=None):
                self.calls.append(variables)
                after = variables['where'].get('_and', [{}, {'id': {'_gt': 0}}])[-1].get('id', {}).get('_gt', 0)
                return {'user_books': [{'id': after + 1}] if after < 3 else []}
        client = CappedClient()
        rows = client.paged('user_books', 'id', {}, '{id:asc}', lambda row: {'id': {'_gt': row['id']}})
        self.assertEqual([r['id'] for r in rows], [1, 2, 3])
        self.assertEqual(len(client.calls), 4)


if __name__ == '__main__':
    unittest.main()
