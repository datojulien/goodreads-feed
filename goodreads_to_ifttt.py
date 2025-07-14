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

# ───── CONFIGURATION ─────
SOURCE_FEED = "https://www.goodreads.com/user/updates_rss/14857928"
TWITTER_OUTPUT = "cleaned_goodreads_twitter.xml"
THREADS_OUTPUT = "cleaned_goodreads_threads.xml"
# ─────────────────────────

def clean_parenthetical(title: str) -> str:
    return re.sub(r"\s*\(.*?\)", "", title).strip()

def parse_reviews(html: str, max_length: int):
    soup = BeautifulSoup(html, "html.parser")
    book_tag = soup.find("a", class_="bookTitle")
    author_tag = soup.find("a", class_="authorName")
    raw_title = book_tag.get_text(strip=True) if book_tag else ""
    author = author_tag.get_text(strip=True) if author_tag else ""
    book = clean_parenthetical(raw_title)

    rating = None
    text = soup.get_text("\n", strip=True)
    m = re.search(r"gave (\d+) stars", text)
    if m:
        rating = int(m.group(1))

    snippet = ""
    first_br = soup.find("br")
    if first_br:
        full = ""
        for node in first_br.next_siblings:
            if isinstance(node, str):
                full += node.strip() + " "
            elif hasattr(node, "get_text"):
                full += node.get_text(" ", strip=True).strip() + " "
            if len(full) > max_length * 2:
                break
        cleaned = re.sub(r"\s+", " ", full).strip()
        if cleaned:
            snippet = cleaned[:max_length].rstrip()
            if len(cleaned) > max_length:
                snippet += "..."

    return {"book": book, "author": author, "rating": rating, "snippet": snippet}

def parse_reading(html: str):
    soup = BeautifulSoup(html, "html.parser")
    bt = soup.find("a", class_="bookTitle")
    at = soup.find("a", class_="authorName")
    book = clean_parenthetical(bt.get_text(strip=True)) if bt else ""
    author = at.get_text(strip=True) if at else ""
    return {"book": book, "author": author}

def parse_progress(html: str):
    soup = BeautifulSoup(html, "html.parser")
    img = soup.find("img", alt=re.compile(r" by "))
    if img and img.has_attr("alt"):
        parts = img["alt"].split(" by ", 1)
        if len(parts) == 2:
            return {"book": clean_parenthetical(parts[0]), "author": parts[1].strip()}
    return parse_reading(html)

def build_progress_bar(percent: float, length: int = 20) -> str:
    filled = int(math.floor((percent / 100) * length))
    empty = length - filled
    pct = f"{percent:.2f}%"
    return "▓" * filled + "░" * empty + "  " + pct

def make_feed(max_snip: int, feed_title: str, self_link: str):
    fg = FeedGenerator()
    fg.id(SOURCE_FEED)
    fg.title(feed_title)
    fg.author({"name": "Julien"})
    fg.link(href=SOURCE_FEED, rel="alternate")
    fg.link(href=self_link, rel="self")
    fg.language("en")
    return fg

def main():
    cache_bust = f"?nocache={int(time.time())}"
    src = feedparser.parse(SOURCE_FEED + cache_bust)

    if src.bozo:
        print("❌ Error parsing feed:", src.bozo_exception, file=sys.stderr)
        sys.exit(1)

    entries = src.entries
    print(f"✅ Fetched {len(entries)} entries.")

    fg_tw = make_feed(200, "Julien’s Goodreads → Twitter Feed", TWITTER_OUTPUT)
    fg_th = make_feed(500, "Julien’s Goodreads → Threads Feed", THREADS_OUTPUT)

    if entries:
        dt = datetime.datetime(*entries[0].published_parsed[:6], tzinfo=timezone.utc)
        fg_tw.updated(dt)
        fg_th.updated(dt)

    count_tw = count_th = 0

    for entry in reversed(entries):
        title = (entry.title or "").strip()
        html = getattr(entry, "content", [{"value": entry.summary}])[0]["value"]
        link = entry.link

        if re.match(r"Julien finished reading '", title):
            d = parse_reading(html)
            lines = [
                f"📘 Finished “{d['book']}” by {d['author']}",
                f"🔗 {link}",
                f"🏷️ #{d['author'].replace(' ', '')}"
            ]
            txt_tw = txt_th = "\n".join(lines)

        elif re.match(r"Julien (?:is currently|started) reading '", title):
            d = parse_reading(html)
            lines = [
                f"🚀 Starting “{d['book']}” by {d['author']}",
                f"🔗 Follow my progress: {link}",
                f"🏷️ #{d['author'].replace(' ', '')} #NowReading"
            ]
            txt_tw = txt_th = "\n".join(lines)

        elif (m := re.search(r"(\d+)% done with (.+)", title)):
            pct = float(m.group(1))
            d = parse_progress(html)
            bar = build_progress_bar(pct)
            lines = [
                f"📈 I’ve read {int(pct)}% of “{d['book']}” by {d['author']}",
                bar,
                f"🔗 Progress: {link}",
                f"🏷️ #{d['author'].replace(' ', '')} #ReadingProgress"
            ]
            txt_tw = txt_th = "\n".join(lines)

        elif re.match(r"Julien added '", title):
            tw = parse_reviews(html, 200)
            th = parse_reviews(html, 500)

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
            print(f"⚠️ Skipped entry: {title}")
            continue

        # Add to Twitter feed
        e_tw = fg_tw.add_entry()
        e_tw.id(entry.guid + "-tw")
        e_tw.title(title)
        e_tw.link(href=link, rel="alternate")
        e_tw.updated(entry.published)
        e_tw.content(txt_tw, type="text")
        count_tw += 1

        # Add to Threads feed
        e_th = fg_th.add_entry()
        e_th.id(entry.guid + "-th")
        e_th.title(title)
        e_th.link(href=link, rel="alternate")
        e_th.updated(entry.published)
        e_th.content(txt_th, type="text")
        count_th += 1

    print(f"✅ Built {count_tw} Twitter entries, {count_th} Threads entries.")

    with open(TWITTER_OUTPUT, "wb") as f:
        f.write(fg_tw.atom_str(pretty=True))
    print(f"✅ Wrote {TWITTER_OUTPUT}")

    with open(THREADS_OUTPUT, "wb") as f:
        f.write(fg_th.atom_str(pretty=True))
    print(f"✅ Wrote {THREADS_OUTPUT}")

if __name__ == "__main__":
    main()
