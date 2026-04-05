import asyncio
import os
import re
import logging
import requests

from dotenv import load_dotenv
load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
# Config
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TMDB_API_KEY     = os.environ["TMDB_API_KEY"]
TMDB_BEARER      = os.environ.get("TMDB_BEARER", "")
CHANNEL_ID       = os.environ["CHANNEL_ID"]        # e.g. "-1001234567890"
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "")  # e.g. "micanal" (sin @)

TMDB_BASE       = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

# ---------------------------------------------------------------------------
# Vote counter  (en memoria; se reinicia al reiniciar el bot)
# {message_id: {"vote_recommend": count, ...}}
# ---------------------------------------------------------------------------
vote_counts: dict[int, dict[str, int]] = {}
# Para evitar votos duplicados por usuario
user_votes: dict[int, dict[int, str]] = {}   # {message_id: {user_id: vote_key}}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def rating_to_stars(rating: float) -> str:
    full = int(rating)
    half = (rating - full) >= 0.5
    stars = "⭐" * full + ("½" if half else "")
    return stars or "☆"


def minutes_to_duration(minutes: int) -> str:
    if not minutes:
        return "N/D"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}min" if h else f"{m}min"


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


def parse_tmdb_url(url: str) -> tuple[str, int] | None:
    match = re.search(r"themoviedb\.org/(movie|tv)/(\d+)", url)
    return (match.group(1), int(match.group(2))) if match else None


def fetch_movie_details(tmdb_id: int) -> dict:
    return tmdb_get(f"/movie/{tmdb_id}")


def fetch_tv_details(tmdb_id: int) -> dict:
    return tmdb_get(f"/tv/{tmdb_id}")


def search_movie(title: str) -> dict | None:
    data = tmdb_get("/search/movie", {"query": title})
    results = data.get("results", [])
    return tmdb_get(f"/movie/{results[0]['id']}") if results else None


def search_tv(title: str) -> dict | None:
    data = tmdb_get("/search/tv", {"query": title})
    results = data.get("results", [])
    return tmdb_get(f"/tv/{results[0]['id']}") if results else None


def channel_url() -> str:
    """URL base del canal para usar como enlace en el caption."""
    if CHANNEL_USERNAME:
        return f"https://t.me/{CHANNEL_USERNAME}"
    # Canal privado: quitar el prefijo -100
    numeric = str(CHANNEL_ID).lstrip("-")
    if numeric.startswith("100"):
        numeric = numeric[3:]
    return f"https://t.me/c/{numeric}"


def build_caption(details: dict, media_type: str) -> str:
    title      = details.get("title") or details.get("name") or "Sin título"
    release_raw = details.get("release_date") or details.get("first_air_date") or ""
    year       = release_raw[:4] if release_raw else "N/D"
    rating     = details.get("vote_average", 0.0)
    stars      = rating_to_stars(rating)
    overview   = details.get("overview") or "Sin descripción disponible."
    genres_raw = ", ".join(g["name"] for g in details.get("genres", [])) or "N/D"

    if media_type == "movie":
        duration = minutes_to_duration(details.get("runtime", 0))
    else:
        ep = details.get("episode_run_time", [])
        duration = (minutes_to_duration(ep[0]) + " por episodio") if ep else "N/D"

    base = channel_url()

    # Título como enlace (sin año) + año en negritas separado
    title_link  = f'<a href="{base}"><b>{title}</b></a>'
    year_bold   = f"<b>({year})</b>"
    # Género como enlace
    genre_link  = f'<a href="{base}">{genres_raw}</a>'

    caption = (
        f"🎬 {title_link} {year_bold}\n"
        f"Disponible AHORA! ⚡\n\n"
        f"🚦 Calificación de usuarios: {stars} ({rating:.1f}/10)\n"
        f"🗓️ Fecha de estreno: {release_raw or 'N/D'}\n\n"
        f"📖 Resumen: {overview}\n\n"
        f"🎭 Género: {genre_link}\n"
        f"⏱️ Duración: {duration}\n"
        f"<tg-spoiler>⚽📺 Activa tu servicio con SPORTIFI.tv  ⚽📺</tg-spoiler>\n"
        f"<tg-spoiler>✉️ elsistematv.com/whatsapp</tg-spoiler>"
    )
    return caption


VOTE_META = {
    "vote_recommend": "👍 La recomiendo",
    "vote_great":     "🥰❤️ Buenísima",
    "vote_not_seen":  "🙈 No la he visto",
    "vote_better":    "❌ Hay mejores",
}


def build_keyboard(message_id: int, share_url: str | None = None) -> InlineKeyboardMarkup:
    counts = vote_counts.get(message_id, {})

    def btn(key: str) -> InlineKeyboardButton:
        label = VOTE_META[key]
        c = counts.get(key, 0)
        text = f"{label}  {c}" if c > 0 else label
        return InlineKeyboardButton(text, callback_data=f"{key}:{message_id}")

    rows = [
        [btn("vote_recommend"), btn("vote_great")],
        [btn("vote_not_seen"),  btn("vote_better")],
    ]

    # Botón COMPARTIR — si tenemos la URL del mensaje la usamos como botón URL,
    # si no usamos switch_inline_query para abrir selector de chat
    if share_url:
        rows.append([InlineKeyboardButton("📤  COMPARTIR", url=share_url)])
    else:
        rows.append([InlineKeyboardButton("📤  COMPARTIR", switch_inline_query="")])

    return InlineKeyboardMarkup(rows)


def message_url(message_id: int) -> str:
    """URL directa al mensaje publicado en el canal."""
    if CHANNEL_USERNAME:
        return f"https://t.me/{CHANNEL_USERNAME}/{message_id}"
    numeric = str(CHANNEL_ID).lstrip("-")
    if numeric.startswith("100"):
        numeric = numeric[3:]
    return f"https://t.me/c/{numeric}/{message_id}"


# ---------------------------------------------------------------------------
# Publicar en el canal
# ---------------------------------------------------------------------------

async def send_movie_to_channel(
    context: ContextTypes.DEFAULT_TYPE,
    details: dict,
    media_type: str,
    reply_chat_id: int | str,
) -> None:
    caption      = build_caption(details, media_type)
    poster_path  = details.get("poster_path")

    # Keyboard provisional sin share_url (no conocemos el message_id aún)
    tmp_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👍 La recomiendo", callback_data="tmp"),
         InlineKeyboardButton("🥰❤️ Buenísima",   callback_data="tmp")],
        [InlineKeyboardButton("🙈 No la he visto", callback_data="tmp"),
         InlineKeyboardButton("❌ Hay mejores",    callback_data="tmp")],
    ])

    if poster_path:
        sent = await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=f"{TMDB_IMAGE_BASE}{poster_path}",
            caption=caption,
            parse_mode="HTML",
            reply_markup=tmp_keyboard,
        )
    else:
        sent = await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=caption,
            parse_mode="HTML",
            reply_markup=tmp_keyboard,
        )

    # Ahora que conocemos el message_id, actualizamos el teclado con el enlace real
    msg_id   = sent.message_id
    vote_counts[msg_id]  = {k: 0 for k in VOTE_META}
    user_votes[msg_id]   = {}
    share    = message_url(msg_id)
    keyboard = build_keyboard(msg_id, share)

    if poster_path:
        await context.bot.edit_message_reply_markup(
            chat_id=CHANNEL_ID, message_id=msg_id, reply_markup=keyboard
        )
    else:
        await context.bot.edit_message_reply_markup(
            chat_id=CHANNEL_ID, message_id=msg_id, reply_markup=keyboard
        )

    if str(reply_chat_id) != str(CHANNEL_ID):
        await context.bot.send_message(chat_id=reply_chat_id, text="✅ Publicado en el canal.")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pelicula <título o URL de TMDb>"""
    if not context.args:
        await update.message.reply_text(
            "⚠️ Uso: /pelicula <título> o /pelicula <URL de TMDb>\n"
            "Ejemplo: /pelicula Inception"
        )
        return

    query = " ".join(context.args).strip()
    await update.message.reply_text("🔍 Buscando información, un momento…")

    try:
        parsed = parse_tmdb_url(query)
        if parsed:
            media_type, tmdb_id = parsed
            details = fetch_movie_details(tmdb_id) if media_type == "movie" else fetch_tv_details(tmdb_id)
        else:
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
    """/serie <título o URL de TMDb>"""
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
            details = fetch_tv_details(parsed[1])
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
        "• /pelicula &lt;título o URL TMDb&gt;\n"
        "• /serie &lt;título o URL TMDb&gt;\n\n"
        "Ejemplos:\n"
        "<code>/pelicula Inception</code>\n"
        "<code>/serie Breaking Bad</code>\n"
        "<code>/pelicula https://www.themoviedb.org/movie/27205-inception</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Callback: votos con conteo tipo reacción
# ---------------------------------------------------------------------------

async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data  # formato "vote_xxx:message_id"
    if ":" not in data:
        return

    vote_key, msg_id_str = data.rsplit(":", 1)
    try:
        msg_id = int(msg_id_str)
    except ValueError:
        return

    if vote_key not in VOTE_META:
        return

    user_id = query.from_user.id

    # Inicializar si no existe
    if msg_id not in vote_counts:
        vote_counts[msg_id] = {k: 0 for k in VOTE_META}
        user_votes[msg_id]  = {}

    prev_vote = user_votes[msg_id].get(user_id)

    if prev_vote == vote_key:
        # Quitar voto (toggle)
        vote_counts[msg_id][vote_key] = max(0, vote_counts[msg_id][vote_key] - 1)
        del user_votes[msg_id][user_id]
        await query.answer("Voto retirado", show_alert=False)
    else:
        # Quitar voto anterior si tenía uno distinto
        if prev_vote:
            vote_counts[msg_id][prev_vote] = max(0, vote_counts[msg_id][prev_vote] - 1)
        # Agregar nuevo voto
        vote_counts[msg_id][vote_key] += 1
        user_votes[msg_id][user_id] = vote_key
        await query.answer(f"{VOTE_META[vote_key]}", show_alert=False)

    # Actualizar teclado con nuevos conteos
    share = message_url(msg_id)
    new_keyboard = build_keyboard(msg_id, share)
    try:
        await query.edit_message_reply_markup(reply_markup=new_keyboard)
    except Exception:
        pass  # Sin cambios si el teclado es idéntico


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("pelicula", cmd_pelicula))
    app.add_handler(CommandHandler("serie",    cmd_serie))
    app.add_handler(CallbackQueryHandler(handle_vote, pattern=r"^vote_\w+:\d+$"))

    logger.info("Bot iniciado. Presiona Ctrl+C para detener.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
