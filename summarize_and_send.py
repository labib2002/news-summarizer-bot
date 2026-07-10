import sqlite3
import configparser
import logging
import google.generativeai as genai
import telegram
import asyncio
import re
import os
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_articles_from_db(db_path):
    """Fetches all articles from the database and groups them by category."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT title, content, category, url FROM articles ORDER BY category, published DESC")
    
    articles_by_category = {}
    for row in cursor.fetchall():
        category = row['category']
        if category not in articles_by_category:
            articles_by_category[category] = []
        # Limit content length to avoid overly long prompts
        articles_by_category[category].append(f"Title: {row['title']}\nSummary: {row['content'][:500]}")
        
    conn.close()
    return articles_by_category

def build_gemini_prompt(articles_by_category):
    """Creates a structured prompt for the AI to get a clean summary."""
    prompt_parts = [
        "You are a world news editor. Your task is to create a concise, neutral, and informative daily news briefing from the following articles, which are grouped by category.",
        "Your response MUST strictly follow this format:",
        "1. Start with a single-sentence overall summary of the day's most important news.",
        "2. For each category, write '## Category Name'.",
        "3. Under each category, write a 1-2 sentence summary of that category's key events.",
        "4. After the category summary, list 2-4 of the most important headlines as bullet points (using '-').",
        "Do not add any conversational text or greetings. Begin the briefing directly.",
        "\n--- ARTICLES START ---\n"
    ]

    for category, articles in articles_by_category.items():
        prompt_parts.append(f"CATEGORY: {category}\n")
        prompt_parts.extend(articles)
        prompt_parts.append("\n")

    prompt_parts.append("--- ARTICLES END ---")
    return "\n".join(prompt_parts)

async def get_summary_from_gemini(api_key, prompt):
    """Sends the prompt to the Gemini API and gets the summary."""
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        logging.info("Sending request to Gemini API...")
        response = await model.generate_content_async(prompt)
        logging.info("Received response from Gemini API.")
        return response.text
    except Exception as e:
        logging.error(f"Error calling Gemini API: {e}")
        return f"Error: Failed to generate summary. {e}"

def sanitize_markdown_v2(text: str) -> str:
    """
    Escapes all special characters for Telegram's MarkdownV2 parser.
    This version is more aggressive to handle complex AI-generated text.
    """
    if not isinstance(text, str):
        return ""
    
    # Characters to escape are: _ * [ ] ( ) ~ ` > # + - = | { } . !
    # Note: We are now escaping the hyphen `-` everywhere.
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    
    # First, escape all special characters.
    sanitized_text = re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)
    
    # Then, intelligently re-format the specific markdown we want to allow.
    # This part should be done AFTER the main sanitation.

    # Convert "## Category" to bold: *Category*
    # Use a regex to find ##, capture the text until the next newline.
    sanitized_text = re.sub(r'\\\#\\\#\s*(.*?)\n', r'*\1*\n', sanitized_text)

    # Convert bullet points that start with "- "
    # Use a regex for lines starting with a hyphen.
    sanitized_text = re.sub(r'^\s*\\\-\s', r'• ', sanitized_text, flags=re.MULTILINE)

    return sanitized_text
    
async def send_telegram_message(bot_token, chat_id, message):
    """Sends the final, formatted, and sanitized message to Telegram."""
    if not message:
        logging.warning("Message is empty, not sending.")
        return

    bot = telegram.Bot(token=bot_token)
    max_length = 4096
    
    try:
        if len(message) > max_length:
            logging.warning("Message is too long, splitting into parts.")
            for i in range(0, len(message), max_length):
                part = message[i:i + max_length]
                await bot.send_message(chat_id=chat_id, text=part, parse_mode='MarkdownV2')
                await asyncio.sleep(1)
        else:
            await bot.send_message(chat_id=chat_id, text=message, parse_mode='MarkdownV2')
        logging.info("Successfully sent message to Telegram.")
        return True # Indicate success
    except telegram.error.BadRequest as e:
        logging.error(f"Telegram API BadRequest Error: {e}")
        logging.error("This is likely a MarkdownV2 parsing error. The message will be written to 'failed_message.txt' for debugging.")
        with open("failed_message.txt", "w", encoding="utf-8") as f:
            f.write(message)
        return False # Indicate failure
    except Exception as e:
        logging.error(f"An unexpected error occurred when sending message: {e}")
        return False # Indicate failure

def clear_database(db_path):
    """Deletes all rows from the articles table."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM articles")
    conn.commit()
    conn.close()
    logging.info(f"Successfully cleared database '{db_path}'.")

async def main():
    # Secrets come from environment variables first (GitHub Actions secrets in
    # CI). A local, gitignored config.ini is still supported as a fallback.
    config = configparser.ConfigParser()
    config.read('config.ini')
    telegram_token = os.environ.get('TELEGRAM_TOKEN') or config.get('telegram', 'token', fallback=None)
    chat_id = os.environ.get('TELEGRAM_CHAT_ID') or config.get('telegram', 'chat_id', fallback=None)
    gemini_key = os.environ.get('GEMINI_API_KEY') or config.get('gemini', 'api_key', fallback=None)
    db_path = config.get('database', 'path', fallback='news_articles.db')

    missing = [name for name, value in [
        ('TELEGRAM_TOKEN', telegram_token),
        ('TELEGRAM_CHAT_ID', chat_id),
        ('GEMINI_API_KEY', gemini_key),
    ] if not value]
    if missing:
        logging.error(f"Missing secrets: {', '.join(missing)}. "
                      "Set them as environment variables or in a local config.ini.")
        raise SystemExit(1)

    articles = get_articles_from_db(db_path)
    if not articles:
        logging.info("No articles in the database to summarize. Exiting.")
        return

    prompt = build_gemini_prompt(articles)
    ai_summary = await get_summary_from_gemini(gemini_key, prompt)
    
    today_date_str = sanitize_markdown_v2(datetime.now().strftime("%A, %B %d, %Y"))
    header = f"🗞️ *Your Daily News Briefing: {today_date_str}*\n\n"
    
    sanitized_summary = sanitize_markdown_v2(ai_summary)
    final_message = header + sanitized_summary

    success = await send_telegram_message(telegram_token, chat_id, final_message)

    if success:
        clear_database(db_path)
    else:
        logging.error("Failed to send Telegram message. Database will not be cleared to allow for retry.")

if __name__ == '__main__':
    asyncio.run(main())