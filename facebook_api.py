"""
facebook_api.py
Conexión con la Facebook Marketing API mediante el SDK facebook-business.

Responsabilidades:
  - Cargar los anuncios activos de la cuenta (ad_id, nombre, adset_id, presupuesto).
  - Polling en segundo plano (hilo aparte) cada N segundos para detectar cambios
    de daily_budget a nivel de Adset y disparar el cierre/apertura de período.
  - Modificar el daily_budget de un Adset en Facebook (Parte 8).
  - Duplicar anuncios con /{ad-id}/copies deep_copy=true (Parte 7).

Todo el módulo tolera que Facebook no responda: nunca tumba la app.
El estado del último polling / errores se expone para mostrarlo en la UI.
"""
import threading
import time
import traceback
from typing import Optional

import config
import db

# --- Import defensivo del SDK: si no está instalado la app sigue viva ---
SDK_DISPONIBLE = True
SDK_ERROR_IMPORT = None
try:
    from facebook_business.api import FacebookAdsApi
    from facebook_business.adobjects.adaccount import AdAccount
    from facebook_business.adobjects.ad import Ad
    from facebook_business.adobjects.adset import AdSet
    from facebook_business.exceptions import FacebookRequestError
except Exception as e:  # pragma: no cover - depende del entorno
    SDK_DISPONIBLE = False
    SDK_ERROR_IMPORT = str(e)
    FacebookRequestError = Exception  # fallback para except


# Estado compartido con la UI (protegido por lock).
_ESTADO_LOCK = threading.Lock()
ESTADO = {
    "api_ok": False,
    "ultimo_polling": None,      # datetime
    "ultimo_error": None,        # str
    "num_anuncios": 0,
    "polling_activo": False,
    "mensajes": [],              # historial corto de eventos
}

_POLLING_THREAD = None
_POLLING_STOP = threading.Event()
_API_INICIALIZADA = False


def _log(msg: str) -> None:
    with _ESTADO_LOCK:
        ts = db.a_texto(db.ahora())
        ESTADO["mensajes"].insert(0, f"[{ts}] {msg}")
        del ESTADO["mensajes"][20:]  # conservar solo los últimos 20


def _set_estado(**kwargs) -> None:
    with _ESTADO_LOCK:
        ESTADO.update(kwargs)


def obtener_estado() -> dict:
    with _ESTADO_LOCK:
        return dict(ESTADO)


# --------------------------------------------------------------------------- #
#  Inicialización de la API
# --------------------------------------------------------------------------- #
def inicializar_api() -> bool:
    """Inicializa el SDK de Facebook. Devuelve True si quedó lista."""
    global _API_INICIALIZADA
    if not SDK_DISPONIBLE:
        _set_estado(api_ok=False, ultimo_error=f"SDK no disponible: {SDK_ERROR_IMPORT}")
        return False
    if not config.facebook_configurado():
        _set_estado(api_ok=False,
                    ultimo_error="Faltan credenciales en .env (APP_ID/APP_SECRET/ACCESS_TOKEN/AD_ACCOUNT_ID)")
        return False
    try:
        FacebookAdsApi.init(config.APP_ID, config.APP_SECRET, config.ACCESS_TOKEN)
        _API_INICIALIZADA = True
        _set_estado(api_ok=True, ultimo_error=None)
        return True
    except Exception as e:
        _set_estado(api_ok=False, ultimo_error=f"Error init API: {e}")
        return False


def _cuenta() -> "AdAccount":
    return AdAccount(config.AD_ACCOUNT_ID)


# --------------------------------------------------------------------------- #
#  Carga de anuncios activos
# --------------------------------------------------------------------------- #
def cargar_anuncios_activos(abrir_periodos: bool = True) -> dict:
    """
    Descarga los anuncios activos de la cuenta y los guarda en SQLite.
    Para cada anuncio obtiene también su adset_id y el daily_budget del adset.

    Devuelve {"ok": bool, "anuncios": [...], "error": str|None}.
    """
    if not (_API_INICIALIZADA or inicializar_api()):
        return {"ok": False, "anuncios": [], "error": obtener_estado()["ultimo_error"]}

    try:
        ads = _cuenta().get_ads(
            params={"effective_status": ["ACTIVE"], "limit": 500},
            fields=[
                Ad.Field.id,
                Ad.Field.name,
                Ad.Field.adset_id,
                Ad.Field.effective_status,
            ],
        )

        anuncios = []
        cache_budget = {}  # adset_id -> presupuesto (unidades de moneda)

        for ad in ads:
            ad_id = ad.get(Ad.Field.id)
            nombre = ad.get(Ad.Field.name) or f"Anuncio {ad_id}"
            adset_id = ad.get(Ad.Field.adset_id)

            presupuesto = None
            if adset_id:
                if adset_id in cache_budget:
                    presupuesto = cache_budget[adset_id]
                else:
                    presupuesto = _leer_daily_budget_adset(adset_id)
                    cache_budget[adset_id] = presupuesto

            db.upsert_anuncio(ad_id, nombre, adset_id=adset_id, activo=1)

            if abrir_periodos and presupuesto is not None:
                db.asegurar_periodo_inicial(ad_id, presupuesto)

            anuncios.append({
                "ad_id": ad_id, "nombre": nombre,
                "adset_id": adset_id, "presupuesto": presupuesto,
            })

        db.marcar_inactivos([a["ad_id"] for a in anuncios])
        _set_estado(api_ok=True, num_anuncios=len(anuncios),
                    ultimo_error=None, ultimo_polling=db.ahora())
        _log(f"Cargados {len(anuncios)} anuncios activos.")
        return {"ok": True, "anuncios": anuncios, "error": None}

    except FacebookRequestError as e:
        msg = _fmt_fb_error(e)
        _set_estado(api_ok=False, ultimo_error=msg)
        _log(f"Error cargando anuncios: {msg}")
        return {"ok": False, "anuncios": [], "error": msg}
    except Exception as e:
        _set_estado(api_ok=False, ultimo_error=str(e))
        _log(f"Error inesperado cargando anuncios: {e}")
        return {"ok": False, "anuncios": [], "error": str(e)}


def _leer_daily_budget_adset(adset_id: str) -> Optional[float]:
    """Lee el daily_budget de un adset (viene en centavos) y lo pasa a unidades."""
    try:
        adset = AdSet(adset_id).api_get(fields=[AdSet.Field.daily_budget])
        raw = adset.get(AdSet.Field.daily_budget)
        if raw is None:
            return None
        return float(raw) / 100.0
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Modificación de presupuesto (Parte 8)
# --------------------------------------------------------------------------- #
def actualizar_presupuesto_facebook(adset_id: str, nuevo_monto: float) -> dict:
    """
    POST /{adset-id} con daily_budget en centavos (monto * 100).
    Devuelve {"ok": bool, "error": str|None}.
    NO toca SQLite: el llamador decide cerrar/abrir período solo si ok=True.
    """
    if not (_API_INICIALIZADA or inicializar_api()):
        return {"ok": False, "error": obtener_estado()["ultimo_error"]}
    if not adset_id:
        return {"ok": False, "error": "Este anuncio no tiene adset_id guardado. "
                                       "Recarga los anuncios desde Facebook."}
    try:
        centavos = int(round(float(nuevo_monto) * 100))
        adset = AdSet(adset_id)
        adset.api_update(params={AdSet.Field.daily_budget: centavos})
        _log(f"Presupuesto de adset {adset_id} actualizado a {nuevo_monto:.2f}.")
        return {"ok": True, "error": None}
    except FacebookRequestError as e:
        return {"ok": False, "error": _fmt_fb_error(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------- #
#  Duplicación de anuncios (Parte 7)
# --------------------------------------------------------------------------- #
def duplicar_anuncio(ad_id: str, num_copias: int, presupuesto: float,
                     activar: bool = False) -> dict:
    """
    Duplica un anuncio `num_copias` veces con /{ad-id}/copies deep_copy=true.
    Cada copia:
      - se crea PAUSED (o ACTIVE si activar=True),
      - recibe el presupuesto indicado en su nuevo adset,
      - se guarda en `anuncios` y se le abre un período nuevo.

    Devuelve {"exitosas": [...], "fallidas": [...], "error_global": str|None}.
    Nunca lanza excepción: los errores por copia se acumulan.
    """
    resultado = {"exitosas": [], "fallidas": [], "error_global": None}

    if not (_API_INICIALIZADA or inicializar_api()):
        resultado["error_global"] = obtener_estado()["ultimo_error"]
        return resultado

    num_copias = max(1, min(int(num_copias), 10))
    original = db.obtener_anuncio(ad_id)
    nombre_base = original["nombre"] if original else f"Anuncio {ad_id}"
    status_copia = "ACTIVE" if activar else "PAUSED"

    for i in range(1, num_copias + 1):
        nombre_copia = f"{nombre_base} - Copia {i}"
        try:
            ad = Ad(ad_id)
            # El endpoint /copies con deep_copy=true copia adset + creativo.
            copia = ad.create_copy(params={
                "deep_copy": True,
                "status_option": status_copia,
            })

            nuevo_ad_id = copia.get("copied_ad_id") or copia.get("ad_id") or copia.get("id")
            # Estructura de retorno variable: intentamos resolver el adset nuevo.
            nuevo_adset_id = _resolver_adset_de_copia(copia, nuevo_ad_id)

            if not nuevo_ad_id:
                raise RuntimeError("Facebook no devolvió el ad_id de la copia.")

            # Asignar presupuesto a la copia (en su adset nuevo).
            if nuevo_adset_id:
                r = actualizar_presupuesto_facebook(nuevo_adset_id, presupuesto)
                if not r["ok"]:
                    _log(f"Copia {nuevo_ad_id} creada pero no se pudo fijar presupuesto: {r['error']}")

            # Guardar en SQLite + abrir período nuevo.
            db.upsert_anuncio(nuevo_ad_id, nombre_copia, adset_id=nuevo_adset_id, activo=1)
            db.abrir_periodo(nuevo_ad_id, float(presupuesto))

            resultado["exitosas"].append({
                "ad_id": nuevo_ad_id, "nombre": nombre_copia,
                "adset_id": nuevo_adset_id, "presupuesto": presupuesto,
            })
            _log(f"Copia creada: {nombre_copia} ({nuevo_ad_id}).")

        except FacebookRequestError as e:
            resultado["fallidas"].append({"nombre": nombre_copia, "error": _fmt_fb_error(e)})
        except Exception as e:
            resultado["fallidas"].append({"nombre": nombre_copia, "error": str(e)})

    return resultado


def _resolver_adset_de_copia(copia: dict, nuevo_ad_id: Optional[str]) -> Optional[str]:
    """Intenta extraer el adset_id de la respuesta de /copies o consultándolo."""
    # Algunas respuestas traen ad_object_ids con la estructura de copiado.
    try:
        obj = copia.get("ad_object_ids") or {}
        adsets = obj.get("adsets")
        if isinstance(adsets, list) and adsets:
            primero = adsets[0]
            if isinstance(primero, dict):
                return primero.get("copied_id") or primero.get("id")
            return str(primero)
    except Exception:
        pass
    # Fallback: consultar el ad recién creado por su adset_id.
    if nuevo_ad_id:
        try:
            ad = Ad(nuevo_ad_id).api_get(fields=[Ad.Field.adset_id])
            return ad.get(Ad.Field.adset_id)
        except Exception:
            return None
    return None


# --------------------------------------------------------------------------- #
#  Polling en segundo plano
# --------------------------------------------------------------------------- #
def _revisar_cambios_presupuesto() -> None:
    """
    Recorre los anuncios activos, lee el daily_budget actual de su adset y,
    si cambió respecto al período abierto, cierra ese período y abre uno nuevo.
    """
    anuncios = db.obtener_anuncios(solo_activos=True)
    cache_budget = {}
    cambios = 0
    for anuncio in anuncios:
        adset_id = anuncio.get("adset_id")
        if not adset_id:
            continue
        if adset_id in cache_budget:
            actual = cache_budget[adset_id]
        else:
            actual = _leer_daily_budget_adset(adset_id)
            cache_budget[adset_id] = actual
        if actual is None:
            continue

        abierto = db.periodo_abierto(anuncio["ad_id"])
        if abierto is None:
            db.abrir_periodo(anuncio["ad_id"], actual)
            continue

        # Comparación con tolerancia de 1 centavo.
        if abs(float(abierto["presupuesto"]) - float(actual)) > 0.01:
            db.cambiar_periodo(anuncio["ad_id"], actual)
            cambios += 1
            _log(f"Cambio detectado en '{anuncio['nombre']}': "
                 f"{abierto['presupuesto']:.2f} → {actual:.2f}. Nuevo período abierto.")
    if cambios:
        _log(f"Polling: {cambios} cambio(s) de presupuesto aplicados.")


def _loop_polling() -> None:
    _set_estado(polling_activo=True)
    # Primera carga inmediata.
    try:
        inicializar_api()
        cargar_anuncios_activos()
    except Exception as e:
        _log(f"Fallo en carga inicial: {e}")

    while not _POLLING_STOP.wait(config.POLLING_INTERVAL_SEG):
        try:
            cargar_anuncios_activos(abrir_periodos=True)
            _revisar_cambios_presupuesto()
            _set_estado(ultimo_polling=db.ahora())
        except Exception as e:
            _set_estado(ultimo_error=str(e))
            _log(f"Error en polling: {e}\n{traceback.format_exc(limit=1)}")
    _set_estado(polling_activo=False)


def iniciar_polling() -> None:
    """Arranca el hilo de polling una sola vez por proceso."""
    global _POLLING_THREAD
    if _POLLING_THREAD and _POLLING_THREAD.is_alive():
        return
    _POLLING_STOP.clear()
    _POLLING_THREAD = threading.Thread(target=_loop_polling, name="fb-polling", daemon=True)
    _POLLING_THREAD.start()
    _log("Hilo de polling de Facebook iniciado.")


def detener_polling() -> None:
    _POLLING_STOP.set()


# --------------------------------------------------------------------------- #
#  Utilidades
# --------------------------------------------------------------------------- #
def _fmt_fb_error(e) -> str:
    """Extrae el mensaje legible de un FacebookRequestError."""
    try:
        msg = e.api_error_message() or ""
        code = e.api_error_code()
        sub = e.api_error_subcode()
        partes = [p for p in [msg, f"(code {code}" + (f"/{sub}" if sub else "") + ")"] if p]
        return " ".join(partes).strip() or str(e)
    except Exception:
        return str(e)
