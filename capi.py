"""
capi.py
Conversions API (CAPI) de Meta: envía eventos de COMPRA (Purchase) al Pixel /
Conjunto de datos, servidor→servidor, para que Facebook registre las ventas de
Click-to-WhatsApp (que ocurren fuera de la web y el pixel no ve solo).

- Lee las compras de la tabla de Supabase ya configurada (compradores).
- Por cada compra construye un evento Purchase con:
    * value + currency
    * user_data: ctwa_clid (id del clic de WhatsApp, NO hasheado) + teléfono/email
      (hasheados SHA-256, como exige Meta) + fbc/fbp si están.
    * event_id = id único de la venta  → dedup (evita doble conteo con el pixel web).
- Deduplica lo ya enviado (guarda los ids en config_kv).

Config (config_kv):
  capi_pixel_id, capi_token, capi_test_code (opcional), y las columnas de match:
  capi_col_ctwa, capi_col_phone, capi_col_email, capi_col_fbc.
El Pixel ID y el token se ponen en la app (Configuración → Pixel / CAPI).
"""
import hashlib
import json
import time
from datetime import datetime, timedelta

import requests

import config
import db
import supabase_source as supa

GRAPH = "https://graph.facebook.com/v21.0"

K_PIXEL = "capi_pixel_id"
K_TOKEN = "capi_token"
K_TEST = "capi_test_code"
K_ENABLED = "capi_auto"           # "1" = enviar automáticamente al sincronizar ventas
K_COL_CTWA = "capi_col_ctwa"
K_COL_PHONE = "capi_col_phone"
K_COL_EMAIL = "capi_col_email"
K_COL_FBC = "capi_col_fbc"
K_ENVIADOS = "capi_enviados"      # JSON: ids de ventas ya enviadas (dedup)

VENTANA_DIAS = 7                  # Meta solo acepta eventos de los últimos 7 días


def configurado() -> bool:
    return bool(db.get_config(K_PIXEL) and db.get_config(K_TOKEN))


# --------------------------------------------------------------------------- #
#  Hashing (Meta exige SHA-256 de datos normalizados)
# --------------------------------------------------------------------------- #
def _sha(v) -> str:
    return hashlib.sha256(str(v).strip().lower().encode("utf-8")).hexdigest()


def _hash_email(v):
    v = (str(v) if v is not None else "").strip().lower()
    if not v or "@" not in v:
        return None
    return _sha(v)


def _hash_phone(v):
    dig = "".join(ch for ch in str(v or "") if ch.isdigit())
    return _sha(dig) if len(dig) >= 7 else None


# --------------------------------------------------------------------------- #
#  Dedup: ids ya enviados
# --------------------------------------------------------------------------- #
def _enviados() -> set:
    try:
        return set(json.loads(db.get_config(K_ENVIADOS, "") or "[]"))
    except Exception:
        return set()


def _guardar_enviados(s: set) -> None:
    # Conserva solo los últimos 10.000 ids (suficiente para dedup, no crece infinito).
    db.set_config(K_ENVIADOS, json.dumps(sorted(s)[-10000:], ensure_ascii=False))


def _moneda() -> str:
    mv = db.get_config("moneda_ventas", "auto")
    if mv and mv != "auto":
        return mv.upper()
    monedas = [a.get("cuenta_moneda") for a in db.obtener_anuncios() if a.get("cuenta_moneda")]
    return (max(set(monedas), key=monedas.count) if monedas else "USD").upper()


def _event_time(valor_hora) -> int:
    """Unix time del evento; si no se puede leer, usa ahora."""
    try:
        import pandas as pd
        ts = pd.to_datetime(valor_hora, errors="coerce")
        if not pd.isna(ts):
            return int(ts.timestamp())
    except Exception:
        pass
    return int(time.time())


def _leer_compradores() -> tuple:
    """Descarga las filas de compradores de Supabase. (filas, error)."""
    if not config.supabase_configurado():
        return [], "Supabase no configurado (faltan SUPABASE_URL/SUPABASE_KEY)."
    m = supa.get_mapeo()
    url = f"{config.SUPABASE_URL}/rest/v1/{m[supa.K_TABLA]}"
    try:
        r = requests.get(url, headers=supa._headers(),
                         params={"select": "*", "limit": 10000}, timeout=30)
        if r.status_code >= 400:
            return [], f"Supabase HTTP {r.status_code}: {r.text[:200]}"
        return r.json(), None
    except Exception as e:
        return [], str(e)


def _construir_evento(fila, m, cols, cur) -> tuple:
    """Devuelve (evento, id) o (None, id) si la fila no sirve."""
    rid = fila.get(m[supa.K_ID])
    rid = str(rid) if rid is not None else None
    valor = supa._parse_valor(fila.get(m[supa.K_VALOR]))
    if rid is None or valor is None or valor <= 0:
        return None, rid
    hora = fila.get(m[supa.K_HORA]) if m.get(supa.K_HORA) else None
    et = _event_time(hora)
    # Solo eventos dentro de la ventana permitida por Meta (7 días).
    if et < int((datetime.utcnow() - timedelta(days=VENTANA_DIAS)).timestamp()):
        return "VIEJO", rid

    ud = {}
    ctwa = cols.get("ctwa") and fila.get(cols["ctwa"])
    if ctwa:
        ud["ctwa_clid"] = str(ctwa)
    ph = _hash_phone(fila.get(cols["phone"])) if cols.get("phone") else None
    if ph:
        ud["ph"] = [ph]
    em = _hash_email(fila.get(cols["email"])) if cols.get("email") else None
    if em:
        ud["em"] = [em]
    fbc = cols.get("fbc") and fila.get(cols["fbc"])
    if fbc:
        ud["fbc"] = str(fbc)
    if not ud:   # sin ningún dato de coincidencia no vale la pena enviarlo
        return None, rid

    ev = {
        "event_name": "Purchase",
        "event_time": et,
        "event_id": rid,
        "action_source": "business_messaging" if ctwa else "website",
        "user_data": ud,
        "custom_data": {"currency": cur, "value": round(float(valor), 2)},
    }
    if ctwa:
        ev["messaging_channel"] = "whatsapp"
    return ev, rid


def enviar_compras(limit: int = 500, prueba: bool = False,
                   test_code: str = None) -> dict:
    """Envía las compras (no enviadas aún) a la Conversions API.
    prueba=True: usa test_event_code y NO marca como enviadas (para ver en
    Events Manager → Eventos de prueba sin afectar los reales)."""
    pixel = db.get_config(K_PIXEL)
    token = db.get_config(K_TOKEN)
    if not (pixel and token):
        return {"ok": False, "error": "Falta el Pixel ID o el token CAPI "
                                       "(Configuración → Pixel / CAPI)."}
    filas, err = _leer_compradores()
    if err:
        return {"ok": False, "error": err}

    m = supa.get_mapeo()
    cols = {"ctwa": db.get_config(K_COL_CTWA) or "",
            "phone": db.get_config(K_COL_PHONE) or "",
            "email": db.get_config(K_COL_EMAIL) or "",
            "fbc": db.get_config(K_COL_FBC) or ""}
    cur = _moneda()
    enviados = _enviados()

    eventos, ids, viejas, sin_match = [], [], 0, 0
    for fila in filas:
        ev, rid = _construir_evento(fila, m, cols, cur)
        if ev == "VIEJO":
            viejas += 1
            continue
        if ev is None:
            if rid is not None:
                sin_match += 1
            continue
        if not prueba and rid in enviados:
            continue
        eventos.append(ev)
        ids.append(rid)
        if len(eventos) >= limit:
            break

    if not eventos:
        return {"ok": True, "enviados": 0, "recibidos": 0, "viejas": viejas,
                "sin_match": sin_match,
                "nota": "No hay compras nuevas por enviar (o ninguna tiene datos de match)."}

    payload = {"data": eventos}
    tc = test_code or (db.get_config(K_TEST) if prueba else None)
    if tc:
        payload["test_event_code"] = tc
    try:
        resp = requests.post(f"{GRAPH}/{pixel}/events",
                             params={"access_token": token}, json=payload, timeout=30)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not resp.ok:
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:400]}"}
    body = resp.json()
    if not prueba:
        enviados.update(ids)
        _guardar_enviados(enviados)
    return {"ok": True, "enviados": len(eventos),
            "recibidos": body.get("events_received", 0),
            "fbtrace": body.get("fbtrace_id"), "viejas": viejas,
            "sin_match": sin_match, "respuesta": body}


def enviar_evento_prueba(test_code: str) -> dict:
    """Envía UN evento Purchase de prueba (datos ficticios) para verificar el pipe
    en Events Manager → Eventos de prueba. NO afecta los datos reales."""
    pixel = db.get_config(K_PIXEL)
    token = db.get_config(K_TOKEN)
    if not (pixel and token):
        return {"ok": False, "error": "Falta el Pixel ID o el token CAPI."}
    ev = {
        "event_name": "Purchase",
        "event_time": int(time.time()),
        "event_id": f"test-{int(time.time())}",
        "action_source": "business_messaging",
        "messaging_channel": "whatsapp",
        "user_data": {"ph": [_hash_phone("573001112233")],
                      "em": [_hash_email("prueba@correo.com")]},
        "custom_data": {"currency": _moneda(), "value": 1.0},
    }
    payload = {"data": [ev], "test_event_code": test_code}
    try:
        resp = requests.post(f"{GRAPH}/{pixel}/events",
                             params={"access_token": token}, json=payload, timeout=30)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not resp.ok:
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:400]}"}
    body = resp.json()
    return {"ok": True, "recibidos": body.get("events_received", 0),
            "fbtrace": body.get("fbtrace_id"), "respuesta": body}
