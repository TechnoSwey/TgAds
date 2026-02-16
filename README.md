# TgAds

## Overview
A Telegram bot for advertising marketplace - connects channel owners with advertisers. Built with Python, aiogram 3.4, SQLAlchemy (SQLite), and Crypto Pay for payments.

## Recent Changes
- 2026-02-12: Initial Replit setup - fixed package imports (cryptopay-sdk → aiocryptopay), added missing `__init__.py` files, added missing FSM state

## Project Architecture
- `bot.py` - Main entry point, bot initialization, scheduler
- `config.py` - Configuration (env vars: BOT_TOKEN, CRYPTO_PAY_TOKEN)
- `database.py` - SQLAlchemy async engine setup (SQLite)
- `models.py` - Database models (User, Channel, AdCampaign, etc.)
- `handlers/` - Telegram bot handlers (owners, advertisers, publishing, withdraw, cleanup)
- `utils/` - Utilities (cryptopay integration, balance service, analytics, channel stats)

## Required Secrets
- `BOT_TOKEN` - Telegram Bot API token
- `CRYPTO_PAY_TOKEN` - Crypto Pay API token

## How to Run
The bot runs via the "Telegram Bot" workflow: `python bot.py`
