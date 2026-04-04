import asyncio
import os
import re
import logging
import requests

from dotenv import load_dotenv
load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config  (set via environment variables or .env / config.py)
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TMDB_API_KEY = os.environ["TMDB_API_KEY"]
TMDB_BEARER = os.environ.get("TMDB_BEARER", "")  # JWT Bearer token (preferido)
CHANNEL_ID = os.environ["CHANNEL_ID"]  # e.g. "@mychannel" o "-1001234567890"

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def rating_to_stars(rating: float) -> str:
    """Convert a 0-10 float rating to a star string (max 10 stars, half-star support)."""
    full = int(rating)
    half = (rating - full) >= 0.5
    stars = "⭐" * full + ("½" if half else "")
    return stars or "☆"


def minutes_to_duration(minutes: int) -> str:
    if not minutes:
        return "N/D"
    h, m = divmod(minutes, 60)
    if h:
        return f"{h}h {m}min"
    return f"{m}min"


def tmdb_get(endpoint: str, params: dict | None = None) -> dict:
    params = params or {}
    params.setdefault("language", "es-ES")
    if TMDB_BEARER:
        headers = {"Authorization": f"Bearer {TMDB_BEARER}"}
        resp = requests.get(f"{TMDB_BASE}{endpoint}", params=params, headers=headers, timeout=10)
    else:
        params["api_key"] = TMDB_API_KEY
        resp = requests.get(f"{TMDB_BASE}{endpoint}", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_genre_names(genre_ids: list[int], media_type: str) -> str:
    endpoint = f"/genre/{media_type}/list"
    data = tmdb_get(endpoint)
    genre_map = {g["id"]: g["name"] for g in data.get("genres", [])}
    names = [genre_map[gid] for gid in genre_ids if gid in genre_map]
    return ", ".join(names) if names else "N/D"


def parse_tmdb_url(url: str) -> tuple[str, int] | None:
    """
    Returns (media_type, tmdb_id) from a TMDb URL, or None if not matched.
    Supports:
      https://www.themoviedb.org/movie/27205-inception
      https://www.themoviedb.org/tv/1396-breaking-bad
    """
    pattern = r"themoviedb\.org/(movie|tv)/(\d+)"
    match = re.search(pattern, url)
    if match:
        return match.group(1), int(match.group(2))
    return None


def fetch_movie_details(tmdb_id: int) -> dict:
    return tmdb_get(f"/movie/{tmdb_id}")


def fetch_tv_details(tmdb_id: int) -> dict:
    return tmdb_get(f"/tv/{tmdb_id}")


def search_movie(title: str) -> dict | None:
    data = tmdb_get("/search/movie", {"query": title})
    results = data.get("results", [])
    if not results:
        return None
    return tmdb_get(f"/movie/{results[0]['id']}")


def search_tv(title: str) -> dict | None:
    data = tmdb_get("/search/tv", {"query": title})
    results = data.get("results", [])
    if not results:
        return None
    return tmdb_get(f"/tv/{results[0]['id']}")


def build_caption(details: dict, media_type: str) -> str:
    title = details.get("title") or details.get("name") or "Sin título"
    release_raw = details.get("release_date") or details.get("first_air_date") or ""
    year = release_raw[:4] if release_raw else "N/D"
    rating = details.get("vote_average", 0.0)
    stars = rating_to_stars(rating)
    overview = details.get("overview") or "Sin descripción disponible."
    genre_ids = [g["id"] for g in details.get("genres", [])]
    genres = ", ".join(g["name"] for g in details.get("genres", [])) if details.get("genres") else "N/D"

    # Duration
    if media_type == "movie":
        runtime = details.get("runtime", 0)
        duration = minutes_to_duration(runtime)
    else:
        ep_runtime = details.get("episode_run_time", [])
        avg = ep_runtime[0] if ep_runtime else 0
        duration = minutes_to_duration(avg) + " por episodio" if avg else "N/D"

    caption = (
        f"🎬 <b>{title}</b> ({year})\n\n"
        f"🚦 <b>Calificación de usuarios:</b> {stars} ({rating:.1f}/10)\n\n"
        f"🗓️ <b>Fecha de estreno:</b> {release_raw or 'N/D'}\n\n"
        f"📖 <b>Resumen:</b>\n{overview}\n\n"
        f"🎭 <b>Género:</b> {genres}\n\n"
        f"⏱️ <b>Duración:</b> {duration}"
    )
    return caption


def build_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("👍 La recomiendo", callback_data="vote_recommend"),
            InlineKeyboardButton("🥰❤️ Buenísima", callback_data="vote_great"),
        ],
        [
            InlineKeyboardButton("🙈 No la he visto", callback_data="vote_not_seen"),
            InlineKeyboardButton("❌ Hay mejores", callback_data="vote_better"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


async def send_movie_to_channel(
    context: ContextTypes.DEFAULT_TYPE,
    details: dict,
    media_type: str,
    reply_chat_id: int | str,
) -> None:
    caption = build_caption(details, media_type)
    keyboard = build_keyboard()
    poster_path = details.get("poster_path")

    if poster_path:
        photo_url = f"{TMDB_IMAGE_BASE}{poster_path}"
        await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=photo_url,
            caption=caption,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    else:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=caption,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    if str(reply_chat_id) != str(CHANNEL_ID):
        await context.bot.send_message(
            chat_id=reply_chat_id,
            text=f"✅ Publicado en el canal.",
        )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /pelicula <título o URL de TMDb>
    """
    if not context.args:
        await update.message.reply_text(
            "⚠️ Uso: /pelicula <título> o /pelicula <URL de TMDb>\n"
            "Ejemplo: /pelicula Inception"
        )
        return

    query = " ".join(context.args).strip()
    await update.message.reply_text("🔍 Buscando información, un momento…")

    try:
        # Check if it's a TMDb URL
        parsed = parse_tmdb_url(query)
        if parsed:
            media_type, tmdb_id = parsed
            if media_type == "movie":
                details = fetch_movie_details(tmdb_id)
            else:
                details = fetch_tv_details(tmdb_id)
        else:
            # Try movie first, then TV
            details = search_movie(query)
            if details:
                media_type = "movie"
            else:
                details = search_tv(query)
                media_type = "tv"

        if not details:
            await update.message.reply_text("❌ No encontré ningún resultado para esa búsqueda.")
            return

        await send_movie_to_channel(context, details, media_type, update.message.chat_id)

    except requests.HTTPError as exc:
        logger.error("TMDb HTTP error: %s", exc)
        await update.message.reply_text(f"❌ Error al consultar TMDb: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error")
        await update.message.reply_text(f"❌ Error inesperado: {exc}")


async def cmd_serie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /serie <título o URL de TMDb>  — busca directamente en TV.
    """
    if not context.args:
        await update.message.reply_text(
            "⚠️ Uso: /serie <título> o /serie <URL de TMDb>\n"
            "Ejemplo: /serie Breaking Bad"
        )
        return

    query = " ".join(context.args).strip()
    await update.message.reply_text("🔍 Buscando información, un momento…")

    try:
        parsed = parse_tmdb_url(query)
        if parsed:
            media_type, tmdb_id = parsed
            details = fetch_tv_details(tmdb_id)
            media_type = "tv"
        else:
            details = search_tv(query)
            media_type = "tv"

        if not details:
            await update.message.reply_text("❌ No encontré ningún resultado para esa búsqueda.")
            return

        await send_movie_to_channel(context, details, media_type, update.message.chat_id)

    except requests.HTTPError as exc:
        logger.error("TMDb HTTP error: %s", exc)
        await update.message.reply_text(f"❌ Error al consultar TMDb: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error")
        await update.message.reply_text(f"❌ Error inesperado: {exc}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🎬 <b>Bot de películas y series</b>\n\n"
        "Comandos disponibles:\n"
        "• /pelicula &lt;título o URL TMDb&gt; — busca película (también prueba en series)\n"
        "• /serie &lt;título o URL TMDb&gt; — busca directamente en series\n\n"
        "Ejemplos:\n"
        "<code>/pelicula Inception</code>\n"
        "<code>/serie Breaking Bad</code>\n"
        "<code>/pelicula https://www.themoviedb.org/movie/27205-inception</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Callback query handler (inline buttons)
# ---------------------------------------------------------------------------

VOTE_LABELS = {
    "vote_recommend": "👍 La recomiendo",
    "vote_great": "🥰❤️ Buenísima",
    "vote_not_seen": "🙈 No la he visto",
    "vote_better": "❌ Hay mejores",
}


async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    label = VOTE_LABELS.get(query.data, query.data)
    user = query.from_user.first_name or "Alguien"
    await query.answer(f"{user} votó: {label}", show_alert=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Python 3.14+ requires an explicit event loop
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("pelicula", cmd_pelicula))
    app.add_handler(CommandHandler("serie", cmd_serie))
    app.add_handler(CallbackQueryHandler(handle_vote, pattern=r"^vote_"))

    logger.info("Bot iniciado. Presiona Ctrl+C para detener.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
