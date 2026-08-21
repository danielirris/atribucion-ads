"""
supabase_source.py
Segunda fuente de ventas: lee filas de una tabla de Supabase (vía su API REST /
PostgREST) y las hace CONVERGER en la misma tabla `ventas`, junto con las del
Excel. Deduplica por el id de cada fila para no importar dos veces.

Credenciales: SUPABASE_URL y SUPABASE_KEY en variables de entorno.
Tabla y mapeo de columnas: configurables desde la app (Configuración → Supabase),
guardados en config_kv.
"""
from datetime import datetime
from typing import Optional

import requests
import pandas as pd

import config
import db

HOJA = "Supabase"

# Claves de configuración (se editan en la UI).
K_TABLA = "sb_tabla"
K_ADID = "sb_col_ad_id"
K_VALOR = "sb_col_valor"
K_HORA = "sb_col_hora"
K_PRODUCTO = "sb_col_producto"
K_ID = "sb_col_id"

DEFAULTS = {
    K_TABLA: "ventas",
    K_ADID: "id_anuncio",
    K_VALOR: "valor_venta",
    K_HORA: "hora_venta",
    K_PRODUCTO: "producto",
    K_ID: "id",
}


def get_mapeo() -> dict:
    return {k: (db.get_config(k) or v) for k, v in DEFAULTS.items()}


def guardar_mapeo(tabla, col_ad_id, col_valor, col_hora, col_producto, col_id) -> None:
    db.set_config(K_TABLA, tabla or DEFAULTS[K_TABLA])
    db.set_config(K_ADID, col_ad_id or DEFAULTS[K_ADID])
    db.set_config(K_VALOR, col_valor or DEFAULTS[K_VALOR])
    db.set_config(K_HORA, col_hora or DEFAULTS[K_HORA])
    db.set_config(K_PRODUCTO, col_producto or "")
    db.set_config(K_ID, col_id or DEFAULTS[K_ID])


def _headers() -> dict:
    return {
        "apikey": config.SUPABASE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_KEY}",
        "Accept": "application/json",
    }


def probar_conexion() -> dict:
    """Lee unas filas para validar credenciales/tabla. {ok, columnas, muestra, error}."""
    if not config.supabase_configurado():
        return {"ok": False, "columnas": [], "muestra": [], "error":
                "Faltan SUPABASE_URL / SUPABASE_KEY en variables de entorno."}
    m = get_mapeo()
    url = f"{config.SUPABASE_URL}/rest/v1/{m[K_TABLA]}"
    try:
        r = requests.get(url, headers=_headers(), params={"select": "*", "limit": 5}, timeout=15)
        if r.status_code >= 400:
            return {"ok": False, "columnas": [], "muestra": [], "error":
                    f"HTTP {r.status_code}: {r.text[:200]}"}
        filas = r.json()
        columnas = list(filas[0].keys()) if filas else []
        return {"ok": True, "columnas": columnas, "muestra": filas, "error": None}
    except Exception as e:
        return {"ok": False, "columnas": [], "muestra": [], "error": str(e)}


def _parse_hora(valor, deteccion: datetime) -> datetime:
    if valor is None:
        return deteccion
    try:
        ts = pd.to_datetime(valor, errors="coerce")
        if pd.isna(ts):
            return deteccion
        return ts.to_pydatetime().replace(microsecond=0, tzinfo=None)
    except Exception:
        return deteccion


def _parse_valor(valor) -> Optional[float]:
    try:
        if valor is None:
            return None
        txt = str(valor).replace("$", "").replace(",", "").strip()
        return float(txt) if txt else None
    except Exception:
        return None


def sincronizar() -> dict:
    """
    Trae las ventas de Supabase y las inserta en `ventas` (dedup por id).
    Devuelve {ok, insertadas, sin_periodo, error}.
    """
    if not config.supabase_configurado():
        return {"ok": False, "insertadas": 0, "sin_periodo": 0,
                "error": "Supabase no configurado (faltan SUPABASE_URL/SUPABASE_KEY)."}
    m = get_mapeo()
    url = f"{config.SUPABASE_URL}/rest/v1/{m[K_TABLA]}"
    try:
        r = requests.get(url, headers=_headers(), params={"select": "*", "limit": 10000}, timeout=30)
        if r.status_code >= 400:
            return {"ok": False, "insertadas": 0, "sin_periodo": 0,
                    "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        filas = r.json()
    except Exception as e:
        return {"ok": False, "insertadas": 0, "sin_periodo": 0, "error": str(e)}

    deteccion = db.ahora()
    insertadas = 0
    sin_periodo = 0
    col_prod = m[K_PRODUCTO]

    for fila in filas:
        ext_id = str(fila.get(m[K_ID])) if fila.get(m[K_ID]) is not None else None
        if ext_id and db.venta_existe(HOJA, ext_id):
            continue  # ya importada
        valor = _parse_valor(fila.get(m[K_VALOR]))
        if valor is None:
            continue
        ad_id = fila.get(m[K_ADID])
        ad_id = str(ad_id).strip() if ad_id is not None else ""
        hora = _parse_hora(fila.get(m[K_HORA]), deteccion)
        producto = str(fila.get(col_prod)) if col_prod and fila.get(col_prod) is not None else None

        periodo = db.periodo_para_hora(ad_id, hora) if ad_id else None
        periodo_id = periodo["id"] if periodo else None
        if periodo_id is None:
            sin_periodo += 1

        db.insertar_venta(ad_id or "(sin anuncio)", valor, hora, periodo_id,
                          HOJA, ext_id=ext_id, producto=producto)
        insertadas += 1

    return {"ok": True, "insertadas": insertadas, "sin_periodo": sin_periodo, "error": None}
