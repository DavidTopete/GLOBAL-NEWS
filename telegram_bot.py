"""
Global News Bot -> Telegram

Consulta NewsAPI por categorias, traduce cada noticia (modo estricto: lo que
no se traduce no se publica), deduplica contra el historial y publica en un
canal de Telegram.

Variables de entorno:
    TOKEN          Token del bot de Telegram (no requerido con DRY_RUN=1)
    CHAT_ID        Chat o canal destino     (no requerido con DRY_RUN=1)
    NEWS_API_KEY   API key de NewsAPI
    DRY_RUN        "1" -> imprime los mensajes en consola, no envia nada
                   y no modifica el historial.
    ZONA_HORARIA   Zona para la fecha del encabezado (default America/Tijuana)

Codigos de salida (para cron / GitHub Actions):
    0  OK (incluye "no habia noticias nuevas")
    1  Error de configuracion, traductor caido, NewsAPI caido o error inesperado
    2  Corrida parcial: alguna noticia no se pudo enviar a Telegram
"""

import os
import re
import sys
import time
import json
import html
import shutil
import logging
import itertools
import unicodedata
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

# Modulo de traduccion multi-proveedor con cache. Debe estar en el mismo
# directorio que este archivo.
from traductor import (
    traducir_estricto,
    TraduccionFallida,
    autotest,
    resumen_stats,
    guardar_cache,
    proveedores_agotados,
)

# ---------------------------------------------------------------------------
# Credenciales y modo de ejecucion
# ---------------------------------------------------------------------------

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
NEWS_API_KEY = os.getenv("NEWS_API_KEY")

DRY_RUN = os.getenv("DRY_RUN", "").strip().lower() in {"1", "true", "yes", "si"}
ZONA_HORARIA = os.getenv("ZONA_HORARIA", "America/Tijuana")

# ---------------------------------------------------------------------------
# Logging con redaccion de secretos
# ---------------------------------------------------------------------------


def redactar(texto):
    """Reemplaza el token de Telegram y la API key por *** en cualquier texto.
    Las excepciones de requests incluyen la URL completa, y la URL de
    Telegram lleva el token dentro."""
    texto = str(texto)
    for secreto in (TOKEN, NEWS_API_KEY):
        if secreto and len(secreto) >= 8:   # evita tapar letras sueltas
            texto = texto.replace(secreto, "***")
    return texto


class FiltroSecretos(logging.Filter):
    """Redacta secretos en TODOS los logs, incluidos tracebacks y los logs
    de otros modulos (p. ej. traductor) que pasen por el handler raiz."""

    def filter(self, record):
        record.msg = redactar(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = redactar(
                logging.Formatter().formatException(record.exc_info)
            )
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(FiltroSecretos())

log = logging.getLogger("global_news_bot")

# ---------------------------------------------------------------------------
# Configuracion general
# ---------------------------------------------------------------------------

ARCHIVO_HISTORIAL = "noticias_enviadas.json"
DIAS_HISTORIAL = 30            # URLs enviadas se recuerdan 30 dias
MAX_URLS_HISTORIAL = 5000      # tope de seguridad por si cambia la frecuencia
HORAS_VENTANA_DEDUP = 72       # titulares recientes usados para deduplicar

MAX_NOTICIAS_POR_CORRIDA = 12

# --- Politica de traduccion estricta ---------------------------------------
# Una noticia que no se pueda traducir NO se publica: se descarta y el bot
# sigue evaluando candidatas hasta llenar el cupo.

# Techo de candidatas evaluadas (traducidas) por corrida, compartido entre
# todas las categorias. Protege coste y rate-limit del traductor.
MAX_CANDIDATOS_EVALUADOS = 80

# Circuit breaker de dos umbrales. Cada noticia exige DOS traducciones
# (titulo + descripcion): un 50% de fallo por campo es 75% de descarte por
# noticia, y varios descartes seguidos ocurren por azar.
MAX_FALLOS_SIN_NINGUN_EXITO = 6     # traductor caido -> abortar
MAX_FALLOS_TRAS_UN_EXITO = 15       # degradacion parcial -> publicar lo obtenido

# Si la descripcion no traduce pero el titulo si:
#   True  -> descarta la noticia
#   False -> publica solo con el titulo
EXIGIR_DESCRIPCION_TRADUCIDA = True

# Sin traductor no se publicaria nada en modo estricto: abortar antes de
# gastar cuota de NewsAPI.
ABORTAR_SI_NO_HAY_TRADUCTOR = True

# --- Deduplicacion ---------------------------------------------------------
DEDUPLICAR_POR_TITULO = True
UMBRAL_SIMILITUD_TITULO = 0.6   # Jaccard sobre tokens significativos
MIN_TOKENS_FIRMA = 4            # debajo de esto no se deduplica por titulo

# --- Formato y Telegram ----------------------------------------------------
MAX_LARGO_TITULO = 250
MAX_LARGO_DESCRIPCION = 900
LIMITE_CAPTION = 1024          # limite de Telegram para el texto de una foto
TEXTO_ENLACE = "🔗 Ver noticia completa"
PAUSA_ENTRE_MENSAJES = 3.0      # canales/grupos: ~20 mensajes/minuto
MAX_REINTENTOS_TELEGRAM = 3
MAX_REINTENTOS_NEWSAPI = 2

URL_TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"
URL_NEWSAPI = "https://newsapi.org/v2/everything"

# ---------------------------------------------------------------------------
# Fuentes y categorias
# ---------------------------------------------------------------------------

DOMINIOS_GENERALES = "reuters.com,bbc.com,cnn.com,bloomberg.com"
DOMINIOS_FINANZAS = "reuters.com,bloomberg.com,cnbc.com"
DOMINIOS_CRIPTO = "coindesk.com,cointelegraph.com,theblock.co"
DOMINIOS_TECNOLOGIA = "theverge.com,reuters.com,bloomberg.com,bbc.com"

QUERY_IRAN = (
    "(Iran OR Iranian OR Tehran OR \"Israel Iran\" OR \"US Iran\" OR "
    "\"Strait of Hormuz\")"
)

QUERY_TRUMP = (
    "(Trump OR \"Donald Trump\" OR \"Trump administration\" OR "
    "\"Trump policy\" OR \"Trump tariffs\")"
)

QUERY_FINANZAS = (
    "(\"Federal Reserve\" OR \"interest rates\" OR \"rate cut\" OR "
    "\"central bank\" OR inflation OR \"Wall Street\" OR stocks OR "
    "\"S&P 500\" OR Nasdaq OR \"Treasury yields\" OR bonds OR earnings OR "
    "\"oil prices\" OR gold OR dollar OR recession)"
)

QUERY_CRIPTO = (
    "(bitcoin OR ethereum OR crypto OR cryptocurrency OR stablecoin OR "
    "\"bitcoin ETF\" OR binance OR defi OR tokenization OR blockchain OR "
    "altcoins OR web3)"
)

QUERY_TECNOLOGIA = (
    "(AI OR \"artificial intelligence\" OR semiconductor OR chips OR "
    "Nvidia OR OpenAI OR \"big tech\" OR technology)"
)

QUERY_GEOPOLITICA = (
    "(geopolitics OR war OR sanctions OR Russia OR Ukraine OR China OR "
    "Taiwan OR NATO OR \"trade war\" OR energy)"
)

# Las categorias solo controlan QUE noticias se eligen y cuantas de cada
# tema; no se muestran en el mensaje.
# Orden = prioridad de seleccion y orden de publicacion.
# cupo:    lugares garantizados por corrida.
# relleno: si alguna categoria no llena su cupo, las marcadas con
#          relleno=True reparten los lugares sobrantes (round-robin).
CATEGORIAS = [
    {"etiqueta": "IRAN", "query": QUERY_IRAN,
     "dominios": DOMINIOS_GENERALES, "page_size": 15, "cupo": 1, "relleno": False},
    {"etiqueta": "TRUMP", "query": QUERY_TRUMP,
     "dominios": DOMINIOS_GENERALES, "page_size": 15, "cupo": 1, "relleno": False},
    {"etiqueta": "FINANZAS", "query": QUERY_FINANZAS,
     "dominios": DOMINIOS_FINANZAS, "page_size": 30, "cupo": 3, "relleno": True},
    {"etiqueta": "CRIPTO", "query": QUERY_CRIPTO,
     "dominios": DOMINIOS_CRIPTO, "page_size": 30, "cupo": 3, "relleno": True},
    {"etiqueta": "TECNOLOGIA", "query": QUERY_TECNOLOGIA,
     "dominios": DOMINIOS_TECNOLOGIA, "page_size": 25, "cupo": 2, "relleno": True},
    {"etiqueta": "GEOPOLITICA", "query": QUERY_GEOPOLITICA,
     "dominios": DOMINIOS_GENERALES, "page_size": 25, "cupo": 2, "relleno": True},
]

# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def ts_ahora():
    return int(time.time())


def fecha_local():
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(ZONA_HORARIA))
        except Exception as error:
            log.warning(f"Zona horaria '{ZONA_HORARIA}' no disponible ({error}); "
                        f"se usa la hora del servidor.")
    return datetime.now()


def recortar(texto, maximo, sufijo="..."):
    """Recorta en el ultimo espacio antes del limite para no partir palabras
    (el traductor recibe texto mas limpio)."""
    if not texto or len(texto) <= maximo:
        return texto
    corte = texto[:maximo]
    espacio = corte.rfind(" ")
    if espacio > maximo * 0.6:
        corte = corte[:espacio]
    return corte.rstrip(" ,;:-") + sufijo


PARAMS_RASTREO = {
    "fbclid", "gclid", "ref", "ref_src", "cmpid", "taid",
    "mc_cid", "mc_eid", "guccounter", "smid", "ocid",
}


def normalizar_url(url):
    """Clave canonica de una URL para el historial: sin parametros de
    rastreo, sin fragmento, sin 'www.' ni '/' final."""
    url = (url or "").strip()
    try:
        partes = urlsplit(url)
    except ValueError:
        return url
    query = [
        (k, v) for k, v in parse_qsl(partes.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in PARAMS_RASTREO
    ]
    host = partes.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    ruta = partes.path.rstrip("/") or "/"
    return urlunsplit(("https", host, ruta, urlencode(query), ""))


# Palabras vacias que no aportan identidad al titular.
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "as", "at", "by", "from", "that", "this", "is", "are", "was", "were",
    "be", "it", "its", "has", "have", "had", "after", "over", "amid", "says",
    "said", "new", "into", "up", "out", "will", "could", "may", "not", "but",
    "than", "more", "about", "what", "how", "why", "who", "s",
}


def tokens_titulo(titulo):
    """Conjunto de tokens significativos del titular. Conserva las cifras:
    son las que distinguen 'Bitcoin hits 90000' de 'Bitcoin hits 70000'."""
    texto = unicodedata.normalize("NFKD", titulo or "")
    texto = texto.encode("ascii", "ignore").decode("ascii").lower()
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    # len(t) > 1 descarta letras sueltas, pero las cifras de un digito se
    # conservan ('Fed cuts 2 times' != 'Fed cuts 3 times').
    return frozenset(
        t for t in texto.split()
        if t not in STOPWORDS and (len(t) > 1 or t.isdigit())
    )


def _cifras(tokens):
    return {t for t in tokens if any(c.isdigit() for c in t)}


def son_similares(a, b, umbral=UMBRAL_SIMILITUD_TITULO):
    """True si dos titulares describen la misma historia.

    - Titulares con pocos tokens: no se comparan (falso negativo barato
      frente a un falso positivo que descartaria una noticia valida).
    - Si ambos tienen cifras y las cifras difieren, son noticias distintas
      aunque el resto del texto coincida.
    """
    if len(a) < MIN_TOKENS_FIRMA or len(b) < MIN_TOKENS_FIRMA:
        return False
    cifras_a, cifras_b = _cifras(a), _cifras(b)
    if cifras_a and cifras_b and cifras_a != cifras_b:
        return False
    return len(a & b) / len(a | b) >= umbral


def articulo_valido(articulo):
    """Filtra articulos dados de baja por NewsAPI ('[Removed]') y entradas
    sin titulo o sin link. Se filtran ANTES de traducir para no gastar cuota
    ni contarlos como fallos en el circuit breaker."""
    titulo = (articulo.get("title") or "").strip()
    link = (articulo.get("url") or "").strip()
    if not titulo or not link:
        return False
    if titulo == "[Removed]" or "removed.com" in link:
        return False
    return link.startswith(("http://", "https://"))


# ---------------------------------------------------------------------------
# Historial
# ---------------------------------------------------------------------------


class Historial:
    """Historial persistente.

    Formato en disco:
        {
          "version": 2,
          "urls":    {"<url normalizada>": <timestamp>, ...},
          "titulos": [{"tokens": [...], "ts": <timestamp>}, ...]
        }

    Acepta y migra el formato antiguo (lista plana de URLs).
    """

    def __init__(self, ruta):
        self.ruta = ruta
        self.urls = {}
        self.titulos = []
        self._cargar()

    def _cargar(self):
        if not os.path.exists(self.ruta):
            return

        try:
            with open(self.ruta, "r", encoding="utf-8") as f:
                datos = json.load(f)
        except (OSError, ValueError) as error:
            log.error(f"Historial ilegible, se respalda y reinicia: {error}")
            try:
                os.replace(self.ruta, f"{self.ruta}.bak_{ts_ahora()}")
            except OSError:
                pass
            return

        ahora = ts_ahora()

        if isinstance(datos, list):
            # Formato antiguo: lista de URLs. Se respalda una vez y se migra.
            try:
                shutil.copy2(self.ruta, f"{self.ruta}.bak_formato_v1")
            except OSError:
                pass
            self.urls = {
                normalizar_url(u): ahora for u in datos if isinstance(u, str) and u
            }
            log.info(f"Historial en formato antiguo migrado ({len(self.urls)} URLs).")

        elif isinstance(datos, dict):
            urls = datos.get("urls", {})
            if isinstance(urls, dict):
                self.urls = {
                    u: int(ts) for u, ts in urls.items()
                    if isinstance(u, str) and isinstance(ts, (int, float))
                }
            self.titulos = [
                t for t in datos.get("titulos", [])
                if isinstance(t, dict)
                and isinstance(t.get("tokens"), list)
                and isinstance(t.get("ts"), (int, float))
            ]

        self._purgar()

    def _purgar(self):
        ahora = ts_ahora()

        limite_urls = ahora - DIAS_HISTORIAL * 86400
        self.urls = {u: ts for u, ts in self.urls.items() if ts >= limite_urls}
        if len(self.urls) > MAX_URLS_HISTORIAL:
            recientes = sorted(self.urls.items(), key=lambda kv: kv[1])
            self.urls = dict(recientes[-MAX_URLS_HISTORIAL:])

        limite_titulos = ahora - HORAS_VENTANA_DEDUP * 3600
        self.titulos = [t for t in self.titulos if t["ts"] >= limite_titulos]

    def contiene(self, url):
        return normalizar_url(url) in self.urls

    def firmas_recientes(self):
        return [frozenset(t["tokens"]) for t in self.titulos]

    def registrar(self, url, tokens):
        ahora = ts_ahora()
        self.urls[normalizar_url(url)] = ahora
        if tokens:
            self.titulos.append({"tokens": sorted(tokens), "ts": ahora})

    def guardar(self):
        """Escritura atomica: un corte a mitad no deja el JSON corrupto."""
        self._purgar()
        datos = {"version": 2, "urls": self.urls, "titulos": self.titulos}
        tmp = f"{self.ruta}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.ruta)

    def __len__(self):
        return len(self.urls)


# ---------------------------------------------------------------------------
# NewsAPI
# ---------------------------------------------------------------------------


def obtener_noticias(query, dominios, page_size=20):
    """Consulta NewsAPI.

    Devuelve la lista de articulos (puede ser vacia) o None si hubo un
    error, para distinguir 'no hay noticias' de 'NewsAPI fallo'.
    La API key va en un header, no en la URL, para que no aparezca en
    mensajes de excepcion.
    """
    params = {
        "q": query,
        "domains": dominios,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": page_size,
    }
    headers = {"X-Api-Key": NEWS_API_KEY}

    for intento in range(1, MAX_REINTENTOS_NEWSAPI + 1):
        try:
            response = requests.get(URL_NEWSAPI, params=params,
                                    headers=headers, timeout=15)
        except requests.exceptions.RequestException as error:
            log.warning(f"NewsAPI error de red (intento {intento}): {error}")
            if intento < MAX_REINTENTOS_NEWSAPI:
                time.sleep(2 * intento)
                continue
            return None

        # 5xx: fallo transitorio del servidor, vale la pena reintentar.
        # 429 en NewsAPI es cuota diaria agotada: reintentar no sirve.
        if response.status_code >= 500 and intento < MAX_REINTENTOS_NEWSAPI:
            log.warning(f"NewsAPI HTTP {response.status_code} (intento {intento}), "
                        f"reintentando...")
            time.sleep(2 * intento)
            continue

        try:
            data = response.json()
        except ValueError:
            log.error(f"NewsAPI devolvio respuesta no JSON (HTTP {response.status_code}).")
            return None

        if response.status_code != 200 or data.get("status") != "ok":
            log.error(f"NewsAPI error HTTP {response.status_code}: "
                      f"{data.get('code')} - {data.get('message')}")
            return None

        return data.get("articles") or []

    return None


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def _llamar_telegram(metodo, datos):
    """Llama a la Bot API. Devuelve (ok, status_code, descripcion).
    Respeta 'retry_after' en 429 y reintenta errores 5xx/red."""
    url = f"{URL_TELEGRAM_API}/{metodo}"

    for intento in range(1, MAX_REINTENTOS_TELEGRAM + 1):
        try:
            response = requests.post(url, data=datos, timeout=30)
        except requests.exceptions.RequestException as error:
            log.warning(f"Telegram {metodo} error de red (intento {intento}): {error}")
            if intento < MAX_REINTENTOS_TELEGRAM:
                time.sleep(2 * intento)
                continue
            return False, None, str(error)

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if response.status_code == 200 and payload.get("ok"):
            return True, 200, ""

        if response.status_code == 429 and intento < MAX_REINTENTOS_TELEGRAM:
            espera = (payload.get("parameters") or {}).get("retry_after", 5)
            espera = min(int(espera), 60) + 1
            log.warning(f"Telegram 429: esperando {espera}s (intento {intento}).")
            time.sleep(espera)
            continue

        if response.status_code >= 500 and intento < MAX_REINTENTOS_TELEGRAM:
            log.warning(f"Telegram {metodo} HTTP {response.status_code} "
                        f"(intento {intento}), reintentando...")
            time.sleep(2 * intento)
            continue

        return False, response.status_code, payload.get("description") or response.text[:300]

    return False, None, "reintentos agotados"


def enviar_texto(texto, preview_url=None):
    """Envia un mensaje de texto. Con preview_url, Telegram muestra la
    vista previa de esa pagina (normalmente con su imagen principal)."""
    if DRY_RUN:
        print("\n" + "-" * 70 + "\n" + texto)
        return True

    if preview_url:
        preview = {"url": preview_url, "prefer_large_media": True,
                   "show_above_text": True}
    else:
        preview = {"is_disabled": True}

    ok, status, descripcion = _llamar_telegram("sendMessage", {
        "chat_id": CHAT_ID,
        "text": texto,
        "parse_mode": "HTML",
        "link_preview_options": json.dumps(preview),
    })
    if not ok:
        log.error(f"Telegram sendMessage fallo (HTTP {status}): {descripcion}")
    return ok


def enviar_noticia(noticia):
    """Envia la noticia como foto con texto al pie. Si no hay imagen o
    Telegram la rechaza (URL caida, formato o tamano no valido), la envia
    como texto con la vista previa del enlace.

    Solo se recurre al texto ante un rechazo definitivo (HTTP 400): ante un
    error de red la foto pudo haberse entregado y reenviar la duplicaria."""
    if DRY_RUN:
        print("\n" + "-" * 70)
        print(f"[IMAGEN] {noticia['imagen'] or '(sin imagen: vista previa del enlace)'}")
        print(noticia["mensaje"])
        return True

    if noticia["imagen"]:
        ok, status, descripcion = _llamar_telegram("sendPhoto", {
            "chat_id": CHAT_ID,
            "photo": noticia["imagen"],
            "caption": noticia["mensaje"],
            "parse_mode": "HTML",
        })
        if ok:
            return True
        if status != 400:
            log.error(f"Telegram sendPhoto fallo (HTTP {status}): {descripcion}")
            return False
        log.warning(f"Telegram rechazo la imagen ({descripcion}); se envia sin foto.")

    return enviar_texto(noticia["mensaje"], preview_url=noticia["link"])


# ---------------------------------------------------------------------------
# Preparacion (traduccion) y formato
# ---------------------------------------------------------------------------


def limpiar_texto(texto):
    """Quita HTML, entidades y saltos de linea: deja un solo parrafo."""
    texto = html.unescape(texto or "")
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip()


def recortar_parrafo(texto, maximo):
    """Recorta preferentemente al final de una oracion; si no hay una
    razonablemente cerca del limite, en el ultimo espacio con '...'."""
    if not texto or len(texto) <= maximo:
        return texto
    corte = texto[:maximo]
    fin = max(corte.rfind(". "), corte.rfind("! "), corte.rfind("? "))
    if fin > maximo * 0.5:
        return corte[:fin + 1]
    return recortar(texto, maximo - 3)


def url_imagen(articulo):
    url = (articulo.get("urlToImage") or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    return url if url.startswith(("http://", "https://")) else ""


def construir_mensaje(titulo_es, resumen_es, link):
    """Titulo + resumen de un parrafo + enlace.

    Debe caber en el texto al pie de una foto (1024 caracteres visibles):
    el resumen se ajusta al espacio que dejan el titulo y el enlace. Se
    recorta el texto plano ANTES de escapar, nunca el HTML."""
    presupuesto = (LIMITE_CAPTION - len(titulo_es) - len(TEXTO_ENLACE)
                   - 4      # dos separadores "\n\n"
                   - 10)    # margen
    partes = [f"<b>{html.escape(titulo_es)}</b>"]
    if resumen_es and presupuesto > 80:
        partes.append(html.escape(recortar_parrafo(resumen_es, presupuesto)))
    partes.append(f'<a href="{html.escape(link, quote=True)}">{TEXTO_ENLACE}</a>')
    return "\n\n".join(partes)


def preparar_noticia(articulo, categoria):
    """Traduce y arma el mensaje. Devuelve None si no es publicable."""
    titulo = limpiar_texto(articulo.get("title"))
    link = (articulo.get("url") or "").strip()

    # Resumen: la descripcion del articulo. Si viene vacia, el inicio del
    # contenido (NewsAPI lo corta y agrega '[+1234 chars]').
    resumen = limpiar_texto(articulo.get("description"))
    if not resumen:
        contenido = limpiar_texto(articulo.get("content"))
        resumen = re.sub(r"\s*\[\+\d+ chars\]$", "", contenido)

    titulo_src = recortar(titulo, MAX_LARGO_TITULO)
    resumen_src = recortar_parrafo(resumen, MAX_LARGO_DESCRIPCION)

    try:
        titulo_es = traducir_estricto(titulo_src)
    except TraduccionFallida as error:
        log.warning(f"DESCARTADA (titulo sin traducir): {titulo_src[:70]} | {error}")
        return None

    resumen_es = ""
    if resumen_src:
        try:
            resumen_es = limpiar_texto(traducir_estricto(resumen_src))
        except TraduccionFallida as error:
            if EXIGIR_DESCRIPCION_TRADUCIDA:
                log.warning(f"DESCARTADA (resumen sin traducir): "
                            f"{titulo_src[:70]} | {error}")
                return None
            log.warning("Resumen sin traducir; se publica solo el titulo.")

    return {
        "etiqueta": categoria["etiqueta"],
        "link": link,
        "imagen": url_imagen(articulo),
        "titulo_original": titulo,
        "mensaje": construir_mensaje(limpiar_texto(titulo_es), resumen_es, link),
    }


# ---------------------------------------------------------------------------
# Seleccion
# ---------------------------------------------------------------------------


class Selector:
    """Estado compartido entre todas las categorias: historial,
    deduplicacion, techo de evaluacion y circuit breaker."""

    def __init__(self, historial):
        self.historial = historial
        self.firmas_previas = historial.firmas_recientes()
        self.firmas_lote = []
        self.intentados = set()

        self.evaluadas = 0
        self.exitos = 0
        self.descartadas_traduccion = 0
        self.duplicadas = 0
        self.ya_enviadas = 0
        self.invalidas = 0
        self.fallos_consecutivos = 0

        self.detenido = False
        self.motivo_detencion = ""

    # -- control -----------------------------------------------------------

    def _detener(self, motivo, nivel=logging.ERROR):
        if not self.detenido:
            self.detenido = True
            self.motivo_detencion = motivo
            log.log(nivel, motivo)

    def puede_continuar(self):
        if self.detenido:
            return False

        if self.evaluadas >= MAX_CANDIDATOS_EVALUADOS:
            self._detener(f"Techo de {MAX_CANDIDATOS_EVALUADOS} candidatas "
                          f"evaluadas alcanzado.", logging.WARNING)
            return False

        if proveedores_agotados():
            self._detener("Proveedores de traduccion agotados.")
            return False

        umbral = (MAX_FALLOS_TRAS_UN_EXITO if self.exitos
                  else MAX_FALLOS_SIN_NINGUN_EXITO)
        if self.fallos_consecutivos >= umbral:
            if self.exitos:
                self._detener(
                    f"Circuit breaker: {self.fallos_consecutivos} fallos consecutivos "
                    f"tras {self.exitos} exitos. Proveedor degradado; se publica "
                    f"lo obtenido.")
            else:
                self._detener(
                    f"Circuit breaker: {self.fallos_consecutivos} fallos consecutivos "
                    f"sin ninguna traduccion exitosa. Traductor caido.")
            return False

        return True

    @property
    def traductor_caido(self):
        return self.exitos == 0 and self.descartadas_traduccion > 0

    # -- seleccion ---------------------------------------------------------

    def _es_duplicada(self, tokens):
        for firma in itertools.chain(self.firmas_previas, self.firmas_lote):
            if son_similares(tokens, firma):
                return True
        return False

    def seleccionar(self, articulos, cupo, categoria, relleno=False):
        """Recorre articulos traduciendo uno a uno; devuelve hasta `cupo`
        noticias publicables de esta categoria."""
        etiqueta = categoria["etiqueta"]
        elegidas = []

        for articulo in articulos:
            if len(elegidas) >= cupo or not self.puede_continuar():
                break

            link = (articulo.get("url") or "").strip()
            clave = normalizar_url(link) if link else f"id:{id(articulo)}"
            # Cada articulo se evalua una sola vez por corrida, aunque
            # aparezca en varias categorias o en la fase de relleno.
            if clave in self.intentados:
                continue
            self.intentados.add(clave)

            if not articulo_valido(articulo):
                self.invalidas += 1
                continue

            if self.historial.contiene(link):
                self.ya_enviadas += 1
                continue

            tokens = tokens_titulo(articulo.get("title"))
            if DEDUPLICAR_POR_TITULO and self._es_duplicada(tokens):
                self.duplicadas += 1
                log.info(f"[{etiqueta}] Duplicada por titulo: "
                         f"{articulo.get('title', '')[:70]}")
                continue

            self.evaluadas += 1
            preparada = preparar_noticia(articulo, categoria)

            if preparada is None:
                self.descartadas_traduccion += 1
                self.fallos_consecutivos += 1
                continue

            self.fallos_consecutivos = 0
            self.exitos += 1
            self.firmas_lote.append(tokens)
            preparada["tokens"] = tokens
            elegidas.append(preparada)

            sufijo = " (relleno)" if relleno else f" {len(elegidas)}/{cupo}"
            log.info(f"[{etiqueta}] Lista{sufijo}: {preparada['titulo_original'][:70]}")

        if not relleno and len(elegidas) < cupo:
            log.warning(f"[{etiqueta}] Solo {len(elegidas)}/{cupo} publicables.")

        return elegidas

    def resumen(self):
        return (f"Evaluadas: {self.evaluadas} | Publicables: {self.exitos} | "
                f"Descartadas por traduccion: {self.descartadas_traduccion} | "
                f"Duplicadas: {self.duplicadas} | Ya enviadas: {self.ya_enviadas} | "
                f"Invalidas: {self.invalidas}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def validar_configuracion():
    requeridas = [("NEWS_API_KEY", NEWS_API_KEY)]
    if not DRY_RUN:
        requeridas += [("TOKEN", TOKEN), ("CHAT_ID", CHAT_ID)]
    faltantes = [nombre for nombre, valor in requeridas if not valor]
    if faltantes:
        log.error(f"Faltan variables de entorno: {', '.join(faltantes)}")
        return False

    suma_cupos = sum(c["cupo"] for c in CATEGORIAS)
    if suma_cupos > MAX_NOTICIAS_POR_CORRIDA:
        log.error(f"La suma de cupos ({suma_cupos}) supera "
                  f"MAX_NOTICIAS_POR_CORRIDA ({MAX_NOTICIAS_POR_CORRIDA}).")
        return False

    etiquetas = [c["etiqueta"] for c in CATEGORIAS]
    if len(etiquetas) != len(set(etiquetas)):
        log.error("Hay etiquetas de categoria repetidas.")
        return False

    return True


def rellenar(selector, resultados, lote):
    """Si alguna categoria no lleno su cupo, reparte los lugares sobrantes
    entre las categorias con relleno=True, una noticia por turno, para
    mantener el balance tematico."""
    faltan = MAX_NOTICIAS_POR_CORRIDA - len(lote)
    if faltan <= 0:
        return lote

    rellenables = [c for c in CATEGORIAS if c["relleno"]]
    log.info(f"Relleno: faltan {faltan} noticias; se reparten entre "
             f"{', '.join(c['etiqueta'] for c in rellenables)}.")

    hubo_progreso = True
    while faltan > 0 and hubo_progreso and selector.puede_continuar():
        hubo_progreso = False
        for categoria in rellenables:
            if faltan <= 0:
                break
            extra = selector.seleccionar(resultados[categoria["etiqueta"]], 1,
                                         categoria, relleno=True)
            if extra:
                lote.extend(extra)
                faltan -= 1
                hubo_progreso = True

    return lote


def construir_encabezado():
    fecha = fecha_local().strftime("%d/%m/%Y")
    return f"<b>🌐 GLOBAL NEWS</b>\n<b>{fecha}</b>"


def main():
    if not validar_configuracion():
        return 1

    if DRY_RUN:
        log.warning("DRY_RUN activo: no se envia nada a Telegram ni se "
                    "modifica el historial.")

    proveedor = autotest()
    if proveedor is None and ABORTAR_SI_NO_HAY_TRADUCTOR:
        log.error("Sin traductor disponible. En modo estricto no se publicaria "
                  "nada; se aborta sin gastar cuota de NewsAPI.")
        return 1

    historial = Historial(ARCHIVO_HISTORIAL)
    log.info(f"Historial cargado: {len(historial)} URLs, "
             f"{len(historial.titulos)} titulares recientes.")

    # --- Consulta por categoria ------------------------------------------
    log.info("Consultando NewsAPI...")
    resultados = {}
    errores_api = 0
    for categoria in CATEGORIAS:
        articulos = obtener_noticias(categoria["query"], categoria["dominios"],
                                     categoria["page_size"])
        if articulos is None:
            errores_api += 1
            articulos = []
        log.info(f"[{categoria['etiqueta']}] NewsAPI devolvio {len(articulos)} articulos.")
        resultados[categoria["etiqueta"]] = articulos

    if errores_api == len(CATEGORIAS):
        log.error("Todas las consultas a NewsAPI fallaron.")
        return 1

    if not any(resultados.values()):
        log.info("Ninguna consulta devolvio articulos. No se publica nada.")
        return 0

    # --- Seleccion (antes de enviar nada: sin encabezados huerfanos) -----
    selector = Selector(historial)
    lote = []
    for categoria in CATEGORIAS:
        lote += selector.seleccionar(resultados[categoria["etiqueta"]],
                                     categoria["cupo"], categoria)

    lote = rellenar(selector, resultados, lote)

    # Publicar agrupado por categoria, en el orden de CATEGORIAS.
    orden = {c["etiqueta"]: i for i, c in enumerate(CATEGORIAS)}
    lote.sort(key=lambda n: orden[n["etiqueta"]])

    guardar_cache()
    log.info(selector.resumen())

    if not lote:
        log.warning("Ninguna noticia resulto publicable. No se envia nada.")
        log.info(resumen_stats())
        return 1 if (errores_api or selector.traductor_caido) else 0

    # --- Envio -----------------------------------------------------------
    if not enviar_texto(construir_encabezado()):
        log.error("No se pudo enviar el encabezado; se aborta sin tocar el historial.")
        return 1

    enviadas = 0
    fallidas = 0

    for noticia in lote:
        if not DRY_RUN:
            time.sleep(PAUSA_ENTRE_MENSAJES)

        if enviar_noticia(noticia):
            historial.registrar(noticia["link"], noticia["tokens"])
            if not DRY_RUN:
                # Guardado incremental: si el job muere a mitad, lo ya
                # enviado queda registrado y no se republica.
                historial.guardar()
            enviadas += 1
        else:
            fallidas += 1
            log.warning(f"No se pudo enviar (se reintentara en otra corrida): "
                        f"{noticia['link']}")

    log.info(f"Total enviadas: {enviadas} | Fallidas: {fallidas}")
    log.info(resumen_stats())

    return 2 if fallidas else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.exception("Error inesperado")
        sys.exit(1)
