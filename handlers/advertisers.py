from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from models import User, Channel, AdCampaign, AdStatus
from keyboards import ad_offers, channel_offer, negotiate_keyboard, payment_keyboard
from utils.analytics import calculate_total_price
from utils.cryptopay import create_payment

router = Router()


class CreateAdStates(StatesGroup):
    waiting_for_days = State()
    waiting_for_text = State()
    waiting_for_media = State()
    waiting_for_button_text = State()
    waiting_for_button_url = State()
    waiting_for_button_text_final = State()
    waiting_for_custom_price = State()
    waiting_for_owner_price = State()


@router.callback_query(F.data == "find_ads")
async def find_ads(callback: CallbackQuery, session: AsyncSession):
    await find_ads_logic(callback.message, session)
    await callback.answer()

@router.message(Command("find_ads"))
async def cmd_find_ads(message: Message, session: AsyncSession):
    await find_ads_logic(message, session)

async def find_ads_logic(message: Message, session: AsyncSession):
    """Логика поиска каналов"""
    result = await session.execute(
        select(Channel)
        .where(Channel.status == "active", Channel.is_suspicious == False)
        .order_by(desc(Channel.average_rating), desc(Channel.quality_score))
    )
    channels = result.scalars().all()
    channels_data = [{'channel': c} for c in channels]
    
    text = "🔍 **Доступные каналы**\n👥 подписчики | 👀 просмотры | ⭐ рейтинг"
    reply_markup = ad_offers(channels_data)
    
    if message.from_user.id == message.bot.id:
        await message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await message.answer(text, parse_mode="Markdown", reply_markup=reply_markup)


@router.callback_query(F.data.startswith("view_channel_"))
async def view_channel(callback: CallbackQuery, session: AsyncSession):
    if not callback.message or not callback.data:
        return
    channel_id = int(callback.data.split("_")[2])
    channel = await session.get(Channel, channel_id)
    if not channel:
        return
    
    text = (
        f"📢 **{channel.title}**\n\n"
        f"👥 Подписчики: {channel.subscribers:,}\n"
        f"👀 Просмотры: {channel.avg_views_5:,}\n"
        f"📈 ERR: {channel.err:.1f}%\n"
        f"⭐ Рейтинг: {channel.average_rating:.1f}/5.0\n"
        f"✅ Заказов: {channel.completed_orders}\n\n"
        f"💰 **Цены за 1 день:**\n"
        f"📝 Пост: ${channel.price_post:.2f}\n"
        f"📌 Закреп: ${channel.price_pin:.2f}\n\n"
        f"💎 **Оплата поденно**\n"
        f"🛡 **Гарантия: возврат 50% при удалении**\n\n"
        f"Выберите тип:"
    )
    
    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=channel_offer(int(channel.id), str(channel.username))
    )
    await callback.answer()


@router.callback_query(F.data.startswith("order_"))
async def order_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not callback.message or not callback.data:
        return
    
    # Handle formats: "order_post_123", "order_pin_123", "order_123", "order_negotiated_123"
    parts = callback.data.split("_")
    
    # If format is "order_negotiated_123"
    if "negotiated" in parts:
        try:
            campaign_id = int(parts[2])
            campaign = await session.get(AdCampaign, campaign_id)
            if not campaign:
                await callback.answer(f"❌ Кампания #{campaign_id} не найдена")
                return
            channel_id = campaign.channel_id
            ad_type = "pin" if campaign.is_pinned else "post"
            price_per_day = float(campaign.agreed_price_per_day or campaign.advertiser_price or campaign.price_per_day)
            
            await state.update_data(
                channel_id=channel_id,
                is_pinned=campaign.is_pinned,
                price_per_day=price_per_day,
                campaign_id=campaign.id
            )
        except (ValueError, IndexError):
            await callback.answer("❌ Ошибка данных кампании")
            return
    # If format is "order_post_123" or "order_pin_123"
    elif len(parts) >= 3:
        ad_type = parts[1]
        try:
            channel_id = int(parts[2])
        except ValueError:
            await callback.answer("❌ Неверный ID канала")
            return
        
        channel = await session.get(Channel, channel_id)
        if not channel:
            await callback.answer(f"❌ Канал #{channel_id} не найден в базе")
            return
            
        price_per_day = channel.price_pin if ad_type == "pin" else channel.price_post
        
        await state.update_data(
            channel_id=channel_id,
            is_pinned=(ad_type == "pin"),
            price_per_day=float(price_per_day)
        )
    # If format is just "order_123" (fallback)
    elif len(parts) == 2:
        ad_type = "post"
        try:
            channel_id = int(parts[1])
        except ValueError:
            await callback.answer("❌ Неверный ID канала")
            return
            
        channel = await session.get(Channel, channel_id)
        if not channel:
            await callback.answer(f"❌ Канал #{channel_id} не найден в базе")
            return
            
        price_per_day = channel.price_post
        
        await state.update_data(
            channel_id=channel_id,
            is_pinned=False,
            price_per_day=float(price_per_day)
        )
    else:
        await callback.answer("❌ Ошибка данных")
        return
    
    # Get data for display
    data = await state.get_data()
    channel = await session.get(Channel, data['channel_id'])
    
    await callback.message.edit_text(
        f"📢 **Канал:** {channel.title if channel else 'Неизвестен'}\n"
        f"💰 **Цена за 1 день:** ${data['price_per_day']:.2f}\n\n"
        f"📅 **Введите количество дней** (1-30):",
        parse_mode="Markdown"
    )
    await state.set_state(CreateAdStates.waiting_for_days)
    await callback.answer()


@router.message(CreateAdStates.waiting_for_days)
async def process_days(message: Message, state: FSMContext):
    if not message.text:
        return
    try:
        days = int(message.text)
        if days < 1 or days > 30:
            await message.answer("❌ От 1 до 30 дней")
            return
        
        data = await state.get_data()
        total_price = calculate_total_price(data['price_per_day'], days)
        
        await state.update_data(duration_days=days, duration_hours=days*24, total_price=total_price)
        await message.answer(
            f"📝 **Создание поста**\n\n📅 Срок: {days} дн.\n💰 Сумма: ${total_price:.2f}\n💳 К оплате: ${total_price*1.03:.2f}\n\nОтправьте **текст** поста:",
            parse_mode="Markdown"
        )
        await state.set_state(CreateAdStates.waiting_for_text)
    except ValueError:
        await message.answer("❌ Введите целое число")


@router.message(CreateAdStates.waiting_for_text)
async def process_text(message: Message, state: FSMContext):
    await state.update_data(message_text=message.text or message.caption)
    await message.answer("📎 Отправьте фото/видео/GIF или 'пропустить'")
    await state.set_state(CreateAdStates.waiting_for_media)


@router.message(CreateAdStates.waiting_for_media)
async def process_media(message: Message, state: FSMContext):
    if message.text and message.text.lower() == 'пропустить':
        await state.update_data(media_file_id=None, media_type=None)
        await message.answer("🔘 Добавить inline кнопку? (да/нет)")
        await state.set_state(CreateAdStates.waiting_for_button_text)
        return
    
    media_type = None
    file_id = None
    
    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
    elif message.animation:
        media_type = "animation"
        file_id = message.animation.file_id
    else:
        await message.answer("❌ Отправьте фото/видео/GIF или 'пропустить'")
        return
    
    await state.update_data(media_file_id=file_id, media_type=media_type)
    await message.answer("🔘 Добавить inline кнопку? (да/нет)")
    await state.set_state(CreateAdStates.waiting_for_button_text)


@router.message(CreateAdStates.waiting_for_button_text)
async def process_button_choice(message: Message, state: FSMContext, session: AsyncSession):
    if message.text and message.text.lower() == 'да':
        await message.answer("Введите **текст** кнопки:", parse_mode="Markdown")
        await state.set_state(CreateAdStates.waiting_for_button_url)
    else:
        await state.update_data(inline_button_text=None, inline_button_url=None)
        await create_campaign(message, state, session)


@router.message(CreateAdStates.waiting_for_button_url)
async def process_button_text(message: Message, state: FSMContext):
    if not message.text:
        return
    await state.update_data(inline_button_text=message.text)
    await message.answer("Введите **ссылку** для кнопки (https://):", parse_mode="Markdown")
    await state.set_state(CreateAdStates.waiting_for_button_text_final)


@router.message(CreateAdStates.waiting_for_button_text_final)
async def process_button_url(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text:
        return
    url = message.text.strip()
    if not url.startswith(('https://', 'http://', 'tg://')):
        await message.answer("❌ Ссылка должна начинаться с https://")
        return
    
    await state.update_data(inline_button_url=url)
    await create_campaign(message, state, session)


async def create_campaign(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    
    # Если мы пришли из торгов, удаляем старую запись (черновик)
    if data.get('campaign_id'):
        old_campaign = await session.get(AdCampaign, data['campaign_id'])
        if old_campaign:
            await session.delete(old_campaign)

    campaign = AdCampaign(
        advertiser_id=int(message.from_user.id),
        channel_id=int(data['channel_id']),
        is_pinned=bool(data['is_pinned']),
        message_text=str(data['message_text']),
        media_file_id=data.get('media_file_id'),
        media_type=data.get('media_type'),
        inline_button_text=data.get('inline_button_text'),
        inline_button_url=data.get('inline_button_url'),
        duration_days=int(data['duration_days']),
        duration_hours=int(data['duration_hours']),
        price_per_day=float(data['price_per_day']),
        total_price=float(data['total_price']),
        status=AdStatus.PENDING.value
    )
    
    session.add(campaign)
    await session.commit()
    await session.refresh(campaign)
    
    payment = await create_payment(session, int(campaign.id), int(message.from_user.id), float(campaign.total_price))
    
    if not payment:
        await message.answer("❌ Ошибка создания платежа")
        return
    
    channel = await session.get(Channel, data['channel_id'])
    if not channel:
        return

    # Предпросмотр поста
    preview_text = f"👀 **Предпросмотр вашего поста:**\n\n{campaign.message_text}"
    
    reply_markup = None
    if campaign.inline_button_text and campaign.inline_button_url:
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        builder = InlineKeyboardBuilder()
        builder.button(text=campaign.inline_button_text, url=campaign.inline_button_url)
        reply_markup = builder.as_markup()

    try:
        if campaign.media_type == "photo":
            await message.answer_photo(campaign.media_file_id, caption=preview_text, parse_mode="Markdown", reply_markup=reply_markup)
        elif campaign.media_type == "video":
            await message.answer_video(campaign.media_file_id, caption=preview_text, parse_mode="Markdown", reply_markup=reply_markup)
        elif campaign.media_type == "animation":
            await message.answer_animation(campaign.media_file_id, caption=preview_text, parse_mode="Markdown", reply_markup=reply_markup)
        else:
            await message.answer(preview_text, parse_mode="Markdown", reply_markup=reply_markup)
    except Exception as e:
        await message.answer(f"⚠️ Ошибка предпросмотра медиа: {e}\n\n{preview_text}", parse_mode="Markdown", reply_markup=reply_markup)

    await message.answer(
        f"✅ **Заказ создан!**\n\n"
        f"📢 Канал: {channel.title}\n"
        f"📅 Срок: {campaign.duration_days} дн.\n"
        f"💰 За день: ${campaign.price_per_day:.2f}\n"
        f"💵 Всего: ${campaign.total_price:.2f}\n"
        f"💳 Комиссия: +${campaign.total_price * 0.03:.2f}\n"
        f"💎 **Итого: ${payment.amount_with_commission:.2f}**\n\n"
        f"📌 При удалении поста - возврат 50%\n\n"
        f"👇 **Оплатите сейчас:**",
        parse_mode="Markdown",
        reply_markup=payment_keyboard(str(payment.pay_url), int(payment.crypto_pay_invoice_id))
    )
    
    await state.clear()


@router.callback_query(F.data.startswith("negotiate_"))
async def negotiate_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not callback.message or not callback.data:
        return
    channel_id = int(callback.data.split("_")[1])
    channel = await session.get(Channel, channel_id)
    if not channel:
        return
    
    await state.update_data(channel_id=channel_id)
    await callback.message.edit_text(
        f"💬 **Торг с {channel.title}**\n\n💰 Цена владельца: ${channel.price_post:.2f}/день\n\nВведите **вашу цену** за 1 день:",
        parse_mode="Markdown"
    )
    await state.set_state(CreateAdStates.waiting_for_custom_price)
    await callback.answer()


@router.message(CreateAdStates.waiting_for_custom_price)
async def process_custom_price(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    if not message.text:
        return
    
    # Сразу сбрасываем состояние, чтобы предотвратить повторную обработку
    await state.set_state(None)
    
    try:
        price_str = message.text.replace(',', '.')
        price = float(price_str)
        if price <= 0:
            raise ValueError
        
        data = await state.get_data()
        channel = await session.get(Channel, data['channel_id'])
        if not channel:
            await message.answer("❌ Канал не найден")
            return
        
        campaign = AdCampaign(
            advertiser_id=int(message.from_user.id),
            channel_id=int(channel.id),
            is_pinned=False,
            message_text="Ожидает согласования",
            duration_days=1,
            duration_hours=24,
            price_per_day=float(channel.price_post),
            total_price=0,
            advertiser_price=price,
            owner_price=float(channel.price_post),
            status=AdStatus.NEGOTIATING.value
        )
        
        session.add(campaign)
        await session.commit()
        
        await bot.send_message(
            int(channel.owner_id),
            f"💬 **Новое предложение!**\n\n📢 Канал: {channel.title}\n👤 Рекламодатель: @{message.from_user.username}\n💰 Ваша цена: ${channel.price_post:.2f}\n💵 Предложение: ${price:.2f}",
            parse_mode="Markdown",
            reply_markup=negotiate_keyboard(int(campaign.id), is_owner=True)
        )
        
        await message.answer(f"✅ **Предложение отправлено!**\n💰 Ваша цена: ${price:.2f}/день")
        await state.clear()
    except ValueError:
        # Если ошибка вводе, возвращаем состояние ожидания цены
        await state.set_state(CreateAdStates.waiting_for_custom_price)
        await message.answer("❌ Введите число больше 0 (например: 0.2)")


@router.callback_query(F.data.startswith("check_payment_"))
async def check_payment_handler(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    if not callback.data:
        return
    invoice_id = int(callback.data.split("_")[2])
    
    from utils.cryptopay import check_invoice_status
    status = await check_invoice_status(invoice_id)
    
    if status == "paid":
        from sqlalchemy import select
        from models import CryptoPayment
        result = await session.execute(
            select(CryptoPayment).where(CryptoPayment.crypto_pay_invoice_id == invoice_id)
        )
        payment = result.scalar_one_or_none()
        
        if payment and payment.status != "paid":
            payment.status = "paid"
            payment.paid_at = datetime.utcnow()
            
            campaign = await session.get(AdCampaign, payment.campaign_id)
            if campaign:
                campaign.status = AdStatus.PAID.value
                
                channel = await session.get(Channel, campaign.channel_id)
                advertiser = await session.get(User, campaign.advertiser_id)
                if channel:
                    from handlers.publishing import send_post_for_review
                    await send_post_for_review(bot, channel.owner_id, campaign, channel, advertiser)
            
            await session.commit()
            await callback.message.edit_text("✅ **Оплата подтверждена!**\n\nВаш заказ отправлен на модерацию владельцу канала. Вы получите уведомление о публикации.", parse_mode="Markdown")
        else:
            await callback.answer("✅ Оплата уже была подтверждена ранее")
    else:
        await callback.answer("⏳ Оплата еще не получена. Попробуйте через минуту.", show_alert=True)


@router.callback_query(F.data.startswith("cancel_order_"))
async def cancel_order_handler(callback: CallbackQuery, session: AsyncSession):
    if not callback.data:
        return
    invoice_id = int(callback.data.split("_")[2])
    
    from sqlalchemy import select
    from models import CryptoPayment, AdCampaign, AdStatus
    
    result = await session.execute(
        select(CryptoPayment).where(CryptoPayment.crypto_pay_invoice_id == invoice_id)
    )
    payment = result.scalar_one_or_none()
    
    if payment:
        payment.status = "cancelled"
        campaign = await session.get(AdCampaign, payment.campaign_id)
        if campaign:
            campaign.status = AdStatus.CANCELLED.value
        
        await session.commit()
        await callback.answer()


@router.callback_query(F.data == "help")
async def show_help(callback: CallbackQuery):
    """Показать помощь"""
    text = (
        "❓ **Помощь по AdTelega**\n\n"
        "📢 **Для владельцев:**\n"
        "1. Добавьте бота в админы канала.\n"
        "2. Добавьте канал через меню.\n"
        "3. Установите цены. Выплаты приходят ежедневно в 12:00 МСК.\n\n"
        "💼 **Для рекламодателей:**\n"
        "1. Найдите подходящий канал.\n"
        "2. Выберите срок и создайте пост.\n"
        "3. Оплатите через Crypto Pay.\n"
        "4. После проверки владельцем пост будет опубликован.\n\n"
        "⚖️ **Правила:**\n"
        "• Удаление поста раньше срока = штраф 50%.\n"
        "• При отклонении поста владельцем - полный возврат."
    )
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="main_menu")
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data == "my_campaigns")
async def show_my_campaigns(callback: CallbackQuery, session: AsyncSession):
    """Показать кампании рекламодателя"""
    from models import AdCampaign, Channel, AdStatus
    from sqlalchemy import select, desc
    
    result = await session.execute(
        select(AdCampaign)
        .where(AdCampaign.advertiser_id == callback.from_user.id)
        .order_by(desc(AdCampaign.created_at))
        .limit(10)
    )
    campaigns = result.scalars().all()
    
    if not campaigns:
        from keyboards import main_menu
        await callback.message.edit_text(
            "📋 У вас пока нет созданных кампаний.",
            reply_markup=main_menu("advertiser")
        )
        await callback.answer()
        return

    text = "📋 **Ваши последние кампании:**\n\n"
    for c in campaigns:
        channel = await session.get(Channel, c.channel_id)
        channel_title = channel.title if channel else "Удален"
        status_emoji = {
            AdStatus.PENDING.value: "⏳",
            AdStatus.PAID.value: "💰",
            AdStatus.ACTIVE.value: "✅",
            AdStatus.COMPLETED.value: "🏁",
            AdStatus.CANCELLED.value: "❌"
        }.get(c.status, "❓")
        
        text += f"{status_emoji} {channel_title} | ${c.total_price:.2f} | {c.status}\n"

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="main_menu")
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("accept_offer_"))
async def accept_offer(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    if not callback.data:
        return
    campaign_id = int(callback.data.split("_")[2])
    campaign = await session.get(AdCampaign, campaign_id)
    if not campaign:
        return
    
    campaign.status = AdStatus.PAID.value
    campaign.agreed_price_per_day = float(campaign.advertiser_price)
    campaign.price_per_day = float(campaign.advertiser_price)
    await session.commit()
    
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Создать пост", callback_data=f"order_negotiated_{campaign.id}")
    
    await bot.send_message(
        int(campaign.advertiser_id),
        f"✅ **Владелец принял ваше предложение!**\n💰 Цена: ${campaign.advertiser_price:.2f}/день\n\nТеперь вы можете создать рекламный пост по этой цене:",
        reply_markup=builder.as_markup()
    )
    
    await callback.message.edit_text("✅ Предложение принято")
    await callback.answer()


@router.callback_query(F.data.startswith("order_negotiated_"))
async def order_negotiated_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    await order_start(callback, state, session)


@router.callback_query(F.data.startswith("reject_offer_"))
async def reject_offer(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    if not callback.data:
        return
    campaign_id = int(callback.data.split("_")[2])
    campaign = await session.get(AdCampaign, campaign_id)
    if not campaign:
        return
    
    campaign.status = AdStatus.CANCELLED.value
    await session.commit()
    
    try:
        await bot.send_message(
            int(campaign.advertiser_id),
            f"❌ Владелец отклонил ваше предложение по цене."
        )
    except: pass
    
    await callback.message.edit_text("❌ Предложение отклонено")
    await callback.answer()


@router.callback_query(F.data.startswith("cancel_offer_"))
async def cancel_offer(callback: CallbackQuery, session: AsyncSession):
    if not callback.data:
        return
    campaign_id = int(callback.data.split("_")[2])
    campaign = await session.get(AdCampaign, campaign_id)
    if not campaign:
        return
    
    campaign.status = AdStatus.CANCELLED.value
    await session.commit()
    
    await callback.message.edit_text("❌ Предложение отменено")
    await callback.answer()


@router.callback_query(F.data.startswith("offer_price_"))
async def owner_counter_offer(callback: CallbackQuery, state: FSMContext):
    if not callback.data:
        return
    campaign_id = int(callback.data.split("_")[2])
    await state.update_data(campaign_id=campaign_id)
    await callback.message.answer("💰 Введите **вашу встречную цену** за 1 день:", parse_mode="Markdown")
    await state.set_state(CreateAdStates.waiting_for_owner_price)
    await callback.answer()


@router.callback_query(F.data.startswith("make_offer_"))
async def advertiser_make_offer(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not callback.data:
        return
    campaign_id = int(callback.data.split("_")[2])
    campaign = await session.get(AdCampaign, campaign_id)
    if not campaign:
        return
    
    await state.update_data(channel_id=campaign.channel_id, campaign_id=campaign_id)
    await callback.message.answer("💰 Введите **вашу новую цену** за 1 день:", parse_mode="Markdown")
    await state.set_state(CreateAdStates.waiting_for_custom_price)
    await callback.answer()


@router.message(CreateAdStates.waiting_for_owner_price)
async def process_owner_counter_price(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    if not message.text:
        return
    try:
        price = float(message.text.replace(',', '.'))
        if price <= 0: raise ValueError
        
        data = await state.get_data()
        campaign = await session.get(AdCampaign, data['campaign_id'])
        if not campaign:
            return
        
        campaign.owner_price = price
        await session.commit()
        
        await bot.send_message(
            int(campaign.advertiser_id),
            f"💬 **Владелец предложил свою цену**\n💰 Его цена: ${price:.2f}/день\n💰 Ваша цена: ${campaign.advertiser_price:.2f}/день",
            reply_markup=negotiate_keyboard(int(campaign.id), is_owner=False)
        )
        
        await message.answer(f"✅ Цена отправлена: ${price:.2f}/день")
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите число больше 0")


@router.callback_query(F.data.startswith("rate_"))
async def process_rating(callback: CallbackQuery, session: AsyncSession):
    """Обработка отзыва от рекламодателя"""
    parts = callback.data.split("_")
    rating = int(parts[1])
    campaign_id = int(parts[2])
    
    from models import AdCampaign, Review, Channel
    campaign = await session.get(AdCampaign, campaign_id)
    if not campaign:
        await callback.answer("Заказ не найден")
        return
        
    # Проверяем, не оставлял ли уже отзыв
    from sqlalchemy import select
    result = await session.execute(
        select(Review).where(Review.campaign_id == campaign_id)
    )
    if result.scalar_one_or_none():
        await callback.answer("Вы уже оставляли отзыв к этому заказу", show_alert=True)
        return
        
    review = Review(
        campaign_id=campaign_id,
        channel_id=campaign.channel_id,
        author_id=callback.from_user.id,
        rating=rating
    )
    session.add(review)
    
    # Обновляем рейтинг канала
    channel = await session.get(Channel, campaign.channel_id)
    if channel:
        old_total = channel.total_reviews or 0
        old_avg = channel.average_rating or 0
        new_total = old_total + 1
        channel.average_rating = (old_avg * old_total + rating) / new_total
        channel.total_reviews = new_total
        channel.completed_orders = (channel.completed_orders or 0) + 1
        
    await session.commit()
    await callback.message.edit_text(f"⭐ **Спасибо за вашу оценку: {rating}/5!**", parse_mode="Markdown")
    
    # Уведомляем владельца об отзыве
    channel = await session.get(Channel, campaign.channel_id)
    if channel:
        await bot.send_message(
            channel.owner_id,
            f"🌟 **Новый отзыв!**\n\n📢 Канал: {channel.title}\n⭐ Оценка: {rating}/5",
            parse_mode="Markdown"
        )
    await callback.answer()
