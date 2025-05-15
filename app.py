import streamlit as st
import pandas as pd
import os
import zipfile
from datetime import datetime, date

CONFIG_FILE = "config.csv"
EXCEL_FILE = "payments.xlsx"
SCREENSHOT_DIR = "screenshots"
SUFFIX_MAP = {"qi": 1, "sx": 1_000, "sp": 1_000_000}
RAW_COLS = ["Timestamp", "Fecha", "Miembro", "Dias", "Cantidad", "Captura"]


def parse_quantity(qstr: str) -> int:
    q = qstr.strip().lower()
    for suf, mul in SUFFIX_MAP.items():
        if q.endswith(suf):
            return int(float(q[: -len(suf)]) * mul)
    return int(float(q))


def format_quantity(units: int) -> str:
    if units % SUFFIX_MAP["sp"] == 0:
        return f"{units // SUFFIX_MAP['sp']}sp"
    if units % SUFFIX_MAP["sx"] == 0:
        return f"{units // SUFFIX_MAP['sx']}sx"
    return f"{units}qi"


def compute_overdue_days(expiry: pd.Timestamp) -> int:
    d = (pd.to_datetime(date.today()) - expiry).days
    return d if d > 0 else 0


def create_empty_payments() -> pd.DataFrame:
    df0 = pd.DataFrame(columns=RAW_COLS)
    with pd.ExcelWriter(EXCEL_FILE, engine="openpyxl") as w:
        df0.to_excel(w, sheet_name="Pagos", index=False)
    return df0


st.set_page_config(layout="wide")

if not os.path.exists(CONFIG_FILE):
    st.error(f"No se encontró `{CONFIG_FILE}`")
    st.stop()
config = pd.read_csv(CONFIG_FILE)

os.makedirs(SCREENSHOT_DIR, exist_ok=True)

if not os.path.exists(EXCEL_FILE):
    pagos_df = create_empty_payments()
else:
    try:
        pagos_df = pd.read_excel(
            EXCEL_FILE,
            sheet_name="Pagos",
            engine="openpyxl",
            parse_dates=["Timestamp", "Fecha"],
        )
    except (zipfile.BadZipFile, ValueError):
        st.warning("⚠️ `payments.xlsx` corrupto o inválido. Se crea uno nuevo.")
        pagos_df = create_empty_payments()

pagos_df["Timestamp"] = pd.to_datetime(pagos_df["Timestamp"], errors="coerce")
pagos_df["Fecha"] = pd.to_datetime(pagos_df["Fecha"], errors="coerce")
pagos_df["expiry_date"] = pagos_df["Fecha"] + pd.to_timedelta(
    pagos_df["Dias"] - 1, unit="d"
)
pagos_df["overdue_days"] = pagos_df["expiry_date"].apply(compute_overdue_days)

st.title("💰 Control de Donaciones")

role = st.sidebar.selectbox("¿Quién eres?", ["Miembro", "Administrador"])
if role == "Miembro":
    miembro = st.selectbox("Tu nombre", config["Miembro"])
    cantidad_str = st.text_input("Cantidad pagada (ej. `50qi`, `1sx`)", value="50qi")
    qi_dia_str = st.text_input("Qi por día (ej. `50qi`)", value="50qi")
    captura = st.file_uploader(
        "Sube tu comprobante (PNG/JPG)", type=["png", "jpg", "jpeg"]
    )

    if st.button("Registrar pago"):
        try:
            cantidad = parse_quantity(cantidad_str)
            qi_por_dia = parse_quantity(qi_dia_str)
            dias = cantidad // qi_por_dia
            if dias < 1:
                st.error("La cantidad no cubre ni un día.")
            else:
                ts = datetime.now()
                ruta = ""
                if captura:
                    fn = ts.strftime("%Y%m%d%H%M%S") + "_" + miembro + ".png"
                    ruta = os.path.join(SCREENSHOT_DIR, fn)
                    with open(ruta, "wb") as f:
                        f.write(captura.getbuffer())
                    captura_fn = fn
                else:
                    captura_fn = ""

                nuevo = {
                    "Timestamp": ts,
                    "Fecha": ts,
                    "Miembro": miembro,
                    "Dias": dias,
                    "Cantidad": cantidad,
                    "Captura": captura_fn,
                }
                pagos_df = pd.concat(
                    [pagos_df, pd.DataFrame([nuevo])], ignore_index=True
                )
                with pd.ExcelWriter(EXCEL_FILE, engine="openpyxl") as w:
                    pagos_df[RAW_COLS].to_excel(w, sheet_name="Pagos", index=False)
                st.success(f"✅ Registrado {dias} día(s) ({format_quantity(cantidad)})")
        except Exception as e:
            st.error(f"Error: {e}")
    st.stop()

pw = st.sidebar.text_input("Contraseña de admin", type="password").strip()
if "admin_password" not in st.secrets:
    st.error("Define `admin_password` en `.streamlit/secrets.toml`")
    st.stop()
if pw != st.secrets["admin_password"].strip():
    st.error("Contraseña incorrecta.")
    st.stop()

st.sidebar.success("👑 Acceso admin concedido")
st.header("🔑 Panel de Administración")

last = pagos_df.sort_values("Timestamp").groupby("Miembro").last().reset_index()
last["expiry_date"] = last["Fecha"] + pd.to_timedelta(last["Dias"] - 1, unit="d")
today = pd.to_datetime(date.today())
status = last[["Miembro", "expiry_date"]].copy()
status["Días atraso"] = (
    (today - status["expiry_date"]).dt.days.clip(lower=0).astype(int).astype(str)
)
missing = set(config["Miembro"]) - set(status["Miembro"])
for m in missing:
    status = status.append(
        {"Miembro": m, "expiry_date": pd.NaT, "Días atraso": "Sin pagos"},
        ignore_index=True,
    )
st.subheader("📋 Estado de miembros")
st.table(status[["Miembro", "Días atraso"]])

st.subheader("🗂️ Historial de pagos")
dias_filter = st.slider("Pagos con atraso ≥ días", min_value=0, max_value=365, value=0)
tabla = pagos_df[pagos_df["overdue_days"] >= dias_filter][RAW_COLS].copy()
page_size = st.number_input("Filas por página", min_value=5, max_value=50, value=10)
n_pages = (len(tabla) + page_size - 1) // page_size
page = st.number_input("Página", min_value=1, max_value=max(1, n_pages), value=1)
start, end = (page - 1) * page_size, page * page_size
view = tabla.iloc[start:end].copy()
view["Timestamp"] = view["Timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
view["Fecha"] = view["Fecha"].dt.strftime("%Y-%m-%d")
view["Cantidad"] = view["Cantidad"].apply(format_quantity)
edited = st.experimental_data_editor(view, num_rows="dynamic", use_container_width=True)
if st.button("Guardar cambios"):
    resto = tabla.drop(view.index, axis=0)
    edited["Timestamp"] = pd.to_datetime(edited["Timestamp"], errors="coerce")
    edited["Fecha"] = pd.to_datetime(edited["Fecha"], errors="coerce")
    edited["Cantidad"] = edited["Cantidad"].apply(parse_quantity)
    new_full = pd.concat([resto, edited], ignore_index=True)[RAW_COLS]
    with pd.ExcelWriter(EXCEL_FILE, engine="openpyxl") as w:
        new_full.to_excel(w, sheet_name="Pagos", index=False)
    st.success("📝 Cambios guardados")
