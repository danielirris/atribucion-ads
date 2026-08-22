"""
ia.py
Asistente de IA que responde preguntas en lenguaje natural sobre los anuncios del
Dashboard. Soporta DOS proveedores; elige automáticamente según la key que tengas:

  - OpenAI   -> si defines OPENAI_API_KEY      (modelo por defecto: gpt-4o-mini)
  - Anthropic-> si defines ANTHROPIC_API_KEY   (modelo por defecto: claude-opus-5)

Puedes forzar el modelo con IA_MODEL. Se le pasan los datos ya calculados del
rango/filtros seleccionados; el modelo responde SOLO sobre esos datos, en español.
"""
import json
import os


def _tiene_openai() -> bool:
    if not os.getenv("OPENAI_API_KEY"):
        return False
    try:
        import openai  # noqa: F401
        return True
    except Exception:
        return False


def _tiene_anthropic() -> bool:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


def proveedor() -> str:
    if _tiene_openai():
        return "openai"
    if _tiene_anthropic():
        return "anthropic"
    return ""


def disponible() -> bool:
    return proveedor() != ""


def modelo() -> str:
    m = (os.getenv("IA_MODEL") or "").strip()
    if m:
        return m
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
    """Envía la pregunta + los datos al proveedor disponible y devuelve el texto.
    `historial` opcional: lista de {'role','content'} de turnos previos (chat)."""
    user = _mensaje_usuario(pregunta, filas, contexto)
    prov = proveedor()
    if prov == "openai":
        from openai import OpenAI
        client = OpenAI()  # usa OPENAI_API_KEY del entorno
        mensajes = [{"role": "system", "content": _SYSTEM}]
        mensajes += (historial or [])
        mensajes.append({"role": "user", "content": user})
        resp = client.chat.completions.create(
            model=modelo(), messages=mensajes, max_tokens=1500, temperature=0.2)
        return (resp.choices[0].message.content or "").strip() or "No obtuve respuesta."
    elif prov == "anthropic":
        import anthropic
        client = anthropic.Anthropic()
        mensajes = list(historial or [])
        mensajes.append({"role": "user", "content": user})
        resp = client.messages.create(
            model=modelo(), max_tokens=1500, system=_SYSTEM, messages=mensajes)
        partes = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        return "\n".join(partes).strip() or "No obtuve respuesta."
    raise RuntimeError("No hay proveedor de IA configurado.")
