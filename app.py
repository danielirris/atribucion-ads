"""
app.py
Dashboard de atribución de ventas a Facebook Ads con seguimiento por período
de presupuesto.

Ejecutar con:
    streamlit run app.py
"""
import time
import html as _html
from datetime import datetime, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import config
import db
import calculos
import facebook_api as fb
import excel_watcher as watcher
import supabase_source as supa

st.set_page_config(page_title="Atribución Facebook Ads", page_icon="📊", layout="wide")

# Marcador de versión: sirve para confirmar que el redeploy tomó el código nuevo.
APP_VERSION = "v4 · 2026-08-20 · Config + filtros + Supabase + tema oscuro"


# --------------------------------------------------------------------------- #
#  Arranque único de servicios en segundo plano (una vez por proceso)
# --------------------------------------------------------------------------- #
@st.cache_resource(show_spinner=False)
def arrancar_servicios():
    """
    Inicializa la DB y lanza los hilos de background UNA sola vez por proceso.
    st.cache_resource garantiza que no se reinicien en cada rerun de Streamlit.
    """
    db.init_db()
    fb.inicializar_api()
    fb.iniciar_polling()      # hilo de polling de Facebook
    watcher.iniciar_watcher() # hilo de watchdog del Excel
    return {"iniciado_en": db.a_texto(db.ahora())}


servicios = arrancar_servicios()


# --------------------------------------------------------------------------- #
#  Helpers de UI
# --------------------------------------------------------------------------- #
def _fmt_money(v):
    try:
        return f"${v:,.2f}"
    except Exception:
        return str(v)


def _usd(v):
    """Formato tipo '1.234,56 US$' (coma decimal), como la herramienta de referencia."""
    if v is None:
        return "—"
    try:
        s = f"{float(v):,.2f}"            # 1,234.56
        s = s.replace(",", "@").replace(".", ",").replace("@", ".")
        return f"{s} US$"
    except Exception:
        return str(v)


def _roas_color(r):
    if r is None:
        return "#9ca3af"
    if r >= 2:
        return "#22c55e"
    if r >= 1:
        return "#eab308"
    return "#ef4444"


def _spark_svg(serie, color="#22c55e", w=78, h=26):
    """Mini-gráfica (polyline SVG) a partir de una lista de valores."""
    pts = [x for x in (serie or []) if x is not None]
    if len(pts) < 2:
        return f'<svg width="{w}" height="{h}"></svg>'
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1.0
    n = len(pts)
    coords = []
    for i, v in enumerate(pts):
        x = (i / (n - 1)) * (w - 4) + 2
        y = h - 2 - ((v - lo) / rng) * (h - 4)
        coords.append(f"{x:.1f},{y:.1f}")
    path = " ".join(coords)
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<polyline points="{path}" fill="none" stroke="{color}" '
            f'stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg>')


def _fecha_corta(iso):
    """'2026-08-04T10:00:00-0700' -> '04 ago 2026'."""
    if not iso:
        return "—"
    meses = ["ene", "feb", "mar", "abr", "may", "jun",
             "jul", "ago", "sep", "oct", "nov", "dic"]
    d = db.a_fecha(str(iso)[:19].replace("T", " ")) if "T" in str(iso) else db.a_fecha(iso)
    if not d:
        return str(iso)[:10]
    return f"{d.day:02d} {meses[d.month - 1]} {d.year}"


@st.cache_data(ttl=120, show_spinner=False)
def _insights_cache(date_preset: str):
    """Cachea los insights de Facebook 2 min para no llamar en cada rerun."""
    return fb.obtener_insights(date_preset)


def cambio_presupuesto_completo(ad_id: str, nuevo_monto: float) -> dict:
    """
    Flujo Parte 8: envía el cambio a Facebook y SOLO si Facebook confirma,
    cierra el período anterior y abre uno nuevo en SQLite.
    Devuelve {"ok": bool, "error": str|None, "solo_local": bool}.
    """
    anuncio = db.obtener_anuncio(ad_id)
    if not anuncio:
        return {"ok": False, "error": "Anuncio no encontrado.", "solo_local": False}

    adset_id = anuncio.get("adset_id")
    conexion_id = anuncio.get("conexion_id")
    hay_conexion = bool(fb._conexiones_efectivas()) if fb.SDK_DISPONIBLE else False

    # Si hay API/conexión disponible, intentamos el cambio en Facebook.
    if fb.SDK_DISPONIBLE and hay_conexion and adset_id:
        r = fb.actualizar_presupuesto_facebook(adset_id, nuevo_monto, conexion_id)
        if not r["ok"]:
            # NO cerramos el período en SQLite.
            return {"ok": False, "error": r["error"], "solo_local": False}
        db.cambiar_periodo(ad_id, nuevo_monto)
        return {"ok": True, "error": None, "solo_local": False}

    # Sin API disponible: registro local solamente (advertencia).
    db.cambiar_periodo(ad_id, nuevo_monto)
    motivo = "sin adset_id" if not adset_id else "Facebook no disponible"
    return {"ok": True, "error": None, "solo_local": True, "motivo": motivo}


# --------------------------------------------------------------------------- #
#  Barra lateral: estado de servicios
# --------------------------------------------------------------------------- #
def sidebar_estado():
    st.sidebar.header("⚙️ Estado del sistema")
    st.sidebar.caption(f"Versión: {APP_VERSION}")

    est_fb = fb.obtener_estado()
    est_wt = watcher.obtener_estado()

    # Facebook
    if est_fb["api_ok"]:
        st.sidebar.success("Facebook API: conectada")
    else:
        st.sidebar.error("Facebook API: sin conexión")
    if est_fb.get("ultimo_error"):
        st.sidebar.caption(f"⚠️ {est_fb['ultimo_error']}")
    up = est_fb.get("ultimo_polling")
    st.sidebar.caption(f"Último polling: {db.a_texto(up) if isinstance(up, datetime) else '—'}")
    st.sidebar.caption(
        f"Conexiones: {est_fb.get('num_conexiones', 0)} · "
        f"Cuentas: {est_fb.get('num_cuentas', 0)} · "
        f"Anuncios activos: {est_fb.get('num_anuncios', 0)}")

    # Watcher
    if est_wt["activo"]:
        st.sidebar.success("Watcher Excel: activo")
    else:
        st.sidebar.warning("Watcher Excel: inactivo")
    if est_wt.get("ultimo_error"):
        st.sidebar.caption(f"⚠️ {est_wt['ultimo_error']}")
    st.sidebar.caption(f"Ventas procesadas (sesión): {est_wt.get('ventas_procesadas', 0)}")

    st.sidebar.divider()
    cta, ctb = st.sidebar.columns(2)
    if cta.button("🔄 Actualizar", use_container_width=True):
        try:
            _insights_cache.clear()
        except Exception:
            pass
        st.rerun()
    if ctb.button("📥 Recargar", use_container_width=True,
                  help="Trae anuncios de todas las conexiones."):
        with st.spinner("Consultando Facebook (todas las conexiones)..."):
            r = fb.cargar_todo()
            try:
                _insights_cache.clear()
            except Exception:
                pass
        if r["num_anuncios"]:
            st.sidebar.success(f"{r['num_anuncios']} anuncios de {r['num_cuentas']} cuenta(s).")
        if r["errores"]:
            st.sidebar.error("Errores: " + "; ".join(r["errores"])[:300])
        st.rerun()

    sidebar_filtros()

    with st.sidebar.expander("📋 Eventos recientes"):
        for m in (est_fb.get("mensajes", []) + est_wt.get("mensajes", []))[:15]:
            st.caption(m)


def sidebar_filtros():
    """Filtros globales que afectan al Dashboard (se guardan en session_state)."""
    st.sidebar.divider()
    st.sidebar.markdown("### 🔎 Filtros")
    todos = db.obtener_anuncios(solo_activos=False)
    cuentas = sorted({(a.get("cuenta_nombre") or "—") for a in todos})
    paises = sorted({(a.get("cuenta_pais") or "—") for a in todos})

    st.sidebar.radio("Estado", ["Activos", "Apagados", "Todos"],
                     horizontal=True, key="f_estado")
    st.sidebar.selectbox("Cuenta publicitaria", ["Todas"] + cuentas, key="f_cuenta")
    st.sidebar.selectbox("País", ["Todos"] + paises, key="f_pais")
    st.sidebar.selectbox("Rango (métricas Facebook)",
                         ["Hoy", "Últimos 7 días", "Últimos 30 días", "Máximo"],
                         key="f_rango")

    # Resumen de gasto invertido por país (rango actual).
    _resumen_pais(todos)


def _resumen_pais(todos):
    """Muestra gasto invertido por país (según insights del rango elegido)."""
    rango_lbl = st.session_state.get("f_rango", "Hoy")
    preset = {"Hoy": "today", "Últimos 7 días": "last_7d",
              "Últimos 30 días": "last_30d", "Máximo": "maximum"}.get(rango_lbl, "today")
    insights = _insights_cache(preset)
    if not insights:
        return
    gasto_pais = {}
    for a in todos:
        pais = a.get("cuenta_pais") or "—"
        sp = insights.get(str(a["ad_id"]), {}).get("spend")
        if sp:
            gasto_pais[pais] = gasto_pais.get(pais, 0.0) + sp
    if not gasto_pais:
        return
    st.sidebar.markdown("**💸 Gasto por país**")
    for pais, g in sorted(gasto_pais.items(), key=lambda x: -x[1]):
        st.sidebar.caption(f"{pais}: {_usd(g)}")


# --------------------------------------------------------------------------- #
#  Sección 1 — Vista general de anuncios activos
# --------------------------------------------------------------------------- #
def _es_activo(a: dict) -> bool:
    est = a.get("effective_status")
    if est:
        return est == "ACTIVE"
    return a.get("activo") == 1


def seccion_vista_general():
    st.header("Anuncios")
    ahora = db.ahora()

    # Los filtros viven en la barra lateral (session_state).
    filtro = st.session_state.get("f_estado", "Activos")
    rango_lbl = st.session_state.get("f_rango", "Hoy")
    cuenta_sel = st.session_state.get("f_cuenta", "Todas")
    pais_sel = st.session_state.get("f_pais", "Todos")

    RANGO = {
        "Hoy": ("today", ahora.replace(hour=0, minute=0, second=0, microsecond=0)),
        "Últimos 7 días": ("last_7d", ahora - timedelta(days=7)),
        "Últimos 30 días": ("last_30d", ahora - timedelta(days=30)),
        "Máximo": ("maximum", None),
    }
    date_preset, cutoff = RANGO.get(rango_lbl, RANGO["Hoy"])

    todos = db.obtener_anuncios(solo_activos=False)
    if filtro == "Activos":
        anuncios = [a for a in todos if _es_activo(a)]
    elif filtro == "Apagados":
        anuncios = [a for a in todos if not _es_activo(a)]
    else:
        anuncios = todos
    if cuenta_sel != "Todas":
        anuncios = [a for a in anuncios if (a.get("cuenta_nombre") or "—") == cuenta_sel]
    if pais_sel != "Todos":
        anuncios = [a for a in anuncios if (a.get("cuenta_pais") or "—") == pais_sel]

    st.caption(f"Ordenado por ROAS ↓ · estado: {filtro} · cuenta: {cuenta_sel} · "
               f"país: {pais_sel} · rango: {rango_lbl} · {len(anuncios)} anuncio(s)")

    if not anuncios:
        st.info("No hay anuncios para este filtro. Usa **📥 Recargar anuncios de Facebook** "
                "en la barra lateral.")
        return

    insights = _insights_cache(date_preset)
    fb_ok = bool(insights)
    ventas_agg = db.ventas_agg_por_ad(cutoff)

    filas = []
    for a in anuncios:
        ad_id = a["ad_id"]
        ins = insights.get(str(ad_id), {})
        vagg = ventas_agg.get(ad_id, {"num_ventas": 0, "ingreso_total": 0.0})
        num, ingresos = vagg["num_ventas"], vagg["ingreso_total"]

        abierto = db.periodo_abierto(ad_id)
        if abierto:
            presupuesto = float(abierto["presupuesto"])
        else:
            ps = db.obtener_periodos(ad_id)
            presupuesto = float(ps[-1]["presupuesto"]) if ps else None

        spend_real = ins.get("spend")
        if spend_real is not None:
            gasto = spend_real
        else:
            m = calculos.metricas_periodo_actual(ad_id, ahora)
            gasto = m["gasto_estimado"] if m else 0.0

        roas = (ingresos / gasto) if gasto and gasto > 0 else 0.0
        filas.append({
            "a": a, "ad_id": ad_id, "activo": _es_activo(a),
            "presupuesto": presupuesto, "spend": spend_real, "gasto": gasto,
            "pct": (spend_real / presupuesto * 100) if (spend_real is not None and presupuesto) else None,
            "cpm": ins.get("cpm"), "ctr": ins.get("ctr"),
            "num": num, "ingresos": ingresos,
            "val_venta": (ingresos / num) if num > 0 else 0.0,
            "ganancia": ingresos - (gasto or 0.0), "roas": roas,
            "conv": ins.get("conversaciones"), "costo_conv": ins.get("costo_conversacion"),
            "badge": calculos.salud_badge(roas, num),
            "serie": calculos.serie_roas_periodos(ad_id, ahora=ahora),
            "accion": calculos.ultima_accion(ad_id, ahora),
        })

    filas.sort(key=lambda f: f["roas"], reverse=True)

    if not fb_ok:
        st.caption("⚠️ Métricas de Facebook (gasto real, CPM, CTR, conversaciones) no "
                   "disponibles ahora — se muestran '—' y el ROAS usa el gasto estimado. "
                   "Revisa la conexión en la barra lateral.")

    st.markdown(_TABLA_CSS + _render_tabla(filas), unsafe_allow_html=True)

    # --- Opción A: editar presupuesto de un anuncio (envía a Facebook) ---
    st.subheader("✏️ Cambiar presupuesto de un anuncio (envía a Facebook)")
    col1, col2, col3 = st.columns([3, 2, 2])
    opciones = {f"{f['a']['nombre']}  ·  {f['ad_id']}": f['ad_id'] for f in filas}
    with col1:
        sel = st.selectbox("Anuncio", list(opciones.keys()), key="edit_sel")
    ad_id_sel = opciones[sel]
    abierto_sel = db.periodo_abierto(ad_id_sel)
    presup_actual = float(abierto_sel["presupuesto"]) if abierto_sel else 0.0
    with col2:
        nuevo = st.number_input("Nuevo presupuesto diario", min_value=0.0,
                                value=float(presup_actual or 0.0), step=1.0, key="edit_monto")
    with col3:
        st.write("")
        st.write("")
        if st.button("💾 Aplicar", key="edit_btn", use_container_width=True):
            r = cambio_presupuesto_completo(ad_id_sel, nuevo)
            if r["ok"] and r.get("solo_local"):
                st.warning(f"Registrado localmente ({r.get('motivo')}). Nuevo período iniciado.")
            elif r["ok"]:
                st.success("✅ Presupuesto actualizado en Facebook Ads — nuevo período iniciado")
            else:
                st.error(f"❌ Facebook rechazó el cambio: {r['error']}\n\nEl período NO se cerró.")
            time.sleep(1.0)
            st.rerun()


# --------------------------------------------------------------------------- #
#  Render de la tabla rica (HTML)
# --------------------------------------------------------------------------- #
_TABLA_CSS = """
<style>
.tbl-wrap { overflow-x:auto; border:1px solid #1f2a37; border-radius:10px; }
table.ads { width:100%; border-collapse:collapse; font-size:12px;
            color:#e5e7eb; background:#0b1220; min-width:1180px; }
table.ads th { text-align:left; font-weight:600; color:#93a4b8; font-size:10.5px;
               text-transform:uppercase; letter-spacing:.03em; padding:9px 10px;
               border-bottom:1px solid #1f2a37; white-space:nowrap; }
table.ads td { padding:9px 10px; border-bottom:1px solid #141d2b; vertical-align:middle; }
table.ads tr:hover td { background:#0f1830; }
.big { font-size:12.5px; font-weight:600; color:#f3f4f6; }
.sub { font-size:10.5px; color:#8b9bb0; }
.pill { display:inline-flex; align-items:center; gap:5px; padding:2px 8px;
        border-radius:999px; font-size:10.5px; font-weight:600; }
.pill-run { background:#0f2a1a; color:#4ade80; }
.pill-off { background:#2a2f39; color:#9ca3af; }
.dot { width:7px; height:7px; border-radius:50%; display:inline-block; }
.tag { font-size:9.5px; color:#6b7a90; border:1px solid #263344; border-radius:4px;
       padding:0 4px; margin-left:4px; }
.badge { display:inline-block; padding:1px 7px; border-radius:5px; font-size:10px;
         font-weight:700; color:#0b1220; }
.bar { height:5px; background:#1f2a37; border-radius:4px; margin-top:4px; overflow:hidden; }
.bar > span { display:block; height:100%; background:#3b82f6; }
.chip { display:inline-block; background:#0f2a1a; color:#4ade80; border-radius:5px;
        padding:1px 7px; font-size:10px; font-weight:600; }
.up { color:#4ade80; } .down { color:#f87171; } .flat { color:#9ca3af; }
</style>
"""


def _c_estado(f):
    if f["activo"]:
        return ('<span class="pill pill-run"><span class="dot" style="background:#4ade80"></span>'
                'Corriendo</span>')
    return ('<span class="pill pill-off"><span class="dot" style="background:#9ca3af"></span>'
            'Pausado</span>')


def _c_presupuesto(f):
    return (f'<div class="big">{_usd(f["presupuesto"])}</div>'
            f'<div class="sub">ABO</div>')


def _c_gasto(f):
    if f["spend"] is None:
        return (f'<div class="big">{_usd(f["gasto"])}</div>'
                f'<div class="sub">estimado</div>')
    pct = f["pct"]
    pct_txt = f"{pct:.0f}%" if pct is not None else "—"
    ancho = min(100, max(0, pct)) if pct is not None else 0
    return (f'<div class="big">{_usd(f["spend"])}</div>'
            f'<div class="sub">{pct_txt}</div>'
            f'<div class="bar"><span style="width:{ancho:.0f}%"></span></div>')


def _c_cpm_ctr(f):
    cpm = _usd(f["cpm"]) if f["cpm"] is not None else "—"
    ctr = f'{f["ctr"]:.2f}% CTR' if f["ctr"] is not None else "— CTR"
    return f'<div class="big">{cpm} <span class="sub">CPM</span></div><div class="sub">{ctr}</div>'


def _c_ventas(f):
    return (f'<div class="big">{_usd(f["val_venta"])} <span class="sub">/ venta</span></div>'
            f'<div class="sub">{_usd(f["ingresos"])} ingresos</div>'
            f'<div class="chip">{f["num"]} venta{"s" if f["num"]!=1 else ""}</div>')


def _c_ganancia(f):
    g = f["ganancia"]
    cls = "up" if g >= 0 else "down"
    signo = "+" if g >= 0 else ""
    return f'<div class="big {cls}">{signo}{_usd(g)}</div>'


def _c_roas(f):
    col = _roas_color(f["roas"])
    return f'<div class="big" style="color:{col};font-size:14px">{f["roas"]:.2f}x</div>'


def _c_salud(f):
    b = f["badge"]
    spark = _spark_svg(f["serie"], color=b["color"])
    return (f'{spark}'
            f'<div><span class="badge" style="background:{b["color"]}">{b["texto"]}</span></div>')


def _c_conv(f):
    if f["conv"] is None:
        return '<div class="big">—</div>'
    cc = _usd(f["costo_conv"]) if f["costo_conv"] is not None else "—"
    return (f'<div class="big">{int(f["conv"])}</div>'
            f'<div class="sub">{cc} / conv.</div>')


def _c_costo_conv(f):
    if f.get("costo_conv") is None:
        return '<div class="big">—</div>'
    return f'<div class="big">{_usd(f["costo_conv"])}</div><div class="sub">por conversación</div>'


def _c_accion(f):
    ac = f["accion"]
    cls = {"up": "up", "down": "down"}.get(ac["direccion"], "flat")
    hace = f'<div class="sub">{_html.escape(ac["hace"])}</div>' if ac["hace"] else ""
    return f'<div class="{cls}" style="font-weight:600">{_html.escape(ac["texto"])}</div>{hace}'


def _render_tabla(filas):
    cols = ["Anuncio", "Cuenta", "Entrega", "Creado", "Presupuesto", "Gasto / Presup.",
            "CPM / CTR", "Ventas", "Ganancia", "ROAS ↓", "Salud 7d",
            "Conversaciones", "Costo por conversación", "Últimas acciones"]
    ths = "".join(f"<th>{c}</th>" for c in cols)
    trs = []
    for f in filas:
        nombre = _html.escape(str(f["a"]["nombre"]))[:60]
        cuenta = _html.escape(str(f["a"].get("cuenta_nombre") or "—"))[:34]
        celdas = [
            f'<div class="big">{nombre}</div><div class="sub">{f["ad_id"]}</div>',
            f'<div class="sub">{cuenta}</div>',
            _c_estado(f),
            f'<div class="sub">{_fecha_corta(f["a"].get("fecha_creacion"))}</div>',
            _c_presupuesto(f),
            _c_gasto(f),
            _c_cpm_ctr(f),
            _c_ventas(f),
            _c_ganancia(f),
            _c_roas(f),
            _c_salud(f),
            _c_conv(f),
            _c_costo_conv(f),
            _c_accion(f),
        ]
        tds = "".join(f"<td>{c}</td>" for c in celdas)
        trs.append(f"<tr>{tds}</tr>")
    return (f'<div class="tbl-wrap"><table class="ads"><thead><tr>{ths}</tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table></div>')


# --------------------------------------------------------------------------- #
#  Sección: Cambios rápidos de presupuesto en lote
# --------------------------------------------------------------------------- #
def seccion_lote():
    st.header("⚡ Cambios rápidos de presupuesto en lote")
    ahora = db.ahora()
    anuncios = db.obtener_anuncios(solo_activos=True)
    if not anuncios:
        st.info("No hay anuncios activos.")
        return

    filas = []
    for a in anuncios:
        m = calculos.metricas_periodo_actual(a["ad_id"], ahora)
        actual = m["presupuesto"] if m else 0.0
        filas.append({
            "ad_id": a["ad_id"],
            "Anuncio": a["nombre"],
            "Presupuesto actual": float(actual or 0.0),
            "Nuevo presupuesto": float(actual or 0.0),
        })
    df = pd.DataFrame(filas)

    editado = st.data_editor(
        df,
        use_container_width=True, hide_index=True, key="editor_lote",
        column_config={
            "ad_id": None,  # oculto
            "Anuncio": st.column_config.TextColumn(disabled=True),
            "Presupuesto actual": st.column_config.NumberColumn(format="$%.2f", disabled=True),
            "Nuevo presupuesto": st.column_config.NumberColumn(format="$%.2f", min_value=0.0),
        },
    )

    if st.button("🚀 Aplicar todos los cambios", type="primary"):
        cambios = editado[abs(editado["Nuevo presupuesto"] - editado["Presupuesto actual"]) > 0.01]
        if cambios.empty:
            st.info("No hay cambios que aplicar.")
        else:
            resultados = []
            for _, fila in cambios.iterrows():
                r = cambio_presupuesto_completo(fila["ad_id"], float(fila["Nuevo presupuesto"]))
                resultados.append({
                    "Anuncio": fila["Anuncio"],
                    "Nuevo": _fmt_money(fila["Nuevo presupuesto"]),
                    "Resultado": ("✅ OK" if r["ok"] and not r.get("solo_local")
                                  else ("⚠️ Solo local" if r.get("solo_local") else f"❌ {r['error']}")),
                })
            st.dataframe(pd.DataFrame(resultados), use_container_width=True, hide_index=True)
            time.sleep(1.0)


# --------------------------------------------------------------------------- #
#  Registro rápido de venta manual (útil en despliegue online)
# --------------------------------------------------------------------------- #
def seccion_venta_manual():
    st.header("🧾 Registrar venta")
    st.caption("Registra una venta al instante (se atribuye al período de presupuesto "
               "activo del anuncio). Alternativa al Excel para uso online.")
    anuncios = db.obtener_anuncios(solo_activos=False)
    opciones = {f"{a['nombre']}  ·  {a['ad_id']}": a["ad_id"] for a in anuncios}

    with st.form("form_venta_manual"):
        c1, c2, c3 = st.columns([3, 2, 2])
        with c1:
            if opciones:
                modo_libre = st.checkbox("Escribir ID manualmente", value=False)
                if modo_libre:
                    ad_id = st.text_input("ID_Anuncio")
                else:
                    sel = st.selectbox("Anuncio", list(opciones.keys()))
                    ad_id = opciones[sel]
            else:
                ad_id = st.text_input("ID_Anuncio (aún no hay anuncios cargados)")
        with c2:
            valor = st.number_input("Valor de la venta", min_value=0.0, value=0.0, step=1.0)
        with c3:
            usar_ahora = st.toggle("Usar hora actual", value=True)
        hora = None
        if not usar_ahora:
            cf1, cf2 = st.columns(2)
            with cf1:
                f = st.date_input("Fecha")
            with cf2:
                h = st.time_input("Hora")
            hora = datetime.combine(f, h)
        enviado = st.form_submit_button("Registrar venta", type="primary")

    if enviado:
        r = watcher.registrar_venta_manual(ad_id, valor, hora=hora)
        if r["ok"]:
            destino = f"período {r['periodo_id']}" if r["periodo_id"] else "sin período (no había presupuesto activo a esa hora)"
            st.success(f"✅ Venta registrada y atribuida a {destino}.")
        else:
            st.error(f"❌ {r['error']}")
        time.sleep(0.8)
        st.rerun()


# --------------------------------------------------------------------------- #
#  Sección 2 — Detalle por anuncio
# --------------------------------------------------------------------------- #
def seccion_detalle():
    st.header("2 · Detalle por anuncio")
    anuncios = db.obtener_anuncios(solo_activos=False)
    if not anuncios:
        st.info("Aún no hay anuncios en la base de datos.")
        return

    opciones = {f"{a['nombre']}  ·  {a['ad_id']}": a["ad_id"] for a in anuncios}
    sel = st.selectbox("Selecciona un anuncio", list(opciones.keys()), key="detalle_sel")
    ad_id = opciones[sel]
    anuncio = db.obtener_anuncio(ad_id)
    ahora = db.ahora()

    metricas = calculos.metricas_todos_los_periodos(ad_id, ahora)
    if not metricas:
        st.warning("Este anuncio no tiene períodos registrados todavía.")
    else:
        # Tabla de todos los períodos
        df = pd.DataFrame([{
            "Período": m["periodo_id"],
            "Estado": m["estado"],
            "Presupuesto": m["presupuesto"],
            "Inicio": db.a_texto(m["hora_inicio"]) if m["hora_inicio"] else "—",
            "Fin": db.a_texto(m["hora_fin"]) if m["hora_fin"] else "— (abierto)",
            "Duración (min)": m["duracion_minutos"],
            "Ventas": m["num_ventas"],
            "Ingreso": m["ingreso_total"],
            "Gasto estimado": m["gasto_estimado"],
            "ROAS": m["roas"],
            "Costo/venta": m["costo_por_venta"],
        } for m in metricas])

        st.subheader("Períodos del día")
        st.dataframe(
            df, use_container_width=True, hide_index=True,
            column_config={
                "Presupuesto": st.column_config.NumberColumn(format="$%.2f"),
                "Ingreso": st.column_config.NumberColumn(format="$%.2f"),
                "Gasto estimado": st.column_config.NumberColumn(format="$%.2f"),
                "ROAS": st.column_config.NumberColumn(format="%.2f x"),
                "Costo/venta": st.column_config.NumberColumn(format="$%.2f"),
            },
        )

        c1, c2 = st.columns(2)

        # Gráfica de barras ROAS por período (Plotly)
        with c1:
            st.subheader("ROAS por período")
            df_bar = df.copy()
            df_bar["Etiqueta"] = df_bar["Período"].astype(str) + " ($" + df_bar["Presupuesto"].round(0).astype(int).astype(str) + ")"
            colores = ["#2ecc71" if r > 2 else "#f1c40f" if r >= 1 else "#e74c3c" for r in df_bar["ROAS"]]
            fig = go.Figure(go.Bar(x=df_bar["Etiqueta"], y=df_bar["ROAS"],
                                   marker_color=colores, text=df_bar["ROAS"].round(2)))
            fig.add_hline(y=1, line_dash="dot", line_color="gray")
            fig.add_hline(y=2, line_dash="dot", line_color="green")
            fig.update_layout(xaxis_title="Período (presupuesto)", yaxis_title="ROAS",
                              height=380, margin=dict(t=20, b=20))
            st.plotly_chart(fig, use_container_width=True)

        # Línea de tiempo de cambios de presupuesto
        with c2:
            st.subheader("Línea de tiempo de presupuesto")
            fig2 = go.Figure()
            xs, ys, textos = [], [], []
            for m in metricas:
                if not m["hora_inicio"]:
                    continue
                fin = m["hora_fin"] or ahora
                xs += [m["hora_inicio"], fin, None]
                ys += [m["presupuesto"], m["presupuesto"], None]
            fig2.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line=dict(width=3, color="#3498db"),
                                      name="Presupuesto"))
            # Marcadores en cada cambio (inicio de período)
            mx = [m["hora_inicio"] for m in metricas if m["hora_inicio"]]
            my = [m["presupuesto"] for m in metricas if m["hora_inicio"]]
            mt = [f"${m['presupuesto']:.0f}" for m in metricas if m["hora_inicio"]]
            fig2.add_trace(go.Scatter(x=mx, y=my, mode="markers+text", text=mt,
                                      textposition="top center",
                                      marker=dict(size=12, color="#e67e22"), name="Cambio"))
            fig2.update_layout(xaxis_title="Hora", yaxis_title="Presupuesto diario ($)",
                               height=380, margin=dict(t=20, b=20), showlegend=False)
            st.plotly_chart(fig2, use_container_width=True)

    st.divider()
    _bloque_duplicar(ad_id, anuncio, ahora)


# --------------------------------------------------------------------------- #
#  Parte 7 — Duplicación de anuncios
# --------------------------------------------------------------------------- #
def _bloque_duplicar(ad_id, anuncio, ahora):
    st.subheader("⚡ Duplicar anuncio")
    m = calculos.metricas_periodo_actual(ad_id, ahora)
    presup_def = m["presupuesto"] if m else 0.0

    with st.expander("Abrir formulario de duplicación"):
        with st.form(f"form_dup_{ad_id}"):
            c1, c2, c3 = st.columns(3)
            with c1:
                n = st.number_input("Número de copias", min_value=1, max_value=10, value=4, step=1)
            with c2:
                presup = st.number_input("Presupuesto por copia", min_value=0.0,
                                         value=float(presup_def or 0.0), step=1.0)
            with c3:
                activar = st.toggle("Activar copias inmediatamente", value=False)
                st.caption("Si está apagado, las copias se crean en PAUSED.")

            enviado = st.form_submit_button("⚡ Duplicar ahora", type="primary")

        if enviado:
            hay_conexion = bool(fb._conexiones_efectivas()) if fb.SDK_DISPONIBLE else False
            if not hay_conexion:
                st.error("No hay conexión de Facebook disponible. Agrega un token en "
                         "**Conexiones** o revisa el .env. No se puede duplicar.")
                return
            with st.spinner(f"Duplicando {n} veces en Facebook..."):
                res = fb.duplicar_anuncio(
                    ad_id, int(n), float(presup), activar=bool(activar),
                    conexion_id=anuncio.get("conexion_id") if anuncio else None,
                    cuenta_id=anuncio.get("cuenta_id") if anuncio else None,
                    cuenta_nombre=anuncio.get("cuenta_nombre") if anuncio else None)

            if res.get("error_global"):
                st.error(f"❌ {res['error_global']}")

            exitosas = res.get("exitosas", [])
            fallidas = res.get("fallidas", [])

            if exitosas:
                st.success(f"✅ {len(exitosas)} copia(s) creada(s) correctamente.")
                st.dataframe(
                    pd.DataFrame([{"Nombre": e["nombre"], "Nuevo ad_id": e["ad_id"],
                                   "adset_id": e.get("adset_id"), "Presupuesto": _fmt_money(e["presupuesto"])}
                                  for e in exitosas]),
                    use_container_width=True, hide_index=True,
                )
            if fallidas:
                st.error(f"⚠️ {len(fallidas)} copia(s) fallaron:")
                for f in fallidas:
                    st.write(f"- **{f['nombre']}**: {f['error']}")
            if not exitosas and not fallidas and not res.get("error_global"):
                st.info("No se creó ninguna copia.")


# --------------------------------------------------------------------------- #
#  Sección 3 — Alertas
# --------------------------------------------------------------------------- #
def seccion_alertas():
    st.header("3 · Alertas")
    alertas = calculos.evaluar_alertas(db.ahora(), umbral_minutos=30, umbral_roas=1.5)
    if not alertas:
        st.success("Sin alertas. Ningún anuncio lleva +30 min con ROAS < 1.5x.")
        return
    for a in alertas:
        st.error(
            f"🚨 **{a['nombre']}** — presupuesto {_fmt_money(a['presupuesto'])}, "
            f"lleva **{a['antiguedad']}** en este presupuesto y su ROAS actual es "
            f"**{a['roas']:.2f}x** ({a['num_ventas']} ventas). Considera ajustar."
        )


# --------------------------------------------------------------------------- #
#  Sección 4 — Registro manual de cambio de presupuesto
# --------------------------------------------------------------------------- #
def seccion_manual():
    st.header("4 · Registro manual de cambio de presupuesto")
    st.caption("Envía el cambio a Facebook (si hay API) y abre un período nuevo. "
               "Si Facebook rechaza el cambio, el período NO se cierra.")
    anuncios = db.obtener_anuncios(solo_activos=False)
    if not anuncios:
        st.info("No hay anuncios registrados.")
        return

    opciones = {f"{a['nombre']}  ·  {a['ad_id']}": a["ad_id"] for a in anuncios}
    with st.form("form_manual"):
        sel = st.selectbox("Anuncio", list(opciones.keys()))
        monto = st.number_input("Nuevo monto (presupuesto diario)", min_value=0.0, value=0.0, step=1.0)
        enviado = st.form_submit_button("Registrar cambio", type="primary")

    if enviado:
        ad_id = opciones[sel]
        r = cambio_presupuesto_completo(ad_id, float(monto))
        if r["ok"] and r.get("solo_local"):
            st.warning(f"⚠️ Registrado solo localmente ({r.get('motivo')}). Nuevo período iniciado.")
        elif r["ok"]:
            st.success("✅ Presupuesto actualizado en Facebook Ads — nuevo período iniciado")
        else:
            st.error(f"❌ Facebook rechazó el cambio: {r['error']}\n\nEl período NO se cerró.")
        time.sleep(1.0)
        st.rerun()


# --------------------------------------------------------------------------- #
#  Candado de contraseña
# --------------------------------------------------------------------------- #
def _gate_password():
    if not config.APP_PASSWORD:
        return
    if st.session_state.get("_auth_ok"):
        return
    st.title("🔒 Acceso")
    st.caption("Esta app puede modificar presupuestos reales. Ingresa la contraseña.")
    pwd = st.text_input("Contraseña", type="password", key="_pwd")
    if st.button("Entrar"):
        if pwd == config.APP_PASSWORD:
            st.session_state["_auth_ok"] = True
            st.rerun()
        else:
            st.error("Contraseña incorrecta.")
    st.stop()


# --------------------------------------------------------------------------- #
#  Sección — Conexiones (multi-Business / tokens)
# --------------------------------------------------------------------------- #
def seccion_conexiones():
    st.header("🔌 Conexiones (Business / tokens)")
    st.caption("Agrega un token de **Usuario del Sistema** por cada Business. La app "
               "descubre sus cuentas automáticamente. Los tokens se guardan solo en "
               "este servidor (volumen /data), nunca en el repositorio.")

    cons = db.obtener_conexiones()
    if cons:
        for c in cons:
            with st.container(border=True):
                col = st.columns([4, 2, 2, 2])
                tok = c.get("token") or ""
                col[0].markdown(f"**{c.get('alias') or 'Conexión'}**  ·  id {c['id']}")
                col[0].caption(f"token …{tok[-6:]}" + ("  ·  app propia" if c.get("app_id") else ""))
                if c.get("ultimo_error"):
                    col[0].caption(f"⚠️ {c['ultimo_error']}")
                col[1].write("🟢 activa" if c["activo"] else "⚪ inactiva")
                if col[2].button("🔎 Probar", key=f"probar_{c['id']}"):
                    r = fb.probar_conexion(tok, c.get("app_id"), c.get("app_secret"))
                    if r["ok"]:
                        nombres = ", ".join(x["name"] for x in r["cuentas"][:12])
                        st.success(f"OK · {len(r['cuentas'])} cuenta(s): {nombres}")
                    else:
                        st.error(r["error"])
                if col[3].button("🗑️ Eliminar", key=f"del_{c['id']}"):
                    db.eliminar_conexion(c["id"])
                    st.rerun()
    else:
        st.info("Aún no hay conexiones guardadas. Si definiste credenciales en el .env, "
                "se usa esa única cuenta por defecto.")

    st.subheader("➕ Agregar conexión")
    with st.form("form_conexion"):
        alias = st.text_input("Alias (ej. BM Tienda MX)")
        token = st.text_area("Token de Usuario del Sistema", height=90,
                             help="Business Settings → Usuarios del sistema → Generar token")
        with st.expander("App propia (opcional, solo si no usas la app central del .env)"):
            app_id = st.text_input("APP_ID (opcional)")
            app_secret = st.text_input("APP_SECRET (opcional)", type="password")
        cc1, cc2 = st.columns(2)
        probar = cc1.form_submit_button("🔎 Probar y descubrir cuentas")
        guardar = cc2.form_submit_button("💾 Guardar conexión", type="primary")

    if probar:
        if not token.strip():
            st.error("Pega primero el token.")
        else:
            r = fb.probar_conexion(token.strip(), app_id or None, app_secret or None)
            if r["ok"]:
                st.success(f"✅ {len(r['cuentas'])} cuenta(s) encontradas:")
                st.dataframe(pd.DataFrame(r["cuentas"]), use_container_width=True, hide_index=True)
            else:
                st.error(f"❌ {r['error']}")
    if guardar:
        if not token.strip():
            st.error("Pega el token.")
        else:
            cid = db.agregar_conexion(alias or "Business", token.strip(),
                                      app_id or None, app_secret or None)
            try:
                _insights_cache.clear()
            except Exception:
                pass
            st.success(f"✅ Conexión #{cid} guardada. Pulsa **📥 Recargar anuncios de "
                       "Facebook** (barra lateral) para cargar sus cuentas.")
            time.sleep(1.0)
            st.rerun()


# --------------------------------------------------------------------------- #
#  Layout principal
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#  Tema visual (modo oscuro pulido)
# --------------------------------------------------------------------------- #
def _inject_css():
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Inter:wght@400;500;600&display=swap');
    :root{
        --bg:#111319; --surface:#1d1f26; --surface2:#282a30; --stroke:rgba(255,255,255,.06);
        --tint:#8cd2d7; --mint:#BFF2E2; --lav:#E6D5FF; --peri:#C7C4FF; --txt:#e2e2ea; --sub:#bec8c9;
    }
    html, body, [data-testid="stAppViewContainer"]{
        background:
          radial-gradient(1200px 500px at 100% -10%, rgba(191,242,226,.06), transparent 60%),
          radial-gradient(900px 500px at -10% 10%, rgba(230,213,255,.05), transparent 55%),
          var(--bg) !important;
        color:var(--txt);
    }
    /* Tipografía de texto (sin tocar los iconos, para no romperlos) */
    body, .stMarkdown, p, label, input, textarea, button, li, td, th{
        font-family:'Inter',sans-serif;
    }
    h1,h2,h3,h4,.stMarkdown h1,.stMarkdown h2,.stMarkdown h3{
        font-family:'Geist',sans-serif !important; letter-spacing:-.01em; color:var(--txt);
    }
    /* Restaurar la fuente de iconos de Material (Streamlit los usa en nav, botones, etc.) */
    [class*="material-symbols"], [class*="material-icons"], .material-icons,
    span[data-testid="stIconMaterial"], [data-testid="stIconMaterial"]{
        font-family:'Material Symbols Outlined','Material Symbols Rounded','Material Icons' !important;
    }
    [data-testid="stSidebar"]{
        background:linear-gradient(180deg,#15171d,#0f1115); border-right:1px solid var(--stroke);
    }
    /* Botones */
    .stButton>button, .stDownloadButton>button, .stFormSubmitButton>button{
        border-radius:12px; border:1px solid var(--stroke); font-weight:600;
        background:var(--surface); color:var(--txt); transition:all .15s ease;
    }
    .stButton>button:hover{ border-color:var(--tint); color:var(--mint); }
    .stButton>button[kind="primary"], .stFormSubmitButton>button[kind="primary"]{
        background:linear-gradient(180deg,#a2e9ee,#8cd2d7); color:#00373a; border:none;
        box-shadow:0 4px 18px rgba(140,210,215,.18);
    }
    /* Inputs */
    [data-baseweb="input"] input, [data-baseweb="textarea"] textarea, .stTextInput input,
    .stNumberInput input, [data-baseweb="select"]>div{
        border-radius:10px !important;
    }
    /* Tarjetas / contenedores con borde y expanders */
    [data-testid="stExpander"], div[data-testid="stVerticalBlockBorderWrapper"]{
        background:rgba(29,31,38,.55); backdrop-filter:blur(14px);
        border:1px solid var(--stroke) !important; border-radius:16px;
    }
    /* Tabs */
    .stTabs [data-baseweb="tab-list"]{ gap:6px; }
    .stTabs [data-baseweb="tab"]{ border-radius:10px 10px 0 0; }
    /* Métricas */
    [data-testid="stMetricValue"]{ font-family:'Geist',sans-serif; color:var(--mint); }
    /* Dividers más sutiles */
    hr{ border-color:var(--stroke) !important; }
    </style>
    """, unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
#  Página: Configuración (Excel + Cuentas + Conexiones + Supabase)
# --------------------------------------------------------------------------- #
def _panel_excel():
    st.subheader("📄 Excel (ventas del socio)")
    st.caption("Fuente 1: el Excel con columnas ID_Anuncio, Valor_Venta, Hora_Venta. "
               "Súbelo aquí cada vez que se agreguen ventas; solo se procesan las nuevas.")
    subido = st.file_uploader("Subir / reemplazar ventas.xlsx", type=["xlsx"], key="up_excel_cfg")
    c1, c2 = st.columns(2)
    if subido is not None and c1.button("Procesar Excel subido", use_container_width=True):
        n = watcher.guardar_excel_subido(subido.getvalue(), reemplazar=True)
        st.success(f"{n} venta(s) nueva(s) procesada(s).")
    if c2.button("📄 Reimportar TODO el Excel", use_container_width=True,
                 help="Reprocesa todas las filas (útil la primera vez)."):
        n = watcher.importar_todo()
        st.success(f"{n} ventas importadas.")


def _panel_supabase():
    st.subheader("🟩 Supabase (tus ventas)")
    st.caption("Fuente 2: una tabla de Supabase con tus ventas. Se une con el Excel en la "
               "misma tabla de ventas. Las credenciales van en variables de entorno "
               "(SUPABASE_URL / SUPABASE_KEY).")
    if not config.supabase_configurado():
        st.warning("Faltan **SUPABASE_URL** y **SUPABASE_KEY** en las variables de entorno "
                   "de EasyPanel. Agrégalas y redespliega.")
    else:
        st.success("Credenciales de Supabase detectadas ✅")

    m = supa.get_mapeo()
    with st.form("form_supabase"):
        tabla = st.text_input("Nombre de la tabla", value=m[supa.K_TABLA])
        c1, c2, c3 = st.columns(3)
        col_ad = c1.text_input("Columna ID del anuncio", value=m[supa.K_ADID])
        col_val = c2.text_input("Columna valor de venta", value=m[supa.K_VALOR])
        col_hora = c3.text_input("Columna fecha/hora", value=m[supa.K_HORA])
        c4, c5 = st.columns(2)
        col_prod = c4.text_input("Columna producto (opcional)", value=m[supa.K_PRODUCTO])
        col_id = c5.text_input("Columna id único (dedup)", value=m[supa.K_ID])
        gcol1, gcol2 = st.columns(2)
        probar = gcol1.form_submit_button("🔎 Probar y ver columnas")
        guardar = gcol2.form_submit_button("💾 Guardar mapeo", type="primary")

    if guardar:
        supa.guardar_mapeo(tabla, col_ad, col_val, col_hora, col_prod, col_id)
        st.success("Mapeo guardado.")
    if probar:
        supa.guardar_mapeo(tabla, col_ad, col_val, col_hora, col_prod, col_id)
        r = supa.probar_conexion()
        if r["ok"]:
            st.success(f"Conectado ✅ Columnas de tu tabla: {', '.join(r['columnas'])}")
            if r["muestra"]:
                st.dataframe(pd.DataFrame(r["muestra"]), use_container_width=True, hide_index=True)
        else:
            st.error(f"❌ {r['error']}")

    if st.button("🔄 Sincronizar ventas de Supabase ahora", type="primary"):
        r = supa.sincronizar()
        if r["ok"]:
            msg = f"✅ {r['insertadas']} venta(s) importada(s) de Supabase."
            if r["sin_periodo"]:
                msg += f" ({r['sin_periodo']} sin período/anuncio activo a esa hora)."
            st.success(msg)
        else:
            st.error(f"❌ {r['error']}")


def _panel_cuentas():
    st.subheader("🗂️ Cuentas publicitarias detectadas")
    todos = db.obtener_anuncios(solo_activos=False)
    if not todos:
        st.info("Aún no hay cuentas. Agrega una conexión y pulsa **Recargar** en la barra lateral.")
        return
    filas = {}
    for a in todos:
        c = a.get("cuenta_nombre") or "—"
        if c not in filas:
            filas[c] = {"Cuenta": c, "País": a.get("cuenta_pais") or "—",
                        "Anuncios": 0, "Activos": 0}
        filas[c]["Anuncios"] += 1
        if _es_activo(a):
            filas[c]["Activos"] += 1
    st.dataframe(pd.DataFrame(list(filas.values())),
                 use_container_width=True, hide_index=True)


def pagina_configuracion():
    st.title("⚙️ Configuración")
    st.caption("Conecta tus Business, tus fuentes de ventas (Excel + Supabase) y revisa "
               "tus cuentas.")
    tabs = st.tabs(["🔌 Conexiones", "📄 Excel", "🟩 Supabase", "🗂️ Cuentas"])
    with tabs[0]:
        seccion_conexiones()
    with tabs[1]:
        _panel_excel()
    with tabs[2]:
        _panel_supabase()
    with tabs[3]:
        _panel_cuentas()


# --------------------------------------------------------------------------- #
#  Página: Dashboard
# --------------------------------------------------------------------------- #
def pagina_dashboard():
    top1, top2 = st.columns([5, 1])
    with top1:
        st.title("📊 Dashboard")
        st.caption(f"Última actualización: {db.a_texto(db.ahora())}")
    with top2:
        st.write("")
        st.write("")
        if st.button("🔄 Actualizar", use_container_width=True, type="primary"):
            try:
                _insights_cache.clear()
            except Exception:
                pass
            st.rerun()

    seccion_vista_general()
    st.divider()
    seccion_alertas()
    st.divider()
    seccion_venta_manual()
    st.divider()
    seccion_detalle()
    st.divider()
    seccion_lote()
    st.divider()
    seccion_manual()


# --------------------------------------------------------------------------- #
#  Entrada principal (navegación por páginas)
# --------------------------------------------------------------------------- #
def main():
    _gate_password()
    _inject_css()
    sidebar_estado()
    nav = st.navigation([
        st.Page(pagina_dashboard, title="Dashboard", icon=":material/dashboard:", default=True),
        st.Page(pagina_configuracion, title="Configuración", icon=":material/settings:"),
    ])
    nav.run()


main()
