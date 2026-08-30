"""
facebook_api.py
Conexión con la Facebook Marketing API — MULTI-BUSINESS / MULTI-TOKEN.

Cada "conexión" (fila en la tabla `conexiones`) representa un Business con su
token de Usuario del Sistema. Una app central (APP_ID/APP_SECRET del .env) se usa
por defecto, pero cada conexión puede traer su propio app_id/app_secret.

La app:
  - Descubre automáticamente TODAS las cuentas publicitarias accesibles por cada
    token (no hay que listar los act_ a mano).
  - Carga los anuncios (activos y pausados) de todas las cuentas de todas las
    conexiones y los guarda con su conexion_id / cuenta_id / cuenta_nombre.
  - Trae Insights (gasto, CPM, CTR, conversaciones) por anuncio, agregando todo.
  - Cambia presupuesto y duplica usando el token correcto de cada anuncio.

Compatibilidad: si NO hay conexiones en la BD pero el .env tiene credenciales de
una sola cuenta, se usa esa como conexión implícita (conexion_id = 0).

Nada tumba la app: los errores por conexión/cuenta se acumulan y se muestran.
"""
import os
import threading
import traceback
from typing import Optional

import config
import db

# --- Import defensivo del SDK ---
SDK_DISPONIBLE = True
SDK_ERROR_IMPORT = None
try:
    from facebook_business.api import FacebookAdsApi
    from facebook_business.session import FacebookSession
    from facebook_business.adobjects.adaccount import AdAccount
    from facebook_business.adobjects.user import User
    from facebook_business.adobjects.ad import Ad
    from facebook_business.adobjects.adset import AdSet
    from facebook_business.adobjects.campaign import Campaign
    from facebook_business.adobjects.adcreative import AdCreative
    from facebook_business.exceptions import FacebookRequestError
except Exception as e:  # pragma: no cover
    SDK_DISPONIBLE = False
    SDK_ERROR_IMPORT = str(e)
    FacebookRequestError = Exception

# ID de la conexión implícita basada en el .env (una sola cuenta).
ENV_CONEXION_ID = 0

# Tipos de acción que cuentan como "conversación iniciada" (mensajería).
_MSG_DEFAULT = [
    "onsite_conversion.messaging_conversation_started_7d",
    "onsite_conversion.total_messaging_connection",
    "messaging_conversation_started_7d",
]
MSG_ACTION_TYPES = [t.strip() for t in
                    os.getenv("MSG_ACTION_TYPES", ",".join(_MSG_DEFAULT)).split(",")
                    if t.strip()]

# Tipos de acción que cuentan como COMPRA (para las ventas/ingresos de Meta).
PURCHASE_ACTION_TYPES = {
    "purchase", "omni_purchase", "offsite_conversion.fb_pixel_purchase",
    "onsite_web_purchase", "onsite_conversion.purchase",
    "web_in_store_purchase", "onsite_web_app_purchase",
}

# Estado compartido con la UI.
_ESTADO_LOCK = threading.Lock()
ESTADO = {
    "api_ok": False,
    "ultimo_polling": None,
    "ultimo_error": None,
    "num_anuncios": 0,
    "num_cuentas": 0,
    "num_conexiones": 0,
    "polling_activo": False,
    "uso_api": None,        # % de uso máx. reportado por Facebook (0-100)
    "throttled": False,     # True si Facebook nos frenó en el último ciclo
    "throttled_ts": None,   # cuándo fue el último freno (para que el aviso se limpie solo)
    "llamados_ciclo": 0,    # llamados a Facebook en el último ciclo
    "pendientes_presupuesto": 0,  # cambios de presupuesto en cola por el límite
    "mensajes": [],
}

# --- Control de ritmo / límite de la API (rate limiting) ------------------- #
# Pausa breve entre cuentas para no golpearlas todas a la vez.
STAGGER_SEG = float(os.getenv("FB_STAGGER_SEG", "0.7"))
# Si el uso reportado supera este %, saltamos la cuenta y la reintentamos luego.
UMBRAL_USO = int(os.getenv("FB_UMBRAL_USO", "80"))
# Códigos de error de Facebook que significan "vas muy rápido / límite".
_RATE_LIMIT_CODES = {4, 17, 32, 613, 80000, 80001, 80002, 80003, 80004,
                     80005, 80006, 80008, 80009, 80014}


def _es_rate_limit(e) -> bool:
    """True si el error es por límite de frecuencia de Facebook."""
    try:
        if int(e.api_error_code()) in _RATE_LIMIT_CODES:
            return True
    except Exception:
        pass
    txt = str(e).lower()
    return ("call to this ad-account" in txt or "rate limit" in txt
            or "too many calls" in txt or "#80004" in txt or "user request limit" in txt)


def _uso_de_headers(headers) -> Optional[int]:
    """Extrae el % de uso máximo del header x-business-use-case-usage (0-100)."""
    import json
    try:
        raw = None
        for k in ("x-business-use-case-usage", "X-Business-Use-Case-Usage",
                  "x-ad-account-usage", "X-Ad-Account-Usage"):
            if headers and headers.get(k):
                raw = headers.get(k)
                break
        if not raw:
            return None
        data = json.loads(raw)
        maxp = 0
        vals = data.values() if isinstance(data, dict) else [data]
        for lst in vals:
            for item in (lst if isinstance(lst, list) else [lst]):
                if isinstance(item, dict):
                    for kk in ("call_count", "total_cputime", "total_time",
                               "acc_id_util_pct"):
                        try:
                            maxp = max(maxp, int(item.get(kk, 0) or 0))
                        except Exception:
                            pass
        return maxp
    except Exception:
        return None


def _uso_de_error(e) -> Optional[int]:
    try:
        return _uso_de_headers(e.http_headers())
    except Exception:
        return None


def _dormir(seg: float) -> None:
    """Pausa que respeta la señal de parada del hilo (no bloquea el apagado)."""
    if seg > 0:
        _POLLING_STOP.wait(seg)


def _contar_llamado() -> None:
    with _ESTADO_LOCK:
        ESTADO["llamados_ciclo"] = ESTADO.get("llamados_ciclo", 0) + 1


def _reiniciar_ciclo() -> None:
    with _ESTADO_LOCK:
        ESTADO["llamados_ciclo"] = 0
        ESTADO["throttled"] = False


def _marcar_throttle(e, nombre_cuenta: str) -> None:
    uso = _uso_de_error(e)
    _set_estado(throttled=True, throttled_ts=db.ahora(),
                uso_api=(uso if uso is not None else ESTADO.get("uso_api")),
                ultimo_error=("Facebook aplicó su límite de frecuencia; "
                              "bajando el ritmo y reintentando en el próximo ciclo."))
    _log(f"Límite de Facebook en {nombre_cuenta} "
         f"(uso {uso if uso is not None else '?'}%). Salto el resto de esta conexión.")


def _throttled_reciente(minutos: int = 12) -> bool:
    """True solo si hubo un freno de Facebook en los últimos `minutos`.
    Así el aviso de la UI se limpia solo cuando ya se recuperó."""
    with _ESTADO_LOCK:
        if not ESTADO.get("throttled"):
            return False
        ts = ESTADO.get("throttled_ts")
    if not ts:
        return False
    try:
        return (db.ahora() - ts).total_seconds() < minutos * 60
    except Exception:
        return True

_POLLING_THREAD = None
_POLLING_STOP = threading.Event()


def _log(msg: str) -> None:
    with _ESTADO_LOCK:
        ts = db.a_texto(db.ahora())
        ESTADO["mensajes"].insert(0, f"[{ts}] {msg}")
        del ESTADO["mensajes"][30:]


def _set_estado(**kwargs) -> None:
    with _ESTADO_LOCK:
        ESTADO.update(kwargs)


def obtener_estado() -> dict:
    with _ESTADO_LOCK:
        return dict(ESTADO)


# --------------------------------------------------------------------------- #
#  Construcción de sesiones/API por conexión
# --------------------------------------------------------------------------- #
def _api_desde(token: str, app_id: Optional[str], app_secret: Optional[str]):
    """Crea una instancia FacebookAdsApi aislada (no toca el default global)."""
    session = FacebookSession(
        app_id=app_id or config.APP_ID or None,
        app_secret=app_secret or config.APP_SECRET or None,
        access_token=token,
    )
    return FacebookAdsApi(session)


def _conexiones_efectivas() -> list:
    """
    Lista de conexiones a usar: las activas de la BD y, si no hay ninguna pero el
    .env tiene credenciales, una conexión implícita basada en el .env.
    Cada item: {"id","alias","token","app_id","app_secret","env_account"}.
    """
    cons = [c for c in db.obtener_conexiones(solo_activas=True) if c.get("token")]
    if cons:
        return [{
            "id": c["id"], "alias": c.get("alias") or f"Conexión {c['id']}",
            "token": c["token"], "app_id": c.get("app_id"),
            "app_secret": c.get("app_secret"), "env_account": None,
        } for c in cons]
    # Fallback al .env (una sola cuenta).
    if config.facebook_configurado():
        return [{
            "id": ENV_CONEXION_ID, "alias": "ENV (.env)",
            "token": config.ACCESS_TOKEN, "app_id": config.APP_ID,
            "app_secret": config.APP_SECRET, "env_account": config.AD_ACCOUNT_ID,
        }]
    return []


def _api_para_conexion(conexion_id: Optional[int]):
    """Devuelve un FacebookAdsApi para la conexión indicada (o None)."""
    if not SDK_DISPONIBLE:
        return None
    if conexion_id in (None, ENV_CONEXION_ID):
        if config.facebook_configurado():
            return _api_desde(config.ACCESS_TOKEN, config.APP_ID, config.APP_SECRET)
        return None
    c = db.obtener_conexion(conexion_id)
    if not c or not c.get("token"):
        return None
    return _api_desde(c["token"], c.get("app_id"), c.get("app_secret"))


def inicializar_api() -> bool:
    """Compat: True si hay al menos una conexión utilizable."""
    if not SDK_DISPONIBLE:
        _set_estado(api_ok=False, ultimo_error=f"SDK no disponible: {SDK_ERROR_IMPORT}")
        return False
    cons = _conexiones_efectivas()
    if not cons:
        _set_estado(api_ok=False,
                    ultimo_error="No hay conexiones. Agrega un token en 'Conexiones' "
                                 "o define credenciales en .env.")
        return False
    _set_estado(num_conexiones=len(cons))
    return True


# --------------------------------------------------------------------------- #
#  Descubrimiento de cuentas
# --------------------------------------------------------------------------- #
def _descubrir_cuentas(api, env_account: Optional[str] = None) -> list:
    """
    Devuelve [{"act_id","account_id","name"}] accesibles por el token.
    Si env_account está dado (conexión .env), usa solo esa cuenta.
    """
    campos = ["account_id", "name", "business_country_code", "currency"]
    if env_account:
        try:
            acc = AdAccount(env_account, api=api).api_get(fields=campos)
            return [{"act_id": env_account,
                     "account_id": acc.get("account_id"),
                     "name": acc.get("name") or env_account,
                     "pais": acc.get("business_country_code"),
                     "moneda": acc.get("currency")}]
        except Exception:
            return [{"act_id": env_account, "account_id": env_account.replace("act_", ""),
                     "name": env_account, "pais": None, "moneda": None}]
    cuentas = []
    accts = User(fbid="me", api=api).get_ad_accounts(fields=campos, params={"limit": 200})
    for a in accts:
        acc_id = a.get("account_id")
        act_id = a.get("id") or (f"act_{acc_id}" if acc_id else None)
        if not act_id:
            continue
        cuentas.append({"act_id": act_id, "account_id": acc_id,
                        "name": a.get("name") or act_id,
                        "pais": a.get("business_country_code"),
                        "moneda": a.get("currency")})
    return cuentas


def probar_conexion(token: str, app_id: Optional[str] = None,
                    app_secret: Optional[str] = None) -> dict:
    """
    Prueba un token y descubre sus cuentas (para la UI 'Conexiones').
    Devuelve {"ok","cuentas":[...],"error"}.
    """
    if not SDK_DISPONIBLE:
        return {"ok": False, "cuentas": [], "error": f"SDK no disponible: {SDK_ERROR_IMPORT}"}
    try:
        api = _api_desde(token, app_id, app_secret)
        cuentas = _descubrir_cuentas(api)
        return {"ok": True, "cuentas": cuentas, "error": None}
    except FacebookRequestError as e:
        return {"ok": False, "cuentas": [], "error": _fmt_fb_error(e)}
    except Exception as e:
        return {"ok": False, "cuentas": [], "error": str(e)}


# --------------------------------------------------------------------------- #
#  Carga de anuncios de TODAS las conexiones/cuentas
# --------------------------------------------------------------------------- #
def cargar_todo(abrir_periodos: bool = True) -> dict:
    """
    Recorre todas las conexiones → cuentas → anuncios (activos y pausados) y los
    guarda. Devuelve {"ok","num_anuncios","num_cuentas","errores":[...]}.
    """
    if not SDK_DISPONIBLE:
        _set_estado(api_ok=False, ultimo_error=f"SDK no disponible: {SDK_ERROR_IMPORT}")
        return {"ok": False, "num_anuncios": 0, "num_cuentas": 0,
                "errores": [SDK_ERROR_IMPORT]}

    cons = _conexiones_efectivas()
    if not cons:
        msg = "No hay conexiones configuradas."
        _set_estado(api_ok=False, ultimo_error=msg)
        return {"ok": False, "num_anuncios": 0, "num_cuentas": 0, "errores": [msg]}

    _reiniciar_ciclo()
    total_ads = 0
    total_activos = 0
    total_cuentas = 0
    ids_vistos = []
    errores = []

    for con in cons:
        try:
            api = _api_desde(con["token"], con["app_id"], con["app_secret"])
        except Exception as e:
            errores.append(f"{con['alias']}: {e}")
            continue

        try:
            cuentas = _descubrir_cuentas(api, con.get("env_account"))
        except FacebookRequestError as e:
            err = _fmt_fb_error(e)
            errores.append(f"{con['alias']}: {err}")
            if con["id"] != ENV_CONEXION_ID:
                db.actualizar_conexion(con["id"], ultimo_error=err)
            continue
        except Exception as e:
            errores.append(f"{con['alias']}: {e}")
            continue

        total_cuentas += len(cuentas)
        cache_budget = {}
        cache_camp = {}

        for idx, cta in enumerate(cuentas):
            if idx:
                _dormir(STAGGER_SEG)  # espaciar cuentas (control de ritmo)
            try:
                _contar_llamado()
                ads = AdAccount(cta["act_id"], api=api).get_ads(
                    params={"effective_status": ["ACTIVE", "PAUSED",
                                                 "ADSET_PAUSED", "CAMPAIGN_PAUSED"],
                            "limit": 500},
                    fields=["id", "name", "adset_id", "campaign_id",
                        "effective_status", "created_time",
                        "campaign{name}",
                        # Presupuesto y país (targeting) VIENEN aquí mismo, para NO
                        # hacer un llamado extra por cada conjunto (evita el rate limit).
                        "adset{name,daily_budget,targeting}"],
                )
            except FacebookRequestError as e:
                if _es_rate_limit(e):
                    _marcar_throttle(e, cta["name"])
                    errores.append(f"{con['alias']}/{cta['name']}: límite de Facebook (reintento luego)")
                    break  # salto el resto de cuentas de esta conexión este ciclo
                errores.append(f"{con['alias']}/{cta['name']}: {_fmt_fb_error(e)}")
                continue
            except Exception as e:
                errores.append(f"{con['alias']}/{cta['name']}: {e}")
                continue

            for ad in ads:
                ad_id = ad.get(Ad.Field.id)
                nombre = ad.get(Ad.Field.name) or f"Anuncio {ad_id}"
                adset_id = ad.get(Ad.Field.adset_id)
                campaign_id = ad.get("campaign_id")
                camp = _a_dict(ad.get("campaign"))
                aset = _a_dict(ad.get("adset"))
                campaign_nombre = camp.get("name")
                adset_nombre = aset.get("name")
                estado = ad.get(Ad.Field.effective_status) or ""
                creado = ad.get(Ad.Field.created_time)
                es_activo = 1 if estado == "ACTIVE" else 0
                total_ads += 1
                if es_activo:
                    total_activos += 1

                presupuesto = None
                pais_pauta = None
                budget_nivel = None
                budget_obj_id = None
                if adset_id:
                    if adset_id in cache_budget:
                        info = cache_budget[adset_id]
                    else:
                        # Presupuesto + país SALEN del propio adset (ya vino en get_ads),
                        # así NO hacemos un llamado extra por conjunto.
                        raw_b = aset.get("daily_budget")
                        if raw_b is not None or aset.get("targeting") is not None:
                            codigos = _paises_de_targeting(aset.get("targeting"))
                            info = {
                                "presupuesto": (float(raw_b) / 100.0) if raw_b not in (None, "") else None,
                                "pais": (", ".join(pais_nombre(c) for c in codigos[:3]) if codigos else None),
                            }
                        else:  # respaldo (rara vez): llamado extra
                            info = _leer_adset_info(adset_id, api)
                        cache_budget[adset_id] = info
                    presupuesto = info.get("presupuesto")
                    pais_pauta = info.get("pais")
                    if presupuesto is not None:
                        budget_nivel = "adset"
                        budget_obj_id = adset_id

                # Si el conjunto no tiene presupuesto propio, es CBO (a nivel de campaña).
                if presupuesto is None and campaign_id:
                    if campaign_id in cache_camp:
                        presupuesto = cache_camp[campaign_id]
                    else:
                        presupuesto = _leer_campaign_budget(campaign_id, api)
                        cache_camp[campaign_id] = presupuesto
                    if presupuesto is not None:
                        budget_nivel = "campaign"
                        budget_obj_id = campaign_id

                # País donde está PAUTADO (targeting). NO se usa el país de la
                # cuenta como respaldo porque suele ser el del negocio, no el de la pauta.
                pais = pais_pauta

                db.upsert_anuncio(
                    ad_id, nombre, adset_id=adset_id, activo=es_activo,
                    fecha_creacion=creado, effective_status=estado,
                    conexion_id=con["id"], cuenta_id=cta["act_id"],
                    cuenta_nombre=cta["name"], cuenta_pais=pais,
                    cuenta_moneda=cta.get("moneda"),
                    campaign_id=campaign_id, campaign_nombre=campaign_nombre,
                    adset_nombre=adset_nombre,
                    budget_nivel=budget_nivel, budget_obj_id=budget_obj_id,
                )
                ids_vistos.append(ad_id)

                if abrir_periodos and es_activo and presupuesto is not None:
                    # Ancla el período inicial a la fecha de CREACIÓN del anuncio
                    # (no a la hora de carga). Y detecta el cambio de presupuesto AQUÍ
                    # mismo (con el valor ya traído), sin llamadas extra por conjunto.
                    creado_dt = db.a_fecha(str(creado)[:19].replace("T", " ")) if creado else None
                    ab = db.periodo_abierto(ad_id)
                    if ab is None:
                        db.asegurar_periodo_inicial(ad_id, presupuesto, creado_dt)
                    elif abs(float(ab["presupuesto"]) - float(presupuesto)) > 0.01:
                        db.cambiar_periodo(ad_id, presupuesto)
                        _log(f"Cambio de presupuesto en '{nombre}': "
                             f"{float(ab['presupuesto']):.2f} → {float(presupuesto):.2f}.")

        if con["id"] != ENV_CONEXION_ID:
            db.actualizar_conexion(con["id"], ultimo_error=None)

    if ids_vistos:
        db.marcar_inactivos(ids_vistos)

    _set_estado(api_ok=(total_ads > 0 or not errores),
                num_anuncios=total_activos, num_cuentas=total_cuentas,
                num_conexiones=len(cons),
                ultimo_error=("; ".join(errores)[:400] if errores else None),
                ultimo_polling=db.ahora())
    # Marca de tiempo persistente del último "arrastre" completo de anuncios
    # (la lee la UI para el timer "última actualización hace X min").
    try:
        db.set_config("ultima_actualizacion", db.a_texto(db.ahora()))
    except Exception:
        pass
    _log(f"Cargados {total_ads} anuncios ({total_activos} activos) de "
         f"{total_cuentas} cuentas en {len(cons)} conexión(es). "
         f"{len(errores)} error(es).")
    return {"ok": not errores or total_ads > 0, "num_anuncios": total_ads,
            "num_cuentas": total_cuentas, "errores": errores}


# Alias de compatibilidad (app y polling llaman a este nombre).
def cargar_anuncios_activos(abrir_periodos: bool = True) -> dict:
    r = cargar_todo(abrir_periodos=abrir_periodos)
    return {"ok": r["ok"], "anuncios": [], "error": "; ".join(r["errores"]) or None}


# Códigos ISO de país -> nombre en español (los más usados en LatAm/mercados).
PAISES = {
    "MX": "México", "CO": "Colombia", "US": "Estados Unidos", "AR": "Argentina",
    "CL": "Chile", "PE": "Perú", "EC": "Ecuador", "GT": "Guatemala", "ES": "España",
    "BR": "Brasil", "VE": "Venezuela", "BO": "Bolivia", "PY": "Paraguay", "UY": "Uruguay",
    "CR": "Costa Rica", "PA": "Panamá", "DO": "República Dominicana", "HN": "Honduras",
    "SV": "El Salvador", "NI": "Nicaragua", "PR": "Puerto Rico", "CA": "Canadá",
    "GB": "Reino Unido", "FR": "Francia", "DE": "Alemania", "IT": "Italia",
}


def pais_nombre(codigo: str) -> str:
    if not codigo:
        return codigo
    return PAISES.get(str(codigo).upper(), str(codigo).upper())


def _a_dict(obj):
    """Convierte objetos del SDK a dict de forma tolerante."""
    if isinstance(obj, dict):
        return obj
    for m in ("export_all_data", "export_data"):
        if hasattr(obj, m):
            try:
                d = getattr(obj, m)()
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
    try:
        return dict(obj)
    except Exception:
        return {}


def _paises_de_targeting(tg) -> list:
    """Extrae códigos de país del targeting: countries, y country de regions/cities."""
    tg = _a_dict(tg)
    geo = _a_dict(tg.get("geo_locations"))
    codigos = []
    for c in (geo.get("countries") or []):
        if c:
            codigos.append(str(c).upper())
    for clave in ("regions", "cities", "zips", "places"):
        for item in (geo.get(clave) or []):
            item = _a_dict(item)
            c = item.get("country") or item.get("country_code")
            if c:
                codigos.append(str(c).upper())
    for cg in (geo.get("country_groups") or []):
        if cg:
            codigos.append(str(cg).upper())
    # únicos, en orden
    vistos, out = set(), []
    for c in codigos:
        if c not in vistos:
            vistos.add(c)
            out.append(c)
    return out


def _leer_adset_info(adset_id: str, api=None) -> dict:
    """
    Lee del conjunto: presupuesto diario (centavos→unidades) y el país donde
    está PAUTADO (targeting.geo_locations), con nombre completo.
    Devuelve {"presupuesto": float|None, "pais": str|None}.
    """
    try:
        adset = AdSet(adset_id, api=api).api_get(
            fields=[AdSet.Field.daily_budget, "targeting"])
        raw = adset.get(AdSet.Field.daily_budget)
        presupuesto = (float(raw) / 100.0) if raw is not None else None
        codigos = _paises_de_targeting(adset.get("targeting"))
        pais = ", ".join(pais_nombre(c) for c in codigos[:3]) if codigos else None
        return {"presupuesto": presupuesto, "pais": pais}
    except Exception:
        return {"presupuesto": None, "pais": None}


def _leer_daily_budget_adset(adset_id: str, api=None) -> Optional[float]:
    """Solo el presupuesto (para el polling)."""
    return _leer_adset_info(adset_id, api).get("presupuesto")


def _leer_campaign_budget(campaign_id: str, api=None) -> Optional[float]:
    """Presupuesto diario de la campaña (CBO), centavos → unidades."""
    try:
        camp = Campaign(campaign_id, api=api).api_get(fields=[Campaign.Field.daily_budget])
        raw = camp.get(Campaign.Field.daily_budget)
        return (float(raw) / 100.0) if raw is not None else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Insights agregados de todas las conexiones/cuentas
# --------------------------------------------------------------------------- #
def obtener_insights(date_preset: str = "today", nivel: str = "ad",
                     time_range: Optional[dict] = None) -> dict:
    """
    Métricas agregadas de todas las conexiones/cuentas, al nivel indicado:
      nivel='ad'       → clave = ad_id
      nivel='adset'    → clave = adset_id
      nivel='campaign' → clave = campaign_id
    Cada valor: {spend, cpm, ctr, impressions, clicks, conversaciones, costo_conversacion}.
    Nunca lanza.
    """
    if not SDK_DISPONIBLE:
        return {}
    nivel = nivel if nivel in ("ad", "adset", "campaign") else "ad"
    id_field = {"ad": "ad_id", "adset": "adset_id", "campaign": "campaign_id"}[nivel]
    cons = _conexiones_efectivas()
    resultado = {}
    for con in cons:
        try:
            api = _api_desde(con["token"], con["app_id"], con["app_secret"])
            cuentas = _descubrir_cuentas(api, con.get("env_account"))
        except Exception as e:
            _log(f"Insights: fallo en {con['alias']}: {e}")
            continue
        for idx, cta in enumerate(cuentas):
            if idx:
                _dormir(STAGGER_SEG)  # espaciar cuentas para no golpear el límite
            try:
                params = {"level": nivel, "limit": 500}
                if time_range and time_range.get("since") and time_range.get("until"):
                    params["time_range"] = {"since": time_range["since"],
                                            "until": time_range["until"]}
                else:
                    params["date_preset"] = date_preset
                _contar_llamado()
                filas = AdAccount(cta["act_id"], api=api).get_insights(
                    params=params,
                    fields=[id_field, "spend", "cpm", "ctr", "impressions",
                            "clicks", "actions", "action_values"])
            except Exception as e:
                if _es_rate_limit(e):
                    _marcar_throttle(e, cta["name"])
                    break  # salto el resto de cuentas de esta conexión este ciclo
                _log(f"Insights {cta['name']}: {e}")
                continue
            for row in filas:
                ad_id = row.get(id_field)
                if not ad_id:
                    continue
                spend = _to_float(row.get("spend"))
                conv = 0.0
                compras = 0.0
                for a in (row.get("actions") or []):
                    at = a.get("action_type")
                    if at in MSG_ACTION_TYPES:
                        conv += _to_float(a.get("value"))
                    if at in PURCHASE_ACTION_TYPES:
                        compras += _to_float(a.get("value"))
                compras_valor = 0.0
                for a in (row.get("action_values") or []):
                    if a.get("action_type") in PURCHASE_ACTION_TYPES:
                        compras_valor += _to_float(a.get("value"))
                resultado[str(ad_id)] = {
                    "spend": spend, "cpm": _to_float(row.get("cpm")),
                    "ctr": _to_float(row.get("ctr")),
                    "impressions": _to_float(row.get("impressions")),
                    "clicks": _to_float(row.get("clicks")),
                    "conversaciones": conv,
                    "costo_conversacion": (spend / conv) if conv > 0 else None,
                    "compras": compras, "compras_valor": compras_valor,
                }
    return resultado


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def serie_diaria(obj_id: str, nivel: str, conexion_id: Optional[int] = None,
                 date_preset: str = "last_7d") -> list:
    """
    Gasto por día (últimos 7 días) de un anuncio/conjunto/campaña.
    Devuelve [{"date": "YYYY-MM-DD", "spend": nativo}]. Nunca lanza.
    """
    if not SDK_DISPONIBLE or not obj_id:
        return []
    api = _api_para_conexion(conexion_id)
    if api is None:
        return []
    try:
        if nivel == "campaign":
            obj = Campaign(obj_id, api=api)
        elif nivel == "adset":
            obj = AdSet(obj_id, api=api)
        else:
            obj = Ad(obj_id, api=api)
        rows = obj.get_insights(
            params={"date_preset": date_preset, "time_increment": 1},
            fields=["spend"])
        return [{"date": r.get("date_start"), "spend": _to_float(r.get("spend"))}
                for r in rows]
    except Exception:
        return []


def gasto_diario_todos(date_preset: str = "last_7d") -> dict:
    """Gasto por DÍA y por anuncio de todas las conexiones/cuentas (para el mini
    gráfico por fila). Una sola llamada de insights por cuenta (level=ad,
    time_increment=1). Devuelve {ad_id: {"YYYY-MM-DD": spend_nativo}}. Nunca lanza.

    El gasto viene en la moneda de la cuenta; el llamador lo pasa a USD."""
    if not SDK_DISPONIBLE:
        return {}
    cons = _conexiones_efectivas()
    resultado: dict = {}
    for con in cons:
        try:
            api = _api_desde(con["token"], con["app_id"], con["app_secret"])
            cuentas = _descubrir_cuentas(api, con.get("env_account"))
        except Exception as e:
            _log(f"Gasto diario: fallo en {con['alias']}: {e}")
            continue
        for idx, cta in enumerate(cuentas):
            if idx:
                _dormir(STAGGER_SEG)  # espaciar cuentas para no golpear el límite
            try:
                _contar_llamado()
                filas = AdAccount(cta["act_id"], api=api).get_insights(
                    params={"level": "ad", "limit": 500,
                            "date_preset": date_preset, "time_increment": 1},
                    fields=["ad_id", "spend"])
            except Exception as e:
                if _es_rate_limit(e):
                    _marcar_throttle(e, cta["name"])
                    break  # salto el resto de cuentas de esta conexión este ciclo
                _log(f"Gasto diario {cta['name']}: {e}")
                continue
            for row in filas:
                ad_id = row.get("ad_id")
                dia = row.get("date_start")
                if not ad_id or not dia:
                    continue
                resultado.setdefault(str(ad_id), {})[dia] = _to_float(row.get("spend"))
    return resultado


def gasto_lifetime_todos(date_preset: str = "maximum") -> dict:
    """Gasto ACUMULADO (lifetime) por anuncio de todas las conexiones/cuentas.
    {ad_id: spend_nativo}. Una llamada de insights por cuenta (sin time_increment ->
    total del rango 'maximum'). Se usa para el snapshot al cambiar presupuesto y para
    calcular el gasto real desde el cambio. Nunca lanza."""
    if not SDK_DISPONIBLE:
        return {}
    cons = _conexiones_efectivas()
    resultado: dict = {}
    for con in cons:
        try:
            api = _api_desde(con["token"], con["app_id"], con["app_secret"])
            cuentas = _descubrir_cuentas(api, con.get("env_account"))
        except Exception as e:
            _log(f"Gasto lifetime: fallo en {con['alias']}: {e}")
            continue
        for idx, cta in enumerate(cuentas):
            if idx:
                _dormir(STAGGER_SEG)
            try:
                _contar_llamado()
                filas = AdAccount(cta["act_id"], api=api).get_insights(
                    params={"level": "ad", "limit": 500, "date_preset": date_preset},
                    fields=["ad_id", "spend"])
            except Exception as e:
                if _es_rate_limit(e):
                    _marcar_throttle(e, cta["name"])
                    break
                _log(f"Gasto lifetime {cta['name']}: {e}")
                continue
            for row in filas:
                ad_id = row.get("ad_id")
                if not ad_id:
                    continue
                resultado[str(ad_id)] = _to_float(row.get("spend"))
    return resultado


def gasto_por_pais(date_preset: str = "today", time_range: Optional[dict] = None) -> list:
    """
    Gasto REAL por país usando el desglose (breakdown) de Facebook, a nivel de
    cuenta. Devuelve [{"country": code, "spend": nativo, "moneda": cur}, ...]
    agregando todas las conexiones/cuentas. Nunca lanza.
    """
    if not SDK_DISPONIBLE:
        return []
    cons = _conexiones_efectivas()
    filas = []
    for con in cons:
        try:
            api = _api_desde(con["token"], con["app_id"], con["app_secret"])
            cuentas = _descubrir_cuentas(api, con.get("env_account"))
        except Exception:
            continue
        for idx, cta in enumerate(cuentas):
            if idx:
                _dormir(STAGGER_SEG)
            moneda = cta.get("moneda") or "USD"
            try:
                params = {"level": "account", "breakdowns": ["country"], "limit": 500}
                if time_range and time_range.get("since") and time_range.get("until"):
                    params["time_range"] = {"since": time_range["since"], "until": time_range["until"]}
                else:
                    params["date_preset"] = date_preset
                _contar_llamado()
                rows = AdAccount(cta["act_id"], api=api).get_insights(
                    params=params, fields=["spend"])
            except Exception as e:
                if _es_rate_limit(e):
                    _marcar_throttle(e, cta["name"])
                    break
                _log(f"Gasto por país {cta['name']}: {e}")
                continue
            for r in rows:
                pais = r.get("country")
                if not pais:
                    continue
                filas.append({"country": str(pais).upper(),
                              "spend": _to_float(r.get("spend")), "moneda": moneda})
    return filas


# --------------------------------------------------------------------------- #
#  Modificación de presupuesto (usa el token de la conexión del anuncio)
# --------------------------------------------------------------------------- #
def actualizar_presupuesto(obj_id: str, nivel: str, nuevo_monto: float,
                           conexion_id: Optional[int] = None) -> dict:
    """
    Cambia el daily_budget en Facebook del objeto correcto:
      nivel='campaign' → la campaña (CBO); si no → el conjunto (adset).
    Monto en centavos (monto*100). NO toca SQLite.
    """
    if not SDK_DISPONIBLE:
        return {"ok": False, "error": f"SDK no disponible: {SDK_ERROR_IMPORT}"}
    if not obj_id:
        return {"ok": False, "error": "Este anuncio no tiene dónde aplicar el presupuesto. "
                                       "Recarga los anuncios."}
    api = _api_para_conexion(conexion_id)
    if api is None:
        return {"ok": False, "error": "No hay token para la conexión de este anuncio."}
    try:
        centavos = int(round(float(nuevo_monto) * 100))
        if nivel == "campaign":
            Campaign(obj_id, api=api).api_update(params={Campaign.Field.daily_budget: centavos})
            _log(f"Presupuesto de campaña {obj_id} → {nuevo_monto:.2f}.")
        else:
            AdSet(obj_id, api=api).api_update(params={AdSet.Field.daily_budget: centavos})
            _log(f"Presupuesto de conjunto {obj_id} → {nuevo_monto:.2f}.")
        return {"ok": True, "error": None}
    except FacebookRequestError as e:
        return {"ok": False, "error": _fmt_fb_error(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def actualizar_presupuesto_facebook(adset_id: str, nuevo_monto: float,
                                    conexion_id: Optional[int] = None) -> dict:
    """Compat: actualiza el presupuesto del conjunto."""
    return actualizar_presupuesto(adset_id, "adset", nuevo_monto, conexion_id)


# --- Cola de cambios de presupuesto PENDIENTES (reintento ante rate limit) --- #
_PEND_KEY = "pendientes_presupuesto"
_PEND_LOCK = threading.Lock()


def _pendientes_leer() -> list:
    import json
    try:
        return json.loads(db.get_config(_PEND_KEY, "") or "[]")
    except Exception:
        return []


def _pendientes_guardar(lst: list) -> None:
    import json
    db.set_config(_PEND_KEY, json.dumps(lst, ensure_ascii=False))


def pendientes_presupuesto() -> list:
    with _PEND_LOCK:
        return _pendientes_leer()


def _encolar_pendiente(obj_id, nivel, monto, conexion_id, etiqueta) -> None:
    """Guarda un cambio de presupuesto que Facebook no aceptó (por límite) para
    reintentarlo solo. Si ya había uno para el mismo objeto, se reemplaza (gana el
    último valor)."""
    with _PEND_LOCK:
        lst = [p for p in _pendientes_leer() if p.get("obj_id") != obj_id]
        lst.append({"obj_id": obj_id, "nivel": nivel, "monto": float(monto),
                    "conexion_id": conexion_id, "etiqueta": etiqueta,
                    "ts": db.a_texto(db.ahora())})
        _pendientes_guardar(lst)
    _set_estado(pendientes_presupuesto=len(lst))


def procesar_pendientes_presupuesto() -> int:
    """Reintenta los cambios de presupuesto pendientes. Devuelve cuántos se aplicaron.
    Se llama periódicamente desde el hilo de ventas (cada pocos minutos)."""
    with _PEND_LOCK:
        lst = _pendientes_leer()
    if not lst:
        return 0
    quedan, aplicados = [], 0
    for p in lst:
        r = actualizar_presupuesto(p["obj_id"], p.get("nivel", "adset"),
                                   p["monto"], p.get("conexion_id"))
        if r.get("ok"):
            aplicados += 1
            _log(f"✔ Presupuesto pendiente aplicado: {p.get('etiqueta') or p['obj_id']} "
                 f"→ {p['monto']:.2f}.")
        elif _es_error_rate_limit_txt(r.get("error")):
            quedan.append(p)   # sigue frenado: reintentar luego
        else:
            _log(f"⚠ Cambio de presupuesto descartado (Facebook lo rechazó): "
                 f"{p.get('etiqueta') or p['obj_id']} — {r.get('error')}")
    with _PEND_LOCK:
        _pendientes_guardar(quedan)
    _set_estado(pendientes_presupuesto=len(quedan))
    return aplicados


def _es_error_rate_limit_txt(txt: Optional[str]) -> bool:
    if not txt:
        return False
    t = str(txt).lower()
    return ("límite" in t or "limit" in t or "too many" in t or "#80004" in t
            or "rate" in t or "frecuencia" in t)


def actualizar_presupuesto_async(obj_id: str, nivel: str, nuevo_monto: float,
                                 conexion_id: Optional[int] = None,
                                 etiqueta: str = "") -> None:
    """Empuja el cambio de presupuesto a Facebook en SEGUNDO PLANO (no bloquea la
    UI). Si Facebook está frenado (rate limit), NO se pierde: se encola y se
    reintenta solo cada pocos minutos hasta aplicarse."""
    def _worker():
        r = actualizar_presupuesto(obj_id, nivel, nuevo_monto, conexion_id)
        if r.get("ok"):
            _log(f"✔ Presupuesto aplicado en Facebook: {etiqueta or obj_id} → {nuevo_monto:.2f}.")
        elif _es_error_rate_limit_txt(r.get("error")):
            _encolar_pendiente(obj_id, nivel, nuevo_monto, conexion_id, etiqueta)
            _log(f"⏳ Facebook está frenado; el presupuesto de {etiqueta or obj_id} "
                 f"→ {nuevo_monto:.2f} quedó EN COLA y se aplicará solo en unos minutos.")
        else:
            _log(f"⚠ Facebook rechazó el presupuesto de {etiqueta or obj_id}: {r.get('error')}")
    threading.Thread(target=_worker, name="pres-async", daemon=True).start()


def cambiar_estado(obj_id: str, nivel: str, activar: bool,
                   conexion_id: Optional[int] = None) -> dict:
    """
    Prende (ACTIVE) o apaga (PAUSED) un anuncio / conjunto / campaña en Facebook.
    Devuelve {"ok": bool, "error": str|None}.
    """
    if not SDK_DISPONIBLE:
        return {"ok": False, "error": f"SDK no disponible: {SDK_ERROR_IMPORT}"}
    if not obj_id:
        return {"ok": False, "error": "Falta el identificador del objeto."}
    api = _api_para_conexion(conexion_id)
    if api is None:
        return {"ok": False, "error": "No hay token para la conexión de este anuncio."}
    estado = "ACTIVE" if activar else "PAUSED"
    try:
        if nivel == "campaign":
            Campaign(obj_id, api=api).api_update(params={"status": estado})
        elif nivel == "adset":
            AdSet(obj_id, api=api).api_update(params={"status": estado})
        else:
            Ad(obj_id, api=api).api_update(params={"status": estado})
        _log(f"{nivel} {obj_id} → {estado}.")
        return {"ok": True, "error": None}
    except FacebookRequestError as e:
        return {"ok": False, "error": _fmt_fb_error(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------- #
#  Duplicación de anuncios
# --------------------------------------------------------------------------- #
def _post_id_del_anuncio(ad_id: str, api) -> Optional[str]:
    """Devuelve el ID de la PUBLICACIÓN real del anuncio (effective_object_story_id),
    que es lo que comparte likes/comentarios/compartidos. None si no se puede leer."""
    try:
        ad = Ad(ad_id, api=api).api_get(fields=["creative"])
        creative_id = _a_dict(ad.get("creative")).get("id")
        if not creative_id:
            return None
        cr = AdCreative(creative_id, api=api).api_get(
            fields=["effective_object_story_id", "object_story_id"])
        return cr.get("effective_object_story_id") or cr.get("object_story_id")
    except Exception as e:
        _log(f"No pude leer la publicación del anuncio {ad_id}: {e}")
        return None


def _forzar_post_existente(nuevo_ad_id: str, post_id: str, cuenta_id: Optional[str],
                           nombre: str, api) -> bool:
    """Hace que la copia use la MISMA publicación (post_id) para conservar las
    interacciones: crea un creativo con object_story_id = post_id y apunta el ad a él."""
    if not (nuevo_ad_id and post_id and cuenta_id):
        return False
    act = str(cuenta_id) if str(cuenta_id).startswith("act_") else f"act_{cuenta_id}"
    creativo = AdAccount(act, api=api).create_ad_creative(params={
        "name": f"Post existente {post_id} · {nombre}"[:100],
        "object_story_id": post_id})
    nuevo_creative_id = creativo.get("id")
    if not nuevo_creative_id:
        return False
    Ad(nuevo_ad_id, api=api).api_update(params={"creative": {"creative_id": nuevo_creative_id}})
    return True


# --- Duplicación MANUAL (sin /copies): reconstruye los objetos reutilizando el
#     creative_id para conservar la prueba social. Funciona en Development Tier. ---
_RATE_CODES = {17, 613, 80000, 80004}   # rate limiting -> reintento con backoff
_ADSET_FIELDS = ["name", "campaign_id", "daily_budget", "lifetime_budget", "billing_event",
                 "optimization_goal", "bid_amount", "bid_strategy", "targeting",
                 "promoted_object", "start_time", "end_time", "attribution_spec",
                 "destination_type"]


def _act_id(cuenta_id) -> str:
    s = str(cuenta_id or "")
    return s if s.startswith("act_") else f"act_{s}"


def _con_backoff(fn, intentos: int = 3, espera_ini: float = 2.0):
    """Reintenta fn() con backoff exponencial SOLO en códigos de rate limit."""
    espera = espera_ini
    ultimo = None
    for i in range(intentos):
        try:
            return fn()
        except FacebookRequestError as e:
            ultimo = e
            try:
                code = e.api_error_code()
            except Exception:
                code = None
            if code in _RATE_CODES and i < intentos - 1:
                _dormir(espera)
                espera *= 2
                continue
            raise
    if ultimo:
        raise ultimo


def _leer_ad_src(api, ad_id: str) -> dict:
    """PASO 1 — lee el anuncio origen (nombre, adset, status, creative id + post)."""
    ad = _a_dict(Ad(ad_id, api=api).api_get(
        fields=["name", "adset_id", "status", "creative{id,effective_object_story_id}"]))
    cr = _a_dict(ad.get("creative"))
    return {"name": ad.get("name"), "adset_id": ad.get("adset_id"),
            "status": ad.get("status"), "creative_id": cr.get("id"),
            "post_id": cr.get("effective_object_story_id")}


def _crear_ad_manual(api, act: str, name: str, adset_id: str, creative_id: str,
                     status: str = "PAUSED", dry_run: bool = False):
    """PASO 2 — crea el anuncio duplicado reutilizando el MISMO creative_id
    (conserva la publicación y su prueba social). NO crea un post nuevo."""
    params = {"name": str(name)[:390], "adset_id": adset_id,
              "creative": {"creative_id": creative_id}, "status": status}
    if dry_run:
        return {"dry_run": True, "endpoint": f"{act}/ads", "params": params}
    return _con_backoff(lambda: AdAccount(act, api=api).create_ad(params=params))


def _campaign_es_cbo(api, campaign_id) -> bool:
    try:
        c = _a_dict(Campaign(campaign_id, api=api).api_get(
            fields=["daily_budget", "lifetime_budget"]))
        return bool(c.get("daily_budget") or c.get("lifetime_budget"))
    except Exception:
        return False


def _crear_adset_manual(api, act: str, src: dict, presupuesto_nat=None,
                        status: str = "PAUSED", sufijo: str = "— copia",
                        dry_run: bool = False):
    """PASO 3 — reconstruye un conjunto con los mismos ajustes del origen.
    Reglas: nunca daily+lifetime a la vez; si la campaña es CBO no se manda
    presupuesto; start_time en el pasado se omite; targeting/promoted_object
    completos. Si Meta rechaza el optimization_goal (2490408 — objetivo de
    rendimiento incompatible), reintenta dejando que Meta elija el compatible."""
    es_cbo = _campaign_es_cbo(api, src.get("campaign_id"))

    def _params(omit=()):
        p = {"name": f"{src.get('name') or 'Conjunto'} {sufijo}"[:390],
             "campaign_id": src.get("campaign_id"), "status": status}
        for k in ("billing_event", "optimization_goal", "bid_amount", "bid_strategy",
                  "targeting", "promoted_object", "attribution_spec", "destination_type"):
            if k in omit:
                continue
            v = src.get(k)
            if v not in (None, "", [], {}):
                p[k] = v
        if src.get("end_time"):
            p["end_time"] = src.get("end_time")   # start_time se omite (empieza ya)
        if not es_cbo:   # presupuesto solo si la campaña NO es CBO/Advantage
            if presupuesto_nat and float(presupuesto_nat) > 0:
                p["daily_budget"] = int(round(float(presupuesto_nat) * 100))
            elif src.get("daily_budget"):
                p["daily_budget"] = src.get("daily_budget")
            elif src.get("lifetime_budget"):
                p["lifetime_budget"] = src.get("lifetime_budget")
        return p

    if dry_run:
        return {"dry_run": True, "endpoint": f"{act}/adsets", "params": _params()}
    try:
        return _con_backoff(lambda: AdAccount(act, api=api).create_ad_set(params=_params()))
    except FacebookRequestError as e:
        try:
            sub = e.api_error_subcode()
        except Exception:
            sub = None
        msg = (getattr(e, "api_error_message", lambda: "")() or "").lower()
        if sub == 2490408 or "performance goal" in msg or "objetivo de rendimiento" in msg:
            _log("Adset: optimization_goal incompatible; reintento sin ese campo.")
            return _con_backoff(lambda: AdAccount(act, api=api).create_ad_set(
                params=_params(omit=("optimization_goal", "bid_amount"))))
        raise


def _ads_de_adset(api, adset_id: str) -> list:
    """Lista de {name, creative_id} de los anuncios de un conjunto."""
    out = []
    try:
        for a in AdSet(adset_id, api=api).get_ads(fields=["name", "creative{id}"]):
            ad = _a_dict(a)
            cid = _a_dict(ad.get("creative")).get("id")
            if cid:
                out.append({"name": ad.get("name") or "Anuncio", "creative_id": cid})
    except Exception as e:
        _log(f"No pude listar anuncios del conjunto {adset_id}: {e}")
    return out


def duplicar_anuncio(ad_id: str, num_copias: int, presupuesto: float,
                     activar: bool = False, conexion_id: Optional[int] = None,
                     cuenta_id: Optional[str] = None,
                     cuenta_nombre: Optional[str] = None,
                     reutilizar_post: bool = True, dry_run: bool = False) -> dict:
    """Duplica un ANUNCIO N veces de forma MANUAL (sin /copies): crea cada copia
    reutilizando el MISMO creative_id, así conserva la publicación y su prueba
    social (likes, comentarios, compartidos). Las copias van al MISMO conjunto y
    quedan PAUSADAS (salvo activar=True). Misma cuenta publicitaria."""
    resultado = {"exitosas": [], "fallidas": [], "error_global": None,
                 "post_id": None, "post_reutilizado": 0}
    if not SDK_DISPONIBLE:
        resultado["error_global"] = f"SDK no disponible: {SDK_ERROR_IMPORT}"
        return resultado
    api = _api_para_conexion(conexion_id)
    if api is None:
        resultado["error_global"] = "No hay token para la conexión de este anuncio."
        return resultado
    if not cuenta_id:
        resultado["error_global"] = "Falta la cuenta publicitaria del anuncio."
        return resultado

    try:
        src = _leer_ad_src(api, ad_id)
    except FacebookRequestError as e:
        resultado["error_global"] = _fmt_fb_error(e)
        return resultado
    creative_id = src.get("creative_id")
    adset_id = src.get("adset_id")
    resultado["post_id"] = src.get("post_id")
    if not creative_id or not adset_id:
        resultado["error_global"] = "No pude leer el creativo/conjunto del anuncio origen."
        return resultado

    act = _act_id(cuenta_id)
    num_copias = max(1, min(int(num_copias), 10))
    original = db.obtener_anuncio(ad_id)
    nombre_base = src.get("name") or (original or {}).get("nombre") or f"Anuncio {ad_id}"
    status = "ACTIVE" if activar else "PAUSED"   # por defecto PAUSED

    for i in range(1, num_copias + 1):
        nombre_copia = f"{nombre_base} - Copia {i}"
        try:
            nad = _crear_ad_manual(api, act, nombre_copia, adset_id, creative_id, status, dry_run)
            if dry_run:
                resultado["exitosas"].append({"nombre": nombre_copia, "dry_run": nad})
                continue
            nuevo_ad_id = _a_dict(nad).get("id")
            if not nuevo_ad_id:
                raise RuntimeError("Facebook no devolvió el ad_id de la copia.")
            resultado["post_reutilizado"] += 1   # mismo creative_id -> mismo post
            db.upsert_anuncio(nuevo_ad_id, nombre_copia, adset_id=adset_id,
                              activo=1 if activar else 0, effective_status=status,
                              conexion_id=conexion_id, cuenta_id=cuenta_id,
                              cuenta_nombre=cuenta_nombre)
            db.abrir_periodo(nuevo_ad_id, float(presupuesto))
            resultado["exitosas"].append({
                "ad_id": nuevo_ad_id, "nombre": nombre_copia, "adset_id": adset_id,
                "creative_id": creative_id, "post_id": src.get("post_id")})
            _log(f"Copia manual creada: {nombre_copia} ({nuevo_ad_id}), creative {creative_id}.")
        except FacebookRequestError as e:
            resultado["fallidas"].append({"nombre": nombre_copia, "error": _fmt_fb_error(e)})
        except Exception as e:
            resultado["fallidas"].append({"nombre": nombre_copia, "error": str(e)})
    return resultado


def _resolver_adset_de_copia(copia: dict, nuevo_ad_id: Optional[str], api) -> Optional[str]:
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
    if nuevo_ad_id:
        try:
            ad = Ad(nuevo_ad_id, api=api).api_get(fields=[Ad.Field.adset_id])
            return ad.get(Ad.Field.adset_id)
        except Exception:
            return None
    return None


def duplicar_objeto(nivel: str, obj_id: str, num_copias: int, presupuesto: float,
                    activar: bool = False, conexion_id: Optional[int] = None,
                    cuenta_id: Optional[str] = None, cuenta_nombre: Optional[str] = None,
                    reutilizar_post: bool = True, dry_run: bool = False) -> dict:
    """Duplica de forma MANUAL (sin /copies) según el NIVEL de la vista:
      - 'ad'       → duplica el ANUNCIO reutilizando el creative (prueba social).
      - 'adset'    → reconstruye el CONJUNTO + recrea sus anuncios (mismos creativos).
      - 'campaign' → reconstruye la CAMPAÑA + sus conjuntos + sus anuncios.
    Todo en la MISMA cuenta publicitaria y PAUSADO (salvo activar=True).
    """
    if nivel == "ad":
        return duplicar_anuncio(obj_id, num_copias, presupuesto, activar, conexion_id,
                                cuenta_id, cuenta_nombre, reutilizar_post, dry_run)

    resultado = {"exitosas": [], "fallidas": [], "error_global": None, "nivel": nivel}
    if not SDK_DISPONIBLE:
        resultado["error_global"] = f"SDK no disponible: {SDK_ERROR_IMPORT}"
        return resultado
    api = _api_para_conexion(conexion_id)
    if api is None:
        resultado["error_global"] = "No hay token para la conexión."
        return resultado
    if not obj_id or not cuenta_id:
        resultado["error_global"] = ("Falta el objeto o la cuenta publicitaria. "
                                     "Pulsa Recargar y reintenta.")
        return resultado

    act = _act_id(cuenta_id)
    num_copias = max(1, min(int(num_copias), 10))
    status = "ACTIVE" if activar else "PAUSED"
    etiqueta = "conjunto" if nivel == "adset" else "campaña"

    for i in range(1, num_copias + 1):
        try:
            if nivel == "adset":
                src = _a_dict(AdSet(obj_id, api=api).api_get(fields=_ADSET_FIELDS))
                ads_src = _ads_de_adset(api, obj_id)
                nuevo_adset = _crear_adset_manual(api, act, src, presupuesto, status,
                                                  sufijo=f"— copia {i}", dry_run=dry_run)
                if dry_run:
                    resultado["exitosas"].append({"nivel": "adset", "dry_run": nuevo_adset,
                                                  "anuncios_a_recrear": len(ads_src)})
                    continue
                nuevo_adset_id = _a_dict(nuevo_adset).get("id")
                if not nuevo_adset_id:
                    raise RuntimeError("No se pudo crear el conjunto nuevo.")
                creados, fallos_ad = 0, []
                for a in ads_src:
                    try:
                        nad = _crear_ad_manual(api, act, f"{a['name']} — copia {i}",
                                               nuevo_adset_id, a["creative_id"], status)
                        nid = _a_dict(nad).get("id")
                        creados += 1
                        db.upsert_anuncio(nid, f"{a['name']} — copia {i}",
                                          adset_id=nuevo_adset_id, activo=1 if activar else 0,
                                          effective_status=status, conexion_id=conexion_id,
                                          cuenta_id=cuenta_id, cuenta_nombre=cuenta_nombre)
                        db.abrir_periodo(nid, float(presupuesto))
                    except FacebookRequestError as e:
                        fallos_ad.append(_fmt_fb_error(e))
                resultado["exitosas"].append({"id": nuevo_adset_id, "nivel": "adset",
                                              "anuncios_creados": creados,
                                              "anuncios_fallidos": fallos_ad})
                _log(f"Conjunto duplicado (manual): {nuevo_adset_id} · {creados} anuncios.")
            else:  # campaign
                c = _a_dict(Campaign(obj_id, api=api).api_get(fields=[
                    "name", "objective", "status", "special_ad_categories", "buying_type",
                    "daily_budget", "lifetime_budget", "bid_strategy"]))
                cp = {"name": f"{c.get('name') or 'Campaña'} — copia {i}"[:390],
                      "objective": c.get("objective"), "status": status,
                      "special_ad_categories": c.get("special_ad_categories") or []}
                if c.get("buying_type"):
                    cp["buying_type"] = c["buying_type"]
                es_cbo = bool(c.get("daily_budget") or c.get("lifetime_budget"))
                if es_cbo:
                    if c.get("daily_budget"):
                        cp["daily_budget"] = c["daily_budget"]
                    elif c.get("lifetime_budget"):
                        cp["lifetime_budget"] = c["lifetime_budget"]
                    if c.get("bid_strategy"):
                        cp["bid_strategy"] = c["bid_strategy"]
                if dry_run:
                    resultado["exitosas"].append({"nivel": "campaign", "dry_run": cp})
                    continue
                nueva_camp = _con_backoff(lambda: AdAccount(act, api=api).create_campaign(params=cp))
                nueva_camp_id = _a_dict(nueva_camp).get("id")
                if not nueva_camp_id:
                    raise RuntimeError("No se pudo crear la campaña nueva.")
                conjuntos = 0
                for asrc_obj in Campaign(obj_id, api=api).get_ad_sets(fields=_ADSET_FIELDS + ["id"]):
                    asrc = _a_dict(asrc_obj)
                    src_adset_id = str(asrc.get("id") or "")
                    asrc["campaign_id"] = nueva_camp_id   # colgar de la nueva campaña
                    nuevo_as = _crear_adset_manual(api, act, asrc,
                                                   None if es_cbo else presupuesto, status)
                    nuevo_as_id = _a_dict(nuevo_as).get("id")
                    if not nuevo_as_id:
                        continue
                    conjuntos += 1
                    for a in _ads_de_adset(api, src_adset_id):
                        try:
                            nad = _crear_ad_manual(api, act, f"{a['name']} — copia",
                                                   nuevo_as_id, a["creative_id"], status)
                            nid = _a_dict(nad).get("id")
                            db.upsert_anuncio(nid, f"{a['name']} — copia", adset_id=nuevo_as_id,
                                              activo=1 if activar else 0, effective_status=status,
                                              conexion_id=conexion_id, cuenta_id=cuenta_id,
                                              cuenta_nombre=cuenta_nombre)
                            db.abrir_periodo(nid, float(presupuesto))
                        except FacebookRequestError as e:
                            _log(f"Anuncio (copia de campaña) falló: {_fmt_fb_error(e)}")
                resultado["exitosas"].append({"id": nueva_camp_id, "nivel": "campaign",
                                              "conjuntos_creados": conjuntos})
                _log(f"Campaña duplicada (manual): {nueva_camp_id} · {conjuntos} conjuntos.")
        except FacebookRequestError as e:
            resultado["fallidas"].append({"nombre": f"{etiqueta} {i}", "error": _fmt_fb_error(e)})
        except Exception as e:
            resultado["fallidas"].append({"nombre": f"{etiqueta} {i}", "error": str(e)})
    return resultado


# --------------------------------------------------------------------------- #
#  Polling en segundo plano
# --------------------------------------------------------------------------- #
def _revisar_cambios_presupuesto() -> None:
    """Detecta cambios de daily_budget y cierra/abre período."""
    anuncios = db.obtener_anuncios(solo_activos=True)
    apis = {}
    cache_budget = {}
    cambios = 0
    for anuncio in anuncios:
        adset_id = anuncio.get("adset_id")
        if not adset_id:
            continue
        cid = anuncio.get("conexion_id")
        if cid not in apis:
            apis[cid] = _api_para_conexion(cid)
        api = apis[cid]
        if api is None:
            continue
        clave = (cid, adset_id)
        if clave in cache_budget:
            actual = cache_budget[clave]
        else:
            actual = _leer_daily_budget_adset(adset_id, api)
            cache_budget[clave] = actual
        if actual is None:
            continue
        abierto = db.periodo_abierto(anuncio["ad_id"])
        if abierto is None:
            db.abrir_periodo(anuncio["ad_id"], actual)
            continue
        if abs(float(abierto["presupuesto"]) - float(actual)) > 0.01:
            db.cambiar_periodo(anuncio["ad_id"], actual)
            cambios += 1
            _log(f"Cambio en '{anuncio['nombre']}': "
                 f"{abierto['presupuesto']:.2f} → {actual:.2f}. Nuevo período.")
    if cambios:
        _log(f"Polling: {cambios} cambio(s) de presupuesto aplicados.")


# Cadencia de actualización automática (hora de Bogotá vía db.ahora()):
#   Día  (06:00–23:00): cada 30 minutos.
#   Noche(23:00–06:00): cada 60 minutos (menos presión sobre la API mientras duermes).
INTERVALO_DIA_SEG = int(os.getenv("FB_INTERVAL_DIA", str(30 * 60)))
INTERVALO_NOCHE_SEG = int(os.getenv("FB_INTERVAL_NOCHE", str(60 * 60)))
HORA_INICIO_NOCHE = 23   # 11 PM
HORA_FIN_NOCHE = 6       # 6 AM


def _intervalo_polling() -> int:
    """Segundos hasta la próxima actualización según la hora local."""
    h = db.ahora().hour
    de_noche = h >= HORA_INICIO_NOCHE or h < HORA_FIN_NOCHE
    return INTERVALO_NOCHE_SEG if de_noche else INTERVALO_DIA_SEG


def _loop_polling() -> None:
    _set_estado(polling_activo=True)
    try:
        cargar_todo()
    except Exception as e:
        _log(f"Fallo en carga inicial: {e}")
    # El intervalo se recalcula en cada vuelta para respetar el horario día/noche.
    # Nota: la sincronización de ventas (Sheets/Supabase) va en su PROPIO hilo
    # rápido (_loop_ventas), no aquí, para que las ventas se actualicen seguido
    # sin esperar a la carga pesada de Facebook.
    while not _POLLING_STOP.wait(_intervalo_polling()):
        try:
            # cargar_todo ya trae el presupuesto en el mismo llamado y detecta los
            # cambios ahí, así que ya NO llamamos _revisar_cambios_presupuesto
            # (que hacía un llamado extra por cada conjunto y disparaba el límite).
            cargar_todo(abrir_periodos=True)
            _set_estado(ultimo_polling=db.ahora())
        except Exception as e:
            _set_estado(ultimo_error=str(e))
            _log(f"Error en polling: {e}\n{traceback.format_exc(limit=1)}")


_VENTAS_SYNC_LOCK = threading.Lock()
_VENTAS_SYNC_RUNNING = False


def _sincronizar_fuentes_ventas() -> int:
    """Auto-sincroniza Google Sheets y Supabase (si están configurados).
    Devuelve cuántas ventas nuevas insertó y actualiza marcas de tiempo.
    Guard: solo una sincronización a la vez (evita solapar manual + periódica)."""
    global _VENTAS_SYNC_RUNNING
    with _VENTAS_SYNC_LOCK:
        if _VENTAS_SYNC_RUNNING:
            return 0
        _VENTAS_SYNC_RUNNING = True
    try:
        return _sincronizar_fuentes_ventas_impl()
    finally:
        with _VENTAS_SYNC_LOCK:
            _VENTAS_SYNC_RUNNING = False


def sync_ventas_en_curso() -> bool:
    with _VENTAS_SYNC_LOCK:
        return _VENTAS_SYNC_RUNNING


def sincronizar_ventas_async() -> bool:
    """Lanza la sincronización de ventas en segundo plano (NO bloquea la UI).
    Devuelve True si la inició, False si ya había una en curso."""
    if sync_ventas_en_curso():
        return False
    threading.Thread(target=_sincronizar_fuentes_ventas,
                     name="ventas-sync-manual", daemon=True).start()
    return True


def _sincronizar_fuentes_ventas_impl() -> int:
    total = 0
    try:
        import gsheets
        if gsheets.get_url():
            r = gsheets.sincronizar()
            n = r.get("insertadas") or 0
            total += n
            if n:
                _log(f"Google Sheets: {n} venta(s) nueva(s).")
    except Exception as e:
        _log(f"Auto-sync Google Sheets: {e}")
    try:
        import supabase_source
        if config.supabase_configurado():
            r = supabase_source.sincronizar()
            n = r.get("insertadas") or 0
            total += n
            if n:
                _log(f"Supabase: {n} venta(s) nueva(s).")
    except Exception as e:
        _log(f"Auto-sync Supabase: {e}")
    # Marca de "última revisión de ventas" (para el timer); y "hubo cambio" solo
    # si entraron ventas nuevas (para que la vista se refresque sola).
    try:
        db.set_config("ultima_sync_ventas", db.a_texto(db.ahora()))
        if total:
            db.set_config("ventas_cambio", db.a_texto(db.ahora()))
    except Exception:
        pass
    return total


# --- Hilo dedicado y rápido para las ventas (Sheets / Supabase) ------------ #
# Cada pocos minutos revisa el Sheet, independiente de la carga de Facebook.
VENTAS_INTERVAL_DIA = int(os.getenv("FB_VENTAS_INTERVAL", "120"))    # 2 min de día
VENTAS_INTERVAL_NOCHE = int(os.getenv("FB_VENTAS_INTERVAL_NOCHE", "600"))  # 10 min de noche
_VENTAS_THREAD = None
_VENTAS_STOP = threading.Event()


def _intervalo_ventas() -> int:
    h = db.ahora().hour
    de_noche = h >= HORA_INICIO_NOCHE or h < HORA_FIN_NOCHE
    return VENTAS_INTERVAL_NOCHE if de_noche else VENTAS_INTERVAL_DIA


def _loop_ventas() -> None:
    # Primera revisión enseguida al arrancar (para que las ventas aparezcan pronto).
    while True:
        try:
            _sincronizar_fuentes_ventas()
        except Exception as e:
            _log(f"Sync de ventas: {e}")
        try:
            # Reintenta los cambios de presupuesto que quedaron en cola por el límite
            # de Facebook. Así un cambio nunca se pierde: se aplica en cuanto se libera.
            procesar_pendientes_presupuesto()
        except Exception as e:
            _log(f"Reintento de presupuestos pendientes: {e}")
        if _VENTAS_STOP.wait(_intervalo_ventas()):
            break


def iniciar_polling() -> None:
    global _POLLING_THREAD, _VENTAS_THREAD
    if not (_POLLING_THREAD and _POLLING_THREAD.is_alive()):
        _POLLING_STOP.clear()
        _POLLING_THREAD = threading.Thread(target=_loop_polling, name="fb-polling", daemon=True)
        _POLLING_THREAD.start()
        _log("Hilo de polling de Facebook iniciado.")
    if not (_VENTAS_THREAD and _VENTAS_THREAD.is_alive()):
        _VENTAS_STOP.clear()
        _VENTAS_THREAD = threading.Thread(target=_loop_ventas, name="ventas-sync", daemon=True)
        _VENTAS_THREAD.start()
        _log("Hilo de sincronización de ventas iniciado.")


def detener_polling() -> None:
    _POLLING_STOP.set()
    _VENTAS_STOP.set()


def sincronizar_ventas_ahora() -> int:
    """Fuerza una sincronización de ventas (Sheets/Supabase) en primer plano.
    Devuelve cuántas ventas nuevas insertó."""
    return _sincronizar_fuentes_ventas()


def estado_hilos() -> dict:
    """Estado de los hilos de segundo plano (para el diagnóstico en la UI)."""
    return {
        "fb_vivo": bool(_POLLING_THREAD and _POLLING_THREAD.is_alive()),
        "ventas_vivo": bool(_VENTAS_THREAD and _VENTAS_THREAD.is_alive()),
    }


# --------------------------------------------------------------------------- #
#  Utilidades
# --------------------------------------------------------------------------- #
def _fmt_fb_error(e) -> str:
    try:
        msg = e.api_error_message() or ""
        code = e.api_error_code()
        sub = e.api_error_subcode()
        # Mensaje HUMANO de Facebook (error_user_title/msg): explica la causa real,
        # mucho más útil que el genérico "Invalid parameter".
        user_title = user_msg = ""
        try:
            body = e.body() if callable(getattr(e, "body", None)) else getattr(e, "_body", None)
            err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
            user_title = (err.get("error_user_title") or "").strip()
            user_msg = (err.get("error_user_msg") or "").strip()
        except Exception:
            pass
        humano = " — ".join([p for p in (user_title, user_msg) if p])
        base = humano or msg
        extra = f"(code {code}" + (f"/{sub}" if sub else "") + ")"
        return " ".join([p for p in [base, extra] if p]).strip() or str(e)
    except Exception:
        return str(e)
