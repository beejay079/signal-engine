"""
notify.py - push a message to your phone.

Two options (set either or both on Render -> Environment):
  NTFY_TOPIC                         free ntfy app, no account (easiest)
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID   Telegram bot
"""
import json
import os
import urllib.parse
import urllib.request


def channels():
    out = []
    if os.getenv("NTFY_TOPIC"):
        out.append("ntfy")
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        out.append("telegram")
    return out


def _post(url, payload: dict, as_json=True):
    data = json.dumps(payload).encode() if as_json else urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json" if as_json
                                          else "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def send(title: str, message: str, priority: int = 4, tags=None, click=None):
    """Returns a list of error strings (empty = all sent)."""
    errors = []
    topic = os.getenv("NTFY_TOPIC")
    if topic:
        server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        body = dict(topic=topic, title=title, message=message, priority=priority, tags=tags or [])
        if click:
            body["click"] = click
        try:
            _post(server, body)
        except Exception as e:
            errors.append(f"ntfy: {e}")
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        text = f"{title}\n\n{message}" + (f"\n\n{click}" if click else "")
        try:
            _post(f"https://api.telegram.org/bot{token}/sendMessage",
                  dict(chat_id=chat, text=text, disable_web_page_preview=True))
        except Exception as e:
            errors.append(f"telegram: {e}")
    if not topic and not (token and chat):
        errors.append("No notification channel set (add NTFY_TOPIC on Render).")
    return errors
