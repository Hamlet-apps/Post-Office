"""A thin Telegram Bot API client (httpx).

    client = TelegramClient(bot)            # a TelegramBot row, or TelegramClient(token="...")
    client.get_me()
    client.send(chat_id, html, button=("Label", "https://..."), image_url="https://...")

Every call returns the "result" value or raises TelegramError, which carries
the HTTP-ish error code Telegram reported (403 = the person blocked the bot).
"""
import httpx

API = "https://api.telegram.org"


class TelegramError(Exception):
    def __init__(self, code, description, retry_after=None):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description
        self.retry_after = retry_after

    @property
    def permanent(self):
        """400 and 403 fail at once; 429 and 5xx are retried."""
        return self.code in (400, 401, 403, 404)

    @property
    def blocked(self):
        return self.code == 403

    def for_staff(self):
        if self.code == 403:
            return "Telegram says this person has blocked the bot"
        if self.code == 429:
            return "Telegram asked us to slow down"
        if self.code == 401:
            return "Telegram does not accept this bot's token"
        if self.code == 400 and "chat not found" in self.description.lower():
            return "Telegram cannot find this person's chat"
        return f"Telegram refused the message ({self.description})"


class TelegramClient:
    def __init__(self, bot=None, token=None, timeout=15):
        self.token = token or (bot.token if bot is not None else "")
        self.timeout = timeout

    def call(self, method, timeout=None, **params):
        params = {k: v for k, v in params.items() if v is not None}
        try:
            response = httpx.post(f"{API}/bot{self.token}/{method}", json=params, timeout=timeout or self.timeout)
            data = response.json()
        except httpx.HTTPError as exc:
            raise TelegramError(503, f"could not reach Telegram ({exc.__class__.__name__})") from exc
        except ValueError as exc:
            raise TelegramError(502, "Telegram sent an answer we could not read") from exc
        if not data.get("ok"):
            raise TelegramError(
                data.get("error_code", response.status_code), data.get("description", "unknown error"),
                (data.get("parameters") or {}).get("retry_after"),
            )
        return data.get("result")

    def get_me(self):
        return self.call("getMe")

    def send(self, chat_id, html, button=None, image_url=None, buttons=None):
        """Send formatted text, with an optional photo and inline buttons.

        button:  (label, url) for one link button
        buttons: [[{"text": ..., "url"|"callback_data": ...}]] for a custom keyboard
        """
        markup = None
        if buttons:
            markup = {"inline_keyboard": buttons}
        elif button and button[0] and button[1]:
            markup = {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]}
        if image_url:
            return self.call("sendPhoto", chat_id=chat_id, photo=image_url, caption=html, parse_mode="HTML",
                             reply_markup=markup)
        return self.call("sendMessage", chat_id=chat_id, text=html, parse_mode="HTML",
                         link_preview_options={"is_disabled": True}, reply_markup=markup)

    def get_updates(self, offset=None, timeout=0):
        return self.call("getUpdates", timeout=timeout + 10, offset=offset,
                         allowed_updates=["message", "callback_query"]) or []

    def answer_callback(self, callback_query_id, text=None):
        return self.call("answerCallbackQuery", callback_query_id=callback_query_id, text=text)

    def set_my_commands(self, commands, language_code=None):
        """commands: [{"command": "help", "description": "..."}]"""
        return self.call("setMyCommands", commands=commands, language_code=language_code)

    def set_webhook(self, url, secret_token=None):
        return self.call("setWebhook", url=url, secret_token=secret_token,
                         allowed_updates=["message", "callback_query"])

    def delete_webhook(self):
        return self.call("deleteWebhook")
