# app/bot/handlers/mac_cards.py
import random
from aiogram import Router, F
from aiogram.types import CallbackQuery
import redis.asyncio as redis
from structlog import get_logger

from app.core.config import settings
from app.bot.keyboards.inline import get_main_menu_keyboard

logger = get_logger()
router = Router(name="mac_cards_router")

# List of rich coaching metaphorical cards for the MVP
MAC_CARDS = [
    {
        "id": 1,
        "title": "🌅 Світанок",
        "description": "Картинка зображує перші промені сонця, що пробиваються крізь густий ранковий туман над горами.",
        "coaching_question": "💭 Які нові можливості чи починання відкриваються перед вами сьогодні, які ви раніше не помічали?"
    },
    {
        "id": 2,
        "title": "🗝️ Старий Ключ",
        "description": "Зображення старовинного латунного ключа, що лежить на відкритій старій книзі.",
        "coaching_question": "💭 Яку відповідь або рішення ви вже маєте всередині себе, але боїтеся цим скористатися?"
    },
    {
        "id": 3,
        "title": "🌳 Глибоке Коріння",
        "description": "Могутній столітній дуб із розгалуженим корінням, що глибоко йде під землю.",
        "coaching_question": "💭 Що або хто є вашою найбільшою опорою прямо зараз? На який внутрішній ресурс ви можете спертися?"
    },
    {
        "id": 4,
        "title": "🎈 Повітряна Куля",
        "description": "Яскрава повітряна куля, що піднімається високо в чисте блакитне небо над хмарами.",
        "coaching_question": "💭 Від якого зайвого вантажу (думок, обов'язків, образ) вам варто звільнитися, щоб рухатися вгору?"
    }
]

async def get_redis_client() -> redis.Redis:
    """Helper connection dependency injector for Redis."""
    return redis.from_url(settings.REDIS_URL)

@router.callback_query(F.data == "menu:mac_card")
async def process_mac_card_request(call: CallbackQuery) -> None:
    """Delivers metaphorical associative coaching cards, checking Redis TTL limits."""
    user_id = call.from_user.id
    limit_key = f"mac:limit:{user_id}"
    
    r_client = await get_redis_client()
    try:
        # 1. Verify Redis 24h throttling limit
        has_played = await r_client.get(limit_key)
        
        if has_played and settings.ENVIRONMENT == "production":
            await call.answer(
                text="🔮 Ви вже отримали свою Карту Дня! Повертайтеся завтра за новою порадою підсвідомості.",
                show_alert=True
            )
            return
            
        # 2. Select a random card from the collection
        card = random.choice(MAC_CARDS)
        
        # 3. Store lock for 24 hours (86400 seconds)
        await r_client.set(limit_key, "1", ex=86400)
        
        card_message = (
            f"🔮 *Ваша Карта Дня:* **{card['title']}**\n\n"
            f"🖼️ *Опис образу:* _{card['description']}_\n\n"
            f"{card['coaching_question']}\n\n"
            f"💡 _Спробуйте не шукати логічної відповіді, а прислухатися до перших асоціацій та емоцій від карти._"
        )
        
        if call.message.photo or call.message.document:
            await call.message.answer(
                text=card_message,
                parse_mode="Markdown",
                reply_markup=get_main_menu_keyboard(user_id=call.from_user.id)
            )
            try:
                await call.message.delete()
            except Exception:
                pass
        else:
            try:
                await call.message.edit_text(
                    text=card_message,
                    parse_mode="Markdown",
                    reply_markup=get_main_menu_keyboard(user_id=call.from_user.id)
                )
            except Exception:
                pass
        logger.info("mac_card_delivered", user=user_id, card_id=card["id"])
        await call.answer()
    finally:
        await r_client.close()
