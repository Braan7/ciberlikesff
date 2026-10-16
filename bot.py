import os
import time
import hashlib
import html
import math
import secrets
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, TypeHandler, filters)

logging.basicConfig(level=logging.INFO)

# ================== CONFIG ==================
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_KEY = os.environ["LIKES_API_KEY"]
BASE = "https://lunexislikes.squareweb.app"
HEADERS = {"X-Api-Key": API_KEY}

DEFAULT_AMOUNT = 500      # likes por defecto en /likes
MAX_AMOUNT = 2000         # máximo por envío (límite de la API)
FREE_DAILY = 1            # usos por día en modo gratis
FREE_MAX_AMOUNT = 100     # máximo de likes por envío en modo gratis
AUTO_AMOUNT = 500         # likes diarios de cada /autoday

TZ = timezone(timedelta(hours=-6))  # hora de México (reinicio a las 00:00)
DB_PATH = os.environ.get("DB_PATH", "ciberbot.db")

PHONE = "+57 318 0051415"
OWNER = PHONE  # solo se muestra el número de contacto
CONTACTO = "+57ㅤ31800ㅤ51415"   # formato de la plantilla de likes enviados
CANAL_ID = int(os.environ.get("CANAL_ID", "-1004299785475"))  # canal de referencias
PRUEBA_LIKES = 100        # likes de la prueba gratuita (una vez por usuario y por ID de Free Fire)

# ----- Método de pago: transferencia bancaria México -----
PAGO_NOMBRE = "Francisco Menesses"
PAGO_CLABE = "661180006413009414"
PAGO_BANCO = "KLAR"
GRUPO_PAGOS_ID = int(os.environ.get("GRUPO_PAGOS_ID", "-1004305752572"))  # grupo donde llegan los comprobantes
LINK_REFERENCIAS = "https://whatsapp.com/channel/0029Vb7uTj165yDEGRqavR14"

# ----- Precios de likes: {likes: (USD, MXN)}. Se paga por transferencia en MXN -----
PRECIOS_LIKES = {
    2000: (6, 110), 4000: (11, 205), 6000: (16, 295), 8000: (20, 370), 10000: (24, 445),
    20000: (45, 835), 40000: (84, 1550), 60000: (119, 2200), 80000: (153, 2830),
    100000: (184, 3410), 200000: (353, 6530), 400000: (660, 12200), 600000: (945, 17500),
    800000: (1200, 22200), 1000000: (1425, 26400),
}
PAQUETES = list(PRECIOS_LIKES)
CRAFT_PRECIO_MXN = 300    # precio del Nick Color Craftland (pago por transferencia)
PEDIDO_GRANDE = 10000     # desde esta cantidad de likes se ofrece precio especial / revendedores

# ----- Nick Color Craftland (API del proveedor de Craftland) -----
CRAFT_BASE = "https://lunexiscraft.squareweb.app"
CRAFT_KEY = os.environ.get("CRAFT_API_KEY", "")   # la key va en variable de entorno, nunca en el código
CRAFT_SEGUIDORES = 65000                         # el pack nivel 6 entrega 65,000 seguidores

CONFIRMAR_DESDE = 1000    # envíos de esta cantidad o más piden confirmación al cliente
LOW_CREDITS = 200         # aviso de "pocos créditos" al cliente
REF_BONUS = 100           # créditos que gana quien invita, cuando el invitado hace su 1ª recarga
AUTO_MAX_DIAS = 30        # máximo de días de un /auto de cliente
ALERTA_SALDO_API = 5000   # te avisa si el saldo de la API baja de esta cantidad
BRAND = "🔥 𝗟𝗜𝗞𝗘𝗦 𝗙𝗙 - 𝗖𝗜𝗕𝗘𝗥𝗦𝗧𝗢𝗥𝗘 🔥"

# IDs de Telegram de los admins (el mismo ALLOWED_IDS de antes)
ADMINS = [int(x) for x in os.environ.get("ALLOWED_IDS", "").split(",") if x.strip()]

# ================== BASE DE DATOS ==================
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY, username TEXT,
    credits INTEGER DEFAULT 0, uses INTEGER DEFAULT 0, uses_date TEXT);
CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER,
    target_id TEXT, nickname TEXT, likes INTEGER, status TEXT, origin TEXT);
CREATE TABLE IF NOT EXISTS autos(
    target_id TEXT PRIMARY KEY, expires TEXT, last_sent TEXT, added_by INTEGER, amount INTEGER);
""")
# Migración: si ya tenías una base anterior sin la columna de créditos
try:
    db.execute("ALTER TABLE users ADD COLUMN credits INTEGER DEFAULT 0")
    db.commit()
except sqlite3.OperationalError:
    pass
try:
    db.execute("ALTER TABLE autos ADD COLUMN amount INTEGER")
    db.commit()
except sqlite3.OperationalError:
    pass

db.executescript("""
CREATE TABLE IF NOT EXISTS movimientos(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER,
    delta INTEGER, tipo TEXT, nota TEXT, saldo INTEGER);
CREATE TABLE IF NOT EXISTS codigos(
    code TEXT PRIMARY KEY, credits INTEGER, created_by INTEGER, created TEXT,
    used_by INTEGER, used_at TEXT);
CREATE TABLE IF NOT EXISTS pruebas(target_id TEXT PRIMARY KEY, user_id INTEGER, ts TEXT);
CREATE TABLE IF NOT EXISTS ordenes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER, likes INTEGER, monto REAL,
    estado TEXT, file_id TEXT, kind TEXT, decidido_por INTEGER, decidido_ts TEXT);
CREATE TABLE IF NOT EXISTS craft_pedidos(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER, chat_id INTEGER,
    player_id TEXT, creditos INTEGER, cobrado INTEGER, code TEXT, estado TEXT,
    f_antes INTEGER, f_despues INTEGER, alertado INTEGER DEFAULT 0);
""")
for _tabla, _col in [
    ("users", "banned INTEGER DEFAULT 0"), ("users", "ref_by INTEGER"),
    ("users", "ref_paid INTEGER DEFAULT 0"), ("users", "created TEXT"),
    ("users", "prueba_usada INTEGER DEFAULT 0"), ("autos", "owner_id INTEGER"), ("autos", "restantes INTEGER"),
    ("ordenes", "producto TEXT"), ("ordenes", "player_id TEXT"), ("craft_pedidos", "orden_id INTEGER"),
]:
    try:
        db.execute(f"ALTER TABLE {_tabla} ADD COLUMN {_col}")
        db.commit()
    except sqlite3.OperationalError:
        pass


def now():
    return datetime.now(TZ)


def hoy():
    return now().date().isoformat()


def cfg(key, default="0"):
    r = db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_cfg(key, val):
    db.execute(
        "INSERT INTO config(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(val)))
    db.commit()


def get_user(uid):
    return db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()


def creditos(uid):
    r = get_user(uid)
    return (r["credits"] or 0) if r else 0


def mov(uid, delta, tipo, nota=""):
    """Guarda un movimiento de créditos en el historial."""
    if not delta:
        return
    db.execute(
        "INSERT INTO movimientos(ts,user_id,delta,tipo,nota,saldo) VALUES(?,?,?,?,?,?)",
        (now().isoformat(timespec="seconds"), uid, delta, tipo, nota, creditos(uid)))
    db.commit()


def cobrar(uid, n, nota="", tipo="envio"):
    """Descuenta n créditos si alcanzan. Devuelve True/False."""
    cur = db.execute(
        "UPDATE users SET credits=credits-? WHERE user_id=? AND credits>=?", (n, uid, n))
    db.commit()
    if cur.rowcount == 1:
        mov(uid, -n, tipo, nota)
        return True
    return False


def devolver(uid, n, nota=""):
    if n > 0:
        db.execute("UPDATE users SET credits=credits+? WHERE user_id=?", (n, uid))
        db.commit()
        mov(uid, n, "devolucion", nota)


def acreditar(uid, n, tipo, nota=""):
    """Suma créditos comprados. Si el usuario fue invitado y es su primera recarga,
    paga el bono a quien lo invitó y devuelve el ID de ese usuario (o None)."""
    db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
    db.execute("UPDATE users SET credits=COALESCE(credits,0)+? WHERE user_id=?", (n, uid))
    db.commit()
    mov(uid, n, tipo, nota)
    r = get_user(uid)
    if tipo in ("recarga", "codigo") and r["ref_by"] and not r["ref_paid"]:
        db.execute("UPDATE users SET ref_paid=1 WHERE user_id=?", (uid,))
        db.execute("UPDATE users SET credits=COALESCE(credits,0)+? WHERE user_id=?",
                   (REF_BONUS, r["ref_by"]))
        db.commit()
        mov(r["ref_by"], REF_BONUS, "referido", f"primera recarga de {uid}")
        return r["ref_by"]
    return None


ETIQ = {"recarga": "Recarga", "codigo": "Código", "envio": "Envío", "devolucion": "Devolución",
        "ajuste": "Ajuste", "referido": "Bono referido", "auto": "Auto", "craft": "Craftland"}


def fmt_mov(r):
    signo = "➕" if r["delta"] > 0 else "➖"
    return (f"{signo} {abs(r['delta'])} • {ETIQ.get(r['tipo'], r['tipo'])} • "
            f"{r['ts'][5:16].replace('T', ' ')} • saldo {r['saldo']}")


def usos_hoy(r):
    return r["uses"] if r and r["uses_date"] == hoy() else 0


def sumar_uso(uid):
    n = usos_hoy(get_user(uid)) + 1
    db.execute("UPDATE users SET uses=?, uses_date=? WHERE user_id=?", (n, hoy(), uid))
    db.commit()


def log_send(uid, target, nick, likes, status, origin):
    db.execute(
        "INSERT INTO logs(ts,user_id,target_id,nickname,likes,status,origin) "
        "VALUES(?,?,?,?,?,?,?)",
        (now().isoformat(timespec="seconds"), uid, target, nick, likes, status, origin))
    db.commit()


def resolver(arg):
    """Acepta ID numérico o @usuario (debe haber escrito /start al bot)."""
    if arg.isdigit():
        return int(arg)
    r = db.execute("SELECT user_id FROM users WHERE username=?",
                   (arg.lstrip("@").lower(),)).fetchone()
    return r["user_id"] if r else None


def resta_reinicio():
    n = now()
    prox = (n + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    s = int((prox - n).total_seconds())
    return f"{s // 3600}h {(s % 3600) // 60}m"


# ================== API ==================
async def api(method, path, **kw):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.request(method, BASE + path, headers=HEADERS, **kw)
    try:
        data = r.json()
    except Exception:
        data = {"error": "Respuesta inválida"}
    return r.status_code, data


def error_txt(code, data):
    if code == 429:
        return "⏳ Demasiadas solicitudes, espera un minuto."
    if code == 503:
        return "🛠 El servicio está en mantenimiento, intenta más tarde."
    if code in (401, 403):
        return "🔑 API Key inválida o cuenta desactivada."
    msg = data.get("error", "Error desconocido") if isinstance(data, dict) else "Error"
    return f"⚠️ {msg}"


async def info_jugador(target_id):
    """Devuelve (región, likes actuales) del jugador, o (None, None) si falla."""
    try:
        pc, pd = await api("GET", f"/api/player/{target_id}")
        if pc == 200:
            b = pd.get("basicInfo", {})
            return b.get("region"), b.get("liked")
    except Exception:
        pass
    return None, None


async def enviar(target_id, amount):
    region, liked_pre = await info_jugador(target_id)
    region = region or "N/D"

    t0 = time.perf_counter()
    code, d = await api("POST", "/api/likes/send",
                        json={"target_id": target_id, "amount": amount})
    secs = time.perf_counter() - t0

    if code == 200:
        enviados = d.get("likes_sent", 0) or 0
        antes = liked_pre if liked_pre is not None else d.get("liked_before")
        despues = antes
        if antes is not None and enviados > 0:
            despues = None
            # El contador del juego tarda en actualizarse: se reintenta unos segundos
            for _ in range(2):
                await asyncio.sleep(4)
                _, actual = await info_jugador(target_id)
                if actual is not None and actual > antes:
                    despues = actual
                    break
            if despues is None:
                despues = f"≈{antes + enviados}"   # aún no se refleja: valor estimado
        d["liked_before"] = antes
        d["liked_after"] = despues
    return code, d, region, secs


def ficha(d, target_id, region, secs):
    antes = d.get("liked_before")
    despues = d.get("liked_after")
    antes = "N/D" if antes is None else antes
    despues = "N/D" if despues is None else despues
    return (
        "🤖 LIKES ENVIADOS CON ÉXITO!\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"↬ Jugador: {d.get('player_nickname')}\n"
        f"↬ ID: {target_id}\n"
        f"↬ Región: {region}\n"
        f"↬ Tiempo: {secs:.1f}s\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"↬ Likes antes: {antes}\n"
        f"↬ Likes después: {despues}\n"
        f"↬ Total agregado: +{d.get('likes_sent', 0)}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"↬ Contacto: {CONTACTO}"
    )


_canal_nombre = None


async def nombre_canal(bot):
    """Nombre del canal de referencias (se consulta una vez y se guarda)."""
    global _canal_nombre
    if _canal_nombre is None:
        try:
            ch = await bot.get_chat(CANAL_ID)
            _canal_nombre = ch.title or (f"@{ch.username}" if ch.username else "el canal")
        except Exception:
            return "el canal"
    return _canal_nombre


async def enviar_referencia(bot, texto):
    """Publica la referencia en el canal. Devuelve True si se pudo."""
    try:
        await bot.send_message(CANAL_ID, texto)
        return True
    except Exception:
        logging.exception("referencia al canal")
        if ADMINS:
            try:
                await bot.send_message(
                    ADMINS[0],
                    "⚠️ No pude enviar la referencia al canal. Revisa que el bot sea "
                    "administrador del canal con permiso para publicar mensajes.")
            except Exception:
                pass
        return False


SIN_ACCESO = (
    "🚫 No tienes likes disponibles.\n\n"
    "🎁 ¿Primera vez? Toca «Prueba gratis» en el menú (/start).\n"
    "🛒 O compra en «Comprar likes».\n"
    f"💰 2,000 likes ➜ ${PRECIOS_LIKES[2000][0]} USD / ${PRECIOS_LIKES[2000][1]} MXN\n"
    f"📲 Soporte: {PHONE}\n\n{BRAND}"
)


# ================== DECORADORES ==================
def admin(fn):
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMINS:
            return await update.message.reply_text("🚫 Solo admin.")
        return await fn(update, ctx)
    return w


def admin_privado(fn):
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMINS:
            return await update.message.reply_text("🚫 Solo admin.")
        if update.effective_chat.type != "private":
            return await update.message.reply_text("🔒 Usa este comando en privado conmigo.")
        return await fn(update, ctx)
    return w


async def registrar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u:
        return
    db.execute(
        "INSERT INTO users(user_id, username, created) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
        (u.id, (u.username or "").lower(), now().isoformat(timespec="seconds")))
    db.commit()
    r = get_user(u.id)
    if r and (r["banned"] or 0) and u.id not in ADMINS:
        try:
            if update.message:
                await update.message.reply_text(f"🚫 Tu acceso fue bloqueado.\n📲 Contacto: {PHONE}")
            elif update.callback_query:
                await update.callback_query.answer("🚫 Acceso bloqueado", show_alert=True)
        except Exception:
            pass
        raise ApplicationHandlerStop


# ================== COMANDOS USUARIO ==================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("esperando", None)
    u = update.effective_user
    if ctx.args and ctx.args[0].startswith("ref_") and ctx.args[0][4:].isdigit():
        ref = int(ctx.args[0][4:])
        r0 = get_user(u.id)
        ya_compro = db.execute("SELECT 1 FROM movimientos WHERE user_id=? LIMIT 1", (u.id,)).fetchone()
        if r0 and r0["ref_by"] is None and ref != u.id and get_user(ref) and not ya_compro:
            db.execute("UPDATE users SET ref_by=? WHERE user_id=?", (ref, u.id))
            db.commit()
    usuario = f"@{u.username}" if u.username else "sin usuario"
    await update.message.reply_text(
        f"👋 ¡Hola, {u.first_name or 'amigo'}! Soy 𝗖𝗜𝗕𝗘𝗥 𝗕𝗢𝗧 ⚡\n\n"
        "👤 𝗧𝗨 𝗣𝗘𝗥𝗙𝗜𝗟\n"
        f"🆔 ID ➜ {u.id}\n"
        f"👤 Usuario ➜ {usuario}\n"
        f"💳 Créditos ➜ {_creditos_txt(u.id)}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "❤️ Likes para Free Fire\n"
        "🎨 Nick Color Craftland\n"
        "🏦 Pago por transferencia • entrega automática\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Elige una opción 👇",
        reply_markup=kb_menu_principal(u.id))


async def ayuda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "╔══════════════╗\n  ❓ 𝗔𝗬𝗨𝗗𝗔\n╚══════════════╝\n\n"
        "❤️ 𝗟𝗜𝗞𝗘𝗦\n"
        "1️⃣ Compra un paquete en 🛒 Comprar likes (transferencia bancaria)\n"
        "2️⃣ Tus likes se guardan en tu cuenta como créditos: 1 crédito = 1 like\n"
        "3️⃣ Entra a ❤️ Likes Free Fire → Enviar likes, escribe el ID y elige la cantidad\n\n"
        "🎨 𝗡𝗜𝗖𝗞 𝗖𝗢𝗟𝗢𝗥 𝗖𝗥𝗔𝗙𝗧𝗟𝗔𝗡𝗗\n"
        f"Escribe tu Player ID y paga ${CRAFT_PRECIO_MXN} MXN por transferencia. "
        "Te avisamos aquí cuando esté listo.\n\n"
        "🎁 𝗣𝗥𝗨𝗘𝗕𝗔 𝗚𝗥𝗔𝗧𝗜𝗦\n"
        f"{PRUEBA_LIKES} likes gratis, una sola vez por usuario y por ID.\n\n"
        "🤝 𝗥𝗘𝗩𝗘𝗡𝗗𝗘𝗗𝗢𝗥𝗘𝗦\n"
        f"Se buscan revendedores. Pedidos desde {PEDIDO_GRANDE:,} likes: precio especial. "
        "Toca «Revendedores» en el menú.\n\n"
        f"📲 Soporte: {PHONE}\n\n{BRAND}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Escribir a soporte", callback_data="m|soporte")],
            [InlineKeyboardButton("🟢 Ver referencias", url=LINK_REFERENCIAS)],
            [InlineKeyboardButton("⬅️ Menú", callback_data="m|menu")]]))


def solo_admin(fn):
    """Solo los admins; si otro usuario lo escribe, el bot no responde nada."""
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMINS:
            return
        return await fn(update, ctx)
    return w


ADMIN_MENU = ''


@solo_admin
async def users_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return await update.message.reply_text("🔒 Usa este comando en privado conmigo.")
    rows = db.execute(
        "SELECT user_id, username, credits FROM users "
        "ORDER BY COALESCE(credits,0) DESC, user_id").fetchall()
    if not rows:
        return await update.message.reply_text("📭 Aún no hay usuarios.")
    bloque = f"👥 𝗨𝗦𝗨𝗔𝗥𝗜𝗢𝗦 ({len(rows)})\n\n"
    for i, r in enumerate(rows, 1):
        nombre = f"@{r['username']}" if r["username"] else "sin usuario"
        linea = f"{i}. {nombre} • 🆔 {r['user_id']} • 💳 {r['credits'] or 0}\n"
        if len(bloque) + len(linea) > 3800:   # límite de Telegram: 4096 por mensaje
            await update.message.reply_text(bloque)
            bloque = ""
        bloque += linea
    if bloque.strip():
        await update.message.reply_text(bloque)


@admin
async def ban_cmd(update, ctx):
    if not ctx.args:
        return await update.message.reply_text("❌ Uso: /ban <ID|@usuario>")
    uid = resolver(ctx.args[0])
    if uid is None or get_user(uid) is None:
        return await update.message.reply_text("❌ Usuario no encontrado.")
    if uid in ADMINS:
        return await update.message.reply_text("❌ No puedes bloquear a un admin.")
    db.execute("UPDATE users SET banned=1 WHERE user_id=?", (uid,))
    db.commit()
    await update.message.reply_text(f"🚫 {ctx.args[0]} bloqueado.")


@admin
async def unban_cmd(update, ctx):
    if not ctx.args:
        return await update.message.reply_text("❌ Uso: /unban <ID|@usuario>")
    uid = resolver(ctx.args[0])
    if uid is None or get_user(uid) is None:
        return await update.message.reply_text("❌ Usuario no encontrado.")
    db.execute("UPDATE users SET banned=0 WHERE user_id=?", (uid,))
    db.commit()
    await update.message.reply_text(f"✅ {ctx.args[0]} desbloqueado.")


@admin_privado
async def codigo_cmd(update, ctx):
    a = ctx.args
    if (not a or not a[0].isdigit() or int(a[0]) < 1
            or (len(a) > 1 and (not a[1].isdigit() or not 1 <= int(a[1]) <= 20))):
        return await update.message.reply_text(
            "❌ Uso: /codigo <créditos> [cantidad de códigos, máx. 20]\n"
            "Ejemplo: /codigo 2000 5")
    n = int(a[0])
    cuantos = int(a[1]) if len(a) > 1 else 1
    codes = []
    for _ in range(cuantos):
        c = "CB-" + secrets.token_hex(4).upper()
        db.execute(
            "INSERT INTO codigos(code, credits, created_by, created) VALUES(?,?,?,?)",
            (c, n, update.effective_user.id, now().isoformat(timespec="seconds")))
        codes.append(c)
    db.commit()
    await update.message.reply_text(
        f"🎟️ {cuantos} código(s) de {n} créditos:\n\n" + "\n".join(codes) +
        "\n\nEl cliente lo canjea en 💳 Mi cuenta → 🎟 Canjear código (cada código se usa una sola vez).")


@solo_admin
async def broadcast(update, ctx):
    partes = (update.message.text or "").split(maxsplit=1)
    if len(partes) < 2 or not partes[1].strip():
        return await update.message.reply_text("❌ Uso: /broadcast <mensaje>")
    texto = partes[1]
    rows = db.execute("SELECT user_id FROM users WHERE COALESCE(banned,0)=0").fetchall()
    aviso = await update.message.reply_text(f"📣 Enviando a {len(rows)} usuarios...")
    ok = fallos = 0
    for r in rows:
        try:
            await ctx.bot.send_message(r["user_id"], texto)
            ok += 1
        except Exception:
            fallos += 1
        await asyncio.sleep(0.06)   # respeta el límite de Telegram
    await aviso.edit_text(f"📣 Listo: ✅ {ok} enviados • ❌ {fallos} fallidos")


@admin_privado
async def buscar(update, ctx):
    if not ctx.args:
        return await update.message.reply_text("❌ Uso: /buscar <ID|@usuario>")
    uid = resolver(ctx.args[0])
    r = get_user(uid) if uid else None
    if not r:
        return await update.message.reply_text("🔎 Usuario no encontrado.")
    env = db.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(likes),0) l FROM logs "
        "WHERE user_id=? AND status='success'", (uid,)).fetchone()
    envios = db.execute("SELECT * FROM logs WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid,)).fetchall()
    movs = db.execute("SELECT * FROM movimientos WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid,)).fetchall()
    nombre = f"@{r['username']}" if r["username"] else "sin usuario"
    t = (
        f"🔎 {nombre} • 🆔 {uid}\n"
        f"💳 Créditos: {r['credits'] or 0}\n"
        f"🚫 Bloqueado: {'sí' if r['banned'] else 'no'}\n"
        f"📅 Primer uso: {(r['created'] or '—')[:16].replace('T', ' ')}\n"
        f"👥 Invitado por: {r['ref_by'] or '—'}\n"
        f"📈 Envíos: {env['c']} • {env['l']} likes\n")
    if envios:
        t += "\n📜 Últimos envíos\n" + "\n".join(
            f"{'✅' if x['status'] == 'success' else '❌'} {x['target_id']} ➜ {x['likes']} • "
            f"{x['ts'][5:16].replace('T', ' ')}" for x in envios) + "\n"
    if movs:
        t += "\n💳 Últimos movimientos\n" + "\n".join(fmt_mov(x) for x in movs)
    await update.message.reply_text(t)


@admin_privado
async def reporte_cmd(update, ctx):
    await update.message.reply_text(await texto_reporte())


@admin_privado
async def backup_cmd(update, ctx):
    ok = await hacer_backup(ctx.bot)
    await update.message.reply_text(
        "🗄️ Backup enviado." if ok else "❌ No se pudo hacer el backup.")


# ---------- Envío de likes ----------
EN_PROCESO = set()   # IDs de Free Fire con un envío en curso (evita doble envío)


async def say(ctx, chat_id, text, **kw):
    return await ctx.bot.send_message(chat_id, text, **kw)


def teclado_resultado(target_id, amount):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔁 Repetir envío", callback_data=f"rep|{target_id}|{amount}"),
        InlineKeyboardButton("💳 Mi saldo", callback_data="saldo"),
    ]])


async def procesar_likes(ctx, uid, chat_id, target_id, amount_arg, confirmado=False):
    es_adm = uid in ADMINS
    if cfg("bot_on", "1") == "0" and not es_adm:
        return await say(ctx, chat_id, "🔴 El bot está apagado por el momento.\n\n" + BRAND)

    # Modo de acceso
    if es_adm:
        modo, max_cant = "admin", MAX_AMOUNT
    elif cfg("unlimited") == "1":
        modo, max_cant = "ilimitado", MAX_AMOUNT
    elif creditos(uid) > 0:
        modo, max_cant = "creditos", min(MAX_AMOUNT, creditos(uid))
    elif cfg("free") == "1":
        usados = usos_hoy(get_user(uid))
        if usados >= FREE_DAILY:
            return await say(
                ctx, chat_id,
                f"⛔ Ya usaste tu envío gratis de hoy ({usados}/{FREE_DAILY}).\n"
                f"⏱️ Se reinicia en {resta_reinicio()}.\n\n"
                f"🛒 ¿Quieres más? Compra créditos con {OWNER}")
        modo, max_cant = "gratis", FREE_MAX_AMOUNT
    else:
        return await say(ctx, chat_id, SIN_ACCESO)

    amount = min(DEFAULT_AMOUNT, max_cant) if amount_arg is None else amount_arg
    if modo == "creditos" and amount > creditos(uid):
        return await say(ctx, chat_id,
                         f"💳 Créditos insuficientes: tienes {creditos(uid)}.\n🛒 Recarga con {OWNER}")
    if amount > max_cant:
        return await say(ctx, chat_id, f"❌ La cantidad máxima por envío es {max_cant}.")

    if target_id in EN_PROCESO:
        return await say(ctx, chat_id,
                         "⏳ Ya hay un envío en curso para ese ID. Espera a que termine.")

    # Confirmación en envíos grandes (solo cuando se gastan créditos)
    if modo == "creditos" and amount >= CONFIRMAR_DESDE and not confirmado:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirmar", callback_data=f"ok|{target_id}|{amount}"),
            InlineKeyboardButton("❌ Cancelar", callback_data="no"),
        ]])
        return await say(
            ctx, chat_id,
            f"⚠️ ¿Confirmas enviar {amount} likes al ID {target_id}?\n"
            "💳 Lo que no se envíe se te devuelve.", reply_markup=kb)

    # Reservar créditos antes de enviar (se devuelve lo que no se use)
    if modo == "creditos" and not cobrar(uid, amount, f"ID {target_id}"):
        return await say(ctx, chat_id, f"💳 Créditos insuficientes: tienes {creditos(uid)}.")

    EN_PROCESO.add(target_id)
    msg = await say(ctx, chat_id, "⏳ Enviando likes, espera un momento...")
    try:
        code, d, region, secs = await enviar(target_id, amount)
        if code != 200:
            if modo == "creditos":
                devolver(uid, amount, f"error API ID {target_id}")
            log_send(uid, target_id, None, 0, "error", "manual")
            if code == 400 and not es_adm:
                return await msg.edit_text(
                    "⚠️ No se pudo procesar ahora (el ID ya tiene un envío en curso "
                    f"o no hay stock).\nNo se descontaron créditos. Intenta más tarde o contacta a {OWNER}")
            return await msg.edit_text(error_txt(code, d))

        enviados = d.get("likes_sent", 0) or 0
        if modo == "creditos":
            devolver(uid, amount - enviados, f"no enviados ID {target_id}")
        elif modo == "gratis":
            sumar_uso(uid)
        log_send(uid, target_id, d.get("player_nickname"), enviados, "success", "manual")

        texto = ficha(d, target_id, region, secs)
        enviada = await enviar_referencia(ctx.bot, texto)
        extra = ""
        if modo == "creditos":
            restante = creditos(uid)
            extra += f"\n💳 Créditos restantes: {restante}"
            if restante < LOW_CREDITS:
                extra += f"\n⚠️ Te quedan pocos créditos. Recarga al {PHONE}"
        if enviada:
            extra += (f"\n📨 Referencia enviada a {await nombre_canal(ctx.bot)}"
                      f"\n🔗 Ver referencias: {LINK_REFERENCIAS}")
        await msg.edit_text(texto + extra, reply_markup=teclado_resultado(target_id, amount))
    except Exception:
        logging.exception("likes")
        if modo == "creditos":
            devolver(uid, amount, f"error de conexión ID {target_id}")
        try:
            await msg.edit_text("❌ No se pudo conectar con la API. No se descontaron créditos.")
        except Exception:
            pass
    finally:
        EN_PROCESO.discard(target_id)


async def botones(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    partes = (q.data or "").split("|")
    if partes[0] == "saldo":
        txt = "♾️ Admin: sin límite" if uid in ADMINS else f"💳 Créditos: {creditos(uid)}"
        return await q.answer(txt, show_alert=True)
    if partes[0] == "no":
        await q.answer("Cancelado")
        try:
            await q.message.edit_text("❌ Envío cancelado.")
        except Exception:
            pass
        return
    if partes[0] in ("rep", "ok") and len(partes) == 3 and partes[1].isdigit() and partes[2].isdigit():
        await q.answer()
        if partes[0] == "ok":
            try:
                await q.message.edit_reply_markup(reply_markup=None)   # evita doble toque
            except Exception:
                pass
        return await procesar_likes(ctx, uid, q.message.chat_id, partes[1], int(partes[2]),
                                    confirmado=(partes[0] == "ok"))
    if partes[0] == "pk" and len(partes) == 2 and partes[1].isdigit():
        likes_n = int(partes[1])
        if likes_n not in PRECIOS_LIKES:
            return await q.answer("Cantidad no válida", show_alert=True)
        await q.answer()
        return await iniciar_compra(ctx, uid, q.message.chat_id, likes_n)
    if partes[0] == "cx" and len(partes) == 2 and partes[1].isdigit():
        cur = db.execute(
            "UPDATE ordenes SET estado='cancelada' WHERE id=? AND user_id=? AND estado='pendiente'",
            (int(partes[1]), uid))
        db.commit()
        if cur.rowcount != 1:
            return await q.answer("Esa orden ya no se puede cancelar.", show_alert=True)
        await q.answer("Orden cancelada")
        try:
            await q.message.edit_text(f"❌ Orden #{partes[1]} cancelada.")
        except Exception:
            pass
        return
    if partes[0] in ("apr", "rec") and len(partes) == 2 and partes[1].isdigit():
        if uid not in ADMINS:
            return await q.answer("🚫 Solo los admins pueden aprobar pagos.", show_alert=True)
        tag = f"@{q.from_user.username}" if q.from_user.username else str(uid)
        ok, txt = await aplicar_decision(ctx.bot, uid, tag, int(partes[1]), partes[0])
        if not ok:
            return await q.answer(txt, show_alert=True)
        await q.answer("Listo")
        try:
            await q.message.edit_caption(caption=(q.message.caption or "") + f"\n\n{txt}",
                                         reply_markup=None)
        except Exception:
            pass
        return
    await q.answer()


# ================== CRAFTLAND (Nick Color) ==================
def pick(d, *keys, default=None):
    """Busca una clave en el JSON (primer nivel o un nivel adentro)."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    for v in d.values():
        if isinstance(v, dict):
            for k in keys:
                if v.get(k) is not None:
                    return v[k]
    return default


def fmt_n(x):
    if isinstance(x, bool) or x is None:
        return "N/D"
    return f"{x:,}" if isinstance(x, int) else str(x)


async def craft_api(method, path, **kw):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.request(method, CRAFT_BASE + path, headers={"X-Api-Key": CRAFT_KEY}, **kw)
    try:
        data = r.json()
    except Exception:
        data = {"error": "Respuesta inválida"}
    return r.status_code, data


def craft_error(code, d):
    c = (d.get("code") or "") if isinstance(d, dict) else ""
    if code == 400:
        return "❌ Player ID inválido."
    if code == 404:
        return "🔎 ID no encontrado en el juego."
    if code == 409:
        if c == "NO_STOCK":
            return "📦 Sin stock por ahora. Intenta más tarde."
        if c == "ID_BUSY":
            return "⏳ Ese ID ya tiene un pedido en curso."
        return "⚠️ Ese ID ya recibió este pack."
    if code == 429:
        return "⏳ Muchas solicitudes, espera un momento e intenta de nuevo."
    if code == 503:
        return "🛠 El servicio está en mantenimiento, intenta más tarde."
    return "⚠️ Este servicio no está disponible por ahora. Intenta más tarde."


async def craft_stock():
    try:
        code, d = await craft_api("GET", "/api/v1/stock")
        return d.get("available") if code == 200 else None
    except Exception:
        return None


def _pedido_activo(player_id):
    return db.execute(
        "SELECT 1 FROM craft_pedidos WHERE player_id=? AND estado IN ('queued','processing')",
        (player_id,)).fetchone()


async def craft_preview(ctx, uid, chat_id, player_id):
    """Muestra los datos del jugador y pide confirmación antes de pasar al pago."""
    es_adm = uid in ADMINS
    if not CRAFT_KEY:
        return await say(ctx, chat_id, "🎨 Este servicio no está disponible por ahora.")
    if cfg("bot_on", "1") == "0" and not es_adm:
        return await say(ctx, chat_id, "🔴 El bot está apagado por el momento.\n\n" + BRAND)
    if _pedido_activo(player_id):
        return await say(ctx, chat_id, "⏳ Ese ID ya tiene un pedido en curso.")
    try:
        if await craft_stock() is False:
            return await say(ctx, chat_id, "📦 Sin stock por ahora. Intenta más tarde.")
        code, d = await craft_api("GET", f"/api/v1/player/{player_id}")
    except Exception:
        logging.exception("craft preview")
        return await say(ctx, chat_id, "❌ No se pudo conectar con el servicio. Intenta de nuevo.")
    if code != 200:
        if code in (401, 403):
            await avisar_admin(ctx.bot, f"⚠️ Craftland: la API respondió {code}. Revisa tu API key del proveedor.")
        return await say(ctx, chat_id, craft_error(code, d))
    if pick(d, "eligible") is False:
        return await say(
            ctx, chat_id,
            "⚠️ Este ID no puede recibir el pack (ya lo recibió o no cumple los requisitos).")

    nick = pick(d, "nickname", "nick", "apelido", "player_nickname", default="N/D")
    region = pick(d, "region", "regiao", default="N/D")
    seg = pick(d, "followers", "seguidores")
    costo = "Gratis (admin)" if es_adm else f"${CRAFT_PRECIO_MXN} MXN"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Crear pedido" if es_adm else "💳 Continuar al pago",
                             callback_data=f"ct|ok|{player_id}"),
        InlineKeyboardButton("❌ Cancelar", callback_data="ct|no"),
    ]])
    await say(
        ctx, chat_id,
        "🎨 NICK COLOR CRAFTLAND\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"↬ Jugador: {nick}\n"
        f"↬ ID: {player_id}\n"
        f"↬ Región: {region}\n"
        f"↬ Seguidores actuales: {fmt_n(seg)}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"↬ Pack: Nivel 6 • +{CRAFT_SEGUIDORES:,} seguidores\n"
        f"↬ Precio: {costo}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "¿Es tu cuenta? Confirma para continuar.",
        reply_markup=kb)


async def craft_crear_en_proveedor(bot, uid, chat_id, player_id, orden_id=None):
    """Crea el pedido en el proveedor. Devuelve (True, código) o (False, texto de error)."""
    if not CRAFT_KEY:
        return False, "Falta la variable CRAFT_API_KEY."
    if _pedido_activo(player_id):
        return False, "Ese ID ya tiene un pedido en curso."
    try:
        code, d = await craft_api("POST", "/api/v1/orders", json={"player_id": player_id})
    except Exception:
        logging.exception("craft pedido")
        return False, "No se pudo conectar con el proveedor. Intenta de nuevo."
    if code not in (200, 201, 202):
        if code in (401, 402, 403):
            await avisar_admin(
                bot, f"⚠️ Craftland: error {code} ({pick(d, 'code', 'error')}). "
                     "Revisa el saldo y la API key del proveedor.")
        return False, craft_error(code, d)
    codigo = pick(d, "code", "order_code", "order_id", "id")
    db.execute(
        "INSERT INTO craft_pedidos(ts,user_id,chat_id,player_id,creditos,cobrado,code,estado,orden_id) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (now().isoformat(timespec="seconds"), uid, chat_id, player_id, 0, 0,
         str(codigo) if codigo is not None else None,
         "queued" if codigo is not None else "revisar", orden_id))
    db.commit()
    if codigo is None:
        await avisar_admin(
            bot, f"⚠️ Craftland: se creó un pedido para el ID {player_id} (cliente {uid}) pero la "
                 "respuesta no trajo código. Revísalo con el proveedor.")
        return True, "en revisión"
    return True, str(codigo)


async def iniciar_compra_craft(ctx, uid, chat_id, player_id):
    oid, monto = crear_orden(uid, 0, "craft", player_id, CRAFT_PRECIO_MXN)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar orden", callback_data=f"cx|{oid}")]])
    await ctx.bot.send_message(chat_id, texto_pago(oid, 0, monto, "craft", player_id),
                               parse_mode="HTML", reply_markup=kb)


def ficha_craft(r, nick, region, antes, despues):
    return (
        "🎨 NICK COLOR ENTREGADO CON ÉXITO!\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"↬ Jugador: {nick}\n"
        f"↬ ID: {r['player_id']}\n"
        f"↬ Región: {region}\n"
        f"↬ Pedido: #{r['code']}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"↬ Seguidores antes: {fmt_n(antes)}\n"
        f"↬ Seguidores después: {fmt_n(despues)}\n"
        f"↬ Pack: +{CRAFT_SEGUIDORES:,} seguidores\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"↬ Contacto: {CONTACTO}"
    )


async def craft_finalizar(bot, r, d, ok):
    nuevo = "completed" if ok else "failed"
    cur = db.execute(
        "UPDATE craft_pedidos SET estado=? WHERE id=? AND estado IN ('queued','processing')",
        (nuevo, r["id"]))
    db.commit()
    if cur.rowcount != 1:
        return
    antes = pick(d, "followers_before")
    despues = pick(d, "followers_after")
    db.execute("UPDATE craft_pedidos SET f_antes=?, f_despues=? WHERE id=?",
               (antes if isinstance(antes, int) else None,
                despues if isinstance(despues, int) else None, r["id"]))
    db.commit()
    if ok:
        nick = pick(d, "nickname", "nick", "player_nickname")
        region = pick(d, "region")
        if nick is None or region is None:
            try:
                pc, pd = await craft_api("GET", f"/api/v1/player/{r['player_id']}")
                if pc == 200:
                    nick = nick or pick(pd, "nickname", "nick", "apelido", "player_nickname")
                    region = region or pick(pd, "region", "regiao")
            except Exception:
                pass
        texto = ficha_craft(r, nick or "N/D", region or "N/D", antes, despues)
        enviada = await enviar_referencia(bot, texto)
        extra = ""
        if enviada:
            extra = (f"\n\n📨 Referencia enviada a {await nombre_canal(bot)}"
                     f"\n🔗 Ver referencias: {LINK_REFERENCIAS}")
        try:
            await bot.send_message(r["chat_id"], texto + extra)
        except Exception:
            logging.exception("craft aviso entrega")
        return
    texto = f"❌ Tu pedido #{r['code']} no pudo completarse."
    if r["cobrado"]:
        devolver(r["user_id"], r["creditos"], f"Craftland no completado #{r['code']}")
        texto += f"\n💳 Se devolvieron {r['creditos']:,} créditos a tu cuenta."
    elif r["orden_id"]:
        texto += "\n💬 Ya avisamos a soporte para resolverlo (reintento o reembolso)."
        await avisar_admin(
            bot, f"💸 Craftland: el pedido #{r['code']} (ID {r['player_id']}) falló y el cliente "
                 f"{r['user_id']} ya pagó ${CRAFT_PRECIO_MXN} MXN (orden #{r['orden_id']}). "
                 "Reintenta el pedido o reembolsa.")
    try:
        await bot.send_message(r["chat_id"], texto)
    except Exception:
        logging.exception("craft aviso falla")


async def craft_alerta_demora(bot, r):
    try:
        edad = (now() - datetime.fromisoformat(r["ts"])).total_seconds()
    except Exception:
        return
    if edad > 1800 and not r["alertado"]:
        db.execute("UPDATE craft_pedidos SET alertado=1 WHERE id=?", (r["id"],))
        db.commit()
        await avisar_admin(
            bot, f"⏰ Craftland: el pedido #{r['code']} (ID {r['player_id']}) lleva más de 30 min "
                 "sin terminar. Revísalo con el proveedor.")


async def craft_revisar(bot):
    rows = db.execute(
        "SELECT * FROM craft_pedidos WHERE estado IN ('queued','processing') "
        "AND code IS NOT NULL ORDER BY id LIMIT 8").fetchall()
    for r in rows:
        try:
            code, d = await craft_api("GET", f"/api/v1/orders/{r['code']}")
        except Exception:
            continue
        if code != 200:
            await craft_alerta_demora(bot, r)
            continue
        st = pick(d, "status")
        if st == "completed":
            await craft_finalizar(bot, r, d, True)
        elif st == "failed":
            await craft_finalizar(bot, r, d, False)
        else:
            if st in ("queued", "processing") and st != r["estado"]:
                db.execute("UPDATE craft_pedidos SET estado=? WHERE id=?", (st, r["id"]))
                db.commit()
            await craft_alerta_demora(bot, r)
        await asyncio.sleep(1)


async def craft_alerta_saldo(bot):
    try:
        code, d = await craft_api("GET", "/api/v1/balance")
    except Exception:
        return
    if code != 200:
        return
    cubiertos = pick(d, "orders_covered")
    if isinstance(cubiertos, int) and cubiertos <= 1 and cfg("craft_alerta", "") != hoy():
        set_cfg("craft_alerta", hoy())
        await avisar_admin(
            bot, f"📉 Craftland: tu saldo con el proveedor solo cubre {cubiertos} pedido(s) "
                 f"({pick(d, 'balance')}). Recarga para no quedarte sin servicio.")


async def craft_loop(app):
    ultimo_bal = 0
    while True:
        try:
            if CRAFT_KEY:
                await craft_revisar(app.bot)
                if time.time() - ultimo_bal > 1800:
                    ultimo_bal = time.time()
                    await craft_alerta_saldo(app.bot)
        except Exception:
            logging.exception("craft_loop")
        await asyncio.sleep(10)


ESTADO_CRAFT = {"queued": "⏳ en fila", "processing": "⚙️ enviando", "completed": "✅ entregado",
                "failed": "❌ no completado", "revisar": "🔎 en revisión"}


async def craft_mis_pedidos(ctx, uid, chat_id):
    rows = db.execute(
        "SELECT * FROM craft_pedidos WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid,)).fetchall()
    if not rows:
        return await say(ctx, chat_id, "📭 Aún no tienes pedidos de Nick Color Craftland.")
    lineas = [f"🧾 #{r['code'] or '—'} • 🆔 {r['player_id']} • {ESTADO_CRAFT.get(r['estado'], r['estado'])}"
              for r in rows]
    await say(ctx, chat_id, "🎨 𝗠𝗜𝗦 𝗣𝗘𝗗𝗜𝗗𝗢𝗦\n\n" + "\n".join(lineas))


@admin_privado
async def craftsaldo_cmd(update, ctx):
    if not CRAFT_KEY:
        return await update.message.reply_text("⚠️ Falta la variable CRAFT_API_KEY.")
    try:
        code, d = await craft_api("GET", "/api/v1/balance")
        if code != 200:
            return await update.message.reply_text(craft_error(code, d))
        disp = await craft_stock()
        stock = "✅ disponible" if disp else ("❌ sin stock" if disp is False else "❓ sin confirmar")
        await update.message.reply_text(
            "🎨 𝗦𝗔𝗟𝗗𝗢 𝗖𝗥𝗔𝗙𝗧𝗟𝗔𝗡𝗗\n\n"
            f"💰 Saldo: {pick(d, 'balance')}\n"
            f"🏷️ Costo por pedido: {pick(d, 'price')}\n"
            f"📦 Pedidos que cubre: {pick(d, 'orders_covered')}\n"
            f"📊 Stock: {stock}\n"
            f"💳 Precio al cliente: ${CRAFT_PRECIO_MXN} MXN")
    except Exception:
        logging.exception("craftsaldo")
        await update.message.reply_text("❌ No se pudo conectar con Craftland.")


@admin_privado
async def craftpedidos_cmd(update, ctx):
    rows = db.execute("SELECT * FROM craft_pedidos ORDER BY id DESC LIMIT 12").fetchall()
    if not rows:
        return await update.message.reply_text("📭 Aún no hay pedidos de Craftland.")
    lineas = [f"#{r['code'] or '—'} • 🆔 {r['player_id']} • cliente {r['user_id']} • "
              f"{ESTADO_CRAFT.get(r['estado'], r['estado'])}" for r in rows]
    await update.message.reply_text("🎨 𝗣𝗘𝗗𝗜𝗗𝗢𝗦 𝗖𝗥𝗔𝗙𝗧𝗟𝗔𝗡𝗗\n\n" + "\n".join(lineas))


# ================== MENÚ POR BOTONES ==================
def _creditos_txt(uid):
    return "∞" if uid in ADMINS else f"{creditos(uid):,}"


class _Shim:
    """Permite reutilizar los comandos desde un botón."""
    def __init__(self, q):
        self.callback_query = q
        self.message = q.message
        self.effective_user = q.from_user
        self.effective_chat = q.message.chat


async def editar(q, texto, kb=None):
    try:
        await q.message.edit_text(texto, reply_markup=kb)
    except Exception:
        try:
            await q.message.reply_text(texto, reply_markup=kb)
        except Exception:
            pass


PEDIR = {}


# ---------- Pagos: transferencia bancaria México ----------
def precio_mxn(likes):
    p = PRECIOS_LIKES.get(likes)
    return float(p[1]) if p else None


def crear_orden(uid, likes, producto="likes", player_id=None, monto=None):
    """Crea una orden pendiente (cancela la pendiente anterior del mismo usuario)."""
    db.execute("UPDATE ordenes SET estado='cancelada' WHERE user_id=? AND estado='pendiente'", (uid,))
    if monto is None:
        monto = precio_mxn(likes)
    cur = db.execute(
        "INSERT INTO ordenes(ts, user_id, likes, monto, estado, producto, player_id) "
        "VALUES(?,?,?,?, 'pendiente', ?, ?)",
        (now().isoformat(timespec="seconds"), uid, likes, monto, producto, player_id))
    db.commit()
    return cur.lastrowid, monto


def texto_pago(oid, likes, monto, producto="likes", player_id=None):
    e = html.escape
    if producto == "craft":
        compra = ("🎨 Compra: <b>Nick Color Craftland</b>\n"
                  f"🆔 Player ID: <b>{e(str(player_id))}</b>\n")
    else:
        compra = f"❤️ Compra: <b>{likes:,}</b> likes (se guardan como créditos)\n"
    return (
        "╔══════════════╗\n"
        f"  🏦 <b>PAGO • ORDEN #{oid}</b>\n"
        "╚══════════════╝\n\n"
        + compra +
        f"💵 Total a pagar: <b>${_fmt(monto)} MXN</b>\n\n"
        "🏦 <b>MÉTODO DE PAGO</b>\n"
        "Transferencia Bancaria México\n\n"
        f"👤 Nombre: <b>{e(PAGO_NOMBRE)}</b>\n"
        f"🔢 CLABE: <code>{e(PAGO_CLABE)}</code>\n"
        f"🏛️ Banco: <b>{e(PAGO_BANCO)}</b>\n"
        f"📝 Concepto: <code>CB{oid}</code>\n\n"
        "📸 Cuando transfieras, <b>envía aquí mismo la foto (o PDF) de tu comprobante</b>.\n"
        "⏳ Lo revisamos y te avisamos en cuanto se apruebe.\n\n"
        f"❓ ¿Dudas? 📲 {PHONE}"
    )


async def iniciar_compra(ctx, uid, chat_id, likes):
    oid, monto = crear_orden(uid, likes)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar orden", callback_data=f"cx|{oid}")]])
    await ctx.bot.send_message(chat_id, texto_pago(oid, likes, monto),
                               parse_mode="HTML", reply_markup=kb)


async def comprar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return await update.message.reply_text(
            "🔒 Usa este menú en privado conmigo para ver los datos de pago.")
    B = InlineKeyboardButton
    filas, fila = [], []
    for n in PAQUETES:
        fila.append(B(f"{n:,} • ${PRECIOS_LIKES[n][1]:,}", callback_data=f"pk|{n}"))
        if len(fila) == 2:
            filas.append(fila)
            fila = []
    if fila:
        filas.append(fila)
    filas.append([B("💎 Pedido grande / 🤝 Revendedores", callback_data="m|revendedor")])
    filas.append([B("⬅️ Menú", callback_data="m|menu")])
    await update.message.reply_text(
        "╔══════════════╗\n  🛒 𝗖𝗢𝗠𝗣𝗥𝗔𝗥 𝗟𝗜𝗞𝗘𝗦\n╚══════════════╝\n\n"
        "▫️ Elige tu paquete (precio en MXN)\n"
        "▫️ 1 like comprado = 1 crédito\n"
        "🏦 Pago: Transferencia Bancaria México\n\n"
        f"💎 Desde {PEDIDO_GRANDE:,} likes: pregunta por precio especial.\n\n"
        "Toca un paquete 👇",
        reply_markup=InlineKeyboardMarkup(filas))


async def recibir_comprobante(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    u = update.effective_user
    if m is None or u is None:
        return
    if m.photo:
        file_id, kind = m.photo[-1].file_id, "photo"
    elif m.document:
        file_id, kind = m.document.file_id, "document"
    else:
        return

    o = db.execute(
        "SELECT * FROM ordenes WHERE user_id=? AND estado='pendiente' ORDER BY id DESC LIMIT 1",
        (u.id,)).fetchone()
    if not o:
        rev = db.execute(
            "SELECT id FROM ordenes WHERE user_id=? AND estado='en_revision' ORDER BY id DESC LIMIT 1",
            (u.id,)).fetchone()
        if rev:
            return await m.reply_text(
                f"🔎 Tu comprobante de la orden #{rev['id']} ya está en revisión. "
                "Te avisamos en cuanto se apruebe.")
        return await m.reply_text(
            "📸 No tienes una compra pendiente.\nElige un paquete en 🛒 Comprar likes (o en "
            "🎨 Nick Color Craftland) desde /start y luego envía tu comprobante.")

    db.execute("UPDATE ordenes SET estado='en_revision', file_id=?, kind=? WHERE id=?",
               (file_id, kind, o["id"]))
    db.commit()
    nombre = f"@{u.username}" if u.username else (u.first_name or "sin usuario")
    if o["producto"] == "craft":
        detalle = f"🎨 Nick Color Craftland • ID {o['player_id']}"
    else:
        detalle = f"❤️ {o['likes']:,} likes"
    cap = (
        f"💳 NUEVA COMPRA • ORDEN #{o['id']}\n\n"
        f"👤 {nombre} • 🆔 {u.id}\n"
        f"{detalle}\n"
        f"💵 ${_fmt(o['monto'])} MXN\n"
        f"📝 Concepto: CB{o['id']}\n"
        f"🕒 {now():%d/%m %H:%M}\n\n"
        "Verifica el pago y decide:"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Aprobar", callback_data=f"apr|{o['id']}"),
        InlineKeyboardButton("❌ Rechazar", callback_data=f"rec|{o['id']}"),
    ]])
    try:
        if kind == "photo":
            await ctx.bot.send_photo(GRUPO_PAGOS_ID, file_id, caption=cap, reply_markup=kb)
        else:
            await ctx.bot.send_document(GRUPO_PAGOS_ID, file_id, caption=cap, reply_markup=kb)
    except Exception:
        logging.exception("comprobante al grupo")
        db.execute("UPDATE ordenes SET estado='pendiente' WHERE id=?", (o["id"],))
        db.commit()
        await avisar_admin(
            ctx.bot, "⚠️ No pude enviar un comprobante al grupo de pagos. Revisa que el bot "
                     "esté en el grupo y pueda escribir.")
        return await m.reply_text(
            "❌ No pude enviar tu comprobante. Inténtalo de nuevo en un momento "
            f"o escribe al {PHONE}.")
    await m.reply_text(
        f"✅ Comprobante recibido\n\n🧾 Orden #{o['id']} • {detalle}\n"
        "⏳ Estamos revisando tu pago. Te avisamos aquí en cuanto se apruebe.")


async def aplicar_decision(bot, admin_id, admin_tag, oid, accion):
    """Aprueba ('apr') o rechaza ('rec') una orden. Devuelve (ok, texto)."""
    o = db.execute("SELECT * FROM ordenes WHERE id=?", (oid,)).fetchone()
    if not o:
        return False, "Orden no encontrada."
    nuevo = "aprobada" if accion == "apr" else "rechazada"
    cur = db.execute(
        "UPDATE ordenes SET estado=?, decidido_por=?, decidido_ts=? WHERE id=? AND estado='en_revision'",
        (nuevo, admin_id, now().isoformat(timespec="seconds"), oid))
    db.commit()
    if cur.rowcount != 1:
        return False, f"Esa orden ya está: {o['estado']}."
    cliente = o["user_id"]
    if accion == "apr":
        if o["producto"] == "craft":
            ok, res = await craft_crear_en_proveedor(bot, cliente, cliente, o["player_id"], orden_id=oid)
            if not ok:
                db.execute("UPDATE ordenes SET estado='en_revision' WHERE id=?", (oid,))
                db.commit()
                return False, f"Craftland: {res} (la orden sigue en revisión, puedes aprobarla de nuevo)."
            try:
                await bot.send_message(
                    cliente,
                    "🎉 ¡Pago aprobado!\n\n🎨 Nick Color Craftland\n"
                    f"🆔 {o['player_id']}\n🧾 Pedido #{res}\n"
                    f"⏳ Te aviso aquí en cuanto esté listo.\n\n{BRAND}")
            except Exception:
                pass
            return True, f"✅ APROBADA por {admin_tag} • pedido #{res}"
        try:
            pago = acreditar(cliente, o["likes"], "recarga", f"orden #{oid} transferencia")
        except Exception:
            logging.exception("acreditar orden")
            db.execute("UPDATE ordenes SET estado='en_revision' WHERE id=?", (oid,))
            db.commit()
            return False, "Error al acreditar los créditos. Intenta de nuevo."
        try:
            await bot.send_message(
                cliente,
                f"🎉 ¡Pago aprobado!\n\n❤️ Se acreditaron {o['likes']:,} likes a tu cuenta\n"
                f"💳 Saldo: {creditos(cliente):,}\n\n"
                f"Abre el menú con /start y entra a ❤️ Likes → Enviar likes.\n\n{BRAND}")
        except Exception:
            pass
        if pago:
            try:
                await bot.send_message(
                    pago, f"🎁 ¡Ganaste {REF_BONUS} créditos! Un amigo que invitaste hizo su "
                          f"primera recarga.\n💳 Saldo: {creditos(pago)}")
            except Exception:
                pass
        return True, f"✅ APROBADA por {admin_tag}"
    try:
        await bot.send_message(
            cliente,
            f"❌ Tu pago de la orden #{oid} no fue aprobado.\n"
            f"Si crees que es un error, escribe al {PHONE} con tu comprobante.\n\n{BRAND}")
    except Exception:
        pass
    return True, f"❌ RECHAZADA por {admin_tag}"


async def _decidir_cmd(update, ctx, accion):
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text(
            f"❌ Uso: /{'aprobar' if accion == 'apr' else 'rechazar'} <número de orden>")
    u = update.effective_user
    tag = f"@{u.username}" if u.username else str(u.id)
    ok, txt = await aplicar_decision(ctx.bot, u.id, tag, int(ctx.args[0]), accion)
    await update.message.reply_text(f"{txt} (orden #{ctx.args[0]})" if ok else f"⚠️ {txt}")


@admin_privado
async def aprobar_cmd(update, ctx):
    await _decidir_cmd(update, ctx, "apr")


@admin_privado
async def rechazar_cmd(update, ctx):
    await _decidir_cmd(update, ctx, "rec")


@admin_privado
async def ordenes_cmd(update, ctx):
    rows = db.execute("SELECT * FROM ordenes ORDER BY id DESC LIMIT 15").fetchall()
    if not rows:
        return await update.message.reply_text("📭 Aún no hay órdenes.")
    est = {"pendiente": "🕓 esperando comprobante", "en_revision": "🔎 en revisión",
           "aprobada": "✅ aprobada", "rechazada": "❌ rechazada", "cancelada": "🚫 cancelada"}
    rev = sum(1 for r in rows if r["estado"] == "en_revision")
    lineas = [
        f"#{r['id']} • 🆔 {r['user_id']} • {r['likes']:,} • ${_fmt(r['monto'])} • "
        f"{est.get(r['estado'], r['estado'])}" for r in rows]
    await update.message.reply_text(
        f"🧾 𝗢́𝗥𝗗𝗘𝗡𝗘𝗦 (en revisión: {rev})\n\n" + "\n".join(lineas) +
        "\n\nDecide con /aprobar <orden> o /rechazar <orden>")


# ---------- Comandos nuevos para clientes ----------
def _fmt(x):
    return f"{x:.2f}".rstrip("0").rstrip(".")


async def precios(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lineas = [f"❤️ {n:,} Likes — ${usd:,} USD / ${mxn:,} MXN" for n, (usd, mxn) in PRECIOS_LIKES.items()]
    B = InlineKeyboardButton
    await update.message.reply_text(
        "❤️ PRECIOS DE LIKES FREE FIRE — CIBERSTORE\n\n" + "\n".join(lineas) + "\n\n"
        f"🎨 Nick Color Craftland — ${CRAFT_PRECIO_MXN} MXN\n\n"
        f"💎 Pedidos de {PEDIDO_GRANDE:,} likes en adelante: ¡mejoramos el precio!\n"
        "🤝 Se buscan revendedores de likes.\n\n"
        "🏦 Pago por transferencia bancaria México (MXN)\n"
        f"📲 Soporte: {PHONE}\n\n{BRAND}",
        reply_markup=InlineKeyboardMarkup([
            [B("🛒 Comprar likes", callback_data="m|comprar"), B("🎨 Nick Color", callback_data="m|craft")],
            [B("💎 Pedido grande / 🤝 Revendedores", callback_data="m|revendedor")],
            [B("⬅️ Menú", callback_data="m|menu")]]))


async def canjear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not ctx.args:
        return await update.message.reply_text(
            "❌ Envía tu código de recarga. Ejemplo: CB-1A2B3C4D")
    code = ctx.args[0].strip().upper()
    cur = db.execute(
        "UPDATE codigos SET used_by=?, used_at=? WHERE code=? AND used_by IS NULL",
        (u.id, now().isoformat(timespec="seconds"), code))
    db.commit()
    if cur.rowcount != 1:
        return await update.message.reply_text("❌ Código inválido o ya usado.")
    n = db.execute("SELECT credits FROM codigos WHERE code=?", (code,)).fetchone()["credits"]
    pago = acreditar(u.id, n, "codigo", code)
    await update.message.reply_text(
        f"🎉 ¡Código canjeado!\n➕ {n} créditos\n💳 Saldo: {creditos(u.id)}\n\n"
        f"Abre el menú con /start y entra a ❤️ Likes → Enviar likes.\n\n{BRAND}")
    quien = f"@{u.username}" if u.username else str(u.id)
    await avisar_admin(ctx.bot, f"🎟️ {quien} canjeó {code} (+{n} créditos)")
    if pago:
        try:
            await ctx.bot.send_message(
                pago, f"🎁 ¡Ganaste {REF_BONUS} créditos! Un amigo que invitaste hizo su primera recarga.\n"
                      f"💳 Saldo: {creditos(pago)}")
        except Exception:
            pass


async def movimientos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = db.execute(
        "SELECT * FROM movimientos WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
    if not rows:
        return await update.message.reply_text("📭 Aún no tienes movimientos de créditos.")
    await update.message.reply_text(
        "╔══════════════╗\n"
        "  📒 𝗠𝗜𝗦 𝗠𝗢𝗩𝗜𝗠𝗜𝗘𝗡𝗧𝗢𝗦\n"
        "╚══════════════╝\n\n" + "\n".join(fmt_mov(r) for r in rows) +
        f"\n\n💳 Saldo actual: {creditos(uid)}")


async def referidos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    link = f"https://t.me/{ctx.bot.username}?start=ref_{uid}"
    inv = db.execute("SELECT COUNT(*) c FROM users WHERE ref_by=?", (uid,)).fetchone()["c"]
    pag = db.execute("SELECT COUNT(*) c FROM users WHERE ref_by=? AND ref_paid=1", (uid,)).fetchone()["c"]
    await update.message.reply_text(
        "╔══════════════╗\n"
        "  👥 𝗥𝗘𝗙𝗘𝗥𝗜𝗗𝗢𝗦\n"
        "╚══════════════╝\n\n"
        f"🎁 Gana {REF_BONUS} créditos por cada amigo que haga su primera recarga.\n\n"
        f"🔗 Tu enlace:\n{link}\n\n"
        f"👤 Invitados: {inv}\n"
        f"✅ Con recarga: {pag} (+{pag * REF_BONUS} créditos ganados)\n\n{BRAND}")


def cancelar_auto(target_id):
    """Borra un auto. Si era de un cliente, le devuelve los días no usados."""
    a = db.execute("SELECT * FROM autos WHERE target_id=?", (target_id,)).fetchone()
    if not a:
        return None, 0
    reembolso = 0
    if a["owner_id"]:
        reembolso = (a["restantes"] or 0) * (a["amount"] or AUTO_AMOUNT)
        devolver(a["owner_id"], reembolso, f"auto cancelado ID {target_id}")
    db.execute("DELETE FROM autos WHERE target_id=?", (target_id,))
    db.commit()
    return a, reembolso


async def prueba_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if cfg("bot_on", "1") == "0" and uid not in ADMINS:
        return await update.message.reply_text("🔴 El bot está apagado por el momento.\n\n" + BRAND)
    if cfg("prueba", "1") != "1":
        return await update.message.reply_text(
            "🎁 La prueba gratuita no está disponible por ahora.\n\n" + SIN_ACCESO)
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text(
            f"🎁 Prueba gratis de {PRUEBA_LIKES} likes (una sola vez)\n\n"
            "❌ Envía un ID de Free Fire válido (solo números).")

    target_id = ctx.args[0]
    r = get_user(uid)
    if uid not in ADMINS:
        if r and r["prueba_usada"]:
            return await update.message.reply_text(
                "🎁 Ya usaste tu prueba gratuita.\n\n"
                f"💰 2,000 likes ➜ ${PRECIOS_LIKES[2000][0]} USD / ${PRECIOS_LIKES[2000][1]} MXN\n"
                f"🛒 Compra likes desde /start → 🛒 Comprar likes\n📲 Soporte: {PHONE}\n\n{BRAND}")
        if db.execute("SELECT 1 FROM pruebas WHERE target_id=?", (target_id,)).fetchone():
            return await update.message.reply_text(
                "⚠️ Ese ID de Free Fire ya recibió una prueba gratuita.")
    if target_id in EN_PROCESO:
        return await update.message.reply_text(
            "⏳ Ya hay un envío en curso para ese ID. Espera a que termine.")

    EN_PROCESO.add(target_id)
    msg = await update.message.reply_text("⏳ Enviando tu prueba gratuita...")
    try:
        code, d, region, secs = await enviar(target_id, PRUEBA_LIKES)
        enviados = (d.get("likes_sent", 0) or 0) if code == 200 else 0
        if code != 200 or enviados <= 0:
            log_send(uid, target_id, None, 0, "error", "prueba")
            if code == 200:
                txt = "⚠️ No se pudieron enviar likes a ese ID ahora."
            elif code == 400 and uid not in ADMINS:
                txt = ("⚠️ No se pudo procesar ahora (el ID ya tiene un envío en curso "
                       "o no hay stock).")
            else:
                txt = error_txt(code, d)
            return await msg.edit_text(txt + "\n🎁 Tu prueba no se gastó, intenta más tarde.")

        if uid not in ADMINS:
            db.execute("UPDATE users SET prueba_usada=1 WHERE user_id=?", (uid,))
            db.execute("INSERT OR IGNORE INTO pruebas(target_id, user_id, ts) VALUES(?,?,?)",
                       (target_id, uid, now().isoformat(timespec="seconds")))
            db.commit()
        log_send(uid, target_id, d.get("player_nickname"), enviados, "success", "prueba")

        texto = ficha(d, target_id, region, secs)
        enviada = await enviar_referencia(ctx.bot, texto)
        extra = "\n\n🎁 Esta fue tu prueba gratuita."
        if enviada:
            extra += (f"\n📨 Referencia enviada a {await nombre_canal(ctx.bot)}"
                      f"\n🔗 Ver referencias: {LINK_REFERENCIAS}")
        extra += f"\n\n🛒 ¿Te gustó? Compra créditos:\n📲 {PHONE}"
        await msg.edit_text(texto + extra)
    except Exception:
        logging.exception("prueba")
        try:
            await msg.edit_text(
                "❌ No se pudo conectar con la API.\n🎁 Tu prueba no se gastó, intenta más tarde.")
        except Exception:
            pass
    finally:
        EN_PROCESO.discard(target_id)


@admin
async def resetprueba_cmd(update, ctx):
    if not ctx.args:
        return await update.message.reply_text("❌ Uso: /resetprueba <ID|@usuario>")
    uid = resolver(ctx.args[0])
    if uid is None or get_user(uid) is None:
        return await update.message.reply_text("❌ Usuario no encontrado.")
    db.execute("UPDATE users SET prueba_usada=0 WHERE user_id=?", (uid,))
    db.execute("DELETE FROM pruebas WHERE user_id=?", (uid,))
    db.commit()
    await update.message.reply_text(f"🎁 {ctx.args[0]} puede usar su prueba gratuita otra vez.")


async def jugador(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMINS and creditos(uid) <= 0 and cfg("free") != "1" and cfg("unlimited") != "1":
        return await update.message.reply_text(SIN_ACCESO)
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("❌ Uso: /jugador 123456789")
    try:
        code, d = await api("GET", f"/api/player/{ctx.args[0]}")
        if code == 404:
            return await update.message.reply_text("🔎 UID no encontrado.")
        if code != 200:
            return await update.message.reply_text(error_txt(code, d))
        b = d.get("basicInfo", {})
        await update.message.reply_text(
            "╔══════════════╗\n"
            "  🎮 𝗣𝗘𝗥𝗙𝗜𝗟 𝗗𝗘𝗟 𝗝𝗨𝗚𝗔𝗗𝗢𝗥 🎮\n"
            "╚══════════════╝\n\n"
            f"👤 𝗡𝗜𝗖𝗞 ➜ {b.get('nickname')}\n"
            f"🆔 𝗨𝗜𝗗 ➜ {b.get('accountId')}\n"
            f"🌎 𝗥𝗘𝗚𝗜𝗢𝗡 ➜ {b.get('region')}\n"
            f"⭐ 𝗡𝗜𝗩𝗘𝗟 ➜ {b.get('level')}\n"
            f"❤️ 𝗟𝗜𝗞𝗘𝗦 ➜ {b.get('liked')}\n\n{BRAND}")
    except Exception:
        logging.exception("jugador")
        await update.message.reply_text("❌ No se pudo conectar con la API.")


async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    r = get_user(u.id)
    rol = "ADMIN" if u.id in ADMINS else "CLIENTE"
    await update.message.reply_text(
        "╔══════════════╗\n"
        "  📊 𝗠𝗜 𝗘𝗦𝗧𝗔𝗗𝗢 📊\n"
        "╚══════════════╝\n\n"
        f"👤 𝗨𝗦𝗨𝗔𝗥𝗜𝗢 ➜ {('@' + u.username) if u.username else u.first_name}\n"
        f"🆔 𝗜𝗗 ➜ {u.id}\n"
        f"🏷️ 𝗥𝗢𝗟 ➜ {rol}\n"
        f"💳 𝗖𝗥𝗘́𝗗𝗜𝗧𝗢𝗦 ➜ {'∞' if u.id in ADMINS else creditos(u.id)}\n"
        f"🎁 𝗨𝗦𝗢𝗦 𝗚𝗥𝗔𝗧𝗜𝗦 𝗛𝗢𝗬 ➜ {usos_hoy(r)}/{FREE_DAILY}\n\n"
        f"🔌 Bot: {'🟢 ON' if cfg('bot_on', '1') == '1' else '🔴 OFF'}\n"
        f"🎁 Modo gratis: {'ON' if cfg('free') == '1' else 'OFF'}\n"
        f"♾️ Modo ilimitado: {'ON' if cfg('unlimited') == '1' else 'OFF'}\n\n{BRAND}")


async def saldo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    # Clientes (y admin en grupos): sus créditos
    if uid not in ADMINS or update.effective_chat.type != "private":
        if uid in ADMINS:
            return await update.message.reply_text("♾️ Eres admin: sin límite de créditos.")
        return await update.message.reply_text(
            "╔══════════════╗\n"
            "  💳 𝗠𝗜𝗦 𝗖𝗥𝗘́𝗗𝗜𝗧𝗢𝗦 💳\n"
            "╚══════════════╝\n\n"
            f"✅ 𝗗𝗜𝗦𝗣𝗢𝗡𝗜𝗕𝗟𝗘𝗦 ➜ {creditos(uid)} likes\n\n"
            f"🛒 Recargar ➜ {OWNER}\n\n{BRAND}")
    # Admin en privado: saldo de la API
    try:
        code, d = await api("GET", "/api/balance")
        if code != 200:
            return await update.message.reply_text(error_txt(code, d))
        exp = d.get("expiry_date") or "En regla ✅"
        deuda = db.execute("SELECT COALESCE(SUM(credits),0) s FROM users").fetchone()["s"]
        await update.message.reply_text(
            "╔══════════════╗\n"
            "  💳 𝗦𝗔𝗟𝗗𝗢 𝗗𝗘 𝗟𝗔 𝗔𝗣𝗜 💳\n"
            "╚══════════════╝\n\n"
            f"✅ 𝗗𝗜𝗦𝗣𝗢𝗡𝗜𝗕𝗟𝗘𝗦 ➜ {d['remaining']}\n"
            f"📦 𝗧𝗢𝗧𝗔𝗟 𝗔𝗡̃𝗔𝗗𝗜𝗗𝗢 ➜ {d['stock_limit']}\n"
            f"📉 𝗨𝗦𝗔𝗗𝗢𝗦 ➜ {d['stock_used']}\n"
            f"👥 𝗖𝗥𝗘́𝗗𝗜𝗧𝗢𝗦 𝗩𝗘𝗡𝗗𝗜𝗗𝗢𝗦 ➜ {deuda}\n"
            f"⏰ 𝗘𝗫𝗣𝗜𝗥𝗔𝗖𝗜𝗢́𝗡 ➜ {exp}\n\n{BRAND}")
    except Exception:
        logging.exception("saldo")
        await update.message.reply_text("❌ No se pudo conectar con la API.")


# ================== ADMIN (GRUPO) ==================
@admin
async def add_cmd(update, ctx):
    a = ctx.args
    if len(a) != 2 or not a[0].isdigit() or int(a[0]) < 1:
        return await update.message.reply_text("❌ Uso: /add <créditos> <ID|@usuario>")
    n = int(a[0])
    uid = resolver(a[1])
    if uid is None:
        return await update.message.reply_text(
            "❌ No conozco a ese usuario. Debe escribirle /start al bot, o usa su ID numérico.")
    pago = acreditar(uid, n, "recarga", "admin")
    await update.message.reply_text(
        f"✅ +{n} créditos a {a[1]}\n💳 Saldo actual: {creditos(uid)}")
    if pago:
        try:
            await ctx.bot.send_message(
                pago, f"🎁 ¡Ganaste {REF_BONUS} créditos! Un amigo que invitaste hizo su primera recarga.\n"
                      f"💳 Saldo: {creditos(pago)}")
        except Exception:
            pass
    try:
        await ctx.bot.send_message(
            uid, f"🎉 Se acreditaron {n} créditos a tu cuenta\n"
                 f"💳 Saldo: {creditos(uid)} likes\n\nAbre /start → ❤️ Likes → Enviar likes\n\n{BRAND}")
    except Exception:
        pass


@admin
async def quitar_cmd(update, ctx):
    a = ctx.args
    if len(a) != 2 or not a[0].isdigit() or int(a[0]) < 1:
        return await update.message.reply_text("❌ Uso: /quitar <créditos> <ID|@usuario>")
    n = int(a[0])
    uid = resolver(a[1])
    if uid is None or get_user(uid) is None:
        return await update.message.reply_text("❌ Usuario no encontrado.")
    antes = creditos(uid)
    db.execute("UPDATE users SET credits=MAX(0, COALESCE(credits,0)-?) WHERE user_id=?", (n, uid))
    db.commit()
    mov(uid, creditos(uid) - antes, "ajuste", "admin")
    await update.message.reply_text(f"➖ -{n} créditos a {a[1]}\n💳 Saldo actual: {creditos(uid)}")


@admin
async def autoday(update, ctx):
    a = ctx.args
    if (len(a) not in (2, 3) or not a[0].isdigit() or not a[1].isdigit()
            or int(a[0]) < 1 or (len(a) == 3 and not a[2].isdigit())):
        return await update.message.reply_text(
            "❌ Uso: /autoday <días> <ID de Free Fire> [cantidad]\n"
            f"Ejemplo: /autoday 7 123456789 2000\n(por defecto {AUTO_AMOUNT} likes al día)")
    cantidad = int(a[2]) if len(a) == 3 else AUTO_AMOUNT
    ex = db.execute("SELECT owner_id FROM autos WHERE target_id=?", (a[1],)).fetchone()
    if ex and ex["owner_id"]:
        return await update.message.reply_text(
            "⚠️ Ese ID tiene un auto de un cliente. Cancélalo primero con /autoremove.")
    if not 1 <= cantidad <= MAX_AMOUNT:
        return await update.message.reply_text(f"❌ La cantidad debe ser de 1 a {MAX_AMOUNT}.")
    vence = (now().date() + timedelta(days=int(a[0]))).isoformat()
    db.execute(
        "INSERT OR REPLACE INTO autos(target_id, expires, last_sent, added_by, amount) "
        "VALUES(?,?,NULL,?,?)",
        (a[1], vence, update.effective_user.id, cantidad))
    db.commit()
    await update.message.reply_text(
        f"🤖 Auto activado para {a[1]}\n❤️ {cantidad} likes diarios\n"
        f"📅 Hasta: {vence}\n(se descuenta de tu saldo de la API)")


# ================== ADMIN (PRIVADO) ==================
@admin_privado
async def usersvip(update, ctx):
    rows = db.execute(
        "SELECT * FROM users WHERE credits > 0 ORDER BY credits DESC LIMIT 40").fetchall()
    if not rows:
        return await update.message.reply_text("📭 No hay clientes con créditos.")
    lines = [
        f"👤 {('@' + r['username']) if r['username'] else r['user_id']} • 💳 {r['credits']}"
        for r in rows]
    await update.message.reply_text("👥 𝗖𝗟𝗜𝗘𝗡𝗧𝗘𝗦 𝗖𝗢𝗡 𝗖𝗥𝗘́𝗗𝗜𝗧𝗢𝗦\n\n" + "\n".join(lines))


@admin_privado
async def logs_cmd(update, ctx):
    rows = db.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 10").fetchall()
    if not rows:
        return await update.message.reply_text("📭 Sin envíos registrados.")
    lines = [
        f"{'✅' if r['status'] == 'success' else '❌'} {r['target_id']} ➜ {r['likes']} • "
        f"{r['origin']} • {r['ts'][5:16].replace('T', ' ')}"
        for r in rows]
    await update.message.reply_text("📜 𝗨́𝗟𝗧𝗜𝗠𝗢𝗦 𝗘𝗡𝗩𝗜́𝗢𝗦\n\n" + "\n".join(lines))


@admin_privado
async def stats(update, ctx):
    tot = db.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(likes),0) l FROM logs WHERE status='success'").fetchone()
    h = db.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(likes),0) l FROM logs "
        "WHERE status='success' AND ts LIKE ?", (hoy() + "%",)).fetchone()
    users = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    clientes = db.execute("SELECT COUNT(*) c FROM users WHERE credits > 0").fetchone()["c"]
    pend = db.execute("SELECT COALESCE(SUM(credits),0) s FROM users").fetchone()["s"]
    autos = db.execute("SELECT COUNT(*) c FROM autos WHERE owner_id IS NOT NULL OR expires >= ?", (hoy(),)).fetchone()["c"]
    await update.message.reply_text(
        "╔══════════════╗\n"
        "  📊 𝗘𝗦𝗧𝗔𝗗𝗜́𝗦𝗧𝗜𝗖𝗔𝗦 📊\n"
        "╚══════════════╝\n\n"
        f"📅 𝗛𝗢𝗬 ➜ {h['c']} envíos • {h['l']} likes\n"
        f"📈 𝗧𝗢𝗧𝗔𝗟 ➜ {tot['c']} envíos • {tot['l']} likes\n"
        f"👥 𝗨𝗦𝗨𝗔𝗥𝗜𝗢𝗦 ➜ {users}\n"
        f"💳 𝗖𝗟𝗜𝗘𝗡𝗧𝗘𝗦 𝗖𝗢𝗡 𝗦𝗔𝗟𝗗𝗢 ➜ {clientes}\n"
        f"🧮 𝗖𝗥𝗘́𝗗𝗜𝗧𝗢𝗦 𝗣𝗘𝗡𝗗𝗜𝗘𝗡𝗧𝗘𝗦 ➜ {pend}\n"
        f"🤖 𝗔𝗨𝗧𝗢𝗦 ➜ {autos}\n\n{BRAND}")


@admin_privado
async def autolist(update, ctx):
    rows = db.execute(
        "SELECT * FROM autos WHERE owner_id IS NOT NULL OR expires >= ? ORDER BY expires",
        (hoy(),)).fetchall()
    if not rows:
        return await update.message.reply_text("📭 No hay IDs automáticos activos.")
    lines = []
    for r in rows:
        if r["owner_id"]:
            resto = f"cliente {r['owner_id']} • quedan {r['restantes']}d"
        else:
            dias = (datetime.fromisoformat(r["expires"]).date() - now().date()).days
            resto = f"quedan {dias}d"
        lines.append(
            f"🤖 {r['target_id']} • ❤️ {r['amount'] or AUTO_AMOUNT}/día • {resto} • "
            f"último: {r['last_sent'] or '—'}")
    await update.message.reply_text("🤖 𝗜𝗗𝗦 𝗔𝗨𝗧𝗢\n\n" + "\n".join(lines))


@admin_privado
async def autoremove(update, ctx):
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("❌ Uso: /autoremove <ID>")
    t = ctx.args[0]
    if t in EN_PROCESO:
        return await update.message.reply_text("⏳ Se está enviando ahora mismo, intenta en un minuto.")
    a, reembolso = cancelar_auto(t)
    if not a:
        return await update.message.reply_text("🔎 Ese ID no estaba en los autos.")
    txt = "🗑️ ID eliminado de los autos."
    if a["owner_id"]:
        txt += f"\n💳 Se devolvieron {reembolso} créditos al cliente {a['owner_id']}."
        try:
            await ctx.bot.send_message(
                a["owner_id"], f"🗑️ Tu auto del ID {t} fue cancelado. Se devolvieron {reembolso} créditos.")
        except Exception:
            pass
    await update.message.reply_text(txt)


@admin_privado
async def historial(update, ctx):
    try:
        code, d = await api("GET", "/api/logs", params={"limit": 10, "days": 7})
        if code != 200:
            return await update.message.reply_text(error_txt(code, d))
        if not d:
            return await update.message.reply_text("📭 Sin envíos recientes.")
        lines = [
            f"{'✅' if x['status'] == 'success' else '❌'} {x['target_id']} ➜ "
            f"{x['likes_sent']} • {x['timestamp'][5:16].replace('T', ' ')}" for x in d]
        await update.message.reply_text("📜 𝗛𝗜𝗦𝗧𝗢𝗥𝗜𝗔𝗟 𝗔𝗣𝗜\n\n" + "\n".join(lines))
    except Exception:
        logging.exception("historial")
        await update.message.reply_text("❌ No se pudo conectar con la API.")


# ================== AUTOS DIARIOS ==================
async def correr_autos(app):
    if cfg("bot_on", "1") == "0":
        return
    rows = db.execute(
        "SELECT * FROM autos WHERE (owner_id IS NOT NULL OR expires >= ?) "
        "AND (last_sent IS NULL OR last_sent < ?)", (hoy(), hoy())).fetchall()
    for a in rows:
        tid = a["target_id"]
        if tid in EN_PROCESO:
            continue
        owner = a["owner_id"]
        cantidad = a["amount"] or AUTO_AMOUNT
        EN_PROCESO.add(tid)
        try:
            code, d, region, secs = await enviar(tid, cantidad)
        except Exception:
            logging.exception("auto")
            break
        finally:
            EN_PROCESO.discard(tid)
        if code in (429, 503):   # se reintenta en el siguiente ciclo
            break
        ok = code == 200
        enviados = (d.get("likes_sent", 0) or 0) if ok else 0
        quien = owner or a["added_by"]
        log_send(quien, tid, d.get("player_nickname") if ok else None, enviados,
                 "success" if ok else "error", "auto")
        db.execute("UPDATE autos SET last_sent=? WHERE target_id=?", (hoy(), tid))
        db.commit()
        extra = ""
        if owner:   # auto de cliente: se devuelve lo no enviado y se descuenta un día
            devolver(owner, cantidad - enviados, f"auto ID {tid}: no enviados")
            rest = (a["restantes"] or 1) - 1
            if rest <= 0:
                db.execute("DELETE FROM autos WHERE target_id=?", (tid,))
                extra = "\n🏁 Tu auto terminó."
            else:
                db.execute("UPDATE autos SET restantes=? WHERE target_id=?", (rest, tid))
                extra = f"\n📅 Días restantes: {rest}"
            db.commit()
        try:
            await app.bot.send_message(
                quien,
                f"🤖 Auto {tid}: " +
                (f"✅ {enviados} likes enviados" if ok else error_txt(code, d)) + extra)
        except Exception:
            pass
        await asyncio.sleep(3)


async def avisar_admin(bot, texto):
    if ADMINS:
        try:
            await bot.send_message(ADMINS[0], texto)
        except Exception:
            logging.exception("avisar_admin")


async def hacer_backup(bot):
    """Manda una copia de la base de datos al admin principal."""
    if not ADMINS:
        return False
    ruta = f"backup_{now():%Y%m%d_%H%M}.db"
    try:
        dst = sqlite3.connect(ruta)
        db.backup(dst)
        dst.close()
        with open(ruta, "rb") as f:
            await bot.send_document(ADMINS[0], f, filename=ruta, caption="🗄️ Backup de la base de datos")
        return True
    except Exception:
        logging.exception("backup")
        return False
    finally:
        try:
            os.remove(ruta)
        except OSError:
            pass


async def texto_reporte():
    h = hoy()
    env = db.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(likes),0) l FROM logs WHERE status='success' AND ts LIKE ?",
        (h + "%",)).fetchone()
    vend = db.execute(
        "SELECT COALESCE(SUM(delta),0) s FROM movimientos WHERE tipo IN ('recarga','codigo') AND ts LIKE ?",
        (h + "%",)).fetchone()["s"]
    nuevos = db.execute("SELECT COUNT(*) c FROM users WHERE created LIKE ?", (h + "%",)).fetchone()["c"]
    clientes = db.execute("SELECT COUNT(*) c FROM users WHERE credits > 0").fetchone()["c"]
    pend = db.execute("SELECT COALESCE(SUM(credits),0) s FROM users").fetchone()["s"]
    api_saldo = "N/D"
    try:
        code, d = await api("GET", "/api/balance")
        if code == 200:
            api_saldo = d.get("remaining", "N/D")
    except Exception:
        pass
    return (
        "╔══════════════╗\n"
        "  📋 𝗥𝗘𝗣𝗢𝗥𝗧𝗘 𝗗𝗘𝗟 𝗗𝗜́𝗔\n"
        "╚══════════════╝\n\n"
        f"📅 {h}\n"
        f"✅ 𝗘𝗡𝗩𝗜́𝗢𝗦 ➜ {env['c']} • {env['l']} likes\n"
        f"💰 𝗖𝗥𝗘́𝗗𝗜𝗧𝗢𝗦 𝗩𝗘𝗡𝗗𝗜𝗗𝗢𝗦 ➜ {vend}\n"
        f"🆕 𝗨𝗦𝗨𝗔𝗥𝗜𝗢𝗦 𝗡𝗨𝗘𝗩𝗢𝗦 ➜ {nuevos}\n"
        f"👥 𝗖𝗟𝗜𝗘𝗡𝗧𝗘𝗦 𝗖𝗢𝗡 𝗦𝗔𝗟𝗗𝗢 ➜ {clientes}\n"
        f"🧮 𝗖𝗥𝗘́𝗗𝗜𝗧𝗢𝗦 𝗣𝗘𝗡𝗗𝗜𝗘𝗡𝗧𝗘𝗦 ➜ {pend}\n"
        f"💳 𝗦𝗔𝗟𝗗𝗢 𝗔𝗣𝗜 ➜ {api_saldo}"
    )


async def revisar_alertas(bot):
    code, d = await api("GET", "/api/balance")
    if code != 200:
        return
    rem = d.get("remaining", 0) or 0
    pend = db.execute("SELECT COALESCE(SUM(credits),0) s FROM users").fetchone()["s"]
    avisos = []
    if rem < ALERTA_SALDO_API:
        avisos.append(f"📉 Tu saldo de la API es bajo: {rem} créditos.")
    if rem < pend:
        avisos.append(f"⚠️ Tus clientes tienen {pend} créditos pendientes y tu saldo de la API es {rem}.")
    if d.get("expiry_date"):
        avisos.append(f"⏰ Tu cuenta de la API expira el {d['expiry_date']} si no recibe créditos.")
    if avisos and cfg("alerta_fecha", "") != hoy():   # máximo un aviso por día
        await avisar_admin(bot, "🚨 𝗔𝗟𝗘𝗥𝗧𝗔\n\n" + "\n".join(avisos))
        set_cfg("alerta_fecha", hoy())


async def mantenimiento_loop(app):
    """Backup diario, reporte diario y alertas de saldo."""
    ultima_alerta = 0.0
    while True:
        try:
            n = now()
            if n.hour >= 3 and cfg("ultimo_backup", "") != hoy():
                if await hacer_backup(app.bot):
                    set_cfg("ultimo_backup", hoy())
            if n.hour == 23 and n.minute >= 55 and cfg("ultimo_reporte", "") != hoy():
                await avisar_admin(app.bot, await texto_reporte())
                set_cfg("ultimo_reporte", hoy())
            if time.time() - ultima_alerta > 1800:
                ultima_alerta = time.time()
                await revisar_alertas(app.bot)
        except Exception:
            logging.exception("mantenimiento")
        await asyncio.sleep(60)


async def auto_loop(app):
    while True:
        try:
            await correr_autos(app)
        except Exception:
            logging.exception("auto_loop")
        await asyncio.sleep(60)


async def post_init(app):
    app.create_task(auto_loop(app))
    app.create_task(mantenimiento_loop(app))
    app.create_task(craft_loop(app))


# ================== MAIN ==================
# ================== MENÚ POR BOTONES ==================
def _creditos_txt(uid):
    return "∞" if uid in ADMINS else f"{creditos(uid):,}"


def kb_menu_principal(uid=None):
    B = InlineKeyboardButton
    filas = [
        [B("❤️ Likes Free Fire", callback_data="m|likes")],
        [B("🎨 Nick Color Craftland", callback_data="m|craft")],
        [B("🛒 Comprar likes", callback_data="m|comprar"), B("💰 Precios", callback_data="m|precios")],
        [B("💳 Mi cuenta", callback_data="m|cuenta"), B("🎁 Prueba gratis", callback_data="m|prueba")],
        [B("🏆 Top clientes", callback_data="m|top")],
        [B("🆘 Ayuda y soporte", callback_data="m|ayuda"), B("🤝 Revendedores", callback_data="m|revendedor")],
    ]
    if uid in ADMINS:
        filas.append([B("👑 Panel admin", callback_data="ad|panel")])
    return InlineKeyboardMarkup(filas)


def texto_menu(uid):
    return (
        "╔══════════════╗\n"
        "  🤖 𝗖𝗜𝗕𝗘𝗥 𝗕𝗢𝗧 • 𝗠𝗘𝗡𝗨́\n"
        "╚══════════════╝\n\n"
        f"💳 Tus créditos: {_creditos_txt(uid)}  (1 crédito = 1 like)\n\n"
        "Elige una opción 👇")


PEDIR = {
    "likes_enviar": ("likes_id", "❤️ Envía el ID de Free Fire que recibirá los likes (solo números):"),
    "prueba": ("prueba_id", f"🎁 Prueba gratis de {PRUEBA_LIKES} likes.\nEnvía el ID de Free Fire (solo números):"),
    "jugador": ("jugador_id", "🔎 Envía el ID del jugador que quieres consultar (solo números):"),
    "craft_comprar": ("craft_id", "🎨 Envía el Player ID de Free Fire que recibirá el pack (solo números):"),
    "canjear": ("canjear", "🎟 Envía tu código de recarga:"),
    "auto_nuevo": ("auto_id", "🤖 Envía el ID de Free Fire que recibirá los likes cada día (solo números):"),
    "soporte": ("soporte", "💬 Escribe tu mensaje para soporte (solo texto) y lo enviamos. Te respondemos por aquí mismo."),
}

_ULT_SOPORTE = {}   # antispam: último mensaje de soporte por usuario

TOGGLES = {
    "bot_on": ("🔌 Bot", "1"),
    "prueba": ("🎁 Prueba gratis", "1"),
    "free": ("🆓 Modo gratis", "0"),
    "unlimited": ("♾️ Modo ilimitado", "0"),
}

ADMIN_AYUDA = (
    "ℹ️ 𝗖𝗢𝗠𝗔𝗡𝗗𝗢𝗦 𝗖𝗢𝗡 𝗗𝗔𝗧𝗢𝗦 (solo admin)\n\n"
    "/add <créditos> <ID|@usr> — Dar créditos\n"
    "/quitar <créditos> <ID|@usr> — Quitar créditos\n"
    "/ban <ID|@usr> · /unban <ID|@usr>\n"
    "/codigo <créditos> [cantidad] — Crear códigos de recarga\n"
    "/broadcast <mensaje> — Anuncio a todos\n"
    "/buscar <ID|@usr> — Ficha de un usuario\n"
    "/aprobar <orden> · /rechazar <orden>\n"
    "/resetprueba <ID|@usr> — Dar otra prueba gratis\n"
    "/autoday <días> <ID> [cantidad] — Auto con tu saldo\n"
    "/autoremove <ID> — Quitar un auto\n"
    "/msg <ID|@usr> <mensaje> — Escribirle a un cliente por el bot\n"
    "/admin — Abrir este panel"
)


def texto_admin():
    return (
        "╔══════════════╗\n"
        "  👑 𝗣𝗔𝗡𝗘𝗟 𝗔𝗗𝗠𝗜𝗡\n"
        "╚══════════════╝\n\n"
        "🟢 activado • 🔴 desactivado\n"
        "Toca un interruptor para cambiarlo.\n"
        "♾️ Modo ilimitado: todos envían sin gastar créditos.")


def kb_admin():
    B = InlineKeyboardButton
    def sw(clave):
        etiqueta, defecto = TOGGLES[clave]
        return B(f"{'🟢' if cfg(clave, defecto) == '1' else '🔴'} {etiqueta}", callback_data=f"ad|t|{clave}")
    return InlineKeyboardMarkup([
        [B("📊 Estadísticas", callback_data="ad|stats"), B("📋 Reporte del día", callback_data="ad|reporte")],
        [B("🧾 Compras", callback_data="ad|ordenes"), B("📜 Últimos envíos", callback_data="ad|logs")],
        [B("👥 Usuarios", callback_data="ad|users"), B("💳 Clientes con saldo", callback_data="ad|clientes")],
        [B("💰 Saldo likes (API)", callback_data="ad|saldo"), B("🎨 Saldo Craftland", callback_data="ad|craftsaldo")],
        [B("🎨 Pedidos Craftland", callback_data="ad|craftpedidos"), B("🤖 Autos activos", callback_data="ad|autolist")],
        [sw("bot_on"), sw("prueba")],
        [sw("free"), sw("unlimited")],
        [B("🏆 Top clientes (con usuarios)", callback_data="ad|top")],
        [B("🗄️ Backup", callback_data="ad|backup"), B("📑 Historial API", callback_data="ad|historial")],
        [B("ℹ️ Comandos con datos", callback_data="ad|ayuda")],
        [B("⬅️ Menú", callback_data="m|menu")],
    ])


def vista_autos(uid):
    B = InlineKeyboardButton
    rows = db.execute("SELECT * FROM autos WHERE owner_id=?", (uid,)).fetchall()
    texto = ("╔══════════════╗\n  🤖 𝗔𝗨𝗧𝗢𝗦 𝗗𝗜𝗔𝗥𝗜𝗢𝗦\n╚══════════════╝\n\n"
             f"Likes automáticos todos los días a un ID, hasta {AUTO_MAX_DIAS} días.\n"
             "Se cobra por adelantado y lo que no se envíe se te devuelve.\n\n")
    filas = [[B("➕ Nuevo auto", callback_data="m|auto_nuevo")]]
    if rows:
        texto += "📋 Tus autos activos:\n" + "\n".join(
            f"🆔 {r['target_id']} • ❤️ {r['amount']}/día • quedan {r['restantes']}d" for r in rows)
        for r in rows:
            filas.append([B(f"❌ Cancelar {r['target_id']}", callback_data=f"au|x|{r['target_id']}")])
    else:
        texto += "📭 No tienes autos activos."
    filas.append([B("⬅️ Volver", callback_data="m|likes")])
    return texto, InlineKeyboardMarkup(filas)


async def crear_auto_cliente(ctx, uid, chat_id, target, dias, cant):
    if uid in ADMINS:
        return await say(ctx, chat_id, "👑 Eres admin: usa /autoday.")
    if cfg("bot_on", "1") == "0":
        return await say(ctx, chat_id, "🔴 El bot está apagado por el momento.")
    if not 1 <= dias <= AUTO_MAX_DIAS or not 1 <= cant <= MAX_AMOUNT:
        return await say(ctx, chat_id, "❌ Datos del auto no válidos.")
    ex = db.execute("SELECT 1 FROM autos WHERE target_id=? AND (owner_id IS NOT NULL OR expires >= ?)",
                    (target, hoy())).fetchone()
    if ex:
        return await say(ctx, chat_id, "⚠️ Ese ID ya tiene un auto activo.")
    total = dias * cant
    if creditos(uid) < total:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Comprar likes", callback_data="m|comprar")]])
        return await say(
            ctx, chat_id,
            f"💳 Necesitas {total:,} créditos ({dias} días × {cant:,}) y tienes {creditos(uid):,}.",
            reply_markup=kb)
    if not cobrar(uid, total, f"auto {dias}d x {cant} ID {target}", "auto"):
        return await say(ctx, chat_id, "💳 Créditos insuficientes.")
    vence = (now().date() + timedelta(days=dias)).isoformat()
    db.execute(
        "INSERT OR REPLACE INTO autos(target_id, expires, last_sent, added_by, amount, owner_id, restantes) "
        "VALUES(?,?,NULL,?,?,?,?)", (target, vence, uid, cant, uid, dias))
    db.commit()
    await say(
        ctx, chat_id,
        "🤖 ¡Auto activado!\n\n"
        f"🆔 ID: {target}\n❤️ {cant:,} likes diarios\n📅 {dias} días\n"
        f"💳 Se cobraron {total:,} créditos • saldo: {creditos(uid):,}\n\n"
        "El primer envío sale en menos de un minuto. Te aviso por aquí cada día.\n"
        "Para cancelarlo: ❤️ Likes → 🤖 Autos diarios.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="m|menu")]]))


async def cancelar_auto_cliente(ctx, uid, chat_id, target):
    a = db.execute("SELECT * FROM autos WHERE target_id=?", (target,)).fetchone()
    if not a or a["owner_id"] != uid:
        return await say(ctx, chat_id, "🔎 No tienes un auto con ese ID.")
    if target in EN_PROCESO:
        return await say(ctx, chat_id, "⏳ Se está enviando ahora mismo, intenta en un minuto.")
    a, reembolso = cancelar_auto(target)
    await say(ctx, chat_id,
              f"🗑️ Auto del ID {target} cancelado.\n💳 Se devolvieron {reembolso:,} créditos • "
              f"saldo: {creditos(uid):,}")


async def editar(q, texto, kb=None):
    try:
        await q.message.edit_text(texto, reply_markup=kb)
    except Exception:
        try:
            await q.message.reply_text(texto, reply_markup=kb)
        except Exception:
            pass


class _Shim:
    """Permite reutilizar las funciones desde un botón."""
    def __init__(self, q):
        self.callback_query = q
        self.message = q.message
        self.effective_user = q.from_user
        self.effective_chat = q.message.chat


async def menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    chat_id = q.message.chat_id
    partes = (q.data or "").split("|")
    await q.answer()
    B = InlineKeyboardButton
    volver = [B("⬅️ Volver", callback_data="m|menu")]
    shim = _Shim(q)
    ctx.args = []

    # ----- Likes: cantidad elegida -----
    if partes[0] == "lk" and len(partes) == 3 and partes[1].isdigit() and partes[2].isdigit():
        return await procesar_likes(ctx, uid, chat_id, partes[1], int(partes[2]))

    # ----- Admin: responder a un cliente por el bot -----
    if partes[0] == "rp" and len(partes) == 2 and partes[1].isdigit():
        if uid not in ADMINS:
            return
        ctx.user_data["esperando"] = (f"resp:{partes[1]}", time.time())
        return await say(
            ctx, chat_id,
            f"💬 Escribe tu respuesta para el cliente {partes[1]}.\n"
            "Se la envío por el bot y tu cuenta no se muestra.",
            reply_markup=InlineKeyboardMarkup([[B("❌ Cancelar", callback_data="m|cancel")]]))

    # ----- Craftland: continuar -----
    if partes[0] == "ct":
        if partes[1:2] == ["ok"] and len(partes) == 3 and partes[2].isdigit():
            try:
                await q.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            if uid in ADMINS:
                ok, res = await craft_crear_en_proveedor(ctx.bot, uid, chat_id, partes[2])
                return await say(ctx, chat_id, f"✅ Pedido creado #{res}" if ok else f"⚠️ {res}")
            return await iniciar_compra_craft(ctx, uid, chat_id, partes[2])
        try:
            await q.message.edit_text("❌ Pedido cancelado.")
        except Exception:
            pass
        return

    # ----- Autos -----
    if partes[0] == "au":
        sub = partes[1] if len(partes) > 1 else ""
        if sub == "d" and len(partes) == 4 and partes[2].isdigit() and partes[3].isdigit():
            tid, dias = partes[2], int(partes[3])
            filas = [[B(f"❤️ {a:,} por día • {dias * a:,} en total", callback_data=f"au|a|{tid}|{dias}|{a}")]
                     for a in (500, 1000, 2000)]
            filas.append([B("❌ Cancelar", callback_data="m|autos")])
            return await editar(q, f"🤖 Auto para el ID {tid}\n📅 {dias} días\n\n¿Cuántos likes por día?",
                                InlineKeyboardMarkup(filas))
        if sub in ("a", "ok") and len(partes) == 5 and all(x.isdigit() for x in partes[2:]):
            tid, dias, cant = partes[2], int(partes[3]), int(partes[4])
            if sub == "a":
                total = dias * cant
                return await editar(
                    q,
                    "🤖 𝗖𝗢𝗡𝗙𝗜𝗥𝗠𝗔 𝗧𝗨 𝗔𝗨𝗧𝗢\n\n"
                    f"🆔 ID: {tid}\n📅 {dias} días\n❤️ {cant:,} likes por día\n"
                    f"💳 Se cobran {total:,} créditos por adelantado (tienes {creditos(uid):,})\n"
                    "↩️ Lo que no se envíe se te devuelve.",
                    InlineKeyboardMarkup([[B("✅ Confirmar", callback_data=f"au|ok|{tid}|{dias}|{cant}"),
                                           B("❌ Cancelar", callback_data="m|autos")]]))
            try:
                await q.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return await crear_auto_cliente(ctx, uid, chat_id, tid, dias, cant)
        if sub == "x" and len(partes) == 3 and partes[2].isdigit():
            return await cancelar_auto_cliente(ctx, uid, chat_id, partes[2])
        return

    # ----- Panel admin -----
    if partes[0] == "ad":
        if uid not in ADMINS:
            return
        sub = partes[1] if len(partes) > 1 else "panel"
        if sub == "panel":
            return await editar(q, texto_admin(), kb_admin())
        if sub == "t" and len(partes) == 3 and partes[2] in TOGGLES:
            clave = partes[2]
            nuevo = "0" if cfg(clave, TOGGLES[clave][1]) == "1" else "1"
            set_cfg(clave, nuevo)
            return await editar(q, texto_admin(), kb_admin())
        acciones = {
            "stats": stats, "reporte": reporte_cmd, "ordenes": ordenes_cmd, "logs": logs_cmd,
            "users": users_cmd, "clientes": usersvip, "saldo": saldo, "craftsaldo": craftsaldo_cmd,
            "craftpedidos": craftpedidos_cmd, "autolist": autolist, "backup": backup_cmd,
            "historial": historial,
        }
        if sub == "top":
            return await say(ctx, chat_id, texto_top_admin())
        if sub in acciones:
            return await acciones[sub](shim, ctx)
        if sub == "ayuda":
            return await say(ctx, chat_id, ADMIN_AYUDA)
        return

    # ----- Navegación -----
    accion = partes[1] if len(partes) > 1 else "menu"
    if accion in ("menu", "cancel"):
        ctx.user_data.pop("esperando", None)
        return await editar(q, texto_menu(uid), kb_menu_principal(uid))

    if accion == "likes":
        return await editar(
            q,
            "╔══════════════╗\n  ❤️ 𝗟𝗜𝗞𝗘𝗦 𝗙𝗥𝗘𝗘 𝗙𝗜𝗥𝗘\n╚══════════════╝\n\n"
            f"▫️ 1 crédito = 1 like • Máximo {MAX_AMOUNT:,} por envío\n"
            f"💳 Tus créditos: {_creditos_txt(uid)}\n\nElige una opción 👇",
            InlineKeyboardMarkup([
                [B("❤️ Enviar likes", callback_data="m|likes_enviar")],
                [B("🤖 Autos diarios", callback_data="m|autos"), B("🔎 Perfil de jugador", callback_data="m|jugador")],
                [B("🛒 Comprar likes", callback_data="m|comprar"), B("🎁 Prueba gratis", callback_data="m|prueba")],
                volver]))

    if accion == "craft":
        if not CRAFT_KEY:
            estado = "🚧 No disponible por ahora"
        else:
            disp = await craft_stock()
            estado = "✅ Disponible" if disp else ("📦 Sin stock por ahora" if disp is False else "✅ Disponible")
        return await editar(
            q,
            "╔══════════════╗\n  🎨 𝗡𝗜𝗖𝗞 𝗖𝗢𝗟𝗢𝗥 𝗖𝗥𝗔𝗙𝗧𝗟𝗔𝗡𝗗\n╚══════════════╝\n\n"
            f"▫️ Pack Nivel 6 • +{CRAFT_SEGUIDORES:,} seguidores\n"
            f"▫️ Precio: ${CRAFT_PRECIO_MXN} MXN\n"
            "▫️ Pago: transferencia bancaria\n"
            f"▫️ Estado: {estado}\n\nElige una opción 👇",
            InlineKeyboardMarkup([
                [B(f"🎨 Comprar ahora • ${CRAFT_PRECIO_MXN} MXN", callback_data="m|craft_comprar")],
                [B("📦 Mis pedidos", callback_data="m|craft_pedidos")],
                volver]))

    if accion == "cuenta":
        return await editar(
            q,
            "╔══════════════╗\n  💳 𝗠𝗜 𝗖𝗨𝗘𝗡𝗧𝗔\n╚══════════════╝\n\n"
            f"💳 Créditos: {_creditos_txt(uid)}  (1 crédito = 1 like)",
            InlineKeyboardMarkup([
                [B("📜 Movimientos", callback_data="m|mov"), B("👥 Invitar amigos", callback_data="m|ref")],
                [B("🎟 Canjear código", callback_data="m|canjear"), B("📊 Mi estado", callback_data="m|status")],
                [B("🛒 Comprar likes", callback_data="m|comprar")],
                volver]))

    if accion == "autos":
        texto, kb = vista_autos(uid)
        return await editar(q, texto, kb)

    if accion in ("top", "top_mes"):
        texto, kb = vista_top(uid, mes=(accion == "top_mes"))
        return await editar(q, texto, kb)

    if accion == "revendedor":
        return await editar(
            q,
            "╔══════════════╗\n  🤝 𝗥𝗘𝗩𝗘𝗡𝗗𝗘𝗗𝗢𝗥𝗘𝗦\n╚══════════════╝\n\n"
            "🔎 Se buscan revendedores de likes.\n\n"
            f"💎 Pedidos de {PEDIDO_GRANDE:,} likes en adelante: mejoramos el precio.\n"
            "⚡ Entrega automática\n"
            "🧾 Referencia de cada envío en nuestro canal\n"
            "🏦 Pago por transferencia\n\n"
            f"Toca el botón y te contactamos, o escríbenos al {PHONE}.",
            InlineKeyboardMarkup([
                [B("📩 Quiero ser revendedor / pedido grande", callback_data="m|rev_solicitar")],
                [B("💬 Escribir a soporte", callback_data="m|soporte")],
                [B("💰 Ver precios", callback_data="m|precios")],
                volver]))

    if accion == "rev_solicitar":
        menu_kb = InlineKeyboardMarkup([[B("💬 Escribir a soporte", callback_data="m|soporte")],
                                        [B("⬅️ Menú", callback_data="m|menu")]])
        clave = f"rev_{uid}"
        if cfg(clave, "") == hoy():
            return await say(ctx, chat_id, "✅ Ya recibimos tu solicitud hoy. Te contactamos pronto.",
                             reply_markup=menu_kb)
        u = q.from_user
        usuario = f"@{u.username}" if u.username else "sin usuario"
        enviado = False
        if ADMINS:
            try:
                await ctx.bot.send_message(
                    ADMINS[0],
                    "🤝 <b>Solicitud de revendedor / pedido grande</b>\n\n"
                    f"👤 <a href=\"tg://user?id={uid}\">{html.escape(u.first_name or 'Cliente')}</a>\n"
                    f"🆔 {uid}\n🔗 {html.escape(usuario)}",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        [[B("💬 Responder por el bot", callback_data=f"rp|{uid}")]]))
                enviado = True
            except Exception:
                logging.exception("solicitud revendedor")
        if not enviado:
            return await say(ctx, chat_id, f"⚠️ No pudimos enviar tu solicitud. Escríbenos al {PHONE}.",
                             reply_markup=menu_kb)
        set_cfg(clave, hoy())
        return await say(
            ctx, chat_id,
            f"✅ ¡Solicitud enviada!\n\nTe contactamos pronto. También puedes escribirnos al {PHONE}.",
            reply_markup=menu_kb)

    if accion in PEDIR:
        tipo, pregunta = PEDIR[accion]
        ctx.user_data["esperando"] = (tipo, time.time())
        return await say(ctx, chat_id, pregunta,
                         reply_markup=InlineKeyboardMarkup([[B("❌ Cancelar", callback_data="m|cancel")]]))

    simples = {"comprar": comprar, "ayuda": ayuda, "mov": movimientos, "ref": referidos,
               "precios": precios, "status": status}
    if accion in simples:
        return await simples[accion](shim, ctx)
    if accion == "craft_pedidos":
        return await craft_mis_pedidos(ctx, uid, chat_id)


async def texto_libre(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Recibe el dato que el menú le pidió al cliente (ID, código...)."""
    m = update.message
    if m is None or not m.text:
        return
    uid = update.effective_user.id
    est = ctx.user_data.get("esperando")
    if not est or time.time() - est[1] > 300:
        ctx.user_data.pop("esperando", None)
        return await m.reply_text("👇 Usa el menú para empezar.", reply_markup=kb_menu_principal(uid))

    tipo = est[0]
    txt = m.text.strip()
    B = InlineKeyboardButton

    # El cliente le escribe a soporte
    if tipo == "soporte":
        ctx.user_data.pop("esperando", None)
        ahora = time.time()
        if ahora - _ULT_SOPORTE.get(uid, 0) < 10:
            return await m.reply_text("⏳ Espera unos segundos antes de enviar otro mensaje.")
        _ULT_SOPORTE[uid] = ahora
        u = update.effective_user
        usuario = f"@{u.username}" if u.username else "sin usuario"
        enviado = False
        for adm in ADMINS:
            try:
                await ctx.bot.send_message(
                    adm,
                    "💬 <b>Mensaje de soporte</b>\n\n"
                    f"👤 {html.escape(u.first_name or 'Cliente')} • 🆔 {uid} • {html.escape(usuario)}\n\n"
                    f"{html.escape(txt[:1500])}",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        [[B("💬 Responder por el bot", callback_data=f"rp|{uid}")]]))
                enviado = True
            except Exception:
                logging.exception("mensaje a soporte")
        if not enviado:
            return await m.reply_text(f"⚠️ No pudimos enviar tu mensaje. Escríbenos al {PHONE}.")
        return await m.reply_text(
            "✅ Mensaje enviado a soporte. Te respondemos por aquí mismo.",
            reply_markup=InlineKeyboardMarkup([[B("⬅️ Menú", callback_data="m|menu")]]))

    # El admin responde a un cliente (sin mostrar su cuenta)
    if tipo.startswith("resp:") and uid in ADMINS:
        ctx.user_data.pop("esperando", None)
        destino = int(tipo.split(":")[1])
        try:
            await ctx.bot.send_message(
                destino, f"💬 𝗦𝗢𝗣𝗢𝗥𝗧𝗘\n\n{txt[:3500]}\n\n{BRAND}",
                reply_markup=InlineKeyboardMarkup([[B("💬 Responder a soporte", callback_data="m|soporte")]]))
        except Exception:
            return await m.reply_text("❌ No pude enviarle el mensaje (puede que haya bloqueado el bot).")
        return await m.reply_text(
            "✅ Enviado al cliente.",
            reply_markup=InlineKeyboardMarkup([[B("💬 Seguir respondiendo", callback_data=f"rp|{destino}")]]))

    if tipo != "canjear" and not txt.isdigit():
        return await m.reply_text("❌ Envía solo el ID, con números. (O toca ❌ Cancelar)")
    ctx.user_data.pop("esperando", None)
    chat_id = update.effective_chat.id

    if tipo == "likes_id":
        montos = [100, 500, 1000, 2000]
        return await say(
            ctx, chat_id, f"❤️ ID {txt}\n¿Cuántos likes quieres enviar?",
            reply_markup=InlineKeyboardMarkup([
                [B(f"{a:,}", callback_data=f"lk|{txt}|{a}") for a in montos[:2]],
                [B(f"{a:,}", callback_data=f"lk|{txt}|{a}") for a in montos[2:]],
                [B("⬅️ Menú", callback_data="m|menu")]]))
    if tipo == "auto_id":
        return await say(
            ctx, chat_id, f"🤖 Auto para el ID {txt}\n\n¿Por cuántos días?",
            reply_markup=InlineKeyboardMarkup([
                [B(f"{d} días", callback_data=f"au|d|{txt}|{d}") for d in (3, 7)],
                [B(f"{d} días", callback_data=f"au|d|{txt}|{d}") for d in (15, 30)],
                [B("❌ Cancelar", callback_data="m|autos")]]))
    if tipo == "craft_id":
        return await craft_preview(ctx, uid, chat_id, txt)
    ctx.args = [txt]
    if tipo == "prueba_id":
        return await prueba_cmd(update, ctx)
    if tipo == "jugador_id":
        return await jugador(update, ctx)
    if tipo == "canjear":
        return await canjear(update, ctx)


@solo_admin
async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(texto_admin(), reply_markup=kb_admin())


# ================== TOP DE CLIENTES (anónimo) ==================
def alias_cliente(uid):
    """Nombre anónimo y estable (no revela el @ ni el ID del cliente)."""
    sal = cfg("top_salt", "")
    if not sal:
        sal = secrets.token_hex(8)
        set_cfg("top_salt", sal)
    return "Cliente " + hashlib.sha256(f"{sal}:{uid}".encode()).hexdigest()[:4].upper()


def top_likes(mes=False):
    """Likes enviados por cliente (sin admins, sin bloqueados, sin pruebas gratis)."""
    consulta = ("SELECT user_id, SUM(likes) AS total FROM logs "
                "WHERE status='success' AND origin IN ('manual','auto')")
    params = ()
    if mes:
        consulta += " AND ts LIKE ?"
        params = (f"{now():%Y-%m}%",)
    consulta += " GROUP BY user_id HAVING total > 0 ORDER BY total DESC, user_id"
    out = []
    for r in db.execute(consulta, params).fetchall():
        u = get_user(r["user_id"])
        if r["user_id"] in ADMINS or (u and (u["banned"] or 0)):
            continue
        out.append({"user_id": r["user_id"], "total": r["total"]})
    return out


def vista_top(uid, mes=False, limite=10):
    B = InlineKeyboardButton
    ranking = top_likes(mes)
    medallas = ["🥇", "🥈", "🥉"]
    titulo = "📅 𝗧𝗢𝗣 𝗗𝗘𝗟 𝗠𝗘𝗦" if mes else "🏆 𝗧𝗢𝗣 𝗖𝗟𝗜𝗘𝗡𝗧𝗘𝗦"
    texto = (f"╔══════════════╗\n  {titulo}\n╚══════════════╝\n\n"
             "Likes enviados por cliente\n\n")
    if not ranking:
        texto += "📭 Aún no hay envíos este periodo. ¡Sé el primero!\n"
    for i, r in enumerate(ranking[:limite], 1):
        marca = medallas[i - 1] if i <= 3 else f"{i}."
        tu = "  👈 tú" if r["user_id"] == uid else ""
        texto += f"{marca} {alias_cliente(r['user_id'])} — {r['total']:,} likes{tu}\n"
    pos = next((i for i, r in enumerate(ranking, 1) if r["user_id"] == uid), None)
    if pos and pos > limite:
        texto += f"\n📍 Tu posición: #{pos} — {ranking[pos - 1]['total']:,} likes\n"
    elif not pos and uid not in ADMINS:
        texto += "\n📍 Aún no apareces. Envía likes para entrar al ranking.\n"
    texto += "\n🔒 Los nombres de los clientes son privados."
    kb = InlineKeyboardMarkup([
        [B("🏆 Total" if mes else "• 🏆 Total •", callback_data="m|top"),
         B("• 📅 Este mes •" if mes else "📅 Este mes", callback_data="m|top_mes")],
        [B("🛒 Comprar likes", callback_data="m|comprar")],
        [B("⬅️ Menú", callback_data="m|menu")]])
    return texto, kb


def texto_top_admin(limite=15):
    ranking = top_likes(False)[:limite]
    if not ranking:
        return "📭 Aún no hay envíos de clientes."
    lineas = []
    for i, r in enumerate(ranking, 1):
        u = get_user(r["user_id"])
        nombre = f"@{u['username']}" if u and u["username"] else "sin usuario"
        lineas.append(f"{i}. {nombre} • 🆔 {r['user_id']} • {r['total']:,} likes")
    return "🏆 𝗧𝗢𝗣 𝗖𝗟𝗜𝗘𝗡𝗧𝗘𝗦 (solo admin)\n\n" + "\n".join(lineas)


@admin
async def msg_cmd(update, ctx):
    partes = (update.message.text or "").split(maxsplit=2)
    if len(partes) < 3:
        return await update.message.reply_text("❌ Uso: /msg <ID|@usuario> <mensaje>")
    destino = resolver(partes[1])
    if destino is None or get_user(destino) is None:
        return await update.message.reply_text("❌ Usuario no encontrado.")
    try:
        await ctx.bot.send_message(
            destino, f"💬 𝗦𝗢𝗣𝗢𝗥𝗧𝗘\n\n{partes[2][:3500]}\n\n{BRAND}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("💬 Responder a soporte", callback_data="m|soporte")]]))
    except Exception:
        return await update.message.reply_text("❌ No pude enviarle el mensaje (puede que haya bloqueado el bot).")
    await update.message.reply_text("✅ Enviado al cliente.")


def main():
    asyncio.set_event_loop(asyncio.new_event_loop())  # compatibilidad Python 3.14
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).concurrent_updates(True).build()

    app.add_handler(TypeHandler(Update, registrar), group=-1)

    handlers = {
        "start": start, "admin": admin_cmd,
        # Comandos de admin que necesitan datos (el resto va por botones):
        "add": add_cmd, "quitar": quitar_cmd, "ban": ban_cmd, "unban": unban_cmd,
        "codigo": codigo_cmd, "broadcast": broadcast, "buscar": buscar,
        "aprobar": aprobar_cmd, "rechazar": rechazar_cmd, "resetprueba": resetprueba_cmd,
        "autoday": autoday, "autoremove": autoremove, "msg": msg_cmd,
    }
    for nombre, fn in handlers.items():
        app.add_handler(CommandHandler(nombre, fn))

    app.add_handler(CallbackQueryHandler(menu_cb, pattern=r"^(m|lk|ct|au|ad|rp)\|"))
    app.add_handler(CallbackQueryHandler(botones))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, texto_libre))
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Document.ALL) & filters.ChatType.PRIVATE, recibir_comprobante))
    app.run_polling()


if __name__ == "__main__":
    main()
