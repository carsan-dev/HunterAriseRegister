import streamlit as st
import pandas as pd
import os
import zipfile
from datetime import date

CONFIG_FILE = "config.csv"
EXCEL_FILE = "payments.xlsx"
SCREENSHOT_DIR = "screenshots"
SUFFIX_MAP = {"qi": 1, "sx": 1_000, "sp": 1_000_000}
RAW_COLS = ["Fecha", "Miembro", "Dias", "Cantidad", "Captura"]


def parse_quantity(qstr):
    q = qstr.strip().lower()
    for suf, mul in SUFFIX_MAP.items():
        if q.endswith(suf):
            return int(float(q[: -len(suf)]) * mul)
    return int(float(q))


def format_quantity(units):
    for suf in ["sp", "sx"]:
        mul = SUFFIX_MAP[suf]
        if units % mul == 0:
            return f"{units // mul}{suf}"
    return f"{units}qi"


def create_empty_payments():
    df0 = pd.DataFrame(columns=RAW_COLS)
    with pd.ExcelWriter(EXCEL_FILE, engine="openpyxl") as w:
        df0.to_excel(w, sheet_name="Pagos", index=False)
    return df0


def load_payments():
    if not os.path.exists(EXCEL_FILE):
        return create_empty_payments()
    try:
        df = pd.read_excel(
            EXCEL_FILE, sheet_name="Pagos", engine="openpyxl", parse_dates=["Fecha"]
        )
    except (zipfile.BadZipFile, ValueError):
        return create_empty_payments()
    df["Fecha"] = pd.to_datetime(df["Fecha"], errors="coerce").dt.date
    df["expiry_date"] = pd.to_datetime(df["Fecha"]) + pd.to_timedelta(
        df["Dias"] - 1, unit="d"
    )
    df["overdue_days"] = (
        pd.to_datetime(date.today()) - df["expiry_date"]
    ).dt.days.clip(lower=0)
    return df


st.set_page_config(layout="wide")
config = pd.read_csv(CONFIG_FILE)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
pagos_df = load_payments()

st.title("💰 Control de Donaciones")
role = st.sidebar.selectbox("¿Quién eres?", ["Miembro", "Administrador"])

if role == "Miembro":
    miembro = st.selectbox("Tu nombre", config["Miembro"])
    cantidad_str = st.text_input("Cantidad pagada (ej. 50qi, 1sx)", "50qi")
    qi_dia_str = st.text_input("Qi por día (ej. 50qi)", "50qi")
    try:
        q = parse_quantity(cantidad_str)
        qd = parse_quantity(qi_dia_str)
        st.info(f"{format_quantity(q)} equivale a {q//qd if qd>0 else 0} día(s).")
    except ValueError:
        pass
    captura = st.file_uploader("Sube tu comprobante", type=["png", "jpg", "jpeg"])
    if st.button("Registrar pago"):
        q = parse_quantity(cantidad_str)
        qd = parse_quantity(qi_dia_str)
        dias = q // qd
        if dias < 1:
            st.error("La cantidad no cubre ni un día.")
        else:
            fn = ""
            if captura:
                fn = date.today().strftime("%Y%m%d") + "_" + miembro + ".png"
                ruta = os.path.join(SCREENSHOT_DIR, fn)
                with open(ruta, "wb") as f:
                    f.write(captura.getbuffer())
            nuevo = {
                "Fecha": date.today(),
                "Miembro": miembro,
                "Dias": dias,
                "Cantidad": q,
                "Captura": fn,
            }
            pagos_df = pd.concat([pagos_df, pd.DataFrame([nuevo])], ignore_index=True)
            with pd.ExcelWriter(EXCEL_FILE, engine="openpyxl") as w:
                pagos_df[RAW_COLS].to_excel(w, sheet_name="Pagos", index=False)
            pagos_df = load_payments()
            st.success(f"✅ Registrado {dias} día(s) ({format_quantity(q)})")
    st.stop()

pw = st.sidebar.text_input("Contraseña de admin", type="password").strip()
if "admin_password" not in st.secrets or pw != st.secrets["admin_password"].strip():
    st.error("Acceso denegado.")
    st.stop()

st.header("🔑 Panel de Administración")
if st.button("🔄 Actualizar tabla"):
    pagos_df = load_payments()

last = pagos_df.sort_values("Fecha").groupby("Miembro").last().reset_index()
last["expiry_date"] = pd.to_datetime(last["Fecha"]) + pd.to_timedelta(
    last["Dias"] - 1, unit="d"
)
today = pd.to_datetime(date.today())
status = last[["Miembro", "expiry_date"]].copy()
status["Días atraso"] = (
    (today - status["expiry_date"]).dt.days.clip(lower=0).astype(int).astype(str)
)
missing = set(config["Miembro"]) - set(status["Miembro"])
if missing:
    extras = pd.DataFrame(
        [{"Miembro": m, "expiry_date": pd.NaT, "Días atraso": "Sin pagos"}]
        for m in missing
    )
    status = pd.concat([status, extras], ignore_index=True)
st.subheader("📋 Estado de miembros")
st.table(status[["Miembro", "Días atraso"]])

st.subheader("🗂️ Historial de pagos")
dias_filter = st.slider("Pagos con atraso ≥ días", 0, 365, 0)
tabla = pagos_df[pagos_df["overdue_days"] >= dias_filter][RAW_COLS].copy()
page_size = st.number_input("Filas por página", 5, 50, 10)
n_pages = (len(tabla) + page_size - 1) // page_size
page = st.number_input("Página", 1, max(1, n_pages), 1)
start, end = (page - 1) * page_size, page * page_size
view = tabla.iloc[start:end].copy()
view["Fecha"] = view["Fecha"].astype(str)
view["Cantidad"] = view["Cantidad"].apply(format_quantity)
view["Eliminar"] = False
edited = st.data_editor(view, use_container_width=True)
if st.button("Guardar cambios"):
    resto = tabla.drop(view.index)
    keep = edited[~edited["Eliminar"]][RAW_COLS].copy()
    keep["Fecha"] = pd.to_datetime(keep["Fecha"], errors="coerce").dt.date
    keep["Cantidad"] = keep["Cantidad"].apply(parse_quantity)
    new_full = pd.concat([resto, keep], ignore_index=True)
    with pd.ExcelWriter(EXCEL_FILE, engine="openpyxl") as w:
        new_full.to_excel(w, sheet_name="Pagos", index=False)
    pagos_df = load_payments()
    st.success("📝 Cambios guardados")
