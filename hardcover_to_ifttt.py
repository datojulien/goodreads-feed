#!/usr/bin/env python3
"""Read public Hardcover journals and append new activity to the existing Atom feeds.

The first run only records a baseline. This program never writes to Hardcover.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import html
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

UTC = dt.timezone.utc
ATOM = 'http://www.w3.org/2005/Atom'
ET.register_namespace('', ATOM)
OUTPUTS = {'tw': ('cleaned_goodreads_twitter.xml', 280), 'th': ('cleaned_goodreads_threads.xml', 500)}
STATE_FILE = 'hardcover_feed_state.json'
EXPECTED_USERNAME = 'julienpierre'
API_URL = 'https://api.hardcover.app/v1/graphql'
JOURNAL_FIELDS = 'id user_id book_id event action_at created_at updated_at journal_date privacy_setting_id metadata'
LIBRARY_FIELDS = '''id book_id privacy_setting_id rating review_has_spoilers
 book { id title slug cached_contributors }
 edition { id title pages audio_seconds cached_contributors }
 user_book_reads { id started_at finished_at progress progress_pages progress_seconds }'''
STARTED = {'status_currently_reading', 'user_book_read_started'}
FINISHED = {'status_read', 'user_book_read_finished'}
SUPPORTED = STARTED | FINISHED | {'progress_updated', 'reviewed', 'rated'}


class FeedError(RuntimeError):
    pass


def instant(value):
    if not isinstance(value, str) or not value:
        raise FeedError('A required source timestamp is missing.')
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise FeedError('A source timestamp is invalid.') from error
    return result.replace(tzinfo=result.tzinfo or UTC).astimezone(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat(timespec='microseconds')


def sql_time(value):
    # reading_journals.updated_at is a UTC timestamp without a timezone.
    return value.astimezone(UTC).replace(tzinfo=None).isoformat(timespec='microseconds')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def signature(row):
    # Likes and other timestamp-only changes must not generate a new announcement.
    return digest({k: row.get(k) for k in ('event', 'book_id', 'action_at', 'journal_date', 'metadata', 'privacy_setting_id')})


class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, value):
        self.parts.append(value)

    def handle_starttag(self, tag, attrs):
        if tag in ('p', 'br', 'div', 'li'):
            self.parts.append(' ')


def plain(value):
    parser = Text()
    parser.feed(value or '')
    return re.sub(r'\s+', ' ', html.unescape(''.join(parser.parts))).strip()


def slate_text(value):
    if isinstance(value, list):
        return ' '.join(slate_text(x) for x in value)
    if isinstance(value, dict):
        if 'text' in value:
            return value['text']
        return slate_text(value.get('document', value.get('children', [])))
    return ''


def clip(value, limit):
    if limit <= 0:
        return ''
    return value if len(value) <= limit else value[:max(0, limit - 1)].rstrip() + '…'


def book_details(record):
    book = record.get('book') or {}
    edition = record.get('edition') or {}
    title = re.sub(r'\s*\(.*?\)', '', edition.get('title') or book.get('title') or '').strip()
    authors = []
    for contribution in edition.get('cached_contributors') or book.get('cached_contributors') or []:
        if contribution.get('contributor_role_id') == 1 or contribution.get('contribution') == 'Author':
            name = (contribution.get('author') or {}).get('name')
            if name and name not in authors:
                authors.append(name)
    slug = book.get('slug')
    if not title or not authors or not slug or not re.fullmatch(r'[a-zA-Z0-9_-]+', slug):
        raise FeedError('A public book is missing usable title, author or link metadata.')
    return title, ', '.join(authors), 'https://hardcover.app/books/' + slug


def event_from(row, record, baseline):
    event = row['event']
    action = instant(row['action_at'])
    updated = instant(row['updated_at'])
    metadata = row.get('metadata')
    if not isinstance(metadata, dict):
        raise FeedError('A supported journal entry has invalid metadata.')
    if event == 'progress_updated':
        # A daily progress journal can be edited in place after its initial action_at.
        # Only an update on/after the baseline day can be a new progress announcement.
        day = str(row.get('journal_date') or action.date())
        if day < baseline.date().isoformat() or updated <= baseline:
            return None
    elif action <= baseline:
        return None  # Includes imports and later edits of old reviews/reading dates.

    kind = 'started' if event in STARTED else 'finished' if event in FINISHED else 'progress' if event == 'progress_updated' else 'reviewed'
    reads = sorted(record.get('user_book_reads') or [], key=lambda x: int(x['id']), reverse=True)
    latest = reads[0] if reads else {}
    supplied_date = metadata.get('started_at' if kind == 'started' else 'finished_at')
    if kind == 'finished' and not supplied_date:
        supplied_date = latest.get('finished_at')
    if kind in ('started', 'finished') and supplied_date and str(supplied_date)[:10] < baseline.date().isoformat():
        return None  # A historical date correction is not a new completion.

    percent = None
    if kind == 'progress':
        percent = metadata.get('progress')
        if percent is None:
            edition = record.get('edition') or {}
            for current, total in [('progress_pages', 'pages'), ('progress_seconds', 'audio_seconds')]:
                if metadata.get(current) is not None and edition.get(total):
                    percent = 100 * float(metadata[current]) / float(edition[total])
                    break
        try:
            percent = float(percent)
        except (TypeError, ValueError) as error:
            raise FeedError('A progress event has no usable percentage or total.') from error
        if not math.isfinite(percent) or not 0 <= percent <= 100:
            raise FeedError('A progress percentage is outside its supported range.')
        if percent in (0, 100):
            return None  # Starting/finishing already has its own announcement.

    title, author, url = book_details(record)
    review = ''
    if event == 'reviewed' and not record.get('review_has_spoilers'):
        review = plain(metadata.get('review_raw') or slate_text(metadata.get('review_slate')) or metadata.get('review'))
    rating = metadata.get('rating', record.get('rating'))
    if rating is not None:
        try:
            rating = float(rating)
        except (TypeError, ValueError) as error:
            raise FeedError('A source rating is invalid.') from error
        if not math.isfinite(rating) or not 0 <= rating <= 5:
            raise FeedError('A source rating is outside its supported range.')
    if event == 'rated' and not rating:
        return None
    # Date-only reading events can be represented at the end of that day. Do not
    # publish a future Atom timestamp when their journal row was updated earlier.
    when = updated if kind == 'progress' else min(action, updated)
    semantic_key = f'{kind}:{row["book_id"]}:{action.date()}' if kind in ('started', 'finished') else f'{event}:{row["id"]}:{signature(row)}'
    revision = digest([signature(row), iso(updated)])[:20]
    return {'id': f'urn:hardcover:journal:{row["id"]}:{revision}', 'kind': kind,
            'source_event': event, 'book_id': row['book_id'], 'semantic_key': semantic_key,
            'title': title, 'author': author, 'url': url, 'rating': rating,
            'review': review, 'percent': percent, 'when': iso(when)}


def plan_update(state, journals, library, account, now):
    library = {str(r['book_id']): r for r in library if r.get('privacy_setting_id') == 1}
    public = [r for r in journals if r.get('privacy_setting_id') == 1 and r.get('user_id') == account['id']]
    public.sort(key=lambda r: (instant(r['updated_at']), int(r['id'])))
    if state is None:
        return {'version': 1, 'account': copy.deepcopy(account), 'baseline_at': iso(now), 'last_poll_at': iso(now),
                'baseline_journal_ids': sorted(str(r['id']) for r in public),
                'observed': {str(r['id']): signature(r) for r in public}, 'published': {}}, []
    if state.get('version') != 1 or state.get('account') != account:
        raise FeedError('The saved feed state belongs to a different account or format.')
    baseline = instant(state['baseline_at'])
    result = copy.deepcopy(state)
    baseline_ids = set(result.setdefault('baseline_journal_ids', list(state['observed'])))
    candidates = []
    for row in public:
        fingerprint = signature(row)
        if result['observed'].get(str(row['id'])) == fingerprint:
            continue
        result['observed'][str(row['id'])] = fingerprint
        if row['event'] not in SUPPORTED or str(row['book_id']) not in library:
            continue
        if str(row['id']) in baseline_ids and row['event'] != 'progress_updated':
            continue  # Existing history stays suppressed even with end-of-day dates.
        candidate = event_from(row, library[str(row['book_id'])], baseline)
        if candidate:
            candidates.append(candidate)
    review_days = {(e['book_id'], instant(e['when']).date()) for e in candidates if e['source_event'] == 'reviewed'}
    events = []
    for event in sorted(candidates, key=lambda e: (e['when'], e['id'])):
        if event['source_event'] == 'rated' and (event['book_id'], instant(event['when']).date()) in review_days:
            continue
        if event['semantic_key'] not in result['published']:
            events.append(event)
            result['published'][event['semantic_key']] = event['id']
    result['last_poll_at'] = iso(now)
    return result, events


def format_post(event, limit):
    title, author, url = event['title'], event['author'], event['url']
    tag = '#' + re.sub(r'[^A-Za-z0-9]+', '', author)
    rating = f'⭐️ Rated: {event["rating"]:g}/5\n' if event.get('rating') else ''
    if event['kind'] == 'started':
        prefix, suffix = '🚀 Starting ', f'\n🔗 Follow my progress: {url}\n🏷️ {tag} #NowReading'
    elif event['kind'] == 'finished':
        prefix, suffix = '📘 Finished ', f'\n🔗 {url}\n🏷️ {tag}'
    elif event['kind'] == 'progress':
        percent = event['percent']
        filled = max(0, min(20, math.floor(percent / 5)))
        bar = '▓' * filled + '░' * (20 - filled) + f'  {percent:.2f}%'
        prefix, suffix = f'📈 I’ve read {percent:g}% of ', f'\n{bar}\n🔗 Progress: {url}\n🏷️ {tag} #ReadingProgress'
    else:
        prefix, suffix = '📚 ', f'\n{rating}🏷️ {tag}\n🔗 {url}'
    # Keep the link and familiar structure; shorten unusually long titles/authors first.
    available = limit - len(prefix) - len(suffix) - len('“” by ')
    author = clip(author, max(12, min(len(author), available // 3)))
    title = clip(title, max(1, available - len(author)))
    body = prefix + f'“{title}” by {author}' + suffix
    if len(body) > limit:
        # An extremely long hashtag is expendable; the book link is retained.
        body = body.replace(tag, clip(tag, max(0, len(tag) - (len(body) - limit))))
    if event['kind'] == 'reviewed' and event.get('review'):
        room = limit - len(body) - len('\n📝 “”')
        if room >= 12:
            body += '\n📝 “' + clip(event['review'], room) + '”'
    if len(body) > limit:
        raise FeedError('A feed post cannot fit its platform character limit.')
    return body


def feed_bytes(original, events, platform):
    try:
        root = ET.fromstring(original)
    except ET.ParseError as error:
        raise FeedError('An existing feed is invalid; no outputs will be replaced.') from error
    if root.tag != f'{{{ATOM}}}feed' or any(root.find(f'{{{ATOM}}}{field}') is None for field in ('id', 'title', 'updated')):
        raise FeedError('An existing feed is not a valid Atom feed.')
    if not events:
        return original  # Baseline and unchanged runs preserve the exact original bytes.
    present = {e.findtext(f'{{{ATOM}}}id') for e in root.findall(f'{{{ATOM}}}entry')}
    added = False
    for event in events:
        guid = event['id'] + '-' + platform
        if guid in present:
            continue
        entry = ET.Element(f'{{{ATOM}}}entry')
        for name, value in [('id', guid), ('title', f'Julien {event["kind"]}: {event["title"]}'), ('updated', event['when'])]:
            ET.SubElement(entry, f'{{{ATOM}}}{name}').text = value
        ET.SubElement(entry, f'{{{ATOM}}}content', {'type': 'text'}).text = format_post(event, OUTPUTS[platform][1])
        ET.SubElement(entry, f'{{{ATOM}}}link', {'href': event['url'], 'rel': 'alternate'})
        root.insert(0, entry)
        present.add(guid)
        added = True
    if not added:
        return original
    # Preserve the existing feed ID, self URL and all retained legacy entry IDs.
    root.find(f'{{{ATOM}}}title').text = f'Julien’s Hardcover → {"Twitter" if platform == "tw" else "Threads"} Feed'
    root.find(f'{{{ATOM}}}updated').text = max(iso(instant(root.findtext(f'{{{ATOM}}}updated'))), *(e['when'] for e in events))
    for old in root.findall(f'{{{ATOM}}}entry')[200:]:
        root.remove(old)
    ET.indent(root)
    result = ET.tostring(root, encoding='utf-8', xml_declaration=True) + b'\n'
    try:
        ET.fromstring(result)
    except ET.ParseError as error:
        raise FeedError('Source content contains characters that cannot be published as XML.') from error
    return result


class Client:
    def __init__(self, token):
        if not token:
            raise FeedError('HARDCOVER_API_TOKEN is required.')
        self.token = token.removeprefix('Bearer ').strip()
        self.last_request = 0.0

    def query(self, text, variables=None):
        # This integration contains query operations only, never mutations.
        if not text.lstrip().startswith('query '):
            raise FeedError('Only named read-only GraphQL queries are supported.')
        delay = 1.1 - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        self.last_request = time.monotonic()
        request = urllib.request.Request(API_URL, data=json.dumps({'query': text, 'variables': variables or {}}).encode(),
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json',
                     'User-Agent': 'datojulien-goodreads-feed/Hardcover-1.0'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            raise FeedError('The Hardcover API request failed; existing feeds and state are unchanged.') from error
        if payload.get('errors') or not isinstance(payload.get('data'), dict):
            raise FeedError('Hardcover returned a GraphQL error; existing feeds and state are unchanged.')
        return payload['data']

    def account(self):
        rows = self.query('query FeedAccount { me { id username } }')['me']
        if len(rows) != 1 or rows[0]['username'] != EXPECTED_USERNAME:
            raise FeedError('The API key is not for the expected Hardcover account.')
        return rows[0]

    def paged(self, table, fields, where, order, cursor):
        result, previous = [], None
        for _ in range(200):
            clause = copy.deepcopy(where)
            if previous is not None:
                clause = {'_and': [clause, cursor(previous)]}
            payload = self.query(f'query FeedPage($where: {table}_bool_exp!) {{ {table}(where:$where, order_by:{order}, limit:200) {{ {fields} }} }}', {'where': clause})
            page = payload[table]
            if not isinstance(page, list):
                raise FeedError('The API returned an invalid page.')
            if not page:
                return result
            if previous is not None and int(page[0]['id']) == int(previous['id']):
                raise FeedError('The API pagination did not advance.')
            result.extend(page)
            previous = page[-1]
            # Continue until an EMPTY page, even if the API caps pages below our limit.
        raise FeedError('The API pagination limit was reached; no partial snapshot will be published.')

    def snapshot(self, account, state, now):
        base = {'user_id': {'_eq': account['id']}, 'privacy_setting_id': {'_eq': 1}}
        library = self.paged('user_books', LIBRARY_FIELDS, base, '{id:asc}', lambda p: {'id': {'_gt': p['id']}})
        count = self.query('query FeedCount($where:user_books_bool_exp!){user_books_aggregate(where:$where){aggregate{count}}}', {'where': base})['user_books_aggregate']['aggregate']['count']
        if len(library) != count or not library:
            raise FeedError('The public library snapshot is incomplete or empty.')
        window = {'_lte': sql_time(now)}
        if state:
            window['_gte'] = sql_time(instant(state['last_poll_at']) - dt.timedelta(minutes=10))
        where = {**base, 'updated_at': window}
        journals = self.paged('reading_journals', JOURNAL_FIELDS, where, '[{updated_at:asc},{id:asc}]',
            lambda p: {'_or': [{'updated_at': {'_gt': p['updated_at']}}, {'updated_at': {'_eq': p['updated_at']}, 'id': {'_gt': p['id']}}]})
        return library, journals


def write_bundle(files):
    staged = []
    try:
        for path, content in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
            staged.append((temp, path))
            with os.fdopen(fd, 'wb') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        # State is supplied last. A crash before its replacement is safely retryable
        # because feed entry IDs also deduplicate already-written entries.
        for temp, path in staged:
            os.replace(temp, path)
    finally:
        for temp, _ in staged:
            if os.path.exists(temp):
                os.unlink(temp)


def run(client, source_dir, output_dir, bootstrap=False, now=None):
    now = now or dt.datetime.now(UTC)
    state_path = source_dir / STATE_FILE
    state = json.loads(state_path.read_text()) if state_path.exists() and not bootstrap else None
    originals = {p: (source_dir / f).read_bytes() for p, (f, _) in OUTPUTS.items()}
    for p in OUTPUTS:
        feed_bytes(originals[p], [], p)  # Validate before reading API or replacing anything.
    account = client.account()
    library, journals = client.snapshot(account, state, now)
    new_state, events = plan_update(state, journals, library, account, now)
    rendered = [(output_dir / OUTPUTS[p][0], feed_bytes(originals[p], events, p)) for p in OUTPUTS]
    rendered.append((output_dir / STATE_FILE, (json.dumps(new_state, sort_keys=True, indent=2) + '\n').encode()))
    write_bundle(rendered)
    report = {'account': account['username'], 'public_library_books': len(library), 'journal_rows_checked': len(journals),
              'baseline_created': state is None, 'new_events': len(events),
              'event_types': {k: sum(e['kind'] == k for e in events) for k in ('started', 'progress', 'finished', 'reviewed')}}
    (output_dir / 'hardcover_feed_report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preview-dir', type=Path, help='Write a preview here; never change repository feeds/state.')
    parser.add_argument('--bootstrap', action='store_true', help='Explicitly reset the baseline and suppress all existing history.')
    parser.add_argument('--token-file', type=Path, help='Local testing only; otherwise use HARDCOVER_API_TOKEN.')
    args = parser.parse_args()
    token = args.token_file.read_text().strip() if args.token_file else os.environ.get('HARDCOVER_API_TOKEN', '')
    source = Path.cwd()
    output = args.preview_dir.resolve() if args.preview_dir else source
    if args.preview_dir and output == source:
        raise FeedError('The preview directory must differ from the repository directory.')
    report = run(Client(token), source, output, args.bootstrap)
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (FeedError, OSError, ValueError, KeyError, TypeError) as error:
        print('Feed update failed: ' + (str(error) if isinstance(error, FeedError) else 'Invalid input or local file operation.'), file=sys.stderr)
        sys.exit(1)
