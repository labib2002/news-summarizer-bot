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
        model = genai.GenerativeModel('gemini-1.5-flash-latest')
        logging.info("Sending request to Gemini API...")
        response = await model.generate_content_async(prompt)
        logging.info("Received response from Gemini API.")
        return response.text
    except Exception as e:
        logging.error(f"Error calling Gemini API: {e}")
        return f"Error: Failed to generate summary. {e}"

def sanitize_markdown_v2(text: str) -> str:
    """Escapes special characters for Telegram's MarkdownV2 parser."""
    if not isinstance(text, str):
        return ""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    # Escape all special characters
    sanitized_text = re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)
    # Re-process for AI-generated markdown (bolding, etc.)
    sanitized_text = sanitized_text.replace(r'\#\# ', '*\n').replace(r'\-', '\n-')
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
    # In GitHub Actions, we'll get secrets from the environment
    if 'GITHUB_ACTIONS' in os.environ:
        telegram_token = os.environ['TELEGRAM_TOKEN']
        chat_id = os.environ['TELEGRAM_CHAT_ID']
        gemini_key = os.environ['GEMINI_API_KEY']
        db_path = 'news_articles.db'
    else: # For local testing, use the config file
        config = configparser.ConfigParser()
        config.read('config.ini')
        telegram_token = config.get('telegram', 'token')
        chat_id = config.get('telegram', 'chat_id')
        gemini_key = config.get('gemini', 'api_key')
        db_path = config.get('database', 'path')

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