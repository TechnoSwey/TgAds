from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from aiogram.filters import Command
from models import User, Channel
from keyboards import main_menu, channels_list, channel_actions
from utils.analytics import calculate_recommended_price
from utils.channel_stats import ChannelStatsCollector
from utils.balance import BalanceService

router = Router()


class AddChannelStates(StatesGroup):
    waiting_for_channel_id = State()
    waiting_for_price_post = State()
    waiting_for_price_pin = State()


class SetPriceStates(StatesGroup):
    waiting_for_price_post = State()
    waiting_for_price_pin = State()


async def check_bot_admin(bot: Bot, channel_id: int) -> bool:
    try:
        bot_member = await bot.get_chat_member(channel_id, bot.id)
        return bot_member.status in ['administrator', 'creator']
    except:
        return False


@router.message(Command("start"))
async def cmd_start(message: Message, session: AsyncSession):
    user = await session.get(User, message.from_user.id)
    
    if not user:
        user = User(
            id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name
        )
        session.add(user)
        await session.commit()
    
    await message.answer(
        f"👋 Привет, {user.first_name}!\n\n"
        "💰 **Поденная оплата** - деньги каждый день\n"
        "🛡 **Гарантия** - штраф 50% за удаление\n"
        "💎 **Мгновенный вывод** - чеки Crypto Pay\n\n"
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=main_menu(user.role)
    )


@router.callback_query(F.data == "my_balance")
async def show_balance(callback: CallbackQuery, session: AsyncSession):
    await show_balance_logic(callback.message, session, callback.from_user.id)
    await callback.answer()

@router.message(Command("balance"))
async def cmd_balance(message: Message, session: AsyncSession):
    await show_balance_logic(message, session, message.from_user.id)

async def show_balance_logic(message: Message, session: AsyncSession, user_id: int):
    """Логика показа баланса"""
    user = await session.get(User, user_id)
    from database import AsyncSessionLocal
    balance_service = BalanceService(AsyncSessionLocal)
    stats = await balance_service.get_owner_stats(user_id)
    
    from models import WithdrawRequest, WithdrawStatus
    result = await session.execute(
        select(WithdrawRequest)
        .where(
            WithdrawRequest.user_id == user_id,
            WithdrawRequest.status == WithdrawStatus.PENDING.value
        )
    )
    pending = result.scalars().all()
    pending_amount = sum(w.amount for w in pending)
    
    available = user.balance - user.frozen_balance - pending_amount
    
    text = (
        f"💰 **Ваш кошелек**\n\n"
        f"💵 **Баланс:** `${user.balance:.2f}`\n"
        f"🔒 **Заморожено:** `${user.frozen_balance:.2f}`\n"
        f"⏳ **В обработке:** `${pending_amount:.2f}`\n"
        f"✅ **Доступно:** `${available:.2f}`\n\n"
        f"📊 **Статистика:**\n"
        f"📥 Всего заработано: `${stats['total_earned']:.2f}`\n"
        f"📤 Всего выведено: `${user.total_withdrawn or 0:.2f}`\n"
        f"⚠️ Штрафы: `${stats['total_penalties']:.2f}`\n"
        f"📋 Нарушений: {stats['total_violations']}"
    )
    
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    
    if available >= 1:
        builder.button(text="💸 ВЫВЕСТИ", callback_data="withdraw_start")
    
    builder.button(text="📋 История", callback_data="withdraw_history")
    builder.button(text="🔙 Назад", callback_data="main_menu")
    builder.adjust(1)
    
    if message.from_user.id == message.bot.id: # Если это редактирование старого сообщения
        await message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    else:
        await message.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())


@router.callback_query(F.data == "my_channels")
async def show_my_channels(callback: CallbackQuery, session: AsyncSession):
    await show_my_channels_logic(callback.message, session, callback.from_user.id)
    await callback.answer()

@router.message(Command("my_channels"))
async def cmd_my_channels(message: Message, session: AsyncSession):
    await show_my_channels_logic(message, session, message.from_user.id)

async def show_my_channels_logic(message: Message, session: AsyncSession, user_id: int):
    result = await session.execute(
        select(Channel).where(Channel.owner_id == user_id)
    )
    channels = result.scalars().all()
    
    text = f"📢 **Ваши каналы:**\nВсего: {len(channels)}" if channels else "📢 У вас пока нет каналов.\n\n➕ Нажмите 'Добавить канал'"
    reply_markup = channels_list(channels)
    
    if message.from_user.id == message.bot.id:
        await message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await message.answer(text, parse_mode="Markdown", reply_markup=reply_markup)


@router.callback_query(F.data == "add_channel")
async def add_channel_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📢 **Добавление канала**\n\n"
        "1️⃣ Добавьте бота в админы канала\n"
        "2️⃣ Выдайте права: отправка, редактирование, закреп, удаление\n"
        "3️⃣ Отправьте ID канала или @username\n\n"
        "Пример: -1001234567890 или @channel",
        parse_mode="Markdown"
    )
    await state.set_state(AddChannelStates.waiting_for_channel_id)
    await callback.answer()


@router.message(AddChannelStates.waiting_for_channel_id)
async def process_channel_id(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    channel_input = message.text.strip()
    channel_id = None
    channel_username = None
    
    if channel_input.startswith('-100'):
        try:
            channel_id = int(channel_input)
        except:
            pass
    elif channel_input.startswith('@'):
        channel_username = channel_input[1:]
    else:
        channel_username = channel_input
    
    try:
        if channel_username:
            chat = await bot.get_chat(f"@{channel_username}")
            channel_id = chat.id
        else:
            chat = await bot.get_chat(channel_id)
        
        existing = await session.get(Channel, channel_id)
        if existing:
            await message.answer("❌ Канал уже добавлен")
            await state.clear()
            return
        
        is_admin = await check_bot_admin(bot, channel_id)
        if not is_admin:
            await message.answer("❌ Бот не администратор! Добавьте в админы.")
            return
        
        collector = ChannelStatsCollector(bot)
        stats = await collector.analyze_channel(channel_id)
        
        await state.update_data(
            channel_id=channel_id,
            channel_title=chat.title,
            channel_username=chat.username,
            subscribers=stats["subscribers"],
            avg_views=stats["avg_views"],
            recommended_post=stats["recommended_price_post"],
            recommended_pin=stats["recommended_price_pin"],
            quality_label=stats["quality_label"],
            err=stats["err"]
        )
        
        text = (
            f"📊 **Аналитика канала**\n\n"
            f"📢 {chat.title}\n"
            f"👥 Подписчики: {stats['subscribers']:,}\n"
            f"👀 Просмотры: {stats['avg_views']:,}\n"
            f"📈 ERR: {stats['err']}%\n"
            f"🏷 Качество: {stats['quality_label']}\n\n"
            f"🤖 **Рекомендуемые цены (1 день):**\n"
            f"📝 Пост: ${stats['recommended_price_post']:.2f}\n"
            f"📌 Закреп: ${stats['recommended_price_pin']:.2f}\n\n"
            f"💰 Введите **вашу цену** за обычный пост (1 день):"
        )
        
        await message.answer(text, parse_mode="Markdown")
        await state.set_state(AddChannelStates.waiting_for_price_post)
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")


@router.message(AddChannelStates.waiting_for_price_post)
async def process_price_post(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(',', '.'))
        if price <= 0:
            raise ValueError
        
        await state.update_data(price_post=price)
        await message.answer("💰 Введите цену за **закрепленный пост** (1 день):", parse_mode="Markdown")
        await state.set_state(AddChannelStates.waiting_for_price_pin)
    except ValueError:
        await message.answer("❌ Введите число больше 0")


@router.message(AddChannelStates.waiting_for_price_pin)
async def process_price_pin(message: Message, state: FSMContext, session: AsyncSession):
    try:
        price_pin = float(message.text.replace(',', '.'))
        if price_pin <= 0:
            raise ValueError
        
        data = await state.get_data()
        
        channel = Channel(
            id=data['channel_id'],
            owner_id=message.from_user.id,
            title=data['channel_title'],
            username=data.get('channel_username'),
            subscribers=data['subscribers'],
            avg_views_5=data['avg_views'],
            price_post=data['price_post'],
            price_pin=price_pin,
            status="active",
            is_bot_admin=True,
            verified_at=datetime.utcnow(),
            suggested_price_post=data['recommended_post'],
            suggested_price_pin=data['recommended_pin'],
            err=data.get('err', 0),
            quality_label=data.get('quality_label', 'Нет данных')
        )
        
        session.add(channel)
        await session.commit()
        
        text = (
            f"✅ **Канал добавлен!**\n\n"
            f"📢 {channel.title}\n"
            f"👥 {channel.subscribers:,} подписчиков\n\n"
            f"💰 **Ваши цены (1 день):**\n"
            f"📝 Пост: ${channel.price_post:.2f}\n"
            f"📌 Закреп: ${channel.price_pin:.2f}\n\n"
            f"⚠️ **Важно:**\n"
            f"• Удаление поста до срока = штраф 50%\n"
            f"• Деньги приходят каждый день в 12:00 МСК"
        )
        
        await message.answer(text, parse_mode="Markdown", reply_markup=channel_actions(channel.id))
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Введите число больше 0")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        await state.clear()


@router.callback_query(F.data.startswith("channel_"))
async def channel_details(callback: CallbackQuery, session: AsyncSession):
    channel_id = int(callback.data.split("_")[1])
    channel = await session.get(Channel, channel_id)
    
    text = (
        f"📢 **{channel.title}**\n\n"
        f"📊 **Статистика:**\n"
        f"👥 Подписчики: {channel.subscribers:,}\n"
        f"👀 Просмотры: {channel.avg_views_5:,}\n"
        f"📈 ERR: {channel.err:.1f}%\n"
        f"🏷 Качество: {channel.quality_label}\n\n"
        f"💰 **Ваши цены (1 день):**\n"
        f"📝 Пост: ${channel.price_post:.2f}\n"
        f"📌 Закреп: ${channel.price_pin:.2f}\n\n"
        f"⭐ **Рейтинг:** {channel.average_rating:.1f}/5.0 ({channel.total_reviews} отзывов)\n"
        f"✅ Заказов: {channel.completed_orders}\n"
        f"⚠️ Нарушений: {channel.violation_count}"
    )
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=channel_actions(channel.id))
    await callback.answer()


@router.callback_query(F.data.startswith("set_prices_"))
async def set_prices_start(callback: CallbackQuery, state: FSMContext):
    channel_id = int(callback.data.split("_")[2])
    await state.update_data(channel_id=channel_id)
    await callback.message.edit_text("💰 Введите **новую цену** за обычный пост (1 день):", parse_mode="Markdown")
    await state.set_state(SetPriceStates.waiting_for_price_post)
    await callback.answer()


@router.message(SetPriceStates.waiting_for_price_post)
async def process_new_price_post(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(',', '.'))
        if price <= 0:
            raise ValueError
        await state.update_data(price_post=price)
        await message.answer("💰 Введите **новую цену** за закрепленный пост (1 день):", parse_mode="Markdown")
        await state.set_state(SetPriceStates.waiting_for_price_pin)
    except ValueError:
        await message.answer("❌ Введите число больше 0")


@router.message(SetPriceStates.waiting_for_price_pin)
async def process_new_price_pin(message: Message, state: FSMContext, session: AsyncSession):
    try:
        price_pin = float(message.text.replace(',', '.'))
        if price_pin <= 0:
            raise ValueError
        
        data = await state.get_data()
        channel = await session.get(Channel, data['channel_id'])
        
        if channel:
            channel.price_post = data['price_post']
            channel.price_pin = price_pin
            await session.commit()
            await message.answer(
                f"✅ **Цены обновлены!**\n\n📝 Пост: ${channel.price_post:.2f}\n📌 Закреп: ${channel.price_pin:.2f}",
                parse_mode="Markdown",
                reply_markup=channel_actions(channel.id)
            )
        
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите число больше 0")


@router.callback_query(F.data == "main_menu")
async def back_to_main(callback: CallbackQuery, session: AsyncSession):
    user = await session.get(User, callback.from_user.id)
    await callback.message.edit_text(
        f"👋 Привет, {user.first_name}!\n\n"
        "💰 **Поденная оплата** - деньги каждый день\n"
        "🛡 **Гарантия** - штраф 50% за удаление\n"
        "💎 **Мгновенный вывод** - чеки Crypto Pay\n\n"
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=main_menu(user.role)
    )
    await callback.answer()
