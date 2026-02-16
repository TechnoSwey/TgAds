from aiocryptopay import AioCryptoPay, Networks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime
import logging
from typing import Optional

from config import config
from models import User, WithdrawRequest, WithdrawStatus

logger = logging.getLogger(__name__)

cp = AioCryptoPay(token=config.CRYPTO_PAY_TOKEN, network=Networks.MAIN_NET)


class CryptoPayWithdraw:
    SUPPORTED_CURRENCIES = {
        "USDT": {"asset": "USDT", "min_amount": 1.0, "decimals": 2},
        "TON": {"asset": "TON", "min_amount": 0.5, "decimals": 2},
        "BTC": {"asset": "BTC", "min_amount": 0.0001, "decimals": 8},
        "ETH": {"asset": "ETH", "min_amount": 0.001, "decimals": 6}
    }
    
    @staticmethod
    async def get_ton_price() -> float:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get('https://tonapi.io/v2/rates?tokens=ton&currencies=usd') as resp:
                    data = await resp.json()
                    return float(data['rates']['TON']['prices']['USD'])
        except:
            return 2.3
    
    @staticmethod
    async def get_btc_price() -> float:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get('https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd') as resp:
                    data = await resp.json()
                    return float(data['bitcoin']['usd'])
        except:
            return 50000.0
    
    @staticmethod
    async def get_eth_price() -> float:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get('https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd') as resp:
                    data = await resp.json()
                    return float(data['ethereum']['usd'])
        except:
            return 3000.0
    
    @classmethod
    async def create_cheque(cls, user_id: int, amount_usd: float, currency: str = "USDT"):
        try:
            if currency not in cls.SUPPORTED_CURRENCIES:
                return None
            
            if currency == "USDT":
                rate = 1.0
            elif currency == "TON":
                rate = await cls.get_ton_price()
            elif currency == "BTC":
                rate = await cls.get_btc_price()
            elif currency == "ETH":
                rate = await cls.get_eth_price()
            else:
                rate = 1.0
            
            amount_crypto = round(amount_usd / rate, cls.SUPPORTED_CURRENCIES[currency]["decimals"])
            min_amount = cls.SUPPORTED_CURRENCIES[currency]["min_amount"]
            
            if amount_crypto < min_amount:
                logger.error(f"Сумма {amount_crypto} {currency} меньше минимальной")
                return None
            
            logger.info(f"Creating check for {amount_crypto} {currency} for user {user_id}")
            # Ошибка 403 METHOD_DISABLED означает, что создание чеков отключено в приложении CryptoBot.
            # На текущий момент мы будем выводить сообщение об этом пользователю.
            # В реальном приложении владельцу нужно включить Checks в настройках @CryptoBot -> My Apps -> [App] -> Checks
            try:
                cheque = await cp.create_check(
                    asset=cls.SUPPORTED_CURRENCIES[currency]["asset"],
                    amount=amount_crypto,
                )
                logger.info(f"✅ Создан чек на {amount_crypto} {currency} для пользователя {user_id}")
                return cheque
            except Exception as e:
                if "METHOD_DISABLED" in str(e):
                    logger.error("❌ Ошибка: Создание чеков отключено в настройках CryptoBot (METHOD_DISABLED).")
                raise e
            
        except Exception as e:
            logger.error(f"Ошибка создания чека: {e}")
            return None
    
    @classmethod
    async def process_withdrawal(cls, session: AsyncSession, withdraw_id: int) -> bool:
        try:
            withdraw = await session.get(WithdrawRequest, withdraw_id)
            if not withdraw or str(withdraw.status) != WithdrawStatus.PENDING.value:
                return False
            
            user = await session.get(User, withdraw.user_id)
            if not user or float(user.balance) < float(withdraw.amount):
                withdraw.status = WithdrawStatus.REJECTED.value
                withdraw.admin_note = "Недостаточно средств"
                await session.commit()
                return False
            
            cheque = await cls.create_cheque(
                user_id=int(user.id),
                amount_usd=float(withdraw.amount),
                currency=str(withdraw.currency)
            )
            
            if not cheque:
                withdraw.status = WithdrawStatus.REJECTED.value
                withdraw.admin_note = "Ошибка создания чека"
                await session.commit()
                return False
            
            withdraw.cheque_id = int(cheque.check_id)
            withdraw.cheque_url = str(cheque.bot_check_url)
            withdraw.cheque_status = "active"
            withdraw.amount_crypto = float(cheque.amount)
            withdraw.currency = str(cheque.asset)
            withdraw.status = WithdrawStatus.COMPLETED.value
            withdraw.processed_at = datetime.utcnow()
            
            user.balance = float(user.balance) - float(withdraw.amount)
            user.total_withdrawn = (float(user.total_withdrawn) if user.total_withdrawn else 0.0) + float(withdraw.amount)
            
            await session.commit()
            logger.info(f"✅ Выплата #{withdraw.id} обработана, баланс -${withdraw.amount}")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка обработки вывода: {e}")
            await session.rollback()
            return False
    
    @classmethod
    async def get_available_currencies(cls, amount_usd: float) -> list:
        available = []
        
        for currency in cls.SUPPORTED_CURRENCIES:
            try:
                if currency == "USDT":
                    rate = 1.0
                elif currency == "TON":
                    rate = await cls.get_ton_price()
                elif currency == "BTC":
                    rate = await cls.get_btc_price()
                elif currency == "ETH":
                    rate = await cls.get_eth_price()
                else:
                    continue
                
                amount_crypto = amount_usd / rate
                min_amount = cls.SUPPORTED_CURRENCIES[currency]["min_amount"]
                
                if amount_crypto >= min_amount:
                    available.append({
                        "currency": currency,
                        "amount": round(amount_crypto, cls.SUPPORTED_CURRENCIES[currency]["decimals"]),
                        "min_amount": min_amount
                    })
                    
            except Exception as e:
                logger.error(f"Ошибка проверки {currency}: {e}")
                continue
        
        return available
