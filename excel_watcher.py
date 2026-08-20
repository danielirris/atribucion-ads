"""
excel_watcher.py
Monitorea ventas.xlsx con watchdog (en un hilo aparte) y procesa filas nuevas.

- Lee TODAS las hojas del Excel.
- Columnas esperadas: ID_Anuncio, Valor_Venta, Hora_Venta.
- Si Hora_Venta viene vacía, se asigna el timestamp del momento de detección.
- Cada venta se atribuye al período de presupuesto activo del anuncio en esa hora.
- Se guarda en la tabla `ventas`.

Para no re-procesar filas ya vistas se lleva un conteo de filas procesadas por
(archivo, hoja). Solo se procesan las filas nuevas (append al final de la hoja).
"""
import os
import threading
import time
import traceback
from datetime import datetime
from typing import Optional

import pandas as pd

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_DISPONIBLE = True
except Exception as e:  # pragma: no cover
    WATCHDOG_DISPONIBLE = False
    _WATCHDOG_ERROR = str(e)
    FileSystemEventHandler = object  # fallback

import config
import db

# Estado compartido con la UI.
_ESTADO_LOCK = threading.Lock()
ESTADO = {
    "activo": False,
    "ultima_lectura": None,
    "ultimo_error": None,
    "ventas_procesadas": 0,
    "mensajes": [],
}

# Cuántas filas ya procesamos por hoja: {nombre_hoja: n_filas}
_FILAS_VISTAS = {}
_PROC_LOCK = threading.Lock()

_OBSERVER = None
_WATCHER_THREAD = None
_STOP = threading.Event()

COLS = ["ID_Anuncio", "Valor_Venta", "Hora_Venta"]


def _log(msg: str) -> None:
    with _ESTADO_LOCK:
        ts = db.a_texto(db.ahora())
        ESTADO["mensajes"].insert(0, f"[{ts}] {msg}")
        del ESTADO["mensajes"][20:]


def _set_estado(**kwargs) -> None:
    with _ESTADO_LOCK:
        ESTADO.update(kwargs)


def obtener_estado() -> dict:
    with _ESTADO_LOCK:
        return dict(ESTADO)


# --------------------------------------------------------------------------- #
#  Parseo de una fila
# --------------------------------------------------------------------------- #
def _parse_hora(valor, deteccion: datetime) -> datetime:
    """Interpreta Hora_Venta. Si viene vacía, usa el timestamp de detección."""
    if valor is None:
        return deteccion
    if isinstance(valor, datetime):
        return valor.replace(microsecond=0)
    try:
        if pd.isna(valor):
            return deteccion
    except Exception:
        pass
    txt = str(valor).strip()
    if txt == "" or txt.lower() in ("nan", "nat", "none"):
        return deteccion
    # Intentar parsear con pandas (soporta muchos formatos).
    try:
        ts = pd.to_datetime(txt, dayfirst=False, errors="coerce")
        if pd.isna(ts):
            ts = pd.to_datetime(txt, dayfirst=True, errors="coerce")
        if pd.isna(ts):
            return deteccion
        return ts.to_pydatetime().replace(microsecond=0)
    except Exception:
        return deteccion


def _parse_valor(valor) -> Optional[float]:
    try:
        if valor is None or pd.isna(valor):
            return None
    except Exception:
        pass
    try:
        # Tolerar formatos "1,234.56" o "$100".
        txt = str(valor).replace("$", "").replace(",", "").strip()
        if txt == "":
            return None
        return float(txt)
    except Exception:
        return None


def _procesar_fila(ad_id: str, valor: float, hora: datetime, hoja: str) -> bool:
    """Atribuye y guarda una venta. Devuelve True si se insertó."""
    ad_id = str(ad_id).strip()
    if not ad_id or ad_id.lower() in ("nan", "none"):
        return False

    periodo = db.periodo_para_hora(ad_id, hora)
    periodo_id = periodo["id"] if periodo else None

    db.insertar_venta(ad_id, valor, hora, periodo_id, hoja)
    if periodo_id is None:
        _log(f"Venta de {ad_id} (${valor:.2f}) guardada SIN período "
             f"(no había período de presupuesto para esa hora).")
    else:
        _log(f"Venta de {ad_id} (${valor:.2f}) atribuida al período {periodo_id}.")
    return True


# --------------------------------------------------------------------------- #
#  Lectura del Excel
# --------------------------------------------------------------------------- #
def procesar_excel(path: Optional[str] = None, solo_nuevas: bool = True) -> int:
    """
    Lee todas las hojas del Excel y procesa las filas nuevas.
    Devuelve cuántas ventas nuevas se insertaron.

    solo_nuevas=False fuerza reprocesar todo (útil para primera carga controlada).
    """
    path = path or config.EXCEL_PATH
    if not os.path.exists(path):
        _set_estado(ultimo_error=f"No existe el archivo {path}")
        return 0

    deteccion = db.ahora()
    total = 0
    try:
        # engine openpyxl para .xlsx. sheet_name=None -> dict de todas las hojas.
        hojas = pd.read_excel(path, sheet_name=None, engine="openpyxl")
    except Exception as e:
        _set_estado(ultimo_error=f"No se pudo leer el Excel: {e}")
        _log(f"Error leyendo Excel: {e}")
        return 0

    with _PROC_LOCK:
        for nombre_hoja, dff in hojas.items():
            if dff is None or dff.empty:
                continue

            # Normalizar nombres de columnas (tolerar variaciones de espacios/caps).
            dff = dff.rename(columns=lambda c: str(c).strip())
            faltan = [c for c in COLS if c not in dff.columns]
            if faltan:
                _log(f"Hoja '{nombre_hoja}' ignorada: faltan columnas {faltan}.")
                continue

            clave = f"{os.path.abspath(path)}::{nombre_hoja}"
            vistas = _FILAS_VISTAS.get(clave, 0) if solo_nuevas else 0
            n_filas = len(dff)

            if solo_nuevas and n_filas <= vistas:
                # No hay filas nuevas (o el archivo se acortó: re-sincronizamos).
                _FILAS_VISTAS[clave] = n_filas
                continue

            nuevas = dff.iloc[vistas:] if solo_nuevas else dff
            for _, fila in nuevas.iterrows():
                valor = _parse_valor(fila.get("Valor_Venta"))
                if valor is None:
                    continue
                hora = _parse_hora(fila.get("Hora_Venta"), deteccion)
                ok = _procesar_fila(fila.get("ID_Anuncio"), valor, hora, nombre_hoja)
                if ok:
                    total += 1

            _FILAS_VISTAS[clave] = n_filas

    if total:
        with _ESTADO_LOCK:
            ESTADO["ventas_procesadas"] += total
    _set_estado(ultima_lectura=deteccion, ultimo_error=None)
    if total:
        _log(f"{total} venta(s) nueva(s) procesada(s).")
    return total


# --------------------------------------------------------------------------- #
#  Handler de watchdog
# --------------------------------------------------------------------------- #
class _ExcelHandler(FileSystemEventHandler):
    def __init__(self, path: str):
        super().__init__()
        self._path = os.path.abspath(path)
        self._ultimo = 0.0

    def on_any_event(self, event):
        try:
            if getattr(event, "is_directory", False):
                return
            ruta = os.path.abspath(getattr(event, "src_path", "") or "")
            dest = os.path.abspath(getattr(event, "dest_path", "") or "")
            # Excel suele guardar con archivos temporales; reaccionamos si toca nuestro archivo.
            objetivo = os.path.basename(self._path)
            if objetivo not in (os.path.basename(ruta), os.path.basename(dest)):
                return
            # Debounce: Excel dispara varios eventos por guardado.
            ahora = time.time()
            if ahora - self._ultimo < 1.0:
                return
            self._ultimo = ahora
            time.sleep(0.5)  # esperar a que Excel termine de escribir
            procesar_excel(self._path, solo_nuevas=True)
        except Exception as e:
            _set_estado(ultimo_error=str(e))
            _log(f"Error procesando evento: {e}")


# --------------------------------------------------------------------------- #
#  Arranque / parada del watcher
# --------------------------------------------------------------------------- #
def iniciar_watcher() -> None:
    """Arranca el observer de watchdog una sola vez por proceso."""
    global _OBSERVER, _WATCHER_THREAD

    if not WATCHDOG_DISPONIBLE:
        _set_estado(activo=False, ultimo_error=f"watchdog no disponible: {_WATCHDOG_ERROR}")
        return

    if _OBSERVER is not None:
        return  # ya iniciado

    carpeta = os.path.dirname(os.path.abspath(config.EXCEL_PATH)) or "."
    os.makedirs(carpeta, exist_ok=True)

    # Sincronizar el conteo inicial de filas SIN procesar históricas de golpe:
    # marcamos como "vistas" las filas ya existentes para no duplicar.
    _sincronizar_filas_existentes()

    handler = _ExcelHandler(config.EXCEL_PATH)
    _OBSERVER = Observer()
    _OBSERVER.schedule(handler, carpeta, recursive=False)
    _OBSERVER.daemon = True
    _OBSERVER.start()
    _set_estado(activo=True, ultimo_error=None)
    _log(f"Watchdog iniciado sobre {config.EXCEL_PATH}")

    # Hilo auxiliar: un barrido periódico por si algún evento se pierde.
    def _barrido():
        while not _STOP.wait(30):
            try:
                procesar_excel(config.EXCEL_PATH, solo_nuevas=True)
            except Exception:
                pass

    _WATCHER_THREAD = threading.Thread(target=_barrido, name="excel-barrido", daemon=True)
    _WATCHER_THREAD.start()


def _sincronizar_filas_existentes() -> None:
    """
    Al iniciar, cuenta las filas existentes y las marca como vistas SIN insertarlas,
    para no duplicar ventas históricas. Si prefieres importar lo existente,
    llama procesar_excel(solo_nuevas=False) manualmente desde la UI.
    """
    path = config.EXCEL_PATH
    if not os.path.exists(path):
        return
    try:
        hojas = pd.read_excel(path, sheet_name=None, engine="openpyxl")
        with _PROC_LOCK:
            for nombre_hoja, dff in hojas.items():
                clave = f"{os.path.abspath(path)}::{nombre_hoja}"
                _FILAS_VISTAS[clave] = 0 if dff is None else len(dff)
        _log("Filas existentes sincronizadas (no se re-importan las históricas).")
    except Exception as e:
        _log(f"No se pudo sincronizar filas existentes: {e}")


def detener_watcher() -> None:
    global _OBSERVER
    _STOP.set()
    if _OBSERVER is not None:
        try:
            _OBSERVER.stop()
            _OBSERVER.join(timeout=3)
        except Exception:
            pass
        _OBSERVER = None
    _set_estado(activo=False)


def importar_todo() -> int:
    """Reprocesa TODAS las filas del Excel (ignora el conteo de vistas)."""
    with _PROC_LOCK:
        _FILAS_VISTAS.clear()
    return procesar_excel(config.EXCEL_PATH, solo_nuevas=False)


# --------------------------------------------------------------------------- #
#  Modo online: subir Excel y registrar venta manual
# --------------------------------------------------------------------------- #
def guardar_excel_subido(contenido: bytes, reemplazar: bool = True) -> int:
    """
    Guarda un Excel subido desde la UI en EXCEL_PATH y procesa sus filas.

    - reemplazar=True: sobrescribe el archivo y procesa SOLO las filas nuevas
      respecto a lo ya visto (ideal cuando subes el mismo archivo con filas
      agregadas al final).
    Devuelve cuántas ventas nuevas se insertaron.
    """
    try:
        with open(config.EXCEL_PATH, "wb") as f:
            f.write(contenido)
    except Exception as e:
        _set_estado(ultimo_error=f"No se pudo guardar el Excel subido: {e}")
        _log(f"Error guardando Excel subido: {e}")
        return 0
    _log("Excel subido guardado. Procesando filas nuevas...")
    return procesar_excel(config.EXCEL_PATH, solo_nuevas=True)


def registrar_venta_manual(ad_id: str, valor: float,
                           hora: Optional[datetime] = None,
                           hoja_origen: str = "Manual") -> dict:
    """
    Registra una venta escrita a mano en la UI (útil en despliegue online,
    donde no siempre hay un Excel local que vigilar). Reutiliza la misma
    atribución por período. Devuelve {"ok": bool, "periodo_id": int|None, "error": str|None}.
    """
    ad_id = str(ad_id).strip()
    if not ad_id:
        return {"ok": False, "periodo_id": None, "error": "ID de anuncio vacío."}
    try:
        valor = float(valor)
    except Exception:
        return {"ok": False, "periodo_id": None, "error": "Valor de venta inválido."}

    h = hora or db.ahora()
    periodo = db.periodo_para_hora(ad_id, h)
    periodo_id = periodo["id"] if periodo else None
    db.insertar_venta(ad_id, valor, h, periodo_id, hoja_origen)
    with _ESTADO_LOCK:
        ESTADO["ventas_procesadas"] += 1
    _log(f"Venta manual de {ad_id} (${valor:.2f}) registrada "
         f"({'período ' + str(periodo_id) if periodo_id else 'sin período'}).")
    return {"ok": True, "periodo_id": periodo_id, "error": None}
