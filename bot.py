"""
Bot de Telegram para publicar películas/series en canal.
Cambios v3:
- Botón COMPARTIR como URL button (t.me/share/url)
- Menú /start con inline keyboard visual
- Flujo /configurar para ajustar VOTE_MAX, VOTE_GROW_SECS, VOTE_GROW_HOURS
- Fix: eliminada línea de Estreno duplicada
- Votos iniciales en 0, crecimiento gradual hasta meta
- Persistencia en data/votes.json
- /editvotos <message_id> para editar conteos
"""

import asyncio
import json
import logging
import math
import os
import re
import random
<<<<<<< HEAD
import logging
import requests
from urllib.parse import quote
=======
from datetime import datetime, timedelta
from pathlib import Path
>>>>>>> 75e5ea3 (feat: compartir URL, menu /start, /configurar votos, fix caption)

import httpx
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TMDB_BEARER      = os.environ["TMDB_BEARER"]
CHANNEL_ID       = os.environ["CHANNEL_ID"]
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "")
ADMIN_ID         = int(os.environ["ADMIN_USER_ID"])
SPORTIFI_LINK    = os.environ.get("SPORTIFI_LINK", "https://elsistematv.com/whatsapp")

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG  = "https://image.tmdb.org/t/p/w500"

vote_config = {
    "VOTE_MAX":        200,
    "VOTE_GROW_SECS":  300,
    "VOTE_GROW_HOURS": 24,
}

DATA_DIR   = Path("data")
VOTES_FILE = DATA_DIR / "votes.json"

<<<<<<< HEAD
# ---------------------------------------------------------------------------
# State machine constants
# ---------------------------------------------------------------------------
S_PUB_VOTES   = "pub_votes"
S_EDIT_VOTES  = "edit_votes"
S_VOTE_CONFIG = "vote_config"
S_AWAIT_MOVIE = "await_movie"
S_AWAIT_SERIE = "await_serie"
S_AWAIT_EDIT  = "await_edit"

# — Vote config flow —
VOTE_CONFIG_KEYS = ["VOTE_MAX", "VOTE_GROW_SECS", "VOTE_GROW_HOURS"]
VOTE_CONFIG_QUESTIONS = {
    "VOTE_MAX":        "📊 *Votos máximos* (VOTE\\_MAX)\nActual: `{current}`\nEscribe el nuevo valor:",
    "VOTE_GROW_SECS":  "⏱️ *Segundos entre ciclos* (VOTE\\_GROW\\_SECS)\nActual: `{current}`\nEscribe el nuevo valor:",
    "VOTE_GROW_HOURS": "🕐 *Horas de crecimiento* (VOTE\\_GROW\\_HOURS)\nActual: `{current}`\nEscribe el nuevo valor:",
}
=======
def load_votes() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if VOTES_FILE.exists():
        try:
            return json.loads(VOTES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}
>>>>>>> 75e5ea3 (feat: compartir URL, menu /start, /configurar votos, fix caption)

def save_votes(votes: dict):
    DATA_DIR.mkdir(exist_ok=True)
    VOTES_FILE.write_text(json.dumps(votes, ensure_ascii=False, indent=2), encoding="utf-8")

BUTTON_KEYS   = ["rec", "love", "ok", "skip"]
BUTTON_LABELS = {
    "rec":  "👍 La recomiendo",
    "love": "🥰❤️ Buenísima",
    "ok":   "🤔 Está bien",
    "skip": "😴 No la vi",
}

(
    ST_PREVIEW,
    ST_ASK_REC,
    ST_ASK_LOVE,
    ST_ASK_OK,
    ST_ASK_SKIP,
    ST_EDIT_BUTTON,
    ST_EDIT_VALUE,
    ST_CFG_MAX,
    ST_CFG_SECS,
    ST_CFG_HOURS,
) = range(10)

pending:  dict[int, dict] = {}
edit_ctx: dict[int, dict] = {}

async def tmdb_get(path: str, params: dict = None) -> dict | None:
    headers = {"Authorization": f"Bearer {TMDB_BEARER}", "accept": "application/json"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{TMDB_BASE}{path}", headers=headers, params=params or {})
    return r.json() if r.status_code == 200 else None

async def search_media(query: str) -> dict | None:
    m = re.search(r"themoviedb\.org/(movie|tv)/(\d+)", query)
    if m:
        kind, mid = m.group(1), m.group(2)
        data = await tmdb_get(f"/{kind}/{mid}", {"language": "es-MX"})
        if data:
            data["_kind"] = kind
            return data
    for kind in ("movie", "tv"):
        endpoint = f"/search/{'movie' if kind == 'movie' else 'tv'}"
        res = await tmdb_get(endpoint, {"query": query, "language": "es-MX"})
        if res and res.get("results"):
            detail = await tmdb_get(f"/{kind}/{res['results'][0]['id']}", {"language": "es-MX"})
            if detail:
                detail["_kind"] = kind
                return detail
    return None

async def search_tv_only(query: str) -> dict | None:
    res = await tmdb_get("/search/tv", {"query": query, "language": "es-MX"})
    if res and res.get("results"):
        detail = await tmdb_get(f"/tv/{res['results'][0]['id']}", {"language": "es-MX"})
        if detail:
            detail["_kind"] = "tv"
            return detail
    return None

def stars(rating: float) -> str:
    full = int(rating / 2)
    half = 1 if (rating / 2 - full) >= 0.5 else 0
    return "⭐️" * full + "✨" * half + "☆" * (5 - full - half)

def build_caption(data: dict) -> str:
    kind     = data.get("_kind", "movie")
    is_movie = kind == "movie"
    title    = data.get("title") if is_movie else data.get("name", "")
    year_raw = (data.get("release_date") or data.get("first_air_date") or "")[:4]
    year     = f"<b>{year_raw}</b>" if year_raw else ""
    rating   = data.get("vote_average", 0)
    overview = (data.get("overview") or "Sin descripción disponible.").replace("\n", " ")
    genres   = ", ".join(g["name"] for g in data.get("genres", []))

    if is_movie:
        runtime = data.get("runtime") or 0
        dur_str = f"{runtime // 60}h {runtime % 60}min" if runtime else "—"
    else:
        ep = data.get("number_of_episodes") or "?"
        se = data.get("number_of_seasons") or "?"
        dur_str = f"{se} temporada(s) · {ep} ep."

    ch = CHANNEL_USERNAME if CHANNEL_USERNAME else f"https://t.me/c/{str(CHANNEL_ID).replace('-100','')}"
    title_link = f'<a href="{ch}">{title}</a>'
    genre_link = f'<a href="{ch}">{genres}</a>' if genres else "—"

    lines = [
        f"🎬 <b>{title_link}</b> ({year})",
        "Disponible AHORA! ⚡",
        "",
        f"Calificación de usuarios: {stars(rating)} ({rating:.1f}/10)",
        "",
        f"📖 Resumen: {overview}",
        "",
        f"🎭 Género: {genre_link}",
        f"⏱️ Duración: {dur_str}",
        f"<tg-spoiler>⚽📺 Activa tu servicio con SPORTIFI.tv ⚽📺</tg-spoiler>",
        f"<tg-spoiler>✉️ {SPORTIFI_LINK}</tg-spoiler>",
    ]
    return "\n".join(lines)

def channel_post_url(channel_msg_id: int) -> str:
    if CHANNEL_USERNAME:
        return f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}/{channel_msg_id}"
    cid = str(CHANNEL_ID).replace("-100", "")
    return f"https://t.me/c/{cid}/{channel_msg_id}"

<<<<<<< HEAD

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


def format_date(raw: str) -> str:
    """Convert YYYY-MM-DD to DD/MM/YYYY."""
    try:
        y, m, d = raw.split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return raw


def build_caption(details: dict, media_type: str) -> str:
    title       = details.get("title") or details.get("name") or "Sin título"
    release_raw = details.get("release_date") or details.get("first_air_date") or ""
    year        = release_raw[:4] if release_raw else "N/D"
    date_fmt    = format_date(release_raw) if release_raw else "N/D"
    rating      = details.get("vote_average", 0.0)
    overview    = details.get("overview") or "Sin descripción disponible."
    genres_str  = ", ".join(g["name"] for g in details.get("genres", [])) or "N/D"

    if media_type == "movie":
        duration = minutes_to_duration(details.get("runtime", 0))
    else:
        ep = details.get("episode_run_time", [])
        duration = (minutes_to_duration(ep[0]) + " por episodio") if ep else "N/D"

    title_link = f'<a href="{CHANNEL_LINK}"><b>{title}</b></a>'
    genre_link = f'{genres_str} (<a href="{CHANNEL_LINK}">t.me/ELSISTEMA</a>)'

    return (
        f"🎬 {title_link} <b>({year})</b>\n"
        f"Disponible AHORA! ⚡️\n\n"
        f"🚦 Calificación: {rating:.1f} ⭐️ ({rating:.1f}/10)\n\n"
        f"📖 Resumen: {overview}\n\n"
        f"🎭 Género: {genre_link}\n"
        f"⏱️ Duración: {duration}  |  🚀 <tg-spoiler>@Sportifi_tv</tg-spoiler>"
=======
def build_keyboard(msg_id: str, entry: dict) -> InlineKeyboardMarkup:
    btns           = entry.get("buttons", {})
    channel_msg_id = entry.get("channel_msg_id", int(msg_id))
    post_url       = channel_post_url(channel_msg_id)
    share_url      = (
        f"https://t.me/share/url"
        f"?url={post_url}"
        f"&text=%C2%A1M%C3%ADralo%20en%20%40ELSISTEMA!"
>>>>>>> 75e5ea3 (feat: compartir URL, menu /start, /configurar votos, fix caption)
    )

    row1, row2 = [], []
    for i, key in enumerate(BUTTON_KEYS):
        total = btns.get(key, {}).get("auto", 0) + btns.get(key, {}).get("real", 0)
        label = f"{BUTTON_LABELS[key]}  {total}" if total else BUTTON_LABELS[key]
        btn   = InlineKeyboardButton(label, callback_data=f"vote:{msg_id}:{key}")
        (row1 if i < 2 else row2).append(btn)

    row3 = [InlineKeyboardButton("📤 COMPARTIR", url=share_url)]
    return InlineKeyboardMarkup([row1, row2, row3])

<<<<<<< HEAD
    def btn(key: str) -> InlineKeyboardButton:
        c    = counts.get(key, 0)
        text = f"{VOTE_META[key]}  {c}" if c > 0 else VOTE_META[key]
        return InlineKeyboardButton(text, callback_data=f"{key}:{message_id}")

    share_label = f"📤  COMPARTIR  {shares}" if shares > 0 else "📤  COMPARTIR"
    msg_url = message_url(message_id)
    share_url = (
        f"https://t.me/share/url"
        f"?url={quote(msg_url, safe='')}"
        f"&text={quote('¡Míralo en @ELSISTEMA!', safe='')}"
    )

    return InlineKeyboardMarkup([
        [btn("vote_recommend"), btn("vote_great")],
        [btn("vote_not_seen"),  btn("vote_better")],
        [InlineKeyboardButton(share_label, url=share_url)],
=======
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬➕ Publicar Película",     callback_data="menu:pelicula")],
        [InlineKeyboardButton("📺➕ Publicar Serie",        callback_data="menu:serie")],
        [InlineKeyboardButton("✏️📊 Editar Post",          callback_data="menu:editar")],
        [InlineKeyboardButton("⚙️ Configuración de Votos", callback_data="menu:config")],
>>>>>>> 75e5ea3 (feat: compartir URL, menu /start, /configurar votos, fix caption)
    ])
    await update.message.reply_text("👋 ¡Hola! ¿Qué quieres hacer?", reply_markup=kb)

async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]
    if action == "pelicula":
        await query.edit_message_text("Escribe: /pelicula <título o URL de TMDb>")
    elif action == "serie":
        await query.edit_message_text("Escribe: /serie <título>")
    elif action == "editar":
        await query.edit_message_text("Escribe: /editvotos <message_id>")
    elif action == "config":
        await query.edit_message_text(
            "⚙️ <b>Configuración actual:</b>\n"
            f"• VOTE_MAX: <code>{vote_config['VOTE_MAX']}</code>\n"
            f"• VOTE_GROW_SECS: <code>{vote_config['VOTE_GROW_SECS']}</code>\n"
            f"• VOTE_GROW_HOURS: <code>{vote_config['VOTE_GROW_HOURS']}</code>\n\n"
            "Usa /configurar para cambiarlos.",
            parse_mode=ParseMode.HTML,
        )

async def cmd_configurar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        f"⚙️ <b>VOTE_MAX</b> — techo de votos automáticos por botón\n"
        f"Actual: <code>{vote_config['VOTE_MAX']}</code>\n\n"
        "Escribe el nuevo valor (0 = sin cambio):",
        parse_mode=ParseMode.HTML,
    )
    return ST_CFG_MAX

async def cfg_ask_secs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        val = int(update.message.text.strip())
        if val > 0:
            vote_config["VOTE_MAX"] = val
    except ValueError:
        await update.message.reply_text("Solo números.")
        return ST_CFG_MAX
    await update.message.reply_text(
        f"⚙️ <b>VOTE_GROW_SECS</b> — segundos entre cada ciclo de crecimiento\n"
        f"Actual: <code>{vote_config['VOTE_GROW_SECS']}</code>\n\nNuevo valor (0 = sin cambio):",
        parse_mode=ParseMode.HTML,
    )
    return ST_CFG_SECS

async def cfg_ask_hours(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        val = int(update.message.text.strip())
        if val > 0:
            vote_config["VOTE_GROW_SECS"] = val
    except ValueError:
        await update.message.reply_text("Solo números.")
        return ST_CFG_SECS
    await update.message.reply_text(
        f"⚙️ <b>VOTE_GROW_HOURS</b> — horas que dura el crecimiento automático\n"
        f"Actual: <code>{vote_config['VOTE_GROW_HOURS']}</code>\n\nNuevo valor (0 = sin cambio):",
        parse_mode=ParseMode.HTML,
    )
    return ST_CFG_HOURS

async def cfg_finish(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        val = int(update.message.text.strip())
        if val > 0:
            vote_config["VOTE_GROW_HOURS"] = val
    except ValueError:
        await update.message.reply_text("Solo números.")
        return ST_CFG_HOURS
    await update.message.reply_text(
        f"✅ <b>Guardado:</b>\n"
        f"• VOTE_MAX: <code>{vote_config['VOTE_MAX']}</code>\n"
        f"• VOTE_GROW_SECS: <code>{vote_config['VOTE_GROW_SECS']}</code>\n"
        f"• VOTE_GROW_HOURS: <code>{vote_config['VOTE_GROW_HOURS']}</code>",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END

async def _send_preview(update: Update, data: dict) -> int:
    caption = build_caption(data)
    poster  = data.get("poster_path")
    img_url = f"{TMDB_IMG}{poster}" if poster else None
    confirm_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Publicar en el canal", callback_data="confirm:yes"),
        InlineKeyboardButton("❌ Cancelar",             callback_data="confirm:no"),
    ]])
    if img_url:
        preview = await update.message.reply_photo(
            img_url, caption=caption, parse_mode=ParseMode.HTML, reply_markup=confirm_kb
        )
    else:
        preview = await update.message.reply_text(
            caption, parse_mode=ParseMode.HTML, reply_markup=confirm_kb
        )
    pending[ADMIN_ID] = {"caption": caption, "img_url": img_url,
                         "preview_msg_id": preview.message_id, "targets": {}}
    return ST_PREVIEW

async def cmd_pelicula(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = " ".join(ctx.args).strip()
    if not args:
        await update.message.reply_text("Uso: /pelicula <título o URL de TMDb>")
        return
    msg  = await update.message.reply_text("🔍 Buscando…")
    data = await search_media(args)
    await msg.delete()
    if not data:
        await update.message.reply_text("❌ No encontré nada.")
        return
    return await _send_preview(update, data)

async def cmd_serie(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = " ".join(ctx.args).strip()
    if not args:
        await update.message.reply_text("Uso: /serie <título>")
        return
    msg  = await update.message.reply_text("🔍 Buscando serie…")
    data = await search_tv_only(args)
    await msg.delete()
    if not data:
        await update.message.reply_text("❌ No encontré esa serie.")
        return
    return await _send_preview(update, data)

async def on_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.split(":")[1] == "no":
        pending.pop(ADMIN_ID, None)
        await query.edit_message_reply_markup(None)
        await query.message.reply_text("❌ Cancelado.")
        return ConversationHandler.END
    await query.edit_message_reply_markup(None)
    await query.message.reply_text(
        f'¿Meta de votos para "{BUTTON_LABELS["rec"]}" en {vote_config["VOTE_GROW_HOURS"]}h?\n'
        "(0 = no crece automáticamente)"
    )
    return ST_ASK_REC

async def ask_love(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        pending[ADMIN_ID]["targets"]["rec"] = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Solo números.")
        return ST_ASK_REC
    await update.message.reply_text(f'¿Meta para "{BUTTON_LABELS["love"]}"?')
    return ST_ASK_LOVE

async def ask_ok(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        pending[ADMIN_ID]["targets"]["love"] = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Solo números.")
        return ST_ASK_LOVE
    await update.message.reply_text(f'¿Meta para "{BUTTON_LABELS["ok"]}"?')
    return ST_ASK_OK

async def ask_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        pending[ADMIN_ID]["targets"]["ok"] = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Solo números.")
        return ST_ASK_OK
    await update.message.reply_text(f'¿Meta para "{BUTTON_LABELS["skip"]}"?')
    return ST_ASK_SKIP

async def do_publish(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        pending[ADMIN_ID]["targets"]["skip"] = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Solo números.")
        return ST_ASK_SKIP

    info = pending.pop(ADMIN_ID, None)
    if not info:
        await update.message.reply_text("❌ No hay publicación pendiente.")
        return ConversationHandler.END

    caption    = info["caption"]
    img_url    = info["img_url"]
    targets    = info["targets"]
    grow_until = (datetime.utcnow() + timedelta(hours=vote_config["VOTE_GROW_HOURS"])).isoformat()

    tmp_kb = InlineKeyboardMarkup([[]])
    if img_url:
        sent = await ctx.bot.send_photo(CHANNEL_ID, img_url, caption=caption,
                                        parse_mode=ParseMode.HTML, reply_markup=tmp_kb)
    else:
        sent = await ctx.bot.send_message(CHANNEL_ID, caption,
                                          parse_mode=ParseMode.HTML, reply_markup=tmp_kb)

    msg_id = str(sent.message_id)
    votes  = load_votes()
    votes[msg_id] = {
        "buttons": {
            key: {
                "auto": 0, "real": 0,
                "target": min(targets.get(key, 0), vote_config["VOTE_MAX"]),
                "grow_until": grow_until,
            }
            for key in BUTTON_KEYS
        },
        "share": {"auto": 0, "real": 0},
        "user_votes": {},
        "channel_msg_id": sent.message_id,
        "published_at": datetime.utcnow().isoformat(),
    }
    save_votes(votes)

    kb = build_keyboard(msg_id, votes[msg_id])
    await ctx.bot.edit_message_reply_markup(CHANNEL_ID, sent.message_id, reply_markup=kb)
    ctx.application.create_task(grow_votes_task(msg_id, ctx.application.bot))

    lines = [f"✅ Publicado. ID: <code>{msg_id}</code>\n", "Metas:"]
    for key in BUTTON_KEYS:
        lines.append(f"  • {BUTTON_LABELS[key]}: {targets.get(key, 0)} votos en {vote_config['VOTE_GROW_HOURS']}h")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    return ConversationHandler.END

async def grow_votes_task(msg_id: str, bot):
    interval = vote_config["VOTE_GROW_SECS"]
    while True:
        await asyncio.sleep(interval)
        votes = load_votes()
        entry = votes.get(msg_id)
        if not entry:
            break

        now     = datetime.utcnow()
        changed = False

        for key in BUTTON_KEYS:
            bdata      = entry["buttons"].get(key, {})
            target     = bdata.get("target", 0)
            auto_now   = bdata.get("auto", 0)
            grow_until = bdata.get("grow_until", "")
            if auto_now >= target or not grow_until:
                continue
            if now > datetime.fromisoformat(grow_until):
                continue
            remaining   = target - auto_now
            cycles_left = max(1, int((datetime.fromisoformat(grow_until) - now).total_seconds() / interval))
            add = max(1, math.ceil(remaining / cycles_left))
            add = max(1, add + random.randint(-1, 1))
            add = min(add, remaining)
            entry["buttons"][key]["auto"] = auto_now + add
            changed = True

        if changed:
            votes[msg_id] = entry
            save_votes(votes)
            kb = build_keyboard(msg_id, entry)
            try:
                await bot.edit_message_reply_markup(
                    CHANNEL_ID, entry["channel_msg_id"], reply_markup=kb
                )
            except Exception as e:
                logger.warning(f"No se pudo editar teclado {msg_id}: {e}")

        if all(entry["buttons"][k].get("auto", 0) >= entry["buttons"][k].get("target", 0)
               for k in BUTTON_KEYS):
            break

async def on_vote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    _, msg_id, key = query.data.split(":")
    user_id = str(query.from_user.id)

    votes = load_votes()
    entry = votes.get(msg_id)
    if not entry:
        await query.answer("Post sin registro.", show_alert=True)
        return

    prev = entry["user_votes"].get(user_id)
    if prev == key:
        entry["user_votes"].pop(user_id)
        entry["buttons"][key]["real"] = max(0, entry["buttons"][key]["real"] - 1)
        await query.answer("Retiraste tu reacción.")
    elif prev:
        entry["buttons"][prev]["real"] = max(0, entry["buttons"][prev]["real"] - 1)
        entry["buttons"][key]["real"] += 1
        entry["user_votes"][user_id] = key
        await query.answer(f"Cambiaste a: {BUTTON_LABELS[key]}")
    else:
        entry["buttons"][key]["real"] += 1
        entry["user_votes"][user_id] = key
        await query.answer(f"Reaccionaste con: {BUTTON_LABELS[key]}")

    votes[msg_id] = entry
    save_votes(votes)
    await query.edit_message_reply_markup(reply_markup=build_keyboard(msg_id, entry))

async def cmd_editvotos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Uso: /editvotos <message_id>")
        return

    msg_id = ctx.args[0]
    votes  = load_votes()
    entry  = votes.get(msg_id)
    if not entry:
        await update.message.reply_text(
            f"⚠️ No encontré el post <code>{msg_id}</code>.\n"
            f"Usa /initpost {msg_id} si el bot se reinició.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [f"📊 Post <code>{msg_id}</code>:\n"]
    for key in BUTTON_KEYS:
        b = entry["buttons"].get(key, {})
        lines.append(f"  [{key}] {BUTTON_LABELS[key]}: {b.get('auto',0)+b.get('real',0)}")
    lines.append("\n¿Qué botón editar? Escribe: rec | love | ok | skip")
    edit_ctx[ADMIN_ID] = {"msg_id": msg_id}
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    return ST_EDIT_BUTTON

<<<<<<< HEAD
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
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬➕ Publicar Película",      callback_data="start_pub_movie")],
        [InlineKeyboardButton("📺➕ Publicar Serie",         callback_data="start_pub_serie")],
        [InlineKeyboardButton("✏️📊 Editar Post",            callback_data="start_edit")],
        [InlineKeyboardButton("⚙️ Configuración de Votos",  callback_data="start_config")],
    ])
    await update.message.reply_text(
        "🎬 <b>Bot de películas y series</b>\n\nElige una opción:",
        parse_mode="HTML",
        reply_markup=kb,
=======
async def edit_ask_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = update.message.text.strip().lower()
    if key not in BUTTON_KEYS:
        await update.message.reply_text(f"Clave inválida. Elige: {' | '.join(BUTTON_KEYS)}")
        return ST_EDIT_BUTTON
    edit_ctx[ADMIN_ID]["key"] = key
    entry = load_votes().get(edit_ctx[ADMIN_ID]["msg_id"], {})
    bdata = entry.get("buttons", {}).get(key, {})
    total = bdata.get("auto", 0) + bdata.get("real", 0)
    await update.message.reply_text(
        f"{BUTTON_LABELS[key]}\nTotal actual: {total}\n\nEscribe el nuevo total:"
>>>>>>> 75e5ea3 (feat: compartir URL, menu /start, /configurar votos, fix caption)
    )
    return ST_EDIT_VALUE

<<<<<<< HEAD

async def _handle_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    force_tv: bool = False,
    query_override: str | None = None,
) -> None:
    if query_override is not None:
        query = query_override
    elif not context.args:
        cmd = "serie" if force_tv else "pelicula"
        await update.message.reply_text(f"⚠️ Uso: /{cmd} <título o URL de TMDb>")
        return
    else:
        query = " ".join(context.args).strip()
    await update.message.reply_text("🔍 Buscando información, un momento…")

=======
async def edit_apply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
>>>>>>> 75e5ea3 (feat: compartir URL, menu /start, /configurar votos, fix caption)
    try:
        new_total = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Solo números.")
        return ST_EDIT_VALUE

    ec    = edit_ctx.pop(ADMIN_ID, None)
    if not ec:
        return ConversationHandler.END
    votes = load_votes()
    entry = votes.get(ec["msg_id"])
    if not entry:
        await update.message.reply_text("Error: post no encontrado.")
        return ConversationHandler.END

    entry["buttons"][ec["key"]]["auto"] = max(0, new_total)
    entry["buttons"][ec["key"]]["real"] = 0
    votes[ec["msg_id"]] = entry
    save_votes(votes)

    kb = build_keyboard(ec["msg_id"], entry)
    try:
        await ctx.bot.edit_message_reply_markup(
            CHANNEL_ID, entry["channel_msg_id"], reply_markup=kb
        )
        await update.message.reply_text(f"✅ {BUTTON_LABELS[ec['key']]}: {new_total}")
    except Exception as e:
        await update.message.reply_text(f"✅ Guardado, pero no pude editar el mensaje: {e}")
    return ConversationHandler.END

async def cmd_initpost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Uso: /initpost <message_id>")
        return
    msg_id = ctx.args[0]
    votes  = load_votes()
    if msg_id in votes:
        await update.message.reply_text("Ya existe. Usa /editvotos.")
        return
    votes[msg_id] = {
        "buttons": {
            key: {"auto": 0, "real": 0, "target": 0, "grow_until": datetime.utcnow().isoformat()}
            for key in BUTTON_KEYS
        },
        "share": {"auto": 0, "real": 0},
        "user_votes": {},
        "channel_msg_id": int(msg_id),
        "published_at": datetime.utcnow().isoformat(),
    }
    save_votes(votes)
    await update.message.reply_text(f"✅ Post {msg_id} iniciado. Usa /editvotos {msg_id}.")

async def resume_grow_tasks(app):
    votes = load_votes()
    now   = datetime.utcnow()
    for msg_id, entry in votes.items():
        needs = any(
            entry["buttons"].get(k, {}).get("auto", 0) < entry["buttons"].get(k, {}).get("target", 0)
            and now < datetime.fromisoformat(entry["buttons"].get(k, {}).get("grow_until", now.isoformat()))
            for k in BUTTON_KEYS
        )
        if needs:
            logger.info(f"Reanudando crecimiento msg_id={msg_id}")
            app.create_task(grow_votes_task(msg_id, app.bot))

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    publish_conv = ConversationHandler(
        entry_points=[
            CommandHandler("pelicula", cmd_pelicula),
            CommandHandler("serie",    cmd_serie),
        ],
        states={
            ST_PREVIEW:  [CallbackQueryHandler(on_confirm, pattern="^confirm:")],
            ST_ASK_REC:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_love)],
            ST_ASK_LOVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_ok)],
            ST_ASK_OK:   [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_skip)],
            ST_ASK_SKIP: [MessageHandler(filters.TEXT & ~filters.COMMAND, do_publish)],
        },
        fallbacks=[],
        per_user=True,
        per_chat=True,
    )

    edit_conv = ConversationHandler(
        entry_points=[CommandHandler("editvotos", cmd_editvotos)],
        states={
            ST_EDIT_BUTTON: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_ask_value)],
            ST_EDIT_VALUE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_apply)],
        },
        fallbacks=[],
        per_user=True,
        per_chat=True,
    )

    config_conv = ConversationHandler(
        entry_points=[CommandHandler("configurar", cmd_configurar)],
        states={
            ST_CFG_MAX:   [MessageHandler(filters.TEXT & ~filters.COMMAND, cfg_ask_secs)],
            ST_CFG_SECS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, cfg_ask_hours)],
            ST_CFG_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, cfg_finish)],
        },
        fallbacks=[],
        per_user=True,
        per_chat=True,
    )

<<<<<<< HEAD
    # ── S_AWAIT_MOVIE ────────────────────────────────────────────────────────
    if state == S_AWAIT_MOVIE:
        context.user_data.clear()
        await _handle_search(update, context, force_tv=False, query_override=(update.message.text or "").strip())
        return

    # ── S_AWAIT_SERIE ────────────────────────────────────────────────────────
    if state == S_AWAIT_SERIE:
        context.user_data.clear()
        await _handle_search(update, context, force_tv=True, query_override=(update.message.text or "").strip())
        return

    # ── S_AWAIT_EDIT ─────────────────────────────────────────────────────────
    if state == S_AWAIT_EDIT:
        text = (update.message.text or "").strip()
        try:
            msg_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ El message_id debe ser un número entero.")
            return
        context.user_data.clear()
        # Delegate to editvotos logic inline
        key_str = str(msg_id)
        if key_str not in vote_counts:
            vote_counts[key_str] = {k: 0 for k in VOTE_KEYS}
            user_votes[key_str]  = {}
        lines = [f"✏️ <b>Mensaje {msg_id} — conteos actuales:</b>"]
        for k in VOTE_KEYS:
            lines.append(f"  {VOTE_META[k]}: {vote_counts[key_str].get(k, 0)}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
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
        return

    # ── S_VOTE_CONFIG ────────────────────────────────────────────────────────
    if state == S_VOTE_CONFIG:
        global VOTE_MAX, VOTE_GROW_SECS, VOTE_GROW_HOURS
        text = (update.message.text or "").strip()
        try:
            value = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ Por favor escribe un número entero.")
            return
        if value <= 0:
            await update.message.reply_text("⚠️ El número debe ser mayor que 0.")
            return

        queue       = context.user_data["queue"]
        collected   = context.user_data["collected"]
        current_key = context.user_data.get("current_key")
        if current_key:
            collected[current_key] = value

        if queue:
            next_key = queue.pop(0)
            context.user_data["current_key"] = next_key
            current_val = {"VOTE_MAX": VOTE_MAX, "VOTE_GROW_SECS": VOTE_GROW_SECS, "VOTE_GROW_HOURS": VOTE_GROW_HOURS}[next_key]
            await update.message.reply_text(
                VOTE_CONFIG_QUESTIONS[next_key].format(current=current_val),
                parse_mode="Markdown",
            )
        else:
            # Apply collected values
            if "VOTE_MAX" in collected:
                VOTE_MAX = collected["VOTE_MAX"]
            if "VOTE_GROW_SECS" in collected:
                VOTE_GROW_SECS = collected["VOTE_GROW_SECS"]
            if "VOTE_GROW_HOURS" in collected:
                VOTE_GROW_HOURS = collected["VOTE_GROW_HOURS"]
            context.user_data.clear()
            await update.message.reply_text(
                f"✅ <b>Configuración guardada</b>\n"
                f"• VOTE_MAX: <code>{VOTE_MAX}</code>\n"
                f"• VOTE_GROW_SECS: <code>{VOTE_GROW_SECS}</code>\n"
                f"• VOTE_GROW_HOURS: <code>{VOTE_GROW_HOURS}</code>\n\n"
                f"Se aplicará a los próximos posts publicados.",
                parse_mode="HTML",
            )
        return

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

    # ── Menú /start ───────────────────────────────────────────────────────────
    if data == "start_pub_movie":
        await query.answer()
        context.user_data["state"] = S_AWAIT_MOVIE
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="🎬 Envía el <b>título</b> o la <b>URL de TMDb</b> de la película:",
            parse_mode="HTML",
        )
        return

    if data == "start_pub_serie":
        await query.answer()
        context.user_data["state"] = S_AWAIT_SERIE
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="📺 Envía el <b>título</b> o la <b>URL de TMDb</b> de la serie:",
            parse_mode="HTML",
        )
        return

    if data == "start_edit":
        await query.answer()
        context.user_data["state"] = S_AWAIT_EDIT
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="✏️ Envía el <b>message_id</b> del post que quieres editar:",
            parse_mode="HTML",
        )
        return

    if data == "start_config":
        await query.answer()
        context.user_data["state"]       = S_VOTE_CONFIG
        context.user_data["queue"]       = VOTE_CONFIG_KEYS[1:]   # VOTE_GROW_SECS, VOTE_GROW_HOURS
        context.user_data["collected"]   = {}
        context.user_data["current_key"] = VOTE_CONFIG_KEYS[0]     # VOTE_MAX
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=VOTE_CONFIG_QUESTIONS["VOTE_MAX"].format(current=VOTE_MAX),
            parse_mode="Markdown",
        )
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
=======
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("initpost", cmd_initpost))
    app.add_handler(publish_conv)
    app.add_handler(edit_conv)
    app.add_handler(config_conv)
    app.add_handler(CallbackQueryHandler(on_menu, pattern="^menu:"))
    app.add_handler(CallbackQueryHandler(on_vote, pattern="^vote:"))

    app.post_init = resume_grow_tasks
>>>>>>> 75e5ea3 (feat: compartir URL, menu /start, /configurar votos, fix caption)

    logger.info("Bot iniciado. Presiona Ctrl+C para detener.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
