"""Every text the bot writes to people. The limits in it come from Settings."""

from dataclasses import asdict

from .config import Settings
from .repo import Stats

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

# the bot's command menu
CMD_START = "Начать"
CMD_PRIVACY = "Какие данные хранит бот"

# for the admins: the subscription cannot be checked (the bot is probably not an admin of the channel)
SUB_CHECK_ALERT = "Не получается проверить подписку на канал: {error}. Бот должен быть админом канала."

# for the admins: an order failed for a reason other than the user's file
ORDER_FAILED_ALERT = "Заказ №{order_id} (пользователь {tg_id}) не обработан: {error}"

# for the admins: /stats and /broadcast
STATS = ("👥 Пользователей: {users_total} (новых за сутки {users_new_day}, за 7 дней {users_new_week})\n"
         "Активных за 7 дней: {users_active_week}\nПодписаны: {users_subscribed}\n"
         "Заблокировали бота: {users_blocked}\n🎬 Видео: за сутки {done_day}, всего {done_total}; "
         "ошибок за сутки {failed_day}\nОчередь: {queued}{processing_mark}")
STATS_PROCESSING = " + 1 в работе"
BC_ASK_POST = "Пришли пост для рассылки. /cancel — отмена."
BC_CONFIRM = "Отправить {n} пользователям?"
BC_GO, BC_CANCEL = "Отправить", "Отмена"
BC_CANCELLED = "Рассылка отменена."
BC_BUSY = "Рассылка уже идёт, дождись отчёта."
BC_PROGRESS = "Рассылка: {done} из {total}…"
BC_REPORT = "Рассылка завершена. Доставлено: {sent}, заблокировали бота: {blocked}, ошибок: {failed}."


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


def stats(s: Stats) -> str:
    return STATS.format(**asdict(s), processing_mark=STATS_PROCESSING if s.processing else "")


def bc_confirm(n: int) -> str:
    return BC_CONFIRM.format(n=n)


def bc_progress(done: int, total: int) -> str:
    return BC_PROGRESS.format(done=done, total=total)


def bc_report(sent: int, blocked: int, failed: int) -> str:
    return BC_REPORT.format(sent=sent, blocked=blocked, failed=failed)
