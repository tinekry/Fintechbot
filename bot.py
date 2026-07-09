from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import time
from pathlib import Path
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile, CallbackQuery, ChosenInlineResult, InlineKeyboardButton,
    InlineKeyboardMarkup, InlineQuery, InlineQueryResultArticle,
    InputMediaPhoto, InputTextMessageContent, KeyboardButton, Message, ReplyKeyboardMarkup,
)

from config import Config, load_config
from database import BankError, Database

router = Router()
cfg: Config
db: Database
bot: Bot
admin_logger = logging.getLogger("admin_actions")
last_stop_click: dict[int, float] = {}
transfer_locks: dict[int, asyncio.Lock] = {}
OWNER_USERNAME = "tinekry_u"
OWNER_TELEGRAM_ID = 6681002410


class Flow(StatesGroup):
    transfer_user = State()
    recipient_search = State()
    transfer_amount = State()
    admin_user = State()
    admin_amount = State()
    task_title = State()
    task_description = State()
    task_reward = State()
    task_deadline = State()
    report = State()
    reject_reason = State()
    broadcast = State()
    election_program = State()
    election_photos = State()
    election_reject_reason = State()
    election_admin_candidate = State()
    election_admin_delta = State()
    election_media_edit = State()
    election_program_edit = State()


def kb(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data)] for text, data in rows
    ])


def persistent_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🏠 Меню")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def inline_user_search(cancel: str = "admin") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔎 Открыть поиск пользователя",
            switch_inline_query_current_chat="",
        )],
        [InlineKeyboardButton(text="Отмена", callback_data=cancel)],
    ])


async def current(event: Message | CallbackQuery):
    tg = event.from_user
    return await db.authorize(tg.id, tg.username)


async def guard(event: Message | CallbackQuery, admin: bool = False, main: bool = False):
    user = await current(event)
    if not user:
        text = "Доступ ограничен. Вас нет в списках граждан государства."
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)
        return None
    is_owner = (
        event.from_user.id == OWNER_TELEGRAM_ID
        or (event.from_user.username or "").lower() == OWNER_USERNAME
    )
    if (admin and not (user["is_admin"] or is_owner)) or (
        main and not (user["is_main_admin"] or is_owner)
    ):
        if isinstance(event, CallbackQuery):
            await event.answer("Недостаточно прав.", show_alert=True)
        else:
            await event.answer("Недостаточно прав.")
        return None
    return user


async def menu_markup(uid: int) -> InlineKeyboardMarkup:
    user = await db.user(uid)
    is_owner = uid == OWNER_TELEGRAM_ID or (user["username"] or "").lower() == OWNER_USERNAME
    rows = [
        [InlineKeyboardButton(text="💳 Мой баланс", callback_data="balance")],
        [InlineKeyboardButton(text="↗️ Перевод", callback_data="transfer"),
         InlineKeyboardButton(text="🧾 История", callback_data="history")],
        [InlineKeyboardButton(text="💼 Доступные задачи", callback_data="tasks:open"),
         InlineKeyboardButton(text="🛠 Мои задачи", callback_data="tasks:worker")],
    ]
    rows.append([InlineKeyboardButton(text="🗳 Выборы", callback_data="election")])
    if user["is_admin"] or user["is_main_admin"] or user["verified_employer"]:
        rows.append([
            InlineKeyboardButton(text="➕ Создать задачу", callback_data="task:new"),
            InlineKeyboardButton(text="📋 Мои вакансии", callback_data="tasks:owner"),
        ])
    if user["is_admin"] or is_owner:
        rows.append([InlineKeyboardButton(text="⚙️ Панель государства", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_menu(message: Message, prefix: str = "nlogn-bank") -> None:
    await message.answer(f"<b>{prefix}</b>\nВыберите действие:", reply_markup=await menu_markup(message.chat.id))


async def staff_log(text: str) -> None:
    admin_logger.info(text)
    if cfg.log_chat_id:
        try:
            await bot.send_message(cfg.log_chat_id, "🏛 <b>Журнал государства</b>\n" + text)
        except Exception:
            logging.exception("Не удалось отправить сообщение в STAFF_LOG_CHAT_ID")


async def sync_transfer_log() -> bytes:
    rows = await db.export_transactions()
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow([
        "ID", "Created UTC", "Type", "Amount", "Note",
        "Sender ID", "Sender Username", "Receiver ID", "Receiver Username",
    ])
    for row in rows:
        writer.writerow(list(row))
    payload = stream.getvalue().encode("utf-8-sig")
    path = Path("data/transactions.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)
    return payload


def remove_admin_from_env(username: str, env_path: Path = Path(".env")) -> bool:
    """Persistently remove username from MAIN_ADMIN_USERNAMES, preserving other lines."""
    if not env_path.exists():
        raise RuntimeError("Файл .env не найден.")
    username = username.strip().lstrip("@").lower()
    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = False
    output: list[str] = []
    for line in lines:
        if line.lstrip().startswith("MAIN_ADMIN_USERNAMES="):
            prefix, raw = line.split("=", 1)
            ending = "\r\n" if raw.endswith("\r\n") else "\n" if raw.endswith("\n") else ""
            values = [x.strip().lstrip("@") for x in raw.strip().split(",") if x.strip()]
            kept = [x for x in values if x.lower() != username]
            changed = len(kept) != len(values)
            line = f"{prefix}={','.join(kept)}{ending}"
        output.append(line)
    if changed:
        temporary = env_path.with_name(env_path.name + ".tmp")
        temporary.write_text("".join(output), encoding="utf-8")
        os.replace(temporary, env_path)
    return changed


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    user = await guard(message)
    if not user:
        return
    first = not bool(user["first_seen"])
    if first:
        await message.answer(
            f"Здравствуйте, <b>{escape(user['name'])}</b>! Добро пожаловать в nlogn-bank. "
            "Ваш стартовый баланс: 0. Чтобы пополнить счёт наличными или сдать стартовый "
            "капитал, подойдите к государственному представителю (учителю)."
        )
    await message.answer("Быстрое меню включено.", reply_markup=persistent_menu())
    await show_menu(message)


@router.message(Command("menu"))
@router.message(F.text == "🏠 Меню")
async def menu(message: Message, state: FSMContext):
    await state.clear()
    if await guard(message):
        await show_menu(message)


@router.message(F.via_bot)
async def ignore_inline_result_message(message: Message):
    """Chosen inline articles are service messages; ChosenInlineResult handles them."""
    return


@router.callback_query(F.data == "menu")
async def menu_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    if await guard(call):
        await call.answer()
        await call.message.edit_text("<b>nlogn-bank</b>\nВыберите действие:",
                                     reply_markup=await menu_markup(call.from_user.id))


@router.callback_query(F.data == "balance")
async def balance(call: CallbackQuery):
    user = await guard(call)
    if not user:
        return
    status = "🔒 Заморожен" if user["frozen"] else "✅ Активен"
    await call.answer()
    await call.message.edit_text(
        f"<b>Мой счёт</b>\n\nБаланс: <b>{user['balance']} nlogn-коинов</b>\n"
        f"Доступно: {user['balance']-user['held']}\nВ холде: {user['held']}\nСтатус: {status}",
        reply_markup=kb(("← В меню", "menu")),
    )


@router.callback_query(F.data == "history")
async def history(call: CallbackQuery):
    if not await guard(call):
        return
    rows = await db.history(call.from_user.id)
    lines = []
    for x in rows:
        sign = "+" if x["receiver_id"] == call.from_user.id else "−"
        peer = x["sender"] if sign == "+" else x["receiver"]
        lines.append(f"{sign}<b>{x['amount']}</b> · {escape(x['note'] or x['kind'])} · @{escape(peer or 'государство')}")
    await call.answer()
    await call.message.edit_text("<b>Последние операции</b>\n\n" + ("\n".join(lines) or "Пока пусто."),
                                 reply_markup=kb(("← В меню", "menu")))


@router.callback_query(F.data == "transfer")
async def transfer_begin(call: CallbackQuery, state: FSMContext):
    user = await guard(call)
    if not user:
        return
    if user["frozen"]:
        return await call.answer("Ваш счёт заморожен.", show_alert=True)
    await state.clear()
    await call.answer()
    await show_recipients(call.message, call.from_user.id, "name")


async def show_recipients(message: Message, viewer_id: int, sort: str,
                          search: str = "") -> None:
    rows = [
        [InlineKeyboardButton(
            text="🔎 Открыть поиск",
            switch_inline_query_current_chat="",
        )],
        [InlineKeyboardButton(text="← В меню", callback_data="menu")],
    ]
    await message.edit_text(
        "<b>Поиск получателя</b>\n\n"
        "Нажмите «Открыть поиск». В строке над клавиатурой введите username "
        "получателя или любую его часть.\n\n"
        "Например: чтобы найти <code>@example</code>, можно ввести "
        "<code>exam</code>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("transfer:list:"))
async def recipient_sort(call: CallbackQuery, state: FSMContext):
    if not await guard(call):
        return
    await state.clear()
    await call.answer()
    await show_recipients(call.message, call.from_user.id, call.data.rsplit(":", 1)[1])


@router.callback_query(F.data == "transfer:search")
async def recipient_search_begin(call: CallbackQuery, state: FSMContext):
    if not await guard(call):
        return
    await state.set_state(Flow.recipient_search)
    await call.answer()
    await call.message.edit_text(
        "Введите username или его часть:",
        reply_markup=kb(("← К списку", "transfer:list:name")),
    )


@router.message(Flow.recipient_search)
async def recipient_search_result(message: Message, state: FSMContext):
    if not await guard(message):
        return
    search = (message.text or "").strip().lstrip("@")
    if not search:
        return await message.answer("Введите username или его часть.")
    users = await db.recipients(message.from_user.id, "name", search)
    rows = [[InlineKeyboardButton(
        text=f"@{u['username']} · {u['name']}"[:64],
        callback_data=f"transfer:pick:{u['telegram_id']}",
    )] for u in users]
    rows.append([InlineKeyboardButton(text="← К списку", callback_data="transfer:list:name")])
    await state.clear()
    await message.answer(
        f"<b>Результаты поиска «{escape(search)}»</b>\n"
        + ("Выберите получателя:" if users else "Ничего не найдено."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.inline_query()
async def inline_recipient_search(query: InlineQuery, state: FSMContext):
    user = await db.authorize(query.from_user.id, query.from_user.username)
    if not user:
        return await query.answer(
            [],
            cache_time=1,
            is_personal=True,
            switch_pm_text="Нет доступа к nlogn-bank",
            switch_pm_parameter="access",
        )
    search = query.query.strip().lstrip("@")
    users = await db.recipients(
        query.from_user.id, "name", search, limit=40, include_self=True
    )
    current_state = await state.get_state()
    state_data = await state.get_data()
    result_kind = "admin" if current_state == Flow.admin_user.state else "recipient"
    action_label = {
        "credit": "Начислить",
        "debit": "Списать",
        "remove_admin": "Удалить администратора",
        "flag": "Изменить статус",
    }.get(state_data.get("action"), "Выбрать")
    results = []
    for recipient in users:
        username = recipient["username"]
        stats = (
            f"Получено: {recipient['received_count']} · "
            f"Отправлено: {recipient['sent_count']} · "
            f"Сумма: {recipient['sent_amount']}"
        )
        results.append(InlineQueryResultArticle(
            id=f"{result_kind}-{recipient['telegram_id']}",
            title=f"@{username} — {recipient['name']}",
            description=(f"{action_label} · " if result_kind == "admin" else "") + stats,
            input_message_content=InputTextMessageContent(
                message_text=f"{action_label}: <b>@{escape(username)}</b>",
                parse_mode=ParseMode.HTML,
            ),
        ))
    await query.answer(results, cache_time=1, is_personal=True)


@router.chosen_inline_result()
async def inline_recipient_chosen(chosen: ChosenInlineResult, state: FSMContext):
    if not (
        chosen.result_id.startswith("recipient-")
        or chosen.result_id.startswith("admin-")
    ):
        return
    user = await db.authorize(chosen.from_user.id, chosen.from_user.username)
    if not user:
        return
    try:
        target_id = int(chosen.result_id.split("-", 1)[1])
    except ValueError:
        return
    target = await db.user(target_id)
    if not target:
        return
    if chosen.result_id.startswith("admin-"):
        await handle_admin_inline_choice(chosen, state, user, target)
        return
    if target_id == chosen.from_user.id:
        await bot.send_message(
            chosen.from_user.id,
            "Нельзя переводить деньги самому себе.",
            reply_markup=persistent_menu(),
        )
        return
    await state.update_data(target=target_id, target_name=target["username"])
    await state.set_state(Flow.transfer_amount)
    try:
        await bot.send_message(
            chosen.from_user.id,
            f"Получатель: <b>@{escape(target['username'] or '')}</b>\nВведите сумму:",
            reply_markup=kb(("Отмена", "menu")),
        )
    except Exception:
        logging.exception("Не удалось открыть ввод суммы после inline-поиска")


async def handle_admin_inline_choice(
    chosen: ChosenInlineResult, state: FSMContext, actor, target
) -> None:
    is_owner = chosen.from_user.id == OWNER_TELEGRAM_ID or (
        chosen.from_user.username or ""
    ).lower() == OWNER_USERNAME
    if not (actor["is_admin"] or is_owner):
        await state.clear()
        return
    data = await state.get_data()
    action = data.get("action")
    if action in {"credit", "debit"}:
        await state.update_data(
            target=target["telegram_id"], target_name=target["username"]
        )
        await state.set_state(Flow.admin_amount)
        await bot.send_message(
            chosen.from_user.id,
            f"Выбран @{escape(target['username'] or '')}. Введите сумму:",
            reply_markup=persistent_menu(),
        )
        return
    if action == "flag":
        field = data.get("field")
        if field not in {"frozen", "is_admin", "is_main_admin", "verified_employer"}:
            await state.clear()
            return
        if field == "is_main_admin" and not (actor["is_main_admin"] or is_owner):
            await state.clear()
            await bot.send_message(chosen.from_user.id, "Недостаточно прав.")
            return
        if target["is_main_admin"] and field in {"frozen", "is_admin"}:
            await bot.send_message(
                chosen.from_user.id,
                "Главного администратора нельзя заморозить или лишить прав.",
            )
            return
        value = await db.toggle_flag(target["telegram_id"], field)
        labels = {
            "frozen": "заморозка счёта",
            "is_admin": "права администратора",
            "is_main_admin": "права главного администратора",
            "verified_employer": "проверенный работодатель",
        }
        await state.clear()
        await bot.send_message(
            chosen.from_user.id,
            f"✅ {labels[field]} для @{escape(target['username'] or '')}: "
            f"{'ВКЛ' if value else 'ВЫКЛ'}",
        )
        await staff_log(
            f"@{escape(chosen.from_user.username or str(chosen.from_user.id))}: "
            f"{labels[field]} для @{escape(target['username'] or '')} → {value}"
        )
        return
    if action == "remove_admin" and is_owner:
        removed_from_env = remove_admin_from_env(target["username"] or "")
        removed = await db.remove_admin(target["telegram_id"])
        await state.clear()
        text = (
            f"✅ Права администратора у @{escape(target['username'] or '')} удалены."
            if removed or removed_from_env
            else "У пользователя уже нет прав администратора."
        )
        await bot.send_message(chosen.from_user.id, text)


@router.callback_query(F.data.startswith("transfer:pick:"))
async def recipient_pick(call: CallbackQuery, state: FSMContext):
    if not await guard(call):
        return
    target_id = int(call.data.rsplit(":", 1)[1])
    target = await db.user(target_id)
    if not target or target_id == call.from_user.id:
        return await call.answer("Получатель недоступен.", show_alert=True)
    await state.update_data(target=target_id, target_name=target["username"])
    await state.set_state(Flow.transfer_amount)
    await call.answer()
    await call.message.edit_text(
        f"Получатель: <b>@{escape(target['username'] or '')}</b>\nВведите сумму:",
        reply_markup=kb(("Отмена", "menu")),
    )


@router.message(Flow.transfer_user)
async def transfer_user(message: Message, state: FSMContext):
    if not await guard(message):
        return
    target = await db.user_by_username(message.text or "")
    if not target or target["telegram_id"] == message.from_user.id:
        return await message.answer("Получатель не найден или это вы. Введите другой @username.")
    await state.update_data(target=target["telegram_id"], target_name=target["username"])
    await state.set_state(Flow.transfer_amount)
    await message.answer("Введите целую положительную сумму:")


@router.message(Flow.transfer_amount)
async def transfer_amount(message: Message, state: FSMContext):
    if not await guard(message):
        return
    try:
        amount = int(message.text or "")
        data = await state.get_data()
        if amount <= 0:
            raise ValueError
    except ValueError:
        return await message.answer("Введите сумму целым положительным числом.")
    await state.update_data(amount=amount)
    await message.answer(
        f"<b>Подтвердите перевод</b>\n\nПолучатель: "
        f"@{escape(data['target_name'] or '')}\nСумма: <b>{amount} nlogn-коинов</b>",
        reply_markup=kb(
            ("✅ Подтвердить", "transfer:confirm"),
            ("❌ Отмена", "menu"),
        ),
    )


@router.callback_query(F.data == "transfer:confirm")
async def transfer_confirm(call: CallbackQuery, state: FSMContext):
    if not await guard(call):
        return
    lock = transfer_locks.setdefault(call.from_user.id, asyncio.Lock())
    async with lock:
        data = await state.get_data()
        if not {"target", "target_name", "amount"} <= data.keys():
            return await call.answer("Перевод уже обработан или устарел.", show_alert=True)
        try:
            await db.transfer(call.from_user.id, data["target"], data["amount"])
        except BankError as exc:
            return await call.answer(str(exc), show_alert=True)
        await state.clear()
    await sync_transfer_log()
    await call.answer("Перевод выполнен!")
    await call.message.edit_text(
        f"✅ Переведено <b>{data['amount']}</b> пользователю "
        f"@{escape(data['target_name'] or '')}.",
        reply_markup=await menu_markup(call.from_user.id),
    )
    try:
        await bot.send_message(
            data["target"],
            f"⚡️ Вам поступил перевод: <b>{data['amount']} nlogn-коинов</b> "
            f"от @{escape(call.from_user.username or str(call.from_user.id))}!",
        )
    except Exception:
        pass


@router.callback_query(F.data == "employers")
async def employers(call: CallbackQuery):
    if not await guard(call):
        return
    users = await db.verified_employers()
    lines = [f"• {escape(u['name'])} (@{escape(u['username'] or 'без_username')})" for u in users]
    await call.answer()
    await call.message.edit_text("<b>Проверенные работодатели</b>\n\n" + ("\n".join(lines) or "Список пуст."),
                                 reply_markup=kb(("← В меню", "menu")))


def task_text(t) -> str:
    status = {
        "open": "Свободна", "working": "В работе", "review": "На проверке",
        "done": "Закрыта", "cancelled": "Отменена",
    }[t["status"]]
    verified = " ✅ Проверенный работодатель" if t["verified_employer"] else ""
    type_text = "Один исполнитель" if t["task_type"] == "single" else "Несколько исполнителей"
    return (f"<b>#{t['id']} · {escape(t['title'])}</b>{verified}\n{escape(t['description'])}\n"
            f"Награда: <b>{t['reward']}</b> · Статус: {status}\nТип: {type_text}"
            + (f"\nДедлайн: {escape(t['deadline'])}" if t["deadline"] else "")
            + (f"\nИсполнитель: @{escape(t['worker'])}" if t["worker"] else ""))


@router.callback_query(F.data.startswith("tasks:"))
async def task_lists(call: CallbackQuery):
    if not await guard(call):
        return
    mode = call.data.split(":")[1]
    rows = await db.tasks(call.from_user.id, mode)
    buttons = []
    for t in rows:
        action = f"task:view:{t['id']}:{mode}"
        mark = "✅ " if t["verified_employer"] else ""
        buttons.append([InlineKeyboardButton(
            text=f"{mark}#{t['id']} · {t['title'][:30]} · {t['reward']}",
            callback_data=action,
        )])
    buttons.append([InlineKeyboardButton(text="← В меню", callback_data="menu")])
    names = {"open": "Доступные задачи", "worker": "Мои текущие задачи", "owner": "Мои вакансии"}
    await call.answer()
    await call.message.edit_text(f"<b>{names[mode]}</b>\n\n" + ("Выберите задачу:" if rows else "Здесь пока пусто."),
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("task:view:"))
async def task_view(call: CallbackQuery):
    user = await guard(call)
    if not user:
        return
    _, _, raw_id, mode = call.data.split(":")
    t = await db.task(int(raw_id))
    if not t:
        return await call.answer("Задача не найдена.", show_alert=True)
    rows = []
    if mode == "open" and t["status"] == "open":
        rows.append([InlineKeyboardButton(text="Взять в работу", callback_data=f"task:take:{t['id']}")])
    can_report = (
        mode == "worker"
        and t["status"] in {"open", "working"}
        and (t["worker_id"] == call.from_user.id or t["task_type"] == "multi")
    )
    if can_report:
        rows.append([InlineKeyboardButton(text="Сдать работу", callback_data=f"task:report:{t['id']}")])
    if t["status"] not in {"done", "cancelled"} and (
        t["creator_id"] == call.from_user.id or user["is_admin"] or user["is_main_admin"]
    ):
        rows.append([InlineKeyboardButton(
            text="🗑 Отменить задачу", callback_data=f"task:cancel:{t['id']}"
        )])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"tasks:{mode}")])
    await call.answer()
    await call.message.edit_text(task_text(t), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("task:take:"))
async def task_take(call: CallbackQuery):
    if not await guard(call):
        return
    task_id = int(call.data.rsplit(":", 1)[1])
    try:
        await db.take_task(task_id, call.from_user.id, cfg.max_active_tasks)
    except BankError as exc:
        return await call.answer(str(exc), show_alert=True)
    t = await db.task(task_id)
    await call.answer("Задача зарезервирована!")
    await call.message.edit_text(task_text(t), reply_markup=kb(("← В меню", "menu")))
    try:
        await bot.send_message(t["creator_id"], f"🔔 Задачу <b>#{task_id} {escape(t['title'])}</b> взял @{escape(call.from_user.username or str(call.from_user.id))}.")
    except Exception:
        pass


@router.callback_query(F.data == "task:new")
async def task_new(call: CallbackQuery, state: FSMContext):
    user = await guard(call)
    if not user:
        return
    if not (user["is_admin"] or user["is_main_admin"] or user["verified_employer"]):
        return await call.answer(
            "Публиковать задачи могут только работодатели и администраторы.",
            show_alert=True,
        )
    if user["frozen"]:
        return await call.answer("Ваш счёт заморожен.", show_alert=True)
    task_limit = 50 if user["verified_employer"] else 10
    await state.clear()
    await call.answer()
    await call.message.edit_text(
        f"<b>Выберите тип задачи</b>\n\nВаш лимит: до {task_limit} активных задач.\n"
        "У массовой задачи отчёт первого исполнителя временно закрывает приём остальных.",
        reply_markup=kb(
            ("👤 Один исполнитель", "task:type:single"),
            ("👥 Несколько исполнителей", "task:type:multi"),
            ("Отмена", "menu"),
        ),
    )


@router.callback_query(F.data.startswith("task:type:"))
async def task_type(call: CallbackQuery, state: FSMContext):
    if not await guard(call):
        return
    selected = call.data.rsplit(":", 1)[1]
    if selected not in {"single", "multi"}:
        return await call.answer("Неизвестный тип.", show_alert=True)
    await state.update_data(task_type=selected)
    await state.set_state(Flow.task_title)
    await call.answer()
    await call.message.edit_text(
        "Введите название задачи:", reply_markup=kb(("Отмена", "menu"))
    )


@router.message(Flow.task_title)
async def new_title(message: Message, state: FSMContext):
    if not await guard(message):
        return
    if not message.text or len(message.text) > 100:
        return await message.answer("Название должно быть текстом до 100 символов.")
    await state.update_data(title=message.text)
    await state.set_state(Flow.task_description)
    await message.answer("Введите описание задачи:")


@router.message(Flow.task_description)
async def new_description(message: Message, state: FSMContext):
    if not message.text or len(message.text) > 1500:
        return await message.answer("Описание должно быть текстом до 1500 символов.")
    await state.update_data(description=message.text)
    await state.set_state(Flow.task_reward)
    await message.answer("Введите награду целым числом:")


@router.message(Flow.task_reward)
async def new_reward(message: Message, state: FSMContext):
    try:
        reward = int(message.text or "")
        if reward <= 0:
            raise ValueError
    except ValueError:
        return await message.answer("Введите целое положительное число.")
    await state.update_data(reward=reward)
    await state.set_state(Flow.task_deadline)
    await message.answer("Введите дедлайн свободным текстом или «нет»:")


@router.message(Flow.task_deadline)
async def new_deadline(message: Message, state: FSMContext):
    data = await state.get_data()
    deadline = None if (message.text or "").lower() in {"нет", "-", "no"} else (message.text or "")[:100]
    try:
        task_id = await db.create_task(message.from_user.id, data["title"], data["description"],
                                       data["reward"], deadline,
                                       data.get("task_type", "single"))
    except BankError as exc:
        return await message.answer(str(exc))
    await state.clear()
    await message.answer(f"✅ Задача <b>#{task_id}</b> опубликована. Награда помещена в холд.",
                         reply_markup=await menu_markup(message.from_user.id))


@router.callback_query(F.data.startswith("task:cancel:"))
async def task_cancel(call: CallbackQuery):
    if not await guard(call):
        return
    task_id = int(call.data.rsplit(":", 1)[1])
    try:
        owner_id = await db.cancel_task(task_id, call.from_user.id)
    except BankError as exc:
        return await call.answer(str(exc), show_alert=True)
    await call.answer("Задача отменена, холд возвращён.")
    await call.message.edit_text(
        f"✅ Задача <b>#{task_id}</b> отменена. Награда возвращена из холда.",
        reply_markup=kb(("← В меню", "menu")),
    )
    if owner_id != call.from_user.id:
        try:
            await bot.send_message(owner_id, f"🏛 Администратор отменил задачу #{task_id}.")
        except Exception:
            pass


@router.callback_query(F.data.startswith("task:report:"))
async def report_begin(call: CallbackQuery, state: FSMContext):
    if not await guard(call):
        return
    task_id = int(call.data.rsplit(":", 1)[1])
    await state.update_data(task_id=task_id)
    await state.set_state(Flow.report)
    await call.answer()
    await call.message.edit_text("Пришлите отчёт: текст, фото/скриншот или ссылку.", reply_markup=kb(("Отмена", "menu")))


@router.message(Flow.report)
async def report_submit(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.photo:
        report_type, value = "photo", message.photo[-1].file_id
    elif message.text:
        report_type, value = "text", message.text[:3000]
    else:
        return await message.answer("Поддерживается текст или фото.")
    try:
        owner = await db.submit_report(data["task_id"], message.from_user.id, report_type, value)
    except BankError as exc:
        return await message.answer(str(exc))
    await state.clear()
    controls = kb(("✅ Принять", f"task:accept:{data['task_id']}"),
                  ("❌ Отклонить", f"task:reject:{data['task_id']}"))
    caption = f"📨 Отчёт по задаче <b>#{data['task_id']}</b> от @{escape(message.from_user.username or str(message.from_user.id))}"
    if report_type == "photo":
        await bot.send_photo(owner, value, caption=caption, reply_markup=controls)
    else:
        await bot.send_message(owner, caption + "\n\n" + escape(value), reply_markup=controls)
    await message.answer("✅ Отчёт отправлен работодателю.", reply_markup=await menu_markup(message.from_user.id))


@router.callback_query(F.data.startswith("task:accept:"))
async def accept(call: CallbackQuery):
    if not await guard(call):
        return
    task_id = int(call.data.rsplit(":", 1)[1])
    try:
        worker = await db.accept_task(task_id, call.from_user.id)
    except BankError as exc:
        return await call.answer(str(exc), show_alert=True)
    await call.answer("Работа принята, награда выплачена!")
    await sync_transfer_log()
    await call.message.edit_reply_markup(reply_markup=None)
    await bot.send_message(worker, f"🎉 Работа по задаче <b>#{task_id}</b> принята. Награда зачислена!")


@router.callback_query(F.data.startswith("task:reject:"))
async def reject_begin(call: CallbackQuery, state: FSMContext):
    if not await guard(call):
        return
    await state.update_data(task_id=int(call.data.rsplit(":", 1)[1]))
    await state.set_state(Flow.reject_reason)
    await call.answer()
    await call.message.answer("Напишите причину отклонения:")


@router.message(Flow.reject_reason)
async def reject_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        worker = await db.reject_task(data["task_id"], message.from_user.id, (message.text or "Без пояснения")[:1000])
    except BankError as exc:
        return await message.answer(str(exc))
    await state.clear()
    await message.answer("Отчёт отклонён; задача возвращена исполнителю.")
    await bot.send_message(worker, f"↩️ Отчёт по задаче <b>#{data['task_id']}</b> отклонён.\nПричина: {escape(message.text or 'Без пояснения')}")


def election_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Кандидаты", callback_data="election:candidates")],
        [InlineKeyboardButton(text="Стать кандидатом", callback_data="election:apply")],
        [InlineKeyboardButton(text="Моя анкета", callback_data="election:mine"),
         InlineKeyboardButton(text="Мой голос", callback_data="election:my_vote")],
        [InlineKeyboardButton(text="← В меню", callback_data="menu")],
    ])


async def send_candidate(chat_id: int, candidate, media,
                         moderation: bool = False, voting: bool = True) -> None:
    username = f"@{candidate['username']}" if candidate["username"] else "без username"
    caption = (
        f"<b>Кандидат #{candidate['id']}</b>\n"
        f"{escape(candidate['name'])} · {escape(username)}\n"
        f"Telegram ID: <code>{candidate['user_id']}</code>\n\n"
        f"{escape(candidate['program'])}"
    )
    photos = [item["file_id"] for item in media if item["media_type"] == "photo"]
    videos = [item["file_id"] for item in media if item["media_type"] == "video"]
    audios = [item["file_id"] for item in media if item["media_type"] == "audio"]
    if len(photos) == 1:
        await bot.send_photo(chat_id, photos[0], caption=caption)
    elif photos:
        photo_group = [
            InputMediaPhoto(media=file_id, caption=caption if index == 0 else None)
            for index, file_id in enumerate(photos)
        ]
        await bot.send_media_group(chat_id, photo_group)
    else:
        await bot.send_message(chat_id, caption + "\n\n<i>Фотографии ещё не добавлены.</i>")
    for file_id in videos:
        await bot.send_video(chat_id, file_id)
    for file_id in audios:
        await bot.send_audio(chat_id, file_id)
    if moderation:
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Одобрить ✅", callback_data=f"election:approve:{candidate['id']}"
            ),
            InlineKeyboardButton(
                text="Отклонить ❌", callback_data=f"election:reject:{candidate['id']}"
            ),
        ]])
        await bot.send_message(chat_id, "Решение по заявке:", reply_markup=markup)
    elif voting:
        await bot.send_message(
            chat_id,
            f"Голосов: <b>{candidate['votes']}</b>",
            reply_markup=kb(("Голосовать 🗳", f"election:vote:{candidate['id']}")),
        )


async def notify_candidate_voters(candidate_id: int, text: str) -> int:
    sent = 0
    for voter_id in await db.election_voter_ids(candidate_id):
        try:
            await bot.send_message(voter_id, text)
            sent += 1
        except Exception:
            logging.exception("Не удалось уведомить избирателя %s", voter_id)
    return sent


@router.callback_query(F.data == "election")
async def election(call: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await guard(call):
        return
    await call.answer()
    await call.message.edit_text("<b>Электоральная площадка</b>\nВыберите действие:",
                                 reply_markup=election_markup())


@router.callback_query(F.data == "election:apply")
async def election_apply(call: CallbackQuery, state: FSMContext):
    if not await guard(call):
        return
    existing = await db.election_candidate_by_user(call.from_user.id)
    if existing and existing[0]["status"] in {"pending", "approved"}:
        return await call.answer("У вас уже есть активная анкета.", show_alert=True)
    await state.set_state(Flow.election_program)
    await call.answer()
    await call.message.edit_text(
        "<b>Регистрация кандидата</b>\n\nПришлите текст предвыборной программы "
        "(до 900 символов).",
        reply_markup=kb(("Отмена", "election")),
    )


@router.message(Flow.election_program)
async def election_program(message: Message, state: FSMContext):
    if not await guard(message):
        return
    program = (message.text or "").strip()
    if not program:
        return await message.answer("Пришлите программу текстовым сообщением.")
    if len(program) > 900:
        return await message.answer("Программа слишком длинная. Максимум — 900 символов.")
    try:
        candidate_id = await db.election_start_candidate(message.from_user.id, program)
    except BankError as exc:
        return await message.answer(str(exc))
    await state.update_data(candidate_id=candidate_id)
    await state.set_state(Flow.election_photos)
    await message.answer(
        "Теперь пришлите от 1 до 5 фотографий. Можно отправить их альбомом. "
        "Когда закончите, нажмите «Готово».",
        reply_markup=kb(("Готово", "election:photos:done"), ("Отмена", "election")),
    )


@router.message(Flow.election_photos, F.photo)
async def election_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        count = await db.election_add_photo(data["candidate_id"], message.photo[-1].file_id)
    except BankError as exc:
        return await message.answer(str(exc))
    await message.answer(
        f"Фото добавлено ({count}/5).",
        reply_markup=kb(("Готово", "election:photos:done"), ("Отмена", "election")),
    )


@router.message(Flow.election_photos)
async def election_photo_invalid(message: Message):
    await message.answer("Ожидаю фотографию или нажатие кнопки «Готово».")


@router.callback_query(F.data == "election:photos:done")
async def election_photos_done(call: CallbackQuery, state: FSMContext):
    if not await guard(call):
        return
    data = await state.get_data()
    try:
        candidate, photos = await db.election_submit(
            data.get("candidate_id", 0), call.from_user.id
        )
    except BankError as exc:
        return await call.answer(str(exc), show_alert=True)
    await state.clear()
    await call.answer()
    await call.message.edit_text(
        "Ваша заявка отправлена админам.",
        reply_markup=election_markup(),
    )
    await notify_candidate_voters(
        candidate["id"],
        f"ℹ️ Кандидат <b>{escape(candidate['name'])}</b>, за которого вы голосовали, "
        "изменил анкету. Новая версия отправлена на модерацию; ваш голос сохранён.",
    )
    admin_ids = set(await db.admin_ids())
    admin_ids.add(OWNER_TELEGRAM_ID)
    for admin_id in admin_ids:
        try:
            await send_candidate(admin_id, candidate, photos, moderation=True)
        except Exception:
            logging.exception("Не удалось отправить заявку кандидата админу %s", admin_id)


@router.callback_query(F.data == "election:mine")
async def election_mine(call: CallbackQuery):
    if not await guard(call):
        return
    item = await db.election_candidate_by_user(call.from_user.id)
    if not item:
        return await call.answer("У вас пока нет анкеты.", show_alert=True)
    candidate, photos = item
    labels = {"draft": "Черновик", "pending": "На модерации", "approved": "Одобрена"}
    await call.answer()
    await call.message.answer(
        f"<b>Моя анкета</b>\nСтатус: <b>{labels[candidate['status']]}</b>"
    )
    await send_candidate(call.from_user.id, candidate, photos, voting=False)
    await call.message.answer(
        "Управление материалами анкеты:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✏️ Изменить описание",
                callback_data="election:program:edit",
            )],
            [InlineKeyboardButton(
                text="➕ Добавить фото / видео / аудио",
                callback_data="election:media:add",
            )],
            [InlineKeyboardButton(
                text="🗑 Удалить медиафайл",
                callback_data="election:media:delete:list",
            )],
            [InlineKeyboardButton(text="← К выборам", callback_data="election")],
        ]),
    )


@router.callback_query(F.data == "election:program:edit")
async def election_program_edit_begin(call: CallbackQuery, state: FSMContext):
    if not await guard(call):
        return
    item = await db.election_candidate_by_user(call.from_user.id)
    if not item:
        return await call.answer("Анкета не найдена.", show_alert=True)
    candidate, _ = item
    await state.update_data(candidate_id=candidate["id"])
    await state.set_state(Flow.election_program_edit)
    await call.answer()
    await call.message.answer(
        "<b>Изменение описания</b>\n\nПришлите новый текст предвыборной "
        "программы (до 900 символов).",
        reply_markup=kb(("Отмена", "election")),
    )


@router.message(Flow.election_program_edit)
async def election_program_edit_save(message: Message, state: FSMContext):
    if not await guard(message):
        return
    program = (message.text or "").strip()
    if not program:
        return await message.answer("Пришлите описание текстовым сообщением.")
    if len(program) > 900:
        return await message.answer("Описание слишком длинное. Максимум — 900 символов.")
    data = await state.get_data()
    try:
        candidate, media = await db.election_update_program(
            data["candidate_id"], message.from_user.id, program
        )
    except BankError as exc:
        return await message.answer(str(exc))
    await state.clear()
    await message.answer(
        "Описание изменено. Анкета отправлена на повторную модерацию.",
        reply_markup=election_markup(),
    )
    await notify_candidate_voters(
        candidate["id"],
        f"ℹ️ Кандидат <b>{escape(candidate['name'])}</b>, за которого вы голосовали, "
        "изменил описание анкеты. Новая версия отправлена на модерацию; "
        "ваш голос сохранён.",
    )
    admin_ids = set(await db.admin_ids())
    admin_ids.add(OWNER_TELEGRAM_ID)
    for admin_id in admin_ids:
        try:
            await send_candidate(admin_id, candidate, media, moderation=True)
        except Exception:
            logging.exception(
                "Не удалось отправить обновлённую заявку админу %s", admin_id
            )


@router.callback_query(F.data == "election:media:add")
async def election_media_add_begin(call: CallbackQuery, state: FSMContext):
    if not await guard(call):
        return
    item = await db.election_candidate_by_user(call.from_user.id)
    if not item:
        return await call.answer("Анкета не найдена.", show_alert=True)
    candidate, _ = item
    await db.election_begin_edit(candidate["id"], call.from_user.id)
    await state.update_data(candidate_id=candidate["id"])
    await state.set_state(Flow.election_media_edit)
    await call.answer()
    await call.message.answer(
        "Отправляйте фото, видео или аудио. Всего в анкете может быть до 15 файлов, "
        "из них до 5 фотографий. После изменений нажмите «Отправить на модерацию».",
        reply_markup=kb(
            ("Отправить на модерацию", "election:photos:done"),
            ("Отмена", "election"),
        ),
    )


@router.message(
    Flow.election_media_edit,
    F.photo | F.video | F.audio,
)
async def election_media_add(message: Message, state: FSMContext):
    if not await guard(message):
        return
    if message.photo:
        media_type, file_id = "photo", message.photo[-1].file_id
    elif message.video:
        media_type, file_id = "video", message.video.file_id
    else:
        media_type, file_id = "audio", message.audio.file_id
    data = await state.get_data()
    try:
        count = await db.election_add_media(data["candidate_id"], file_id, media_type)
    except BankError as exc:
        return await message.answer(str(exc))
    labels = {"photo": "Фото", "video": "Видео", "audio": "Аудио"}
    await message.answer(
        f"{labels[media_type]} добавлено. Всего файлов: {count}/15.",
        reply_markup=kb(
            ("Отправить на модерацию", "election:photos:done"),
            ("Отмена", "election"),
        ),
    )


@router.message(Flow.election_media_edit)
async def election_media_invalid(message: Message):
    await message.answer("Пришлите фотографию, видео или аудиофайл.")


@router.callback_query(F.data == "election:media:delete:list")
async def election_media_delete_list(call: CallbackQuery, state: FSMContext):
    if not await guard(call):
        return
    item = await db.election_candidate_by_user(call.from_user.id)
    if not item:
        return await call.answer("Анкета не найдена.", show_alert=True)
    candidate, media = item
    if not media:
        return await call.answer("В анкете нет медиафайлов.", show_alert=True)
    await db.election_begin_edit(candidate["id"], call.from_user.id)
    await state.update_data(candidate_id=candidate["id"])
    await state.set_state(Flow.election_media_edit)
    icons = {"photo": "📷 Фото", "video": "🎬 Видео", "audio": "🎵 Аудио"}
    rows = [
        [InlineKeyboardButton(
            text=f"Удалить {icons[item['media_type']]} #{index}",
            callback_data=f"election:media:delete:{item['id']}",
        )]
        for index, item in enumerate(media, 1)
    ]
    rows.append([InlineKeyboardButton(
        text="Отправить на модерацию", callback_data="election:photos:done"
    )])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="election")])
    await call.answer()
    await call.message.answer(
        "Выберите медиафайл для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("election:media:delete:"))
async def election_media_delete(call: CallbackQuery, state: FSMContext):
    if not await guard(call):
        return
    data = await state.get_data()
    try:
        media_id = int(call.data.rsplit(":", 1)[1])
        await db.election_delete_media(
            data.get("candidate_id", 0), media_id, call.from_user.id
        )
    except (ValueError, BankError) as exc:
        return await call.answer(str(exc), show_alert=True)
    await call.answer("Медиафайл удалён.", show_alert=True)
    await call.message.edit_text(
        "Медиафайл удалён. Можно удалить другие через «Мою анкету» или отправить "
        "текущую версию на повторную модерацию.",
        reply_markup=kb(
            ("Отправить на модерацию", "election:photos:done"),
            ("Моя анкета", "election:mine"),
        ),
    )


@router.callback_query(F.data == "election:candidates")
async def election_candidates(call: CallbackQuery):
    if not await guard(call):
        return
    candidates = await db.election_approved()
    await call.answer()
    await call.message.edit_text(
        "<b>Кандидаты</b>\n" + (
            "Список опубликованных участников:" if candidates else "Одобренных кандидатов пока нет."
        ),
        reply_markup=election_markup(),
    )
    for candidate, photos in candidates:
        await send_candidate(call.from_user.id, candidate, photos)


@router.callback_query(F.data.regexp(r"^election:vote:\d+$"))
async def election_vote(call: CallbackQuery):
    if not await guard(call):
        return
    candidate_id = int(call.data.rsplit(":", 1)[1])
    try:
        votes = await db.election_vote(candidate_id, call.from_user.id)
    except BankError as exc:
        return await call.answer(str(exc), show_alert=True)
    await call.answer("Ваш голос принят!", show_alert=True)
    await call.message.edit_text(
        f"Голосов: <b>{votes}</b>",
        reply_markup=kb(("Голосовать 🗳", f"election:vote:{candidate_id}")),
    )


@router.callback_query(F.data == "election:my_vote")
async def election_my_vote(call: CallbackQuery):
    if not await guard(call):
        return
    candidate = await db.election_vote_choice(call.from_user.id)
    if not candidate:
        return await call.answer("Вы ещё не голосовали.", show_alert=True)
    statuses = {
        "draft": "анкета редактируется",
        "pending": "анкета на модерации",
        "approved": "участвует в выборах",
    }
    username = f"@{candidate['username']}" if candidate["username"] else "без username"
    await call.answer()
    await call.message.edit_text(
        f"<b>Ваш голос</b>\n\n"
        f"Кандидат #{candidate['id']}: <b>{escape(candidate['name'])}</b> "
        f"({escape(username)})\n"
        f"Статус: {statuses[candidate['status']]}\n"
        f"Текущий счётчик: <b>{candidate['votes']}</b>",
        reply_markup=kb(
            ("Отменить голос", "election:vote:cancel"),
            ("← К выборам", "election"),
        ),
    )


@router.callback_query(F.data == "election:vote:cancel")
async def election_cancel_vote(call: CallbackQuery):
    if not await guard(call):
        return
    try:
        _, votes = await db.election_cancel_vote(call.from_user.id)
    except BankError as exc:
        return await call.answer(str(exc), show_alert=True)
    await call.answer("Ваш голос отменён.", show_alert=True)
    await call.message.edit_text(
        f"Ваш голос отменён. Новый счётчик кандидата: <b>{votes}</b>.",
        reply_markup=election_markup(),
    )


@router.callback_query(F.data.startswith("election:approve:"))
async def election_approve(call: CallbackQuery):
    if not await guard(call, admin=True):
        return
    candidate_id = int(call.data.rsplit(":", 1)[1])
    try:
        user_id = await db.election_moderate(candidate_id, True)
    except BankError as exc:
        return await call.answer(str(exc), show_alert=True)
    await call.answer("Кандидат одобрен.", show_alert=True)
    await call.message.edit_text("✅ Заявка одобрена.")
    try:
        await bot.send_message(user_id, "✅ Ваша анкета кандидата одобрена и опубликована.")
    except Exception:
        pass


@router.callback_query(F.data.startswith("election:reject:"))
async def election_reject(call: CallbackQuery, state: FSMContext):
    if not await guard(call, main=True):
        return
    await state.update_data(candidate_id=int(call.data.rsplit(":", 1)[1]))
    await state.set_state(Flow.election_reject_reason)
    await call.answer()
    await call.message.answer("Укажите причину отклонения:")


@router.message(Flow.election_reject_reason)
async def election_reject_reason(message: Message, state: FSMContext):
    if not await guard(message, main=True):
        return
    reason = (message.text or "").strip()
    if not reason:
        return await message.answer("Причина должна быть текстом.")
    data = await state.get_data()
    voter_ids = await db.election_voter_ids(data["candidate_id"])
    try:
        user_id = await db.election_moderate(data["candidate_id"], False)
    except BankError as exc:
        return await message.answer(str(exc))
    await state.clear()
    await message.answer("❌ Заявка отклонена и удалена.")
    try:
        await bot.send_message(
            user_id, f"❌ Ваша анкета кандидата отклонена.\nПричина: {escape(reason[:1000])}"
        )
    except Exception:
        pass
    for voter_id in voter_ids:
        try:
            await bot.send_message(
                voter_id,
                "ℹ️ Кандидат, за которого вы голосовали, отклонён модерацией. "
                "Ваш голос освобождён, вы можете выбрать другого кандидата.",
            )
        except Exception:
            pass


def election_admin_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Удалить участника", callback_data="admin:election:delete")],
        [InlineKeyboardButton(text="Изменить голоса", callback_data="admin:election:adjust")],
        [InlineKeyboardButton(text="← В админ-панель", callback_data="admin")],
    ])


@router.callback_query(F.data == "admin:election")
async def election_admin(call: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await guard(call, main=True):
        return
    await call.answer()
    await call.message.edit_text("<b>Управление выборами</b>", reply_markup=election_admin_markup())


@router.callback_query(F.data.in_({"admin:election:delete", "admin:election:adjust"}))
async def election_admin_begin(call: CallbackQuery, state: FSMContext):
    if not await guard(call, main=True):
        return
    await state.update_data(election_action=call.data.rsplit(":", 1)[1])
    await state.set_state(Flow.election_admin_candidate)
    await call.answer()
    await call.message.answer("Введите числовой ID кандидата:")


@router.message(Flow.election_admin_candidate)
async def election_admin_candidate(message: Message, state: FSMContext):
    if not await guard(message, main=True):
        return
    try:
        candidate_id = int(message.text or "")
    except ValueError:
        return await message.answer("Введите числовой ID кандидата.")
    data = await state.get_data()
    if data["election_action"] == "delete":
        voter_ids = await db.election_voter_ids(candidate_id)
        try:
            user_id = await db.election_delete(candidate_id)
        except BankError as exc:
            return await message.answer(str(exc))
        await state.clear()
        await message.answer("Кандидат удалён вместе со всеми голосами.",
                             reply_markup=election_admin_markup())
        try:
            await bot.send_message(user_id, "Ваша кандидатура принудительно снята с выборов.")
        except Exception:
            pass
        for voter_id in voter_ids:
            try:
                await bot.send_message(
                    voter_id,
                    "ℹ️ Кандидат, за которого вы голосовали, снят с выборов. "
                    "Ваш голос освобождён, вы можете выбрать другого кандидата.",
                )
            except Exception:
                pass
        return
    await state.update_data(candidate_id=candidate_id)
    await state.set_state(Flow.election_admin_delta)
    await message.answer("Введите изменение голосов целым числом, например 10 или -3:")


@router.message(Flow.election_admin_delta)
async def election_admin_delta(message: Message, state: FSMContext):
    if not await guard(message, main=True):
        return
    try:
        delta = int(message.text or "")
    except ValueError:
        return await message.answer("Введите целое число со знаком или без.")
    data = await state.get_data()
    try:
        total = await db.election_adjust_votes(data["candidate_id"], delta)
    except BankError as exc:
        return await message.answer(str(exc))
    await state.clear()
    await message.answer(
        f"Счётчик изменён на {delta:+d}. Новый результат: {total}.",
        reply_markup=election_admin_markup(),
    )


def admin_markup(main: bool, owner: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="➕ Эмиссия", callback_data="admin:money:credit"),
         InlineKeyboardButton(text="➖ Налог / штраф", callback_data="admin:money:debit")],
        [InlineKeyboardButton(text="🔐 Счёт", callback_data="admin:flag:frozen"),
         InlineKeyboardButton(text="✅ Проверенный", callback_data="admin:flag:verified_employer")],
        [InlineKeyboardButton(text="🧮 Аудит", callback_data="admin:audit")],
        [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="👮 Обычный администратор", callback_data="admin:flag:is_admin")],
    ]
    if main:
        rows += [
            [InlineKeyboardButton(text="🗳 Управление выборами", callback_data="admin:election")],
            [InlineKeyboardButton(text="🧹 Обнулить все балансы", callback_data="admin:balances:reset"),
             InlineKeyboardButton(text="↩️ Отменить обнуление", callback_data="admin:balances:undo")],
            [InlineKeyboardButton(text="⭐ Главный администратор", callback_data="admin:flag:is_main_admin")],
            [InlineKeyboardButton(text="🛑 Глобальный стоп-кран", callback_data="admin:stop")],
            [InlineKeyboardButton(text="📥 Скачать итоги CSV", callback_data="admin:export")],
        ]
    if owner:
        rows += [
            [InlineKeyboardButton(text="🗑 Удалить администратора", callback_data="admin:remove_admin")],
            [InlineKeyboardButton(text="📄 Журнал всех переводов", callback_data="admin:transfer_log")],
        ]
    rows.append([InlineKeyboardButton(text="← В меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin")
async def admin(call: CallbackQuery, state: FSMContext):
    user = await guard(call, admin=True)
    if not user:
        return
    await state.clear()
    await call.answer()
    is_owner = call.from_user.id == OWNER_TELEGRAM_ID or (
        call.from_user.username or ""
    ).lower() == OWNER_USERNAME
    await call.message.edit_text(
        "<b>Панель государства</b>\nДействия журналируются.",
        reply_markup=admin_markup(bool(user["is_main_admin"] or is_owner), is_owner),
    )


@router.callback_query(F.data == "admin:remove_admin")
async def remove_admin_begin(call: CallbackQuery, state: FSMContext):
    is_owner = call.from_user.id == OWNER_TELEGRAM_ID or (
        call.from_user.username or ""
    ).lower() == OWNER_USERNAME
    if not await guard(call) or not is_owner:
        return await call.answer("Кнопка доступна только @tinekry_u.", show_alert=True)
    await state.set_state(Flow.admin_user)
    await state.update_data(action="remove_admin")
    await call.answer()
    await call.message.edit_text(
        "Нажмите кнопку поиска и выберите администратора:",
        reply_markup=inline_user_search(),
    )


@router.callback_query(F.data.startswith("admin:money:"))
async def admin_money_begin(call: CallbackQuery, state: FSMContext):
    if not await guard(call, admin=True):
        return
    await state.update_data(action=call.data.split(":")[2])
    await state.set_state(Flow.admin_user)
    await call.answer()
    await call.message.edit_text(
        "Нажмите кнопку поиска и выберите гражданина:",
        reply_markup=inline_user_search(),
    )


@router.callback_query(F.data.startswith("admin:flag:"))
async def admin_flag_begin(call: CallbackQuery, state: FSMContext):
    user = await guard(call, admin=True)
    if not user:
        return
    field = call.data.split(":")[2]
    is_owner = call.from_user.id == OWNER_TELEGRAM_ID or (
        call.from_user.username or ""
    ).lower() == OWNER_USERNAME
    if field == "is_main_admin" and not (user["is_main_admin"] or is_owner):
        return await call.answer("Только главный администратор.", show_alert=True)
    await state.update_data(action="flag", field=field)
    await state.set_state(Flow.admin_user)
    await call.answer()
    await call.message.edit_text(
        "Нажмите кнопку поиска и выберите гражданина:",
        reply_markup=inline_user_search(),
    )


@router.callback_query(F.data == "admin:broadcast")
async def broadcast_begin(call: CallbackQuery, state: FSMContext):
    if not await guard(call, admin=True):
        return
    await state.set_state(Flow.broadcast)
    await call.answer()
    await call.message.edit_text(
        "Пришлите текст рассылки. На следующем шаге бот попросит подтверждение.",
        reply_markup=kb(("Отмена", "admin")),
    )


@router.message(Flow.broadcast)
async def broadcast_preview(message: Message, state: FSMContext):
    if not await guard(message, admin=True):
        return
    if not message.text or len(message.text) > 3500:
        return await message.answer("Отправьте текст длиной до 3500 символов.")
    await state.update_data(broadcast_text=message.text)
    await message.answer(
        f"<b>Предпросмотр рассылки</b>\n\n{escape(message.text)}",
        reply_markup=kb(
            ("✅ Отправить всем", "admin:broadcast:confirm"),
            ("❌ Отмена", "admin"),
        ),
    )


@router.callback_query(F.data == "admin:broadcast:confirm")
async def broadcast_confirm(call: CallbackQuery, state: FSMContext):
    actor = await guard(call, admin=True)
    if not actor:
        return
    data = await state.get_data()
    text = data.get("broadcast_text")
    if not text:
        return await call.answer("Рассылка устарела.", show_alert=True)
    await state.clear()
    await call.answer("Рассылка началась.")
    sent = failed = 0
    for uid in await db.all_user_ids():
        try:
            await bot.send_message(uid, f"📣 <b>Объявление</b>\n\n{escape(text)}")
            sent += 1
        except Exception:
            failed += 1
    await call.message.answer(
        f"✅ Рассылка завершена. Доставлено: {sent}, ошибок: {failed}.",
        reply_markup=admin_markup(
            bool(actor["is_main_admin"]),
            call.from_user.id == OWNER_TELEGRAM_ID
            or (call.from_user.username or "").lower() == OWNER_USERNAME,
        ),
    )
    await staff_log(
        f"@{escape(call.from_user.username or str(call.from_user.id))}: "
        f"рассылка, доставлено {sent}, ошибок {failed}"
    )


@router.message(Flow.admin_user)
async def admin_choose_user(message: Message, state: FSMContext):
    actor = await guard(message, admin=True)
    if not actor:
        return
    target = await db.user_by_username(message.text or "")
    if not target:
        return await message.answer("Пользователь не найден. Повторите @username.")
    data = await state.get_data()
    if data["action"] == "remove_admin":
        is_owner = message.from_user.id == OWNER_TELEGRAM_ID or (
            message.from_user.username or ""
        ).lower() == OWNER_USERNAME
        if not is_owner:
            await state.clear()
            return await message.answer("Действие доступно только @tinekry_u.")
        try:
            removed_from_env = remove_admin_from_env(target["username"] or "")
        except RuntimeError as exc:
            return await message.answer(f"Не удалось обновить конфигурацию: {escape(str(exc))}")
        removed = await db.remove_admin(target["telegram_id"])
        if not removed:
            if not removed_from_env:
                return await message.answer("У пользователя уже нет прав администратора.")
        await state.clear()
        await message.answer(
            f"✅ Права администратора у @{escape(target['username'] or '')} удалены сразу"
            + (" и username удалён из .env." if removed_from_env else "."),
            reply_markup=admin_markup(True, True),
        )
        await staff_log(
            f"@tinekry_u удалил права администратора у @{escape(target['username'] or '')}"
            + (" и запись из .env" if removed_from_env else "")
        )
        try:
            await bot.send_message(
                target["telegram_id"],
                "🏛 @tinekry_u удалил ваши права администратора.",
            )
        except Exception:
            pass
        return
    if data["action"] == "flag":
        if target["is_main_admin"] and data["field"] in {"frozen", "is_admin"}:
            return await message.answer("Главного администратора нельзя заморозить или лишить прав.")
        value = await db.toggle_flag(target["telegram_id"], data["field"])
        labels = {"frozen": "заморозка счёта", "is_admin": "права администратора",
                  "is_main_admin": "права главного администратора",
                  "verified_employer": "проверенный работодатель"}
        await state.clear()
        await message.answer(f"✅ {labels[data['field']]}: {'ВКЛ' if value else 'ВЫКЛ'}",
                             reply_markup=admin_markup(bool(actor["is_main_admin"])))
        await staff_log(f"@{escape(message.from_user.username or str(message.from_user.id))}: "
                        f"{labels[data['field']]} для @{escape(target['username'] or '')} → {value}")
        try:
            await bot.send_message(target["telegram_id"], f"🏛 Государство изменило параметр «{labels[data['field']]}»: {'ВКЛ' if value else 'ВЫКЛ'}.")
        except Exception:
            pass
        return
    await state.update_data(target=target["telegram_id"], target_name=target["username"])
    await state.set_state(Flow.admin_amount)
    await message.answer("Введите сумму:")


@router.message(Flow.admin_amount)
async def admin_amount(message: Message, state: FSMContext):
    actor = await guard(message, admin=True)
    if not actor:
        return
    data = await state.get_data()
    try:
        amount = int(message.text or "")
        credit = data["action"] == "credit"
        await db.admin_money(data["target"], amount, credit, "Эмиссия" if credit else "Налог/штраф")
    except (ValueError, BankError) as exc:
        return await message.answer(str(exc) if isinstance(exc, BankError) else "Введите целое число.")
    await state.clear()
    await sync_transfer_log()
    verb = "начислено" if credit else "списано"
    await message.answer(f"✅ @{escape(data['target_name'] or '')}: {verb} {amount}.",
                         reply_markup=admin_markup(bool(actor["is_main_admin"])))
    await staff_log(f"@{escape(message.from_user.username or str(message.from_user.id))}: "
                    f"{verb} <b>{amount}</b> для @{escape(data['target_name'] or '')}")
    try:
        await bot.send_message(data["target"], f"🏛 Государство: {verb} <b>{amount} nlogn-коинов</b>.")
    except Exception:
        pass


@router.callback_query(F.data == "admin:audit")
async def audit(call: CallbackQuery):
    user = await guard(call, admin=True)
    if not user:
        return
    total, budget, count = await db.audit()
    await call.answer()
    await call.message.edit_text(
        f"<b>Аудит системы</b>\n\nГраждан: {count}\nДенежная масса: <b>{total}</b>\n"
        f"Госбюджет: <b>{budget}</b>",
        reply_markup=admin_markup(bool(user["is_main_admin"])),
    )


@router.callback_query(F.data == "admin:stop")
async def stop(call: CallbackQuery):
    if not await guard(call, main=True):
        return
    moment = time.monotonic()
    if moment - last_stop_click.get(call.from_user.id, 0) < 2:
        return await call.answer("Запрос уже обработан.", show_alert=True)
    last_stop_click[call.from_user.id] = moment
    value = await db.toggle_stop()
    await call.answer("Переводы остановлены!" if value else "Переводы возобновлены!", show_alert=True)
    await staff_log(f"@{escape(call.from_user.username or str(call.from_user.id))}: "
                    f"глобальный стоп-кран → {'СТОП' if value else 'РАБОТА'}")


@router.callback_query(F.data == "admin:balances:reset")
async def balances_reset_warning(call: CallbackQuery):
    if not await guard(call, main=True):
        return
    await call.answer()
    await call.message.edit_text(
        "<b>Обнуление всех балансов</b>\n\n"
        "Будет сохранён снимок баланса и холда каждого пользователя, после чего "
        "оба значения станут равны нулю.",
        reply_markup=kb(
            ("Подтвердить обнуление", "admin:balances:reset:confirm"),
            ("Отмена", "admin"),
        ),
    )


@router.callback_query(F.data == "admin:balances:reset:confirm")
async def balances_reset_confirm(call: CallbackQuery):
    if not await guard(call, main=True):
        return
    snapshot_id, count = await db.reset_all_balances(call.from_user.id)
    await call.answer("Балансы обнулены.", show_alert=True)
    await call.message.edit_text(
        f"✅ Снимок <b>#{snapshot_id}</b> сохранён. Обнулено пользователей: {count}.",
        reply_markup=admin_markup(True),
    )
    await staff_log(
        f"@{escape(call.from_user.username or str(call.from_user.id))}: "
        f"сохранил снимок балансов #{snapshot_id} и обнулил {count} пользователей"
    )


@router.callback_query(F.data == "admin:balances:undo")
async def balances_undo_warning(call: CallbackQuery):
    if not await guard(call, main=True):
        return
    await call.answer()
    await call.message.edit_text(
        "<b>Отмена последнего обнуления</b>\n\n"
        "Текущие балансы пользователей будут заменены значениями из последнего "
        "не восстановленного снимка.",
        reply_markup=kb(
            ("Восстановить балансы", "admin:balances:undo:confirm"),
            ("Отмена", "admin"),
        ),
    )


@router.callback_query(F.data == "admin:balances:undo:confirm")
async def balances_undo_confirm(call: CallbackQuery):
    if not await guard(call, main=True):
        return
    try:
        snapshot_id, count = await db.restore_last_balance_reset(call.from_user.id)
    except BankError as exc:
        return await call.answer(str(exc), show_alert=True)
    await call.answer("Балансы восстановлены.", show_alert=True)
    await call.message.edit_text(
        f"↩️ Снимок <b>#{snapshot_id}</b> восстановлен для {count} пользователей.",
        reply_markup=admin_markup(True),
    )
    await staff_log(
        f"@{escape(call.from_user.username or str(call.from_user.id))}: "
        f"восстановил снимок балансов #{snapshot_id} для {count} пользователей"
    )


@router.callback_query(F.data == "admin:export")
async def export(call: CallbackQuery):
    if not await guard(call, main=True):
        return
    rows = await db.export_rows()
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["Name", "User ID", "Username", "Balance", "Held", "Frozen", "Admin", "Verified employer"])
    for row in rows:
        writer.writerow(list(row))
    payload = stream.getvalue().encode("utf-8-sig")
    await call.answer()
    await call.message.answer_document(BufferedInputFile(payload, filename="nlogn_bank_results.csv"),
                                       caption="Итоги игры на момент выгрузки.")
    await staff_log(f"@{escape(call.from_user.username or str(call.from_user.id))}: экспортировал итоги игры")


@router.callback_query(F.data == "admin:transfer_log")
async def transfer_log(call: CallbackQuery):
    is_owner = call.from_user.id == OWNER_TELEGRAM_ID or (
        call.from_user.username or ""
    ).lower() == OWNER_USERNAME
    if not await guard(call) or not is_owner:
        return await call.answer("Доступно только @tinekry_u.", show_alert=True)
    payload = await sync_transfer_log()
    await call.answer()
    await call.message.answer_document(
        BufferedInputFile(payload, filename="transactions.csv"),
        caption="Полный журнал денежных операций.",
    )


@router.callback_query()
async def unknown_callback(call: CallbackQuery):
    await call.answer("Кнопка устарела. Откройте /menu.", show_alert=True)


@router.message()
async def unknown_message(message: Message):
    if await guard(message):
        await message.answer("Используйте кнопки меню или команду /menu.")


async def main() -> None:
    global cfg, db, bot
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    admin_log_path = Path("data/admin_actions.log")
    admin_log_path.parent.mkdir(parents=True, exist_ok=True)
    if not admin_logger.handlers:
        admin_handler = logging.FileHandler(admin_log_path, encoding="utf-8")
        admin_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
        admin_logger.addHandler(admin_handler)
    admin_logger.setLevel(logging.INFO)
    admin_logger.propagate = False
    cfg = load_config()
    db = Database(cfg.database_path)
    await db.init()
    await db.import_whitelist(cfg.whitelist_csv, cfg.main_admins)
    await sync_transfer_log()
    bot = Bot(cfg.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
