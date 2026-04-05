import asyncio
import json
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
    MessageHandler,
    ContextTypes,
    filters,
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
VOTE_MAX        = int(os.environ.get("VOTE_MAX",        "200"))  # techo de votos automáticos
VOTE_GROW_SECS  = int(os.environ.get("VOTE_GROW_SECS",  "240"))  # segundos entre cada ciclo de crecimiento
VOTE_GROW_HOURS = int(os.environ.get("VOTE_GROW_HOURS", "12"))   # horas que dura el crecimiento automático

# — Contador COMPARTIR —
SHARE_GROW_SECS  = int(os.environ.get("SHARE_GROW_SECS",  "300"))
SHARE_GROW_MIN   = int(os.environ.get("SHARE_GROW_MIN",   "1"))
SHARE_GROW_MAX   = int(os.environ.get("SHARE_GROW_MAX",   "3"))
SHARE_GROW_HOURS = int(os.environ.get("SHARE_GROW_HOURS", "12"))

TMDB_BASE       = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

# ---------------------------------------------------------------------------
# Vote metadata
# ---------------------------------------------------------------------------
VOTE_META = {
    "vote_recommend": "👍 La recomiendo",
    "vote_great":     "🥰❤️ Buenísima",
    "vote_not_seen":  "🙈 No la he visto",
    "vote_better":    "❌ Hay mejores",
}
VOTE_KEYS = list(VOTE_META.keys())

# ---------------------------------------------------------------------------
# State machine constants
# ---------------------------------------------------------------------------
S_PUB_VOTES  = "pub_votes"
S_EDIT_VOTES = "edit_votes"

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
DATA_DIR  = os.path.join(os.path.dirname(__file__), "data")
DATA_FILE = os.path.join(DATA_DIR, "votes.json")

# In-memory stores — all keys are strings
vote_counts:  dict[str, dict[str, int]] = {}   # {msg_id: {vote_key: count}}
user_votes:   dict[str, dict[str, str]] = {}   # {msg_id: {user_id: vote_key}}
share_counts: dict[str, int]            = {}   # {msg_id: count}
vote_targets: dict[str, dict[str, int]] = {}   # {msg_id: {vote_key: target}}

# Not persisted
pending_posts: dict[str, dict] = {}            # {temp_id: {details, media_type, chat_id}}


def load_data() -> None:
    """Load persisted data from data/votes.json into global dicts."""
    global vote_counts, user_votes, share_counts, vote_targets
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(DATA_FILE):
        logger.info("No data file found, starting fresh.")
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        vote_counts  = raw.get("vote_counts",  {})
        user_votes   = raw.get("user_votes",   {})
        share_counts = raw.get("share_counts", {})
        vote_targets = raw.get("vote_targets", {})
        logger.info("Loaded data for %d messages.", len(vote_counts))
    except Exception as exc:
        logger.error("Failed to load data: %s", exc)


def save_data() -> None:
    """Atomically write all four dicts to data/votes.json."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_file = DATA_FILE + ".tmp"
    payload = {
        "vote_counts":  vote_counts,
        "user_votes":   user_votes,
        "share_counts": share_counts,
        "vote_targets": vote_targets,
    }
    try:
        with open(tmp_file, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_file, DATA_FILE)
    except Exception as exc:
        logger.error("Failed to save data: %s", exc)

# ---------------------------------------------------------------------------
# TMDb helpers
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
# Channel URL helpers
# ---------------------------------------------------------------------------

def _channel_numeric() -> str:
    n = str(CHANNEL_ID).lstrip("-")
    return n[3:] if n.startswith("100") else n


def channel_base_url() -> str:
    if CHANNEL_USERNAME:
        return f"https://t.me/{CHANNEL_USERNAME}"
    return f"https://t.me/c/{_channel_numeric()}"


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
    key_str = str(message_id)
    counts  = vote_counts.get(key_str, {})
    shares  = share_counts.get(key_str, 0)

    def btn(key: str) -> InlineKeyboardButton:
        c    = counts.get(key, 0)
        text = f"{VOTE_META[key]}  {c}" if c > 0 else VOTE_META[key]
        return InlineKeyboardButton(text, callback_data=f"{key}:{message_id}")

    share_label = f"📤  COMPARTIR  {shares}" if shares > 0 else "📤  COMPARTIR"

    return InlineKeyboardMarkup([
        [btn("vote_recommend"), btn("vote_great")],
        [btn("vote_not_seen"),  btn("vote_better")],
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

async def auto_grow_votes(bot, message_id: int) -> None:
    """Grow votes toward their targets over VOTE_GROW_HOURS using a natural distribution."""
    key_str      = str(message_id)
    total_cycles = (VOTE_GROW_HOURS * 3600) // max(VOTE_GROW_SECS, 1)

    for cycle_num in range(total_cycles):
        await asyncio.sleep(VOTE_GROW_SECS)
        if key_str not in vote_counts:
            break

        remaining_cycles = total_cycles - cycle_num
        changed = False

        for key in VOTE_KEYS:
            target  = vote_targets.get(key_str, {}).get(key, 0)
            current = vote_counts[key_str].get(key, 0)
            needed  = target - current
            if needed <= 0:
                continue
            expected = needed / remaining_cycles
            to_add   = int(expected)
            if random.random() < (expected - to_add):
                to_add += 1
            if to_add > 0:
                vote_counts[key_str][key] = min(current + to_add, max(target, VOTE_MAX))
                changed = True

        if changed:
            save_data()
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
    """Randomly grow the share counter over SHARE_GROW_HOURS."""
    key_str = str(message_id)
    iters   = (SHARE_GROW_HOURS * 3600) // max(SHARE_GROW_SECS, 1)

    for _ in range(iters):
        await asyncio.sleep(SHARE_GROW_SECS)
        if key_str not in share_counts:
            break

        share_counts[key_str] += random.randint(SHARE_GROW_MIN, SHARE_GROW_MAX)
        save_data()
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
# do_publish — final publish after targets collected
# ---------------------------------------------------------------------------

async def do_publish(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    post: dict,
    collected_targets: dict,
) -> None:
    """Send the post to the channel, seed data, start auto-grow tasks."""
    details    = post["details"]
    media_type = post["media_type"]
    caption    = build_caption(details, media_type)
    poster_path = details.get("poster_path")

    # Placeholder keyboard until we have the real message_id
    tmp_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👍 La recomiendo", callback_data="tmp"),
            InlineKeyboardButton("🥰❤️ Buenísima",   callback_data="tmp"),
        ],
        [
            InlineKeyboardButton("🙈 No la he visto", callback_data="tmp"),
            InlineKeyboardButton("❌ Hay mejores",    callback_data="tmp"),
        ],
        [InlineKeyboardButton("📤  COMPARTIR", callback_data="tmp")],
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

    msg_id  = sent.message_id
    key_str = str(msg_id)

    # Initialise with zero votes
    vote_counts[key_str]  = {k: 0 for k in VOTE_KEYS}
    user_votes[key_str]   = {}
    share_counts[key_str] = 0
    vote_targets[key_str] = collected_targets
    save_data()

    # Replace placeholder keyboard with real one
    await context.bot.edit_message_reply_markup(
        chat_id=CHANNEL_ID,
        message_id=msg_id,
        reply_markup=build_channel_keyboard(msg_id),
    )

    # Start background growth tasks
    asyncio.create_task(auto_grow_votes(context.bot, msg_id))
    asyncio.create_task(auto_grow_shares(context.bot, msg_id))

    # Notify the admin
    await context.bot.send_message(
        chat_id=post["chat_id"],
        text=(
            f"✅ <b>Publicado en el canal.</b>\n"
            f"ID de mensaje: <code>{msg_id}</code>\n"
            f"Targets: {collected_targets}"
        ),
        parse_mode="HTML",
    )

# ---------------------------------------------------------------------------
# Vote-target collection helpers
# ---------------------------------------------------------------------------

def _next_vote_question(vote_key: str, suffix: str = "") -> str:
    label = VOTE_META[vote_key]
    return (
        f"📊 ¿Cuántos votos quieres para *{label}* al final del día?\n"
        f"(escribe 0 para que no crezca){suffix}"
    )


def _next_edit_question(vote_key: str, current: int) -> str:
    label = VOTE_META[vote_key]
    return (
        f"✏️ ¿Nuevo conteo para *{label}*? (actual: {current})\n"
        f"Escribe número o 's' para saltar"
    )

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎬 <b>Bot de películas y series</b>\n\n"
        "Comandos:\n"
        "• /pelicula &lt;título o URL TMDb&gt;\n"
        "• /serie &lt;título o URL TMDb&gt;\n"
        "• /editvotos &lt;message_id&gt;\n\n"
        "El bot te mostrará una <b>vista previa</b> con la configuración de votos antes de publicar.",
        parse_mode="HTML",
    )


async def _handle_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    force_tv: bool = False,
) -> None:
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
        full_text  = header + caption

        if details.get("poster_path"):
            await context.bot.send_photo(
                chat_id=update.message.chat_id,
                photo=f"{TMDB_IMAGE_BASE}{details['poster_path']}",
                caption=full_text,
                parse_mode="HTML",
                reply_markup=preview_kb,
            )
        else:
            await update.message.reply_text(
                full_text, parse_mode="HTML", reply_markup=preview_kb
            )

    except requests.HTTPError as exc:
        logger.error("TMDb HTTP error: %s", exc)
        await update.message.reply_text(f"❌ Error al consultar TMDb: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error in search")
        await update.message.reply_text(f"❌ Error inesperado: {exc}")


async def cmd_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_search(update, context, force_tv=False)


async def cmd_serie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_search(update, context, force_tv=True)


async def cmd_editvotos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("⚠️ Uso: /editvotos <message_id>")
        return

    try:
        msg_id  = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ El message_id debe ser un número entero.")
        return

    key_str = str(msg_id)

    # Ensure entry exists in memory (don't save yet)
    if key_str not in vote_counts:
        vote_counts[key_str] = {k: 0 for k in VOTE_KEYS}
        user_votes[key_str]  = {}

    # Show current counts
    lines = [f"✏️ <b>Mensaje {msg_id} — conteos actuales:</b>"]
    for k in VOTE_KEYS:
        lines.append(f"  {VOTE_META[k]}: {vote_counts[key_str].get(k, 0)}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    # Set state
    context.user_data["state"]       = S_EDIT_VOTES
    context.user_data["queue"]       = VOTE_KEYS[:]
    context.user_data["collected"]   = {}
    context.user_data["edit_msg_id"] = msg_id

    first_key = context.user_data["queue"].pop(0)
    current   = vote_counts[key_str].get(first_key, 0)
    await update.message.reply_text(
        _next_edit_question(first_key, current),
        parse_mode="Markdown",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get("state")

    # ── S_PUB_VOTES ─────────────────────────────────────────────────────────
    if state == S_PUB_VOTES:
        text = (update.message.text or "").strip()
        try:
            value = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ Por favor escribe un número entero (ej. 15).")
            return
        if value < 0:
            await update.message.reply_text("⚠️ El número debe ser 0 o mayor.")
            return

        # Store for this key
        queue     = context.user_data["queue"]
        collected = context.user_data["collected"]

        # Determine which key we just answered (the last popped — stored in user_data)
        current_key = context.user_data.get("current_key")
        if current_key:
            collected[current_key] = value

        if queue:
            next_key = queue.pop(0)
            context.user_data["current_key"] = next_key
            await update.message.reply_text(
                _next_vote_question(next_key),
                parse_mode="Markdown",
            )
        else:
            # All targets collected — publish
            temp_id = context.user_data.get("temp_id")
            post    = pending_posts.pop(temp_id, None)
            # Clear state before async work
            context.user_data.clear()

            if not post:
                await update.message.reply_text("⚠️ El post ya no está disponible (puede haber sido cancelado).")
                return

            await do_publish(context, update.effective_user.id, post, collected)
        return

    # ── S_EDIT_VOTES ─────────────────────────────────────────────────────────
    if state == S_EDIT_VOTES:
        text = (update.message.text or "").strip().lower()
        queue     = context.user_data["queue"]
        collected = context.user_data["collected"]
        msg_id    = context.user_data["edit_msg_id"]
        key_str   = str(msg_id)

        current_key = context.user_data.get("current_key")
        if current_key:
            if text == "s":
                pass  # skip — keep current value
            else:
                try:
                    value = int(text)
                except ValueError:
                    await update.message.reply_text(
                        "⚠️ Escribe un número entero o 's' para saltar."
                    )
                    return
                if value < 0:
                    await update.message.reply_text("⚠️ El número debe ser 0 o mayor.")
                    return
                collected[current_key] = value

        if queue:
            next_key = queue.pop(0)
            context.user_data["current_key"] = next_key
            current_val = vote_counts.get(key_str, {}).get(next_key, 0)
            await update.message.reply_text(
                _next_edit_question(next_key, current_val),
                parse_mode="Markdown",
            )
        else:
            # Apply changes
            for k, v in collected.items():
                if key_str not in vote_counts:
                    vote_counts[key_str] = {kk: 0 for kk in VOTE_KEYS}
                vote_counts[key_str][k] = v
            save_data()
            context.user_data.clear()

            # Update keyboard in channel
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=CHANNEL_ID,
                    message_id=msg_id,
                    reply_markup=build_channel_keyboard(msg_id),
                )
                confirm = f"✅ Conteos actualizados para el mensaje {msg_id}."
            except Exception as exc:
                confirm = (
                    f"✅ Conteos guardados, pero no se pudo editar el teclado "
                    f"en el canal: {exc}"
                )
            await update.message.reply_text(confirm)
        return

    # No active state — ignore silently
    return


async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data  = query.data or ""

    # ── tmp placeholder ───────────────────────────────────────────────────────
    if data == "tmp":
        await query.answer()
        return

    # ── Publicar ──────────────────────────────────────────────────────────────
    if data.startswith("pub:"):
        await query.answer()
        temp_id = data[4:]
        post    = pending_posts.get(temp_id)
        if not post:
            try:
                await query.edit_message_caption(
                    caption="⚠️ Ya fue publicado o cancelado.", parse_mode="HTML"
                )
            except Exception:
                pass
            return

        # Start pub_votes flow
        context.user_data["state"]   = S_PUB_VOTES
        context.user_data["queue"]   = VOTE_KEYS[:]
        context.user_data["collected"] = {}
        context.user_data["temp_id"] = temp_id

        first_key = context.user_data["queue"].pop(0)
        context.user_data["current_key"] = first_key

        try:
            await query.edit_message_caption(
                caption="📊 Configurando targets de votos…", parse_mode="HTML"
            )
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=_next_vote_question(first_key),
            parse_mode="Markdown",
        )
        return

    # ── Cancelar ──────────────────────────────────────────────────────────────
    if data.startswith("cancel:"):
        await query.answer()
        temp_id = data[7:]
        pending_posts.pop(temp_id, None)
        try:
            await query.edit_message_caption(
                caption="❌ Publicación cancelada.", parse_mode="HTML"
            )
        except Exception:
            try:
                await query.edit_message_text("❌ Publicación cancelada.")
            except Exception:
                pass
        return

    # ── COMPARTIR ─────────────────────────────────────────────────────────────
    if data.startswith("share:"):
        try:
            msg_id = int(data[6:])
        except ValueError:
            await query.answer()
            return
        key_str = str(msg_id)
        share_counts[key_str] = share_counts.get(key_str, 0) + 1
        save_data()
        url = message_url(msg_id)
        await query.answer(url=url)
        try:
            await query.edit_message_reply_markup(
                reply_markup=build_channel_keyboard(msg_id)
            )
        except Exception:
            pass
        return

    # ── Votos ─────────────────────────────────────────────────────────────────
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

        user_id = str(query.from_user.id)
        key_str = str(msg_id)

        if key_str not in vote_counts:
            vote_counts[key_str] = {k: 0 for k in VOTE_KEYS}
            user_votes[key_str]  = {}

        prev = user_votes[key_str].get(user_id)
        if prev == vote_key:
            # Toggle off
            vote_counts[key_str][vote_key] = max(0, vote_counts[key_str][vote_key] - 1)
            del user_votes[key_str][user_id]
            await query.answer("Voto retirado")
        else:
            if prev:
                vote_counts[key_str][prev] = max(0, vote_counts[key_str][prev] - 1)
            vote_counts[key_str][vote_key] += 1
            user_votes[key_str][user_id] = vote_key
            await query.answer(VOTE_META[vote_key])

        save_data()
        try:
            await query.edit_message_reply_markup(
                reply_markup=build_channel_keyboard(msg_id)
            )
        except Exception:
            pass
        return

    await query.answer()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    load_data()

    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("pelicula",  cmd_pelicula))
    app.add_handler(CommandHandler("serie",     cmd_serie))
    app.add_handler(CommandHandler("editvotos", cmd_editvotos))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot iniciado. Presiona Ctrl+C para detener.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
