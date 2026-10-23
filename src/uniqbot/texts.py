"""Every text the bot writes to people. The limits in it come from Settings."""

from .config import Settings

WELCOME = "Пришли видео до {max_mb} МБ и не длиннее {max_min} мин — верну уникальную копию файлом."
NOT_SUBSCRIBED = "Бот бесплатный для подписчиков канала. Подпишись и нажми «Я подписался»."
BTN_SUBSCRIBE = "Подписаться"
BTN_CHECK = "Я подписался"
STILL_NOT_SUBSCRIBED = "Подписка пока не видна. Подпишись и нажми ещё раз."
CHECK_FAILED = "Временная ошибка, попробуй через пару минут."
SEND_VIDEO_HINT = "Пришли обычное видео файлом или как видео."
TOO_BIG = "Файл больше {max_mb} МБ."
TOO_LONG = "Видео длиннее {max_min} мин."
HAS_ACTIVE = "У тебя уже есть видео в работе, дождись результата."
ACCEPTED = "Принял! Ты в очереди: {position}-й. Результат пришлю сюда."
PROCESSING = "Обрабатываю…"
DONE_STATUS = "Готово ✅"
DONE_CAPTION = "Готово ✅ Можно выкладывать"
FAILED = "Не получилось обработать это видео, пришли другой файл."
NOT_A_VIDEO = "В файле не нашлось видео. Пришли обычное видео."
PRIVACY = ("Бот хранит твой Telegram ID, имя, @username, язык, дату первого и последнего визита, метку ссылки, "
           "по которой ты пришёл, статус подписки на канал и число обработанных видео. Это нужно, чтобы "
           "пускать подписчиков, считать статистику и присылать новости бота. Видео удаляются сразу после "
           "обработки.")

# for the admins: the subscription cannot be checked (the bot is probably not an admin of the channel)
SUB_CHECK_ALERT = "Не получается проверить подписку на канал: {error}. Бот должен быть админом канала."

# for the admins: an order failed for a reason other than the user's file
ORDER_FAILED_ALERT = "Заказ №{order_id} (пользователь {tg_id}) не обработан: {error}"


def _max_min(settings: Settings) -> int:
    return settings.max_duration_sec // 60


def welcome(settings: Settings) -> str:
    return WELCOME.format(max_mb=settings.max_file_mb, max_min=_max_min(settings))


def too_big(settings: Settings) -> str:
    return TOO_BIG.format(max_mb=settings.max_file_mb)


def too_long(settings: Settings) -> str:
    return TOO_LONG.format(max_min=_max_min(settings))


def accepted(position: int) -> str:
    return ACCEPTED.format(position=position)


def sub_check_alert(error: object) -> str:
    return SUB_CHECK_ALERT.format(error=error)


def order_failed_alert(order_id: int, tg_id: int, error: str) -> str:
    return ORDER_FAILED_ALERT.format(order_id=order_id, tg_id=tg_id, error=error)
