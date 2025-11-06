#!/usr/bin/env python3
"""
goodreads_to_ifttt.py

Fetches Julien’s Goodreads RSS feed, filters it, and outputs two cleaned Atom feeds:
• Twitter (<=280 chars)
• Threads (~500 chars)

Handles:
1. 🚀 Started reading
2. 📈 Progress updates
3. 📚 Finished with review
4. 📘 Finished without review
"""

import re
import sys
import math
import time
import datetime
from datetime import timezone
import feedparser
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from typing import Dict, Any

# ───── CONFIGURATION ─────
SOURCE_FEED = "https://www.goodreads.com/user/updates_rss/14857928"
TWITTER_OUTPUT = "cleaned_goodreads_twitter.xml"
THREADS_OUTPUT = "cleaned_goodreads_threads.xml"
# ─────────────────────────

def clean_parenthetical(title: str) -> str:
    return re.sub(r"\s*\(.*?\)", "", title or "").strip()

def _sanitize_hashtag(s: str) -> str:
    # Remove spaces and punctuation for a simple hashtag
    return re.sub(r"[^A-Za-z0-9]+", "", (s or ""))

def extract_html(entry: Dict[str, Any]) -> str:
    """
    Robustly extract HTML-ish body from a feedparser entry without raising on missing keys.
    Checks `content[0].value|content` → `summary` → `description` → "".
    """
    content_list = entry.get("content")
    if content_list and isinstance(content_list, list):
        first = content_list[0] or {}
        return first.get("value") or first.get("content") or ""
    return entry.get("summary") or entry.get("description") or ""

def parse_reviews(html: str, max_length: int):
    soup = BeautifulSoup(html or "", "html.parser")
    book_tag = soup.find("a", class_="bookTitle")
    author_tag = soup.find("a", class_="authorName")
    raw_title = book_tag.get_text(strip=True) if book_tag else ""
    author = author_tag.get_text(strip=True) if author_tag else ""
    book = clean_parenthetical(raw_title)

    rating = None
    text = soup.get_text("\n", strip=True)
    m = re.search(r"gave (\d+) stars", text)
    if m:
        try:
            rating = int(m.group(1))
        except ValueError:
            rating = None

    snippet = ""
    first_br = soup.find("br")
    if first_br:
        full = ""
        for node in first_br.next_siblings:
            if isinstance(node, str):
                chunk = node.strip()
                if chunk:
                    full += chunk + " "
            elif hasattr(node, "get_text"):
                chunk = node.get_text(" ", strip=True).strip()
                if chunk:
                    full += chunk + " "
            if len(full) > max_length * 2:
                break
        cleaned = re.sub(r"\s+", " ", full).strip()
        if cleaned:
            snippet = cleaned[:max_length].rstrip()
            if len(cleaned) > max_length:
                snippet += "..."

    return {"book": book, "author": author, "rating": rating, "snippet": snippet}

def parse_reading(html: str):
    soup = BeautifulSoup(html or "", "html.parser")
    bt = soup.find("a", class_="bookTitle")
    at = soup.find("a", class_="authorName")
    book = clean_parenthetical(bt.get_text(strip=True) if bt else "")
    author = (at.get_text(strip=True) if at else "")
    return {"book": book, "author": author}

def parse_progress(html: str):
    soup = BeautifulSoup(html or "", "html.parser")
    img = soup.find("img", alt=re.compile(r" by "))
    if img and img.has_attr("alt"):
        parts = img["alt"].split(" by ", 1)
        if len(parts) == 2:
            return {"book": clean_parenthetical(parts[0]), "author": parts[1].strip()}
    return parse_reading(html)

def build_progress_bar(percent: float, length: int = 20) -> str:
    filled = int(math.floor((percent / 100.0) * length))
    filled = max(0, min(length, filled))
    empty = length - filled
    pct = f"{percent:.2f}%"
    return "▓" * filled + "░" * empty + "  " + pct

def make_feed(feed_title: str, self_link: str):
    fg = FeedGenerator()
    fg.id(SOURCE_FEED)
    fg.title(feed_title)
    fg.author({"name": "Julien"})
    fg.link(href=SOURCE_FEED, rel="alternate")
    fg.link(href=self_link, rel="self")
    fg.language("en")
    return fg

def to_dt(entry: Dict[str, Any]) -> datetime.datetime:
    """
    Convert entry's published_parsed/updated_parsed to timezone-aware datetime.
    Falls back to now() UTC if missing.
    """
    tup = entry.get("published_parsed") or entry.get("updated_parsed")
    if tup:
        try:
            return datetime.datetime(*tup[:6], tzinfo=timezone.utc)
        except Exception:
            pass
    return datetime.datetime.now(tz=timezone.utc)

def main():
    cache_bust = f"?nocache={int(time.time())}"
    src = feedparser.parse(SOURCE_FEED + cache_bust)

    if src.bozo:
        print("❌ Error parsing feed:", src.bozo_exception, file=sys.stderr)
        # don't hard-exit; sometimes bozo is set but entries are usable
        if not getattr(src, "entries", None):
            sys.exit(1)

    entries = list(getattr(src, "entries", []))
    print(f"✅ Fetched {len(entries)} entries.")

    fg_tw = make_feed("Julien’s Goodreads → Twitter Feed", TWITTER_OUTPUT)
    fg_th = make_feed("Julien’s Goodreads → Threads Feed", THREADS_OUTPUT)

    if entries:
        fg_tw.updated(to_dt(entries[0]))
        fg_th.updated(to_dt(entries[0]))

    count_tw = count_th = 0

    # Process oldest → newest (stable ordering in output)
    for entry in reversed(entries):
        try:
            title = (entry.get("title") or "").strip()
            link = entry.get("link") or ""
            html = extract_html(entry)

            # Skip activity types we don't care about (and which often lack body)
            if not title:
                print("⚠️  Skipped entry: (no title)")
                continue
            if any(
                title.startswith(prefix) for prefix in [
                    "Julien liked a review",
                    "Julien liked",
                    "Julien wants to read",
                    "Julien added a quote",
                    "Julien is friends with",
                ]
            ):
                print(f"⚠️  Skipped entry (activity): {title}")
                continue

            # Classify
            txt_tw = txt_th = None

            if re.match(r"Julien finished reading '", title):
                d = parse_reading(html)
                lines = [
                    f"📘 Finished “{d['book']}” by {d['author']}",
                    f"🔗 {link}",
                    f"🏷️ #{_sanitize_hashtag(d['author'])}"
                ]
                txt_tw = txt_th = "\n".join(lines)

            elif re.match(r"Julien (?:is currently|started) reading '", title):
                d = parse_reading(html)
                lines = [
                    f"🚀 Starting “{d['book']}” by {d['author']}",
                    f"🔗 Follow my progress: {link}",
                    f"🏷️ #{_sanitize_hashtag(d['author'])} #NowReading"
                ]
                txt_tw = txt_th = "\n".join(lines)

            elif (m := re.search(r"(\d+)% done with (.+)", title)):
                try:
                    pct = float(m.group(1))
                except ValueError:
                    pct = 0.0
                d = parse_progress(html)
                bar = build_progress_bar(pct)
                lines = [
                    f"📈 I’ve read {int(pct)}% of “{d['book']}” by {d['author']}",
                    bar,
                    f"🔗 Progress: {link}",
                    f"🏷️ #{_sanitize_hashtag(d['author'])} #ReadingProgress"
                ]
                txt_tw = txt_th = "\n".join(lines)

            elif re.match(r"Julien added '", title) or re.match(r"Julien reviewed '", title):
                tw = parse_reviews(html, 200)
                th = parse_reviews(html, 500)

                lines_tw = [f"📚 “{tw['book']}” by {tw['author']}"]
                if tw["rating"] is not None:
                    lines_tw.append(f"⭐️ Rated: {tw['rating']}/5")
                if tw["snippet"]:
                    lines_tw.append(f"📝 \"{tw['snippet']}\"")
                lines_tw.append(f"🏷️ #{_sanitize_hashtag(tw['author'])}")
                txt_tw = "\n".join(lines_tw)

                lines_th = [f"📚 “{th['book']}” by {th['author']}"]
                if th["rating"] is not None:
                    lines_th.append(f"⭐️ Rated: {th['rating']}/5")
                if th["snippet"]:
                    lines_th.append(f"📝 \"{th['snippet']}\"")
                lines_th.append(f"🏷️ #{_sanitize_hashtag(th['author'])}")
                txt_th = "\n".join(lines_th)

            else:
                print(f"⚠️  Skipped entry (unhandled type): {title}")
                continue

            # Build entries
            guid = entry.get("guid") or entry.get("id") or (link or title)
            updated_dt = to_dt(entry)

            # Twitter feed
            e_tw = fg_tw.add_entry()
            e_tw.id(str(guid) + "-tw")
            e_tw.title(title)
            if link:
                e_tw.link(href=link, rel="alternate")
            e_tw.updated(updated_dt)
            e_tw.content(txt_tw or "", type="text")
            count_tw += 1

            # Threads feed
            e_th = fg_th.add_entry()
            e_th.id(str(guid) + "-th")
            e_th.title(title)
            if link:
                e_th.link(href=link, rel="alternate")
            e_th.updated(updated_dt)
            e_th.content(txt_th or "", type="text")
            count_th += 1

        except Exception as e:
            print(f"⚠️  Skipped entry due to error: {(entry.get('title') or 'Untitled')} — {e}")
            continue

    print(f"✅ Built {count_tw} Twitter entries, {count_th} Threads entries.")

    with open(TWITTER_OUTPUT, "wb") as f:
        f.write(fg_tw.atom_str(pretty=True))
    print(f"✅ Wrote {TWITTER_OUTPUT}")

    with open(THREADS_OUTPUT, "wb") as f:
        f.write(fg_th.atom_str(pretty=True))
    print(f"✅ Wrote {THREADS_OUTPUT}")

if __name__ == "__main__":
    main()