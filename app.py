import streamlit as st
import pandas as pd
import os
import zipfile
from datetime import datetime, date

CONFIG_FILE = "config.csv"
EXCEL_FILE = "payments.xlsx"
SCREENSHOT_DIR = "screenshots"
SUFFIX_MAP = {"qi": 1, "sx": 1_000, "sp": 1_000_000}

def parse_quantity(qstr: str) -> int:
    """Convierte '50qi', '3sx', '2sp' o número simple a unidades base."""
    qstr = qstr.strip().lower()
    for suf, mul in SUFFIX_MAP.items():
        if qstr.endswith(suf):
            return int(float(qstr[: -len(suf)]) * mul)
    return int(float(qstr))


def format_quantity(units: int) -> str:
    """Formatea unidades a la unidad mayor posible con sufijo."""
    if units % SUFFIX_MAP["sp"] == 0:
        return f"{units // SUFFIX_MAP['sp']}sp"
    if units % SUFFIX_MAP["sx"] == 0:
        return f"{units // SUFFIX_MAP['sx']}sx"
    return f"{units}qi"


def compute_overdue_days(last_expiry: date) -> int:
    dias = (date.today() - last_expiry).days
    return dias if dias > 0 else 0


def create_empty_payments() -> pd.DataFrame:
    """Crea un DataFrame vacío y guarda un nuevo payments.xlsx válido."""
    df0 = pd.DataFrame(
        columns=["Timestamp", "Fecha", "Miembro", "Dias", "Cantidad", "Captura"]
    )
    with pd.ExcelWriter(EXCEL_FILE, engine="openpyxl") as writer:
        df0.to_excel(writer, sheet_name="Pagos", index=False)
    return df0

if not os.path.exists(CONFIG_FILE):
    st.error(f"No existe {CONFIG_FILE}")
    st.stop()
config = pd.read_csv(CONFIG_FILE)

os.makedirs(SCREENSHOT_DIR, exist_ok=True)

if not os.path.exists(EXCEL_FILE):
    pagos_df = create_empty_payments()
else:
    try:
        pagos_df = pd.read_excel(
            EXCEL_FILE, sheet_name="Pagos", parse_dates=["Timestamp", "Fecha"]
        )
    except (zipfile.BadZipFile, ValueError):
        st.warning("⚠️ El archivo de pagos está corrupto o inválido. Se crea uno nuevo.")
        pagos_df = create_empty_payments()

st.set_page_config(layout="wide")
st.title("💰 Control de Donaciones del Gremio")

st.sidebar.header("Acceso")
role = st.sidebar.selectbox("Eres...", ["Miembro", "Administrador"])
if role == "Miembro":
    miembro = st.selectbox("Tu nombre", config["Miembro"])
    cantidad_str = st.text_input("Cantidad pagada (ej. 50qi, 1sx, 2sp)", value="50qi")
    qi_dia_str = st.text_input("Qi por día (ej. 50qi)", value="50qi")
    captura = st.file_uploader("Sube tu captura (PNG/JPG)", type=["png", "jpg", "jpeg"])

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
                    nombre = f"{ts.strftime('%Y%m%d%H%M%S')}_{miembro}.png"
                    ruta = os.path.join(SCREENSHOT_DIR, nombre)
                    with open(ruta, "wb") as f:
                        f.write(captura.getbuffer())

                nuevo = {
                    "Timestamp": ts,
                    "Fecha": ts.date(),
                    "Miembro": miembro,
                    "Dias": dias,
                    "Cantidad": cantidad,
                    "Captura": os.path.basename(ruta),
                }
                pagos_df = pd.concat(
                    [pagos_df, pd.DataFrame([nuevo])], ignore_index=True
                )
                with pd.ExcelWriter(EXCEL_FILE, engine="openpyxl") as writer:
                    pagos_df.to_excel(writer, sheet_name="Pagos", index=False)
                st.success(f"Registrado {dias} día(s) ({format_quantity(cantidad)})")
        except Exception as e:
            st.error(f"Error al registrar el pago: {e}")
    st.stop()

pw_input = st.sidebar.text_input("Contraseña de admin", type="password").strip()
if "admin_password" not in st.secrets:
    st.error("No has configurado la contraseña en `st.secrets['admin_password']`.")
    st.stop()
if pw_input != st.secrets["admin_password"].strip():
    st.error("Contraseña incorrecta.")
    st.stop()

st.sidebar.success("Acceso de administrador concedido.")
st.header("🔑 Panel de Administración")

status = []
for _, row in config.iterrows():
    m = row["Miembro"]
    dfm = pagos_df[pagos_df["Miembro"] == m]
    if dfm.empty:
        overdue = None
    else:
        ultima = dfm.iloc[-1]
        last_expiry = ultima["Fecha"].date() + pd.Timedelta(
            days=int(ultima["Dias"]) - 1
        )
        overdue = compute_overdue_days(last_expiry)
    status.append(
        {"Miembro": m, "Días atraso": overdue if overdue is not None else "Sin pagos"}
    )
st.subheader("📋 Estado de miembros")
st.table(pd.DataFrame(status))

st.subheader("🗂️ Historial de pagos")

dias_filter = st.slider(
    "Mostrar pagos con atraso ≥ días", min_value=0, max_value=100, value=0
)

mask = []
for _, pago in pagos_df.iterrows():
    last_expiry = pago["Fecha"].date() + pd.Timedelta(days=int(pago["Dias"]) - 1)
    overdue = compute_overdue_days(last_expiry)
    mask.append(overdue >= dias_filter)

tabla = pagos_df.loc[mask].copy()

page_size = st.number_input("Filas por página", min_value=5, max_value=50, value=10)
page = st.number_input("Página", min_value=1, value=1)
start = (page - 1) * page_size
end = start + page_size
view = tabla.iloc[start:end]

view["Cantidad"] = view["Cantidad"].apply(format_quantity)
view["Timestamp"] = view["Timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
view["Fecha"] = view["Fecha"].dt.strftime("%Y-%m-%d")

edited = st.experimental_data_editor(view, num_rows="dynamic", use_container_width=True)

if st.button("Guardar cambios"):
    resto = pd.concat([tabla.iloc[:start], tabla.iloc[end:]], ignore_index=True)
    new_full = pd.concat([resto, edited], ignore_index=True)
    new_full["Timestamp"] = pd.to_datetime(new_full["Timestamp"])
    new_full["Fecha"] = pd.to_datetime(new_full["Fecha"]).dt.date
    new_full["Cantidad"] = new_full["Cantidad"].apply(parse_quantity)
    with pd.ExcelWriter(EXCEL_FILE, engine="openpyxl") as writer:
        new_full.to_excel(writer, sheet_name="Pagos", index=False)
    st.success("Cambios guardados.")
