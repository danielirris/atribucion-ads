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
K_PAIS = "sb_col_pais"
K_MONEDA = "sb_col_moneda"

DEFAULTS = {
    K_TABLA: "ventas",
    K_ADID: "id_anuncio",
    K_VALOR: "valor_venta",
    K_HORA: "hora_venta",
    K_PRODUCTO: "producto",
    K_ID: "id",
    K_PAIS: "",
    K_MONEDA: "",
}

# País (ISO-2) → moneda local en que se ingresó el `valor`. Cada venta se convierte
# a USD con la tasa de SU moneda, no con una sola tasa global.
PAIS_A_MONEDA = {
    "CO": "COP", "PE": "PEN", "CL": "CLP", "VE": "VES", "EC": "USD",
    "MX": "MXN", "AR": "ARS", "BR": "BRL", "US": "USD", "BO": "BOB",
    "PY": "PYG", "UY": "UYU", "GT": "GTQ", "DO": "DOP", "PA": "USD",
    "CR": "CRC", "ES": "EUR", "HN": "HNL", "NI": "NIO", "SV": "USD",
    "GT2": "GTQ",
}
# País escrito con nombre (no ISO-2) → ISO-2, por si la columna trae el nombre.
_NOMBRE_A_ISO = {
    "colombia": "CO", "peru": "PE", "chile": "CL", "venezuela": "VE",
    "ecuador": "EC", "mexico": "MX", "argentina": "AR", "brasil": "BR",
    "brazil": "BR", "estados unidos": "US", "usa": "US", "bolivia": "BO",
    "paraguay": "PY", "uruguay": "UY", "guatemala": "GT", "panama": "PA",
    "costa rica": "CR", "espana": "ES", "republica dominicana": "DO",
    "honduras": "HN", "nicaragua": "NI", "el salvador": "SV",
}


def _moneda_de(pais: Optional[str], moneda_col: Optional[str]) -> Optional[str]:
    """Determina la moneda de la venta: columna moneda (si existe) tiene prioridad;
    si no, se deriva del país. Devuelve None si no se puede determinar."""
    if moneda_col:
        mc = str(moneda_col).strip().upper()
        if mc and mc not in ("NAN", "NONE"):
            return mc
    if not pais:
        return None
    p = str(pais).strip()
    if not p:
        return None
    # ISO-2 directo, o nombre de país normalizado (sin acentos, minúsculas).
    if len(p) == 2 and p.upper() in PAIS_A_MONEDA:
        return PAIS_A_MONEDA[p.upper()]
    import unicodedata
    norm = unicodedata.normalize("NFKD", p.lower()).encode("ascii", "ignore").decode().strip()
    iso = _NOMBRE_A_ISO.get(norm)
    return PAIS_A_MONEDA.get(iso) if iso else PAIS_A_MONEDA.get(p.upper())


def get_mapeo() -> dict:
    out = {}
    for k, v in DEFAULTS.items():
        val = db.get_config(k)
        out[k] = val if val is not None else v
    return out


def guardar_mapeo(tabla, col_ad_id, col_valor, col_hora, col_producto, col_id,
                  col_pais="", col_moneda="") -> None:
    db.set_config(K_TABLA, tabla or DEFAULTS[K_TABLA])
    db.set_config(K_ADID, col_ad_id or DEFAULTS[K_ADID])
    db.set_config(K_VALOR, col_valor or DEFAULTS[K_VALOR])
    db.set_config(K_HORA, col_hora or DEFAULTS[K_HORA])
    db.set_config(K_PRODUCTO, col_producto or "")
    db.set_config(K_ID, col_id or DEFAULTS[K_ID])
    db.set_config(K_PAIS, col_pais or "")
    db.set_config(K_MONEDA, col_moneda or "")


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
        if filas:
            columnas = list(filas[0].keys())
            return {"ok": True, "columnas": columnas, "muestra": filas,
                    "error": None, "vacia": False}
        # 0 filas: la conexión funciona pero no devolvió datos. Intentamos leer las
        # columnas del ESQUEMA (OpenAPI) y avisamos la causa probable (RLS o vacía).
        columnas = []
        try:
            rs = requests.get(f"{config.SUPABASE_URL}/rest/v1/",
                              headers=_headers(), timeout=15)
            props = ((rs.json().get("definitions") or {}).get(m[K_TABLA]) or {}).get("properties") or {}
            columnas = list(props.keys())
        except Exception:
            pass
        return {"ok": True, "columnas": columnas, "muestra": [], "error": None,
                "vacia": True}
    except Exception as e:
        return {"ok": False, "columnas": [], "muestra": [], "error": str(e)}


def _parse_hora(valor, deteccion: datetime) -> datetime:
    if valor is None:
        return deteccion
    try:
        ts = pd.to_datetime(valor, errors="coerce")
        if pd.isna(ts):
            return deteccion
        # Convertir a la zona horaria de la operación (Bogotá) ANTES de quitar la tz.
        # Supabase guarda timestamptz en UTC; si solo se hiciera replace(tzinfo=None)
        # las ventas nocturnas UTC quedarían con la fecha del día siguiente y el
        # dashboard las descartaría como "futuras".
        try:
            if ts.tzinfo is not None:
                ts = ts.tz_convert(config.APP_TZ)
        except Exception:
            pass
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
    ya_existian = 0     # saltadas por dedup (id ya importado / id repetido)
    sin_valor = 0       # saltadas porque el valor no se pudo leer
    _ids_lote = set()   # ids vistos en ESTE mismo lote (para detectar duplicados)
    col_prod = m[K_PRODUCTO]

    for fila in filas:
        ext_id = str(fila.get(m[K_ID])) if fila.get(m[K_ID]) is not None else None
        if ext_id and (ext_id in _ids_lote or db.venta_existe(HOJA, ext_id)):
            ya_existian += 1
            continue  # ya importada (o id repetido en la misma tabla)
        valor = _parse_valor(fila.get(m[K_VALOR]))
        if valor is None:
            sin_valor += 1
            continue
        if ext_id:
            _ids_lote.add(ext_id)
        ad_id = fila.get(m[K_ADID])
        ad_id = str(ad_id).strip() if ad_id is not None else ""
        hora = _parse_hora(fila.get(m[K_HORA]), deteccion)
        producto = str(fila.get(col_prod)) if col_prod and fila.get(col_prod) is not None else None
        col_pais = m.get(K_PAIS)
        pais = str(fila.get(col_pais)).strip() if col_pais and fila.get(col_pais) is not None else None
        if pais and pais.lower() in ("nan", "none", ""):
            pais = None
        col_moneda = m.get(K_MONEDA)
        moneda_col = str(fila.get(col_moneda)).strip() if col_moneda and fila.get(col_moneda) is not None else None
        # Moneda de ESTA venta: columna moneda si existe, si no se deriva del país.
        moneda = _moneda_de(pais, moneda_col)

        periodo = db.periodo_para_hora(ad_id, hora) if ad_id else None
        periodo_id = periodo["id"] if periodo else None
        if periodo_id is None:
            sin_periodo += 1

        db.insertar_venta(ad_id or "(sin anuncio)", valor, hora, periodo_id,
                          HOJA, ext_id=ext_id, producto=producto, pais=pais,
                          moneda=moneda)
        insertadas += 1

    return {"ok": True, "insertadas": insertadas, "sin_periodo": sin_periodo,
            "leidas": len(filas), "ya_existian": ya_existian, "sin_valor": sin_valor,
            "error": None}
