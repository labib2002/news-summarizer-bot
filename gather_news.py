import feedparser
import sqlite3
import configparser
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def setup_database(db_path):
    """Creates the database and table if they don't exist."""
    conn = sqlite3.connect(db_path)
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
    conn.commit()
    conn.close()
    logging.info(f"Database ready at '{db_path}'.")

def store_article(cursor, entry, category):
    """Inserts a single article into the database, ignoring duplicates."""
    title = entry.get('title', 'No Title')
    url = entry.get('link', '')
    published = entry.get('published', '')
    content = entry.get('summary', entry.get('description', ''))

    if not url:
        return

    try:
        cursor.execute(
            "INSERT INTO articles (url, title, category, published, content) VALUES (?, ?, ?, ?, ?)",
            (url, title, category, published, content)
        )
        logging.info(f"Stored: [{category}] {title}")
    except sqlite3.IntegrityError:
        logging.debug(f"Skipping duplicate article: {title}")
        pass
    except Exception as e:
        logging.error(f"Error storing article {title}: {e}")

def main():
    config = configparser.ConfigParser()
    config.read('config.ini')

    db_path = config.get('database', 'path', fallback='news_articles.db')
    setup_database(db_path)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    for section in config.sections():
        if section.startswith('feeds_'):
            category = section.replace('feeds_', '', 1).replace('_', ' ').title()
            logging.info(f"--- Fetching feeds for category: {category} ---")
            for name, url in config.items(section):
                try:
                    feed = feedparser.parse(url)
                    for entry in feed.entries:
                        store_article(cursor, entry, category)
                except Exception as e:
                    logging.error(f"Could not parse feed {name} ({url}): {e}")

    conn.commit()
    conn.close()
    logging.info("--- Finished gathering all news articles. ---")

if __name__ == '__main__':
    main()