"""
crear_excel_ejemplo.py
Utilidad opcional: genera un ventas.xlsx de ejemplo con varias hojas y las
columnas correctas, para probar el monitoreo del Excel.

Uso:
    python crear_excel_ejemplo.py
"""
import pandas as pd

import config


def main():
    hoja1 = pd.DataFrame({
        "ID_Anuncio": ["1234567890", "1234567890", "9876543210"],
        "Valor_Venta": [250.0, 180.5, 99.9],
        # La primera fila deja Hora_Venta vacía -> la app le pondrá el timestamp de detección
        "Hora_Venta": ["", "2026-08-20 10:15:00", "2026-08-20 11:02:00"],
    })
    hoja2 = pd.DataFrame({
        "ID_Anuncio": ["9876543210"],
        "Valor_Venta": [320.0],
        "Hora_Venta": [""],
    })

    with pd.ExcelWriter(config.EXCEL_PATH, engine="openpyxl") as writer:
        hoja1.to_excel(writer, sheet_name="Vendedor_A", index=False)
        hoja2.to_excel(writer, sheet_name="Vendedor_B", index=False)

    print(f"Excel de ejemplo creado en: {config.EXCEL_PATH}")


if __name__ == "__main__":
    main()
