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
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "")

# — Votos automáticos —
VOTE_SEED_MIN   = int(os.environ.get("VOTE_SEED_MIN",   "8"))    # votos iniciales mínimos por botón
VOTE_SEED_MAX   = int(os.environ.get("VOTE_SEED_MAX",   "25"))   # votos iniciales máximos por botón
VOTE_MAX        = int(os.environ.get("VOTE_MAX",        "200"))  # techo de votos automáticos por botón
VOTE_GROW_SECS  = int(os.environ.get("VOTE_GROW_SECS",  "240"))  # segundos entre cada ciclo de crecimiento
VOTE_GROW_MIN   = int(os.environ.get("VOTE_GROW_MIN",   "1"))    # votos mínimos por ciclo (distribuidos)
VOTE_GROW_MAX   = int(os.environ.get("VOTE_GROW_MAX",   "4"))    # votos máximos por ciclo (distribuidos)
VOTE_GROW_HOURS = int(os.environ.get("VOTE_GROW_HOURS", "12"))   # horas que dura el crecimiento automático

# — Contador COMPARTIR —
SHARE_GROW_SECS  = int(os.environ.get("SHARE_GROW_SECS",  "300")) # segundos entre cada ciclo
SHARE_GROW_MIN   = int(os.environ.get("SHARE_GROW_MIN",   "1"))   # mínimo a agregar por ciclo
SHARE_GROW_MAX   = int(os.environ.get("SHARE_GROW_MAX",   "3"))   # máximo a agregar por ciclo
SHARE_GROW_HOURS = int(os.environ.get("SHARE_GROW_HOURS", "12"))  # horas que dura el crecimiento

TMDB_BASE       = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

# ---------------------------------------------------------------------------
# Estado en memoria
# ---------------------------------------------------------------------------
# {message_id: {vote_key: count}}
vote_counts:  dict[int, dict[str, int]] = {}
# {message_id: {user_id: vote_key}}  — un voto por usuario
user_votes:   dict[int, dict[int, str]] = {}
# {message_id: int}  — contador de compartidos (empieza en 0)
share_counts: dict[int, int] = {}
# {temp_id: {details, media_type, chat_id}}
pending_posts: dict[str, dict] = {}

VOTE_META = {
    "vote_recommend": "👍 La recomiendo",
    "vote_great":     "🥰❤️ Buenísima",
    "vote_not_seen":  "🙈 No la he visto",
    "vote_better":    "❌ Hay mejores",
}

# ---------------------------------------------------------------------------
# TMDb
# ---------------------------------------------------------------------------

def tmdb_get(endpoint: str, params: dict | None = None) -> dict:
    params = params or {}
    params.setdefault("language", "es-ES")
    if TMDB_BEARER:
        resp = requests.get(
            f"{TMDB_BASE}{endpoint}", params=params,
            headers={"Authorization": f"Bearer {TMDB_BEARER}"}, timeout=10,
        )
    else:
        params["api_key"] = TMDB_API_KEY
        resp = requests.get(f"{TMDB_BASE}{endpoint}", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def parse_tmdb_url(url: str) -> tuple[str, int] | None:
    m = re.search(r"themoviedb\.org/(movie|tv)/(\d+)", url)
    return (m.group(1), int(m.group(2))) if m else None


def search_movie(title: str) -> dict | None:
    r = tmdb_get("/search/movie", {"query": title}).get("results", [])
    return tmdb_get(f"/movie/{r[0]['id']}") if r else None


def search_tv(title: str) -> dict | None:
    r = tmdb_get("/search/tv", {"query": title}).get("results", [])
    return tmdb_get(f"/tv/{r[0]['id']}") if r else None


def fetch_details(media_type: str, tmdb_id: int) -> dict:
    return tmdb_get(f"/{media_type}/{tmdb_id}")

# ---------------------------------------------------------------------------
# URLs del canal
# ---------------------------------------------------------------------------

def _channel_numeric() -> str:
    n = str(CHANNEL_ID).lstrip("-")
    return n[3:] if n.startswith("100") else n


def channel_base_url() -> str:
    return f"https://t.me/{CHANNEL_USERNAME}" if CHANNEL_USERNAME else f"https://t.me/c/{_channel_numeric()}"


def message_url(message_id: int) -> str:
    if CHANNEL_USERNAME:
        return f"https://t.me/{CHANNEL_USERNAME}/{message_id}"
    return f"https://t.me/c/{_channel_numeric()}/{message_id}"

# ---------------------------------------------------------------------------
# Caption
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


def build_caption(details: dict, media_type: str) -> str:
    title       = details.get("title") or details.get("name") or "Sin título"
    release_raw = details.get("release_date") or details.get("first_air_date") or ""
    year        = release_raw[:4] if release_raw else "N/D"
    rating      = details.get("vote_average", 0.0)
    overview    = details.get("overview") or "Sin descripción disponible."
    genres_str  = ", ".join(g["name"] for g in details.get("genres", [])) or "N/D"

    if media_type == "movie":
        duration = minutes_to_duration(details.get("runtime", 0))
    else:
        ep = details.get("episode_run_time", [])
        duration = (minutes_to_duration(ep[0]) + " por episodio") if ep else "N/D"

    base       = channel_base_url()
    title_link = f'<a href="{base}"><b>{title}</b></a>'
    genre_link = f'<a href="{base}">{genres_str}</a>'

    return (
        f"🎬 {title_link} <b>({year})</b>\n"
        f"Disponible AHORA! ⚡\n\n"
        f"🚦 Calificación de usuarios: {rating_to_stars(rating)} ({rating:.1f}/10)\n"
        f"🗓️ Fecha de estreno: {release_raw or 'N/D'}\n\n"
        f"📖 Resumen: {overview}\n\n"
        f"🎭 Género: {genre_link}\n"
        f"⏱️ Duración: {duration}\n"
        f"<tg-spoiler>⚽📺 Activa tu servicio con SPORTIFI.tv  ⚽📺</tg-spoiler>\n"
        f"<tg-spoiler>✉️ elsistematv.com/whatsapp</tg-spoiler>"
    )

# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def build_channel_keyboard(message_id: int) -> InlineKeyboardMarkup:
    counts = vote_counts.get(message_id, {})
    shares = share_counts.get(message_id, 0)

    def btn(key: str) -> InlineKeyboardButton:
        c    = counts.get(key, 0)
        text = f"{VOTE_META[key]}  {c}" if c > 0 else VOTE_META[key]
        return InlineKeyboardButton(text, callback_data=f"{key}:{message_id}")

    share_label = f"📤  COMPARTIR  {shares}" if shares > 0 else "📤  COMPARTIR"

    return InlineKeyboardMarkup([
        [btn("vote_recommend"), btn("vote_great")],
        [btn("vote_not_seen"),  btn("vote_better")],
        # Botón COMPARTIR: callback que responde con la URL (answer url trick)
        [InlineKeyboardButton(share_label, callback_data=f"share:{message_id}")],
    ])


def build_preview_keyboard(temp_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Publicar en el canal", callback_data=f"pub:{temp_id}"),
        InlineKeyboardButton("❌ Cancelar",            callback_data=f"cancel:{temp_id}"),
    ]])

# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

def _distribute_votes(total: int, n: int) -> list[int]:
    """Reparte `total` votos aleatoriamente entre `n` botones (algunos pueden quedar en 0)."""
    result = [0] * n
    for _ in range(total):
        result[random.randrange(n)] += 1
    return result


async def auto_grow_votes(bot, message_id: int) -> None:
    """Incrementa votos gradualmente respetando VOTE_MAX. Los votos reales no tienen techo."""
    iters = (VOTE_GROW_HOURS * 3600) // max(VOTE_GROW_SECS, 1)
    keys  = list(VOTE_META.keys())

    for _ in range(iters):
        await asyncio.sleep(VOTE_GROW_SECS)
        if message_id not in vote_counts:
            break

        # Votos a repartir este ciclo
        pool = random.randint(VOTE_GROW_MIN, VOTE_GROW_MAX)
        dist = _distribute_votes(pool, len(keys))

        changed = False
        for key, add in zip(keys, dist):
            if add == 0:
                continue
            current = vote_counts[message_id][key]
            # Solo suma automáticamente si no llegó al techo
            if current < VOTE_MAX:
                vote_counts[message_id][key] = min(current + add, VOTE_MAX)
                changed = True

        if changed:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=CHANNEL_ID,
                    message_id=message_id,
                    reply_markup=build_channel_keyboard(message_id),
                )
            except Exception as exc:
                logger.debug("auto_grow_votes stop: %s", exc)
                break


async def auto_grow_shares(bot, message_id: int) -> None:
    """Incrementa el contador de COMPARTIR gradualmente, empieza en 0."""
    iters = (SHARE_GROW_HOURS * 3600) // max(SHARE_GROW_SECS, 1)

    for _ in range(iters):
        await asyncio.sleep(SHARE_GROW_SECS)
        if message_id not in share_counts:
            break

        share_counts[message_id] += random.randint(SHARE_GROW_MIN, SHARE_GROW_MAX)
        try:
            await bot.edit_message_reply_markup(
                chat_id=CHANNEL_ID,
                message_id=message_id,
                reply_markup=build_channel_keyboard(message_id),
            )
        except Exception as exc:
            logger.debug("auto_grow_shares stop: %s", exc)
            break

# ---------------------------------------------------------------------------
# Publicar en el canal
# ---------------------------------------------------------------------------

async def publish_to_channel(context: ContextTypes.DEFAULT_TYPE, details: dict, media_type: str) -> None:
    caption     = build_caption(details, media_type)
    poster_path = details.get("poster_path")

    # Keyboard placeholder sin message_id
    tmp_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("👍 La recomiendo", callback_data="tmp"),
        InlineKeyboardButton("🥰❤️ Buenísima",   callback_data="tmp"),
    ], [
        InlineKeyboardButton("🙈 No la he visto", callback_data="tmp"),
        InlineKeyboardButton("❌ Hay mejores",    callback_data="tmp"),
    ], [
        InlineKeyboardButton("📤  COMPARTIR", callback_data="tmp"),
    ]])

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

    # Sembrar votos iniciales aleatorios por botón
    vote_counts[msg_id]  = {k: random.randint(VOTE_SEED_MIN, VOTE_SEED_MAX) for k in VOTE_META}
    user_votes[msg_id]   = {}
    share_counts[msg_id] = 0   # COMPARTIR siempre empieza en 0

    # Actualizar teclado con conteos reales
    await context.bot.edit_message_reply_markup(
        chat_id=CHANNEL_ID,
        message_id=msg_id,
        reply_markup=build_channel_keyboard(msg_id),
    )

    # Lanzar crecimiento automático en segundo plano
    asyncio.create_task(auto_grow_votes(context.bot, msg_id))
    asyncio.create_task(auto_grow_shares(context.bot, msg_id))

# ---------------------------------------------------------------------------
# Flujo preview → publicar
# ---------------------------------------------------------------------------

def preview_info_text() -> str:
    return (
        f"\n\n─────────────────────\n"
        f"⚙️ <b>Configuración de votos automáticos:</b>\n"
        f"• Votos iniciales por botón: {VOTE_SEED_MIN}–{VOTE_SEED_MAX}\n"
        f"• Techo automático por botón: {VOTE_MAX}\n"
        f"• Crecimiento cada: {VOTE_GROW_SECS}s durante {VOTE_GROW_HOURS}h\n"
        f"• Votos por ciclo (distribuidos): {VOTE_GROW_MIN}–{VOTE_GROW_MAX}\n"
        f"• COMPARTIR: crece cada {SHARE_GROW_SECS}s ({SHARE_GROW_MIN}–{SHARE_GROW_MAX} por ciclo) durante {SHARE_GROW_HOURS}h\n"
        f"  (comienza en 0, los clics reales también suman)"
    )


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
            details = fetch_details(media_type, tmdb_id)
        elif force_tv:
            details, media_type = search_tv(query), "tv"
        else:
            details = search_movie(query)
            if details:
                media_type = "movie"
            else:
                details, media_type = search_tv(query), "tv"

        if not details:
            await update.message.reply_text("❌ No encontré ningún resultado.")
            return

        temp_id = f"{update.effective_user.id}_{update.message.message_id}"
        pending_posts[temp_id] = {
            "details":    details,
            "media_type": media_type,
            "chat_id":    update.message.chat_id,
        }

        caption    = build_caption(details, media_type)
        preview_kb = build_preview_keyboard(temp_id)
        header     = "👁️ <b>VISTA PREVIA</b>\n\n"
        full_text  = header + caption + preview_info_text()

        if details.get("poster_path"):
            await context.bot.send_photo(
                chat_id=update.message.chat_id,
                photo=f"{TMDB_IMAGE_BASE}{details['poster_path']}",
                caption=full_text,
                parse_mode="HTML",
                reply_markup=preview_kb,
            )
        else:
            await update.message.reply_text(full_text, parse_mode="HTML", reply_markup=preview_kb)

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
    await update.message.reply_text(
        "🎬 <b>Bot de películas y series</b>\n\n"
        "Comandos:\n"
        "• /pelicula &lt;título o URL TMDb&gt;\n"
        "• /serie &lt;título o URL TMDb&gt;\n\n"
        "El bot te mostrará una <b>vista previa</b> con la configuración de votos antes de publicar.",
        parse_mode="HTML",
    )

# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data  = query.data

    # ── Publicar ────────────────────────────────────────────────────────────
    if data.startswith("pub:"):
        await query.answer()
        temp_id = data[4:]
        post    = pending_posts.pop(temp_id, None)
        if not post:
            try:
                await query.edit_message_caption(caption="⚠️ Ya fue publicado o cancelado.", parse_mode="HTML")
            except Exception:
                pass
            return
        await publish_to_channel(context, post["details"], post["media_type"])
        try:
            await query.edit_message_caption(caption="✅ <b>Publicado en el canal.</b>", parse_mode="HTML")
        except Exception:
            pass
        return

    # ── Cancelar ────────────────────────────────────────────────────────────
    if data.startswith("cancel:"):
        await query.answer()
        pending_posts.pop(data[7:], None)
        try:
            await query.edit_message_caption(caption="❌ Publicación cancelada.", parse_mode="HTML")
        except Exception:
            await query.edit_message_text("❌ Publicación cancelada.")
        return

    # ── COMPARTIR ────────────────────────────────────────────────────────────
    if data.startswith("share:"):
        try:
            msg_id = int(data[6:])
        except ValueError:
            await query.answer()
            return
        # Sumar 1 al contador por el clic real
        share_counts[msg_id] = share_counts.get(msg_id, 0) + 1
        url = message_url(msg_id)
        # answer(url=...) abre la URL en el cliente sin abrir un chat nuevo
        await query.answer(url=url)
        # Actualizar el botón con el nuevo conteo
        try:
            await query.edit_message_reply_markup(reply_markup=build_channel_keyboard(msg_id))
        except Exception:
            pass
        return

    # ── Votos ────────────────────────────────────────────────────────────────
    if data.startswith("vote_") and ":" in data:
        vote_key, msg_id_str = data.rsplit(":", 1)
        try:
            msg_id = int(msg_id_str)
        except ValueError:
            await query.answer()
            return
        if vote_key not in VOTE_META:
            await query.answer()
            return

        user_id = query.from_user.id

        if msg_id not in vote_counts:
            vote_counts[msg_id] = {k: 0 for k in VOTE_META}
            user_votes[msg_id]  = {}

        prev = user_votes[msg_id].get(user_id)
        if prev == vote_key:
            # Toggle: quitar voto (los votos reales pueden bajar de VOTE_MAX)
            vote_counts[msg_id][vote_key] = max(0, vote_counts[msg_id][vote_key] - 1)
            del user_votes[msg_id][user_id]
            await query.answer("Voto retirado")
        else:
            if prev:
                vote_counts[msg_id][prev] = max(0, vote_counts[msg_id][prev] - 1)
            # Los votos reales suman siempre, sin techo
            vote_counts[msg_id][vote_key] += 1
            user_votes[msg_id][user_id] = vote_key
            await query.answer(VOTE_META[vote_key])

        try:
            await query.edit_message_reply_markup(reply_markup=build_channel_keyboard(msg_id))
        except Exception:
            pass
        return

    await query.answer()

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
