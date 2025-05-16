import streamlit as st
import pandas as pd
import os
import zipfile
from datetime import date, datetime
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh

CONFIG_FILE = "config.csv"
EXCEL_FILE = "payments.xlsx"
SCREENSHOT_DIR = "screenshots"
SUFFIX_MAP = {"qi": 1, "sx": 1000, "sp": 1000000}
RAW_COLS = ["Fecha", "Miembro", "Dias", "Cantidad", "Captura"]
ESP = ZoneInfo("Europe/Madrid")


def parse_quantity(qstr):
    q = qstr.strip().lower()
    for suf, mul in SUFFIX_MAP.items():
        if q.endswith(suf):
            return int(float(q[: -len(suf)]) * mul)
    return int(float(q))


def format_quantity(units):
    for suf in ("sp", "sx"):
        mul = SUFFIX_MAP[suf]
        if units % mul == 0:
            return f"{units//mul}{suf}"
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
    df["Captura"] = df["Captura"].fillna("").astype(str)
    return df


def compute_expiry(group):
    exp = None
    for f, d in zip(group["Fecha"], group["Dias"]):
        f = pd.to_datetime(f)
        if exp is None or f > exp:
            exp = f + pd.to_timedelta(d - 1, unit="d")
        else:
            exp = exp + pd.to_timedelta(d, unit="d")
    return exp


st.set_page_config(layout="wide")
config = pd.read_csv(CONFIG_FILE)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
pagos_df = load_payments()

st.title("💰 Control de Donaciones")
role = st.sidebar.selectbox("¿Quién eres?", ["Miembro", "Administrador"])
pw = st.sidebar.text_input("Contraseña de admin", type="password", key="admin_pw")

if role == "Miembro":
    miembro = st.selectbox("Tu nombre", config["Miembro"])
    fecha = st.date_input("Fecha de la donación", value=datetime.now(tz=ESP).date())
    cantidad_str = st.text_input("Cantidad pagada (ej. 50qi, 1sx)", "50qi")
    qi_dia_str = st.text_input("Qi por día (ej. 50qi)", "50qi")
    try:
        q = parse_quantity(cantidad_str)
        qd = parse_quantity(qi_dia_str)
        est = q // qd if qd > 0 else 0
        st.info(f"{format_quantity(q)} equivale a {est} día(s)")
    except ValueError:
        pass
    captura = st.file_uploader(
        "Sube tu comprobante (PNG/JPG)", type=["png", "jpg", "jpeg"]
    )
    if st.button("Registrar pago"):
        q = parse_quantity(cantidad_str)
        dias = q // parse_quantity(qi_dia_str)
        if dias < 1:
            st.error("La cantidad no cubre ni un día")
        else:
            fn = ""
            if captura:
                base = f"{fecha.strftime('%Y%m%d')}_{miembro}"
                fn = base + ".png"
                i = 1
                while os.path.exists(os.path.join(SCREENSHOT_DIR, fn)):
                    fn = f"{base}_{i}.png"
                    i += 1
                with open(os.path.join(SCREENSHOT_DIR, fn), "wb") as f:
                    f.write(captura.getbuffer())
            nuevo = {
                "Fecha": fecha,
                "Miembro": miembro,
                "Dias": dias,
                "Cantidad": q,
                "Captura": fn,
            }
            pagos_df = pd.concat([pagos_df, pd.DataFrame([nuevo])], ignore_index=True)
            with pd.ExcelWriter(EXCEL_FILE, engine="openpyxl") as w:
                pagos_df.to_excel(w, sheet_name="Pagos", index=False)
            st.success("✅ Pago registrado")
    st.stop()

if (
    "admin_password" not in st.secrets
    or pw.strip() != st.secrets["admin_password"].strip()
):
    st.error("Acceso denegado")
    st.stop()

st_autorefresh(interval=5000, key="datarefresh")
st.sidebar.success("👑 Acceso admin concedido")

if "last_count" not in st.session_state:
    st.session_state["last_count"] = len(pagos_df)
if "pending_notifications" not in st.session_state:
    st.session_state["pending_notifications"] = []
new_count = len(pagos_df)
if new_count > st.session_state["last_count"]:
    pagos_sorted = pagos_df.sort_values("Fecha").reset_index(drop=True)
    for i in range(st.session_state["last_count"], new_count):
        p = pagos_sorted.iloc[i]
        ph = st.sidebar.empty()
        st.session_state["pending_notifications"].append(
            {
                "time": datetime.now(tz=ESP),
                "Miembro": p["Miembro"],
                "Cantidad": p["Cantidad"],
                "Dias": p["Dias"],
                "placeholder": ph,
            }
        )
    st.session_state["last_count"] = new_count
elif new_count < st.session_state["last_count"]:
    st.session_state["last_count"] = new_count
now = datetime.now(tz=ESP)
filtered = []
for n in st.session_state["pending_notifications"]:
    if (now - n["time"]).total_seconds() < 30:
        n["placeholder"].info(
            f"🔔 Pago: **{n['Miembro']}** — {format_quantity(n['Cantidad'])} ({n['Dias']} días)"
        )
        filtered.append(n)
    else:
        n["placeholder"].empty()
st.session_state["pending_notifications"] = filtered

st.header("🔑 Panel de Administración")
members_list = config["Miembro"].unique()
status_rows = []
today_date = datetime.now(tz=ESP).date()
for m in members_list:
    grp = pagos_df[pagos_df["Miembro"] == m]
    if grp.empty:
        status_rows.append(
            {"Miembro": m, "Días restantes": "Sin pagos", "Días atraso": "Sin pagos"}
        )
    else:
        exp = compute_expiry(grp)
        days_left = max((exp.date() - today_date).days, 0)
        days_over = max((today_date - exp.date()).days, 0)
        status_rows.append(
            {"Miembro": m, "Días restantes": days_left, "Días atraso": days_over}
        )
status_df = pd.DataFrame(status_rows)

st.subheader("📋 Estado de miembros")
st.table(status_df[["Miembro", "Días restantes", "Días atraso"]].astype(str))

st.subheader("🗂️ Historial de pagos")
members_hist = ["Todos"] + sorted(config["Miembro"].unique())
sel_hist_member = st.selectbox("Filtrar por miembro", members_hist)
min_val = pagos_df["Fecha"].min()
min_date = min_val.date() if hasattr(min_val, "date") else date.today()
max_val = pagos_df["Fecha"].max()
max_date = max_val.date() if hasattr(max_val, "date") else min_date
rango = st.date_input("Rango de fechas", [min_date, max_date])
if not isinstance(rango, (list, tuple)) or len(rango) != 2:
    st.error("Por favor, añade la fecha de fin al rango")
    st.stop()
date_inf, date_sup = rango
tabla = pagos_df.copy()
if sel_hist_member != "Todos":
    tabla = tabla[tabla["Miembro"] == sel_hist_member]
tabla = tabla[(tabla["Fecha"] >= date_inf) & (tabla["Fecha"] <= date_sup)]
tabla["expiry_date"] = pd.to_datetime(tabla["Fecha"]) + pd.to_timedelta(
    tabla["Dias"] - 1, unit="d"
)

today_date = datetime.now(tz=ESP).date()
tabla["Días atraso"] = (
    (pd.to_datetime(today_date) - tabla["expiry_date"])
    .dt.days.clip(lower=0)
    .astype(int)
)

dias_filter = st.slider("Pagos con atraso ≥ días", 0, 365, 0)
tabla = tabla[tabla["Días atraso"] >= dias_filter][RAW_COLS].copy()

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
    outside = tabla.drop(view.index)
    keep = edited[~edited["Eliminar"]][RAW_COLS].copy()
    keep["Fecha"] = pd.to_datetime(keep["Fecha"], errors="coerce").dt.date
    keep["Cantidad"] = keep["Cantidad"].apply(parse_quantity)
    new_full = pd.concat([outside, keep], ignore_index=True)
    with pd.ExcelWriter(EXCEL_FILE, engine="openpyxl") as w:
        new_full.to_excel(w, sheet_name="Pagos", index=False)
    pagos_df = load_payments()
    st.success("📝 Cambios guardados")

st.subheader("📸 Capturas")
members = ["Todos"] + sorted(config["Miembro"].unique())
sel_member = st.selectbox("Mostrar capturas de:", members)
df_cap = (
    pagos_df if sel_member == "Todos" else pagos_df[pagos_df["Miembro"] == sel_member]
)
df_cap = df_cap.sort_values("Fecha")
if "show_all_caps" not in st.session_state:
    st.session_state.show_all_caps = False
btn_label = (
    "Ocultar capturas"
    if st.session_state.show_all_caps
    else "Mostrar todas las capturas"
)
if st.button(btn_label):
    st.session_state.show_all_caps = not st.session_state.show_all_caps
caps_to_show = df_cap if st.session_state.show_all_caps else df_cap.head(12)
for i in range(0, len(caps_to_show), 6):
    cols = st.columns(6, gap="small")
    for j, col in enumerate(cols):
        idx = i + j
        if idx < len(caps_to_show):
            r = caps_to_show.iloc[idx]
            fn = r.Captura
            if fn:
                path = os.path.join(SCREENSHOT_DIR, fn)
                if os.path.exists(path):
                    col.image(path, width=150)
                    col.markdown(
                        f"<div style='text-align:left;'><strong>{r.Miembro}</strong><br>{r.Fecha:%Y-%m-%d}</div>",
                        unsafe_allow_html=True,
                    )
options = [""]
paths = {}
for r in df_cap.itertuples():
    fn = r.Captura
    if fn:
        opt = f"{r.Miembro} — {r.Fecha:%Y-%m-%d}"
        options.append(opt)
        paths[opt] = os.path.join(SCREENSHOT_DIR, fn)
sel = st.selectbox("Selecciona captura para ampliar:", options)
if sel:
    st.image(paths[sel], use_container_width=True)
