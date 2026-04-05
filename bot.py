import asyncio
import os
import re
import random
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
CHANNEL_ID       = os.environ["CHANNEL_ID"]
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "")  # sin @

# --- Votos automáticos ---
# Cantidad de votos iniciales por botón al publicar (aleatorio entre MIN y MAX)
VOTE_SEED_MIN  = int(os.environ.get("VOTE_SEED_MIN",  "8"))
VOTE_SEED_MAX  = int(os.environ.get("VOTE_SEED_MAX",  "25"))
# Cada cuántos segundos se agregan votos automáticos
VOTE_GROW_SECS = int(os.environ.get("VOTE_GROW_SECS", "240"))
# Cuántos votos automáticos se agregan por intervalo (aleatorio entre MIN y MAX)
VOTE_GROW_MIN  = int(os.environ.get("VOTE_GROW_MIN",  "1"))
VOTE_GROW_MAX  = int(os.environ.get("VOTE_GROW_MAX",  "4"))
# Cuántas horas dura el crecimiento automático
VOTE_GROW_HOURS = int(os.environ.get("VOTE_GROW_HOURS", "12"))

TMDB_BASE       = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

# ---------------------------------------------------------------------------
# Estado en memoria
# ---------------------------------------------------------------------------
# Conteo de votos por message_id publicado en el canal
vote_counts: dict[int, dict[str, int]] = {}
# Un voto por usuario: {message_id: {user_id: vote_key}}
user_votes:  dict[int, dict[int, str]] = {}
# Posts pendientes de aprobación: {temp_id: {details, media_type, requester_chat_id}}
pending_posts: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# TMDb helpers
# ---------------------------------------------------------------------------

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
    m = re.search(r"themoviedb\.org/(movie|tv)/(\d+)", url)
    return (m.group(1), int(m.group(2))) if m else None


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

# ---------------------------------------------------------------------------
# Caption & keyboard builders
# ---------------------------------------------------------------------------

def rating_to_stars(rating: float) -> str:
    full = int(rating)
    half = (rating - full) >= 0.5
    return ("⭐" * full + ("½" if half else "")) or "☆"


def minutes_to_duration(minutes: int) -> str:
    if not minutes:
        return "N/D"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}min" if h else f"{m}min"


def channel_base_url() -> str:
    if CHANNEL_USERNAME:
        return f"https://t.me/{CHANNEL_USERNAME}"
    numeric = str(CHANNEL_ID).lstrip("-")
    if numeric.startswith("100"):
        numeric = numeric[3:]
    return f"https://t.me/c/{numeric}"


def message_url(message_id: int) -> str:
    if CHANNEL_USERNAME:
        return f"https://t.me/{CHANNEL_USERNAME}/{message_id}"
    numeric = str(CHANNEL_ID).lstrip("-")
    if numeric.startswith("100"):
        numeric = numeric[3:]
    return f"https://t.me/c/{numeric}/{message_id}"


def build_caption(details: dict, media_type: str) -> str:
    title       = details.get("title") or details.get("name") or "Sin título"
    release_raw = details.get("release_date") or details.get("first_air_date") or ""
    year        = release_raw[:4] if release_raw else "N/D"
    rating      = details.get("vote_average", 0.0)
    stars       = rating_to_stars(rating)
    overview    = details.get("overview") or "Sin descripción disponible."
    genres_str  = ", ".join(g["name"] for g in details.get("genres", [])) or "N/D"

    if media_type == "movie":
        duration = minutes_to_duration(details.get("runtime", 0))
    else:
        ep = details.get("episode_run_time", [])
        duration = (minutes_to_duration(ep[0]) + " por episodio") if ep else "N/D"

    base = channel_base_url()
    title_link = f'<a href="{base}"><b>{title}</b></a>'
    year_bold  = f"<b>({year})</b>"
    genre_link = f'<a href="{base}">{genres_str}</a>'

    return (
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
        [InlineKeyboardButton(
            "📤  COMPARTIR",
            url=share_url if share_url else channel_base_url()
        )],
    ]
    return InlineKeyboardMarkup(rows)


def build_preview_keyboard(temp_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Publicar en el canal", callback_data=f"pub:{temp_id}"),
        InlineKeyboardButton("❌ Cancelar",            callback_data=f"cancel:{temp_id}"),
    ]])

# ---------------------------------------------------------------------------
# Votos automáticos (background task)
# ---------------------------------------------------------------------------

async def auto_grow_votes(bot, message_id: int) -> None:
    """Incrementa votos gradualmente para simular actividad orgánica."""
    iterations = (VOTE_GROW_HOURS * 3600) // max(VOTE_GROW_SECS, 1)
    for _ in range(iterations):
        await asyncio.sleep(VOTE_GROW_SECS)
        if message_id not in vote_counts:
            break
        for key in VOTE_META:
            vote_counts[message_id][key] += random.randint(VOTE_GROW_MIN, VOTE_GROW_MAX)
        share = message_url(message_id)
        try:
            await bot.edit_message_reply_markup(
                chat_id=CHANNEL_ID,
                message_id=message_id,
                reply_markup=build_keyboard(message_id, share),
            )
        except Exception as exc:
            logger.debug("auto_grow stop: %s", exc)
            break

# ---------------------------------------------------------------------------
# Publicar en el canal
# ---------------------------------------------------------------------------

async def publish_to_channel(context: ContextTypes.DEFAULT_TYPE, details: dict, media_type: str) -> None:
    caption     = build_caption(details, media_type)
    poster_path = details.get("poster_path")

    # Teclado provisional (sin message_id aún)
    tmp_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👍 La recomiendo", callback_data="tmp"),
         InlineKeyboardButton("🥰❤️ Buenísima",   callback_data="tmp")],
        [InlineKeyboardButton("🙈 No la he visto", callback_data="tmp"),
         InlineKeyboardButton("❌ Hay mejores",    callback_data="tmp")],
        [InlineKeyboardButton("📤  COMPARTIR",     callback_data="tmp")],
    ])

    if poster_path:
        sent = await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=f"{TMDB_IMAGE_BASE}{poster_path}",
            caption=caption,
            parse_mode="HTML",
            reply_markup=tmp_kb,
        )
    else:
        sent = await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=caption,
            parse_mode="HTML",
            reply_markup=tmp_kb,
        )

    msg_id = sent.message_id

    # Sembrar votos iniciales aleatorios
    vote_counts[msg_id] = {k: random.randint(VOTE_SEED_MIN, VOTE_SEED_MAX) for k in VOTE_META}
    user_votes[msg_id]  = {}

    # Actualizar teclado con conteos reales y enlace de compartir
    share = message_url(msg_id)
    await context.bot.edit_message_reply_markup(
        chat_id=CHANNEL_ID,
        message_id=msg_id,
        reply_markup=build_keyboard(msg_id, share),
    )

    # Lanzar crecimiento automático en segundo plano
    asyncio.create_task(auto_grow_votes(context.bot, msg_id))


# ---------------------------------------------------------------------------
# Flujo de búsqueda → preview → publicar
# ---------------------------------------------------------------------------

async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE, force_tv: bool = False) -> None:
    if not context.args:
        cmd = "serie" if force_tv else "pelicula"
        await update.message.reply_text(f"⚠️ Uso: /{cmd} <título o URL de TMDb>")
        return

    query = " ".join(context.args).strip()
    await update.message.reply_text("🔍 Buscando información, un momento…")

    try:
        parsed = parse_tmdb_url(query)
        if parsed:
            media_type, tmdb_id = parsed
            details = fetch_tv_details(tmdb_id) if media_type == "tv" else fetch_movie_details(tmdb_id)
        elif force_tv:
            details    = search_tv(query)
            media_type = "tv"
        else:
            details = search_movie(query)
            if details:
                media_type = "movie"
            else:
                details    = search_tv(query)
                media_type = "tv"

        if not details:
            await update.message.reply_text("❌ No encontré ningún resultado.")
            return

        # Guardar en pending y enviar preview
        temp_id = f"{update.effective_user.id}_{update.message.message_id}"
        pending_posts[temp_id] = {
            "details":    details,
            "media_type": media_type,
            "chat_id":    update.message.chat_id,
        }

        caption     = build_caption(details, media_type)
        poster_path = details.get("poster_path")
        preview_kb  = build_preview_keyboard(temp_id)

        header = "👁️ <b>VISTA PREVIA</b> — revisa antes de publicar:\n\n"

        if poster_path:
            await context.bot.send_photo(
                chat_id=update.message.chat_id,
                photo=f"{TMDB_IMAGE_BASE}{poster_path}",
                caption=header + caption,
                parse_mode="HTML",
                reply_markup=preview_kb,
            )
        else:
            await update.message.reply_text(
                header + caption,
                parse_mode="HTML",
                reply_markup=preview_kb,
            )

    except requests.HTTPError as exc:
        logger.error("TMDb HTTP error: %s", exc)
        await update.message.reply_text(f"❌ Error al consultar TMDb: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error")
        await update.message.reply_text(f"❌ Error inesperado: {exc}")


async def cmd_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_search(update, context, force_tv=False)


async def cmd_serie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_search(update, context, force_tv=True)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🎬 <b>Bot de películas y series</b>\n\n"
        "Comandos:\n"
        "• /pelicula &lt;título o URL TMDb&gt;\n"
        "• /serie &lt;título o URL TMDb&gt;\n\n"
        "El bot te mostrará una <b>vista previa</b> para que apruebes antes de publicar en el canal."
    )
    await update.message.reply_text(text, parse_mode="HTML")

# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data  = query.data

    # ── Confirmar publicación ───────────────────────────────────────────────
    if data.startswith("pub:"):
        temp_id = data[4:]
        post    = pending_posts.pop(temp_id, None)
        if not post:
            await query.edit_message_caption(
                caption="⚠️ Este post ya fue publicado o cancelado.",
                parse_mode="HTML",
            )
            return
        await publish_to_channel(context, post["details"], post["media_type"])
        # Actualizar el mensaje de preview para confirmar
        try:
            await query.edit_message_caption(
                caption="✅ <b>Publicado en el canal.</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # ── Cancelar publicación ────────────────────────────────────────────────
    if data.startswith("cancel:"):
        temp_id = data[7:]
        pending_posts.pop(temp_id, None)
        try:
            await query.edit_message_caption(
                caption="❌ Publicación cancelada.",
                parse_mode="HTML",
            )
        except Exception:
            await query.edit_message_text("❌ Publicación cancelada.")
        return

    # ── Votos ───────────────────────────────────────────────────────────────
    if ":" in data and data.startswith("vote_"):
        vote_key, msg_id_str = data.rsplit(":", 1)
        try:
            msg_id = int(msg_id_str)
        except ValueError:
            return
        if vote_key not in VOTE_META:
            return

        user_id = query.from_user.id

        if msg_id not in vote_counts:
            vote_counts[msg_id] = {k: 0 for k in VOTE_META}
            user_votes[msg_id]  = {}

        prev = user_votes[msg_id].get(user_id)
        if prev == vote_key:
            # Toggle off
            vote_counts[msg_id][vote_key] = max(0, vote_counts[msg_id][vote_key] - 1)
            del user_votes[msg_id][user_id]
            await query.answer("Voto retirado", show_alert=False)
        else:
            if prev:
                vote_counts[msg_id][prev] = max(0, vote_counts[msg_id][prev] - 1)
            vote_counts[msg_id][vote_key] += 1
            user_votes[msg_id][user_id] = vote_key
            await query.answer(VOTE_META[vote_key], show_alert=False)

        share = message_url(msg_id)
        try:
            await query.edit_message_reply_markup(reply_markup=build_keyboard(msg_id, share))
        except Exception:
            pass

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
    app.add_handler(CallbackQueryHandler(handle_callbacks))

    logger.info("Bot iniciado. Presiona Ctrl+C para detener.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
