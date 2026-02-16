from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta
import logging

from models import AdCampaign, AdStatus, Channel, User
from keyboards import moderation_keyboard

router = Router()
logger = logging.getLogger(__name__)

balance_service = None


class ModerationStates(StatesGroup):
    waiting_for_comment = State()


@router.callback_query(F.data.startswith("publish_ad_"))
async def start_moderation(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    """Начало модерации - владелец проверяет пост"""
    campaign_id = int(callback.data.split("_")[2])
    campaign = await session.get(AdCampaign, campaign_id)
    channel = await session.get(Channel, campaign.channel_id)
    
    if callback.from_user.id != channel.owner_id:
        await callback.answer("❌ Вы не владелец канала")
        return
    
    if campaign.status != AdStatus.PAID.value:
        await callback.answer("❌ Кампания не оплачена")
        return
    
    advertiser = await session.get(User, campaign.advertiser_id)
    
    # Отправляем пост на проверку
    await send_post_for_review(bot, callback.from_user.id, campaign, channel, advertiser)
    await callback.message.delete()
    await callback.answer()


async def send_post_for_review(bot: Bot, chat_id: int, campaign: AdCampaign, channel: Channel, advertiser: User):
    """Отправка поста владельцу на проверку"""
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    
    # Сначала отправляем информацию о заказе
    info_text = (
        f"💎 **НОВЫЙ ЗАКАЗ**\n\n"
        f"📢 **Канал:** {channel.title}\n"
        f"👤 **Рекламодатель:** @{advertiser.username or advertiser.first_name}\n"
        f"💰 **Сумма:** ${campaign.total_price:.2f}\n"
        f"📅 **Срок:** {campaign.duration_days} дн.\n"
        f"📌 **Тип:** {'🔝 Закреп' if campaign.is_pinned else '📝 Обычный'}\n\n"
        f"👇 **ПОСТ НИЖЕ:**"
    )
    await bot.send_message(chat_id, info_text, parse_mode="Markdown")

    # Формируем клавиатуру самого поста (если есть кнопка)
    post_reply_markup = None
    if campaign.inline_button_text and campaign.inline_button_url:
        post_builder = InlineKeyboardBuilder()
        post_builder.button(text=campaign.inline_button_text, url=campaign.inline_button_url)
        post_reply_markup = post_builder.as_markup()

    try:
        # Отправляем сам пост
        if campaign.media_type == "photo":
            await bot.send_photo(chat_id, campaign.media_file_id, caption=campaign.message_text, reply_markup=post_reply_markup, parse_mode="HTML")
        elif campaign.media_type == "video":
            await bot.send_video(chat_id, campaign.media_file_id, caption=campaign.message_text, reply_markup=post_reply_markup, parse_mode="HTML")
        elif campaign.media_type == "animation":
            await bot.send_animation(chat_id, campaign.media_file_id, caption=campaign.message_text, reply_markup=post_reply_markup, parse_mode="HTML")
        else:
            await bot.send_message(chat_id, campaign.message_text, reply_markup=post_reply_markup, parse_mode="HTML")
        
        # Формируем клавиатуру модерации
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ ПРИНЯТЬ И ОПУБЛИКОВАТЬ", callback_data=f"approve_post_{campaign.id}")
        builder.button(text="❌ ОТКЛОНИТЬ", callback_data=f"reject_post_{campaign.id}")
        builder.button(text="📝 ЗАМЕЧАНИЕ", callback_data=f"comment_post_{campaign.id}")
        builder.adjust(1)
        
        # В конце отправляем кнопки управления модерацией
        await bot.send_message(chat_id, "✅ **Проверьте содержание выше и примите решение:**", parse_mode="Markdown", reply_markup=builder.as_markup())
        
    except Exception as e:
        logger.error(f"Ошибка отправки на модерацию: {e}")
        # Если произошла ошибка (например, неверный HTML), отправляем как текст
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ ПРИНЯТЬ И ОПУБЛИКОВАТЬ", callback_data=f"approve_post_{campaign.id}")
        builder.button(text="❌ ОТКЛОНИТЬ", callback_data=f"reject_post_{campaign.id}")
        builder.button(text="📝 ЗАМЕЧАНИЕ", callback_data=f"comment_post_{campaign.id}")
        builder.adjust(1)
        await bot.send_message(chat_id, f"❌ Ошибка отображения медиа или разметки.\n\nТекст: {campaign.message_text}", reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("approve_post_"))
async def approve_and_publish(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    """Владелец ОДОБРИЛ - публикуем"""
    campaign_id = int(callback.data.split("_")[2])
    campaign = await session.get(AdCampaign, campaign_id)
    channel = await session.get(Channel, campaign.channel_id)
    
    try:
        # Публикация
        message = await publish_to_channel(bot, campaign)
        
        campaign.channel_post_id = message.message_id
        campaign.status = AdStatus.ACTIVE.value
        campaign.start_date = datetime.utcnow()
        campaign.end_date = datetime.utcnow() + timedelta(days=campaign.duration_days)
        
        # Если это закреп - закрепляем
        if campaign.is_pinned:
            try:
                await bot.pin_chat_message(chat_id=campaign.channel_id, message_id=message.message_id)
                logger.info(f"📌 Пост {message.message_id} закреплен в канале {campaign.channel_id}")
            except Exception as e:
                logger.error(f"⚠️ Ошибка закрепления поста: {e}")

        await session.commit()
        
        # Создаем поденные выплаты
        if balance_service:
            await balance_service.create_daily_payments(campaign)
        
        await callback.message.delete()
        await callback.message.answer(
            f"✅ **Пост опубликован!**\n📢 {channel.title}\n🆔 ID: {message.message_id}\n🗑 Удаление: {campaign.end_date.strftime('%d.%m.%Y %H:%M')}",
            parse_mode="Markdown"
        )
        
        await bot.send_message(
            campaign.advertiser_id,
            f"✅ **Реклама опубликована!**\n📢 {channel.title}\n📅 {campaign.duration_days} дн.\n🗑 Удаление: {campaign.end_date.strftime('%d.%m.%Y %H:%M')}",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка публикации: {str(e)}")
    
    await callback.answer()


@router.callback_query(F.data.startswith("reject_post_"))
async def reject_post(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    """Владелец ОТКЛОНИЛ"""
    campaign_id = int(callback.data.split("_")[2])
    campaign = await session.get(AdCampaign, campaign_id)
    channel = await session.get(Channel, campaign.channel_id)
    
    campaign.status = AdStatus.CANCELLED.value
    await session.commit()
    
    try:
        await callback.message.delete()
    except Exception:
        pass
        
    await callback.message.answer("❌ Пост отклонен")
    await bot.send_message(campaign.advertiser_id, f"❌ Пост отклонен в канале {channel.title}")


@router.callback_query(F.data.startswith("comment_post_"))
async def comment_post(callback: CallbackQuery, state: FSMContext):
    """Замечание к посту"""
    campaign_id = int(callback.data.split("_")[2])
    await state.update_data(campaign_id=campaign_id)
    await callback.message.answer("📝 Напишите замечание к посту:")
    await state.set_state(ModerationStates.waiting_for_comment)
    await callback.answer()


@router.message(ModerationStates.waiting_for_comment)
async def process_comment(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    """Отправка замечания"""
    data = await state.get_data()
    campaign = await session.get(AdCampaign, data['campaign_id'])
    channel = await session.get(Channel, campaign.channel_id)
    
    await bot.send_message(
        campaign.advertiser_id,
        f"📝 **Замечание к посту**\n📢 {channel.title}\n💬 {message.text}"
    )
    
    await message.answer("✅ Замечание отправлено")
    await state.clear()


async def publish_to_channel(bot: Bot, campaign: AdCampaign):
    """Публикация в канал"""
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    
    reply_markup = None
    if campaign.inline_button_text and campaign.inline_button_url:
        builder = InlineKeyboardBuilder()
        builder.button(text=campaign.inline_button_text, url=campaign.inline_button_url)
        reply_markup = builder.as_markup()
    
    # В aiogram 3.x для публикации в канал используется ID канала (campaign.channel_id)
    if campaign.media_type == "photo":
        return await bot.send_photo(chat_id=campaign.channel_id, photo=campaign.media_file_id, caption=campaign.message_text, reply_markup=reply_markup, parse_mode="HTML")
    elif campaign.media_type == "video":
        return await bot.send_video(chat_id=campaign.channel_id, video=campaign.media_file_id, caption=campaign.message_text, reply_markup=reply_markup, parse_mode="HTML")
    elif campaign.media_type == "animation":
        return await bot.send_animation(chat_id=campaign.channel_id, animation=campaign.media_file_id, caption=campaign.message_text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        return await bot.send_message(chat_id=campaign.channel_id, text=campaign.message_text, reply_markup=reply_markup, parse_mode="HTML")
