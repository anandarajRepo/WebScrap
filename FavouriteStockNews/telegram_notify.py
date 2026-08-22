"""Telegram notification helper for the stock news agent.

Sends formatted alert messages to a Telegram chat via a bot. Reads the bot
token and chat id from the TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
environment variables (typically supplied as GitHub Actions secrets).

Failures here never raise to the caller: a Telegram outage or a bad token
should not fail the whole news-fetch run. Problems are logged and reported
via the boolean return value instead.
"""

import html
import logging
import os

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram hard-limits a single message to 4096 characters.
MAX_MESSAGE_LEN = 4096


def _credentials():
    """Return (token, chat_id) from the environment, or (None, None)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    return token, chat_id


def is_configured():
    """True when both Telegram credentials are present in the environment."""
    token, chat_id = _credentials()
    return bool(token) and bool(chat_id)


def _send_raw(text):
    """Send a single message. Returns True on success, False otherwise."""
    token, chat_id = _credentials()
    if not token or not chat_id:
        logger.warning(
            "Telegram not configured (missing TELEGRAM_BOT_TOKEN or "
            "TELEGRAM_CHAT_ID); skipping notification."
        )
        return False

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        resp = requests.post(
            TELEGRAM_API.format(token=token), data=payload, timeout=20
        )
        if resp.status_code != 200:
            logger.error(
                "Telegram send failed (HTTP %s): %s",
                resp.status_code,
                resp.text[:500],
            )
            return False
        return True
    except requests.RequestException as exc:
        logger.error("Telegram send raised an exception: %s", exc)
        return False


def _format_article(article):
    """Render a single article as an HTML snippet for a Telegram message."""
    ticker = html.escape(str(article.get("matched_ticker", "?")))
    title = html.escape(str(article.get("title", "(no title)")))
    source = html.escape(str(article.get("source", "unknown")))
    url = article.get("url", "")

    if url:
        headline = f'<a href="{html.escape(url, quote=True)}">{title}</a>'
    else:
        headline = title

    return f"<b>[{ticker}]</b> {headline}\n<i>{source}</i>"


def _chunk_messages(header, blocks):
    """Yield message strings that each stay under the Telegram size limit."""
    current = header
    for block in blocks:
        candidate = current + "\n\n" + block if current else block
        if len(candidate) > MAX_MESSAGE_LEN and current:
            yield current
            current = block
        else:
            current = candidate
    if current:
        yield current


def send_articles(articles):
    """Send one or more matched articles as Telegram alert(s).

    Multiple articles from the same run are batched into a single message
    (split only when they exceed Telegram's size limit) to avoid spam.

    Returns True if at least one message was sent successfully. Never
    raises.
    """
    if not articles:
        return False

    if not is_configured():
        logger.warning("Skipping Telegram alert: credentials not configured.")
        return False

    count = len(articles)
    if count == 1:
        header = "\U0001F4E2 <b>New stock news</b>"
    else:
        header = f"\U0001F4E2 <b>{count} new stock news articles</b>"

    blocks = [_format_article(a) for a in articles]

    any_sent = False
    for message in _chunk_messages(header, blocks):
        if _send_raw(message):
            any_sent = True

    return any_sent
