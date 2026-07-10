import configparser
import re
import sqlite3
import feedparser
import httpx
import asyncio
import logging
from typing import List, Dict, Tuple

import trafilatura
from bs4 import BeautifulSoup

# --- Setup logging for detailed status ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# --- Article body fetching ---
# The summarizer works best when it sees real article text, not just the feed
# title/summary, so after storing new articles we fetch each article page and
# extract its main text.
BODY_MAX_CHARS = 3000        # cap stored per article
BODY_CONCURRENCY = 8         # simultaneous article fetches
BODY_TIMEOUT = 20.0          # seconds per request

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

def setup_database(db_path: str):
    """Creates the database and table if they don't exist."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS articles (
                url TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                published TEXT,
                content TEXT,
                body TEXT DEFAULT ''
            )
        ''')
        # Databases created before the body column existed: add it in place.
        try:
            cursor.execute("ALTER TABLE articles ADD COLUMN body TEXT DEFAULT ''")
            logging.info("Added missing 'body' column to existing database.")
        except sqlite3.OperationalError:
            pass  # column already exists
        logging.info(f"Database ready at '{db_path}'.")

async def fetch_feed(session: httpx.AsyncClient, category: str, name: str, url: str) -> List[Dict]:
    """Asynchronously fetches and parses a single RSS feed with a timeout and redirects."""
    articles = []
    try:
        logging.info(f"-> Fetching: [{category}] {name}")
        # KEY IMPROVEMENT: Set a user-agent to pretend we are a browser and follow redirects
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = await session.get(url, timeout=60.0, headers=headers, follow_redirects=True)
        response.raise_for_status()
        
        feed = feedparser.parse(response.text)
        
        for entry in feed.entries:
            article = {
                "url": entry.get("link"),
                "title": entry.get("title", "No Title"),
                "category": category,
                "published": entry.get("published", ""),
                "content": entry.get("summary", entry.get("description", ""))
            }
            if article["url"]:
                articles.append(article)
        
        logging.info(f"[SUCCESS] Fetched {len(articles)} articles from: {name}")
        return articles

    except httpx.TimeoutException:
        logging.warning(f"[TIMEOUT] Feed timed out: {name} ({url})")
    except httpx.RequestError as e:
        logging.error(f"[HTTP ERROR] Could not fetch {name}: {e}")
    except Exception as e:
        logging.error(f"[PARSE ERROR] Failed to parse {name}: {e}")
    
    return []

def store_articles_in_db(db_path: str, all_articles: List[Dict]) -> List[str]:
    """Stores a list of articles in the database, ignoring duplicates.
    Returns the URLs that were newly inserted this run."""
    if not all_articles:
        logging.info("No new articles to store.")
        return []

    new_urls: List[str] = []
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        for article in all_articles:
            try:
                cursor.execute(
                    "INSERT INTO articles (url, title, category, published, content) VALUES (?, ?, ?, ?, ?)",
                    (article["url"], article["title"], article["category"], article["published"], article["content"])
                )
                new_urls.append(article["url"])
            except sqlite3.IntegrityError:
                pass
            except Exception as e:
                logging.error(f"DB Error storing {article.get('title')}: {e}")

        logging.info(f"Database transaction complete. Stored {len(new_urls)} new articles.")
    return new_urls

def _collapse_whitespace(text: str) -> str:
    """Normalize runs of whitespace but keep paragraph breaks."""
    text = re.sub(r'[ \t\r\f\v]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()

def _extract_with_soup(html: str) -> str:
    """Fallback extractor: prefer <article>, then <main>, then the element
    holding the densest cluster of <p> text."""
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'noscript', 'template']):
        tag.decompose()

    container = soup.find('article') or soup.find('main')
    if container is None:
        # Densest paragraph cluster: the parent whose direct <p> children
        # carry the most text. Parents are deduplicated by identity because
        # bs4 tags compare by content, not by node.
        candidates = {}
        for p in soup.find_all('p'):
            if p.parent is not None:
                candidates[id(p.parent)] = p.parent
        best_len = 0
        for parent in candidates.values():
            cluster_len = sum(len(p.get_text(strip=True)) for p in parent.find_all('p', recursive=False))
            if cluster_len > best_len:
                best_len = cluster_len
                container = parent
    if container is None:
        return ''

    paragraphs = [p.get_text(' ', strip=True) for p in container.find_all('p')]
    text = '\n\n'.join(p for p in paragraphs if p)
    if not text:
        text = container.get_text(' ', strip=True)
    return _collapse_whitespace(text)

def extract_body(html: str) -> Tuple[str, str]:
    """Extract the main article text from an HTML page.
    Returns (text capped at BODY_MAX_CHARS, extractor name) or ('', 'failed')."""
    text = ''
    method = 'failed'
    try:
        extracted = trafilatura.extract(html, include_comments=False, include_tables=False)
        if extracted and extracted.strip():
            text = extracted
            method = 'trafilatura'
    except Exception as e:
        logging.debug(f"trafilatura extraction error: {e}")

    if not text:
        try:
            fallback = _extract_with_soup(html)
            if fallback:
                text = fallback
                method = 'soup'
        except Exception as e:
            logging.debug(f"BeautifulSoup extraction error: {e}")

    return _collapse_whitespace(text)[:BODY_MAX_CHARS], method

async def fetch_article_body(session: httpx.AsyncClient, semaphore: asyncio.Semaphore, url: str) -> Tuple[str, str, str]:
    """Fetch one article page and extract its body.
    Never raises: a failure yields ('', 'failed') for this URL only."""
    async with semaphore:
        try:
            response = await session.get(url, timeout=BODY_TIMEOUT, headers=BROWSER_HEADERS, follow_redirects=True)
            response.raise_for_status()
            body, method = extract_body(response.text)
            return url, body, method
        except Exception as e:
            logging.warning(f"[BODY FAIL] {url}: {type(e).__name__}: {e}")
            return url, '', 'failed'

async def fetch_and_store_bodies(db_path: str, urls: List[str]) -> Dict[str, int]:
    """Fetch every new article URL (bounded concurrency), extract main text,
    and store it in the body column. Returns extraction stats."""
    stats = {'trafilatura': 0, 'soup': 0, 'failed': 0, 'total_chars': 0}
    if not urls:
        return stats

    logging.info(f"--- Fetching article bodies for {len(urls)} new articles "
                 f"(concurrency={BODY_CONCURRENCY}, timeout={BODY_TIMEOUT}s) ---")
    semaphore = asyncio.Semaphore(BODY_CONCURRENCY)
    async with httpx.AsyncClient() as session:
        results = await asyncio.gather(
            *(fetch_article_body(session, semaphore, url) for url in urls)
        )

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        for url, body, method in results:
            cursor.execute("UPDATE articles SET body = ? WHERE url = ?", (body, url))
            stats[method] += 1
            stats['total_chars'] += len(body)

    extracted = stats['trafilatura'] + stats['soup']
    avg_len = stats['total_chars'] // extracted if extracted else 0
    logging.info(f"--- Body extraction: {stats['trafilatura']} via trafilatura, "
                 f"{stats['soup']} via fallback, {stats['failed']} failed (stored empty); "
                 f"avg {avg_len} chars per extracted body. ---")
    return stats

async def main():
    logging.info("--- Starting V3 Concurrent News Gatherer (with redirects) ---")
    config = configparser.ConfigParser()
    # feeds.ini is the committed, secret-free feed list. A local, gitignored
    # config.ini is still read afterwards (backward compatible) and can
    # override or extend it.
    loaded = config.read(['feeds.ini', 'config.ini'])
    logging.info(f"Loaded configuration from: {loaded or 'nothing (no config files found!)'}")

    db_path = config.get('database', 'path', fallback='news_articles.db')
    setup_database(db_path)

    feeds_to_fetch: List[Tuple[str, str, str]] = []
    for section in config.sections():
        if section.startswith('feeds_'):
            category = section.replace('feeds_', '', 1).replace('_', ' ').title()
            for name, url in config.items(section):
                feeds_to_fetch.append((category, name, url))

    if not feeds_to_fetch:
        logging.error("No [feeds_*] sections found in feeds.ini/config.ini. Nothing to fetch.")
        raise SystemExit(1)

    async with httpx.AsyncClient() as session:
        tasks = [fetch_feed(session, cat, name, url) for cat, name, url in feeds_to_fetch]
        results = await asyncio.gather(*tasks)

    all_articles = [article for feed_articles in results for article in feed_articles]
    logging.info(f"--- Fetched a total of {len(all_articles)} articles from all feeds. ---")
    new_urls = store_articles_in_db(db_path, all_articles)
    await fetch_and_store_bodies(db_path, new_urls)
    logging.info("--- V3 News Gatherer Finished. ---")

if __name__ == "__main__":
    if asyncio.get_event_loop().is_running():
         asyncio.run(main())
    else:
         asyncio.run(main())