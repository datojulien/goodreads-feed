#!/usr/bin/env python3
"""
goodreads_to_ifttt.py

Fetches a Goodreads RSS feed (“Julien’s Updates”), filters/cleans it so that each entry’s
<content> is formatted for both:
  • Twitter (≤280 characters, truncated snippets)
  • Threads (longer snippets allowed)

Distinguishes:
  1. 🚀 Started reading
  2. 📈 Progress update
  3. 📚 Finished with a review
  4. 📘 Finished without a review (optional)

Removes any parentheticals (e.g. “(Paperback)”) from book titles and extracts a true snippet 
from a review when present. For Twitter, limits snippet to ~200 chars; for Threads, allows ~500 chars.
Produces two Atom files:
  • cleaned_goodreads_twitter.xml
  • cleaned_goodreads_threads.xml
"""

import re
import sys
import math
import datetime
from datetime import timezone
import feedparser
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

# ──────────── CONFIGURATION ────────────

# Your Goodreads “Updates RSS” URL:
SOURCE_FEED = "https://www.goodreads.com/user/updates_rss/14857928"

# Output filenames (relative to repo root):
TWITTER_OUTPUT = "cleaned_goodreads_twitter.xml"
THREADS_OUTPUT = "cleaned_goodreads_threads.xml"

# ──────────────────────────────────────────

def clean_parenthetical(title: str) -> str:
    """Remove any parenthetical (e.g. "(Paperback)") from a book title."""
    return re.sub(r"\s*\(.*?\)", "", title).strip()

def parse_reviews(html: str, max_length: int):
    """Extract book_title, author_name, rating, snippet (up to max_length chars)."""
    soup = BeautifulSoup(html, "html.parser")
    # Book title
    book_tag = soup.find("a", class_="bookTitle")
    raw_title = book_tag.get_text(strip=True) if book_tag else ""
    book = clean_parenthetical(raw_title)
    # Author
    author_tag = soup.find("a", class_="authorName")
    author = author_tag.get_text(strip=True) if author_tag else ""
    # Rating
    text = soup.get_text("\n", strip=True)
    m = re.search(r"gave (\d+) stars", text)
    rating = int(m.group(1)) if m else None
    # Snippet
    snippet = ""
    first_br = soup.find("br")
    if first_br:
        full = ""
        for node in first_br.next_siblings:
            full += (node.get_text(" ", strip=True) if not isinstance(node, str) else node).strip() + " "
            if len(full) > max_length * 2:
                break
        line = re.sub(r"\s+", " ", full).strip()
        if line:
            snippet = line[:max_length].rstrip()
            if len(line) > max_length:
                snippet += "..."
    return {"book": book, "author": author, "rating": rating, "snippet": snippet}

def parse_reading(html: str):
    """Extract book_title & author_name from a start/finish entry."""
    soup = BeautifulSoup(html, "html.parser")
    bt = soup.find("a", class_="bookTitle")
    book = clean_parenthetical(bt.get_text(strip=True)) if bt else ""
    at = soup.find("a", class_="authorName")
    author = at.get_text(strip=True) if at else ""
    return {"book": book, "author": author}

def parse_progress(html: str):
    """Extract book & author from <img alt="Title by Author"> or fallback."""
    soup = BeautifulSoup(html, "html.parser")
    img = soup.find("img", alt=re.compile(r" by "))
    if img and img.has_attr("alt"):
        parts = img["alt"].split(" by ", 1)
        if len(parts) == 2:
            return {"book": clean_parenthetical(parts[0]), "author": parts[1].strip()}
    return parse_reading(html)

def build_progress_bar(percent: float, length: int = 20) -> str:
    """
    Granular bar: “▓” filled, “░” empty, plus percentage.
    E.g. 9.36% → "▓░░░░░░░░░░░░░░░░░  9.36%"
    """
    filled = int(math.floor((percent / 100) * length))
    empty = length - filled
    pct = f"{percent:.2f}%"
    return "▓" * filled + "░" * empty + "  " + pct

def make_feed(max_snip: int, feed_title: str, self_link: str):
    """Return a FeedGenerator configured for either Twitter or Threads."""
    fg = FeedGenerator()
    fg.id(SOURCE_FEED)
    fg.title(feed_title)
    fg.author({"name": "Julien"})
    fg.link(href=SOURCE_FEED, rel="alternate")
    fg.link(href=self_link, rel="self")
    fg.language("en")
    # timestamp from newest entry
    return fg

def main():
    src = feedparser.parse(SOURCE_FEED)
    if src.bozo:
        print("Error parsing feed:", src.bozo_exception, file=sys.stderr)
        sys.exit(1)
    entries = src.entries
    print(f"Fetched {len(entries)} entries.")

    # Build both feeds
    fg_tw = make_feed(200, "Julien’s Goodreads → Twitter Feed", TWITTER_OUTPUT)
    fg_th = make_feed(500, "Julien’s Goodreads → Threads Feed", THREADS_OUTPUT)

    # Use newest pubDate for feed updated
    if entries:
        dt = datetime.datetime(*entries[0].published_parsed[:6], tzinfo=timezone.utc)
        fg_tw.updated(dt)
        fg_th.updated(dt)

    count_tw = count_th = 0

    for entry in reversed(entries):
        title = " ".join((entry.title or "").split())
        html = (entry.content[0].value if hasattr(entry, "content") else entry.summary)
        link = entry.link

        # Finished without review
        if re.match(r"Julien finished reading '", title):
            d = parse_reading(html)
            lines = [
                f"📘 Finished “{d['book']}” by {d['author']}",
                f"🔗 {link}",
                f"🏷️ #{d['author'].replace(' ', '')}"
            ]
            txt_tw = txt_th = "\n".join(lines)

        # Started reading
        elif re.match(r"Julien (?:is currently|started) reading '", title):
            d = parse_reading(html)
            lines = [
                f"🚀 Starting “{d['book']}” by {d['author']}",
                f"🔗 Follow my progress: {link}",
                f"🏷️ #{d['author'].replace(' ', '')} #NowReading"
            ]
            txt_tw = txt_th = "\n".join(lines)

        # Progress update
        elif m := re.search(r"(\d+)% done with (.+)", title):
            pct = float(m.group(1))
            d = parse_progress(html)
            bar = build_progress_bar(pct, length=20)
            lines = [
                f"📈 I have read {int(pct)}% of “{d['book']}” by {d['author']}",
                bar,
                f"🔗 Progress: {link}",
                f"🏷️ #{d['author'].replace(' ', '')} #ReadingProgress"
            ]
            txt_tw = txt_th = "\n".join(lines)

        # Finished with review
        elif re.match(r"Julien added '", title):
            tw = parse_reviews(html, max_length=200)
            th = parse_reviews(html, max_length=500)
            lines_tw = [f"📚 “{tw['book']}” by {tw['author']}"]
            if tw["rating"] is not None:
                lines_tw.append(f"⭐️ Rated: {tw['rating']}/5")
            if tw["snippet"]:
                lines_tw.append(f"📝 \"{tw['snippet']}\"")
            lines_tw.append(f"🏷️ #{tw['author'].replace(' ', '')}")
            txt_tw = "\n".join(lines_tw)

            lines_th = [f"📚 “{th['book']}” by {th['author']}"]
            if th["rating"] is not None:
                lines_th.append(f"⭐️ Rated: {th['rating']}/5")
            if th["snippet"]:
                lines_th.append(f"📝 \"{th['snippet']}\"")
            lines_th.append(f"🏷️ #{th['author'].replace(' ', '')}")
            txt_th = "\n".join(lines_th)

        else:
            # skip comments/likes/etc.
            continue

        # add to Twitter feed
        e_tw = fg_tw.add_entry()
        e_tw.id(entry.guid + "-tw")
        e_tw.title(entry.title)
        e_tw.link(href=link, rel="alternate")
        e_tw.updated(entry.published)
        e_tw.content(txt_tw, type="text")
        count_tw += 1

        # add to Threads feed
        e_th = fg_th.add_entry()
        e_th.id(entry.guid + "-th")
        e_th.title(entry.title)
        e_th.link(href=link, rel="alternate")
        e_th.updated(entry.published)
        e_th.content(txt_th, type="text")
        count_th += 1

    print(f"Built {count_tw} Twitter entries, {count_th} Threads entries.")

    # write out files
    with open(TWITTER_OUTPUT, "wb") as f:
        f.write(fg_tw.atom_str(pretty=True))
    print(f"Wrote {TWITTER_OUTPUT}")

    with open(THREADS_OUTPUT, "wb") as f:
        f.write(fg_th.atom_str(pretty=True))
    print(f"Wrote {THREADS_OUTPUT}")

if __name__ == "__main__":
    main()
