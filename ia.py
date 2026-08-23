"""
ia.py
Asistente de IA sobre los anuncios del Dashboard. Soporta OpenAI o Anthropic.

La configuración (proveedor, API key, modelo) se puede poner:
  1) En la app: Configuración → IA  (se guarda en la base, volumen /data), o
  2) Como variables de entorno: OPENAI_API_KEY / ANTHROPIC_API_KEY / IA_MODEL.
La config de la app tiene prioridad; si no hay, se usan las variables de entorno.
"""
import json
import os

import db

K_PROV = "ia_proveedor"     # "openai" | "anthropic" | ""(auto)
K_KEY = "ia_api_key"
K_MODELO = "ia_modelo"


def _cfg() -> dict:
    return {
        "proveedor": (db.get_config(K_PROV, "") or "").strip().lower(),
        "api_key": (db.get_config(K_KEY, "") or "").strip(),
        "modelo": (db.get_config(K_MODELO, "") or "").strip(),
    }


def _sdk_ok(prov: str) -> bool:
    try:
        if prov == "openai":
            import openai  # noqa: F401
        else:
            import anthropic  # noqa: F401
        return True
    except Exception:
        return False


def _key(prov: str):
    """API key de un proveedor: primero la de la config, luego la del entorno."""
    c = _cfg()
    if c["proveedor"] == prov and c["api_key"]:
        return c["api_key"]
    return os.getenv("OPENAI_API_KEY") if prov == "openai" else os.getenv("ANTHROPIC_API_KEY")


def proveedor() -> str:
    """Proveedor a usar: el forzado en config (si tiene key y SDK), o autodetección."""
    forzado = _cfg()["proveedor"]
    if forzado in ("openai", "anthropic") and _key(forzado) and _sdk_ok(forzado):
        return forzado
    for p in ("openai", "anthropic"):
        if _key(p) and _sdk_ok(p):
            return p
    return ""


def disponible() -> bool:
    return proveedor() != ""


def modelo() -> str:
    c = _cfg()
    if c["modelo"]:
        return c["modelo"]
    env = (os.getenv("IA_MODEL") or "").strip()
    if env:
        return env
    return "gpt-4o-mini" if proveedor() == "openai" else "claude-opus-5"


def _fila_min(f: dict) -> dict:
    def r(x, n=2):
        try:
            return round(float(x), n)
        except Exception:
            return None
    return {
        "anuncio": str(f.get("nombre") or "")[:90],
        "cuenta": f.get("cuenta"),
        "estado": "activo" if (f.get("activos") or 0) > 0 else "pausado",
        "presupuesto_usd": r(f.get("presupuesto") or 0),
        "gasto_usd": r(f.get("gasto") or 0),
        "ventas": int(f.get("num") or 0),
        "ingresos_usd": r(f.get("ingresos") or 0),
        "roas": r(f.get("roas") or 0),
        "utilidad_usd": r(f.get("ganancia") or 0),
        "costo_por_venta_usd": r(f.get("costo_venta") or 0),
        "cpm_usd": r(f.get("cpm")) if f.get("cpm") is not None else None,
        "ctr_pct": r(f.get("ctr")) if f.get("ctr") is not None else None,
        "conversaciones": int(f.get("conv") or 0) if f.get("conv") is not None else 0,
        "roas_desde_ult_cambio": r(f.get("roas_mod") or 0),
        "ventas_desde_ult_cambio": int(f.get("ventas_mod") or 0),
    }


_SYSTEM = (
    "Eres un analista experto en Facebook Ads. Respondes SOLO con base en los datos "
    "que te dan (una lista JSON de anuncios/conjuntos/campañas con sus métricas); NO "
    "inventes datos que no estén ahí. Todos los montos están en dólares (USD). "
    "Responde en español, claro y conciso, con listas o tablas cuando ayude. "
    "Cuando pregunten por 'promedio' o 'por debajo del promedio', calcula el promedio "
    "de la métrica sobre los anuncios dados. Los datos ya corresponden al rango "
    "indicado en el contexto. Al listar anuncios, muestra el nombre y las cifras clave."
)


def _mensaje_usuario(pregunta: str, filas: list, contexto: str) -> str:
    datos = [_fila_min(f) for f in filas]
    return (
        f"CONTEXTO (rango y filtros actuales): {contexto}\n\n"
        f"DATOS ({len(datos)} elementos, JSON):\n"
        f"{json.dumps(datos, ensure_ascii=False)}\n\n"
        f"PREGUNTA: {pregunta.strip()}"
    )


def preguntar(pregunta: str, filas: list, contexto: str, historial=None) -> str:
    user = _mensaje_usuario(pregunta, filas, contexto)
    prov = proveedor()
    if prov == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=_key("openai"))
        mensajes = [{"role": "system", "content": _SYSTEM}]
        mensajes += (historial or [])
        mensajes.append({"role": "user", "content": user})
        resp = client.chat.completions.create(
            model=modelo(), messages=mensajes, max_tokens=1500, temperature=0.2)
        return (resp.choices[0].message.content or "").strip() or "No obtuve respuesta."
    elif prov == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=_key("anthropic"))
        mensajes = list(historial or [])
        mensajes.append({"role": "user", "content": user})
        resp = client.messages.create(
            model=modelo(), max_tokens=1500, system=_SYSTEM, messages=mensajes)
        partes = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        return "\n".join(partes).strip() or "No obtuve respuesta."
    raise RuntimeError("No hay proveedor de IA configurado.")
