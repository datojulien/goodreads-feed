# Reading feeds

The Hardcover adapter reads Julien's **public** reading journal through the official
GraphQL API and keeps the existing Atom feed paths:

- `cleaned_goodreads_twitter.xml` (280 characters per post)
- `cleaned_goodreads_threads.xml` (500 characters per post)

The legacy filenames, feed IDs and retained Goodreads entry IDs remain stable so
existing subscribers can keep their URLs. New entries link to Hardcover. The old
Goodreads generator is retained for reference; the proposed workflow uses
`hardcover_to_ifttt.py`. The NYT files and `cleaned_goodreads.xml` are unrelated.

## Behavior

- First successful run: record a baseline, leaving both feed files byte-for-byte
  unchanged. Imported books, reviews and historical finishes are not announced.
- Later runs: process new public started, progress, finished, rating and review
  journal events. Progress revisions are detected even when Hardcover edits the
  same daily journal entry in place. Zero/100-percent progress uses the separate
  start/finish announcement instead.
- Use the selected edition title/author when available. Spoiler review text,
  private journal entries and books outside the public library are excluded.
- Preserve each journal revision's stable entry ID and deduplicate repeated
  polls. Duplicate status/read start or finish events produce one announcement
  per book per UTC day. A rating and review received in one poll are combined.
- Keep the latest 200 feed entries. Save only public journal fingerprints and
  publication IDs in `hardcover_feed_state.json`, never the token or private notes.
- Fetch every page before writing anything. API failures, schema errors,
  incomplete library snapshots and invalid XML leave existing feeds/state intact.
  Both feeds are prepared before replacement, and state is replaced last so a
  partially interrupted local write can be retried without duplicate entry IDs.

Hardcover becomes the tracker for future updates. There is no continuous
Goodreads-to-Hardcover synchronization in this repository.

## Setup and activation

The approved `HARDCOVER_API_TOKEN` Actions secret is a restricted read-only key
named `GitHub reading RSS`, expiring **28 October 2027**. It has catalog data,
public library, public journal, public profile and social-read scopes. The adapter
only calls read queries. It cannot edit the library or catalog.

The migration is staged behind the repository variable
`HARDCOVER_FEED_ENABLED`. **Leave it unset until live activation is approved.**
The workflow retains the hourly schedule; a scheduled feed job is skipped while
the variable is unset. Pull requests run offline tests without API secrets.

1. Run the workflow manually on the feature branch with `mode=preview`. Inspect
   the downloadable `hardcover-feed-preview` artifact and its counts. Preview
   mode does not commit or replace live feeds.
2. Resolve imported-history corrections during this setup period, or explicitly
   decide which books should stay unchanged. The feed adapter does not repair
   the earlier import's mismatches, ratings or reread history.
3. After approval, merge the migration and run `mode=bootstrap` on `main`. This
   records the current journal baseline without posting existing history.
4. Set `HARDCOVER_FEED_ENABLED=true`. Later scheduled runs append new activity.
   Publishing mode refuses to run from a feature branch or without that variable.

To stop updates, unset that variable. Existing XML feeds remain available. The
push uses the standard Actions token with contents-write permission, has no
force-push step, and fails safely if another commit has advanced `main`.

The first baseline must be complete. Do not delete its state file during normal
operation. An explicit bootstrap deliberately suppresses all activity up to its
new baseline time and should only be used during setup or an approved reset.

## Local validation

Python 3.11+ and its standard library are sufficient for the new adapter/tests:

```sh
python -m unittest discover -s tests -v
```

Provide `HARDCOVER_API_TOKEN` through the environment, then create a local preview:

```sh
python hardcover_to_ifttt.py --preview-dir /tmp/hardcover-preview
```

For a second local poll, run a working copy of the generator from that preview
directory, or copy its baseline state into a disposable test checkout. Do not
copy tokens into the repository. Local previews and bootstrap operations do not
change Hardcover account data.

## Validation limits

The live read-only API check verified all 921 public library books and existing
progress, finish and review journal payloads. The account has no existing start
journal entry, so started events are tested with the officially documented
`status_currently_reading` format. No artificial account activity was created.
An actual future start/finish/review remains the final end-to-end publishing
check after activation; fixture tests are not a claim that a social post was sent.

Polling sees the latest revision of a daily progress entry. Multiple intermediate
updates between polls may be consolidated into the latest percentage. Historical
review edits and backdated reading-date corrections are suppressed instead of
being reposted as new reading. Correcting a current status can create fresh
activity, which is why the bootstrap should follow the import cleanup decision.

Primary API references:

- https://docs.hardcover.app/api/graphql/schemas/readingjournals/
- https://docs.hardcover.app/api/guides/updatingreadingjournal/
- https://docs.hardcover.app/api/getting-started/
