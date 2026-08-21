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
    "mensajes": [],
}

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

        for cta in cuentas:
            try:
                ads = AdAccount(cta["act_id"], api=api).get_ads(
                    params={"effective_status": ["ACTIVE", "PAUSED",
                                                 "ADSET_PAUSED", "CAMPAIGN_PAUSED"],
                            "limit": 500},
                    fields=["id", "name", "adset_id", "campaign_id",
                        "effective_status", "created_time",
                        "campaign{name}", "adset{name}"],
                )
            except FacebookRequestError as e:
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
                    db.asegurar_periodo_inicial(ad_id, presupuesto)

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
        for cta in cuentas:
            try:
                params = {"level": nivel, "limit": 500}
                if time_range and time_range.get("since") and time_range.get("until"):
                    params["time_range"] = {"since": time_range["since"],
                                            "until": time_range["until"]}
                else:
                    params["date_preset"] = date_preset
                filas = AdAccount(cta["act_id"], api=api).get_insights(
                    params=params,
                    fields=[id_field, "spend", "cpm", "ctr", "impressions",
                            "clicks", "actions", "action_values"])
            except Exception as e:
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
        for cta in cuentas:
            moneda = cta.get("moneda") or "USD"
            try:
                params = {"level": "account", "breakdowns": ["country"], "limit": 500}
                if time_range and time_range.get("since") and time_range.get("until"):
                    params["time_range"] = {"since": time_range["since"], "until": time_range["until"]}
                else:
                    params["date_preset"] = date_preset
                rows = AdAccount(cta["act_id"], api=api).get_insights(
                    params=params, fields=["spend"])
            except Exception as e:
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
def duplicar_anuncio(ad_id: str, num_copias: int, presupuesto: float,
                     activar: bool = False, conexion_id: Optional[int] = None,
                     cuenta_id: Optional[str] = None,
                     cuenta_nombre: Optional[str] = None) -> dict:
    """Duplica un anuncio N veces con deep_copy usando el token de su conexión."""
    resultado = {"exitosas": [], "fallidas": [], "error_global": None}
    if not SDK_DISPONIBLE:
        resultado["error_global"] = f"SDK no disponible: {SDK_ERROR_IMPORT}"
        return resultado
    api = _api_para_conexion(conexion_id)
    if api is None:
        resultado["error_global"] = "No hay token para la conexión de este anuncio."
        return resultado

    num_copias = max(1, min(int(num_copias), 10))
    original = db.obtener_anuncio(ad_id)
    nombre_base = original["nombre"] if original else f"Anuncio {ad_id}"
    status_copia = "ACTIVE" if activar else "PAUSED"

    for i in range(1, num_copias + 1):
        nombre_copia = f"{nombre_base} - Copia {i}"
        try:
            copia = Ad(ad_id, api=api).create_copy(params={
                "deep_copy": True, "status_option": status_copia})
            nuevo_ad_id = copia.get("copied_ad_id") or copia.get("ad_id") or copia.get("id")
            nuevo_adset_id = _resolver_adset_de_copia(copia, nuevo_ad_id, api)
            if not nuevo_ad_id:
                raise RuntimeError("Facebook no devolvió el ad_id de la copia.")

            if nuevo_adset_id:
                r = actualizar_presupuesto_facebook(nuevo_adset_id, presupuesto, conexion_id)
                if not r["ok"]:
                    _log(f"Copia {nuevo_ad_id}: presupuesto no aplicado: {r['error']}")

            db.upsert_anuncio(nuevo_ad_id, nombre_copia, adset_id=nuevo_adset_id,
                              activo=1 if activar else 0, effective_status=status_copia,
                              conexion_id=conexion_id, cuenta_id=cuenta_id,
                              cuenta_nombre=cuenta_nombre)
            db.abrir_periodo(nuevo_ad_id, float(presupuesto))
            resultado["exitosas"].append({
                "ad_id": nuevo_ad_id, "nombre": nombre_copia,
                "adset_id": nuevo_adset_id, "presupuesto": presupuesto})
            _log(f"Copia creada: {nombre_copia} ({nuevo_ad_id}).")
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
#   Día  (06:00–23:00): cada 15 minutos.
#   Noche(23:00–06:00): cada 60 minutos (menos presión sobre la API mientras duermes).
INTERVALO_DIA_SEG = 15 * 60
INTERVALO_NOCHE_SEG = 60 * 60
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
    while not _POLLING_STOP.wait(_intervalo_polling()):
        try:
            cargar_todo(abrir_periodos=True)
            _revisar_cambios_presupuesto()
            _sincronizar_fuentes_ventas()
            _set_estado(ultimo_polling=db.ahora())
        except Exception as e:
            _set_estado(ultimo_error=str(e))
            _log(f"Error en polling: {e}\n{traceback.format_exc(limit=1)}")


def _sincronizar_fuentes_ventas() -> None:
    """Auto-sincroniza Google Sheets y Supabase (si están configurados)."""
    try:
        import gsheets
        if gsheets.get_url():
            r = gsheets.sincronizar()
            if r.get("insertadas"):
                _log(f"Google Sheets: {r['insertadas']} venta(s) nueva(s).")
    except Exception as e:
        _log(f"Auto-sync Google Sheets: {e}")
    try:
        import supabase_source
        if config.supabase_configurado():
            r = supabase_source.sincronizar()
            if r.get("insertadas"):
                _log(f"Supabase: {r['insertadas']} venta(s) nueva(s).")
    except Exception as e:
        _log(f"Auto-sync Supabase: {e}")
    _set_estado(polling_activo=False)


def iniciar_polling() -> None:
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
    try:
        msg = e.api_error_message() or ""
        code = e.api_error_code()
        sub = e.api_error_subcode()
        extra = f"(code {code}" + (f"/{sub}" if sub else "") + ")"
        return " ".join([p for p in [msg, extra] if p]).strip() or str(e)
    except Exception:
        return str(e)
