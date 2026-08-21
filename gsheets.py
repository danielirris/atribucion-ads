"""
gsheets.py
Lee un Google Sheets (todas sus pestañas) sin credenciales, usando la exportación
a XLSX. El Sheet debe estar compartido como "Cualquiera con el enlace: Lector".

Flujo:
  - El usuario pega la URL del Sheet en Configuración.
  - La app descarga  https://docs.google.com/spreadsheets/d/<ID>/export?format=xlsx
    (todas las pestañas) y las procesa igual que un Excel, detectando columnas
    de forma flexible (ID del anuncio, valor, hora, país).
  - Deduplica por hoja + número de fila para no reimportar.
"""
import io
import re
from datetime import datetime
from typing import Optional

import requests
import pandas as pd

import db
import excel_watcher as watcher

HOJA = "GoogleSheets"
K_URL = "gsheet_url"
# Overrides manuales de columnas (si el auto-detector se equivoca).
K_COL = {"id": "gsheet_col_id", "valor": "gsheet_col_valor",
         "hora": "gsheet_col_hora", "pais": "gsheet_col_pais"}


def get_url() -> str:
    return db.get_config(K_URL, "") or ""


def set_url(url: str) -> None:
    db.set_config(K_URL, (url or "").strip())


def get_overrides() -> dict:
    return {k: (db.get_config(ck) or "") for k, ck in K_COL.items()}


def set_overrides(id_c="", valor_c="", hora_c="", pais_c="") -> None:
    db.set_config(K_COL["id"], id_c or "")
    db.set_config(K_COL["valor"], valor_c or "")
    db.set_config(K_COL["hora"], hora_c or "")
    db.set_config(K_COL["pais"], pais_c or "")


def _columnas(cols) -> dict:
    """Detección automática, sobreescrita por los overrides manuales si existen."""
    cmap = watcher.detectar_columnas(cols)
    for k, ck in K_COL.items():
        manual = (db.get_config(ck) or "").strip()
        if manual:
            # buscar la columna real por nombre (tolerante a mayúsculas/espacios)
            for c in cols:
                if str(c).strip().lower() == manual.lower():
                    cmap[k] = c
                    break
            else:
                cmap[k] = manual
    return cmap


def _extraer_id(url: str) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if m:
        return m.group(1)
    # por si pegan solo el ID
    if re.fullmatch(r"[a-zA-Z0-9-_]{20,}", url.strip()):
        return url.strip()
    return None


def _descargar_xlsx(url: str):
    sid = _extraer_id(url)
    if not sid:
        return None, "No pude extraer el ID del Sheet de esa URL."
    export = f"https://docs.google.com/spreadsheets/d/{sid}/export?format=xlsx"
    try:
        r = requests.get(export, timeout=30)
        if r.status_code == 200 and r.content[:2] == b"PK":  # xlsx = zip (PK)
            return r.content, None
        if "text/html" in r.headers.get("content-type", ""):
            return None, ("No tengo acceso al Sheet. Compártelo como "
                          "'Cualquiera con el enlace: Lector'.")
        return None, f"HTTP {r.status_code} al descargar el Sheet."
    except Exception as e:
        return None, str(e)


def probar() -> dict:
    """Descarga y lista las pestañas y columnas detectadas. {ok,hojas,error}."""
    contenido, err = _descargar_xlsx(get_url())
    if err:
        return {"ok": False, "hojas": [], "error": err}
    try:
        hojas = pd.read_excel(io.BytesIO(contenido), sheet_name=None, engine="openpyxl")
    except Exception as e:
        return {"ok": False, "hojas": [], "error": f"No pude leer el Sheet: {e}"}
    info = []
    for nombre, dff in hojas.items():
        cols = [str(c).strip() for c in (dff.columns if dff is not None else [])]
        dfr = dff.rename(columns=lambda c: str(c).strip()) if dff is not None else None
        cmap = _columnas(cols)
        muestra_id = None
        if dfr is not None and cmap["id"] and cmap["id"] in dfr.columns and len(dfr):
            for v in dfr[cmap["id"]].tolist():
                if v is not None and str(v).strip().lower() not in ("nan", "none", ""):
                    muestra_id = str(v).strip()
                    break
        info.append({"nombre": nombre, "columnas": cols, "detectado": cmap,
                     "muestra_id": muestra_id, "filas": 0 if dff is None else len(dff)})
    return {"ok": True, "hojas": info, "error": None}


def sincronizar() -> dict:
    """Descarga todas las pestañas y las importa (dedup por hoja+fila). {ok,insertadas,sin_periodo,error}."""
    contenido, err = _descargar_xlsx(get_url())
    if err:
        return {"ok": False, "insertadas": 0, "sin_periodo": 0, "error": err}
    try:
        hojas = pd.read_excel(io.BytesIO(contenido), sheet_name=None, engine="openpyxl")
    except Exception as e:
        return {"ok": False, "insertadas": 0, "sin_periodo": 0, "error": str(e)}

    deteccion = db.ahora()
    insertadas, sin_periodo = 0, 0
    detalle = []
    for nombre_hoja, dff in hojas.items():
        if dff is None or dff.empty:
            detalle.append({"hoja": nombre_hoja, "leidas": 0, "insertadas": 0,
                            "motivo": "hoja vacía"})
            continue
        dff = dff.rename(columns=lambda c: str(c).strip())
        cmap = _columnas(list(dff.columns))
        if not cmap["id"] or not cmap["valor"]:
            detalle.append({"hoja": nombre_hoja, "leidas": len(dff), "insertadas": 0,
                            "motivo": f"no detecté columna de ID y/o valor. Columnas: {list(dff.columns)}"})
            continue
        ins_hoja, dup, sin_val, sin_id = 0, 0, 0, 0
        for i, (_, fila) in enumerate(dff.iterrows()):
            ext_id = f"{nombre_hoja}#{i}"
            if db.venta_existe(HOJA, ext_id):
                dup += 1
                continue
            valor = watcher._parse_valor(fila.get(cmap["valor"]))
            if valor is None:
                sin_val += 1
                continue
            ad_id = fila.get(cmap["id"])
            ad_id = str(ad_id).strip() if ad_id is not None else ""
            if not ad_id or ad_id.lower() in ("nan", "none"):
                sin_id += 1
                continue
            hora = watcher._parse_hora(fila.get(cmap["hora"]) if cmap["hora"] else None, deteccion)
            pais = fila.get(cmap["pais"]) if cmap["pais"] else None
            pais = str(pais).strip() if pais is not None and str(pais).strip().lower() not in ("nan", "none", "") else None
            periodo = db.periodo_para_hora(ad_id, hora)
            periodo_id = periodo["id"] if periodo else None
            if periodo_id is None:
                sin_periodo += 1
            db.insertar_venta(ad_id, valor, hora, periodo_id, HOJA,
                              ext_id=ext_id, pais=pais)
            insertadas += 1
            ins_hoja += 1
        detalle.append({"hoja": nombre_hoja, "leidas": len(dff), "insertadas": ins_hoja,
                        "columnas": cmap,
                        "motivo": (f"OK · {ins_hoja} nuevas, {dup} ya estaban, "
                                   f"{sin_val} sin valor, {sin_id} sin ID")})
    return {"ok": True, "insertadas": insertadas, "sin_periodo": sin_periodo,
            "detalle": detalle, "error": None}
