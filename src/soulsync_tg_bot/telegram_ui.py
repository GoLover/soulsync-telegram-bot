import asyncio
from datetime import timedelta

from telegram import Bot, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import BadRequest, RetryAfter


async def show(
    bot: Bot,
    chat: int,
    message: int | None,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> int:
    for attempt in range(2):
        try:
            if message is not None:
                try:
                    await bot.edit_message_text(
                        text,
                        chat_id=chat,
                        message_id=message,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
                    return message
                except BadRequest as exc:
                    reason = str(exc).lower()
                    if "message is not modified" in reason:
                        return message
                    if not any(
                        part in reason
                        for part in (
                            "message to edit not found",
                            "message can't be edited",
                            "message_id_invalid",
                        )
                    ):
                        raise
            sent = await bot.send_message(
                chat,
                text,
                reply_markup=keyboard,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            return sent.message_id
        except RetryAfter as exc:
            delay = exc.retry_after
            seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
            if attempt or seconds > 30:
                raise
            await asyncio.sleep(seconds + 0.1)
    raise AssertionError("Telegram retry loop did not return")
