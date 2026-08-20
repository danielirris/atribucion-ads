"""
calculos.py
Lógica de negocio: métricas por período (ROAS, gasto estimado, costo por venta).

Fórmulas (según especificación):
    gasto_estimado = (presupuesto_diario / 1440) * duracion_en_minutos
    roas           = ingreso_total / gasto_estimado
    costo_por_venta = gasto_estimado / numero_ventas
"""
from datetime import datetime
from typing import Optional

import config
import db


def duracion_minutos(periodo: dict, ahora: Optional[datetime] = None) -> float:
    """
    Duración del período en minutos.
    - Si está cerrado, usa la duración guardada (o la recalcula).
    - Si está abierto (hora_fin NULL), la calcula hasta 'ahora'.
    """
    ahora = ahora or db.ahora()
    inicio = db.a_fecha(periodo["hora_inicio"])
    if inicio is None:
        return 0.0
    fin = db.a_fecha(periodo.get("hora_fin"))
    if fin is None:  # período ACTIVO
        return max(0.0, (ahora - inicio).total_seconds() / 60.0)
    # período CERRADO
    if periodo.get("duracion_minutos") is not None:
        return float(periodo["duracion_minutos"])
    return max(0.0, (fin - inicio).total_seconds() / 60.0)


def metricas_periodo(periodo: dict, ahora: Optional[datetime] = None) -> dict:
    """
    Calcula todas las métricas de un período y devuelve un dict enriquecido.
    """
    ahora = ahora or db.ahora()
    activo = periodo.get("hora_fin") is None
    dur = duracion_minutos(periodo, ahora)

    resumen = db.resumen_ventas_periodo(periodo["id"])
    num_ventas = resumen["num_ventas"]
    ingreso_total = float(resumen["ingreso_total"] or 0.0)

    presupuesto = float(periodo["presupuesto"])
    gasto_estimado = (presupuesto / config.MINUTOS_POR_DIA) * dur

    roas = (ingreso_total / gasto_estimado) if gasto_estimado > 0 else 0.0
    costo_por_venta = (gasto_estimado / num_ventas) if num_ventas > 0 else 0.0

    inicio = db.a_fecha(periodo["hora_inicio"])

    return {
        "periodo_id": periodo["id"],
        "ad_id": periodo["ad_id"],
        "presupuesto": presupuesto,
        "hora_inicio": inicio,
        "hora_fin": db.a_fecha(periodo.get("hora_fin")),
        "duracion_minutos": round(dur, 1),
        "num_ventas": num_ventas,
        "ingreso_total": round(ingreso_total, 2),
        "gasto_estimado": round(gasto_estimado, 2),
        "roas": round(roas, 2),
        "costo_por_venta": round(costo_por_venta, 2),
        "estado": "ACTIVO" if activo else "CERRADO",
    }


def metricas_todos_los_periodos(ad_id: str, ahora: Optional[datetime] = None) -> list:
    """Lista de métricas para todos los períodos históricos de un anuncio."""
    ahora = ahora or db.ahora()
    return [metricas_periodo(p, ahora) for p in db.obtener_periodos(ad_id)]


def metricas_periodo_actual(ad_id: str, ahora: Optional[datetime] = None) -> Optional[dict]:
    """Métricas del período actualmente abierto de un anuncio (o None)."""
    ahora = ahora or db.ahora()
    p = db.periodo_abierto(ad_id)
    return metricas_periodo(p, ahora) if p else None


def color_roas(roas: float) -> str:
    """Etiqueta de color según ROAS: 🟢 >2, 🟡 1-2, 🔴 <1."""
    if roas is None:
        return "⚪"
    if roas > 2:
        return "🟢"
    if roas >= 1:
        return "🟡"
    return "🔴"


def formato_antiguedad(minutos: float) -> str:
    """Convierte minutos en un texto legible: '1h 23m', '45m', etc."""
    if minutos is None:
        return "—"
    minutos = int(round(minutos))
    if minutos < 60:
        return f"{minutos}m"
    horas = minutos // 60
    resto = minutos % 60
    if horas < 24:
        return f"{horas}h {resto}m"
    dias = horas // 24
    horas_r = horas % 24
    return f"{dias}d {horas_r}h"


def evaluar_alertas(ahora: Optional[datetime] = None,
                    umbral_minutos: float = 30,
                    umbral_roas: float = 1.5) -> list:
    """
    Genera alertas para anuncios activos cuyo período actual:
      - lleva más de `umbral_minutos` minutos activo, y
      - tiene ROAS menor a `umbral_roas`.
    """
    ahora = ahora or db.ahora()
    alertas = []
    for anuncio in db.obtener_anuncios(solo_activos=True):
        m = metricas_periodo_actual(anuncio["ad_id"], ahora)
        if not m:
            continue
        if m["duracion_minutos"] > umbral_minutos and m["roas"] < umbral_roas:
            alertas.append({
                "ad_id": anuncio["ad_id"],
                "nombre": anuncio["nombre"],
                "presupuesto": m["presupuesto"],
                "antiguedad": formato_antiguedad(m["duracion_minutos"]),
                "duracion_minutos": m["duracion_minutos"],
                "roas": m["roas"],
                "num_ventas": m["num_ventas"],
            })
    return alertas
