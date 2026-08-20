"""
app.py
Dashboard de atribución de ventas a Facebook Ads con seguimiento por período
de presupuesto.

Ejecutar con:
    streamlit run app.py
"""
import time
from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import config
import db
import calculos
import facebook_api as fb
import excel_watcher as watcher

st.set_page_config(page_title="Atribución Facebook Ads", page_icon="📊", layout="wide")


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
#  Auto-refresco cada 2 minutos (recarga la página)
# --------------------------------------------------------------------------- #
def auto_refresco(segundos: int = 120):
    st.components.v1.html(
        f"""
        <script>
            setTimeout(function() {{
                window.parent.location.reload();
            }}, {segundos * 1000});
        </script>
        """,
        height=0,
    )


# --------------------------------------------------------------------------- #
#  Helpers de UI
# --------------------------------------------------------------------------- #
def _fmt_money(v):
    try:
        return f"${v:,.2f}"
    except Exception:
        return str(v)


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
    estado_api = fb.obtener_estado()

    # Si la API está disponible, intentamos el cambio en Facebook.
    if fb.SDK_DISPONIBLE and config.facebook_configurado() and adset_id:
        r = fb.actualizar_presupuesto_facebook(adset_id, nuevo_monto)
        if not r["ok"]:
            # NO cerramos el período en SQLite.
            return {"ok": False, "error": r["error"], "solo_local": False}
        db.cambiar_periodo(ad_id, nuevo_monto)
        return {"ok": True, "error": None, "solo_local": False}

    # Sin API disponible: registro local solamente (advertencia).
    db.cambiar_periodo(ad_id, nuevo_monto)
    motivo = "sin adset_id" if not adset_id else "API de Facebook no disponible"
    return {"ok": True, "error": None, "solo_local": True, "motivo": motivo}


# --------------------------------------------------------------------------- #
#  Barra lateral: estado de servicios
# --------------------------------------------------------------------------- #
def sidebar_estado():
    st.sidebar.header("⚙️ Estado del sistema")

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
    st.sidebar.caption(f"Anuncios activos: {est_fb.get('num_anuncios', 0)}")

    # Watcher
    if est_wt["activo"]:
        st.sidebar.success("Watcher Excel: activo")
    else:
        st.sidebar.warning("Watcher Excel: inactivo")
    if est_wt.get("ultimo_error"):
        st.sidebar.caption(f"⚠️ {est_wt['ultimo_error']}")
    st.sidebar.caption(f"Ventas procesadas (sesión): {est_wt.get('ventas_procesadas', 0)}")

    st.sidebar.divider()
    if st.sidebar.button("🔄 Actualizar ahora", use_container_width=True):
        st.rerun()

    if st.sidebar.button("📥 Recargar anuncios de Facebook", use_container_width=True):
        with st.spinner("Consultando Facebook..."):
            r = fb.cargar_anuncios_activos()
        if r["ok"]:
            st.sidebar.success(f"{len(r['anuncios'])} anuncios cargados.")
        else:
            st.sidebar.error(f"Error: {r['error']}")
        st.rerun()

    if st.sidebar.button("📄 Importar TODO el Excel", use_container_width=True,
                         help="Reprocesa todas las filas del Excel (útil la primera vez)."):
        n = watcher.importar_todo()
        st.sidebar.success(f"{n} ventas importadas.")
        st.rerun()

    st.sidebar.divider()
    st.sidebar.subheader("⬆️ Subir ventas.xlsx (modo online)")
    subido = st.sidebar.file_uploader(
        "Sube o reemplaza el Excel", type=["xlsx"], key="uploader_excel",
        help="En la nube no hay un Excel local que vigilar: sube aquí tu archivo "
             "cada vez que agregues ventas. Solo se procesan las filas nuevas.")
    if subido is not None:
        if st.sidebar.button("Procesar Excel subido", use_container_width=True):
            n = watcher.guardar_excel_subido(subido.getvalue(), reemplazar=True)
            st.sidebar.success(f"{n} venta(s) nueva(s) procesada(s).")
            st.rerun()

    with st.sidebar.expander("🔧 Configuración (.env)"):
        st.json(config.resumen_config())

    with st.sidebar.expander("📋 Eventos recientes"):
        for m in (est_fb.get("mensajes", []) + est_wt.get("mensajes", []))[:15]:
            st.caption(m)


# --------------------------------------------------------------------------- #
#  Sección 1 — Vista general de anuncios activos
# --------------------------------------------------------------------------- #
def seccion_vista_general():
    st.header("1 · Vista general de anuncios activos")
    ahora = db.ahora()
    anuncios = db.obtener_anuncios(solo_activos=True)

    if not anuncios:
        st.info("No hay anuncios activos. Usa **Recargar anuncios de Facebook** en la barra lateral "
                "o registra cambios manuales más abajo.")
        return

    filas = []
    for a in anuncios:
        m = calculos.metricas_periodo_actual(a["ad_id"], ahora)
        if m:
            filas.append({
                "🚦": calculos.color_roas(m["roas"]),
                "Anuncio": a["nombre"],
                "ad_id": a["ad_id"],
                "Presupuesto": m["presupuesto"],
                "Tiempo en este presupuesto": calculos.formato_antiguedad(m["duracion_minutos"]),
                "Ventas (período actual)": m["num_ventas"],
                "Ingreso (período)": m["ingreso_total"],
                "ROAS (período actual)": m["roas"],
            })
        else:
            filas.append({
                "🚦": "⚪", "Anuncio": a["nombre"], "ad_id": a["ad_id"],
                "Presupuesto": None, "Tiempo en este presupuesto": "—",
                "Ventas (período actual)": 0, "Ingreso (período)": 0.0,
                "ROAS (período actual)": 0.0,
            })

    df = pd.DataFrame(filas)
    st.dataframe(
        df.drop(columns=["ad_id"]),
        use_container_width=True, hide_index=True,
        column_config={
            "Presupuesto": st.column_config.NumberColumn(format="$%.2f"),
            "Ingreso (período)": st.column_config.NumberColumn(format="$%.2f"),
            "ROAS (período actual)": st.column_config.NumberColumn(format="%.2f x"),
        },
    )

    # --- Opción A: editar presupuesto con lápiz (por anuncio) ---
    st.subheader("✏️ Cambiar presupuesto de un anuncio (envía a Facebook)")
    col1, col2, col3 = st.columns([3, 2, 2])
    opciones = {f"{a['nombre']}  ·  {a['ad_id']}": a["ad_id"] for a in anuncios}
    with col1:
        sel = st.selectbox("Anuncio", list(opciones.keys()), key="edit_sel")
    ad_id_sel = opciones[sel]
    m_actual = calculos.metricas_periodo_actual(ad_id_sel, ahora)
    presup_actual = m_actual["presupuesto"] if m_actual else 0.0
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
            if not (fb.SDK_DISPONIBLE and config.facebook_configurado()):
                st.error("Facebook API no está disponible/configurada. No se puede duplicar.")
                return
            with st.spinner(f"Duplicando {n} veces en Facebook..."):
                res = fb.duplicar_anuncio(ad_id, int(n), float(presup), activar=bool(activar))

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
#  Layout principal
# --------------------------------------------------------------------------- #
def main():
    st.title("📊 Atribución de ventas · Facebook Ads")
    st.caption(f"Actualizado: {db.a_texto(db.ahora())} · auto-refresco cada 2 min")

    sidebar_estado()

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

    auto_refresco(120)


if __name__ == "__main__":
    main()
