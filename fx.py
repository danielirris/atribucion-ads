"""
fx.py
Tipo de cambio a USD, actualizado automáticamente una vez al día.

- tasa_a_usd("MXN") devuelve cuántos USD vale 1 MXN (ej. ~0.058).
- Se cachea por día en config_kv (clave fx_<fecha>_<moneda>).
- Se puede fijar un valor manual por moneda (config fx_manual_<moneda>).
- Si la API falla y no hay caché ni manual, usa un valor de respaldo.

API gratuita sin llave: frankfurter.app (respaldo: open.er-api.com).
"""
from typing import Optional

import requests

import db

# Respaldo aproximado si no hay internet ni caché (1 unidad de moneda -> USD).
FALLBACK = {
    "USD": 1.0, "MXN": 0.058, "EUR": 1.08, "COP": 0.00025, "ARS": 0.0011,
    "BRL": 0.19, "CLP": 0.0011, "PEN": 0.27, "GBP": 1.27, "CAD": 0.73,
    # OJO: VES (bolívar) es muy volátil y muchas APIs no lo cotizan. Este respaldo
    # es aproximado; fija una tasa manual (Configuración → tasa manual VES) para
    # que la conversión sea correcta.
    "VES": 0.020, "BOB": 0.145, "PYG": 0.00013, "UYU": 0.025,
    "GTQ": 0.13, "DOP": 0.017, "CRC": 0.0020, "HNL": 0.040, "NIO": 0.027,
}


def _hoy() -> str:
    return db.a_texto(db.ahora())[:10]


def tasa_a_usd(moneda: Optional[str]) -> float:
    """Multiplicador para convertir `moneda` -> USD."""
    moneda = (moneda or "USD").upper().strip()
    if moneda in ("USD", ""):
        return 1.0

    # 1) Override manual (si el usuario fijó una tasa).
    man = db.get_config(f"fx_manual_{moneda}")
    if man:
        try:
            return float(man)
        except (TypeError, ValueError):
            pass

    # 2) Caché del día.
    clave = f"fx_{_hoy()}_{moneda}"
    cached = db.get_config(clave)
    if cached:
        try:
            return float(cached)
        except (TypeError, ValueError):
            pass

    # 3) Consultar API y cachear.
    r = _consultar(moneda)
    if r:
        db.set_config(clave, str(r))
        return r

    # 4) Respaldo.
    return FALLBACK.get(moneda, 1.0)


def _consultar(moneda: str) -> Optional[float]:
    try:
        resp = requests.get("https://api.frankfurter.app/latest",
                            params={"from": moneda, "to": "USD"}, timeout=10)
        if resp.ok:
            v = (resp.json().get("rates") or {}).get("USD")
            if v:
                return float(v)
    except Exception:
        pass
    try:
        resp = requests.get(f"https://open.er-api.com/v6/latest/{moneda}", timeout=10)
        if resp.ok:
            v = (resp.json().get("rates") or {}).get("USD")
            if v:
                return float(v)
    except Exception:
        pass
    return None


def info_tasa(moneda: Optional[str]) -> dict:
    """Info para la UI: {moneda, tasa, origen}."""
    moneda = (moneda or "USD").upper().strip()
    if moneda in ("USD", ""):
        return {"moneda": "USD", "tasa": 1.0, "origen": "USD (sin conversión)"}
    if db.get_config(f"fx_manual_{moneda}"):
        return {"moneda": moneda, "tasa": tasa_a_usd(moneda), "origen": "manual"}
    if db.get_config(f"fx_{_hoy()}_{moneda}"):
        return {"moneda": moneda, "tasa": tasa_a_usd(moneda), "origen": f"automático ({_hoy()})"}
    return {"moneda": moneda, "tasa": tasa_a_usd(moneda), "origen": "respaldo/nuevo"}
