"""
ia.py
Asistente de IA (Claude) que responde preguntas en lenguaje natural sobre los
anuncios que se ven en el Dashboard. Se le pasan los datos ya calculados del
rango/filtros seleccionados y la pregunta del usuario; Claude razona SOLO sobre
esos datos y responde en español.

Requisitos:
  - Paquete `anthropic` (en requirements.txt).
  - Variable de entorno ANTHROPIC_API_KEY (se pone en EasyPanel).
  - Opcional IA_MODEL para elegir el modelo (por defecto claude-opus-5).
"""
import json
import os
from typing import Optional


def disponible() -> bool:
    """True si hay clave de API configurada y el SDK instalado."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


def modelo() -> str:
    # Por defecto el más capaz; se puede bajar a claude-sonnet-5 o claude-haiku-4-5
    # con la variable IA_MODEL para reducir costo.
    return os.getenv("IA_MODEL", "claude-opus-5").strip() or "claude-opus-5"


def _fila_min(f: dict) -> dict:
    """Versión compacta de una fila (solo lo que la IA necesita)."""
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
        # Desde el último cambio de presupuesto:
        "roas_desde_ult_cambio": r(f.get("roas_mod") or 0),
        "ventas_desde_ult_cambio": int(f.get("ventas_mod") or 0),
    }


_SYSTEM = (
    "Eres un analista experto en Facebook Ads. Respondes SOLO con base en los datos "
    "que te dan (una lista JSON de anuncios/conjuntos/campañas con sus métricas); NO "
    "inventes datos que no estén ahí. Todos los montos están en dólares (USD). "
    "Responde en español, claro y conciso, usando listas o tablas cuando ayude. "
    "Cuando te pregunten por 'promedio' o 'por debajo del promedio', calcula el "
    "promedio de la métrica sobre los anuncios que te dieron. Si la pregunta implica "
    "un periodo (hoy, últimos N días) recuerda que los datos ya corresponden al rango "
    "seleccionado que se indica en el contexto; si el rango no coincide, dilo. "
    "Cuando listes anuncios, muestra el nombre y las cifras relevantes."
)


def preguntar(pregunta: str, filas: list, contexto: str) -> str:
    """Envía la pregunta + los datos a Claude y devuelve la respuesta en texto."""
    import anthropic
    client = anthropic.Anthropic()  # usa ANTHROPIC_API_KEY del entorno
    datos = [_fila_min(f) for f in filas]
    user = (
        f"CONTEXTO (rango y filtros actuales): {contexto}\n\n"
        f"DATOS ({len(datos)} elementos, JSON):\n"
        f"{json.dumps(datos, ensure_ascii=False)}\n\n"
        f"PREGUNTA: {pregunta.strip()}"
    )
    resp = client.messages.create(
        model=modelo(),
        max_tokens=2000,
        output_config={"effort": "medium"},
        system=_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    partes = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return "\n".join(partes).strip() or "No obtuve respuesta."
