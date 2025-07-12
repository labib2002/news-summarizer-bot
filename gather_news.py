import configparser
import sqlite3
import feedparser
import httpx
import asyncio
import logging
from typing import List, Dict, Tuple

# --- Setup logging for detailed status ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

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
                content TEXT
            )
        ''')
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

def store_articles_in_db(db_path: str, all_articles: List[Dict]):
    """Stores a list of articles in the database, ignoring duplicates."""
    if not all_articles:
        logging.info("No new articles to store.")
        return
        
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        stored_count = 0
        for article in all_articles:
            try:
                cursor.execute(
                    "INSERT INTO articles (url, title, category, published, content) VALUES (?, ?, ?, ?, ?)",
                    (article["url"], article["title"], article["category"], article["published"], article["content"])
                )
                stored_count += 1
            except sqlite3.IntegrityError:
                pass
            except Exception as e:
                logging.error(f"DB Error storing {article.get('title')}: {e}")
        
        logging.info(f"Database transaction complete. Stored {stored_count} new articles.")

async def main():
    logging.info("--- Starting V3 Concurrent News Gatherer (with redirects) ---")
    config = configparser.ConfigParser()
    config.read('config.ini')

    db_path = config.get('database', 'path', fallback='news_articles.db')
    setup_database(db_path)

    feeds_to_fetch: List[Tuple[str, str, str]] = []
    for section in config.sections():
        if section.startswith('feeds_'):
            category = section.replace('feeds_', '', 1).replace('_', ' ').title()
            for name, url in config.items(section):
                feeds_to_fetch.append((category, name, url))

    async with httpx.AsyncClient() as session:
        tasks = [fetch_feed(session, cat, name, url) for cat, name, url in feeds_to_fetch]
        results = await asyncio.gather(*tasks)

    all_articles = [article for feed_articles in results for article in feed_articles]
    logging.info(f"--- Fetched a total of {len(all_articles)} articles from all feeds. ---")
    store_articles_in_db(db_path, all_articles)
    logging.info("--- V3 News Gatherer Finished. ---")

if __name__ == "__main__":
    if asyncio.get_event_loop().is_running():
         asyncio.run(main())
    else:
         asyncio.run(main())