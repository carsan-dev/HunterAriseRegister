import streamlit as st
import pandas as pd
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh
from supabase import create_client, Client

# Configuración
CONFIG_FILE = "config.csv"
SCREENSHOT_DIR = "screenshots"
ESP = ZoneInfo("Europe/Madrid")
SUFFIX_MAP = {"qi": 1, "sx": 1000, "sp": 1000000}
RAW_COLS = ["Fecha", "Miembro", "Dias", "Cantidad", "Captura"]

# Inicializar Supabase
SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def parse_quantity(qstr):
    q = qstr.strip().lower()
    for suf, mul in SUFFIX_MAP.items():
        if q.endswith(suf):
            return float(q[: -len(suf)]) * mul
    return float(q)


def format_quantity(units):
    for suf in ("sp", "sx", "qi"):
        mul = SUFFIX_MAP[suf]
        val = units / mul
        if val >= 1:
            if float(val).is_integer():
                return f"{int(val)}{suf}"
            s = f"{val:.3f}".rstrip("0").rstrip(".")
            return f"{s}{suf}"
    return f"{units}qi"


def load_payments():
    res = (
        supabase.table("pagos")
        .select("id, Fecha, Miembro, Dias, Cantidad, Captura")
        .execute()
    )
    data = res.data or []
    df = pd.DataFrame(data)
    if not df.empty:
        df["Fecha"] = pd.to_datetime(df["Fecha"]).dt.date
    return df


def save_payment(fecha, miembro, dias, cantidad, captura):
    record = {
        "Fecha": fecha.isoformat(),
        "Miembro": miembro,
        "Dias": dias,
        "Cantidad": cantidad,
        "Captura": captura,
    }
    supabase.table("pagos").insert(record).execute()


def delete_all_and_insert(df_full):
    supabase.table("pagos").delete().neq("id", 0).execute()
    records = []
    for _, row in df_full.iterrows():
        records.append(
            {
                "Fecha": (
                    row["Fecha"].isoformat()
                    if isinstance(row["Fecha"], date)
                    else row["Fecha"]
                ),
                "Miembro": row["Miembro"],
                "Dias": row["Dias"],
                "Cantidad": row["Cantidad"],
                "Captura": row["Captura"],
            }
        )
    if records:
        supabase.table("pagos").insert(records).execute()


def compute_expiry(group):
    exp = None
    for f, d in zip(group["Fecha"], group["Dias"]):
        f = pd.to_datetime(f)
        if exp is None or f > exp:
            exp = f + pd.to_timedelta(d - 1, unit="d")
        else:
            exp += pd.to_timedelta(d, unit="d")
    return exp


def member_view(config):
    miembro = st.selectbox("Tu nombre", config["Miembro"])
    fecha = st.date_input("Fecha de la donación", datetime.now(tz=ESP).date())
    cantidad_str = st.text_input("Cantidad pagada (ej. 1sx)", "1sx")
    qi_dia_str = st.text_input("Sx por día (ej. 1sx)", "1sx")
    try:
        q = parse_quantity(cantidad_str)
        qd = parse_quantity(qi_dia_str)
        est = q / qd if qd > 0 else 0
        days_str = f"{est:.2f}" if not float(est).is_integer() else str(int(est))
        st.info(f"{format_quantity(q)} equivale a {days_str} día(s)")
    except Exception:
        st.error("Error al calcular la cantidad. Revisa el formato.")
    captura = st.file_uploader("Comprobante (PNG/JPG)", type=["png", "jpg", "jpeg"])
    if st.button("Registrar pago"):
        try:
            q = parse_quantity(cantidad_str)
            dias = q / parse_quantity(qi_dia_str)
            if dias < 1:
                st.error("La cantidad no cubre ni un día")
            else:
                fn = ""
                if captura:
                    base = f"{fecha.strftime('%Y%m%d')}_{miembro}"
                    fn = base + ".png"
                    i = 1
                    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
                    while os.path.exists(os.path.join(SCREENSHOT_DIR, fn)):
                        fn = f"{base}_{i}.png"
                        i += 1
                    with open(os.path.join(SCREENSHOT_DIR, fn), "wb") as f:
                        f.write(captura.getbuffer())
                save_payment(fecha, miembro, dias, q, fn)
                st.success("✅ Pago registrado")
        except Exception:
            st.error("Error al registrar. Verifica los datos.")
    st.stop()


def show_historial(config):
    pagos_df = load_payments()
    st.subheader("🗂️ Historial de pagos")
    members = ["Todos"] + sorted(config["Miembro"].unique())
    sel = st.selectbox("Filtrar por miembro", members, key="hist_member")
    if pagos_df.empty:
        min_val = max_val = date.today()
    else:
        min_val = pagos_df["Fecha"].min()
        max_val = pagos_df["Fecha"].max()
    dates = st.date_input("Rango de fechas", [min_val, max_val], key="hist_dates")
    if not (isinstance(dates, (list, tuple)) and len(dates) == 2):
        st.error("Selecciona fecha inicio y final para el filtro.")
        return
    lo, hi = dates
    df = pagos_df[(pagos_df["Fecha"] >= lo) & (pagos_df["Fecha"] <= hi)]
    if sel != "Todos":
        df = df[df["Miembro"] == sel]
    df_edit = df.copy()
    df_edit["Cantidad_fmt"] = df_edit["Cantidad"].apply(format_quantity)
    df_edit["Eliminar"] = False
    edited = st.data_editor(
        df_edit[
            ["id", "Fecha", "Miembro", "Dias", "Cantidad_fmt", "Captura", "Eliminar"]
        ],
        column_config={
            "id": {"hidden": True},
            "Captura": {"disabled": True},
            "Cantidad_fmt": {"title": "Cantidad"},
            "Eliminar": {"type": "boolean"},
        },
        use_container_width=True,
        key="hist_editor",
    )
    if st.button("Guardar cambios", key="hist_save"):
        keep = edited[~edited["Eliminar"]].copy()
        keep["Cantidad"] = keep["Cantidad_fmt"].apply(parse_quantity)
        keep["Fecha"] = pd.to_datetime(keep["Fecha"]).dt.date
        outside = load_payments()[~load_payments()["id"].isin(df["id"])]
        new_full = pd.concat([outside[RAW_COLS], keep[RAW_COLS]], ignore_index=True)
        delete_all_and_insert(new_full)
        st.success("📝 Cambios guardados")


def show_capturas(config):
    pagos_df = load_payments()
    st.subheader("📸 Capturas de pagos")
    members = ["Todos"] + sorted(config["Miembro"].unique())
    sel = st.selectbox("Mostrar capturas de:", members, key="cap_member")
    df = pagos_df if sel == "Todos" else pagos_df[pagos_df["Miembro"] == sel]
    df = df.sort_values("Fecha", ascending=False)
    if "show_all" not in st.session_state:
        st.session_state["show_all"] = False
    btn = "Mostrar todas" if not st.session_state["show_all"] else "Ocultar"
    if st.button(btn, key="cap_toggle"):
        st.session_state["show_all"] = not st.session_state["show_all"]
    display = df if st.session_state["show_all"] else df.head(5)
    for _, r in display.iterrows():
        if not r["Captura"]:
            continue
        path = os.path.join(SCREENSHOT_DIR, r["Captura"])
        if not os.path.exists(path):
            continue
        c1, c2 = st.columns([1, 3])
        with c1:
            st.image(path, width=100)
        with c2:
            st.markdown(f"**Miembro:** {r['Miembro']}")
            st.markdown(f"**Fecha:** {r['Fecha']}")
            st.markdown(f"**Cantidad:** {format_quantity(r['Cantidad'])}")
            with st.expander("🔍 Ampliar captura"):
                st.image(path, use_container_width=True)
        st.markdown("---")


def main():
    config = pd.read_csv(CONFIG_FILE)
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    st.set_page_config(layout="wide")
    st.title("💰 Control de Donaciones con Supabase")
    role = st.sidebar.selectbox("¿Quién eres?", ["Miembro", "Administrador"])
    if role == "Miembro":
        member_view(config)
        return
    pw = st.sidebar.text_input("Contraseña admin", type="password")
    if pw != st.secrets.get("admin_password", ""):
        st.error("Acceso denegado")
        return
    st.sidebar.success("👑 Acceso admin concedido")
    st_autorefresh(interval=5000, key="datarefresh")
    show_historial(config)
    show_capturas(config)


if __name__ == "__main__":
    main()
