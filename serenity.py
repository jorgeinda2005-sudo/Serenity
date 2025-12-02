import os
import sys
import json
import re
import sqlite3
import tempfile
import logging
from datetime import datetime, timedelta

from dotenv import load_dotenv
from html import escape
from openai import OpenAI
from twilio.rest import Client as TwilioClient

import pandas as pd
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

db_file = os.path.join(BASE_DIR, "serenity.db")
JSON_PATH = os.path.join(BASE_DIR, "info.json")

TOKEN_TELEGRAM = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_PASS = os.getenv("GMAIL_PASS", "")
TWILIO_SID = os.getenv("TWILIO_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")
TWILIO_WHATSAPP_TO = os.getenv("TWILIO_WHATSAPP_TO", "")

COOLDOWN_DIAS = int(os.getenv("COOLDOWN_DIAS_INFO", "3"))
LINK_UNACAR = os.getenv(
    "LINK_UNACAR",
    "https://www.unacar.mx/unacar/Documentos/DIRECTORIO_COMPLETO_MOD_08_06_23.pdf",
)

RISK_COOLDOWN_MINUTES = 0

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID and TWILIO_TOKEN else None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("serenity")


def crear_base_datos():
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY,
            user_name TEXT,
            ultima_alerta TEXT,
            UNIQUE(user_name)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            user_message TEXT,
            bot_message TEXT,
            timestamp TEXT,
            FOREIGN KEY (user_id) REFERENCES usuarios (id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS datos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            nombre TEXT,
            numero TEXT,
            correo_institucional TEXT,
            fecha TEXT,
            facultad TEXT,
            FOREIGN KEY (user_id) REFERENCES usuarios (id)
        )
    """)

    try:
        cursor.execute("ALTER TABLE datos ADD COLUMN facultad TEXT")
    except Exception:
        pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS dependencias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            nivel_dependencia TEXT,
            puntaje_total INTEGER DEFAULT 0,
            ultima_evaluacion TEXT,
            contador_mensajes INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES usuarios (id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS historial_dependencias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            nivel_dependencia TEXT,
            puntaje_total INTEGER DEFAULT 0,
            fecha_evaluacion TEXT,
            FOREIGN KEY (user_id) REFERENCES usuarios (id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS alertas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER,
            tipo_alerta TEXT,
            nivel TEXT,
            descripcion TEXT,
            fecha TEXT,
            enviada_correo INTEGER DEFAULT 0,
            enviada_whatsapp INTEGER DEFAULT 0,
            datos_autorizados INTEGER DEFAULT 0,
            FOREIGN KEY (usuario_id) REFERENCES usuarios (id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS perfil_emocional (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            fecha TEXT,
            estado_emocional_predominante TEXT,
            patrones_expresion TEXT,
            intencion_divulgacion TEXT,
            rasgos_personalidad TEXT,
            necesidades_esperadas TEXT,
            recomendaciones TEXT,
            FOREIGN KEY (user_id) REFERENCES usuarios (id)
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS psicologos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            usuario TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            activo INTEGER DEFAULT 1
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS psicologos_facultades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            psicologo_id INTEGER NOT NULL,
            facultad TEXT NOT NULL,
            FOREIGN KEY (psicologo_id) REFERENCES psicologos(id)
        )
    """)

    conn.commit()
    conn.close()
    logger.info("✅ Base de datos verificada")


def registrar_mensaje_db(user_id, user_name, user_message, bot_message):
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    timestamp = datetime.now().isoformat()
    cursor.execute(
        "INSERT OR IGNORE INTO usuarios (id, user_name, ultima_alerta) VALUES (?, ?, ?)",
        (user_id, user_name, None)
    )
    cursor.execute("""
        INSERT INTO conversaciones (user_id, user_message, bot_message, timestamp)
        VALUES (?, ?, ?, ?)
    """, (user_id, user_message, bot_message, timestamp))
    conn.commit()
    conn.close()


def obtener_historial_usuario(user_id, limite=5):
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_message, bot_message, timestamp
        FROM conversaciones WHERE user_id=?
        ORDER BY id DESC LIMIT ?
    """, (user_id, limite))
    datos = cursor.fetchall()
    conn.close()
    return datos[::-1]


def contar_mensajes_usuario(user_id):
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM conversaciones WHERE user_id=?", (user_id,))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def registrar_alerta(usuario_id, tipo_alerta, nivel, descripcion, enviada_correo=0, enviada_whatsapp=0):
    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO alertas (usuario_id, tipo_alerta, nivel, descripcion, fecha, enviada_correo, enviada_whatsapp)
            VALUES (?, ?, ?, ?, datetime('now', 'localtime'), ?, ?)
        """, (usuario_id, tipo_alerta, nivel, descripcion, enviada_correo, enviada_whatsapp))
        alerta_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return alerta_id
    except Exception as e:
        logger.error(f"[Alertas] Error registrando alerta: {e}")
        return None


def puede_generar_alerta_clinica(user_id):
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute("SELECT fecha FROM alertas WHERE usuario_id=? ORDER BY id DESC LIMIT 1", (user_id,))
    fila = cursor.fetchone()
    conn.close()
    if not fila:
        return True
    ultima = datetime.fromisoformat(fila[0])
    return (datetime.now() - ultima) >= timedelta(minutes=RISK_COOLDOWN_MINUTES)


def puede_enviar_info_psicologia(user_id):
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute("SELECT ultima_alerta FROM usuarios WHERE id=?", (user_id,))
    fila = cursor.fetchone()
    conn.close()
    if fila and fila[0]:
        ultima = datetime.fromisoformat(fila[0])
        return (datetime.now() - ultima).days >= COOLDOWN_DIAS
    return True


def actualizar_ultima_alerta(user_id):
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE usuarios SET ultima_alerta=? WHERE id=?",
        (datetime.now().isoformat(), user_id)
    )
    conn.commit()
    conn.close()


def obtener_datos_usuario(user_id):
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT nombre, numero, correo_institucional, facultad FROM datos WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (user_id,)
    )
    fila = cursor.fetchone()
    conn.close()
    if fila:
        return {
            "nombre": fila[0],
            "numero": fila[1],
            "correo_institucional": fila[2],
            "facultad": fila[3],
            "consentimiento": "Sí"
        }
    return None


def obtener_fecha_ultima_alerta_con_datos(user_id):
    """
    Devuelve la fecha de la última alerta donde datos_autorizados = 1, o None.
    """
    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT fecha FROM alertas
            WHERE usuario_id=? AND datos_autorizados=1
            ORDER BY datetime(fecha) DESC
            LIMIT 1
            """,
            (user_id,)
        )
        fila = cursor.fetchone()
        conn.close()
        if fila and fila[0]:
            return datetime.fromisoformat(fila[0])
    except Exception as e:
        logger.error(f"[Alertas] Error obteniendo última alerta con datos: {e}")
    return None


def debe_pedir_datos(user_id):
    """
    Regla que define si se deben pedir nuevamente los datos al usuario:
    - Si NO hay datos almacenados → True (pedir datos)
    - Si NO hay alerta previa con datos_autorizados=1 → True
    - Si la última alerta con datos tiene 15 días o más → True
    - En caso contrario → False (usar datos previos sin preguntar)
    """
    datos = obtener_datos_usuario(user_id)
    if not datos:
        return True

    ultima_alerta_con_datos = obtener_fecha_ultima_alerta_con_datos(user_id)
    if not ultima_alerta_con_datos:
        return True

    dias_transcurridos = (datetime.now() - ultima_alerta_con_datos).days
    return dias_transcurridos >= 15


def marcar_alerta_enviada(alerta_id, datos_autorizados):
    """
    Marca una alerta como enviada por correo y WhatsApp,
    y actualiza si los datos fueron autorizados (1) o no (0).
    """
    if not alerta_id:
        return
    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE alertas
            SET datos_autorizados=?, enviada_correo=1, enviada_whatsapp=1
            WHERE id=?
            """,
            (1 if datos_autorizados else 0, alerta_id)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[Alertas] Error actualizando alerta: {e}")


def openai_chat(messages, temperature=0.7):
    if not client:
        return "Lo siento, ahora mismo no puedo generar respuestas."
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=temperature
        )
        return resp.choices[0].message.content
    except Exception as e:
        logger.error(f"[OpenAI] Error: {e}")
        return "⚠️ Estoy teniendo dificultades para responder ahora mismo."


def detectar_riesgo(user_id, mensaje_actual):
    if not client:
        return False, None, "OpenAI no configurado", None
    try:
        historial = obtener_historial_usuario(user_id, limite=7)
        contexto = "".join(
            [f"{i}. [{fecha}] Usuario: {msg}\n" for i, (msg, _b, fecha) in enumerate(historial, 1)]
        )

        prompt = f"""
Eres un evaluador clínico especializado en psicología preventiva. Analiza la conversación
y el último mensaje únicamente desde una perspectiva emocional y psicológica.

Evalúa si existen señales de riesgo actual relacionadas con:
suicidio, drogadicción, violencia familiar, abuso sexual o depresión severa.

Si el mensaje muestra calma, saludo, agradecimiento o cambio de tema → NO hay riesgo.

Responde SOLO con un JSON válido con claves MAYÚSCULAS así EXACTAMENTE:
{{"RIESGO":"SI/NO","TEMA":"suicidio/drogadiccion/violencia/abuso/depresion/ninguno","RAZON":"..."}}

Contexto:
{contexto}

Mensaje actual del usuario: {mensaje_actual}
"""

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )

        content = resp.choices[0].message.content.strip()
        content = content.replace("\n", " ").replace("\r", " ")
        m = re.search(r"\{.*\}", content, re.DOTALL)
        json_text = m.group(0) if m else None

        parsed = {"RIESGO": "NO", "TEMA": "ninguno", "RAZON": ""}
        if json_text:
            try:
                parsed = json.loads(json_text)
            except Exception:
                pass

        riesgo_flag = parsed.get("RIESGO", "NO").upper().startswith("S")
        tema = parsed.get("TEMA", "ninguno").lower()
        razon = parsed.get("RAZON", "")

        alerta_id = None
        if riesgo_flag and puede_generar_alerta_clinica(user_id):
            tipo_alerta = f"riesgo {tema}" if tema != "ninguno" else "riesgo psicológico"
            nivel = "crítico" if tema == "suicidio" else "alto"
            alerta_id = registrar_alerta(user_id, tipo_alerta, nivel, razon)

        return riesgo_flag, tema, razon, alerta_id

    except Exception as e:
        logger.error(f"[Riesgo] Error: {e}")
        return False, None, "Error", None


def detectar_dependencia(user_id):
    if not client:
        return None
    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT nivel_dependencia, contador_mensajes FROM dependencias WHERE user_id=?",
            (user_id,)
        )
        fila = cursor.fetchone()
        nivel_actual, contador = fila if fila else (None, 0)
        contador += 1

        LIMITE_MENSAJES = 15
        if contador < LIMITE_MENSAJES:
            if fila:
                cursor.execute(
                    "UPDATE dependencias SET contador_mensajes=? WHERE user_id=?",
                    (contador, user_id)
                )
            else:
                cursor.execute("""
                    INSERT INTO dependencias (user_id, nivel_dependencia, ultima_evaluacion, contador_mensajes)
                    VALUES (?, ?, ?, ?)
                """, (user_id, "baja", datetime.now().isoformat(), contador))
            conn.commit()
            conn.close()
            return None

        cursor.execute("""
            SELECT user_message, bot_message FROM conversaciones WHERE user_id=?
            ORDER BY id DESC LIMIT 15
        """, (user_id,))
        historial = cursor.fetchall()
        conn.close()

        mensajes = "\n".join([f"Usuario: {u}\nSerenity: {b}" for u, b in reversed(historial)])
#1. Necesidad frecuente de usarlo.
#2. Dificultad para dejar de usarlo.
#3. Conexión emocional hacia el chatbot.
#4. Malestar si no puedo usarlo.
#5. Interferencia en rutinas/relaciones.
#6. Pensar frecuentemente en conversaciones.
#7. Uso para sentirse comprendido(a).
#8. Creencia de depender demasiado.
        prompt = f"""
Evalúa el nivel de dependencia emocional hacia un chatbot en escala 1-5 según estos ítems:
1. Si no puedo usar chatbots de IA, me sentiría ansioso o incómodo.
2. Necesito abrir los chatbots de IA antes de empezar a trabajar o realizar tareas.
3. Si no puedo usar chatbots de IA, me resultaría difícil obtener la información necesaria.
4. Incluso cuando me enfrento a tareas o trabajos que podría completar fácilmente por mi cuenta, tiendo a buscar ayuda en los chatbots de IA.
5. En comparación con otras personas o cosas, prefiero dedicar tiempo a los chatbots de IA.
6. Incluso si no los uso activamente, los mantengo conectados o ejecutándolos en segundo plano.
7. Cada vez dedicio más tiempo a los chatbots de IA. 
8. Para mí, la vida sin chatbots de IA sería un inconveniente.

Responde SOLO en JSON:
{{"items":[n1...n8],"total":X,"nivel":"baja"|"media"|"alta"}}

Conversación:
{mensajes}
"""

        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        content = r.choices[0].message.content.strip().replace("\n", " ").replace("\r", " ")
        m = re.search(r"\{.*\}", content)
        j = m.group(0) if m else None

        parsed = {"total": 8, "nivel": "baja"}
        if j:
            try:
                parsed = json.loads(j)
            except Exception:
                pass

        total = int(parsed.get("total", 8))
        nivel = parsed.get("nivel", "baja").lower()
        if nivel not in ["baja", "media", "alta"]:
            nivel = "baja"

        fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO dependencias (user_id, nivel_dependencia, puntaje_total, ultima_evaluacion, contador_mensajes)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                nivel_dependencia=excluded.nivel_dependencia,
                puntaje_total=excluded.puntaje_total,
                ultima_evaluacion=excluded.ultima_evaluacion,
                contador_mensajes=0
        """, (user_id, nivel, total, fecha))

        cursor.execute("""
            INSERT INTO historial_dependencias (user_id, nivel_dependencia, puntaje_total, fecha_evaluacion)
            VALUES (?, ?, ?, ?)
        """, (user_id, nivel, total, fecha))

        conn.commit()
        conn.close()
        return nivel
    except Exception as e:
        logger.error(f"[Dependencia] Error: {e}")
        return None


def analizar_perfil_emocional(user_id):
    if not client:
        return None
    try:
        conn = sqlite3.connect(db_file)
        cur = conn.cursor()
        cur.execute(
            "SELECT user_message FROM conversaciones WHERE user_id=? ORDER BY id DESC LIMIT 20",
            (user_id,)
        )
        filas = cur.fetchall()
        conn.close()

        msgs = [f[0] for f in filas[::-1]]
        if not msgs:
            return None

        context = "\n".join([f"- {m}" for m in msgs])

        prompt = f"""
Genera un perfil emocional breve con EXACTAMENTE estas claves en JSON:
1) estado_emocional_predominante
2) patrones_expresion
3) intencion_divulgacion
4) rasgos_personalidad
5) necesidades_esperadas
6) recomendaciones

Responde SOLO con JSON válido redactado con buena ortografía.

Mensajes:
{context}
"""

        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        content = r.choices[0].message.content.strip().replace("\n", " ").replace("\r", " ")
        m = re.search(r"\{.*\}", content)
        j = m.group(0) if m else None

        perfil = {
            "estado_emocional_predominante": "",
            "patrones_expresion": "",
            "intencion_divulgacion": "",
            "rasgos_personalidad": "",
            "necesidades_esperadas": "",
            "recomendaciones": "",
        }

        if j:
            try:
                parsed = json.loads(j)
                for k in perfil.keys():
                    if k in parsed:
                        perfil[k] = str(parsed[k]).strip()
            except Exception:
                pass

        if not any(perfil.values()):
            perfil.update({
                "estado_emocional_predominante": "indeterminado",
                "patrones_expresion": "No se detectaron patrones claros.",
                "intencion_divulgacion": "media",
                "rasgos_personalidad": "no determinado",
                "necesidades_esperadas": "acompañamiento emocional",
                "recomendaciones": "Mantener contención y ofrecer recursos.",
            })
        return perfil
    except Exception as e:
        logger.error(f"[Perfil] Error: {e}")
        return None


def guardar_perfil_emocional(user_id, perfil):
    try:
        conn = sqlite3.connect(db_file)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO perfil_emocional (
                user_id, fecha, estado_emocional_predominante, patrones_expresion,
                intencion_divulgacion, rasgos_personalidad, necesidades_esperadas, recomendaciones
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id, datetime.now().isoformat(),
            perfil.get("estado_emocional_predominante", ""),
            perfil.get("patrones_expresion", ""),
            perfil.get("intencion_divulgacion", ""),
            perfil.get("rasgos_personalidad", ""),
            perfil.get("necesidades_esperadas", ""),
            perfil.get("recomendaciones", ""),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[Perfil] Error guardando: {e}")


def enviar_alerta_correo(user_id, user_name, historial, tema_alerta, mensaje_activador, datos_usuario, alerta_id):
    if not GMAIL_USER or not GMAIL_PASS:
        return

    cuerpo = f"🚨 ALERTA DE ALTO RIESGO: {tema_alerta.upper()} 🚨\n\n"
    cuerpo += f"👤 Usuario (Telegram): {user_name}\n"
    cuerpo += f"🗨️ Mensaje de alerta:\n{mensaje_activador}\n\n📅 Historial reciente:\n"
    for i, (msg, _bot, fecha) in enumerate(historial, 1):
        cuerpo += f"{i}. [{fecha}] Usuario: {msg}\n"

    if datos_usuario:
        cuerpo += "\nDatos del usuario con consentimiento:\n"
        cuerpo += f"Nombre: {datos_usuario['nombre']}\n"
        cuerpo += f"Teléfono: {datos_usuario['numero']}\n"
        cuerpo += f"Correo institucional: {datos_usuario['correo_institucional']}\n"
        if datos_usuario.get("facultad"):
            cuerpo += f"Facultad: {datos_usuario['facultad']}\n"
    else:
        cuerpo += "\nℹ Usuario no autorizó compartir datos.\n"

    try:
        conn = sqlite3.connect(db_file)
        df = pd.read_sql_query(
            "SELECT * FROM conversaciones WHERE user_id=? ORDER BY id ASC",
            conn,
            params=(user_id,)
        )
        conn.close()
        excel_path = os.path.join(tempfile.gettempdir(), f"alerta_{user_id}.xlsx")
        df.to_excel(excel_path, index=False)
    except Exception:
        excel_path = None

    msg_email = MIMEMultipart()
    msg_email["Subject"] = f"Alerta de riesgo - Usuario {user_name} - Tema: {tema_alerta}"
    msg_email["From"] = GMAIL_USER
    msg_email["To"] = GMAIL_USER
    msg_email.attach(MIMEText(cuerpo, "plain"))

    if excel_path and os.path.isfile(excel_path):
        with open(excel_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{os.path.basename(excel_path)}"'
        )
        msg_email.attach(part)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_PASS)
            smtp.sendmail(GMAIL_USER, [GMAIL_USER], msg_email.as_string())
    except Exception as e:
        logger.error(f"[Correo] Error enviando: {e}")


def enviar_alerta_whatsapp(user_id, user_name, tema_alerta, mensaje_activador, alerta_id):
    if not twilio_client:
        return
    try:
        cuerpo = (
            f"🚨 *ALERTA DE ALTO RIESGO ({tema_alerta.upper()})* 🚨\n\n"
            f"👤 Usuario: {user_name}\n"
            f"🗨️ Mensaje de alerta:\n{mensaje_activador}\n\n"
            f"📅 Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            "Revisa correo para más información."
        )
        twilio_client.messages.create(
            body=cuerpo,
            from_=TWILIO_WHATSAPP_FROM,
            to=TWILIO_WHATSAPP_TO
        )
    except Exception as e:
        logger.error(f"[WhatsApp] Error: {e}")


async def responder_info_psicologia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not puede_enviar_info_psicologia(user_id):
        if update.message:
            await update.message.reply_text("Ya te compartí la información recientemente 💙")
        elif update.callback_query and update.callback_query.message:
            await update.callback_query.message.reply_text("Ya te compartí la información recientemente 💙")
        return

    texto = None
    if os.path.isfile(JSON_PATH):
        try:
            with open(JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            texto = (
                "<b>Información de Apoyo Psicológico</b>\n\n"
                f"{escape(data.get('introduccion', 'Sin datos'))}\n\n"
                "<b>📍 Servicios disponibles:</b>\n"
                f"{escape(data.get('servicios', 'Sin datos'))}\n\n"
                "<b>📞 Contacto:</b>\n"
                f"{escape(data.get('contacto', 'Sin datos'))}\n\n"
                "Estoy aquí para acompañarte"
            )
        except Exception:
            texto = None

    if not texto:
        texto = (
            "<b>Apoyo Psicopedagógico UNACAR</b>\n\n"
            "Puedes acudir a:\n\n<b>Unidad de Servicios Psicopedagógicos - UNACAR</b>\n"
            "Ubicado en Campus Principal, Plaza Cultural Universitaria, puerta 204\n"
            "Horario de atención: Lunes a Viernes de 9:00 a 14:00 y de 16:00 a 19:00\n\n"
            "<b>Servicios:</b>\n"
            "• Orientación psicológica individual\n"
            "• Atención en crisis emocional\n"
            "• Talleres grupales y asesoramiento académico-emocional\n"
            "• Canalización a especialistas con descuento para estudiantes UNACAR\n"
            "• Seguimiento psicológico dentro de la institución\n\n"
            "Todos los servicios son gratuitos y confidenciales para estudiantes.\n\n"
            f"📄 <b>Directorio completo:</b>\n{LINK_UNACAR}"
        )

    if update.message:
        await update.message.reply_text(texto, parse_mode="HTML")
    elif update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(texto, parse_mode="HTML")

    actualizar_ultima_alerta(user_id)


def boton_consentimiento():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Sí", callback_data="consent_si"),
         InlineKeyboardButton("No", callback_data="consent_no")]
    ])


def teclado_facultades():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Fac. Económicas Administrativas", callback_data="fac_ecoadm")],
        [InlineKeyboardButton("Fac. Ciencias de la Información", callback_data="fac_info")],
        [InlineKeyboardButton("Fac. Ciencias de la Salud", callback_data="fac_salud")],
        [InlineKeyboardButton("Fac. Ciencias Educativas", callback_data="fac_educativas")],
        [InlineKeyboardButton("Fac. Ciencias Naturales", callback_data="fac_nat")],
        [InlineKeyboardButton("Facultad de Derecho", callback_data="fac_derecho")],
        [InlineKeyboardButton("Facultad de Ingeniería", callback_data="fac_ing")],
        [InlineKeyboardButton("Facultad de Química", callback_data="fac_quim")]
    ])


async def consentimiento_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    dato_riesgo = context.user_data.get("riesgo_data", {})

    if not dato_riesgo:
        await query.edit_message_text("⚠️ No se encontró información.")
        return

    alerta_id = dato_riesgo.get("alerta_id")

    if query.data == "consent_si":
        context.user_data["estado"] = "esperando_nombre"
        await query.edit_message_text("Gracias.\n\nEscribe tu nombre completo:")
        return

    if query.data == "consent_no":
        context.user_data["estado"] = None
        enviar_alerta_correo(
            user.id,
            user.first_name,
            dato_riesgo["historial"],
            dato_riesgo["tema"],
            dato_riesgo["mensaje"],
            None,
            alerta_id,
        )
        enviar_alerta_whatsapp(
            user.id,
            user.first_name,
            dato_riesgo["tema"],
            dato_riesgo["mensaje"],
            alerta_id,
        )
        marcar_alerta_enviada(alerta_id, datos_autorizados=0)
        actualizar_ultima_alerta(user.id)
        await query.edit_message_text("Entiendo.\nSi quieres, podemos seguir hablando de lo que sientes.")
        return


async def manejo_datos_usuario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    estado = context.user_data.get("estado")
    text = (update.message.text or "").strip()

    if estado == "esperando_nombre":
        if not re.match(r"^[A-Za-zÁÉÍÓÚáéíóúÑñ ]{3,50}$", text):
            await update.message.reply_text("Por favor escribe tu nombre real (solo letras).")
            return
        context.user_data["nombre"] = text
        context.user_data["estado"] = "esperando_numero"
        await update.message.reply_text("Gracias, \nAhora escribe tu número de contacto (solo números sin espacios):")
        return

    if estado == "esperando_numero":
        if not re.match(r"^[0-9]{10,12}$", text):
            await update.message.reply_text("Tu número debe contener solo dígitos sin espacios (mínimo 10).")
            return
        context.user_data["numero"] = text
        context.user_data["estado"] = "esperando_correo"
        await update.message.reply_text("Perfecto, por último, escribe tu correo institucional o personal:")
        return

    if estado == "esperando_correo":
        if not re.match(r"^[A-Za-z0-9._%+-]+@(gmail\.com|mail\.unacar\.mx|delfines\.unacar\.mx)$", text):
            await update.message.reply_text(
                "Correo inválido. Usa uno tipo:\n nombre@gmail.com\n nombre@mail.unacar.mx\n nombre@delfines.unacar.mx "
            )
            return

        context.user_data["correo"] = text
        context.user_data["estado"] = "esperando_facultad"

        await update.message.reply_text(
            "Gracias.\nAhora selecciona tu facultad:",
            reply_markup=teclado_facultades()
        )
        return

    if estado == "esperando_facultad":
        await update.message.reply_text(
            "Por favor selecciona tu facultad usando los botones que aparecen en pantalla"
        )
        return


async def callback_facultad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    fac_map = {
        "fac_ecoadm": "Facultad de Ciencias Económicas Administrativas",
        "fac_info": "Facultad de Ciencias de la Información",
        "fac_salud": "Facultad de Ciencias de la Salud",
        "fac_educativas": "Facultad de Ciencias Educativas",
        "fac_nat": "Facultad de Ciencias Naturales",
        "fac_derecho": "Facultad de Derecho",
        "fac_ing": "Facultad de Ingeniería",
        "fac_quim": "Facultad de Química"
    }

    facultad = fac_map.get(query.data, None)
    if not facultad:
        await query.edit_message_text("Ocurrió un problema al registrar la facultad.")
        return
    nombre = context.user_data.get("nombre")
    numero = context.user_data.get("numero")
    correo_inst = context.user_data.get("correo")

    if not (nombre and numero and correo_inst):
        await query.edit_message_text(
            "⚠️ No se encontraron tus datos completos. Por favor, vuelve a intentarlo más tarde."
        )
        context.user_data["estado"] = None
        return
    try:
        conn = sqlite3.connect(db_file)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO datos (user_id, nombre, numero, correo_institucional, fecha, facultad) VALUES (?, ?, ?, ?, ?, ?)",
            (user.id, nombre, numero, correo_inst, datetime.now().isoformat(), facultad),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[Datos] Error guardando datos con facultad: {e}")

    dato_riesgo = context.user_data.pop("riesgo_data", {})
    alerta_id = dato_riesgo.get("alerta_id")
    datos_usuario = obtener_datos_usuario(user.id)

    enviar_alerta_correo(
        user.id,
        user.first_name,
        dato_riesgo.get("historial", []),
        dato_riesgo.get("tema", "riesgo"),
        dato_riesgo.get("mensaje", ""),
        datos_usuario,
        alerta_id,
    )
    enviar_alerta_whatsapp(
        user.id,
        user.first_name,
        dato_riesgo.get("tema", "riesgo"),
        dato_riesgo.get("mensaje", ""),
        alerta_id,
    )

    marcar_alerta_enviada(alerta_id, datos_autorizados=1)
    actualizar_ultima_alerta(user.id)

    context.user_data["estado"] = None
    for clave in ["nombre", "numero", "correo"]:
        context.user_data.pop(clave, None)

    await query.edit_message_text(
        "¡Perfecto! Tus datos fueron registrados correctamente.\n"
        "El área psicopedagógica podrá contactarte según tu facultad.\n\n"
        "¿Quieres seguir contándome cómo te sientes?"
    )


async def comando_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("Ver apoyo psicopedagogico", callback_data="menu_info")],
        [InlineKeyboardButton("Eliminar mis datos", callback_data="menu_del")],
    ])
    await update.message.reply_text(
        "📋 <b>Menú de opciones</b>",
        parse_mode="HTML",
        reply_markup=teclado
    )


async def callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "menu_info":
        await responder_info_psicologia(update, context)
        return

    if query.data == "menu_del":
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton("Sí, eliminar", callback_data="del_yes"),
             InlineKeyboardButton("No, cancelar", callback_data="del_no")]
        ])
        await query.edit_message_text(
            "⚠️ ¿Seguro que deseas eliminar tu información?\n(No afectará tus conversaciones previas).",
            reply_markup=teclado
        )
        return

    if query.data == "del_no":
        await query.edit_message_text("Perfecto. No se eliminó nada.")
        return

    if query.data == "del_yes":
        user_id = query.from_user.id
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM datos WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()
        await query.edit_message_text(
            "Tus datos fueron eliminados correctamente.\nPodemos seguir platicando cuando gustes."
        )
        return


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_input = (update.message.text or "").strip()

    historial = obtener_historial_usuario(user.id, limite=7)
    mensajes = [{
        "role": "system",
        "content": (
            "Eres Serenity, un acompañante emocional cálido y empático. Validas emociones, haces preguntas suaves "
            "y evitas diagnosticar o medicar. No te salgas del contexto psicologico y bienestar emocional. No respondas preguntas relacionadas a matematicas, programación historia o otras cosas."
        )
    }]

    for u_msg, b_msg, _ in historial:
        mensajes.append({"role": "user", "content": u_msg})
        mensajes.append({"role": "assistant", "content": b_msg})
    mensajes.append({"role": "user", "content": user_input})

    bot_reply = openai_chat(mensajes, temperature=0.7)

    registrar_mensaje_db(user.id, user.first_name, user_input, bot_reply)
    await update.message.reply_text(bot_reply)

    riesgo, tema, razon, alerta_id = detectar_riesgo(user.id, user_input)
    if riesgo:
        riesgo_info = {
            "historial": historial,
            "tema": tema,
            "mensaje": user_input,
            "alerta_id": alerta_id
        }
        context.user_data["riesgo_data"] = riesgo_info

        if debe_pedir_datos(user.id):
            await update.message.reply_text(
                "Lo que compartes es muy importante, por eso quiero que puedas recibir apoyo personalizado,\n"
                "¿Autorizas a compartir tus datos para que puedan apoyarte directamente?",
                reply_markup=boton_consentimiento(),
            )
        else:
            datos_usuario = obtener_datos_usuario(user.id)
            enviar_alerta_correo(
                user.id,
                user.first_name,
                historial,
                tema,
                user_input,
                datos_usuario,
                alerta_id,
            )
            enviar_alerta_whatsapp(
                user.id,
                user.first_name,
                tema,
                user_input,
                alerta_id,
            )
            marcar_alerta_enviada(alerta_id, datos_autorizados=1)
            actualizar_ultima_alerta(user.id)

#            await update.message.reply_text(
#                "Lo que compartes es muy importante.\n"
#                "He usado los datos que ya me habías autorizado anteriormente para que el área psicopedagógica pueda "
#                "contactarte y brindarte apoyo.\n\n"
#                "Si en algún momento quieres que tus datos se eliminen, puedes usar la opción \"Eliminar mis datos\" en /menu."
#            )

    nivel_dep = detectar_dependencia(user.id)
    if nivel_dep == "alta":
        await update.message.reply_text(
            "Me alegra que podamos hablar, pero también es importante apoyarte en personas cercanas o profesionales.\n"
            "¿Quieres que te comparta algunos recursos?"
        )

    total_msgs = contar_mensajes_usuario(user.id)
    if total_msgs and total_msgs % 20 == 0:
        perfil = analizar_perfil_emocional(user.id)
        if perfil:
            guardar_perfil_emocional(user.id, perfil)


async def manejar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("estado") in ["esperando_nombre", "esperando_numero", "esperando_correo", "esperando_facultad"]:
        await manejo_datos_usuario(update, context)
    else:
        await chat(update, context)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    crear_base_datos()
    await update.message.reply_text(
        "<b>Aviso de privacidad</b>\n\n"
        "Hola 👋 Soy <b>Serenity</b>, un acompañante emocional.\n\n"
        "Guardamos tus mensajes únicamente para acompañarte de mejor manera.\n"
        "Tus datos no se comparten con terceros, solo con apoyo psicopedagógico en caso de riesgo y solo con tu consentimiento.\n\n"
        "Puedes escribir <b>/menu</b> para ver opciones adicionales.\n"
        "Gracias por tu confianza.",
        parse_mode="HTML"
    )
    
import asyncio
from flask import Flask, request

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")
WEBHOOK_PATH = f"/webhook/{TOKEN_TELEGRAM}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

flask_app = Flask(__name__)

telegram_app = None


@flask_app.route("/", methods=["GET"])
def home():
    return "Serenity está vivo 💙", 200


@flask_app.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    if request.method == "POST":
        try:
            update = Update.de_json(request.get_json(force=True), telegram_app.bot)
            telegram_app.update_queue.put_nowait(update)
        except Exception as e:
            logger.error(f"[Webhook] Error al procesar actualización: {e}")
        return "OK", 200


async def configurar_webhook():
    await telegram_app.bot.delete_webhook()
    await telegram_app.bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"🌐 Webhook configurado en: {WEBHOOK_URL}")


def main():
    global telegram_app

    crear_base_datos()

    if not TOKEN_TELEGRAM:
        raise RuntimeError("Falta TELEGRAM_TOKEN en variables de entorno")

    telegram_app = Application.builder().token(TOKEN_TELEGRAM).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("menu", comando_menu))
    telegram_app.add_handler(CallbackQueryHandler(callback_menu, pattern="menu_.*|del_.*"))
    telegram_app.add_handler(CallbackQueryHandler(consentimiento_callback, pattern="consent_.*"))
    telegram_app.add_handler(CallbackQueryHandler(callback_facultad, pattern="fac_.*"))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensaje))
    loop = asyncio.get_event_loop()
    loop.create_task(telegram_app.initialize())
    loop.create_task(configurar_webhook())
    loop.create_task(telegram_app.start())
    flask_app.run(host="0.0.0.0", port=10000)

if __name__ == "__main__":
    import asyncio
    main()
