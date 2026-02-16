from aiogram import Bot
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
import asyncio
import logging

from models import AdCampaign, AdStatus, Channel
from utils.balance import BalanceService
from config import config

logger = logging.getLogger(__name__)


class DeletionTracker:
    """Отслеживание удаления постов"""
    
    def __init__(self, bot: Bot, session_factory):
        self.bot = bot
        self.session_factory = session_factory
        self.balance_service = BalanceService(session_factory)
    
    async def on_message_deleted(self, channel_id: int, message_id: int):
        """Пост удален - применяем штраф"""
        async with self.session_factory() as session:
            result = await session.execute(
                select(AdCampaign)
                .where(
                    AdCampaign.channel_id == channel_id,
                    AdCampaign.channel_post_id == message_id,
                    AdCampaign.status == AdStatus.ACTIVE.value
                )
            )
            campaign = result.scalar_one_or_none()
            
            if not campaign:
                return
            
            penalty = await self.balance_service.apply_penalty(campaign.id)
            
            if penalty:
                channel = await session.get(Channel, channel_id)
                
                await self.bot.send_message(
                    channel.owner_id,
                    f"⚠️ **НАРУШЕНИЕ!**\n\nВы удалили пост до срока.\n💰 Заработано: ${penalty['earned']:.2f}\n💸 Штраф 50%: -${penalty['penalty']:.2f}\n💵 Баланс: ${penalty['owner_balance']:.2f}",
                    parse_mode="Markdown"
                )
                
                await self.bot.send_message(
                    campaign.advertiser_id,
                    f"✅ **Возврат средств!**\n\nВладелец удалил пост досрочно.\n💰 Вам возвращено: ${penalty['penalty']:.2f}",
                    parse_mode="Markdown"
                )
    
    async def check_expirations(self):
        """Проверка истечения срока размещения и автоматическое удаление"""
        logger.info("🕒 Проверка истечения срока постов...")
        async with self.session_factory() as session:
            now = datetime.utcnow()
            result = await session.execute(
                select(AdCampaign).where(
                    AdCampaign.status == AdStatus.ACTIVE.value,
                    AdCampaign.end_date <= now
                )
            )
            campaigns = result.scalars().all()
            
            for c in campaigns:
                try:
                    # 1. Удаляем пост из канала
                    await self.bot.delete_message(chat_id=c.channel_id, message_id=c.channel_post_id)
                    logger.info(f"🗑 Пост #{c.channel_post_id} удален из канала {c.channel_id} (срок истек)")
                except Exception as e:
                    logger.error(f"⚠️ Ошибка удаления поста #{c.channel_post_id}: {e}")
                
                # 2. Обновляем статус
                c.status = AdStatus.COMPLETED.value
                await session.commit()
                
                # 3. Уведомляем стороны
                channel = await session.get(Channel, c.channel_id)
                
                # Владельцу
                await self.bot.send_message(
                    channel.owner_id,
                    f"🏁 **Рекламная кампания завершена!**\n\n📢 Канал: {channel.title}\n🗑 Пост успешно удален из канала.\n💰 Все средства зачислены на ваш баланс.",
                    parse_mode="Markdown"
                )
                
                # Рекламодателю + предложение отзыва
                from keyboards import rating_keyboard
                await self.bot.send_message(
                    c.advertiser_id,
                    f"🏁 **Ваша рекламная кампания завершена!**\n\n📢 Канал: {channel.title}\n🗑 Пост удален согласно сроку размещения.\n\nПожалуйста, оцените работу канала:",
                    parse_mode="Markdown",
                    reply_markup=rating_keyboard(c.id)
                )

    async def start_polling(self):
        """Проверка каждую минуту"""
        logger.info("👀 Запуск отслеживания удалений...")
        
        while True:
            try:
                await self.check_expirations()
                
                async with self.session_factory() as session:
                    result = await session.execute(
                        select(AdCampaign).where(AdCampaign.status == AdStatus.ACTIVE.value)
                    )
                    campaigns = result.scalars().all()
                    
                    for c in campaigns:
                        try:
                            if c.channel_post_id:
                                try:
                                    # Use bot.get_chat_member or bot.get_message to check existence
                                    # get_chat is for chat info, not for message. 
                                    # Actually, there is no direct "check if message exists" without fetching it.
                                    # But we can try to get it.
                                    await self.bot.forward_message(chat_id=config.ADMIN_IDS[0], from_chat_id=c.channel_id, message_id=c.channel_post_id, disable_notification=True)
                                except Exception as e:
                                    err_msg = str(e).lower()
                                    if "message not found" in err_msg or "message to forward not found" in err_msg:
                                        await self.on_message_deleted(c.channel_id, c.channel_post_id)
                                    elif "chat not found" in err_msg or "bot was kicked" in err_msg or "not a member" in err_msg:
                                        logger.warning(f"⚠️ Канал {c.channel_id} недоступен для проверки: {e}")
                                    else:
                                        logger.error(f"Error checking message {c.channel_post_id} in {c.channel_id}: {e}")
                        except Exception:
                            pass
                        await asyncio.sleep(0.5)
                
                await asyncio.sleep(60)
                
            except Exception as e:
                logger.error(f"Ошибка: {e}")
                await asyncio.sleep(60)
