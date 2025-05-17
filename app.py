import streamlit as st
import pandas as pd
import sqlite3
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh

CONFIG_FILE = "config.csv"
DB_FILE = "payments.db"
SCREENSHOT_DIR = "screenshots"
SUFFIX_MAP = {"qi": 1, "sx": 1000, "sp": 1000000}
RAW_COLS = ["Fecha", "Miembro", "Dias", "Cantidad", "Captura"]
ESP = ZoneInfo("Europe/Madrid")


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
            else:
                s = f"{val:.3f}".rstrip('0').rstrip('.')
                return f"{s}{suf}"
    return f"{units}qi"


def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pagos (
            Fecha TEXT,
            Miembro TEXT,
            Dias REAL,
            Cantidad REAL,
            Captura TEXT
        )
        """
    )
    conn.commit()
    return conn


def load_payments(conn):
    df = pd.read_sql("SELECT rowid as id, * FROM pagos", conn, parse_dates=["Fecha"])
    df["Fecha"] = pd.to_datetime(df["Fecha"]).dt.date
    return df


def save_payment(conn, fecha, miembro, dias, cantidad, captura):
    conn.execute(
        "INSERT INTO pagos (Fecha,Miembro,Dias,Cantidad,Captura) VALUES (?,?,?,?,?)",
        (fecha.isoformat(), miembro, dias, cantidad, captura),
    )
    conn.commit()


def compute_expiry(group):
    exp = None
    for f, d in zip(group["Fecha"], group["Dias"]):
        f = pd.to_datetime(f)
        if exp is None or f > exp:
            exp = f + pd.to_timedelta(d - 1, unit="d")
        else:
            exp += pd.to_timedelta(d, unit="d")
    return exp


def member_view(conn, config):
    miembro = st.selectbox("Tu nombre", config["Miembro"])
    fecha = st.date_input("Fecha de la donación", datetime.now(tz=ESP).date())
    cantidad_str = st.text_input("Cantidad pagada (ej. 1sx)", "1sx")
    qi_dia_str = st.text_input("Sx por día (ej. 1sx)", "1sx")
    try:
        q = parse_quantity(cantidad_str)
        qd = parse_quantity(qi_dia_str)
        est = q / qd if qd > 0 else 0
        if float(est).is_integer():
            days_str = f"{int(est)} día(s)"
        else:
            days_str = f"{est:.2f} día(s)"
        st.info(f"{format_quantity(q)} equivale a {days_str}")
    except Exception:
        st.error("Error al calcular la cantidad. Revisa el formato de entrada.")
    captura = st.file_uploader("Comprobante (PNG/JPG)", type=["png", "jpg", "jpeg"])
    if st.button("Registrar pago"):
        try:
            q = parse_quantity(cantidad_str)
            qd = parse_quantity(qi_dia_str)
            dias = q / qd if qd > 0 else 0
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
                save_payment(conn, fecha, miembro, dias, q, fn)
                st.success("✅ Pago registrado")
        except Exception:
            st.error("Error al registrar el pago. Revisa datos.")
    st.stop()


def show_notifications(pagos_df):
    if "last_count" not in st.session_state:
        st.session_state["last_count"] = len(pagos_df)
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
    now = datetime.now(tz=ESP)
    kept = []
    for n in st.session_state["pending_notifications"]:
        if (now - n["time"]).total_seconds() < 30:
            n["placeholder"].info(
                f"🔔 Pago: **{n['Miembro']}** — {format_quantity(n['Cantidad'])} ({n['Dias']} días)"
            )
            kept.append(n)
        else:
            n["placeholder"].empty()
    st.session_state["pending_notifications"] = kept


def admin_dashboard(pagos_df, config):
    st.header("🔑 Panel de Administración")
    rows = []
    today = datetime.now(tz=ESP).date()
    for m in config["Miembro"].unique():
        grp = pagos_df[pagos_df["Miembro"] == m]
        if grp.empty:
            rows.append({"Miembro": m, "Días restantes": "Sin pagos", "Días atraso": "Sin pagos"})
        else:
            exp = compute_expiry(grp)
            left = max((exp.date() - today).days, 0)
            over = max((today - exp.date()).days, 0)
            rows.append({"Miembro": m, "Días restantes": left, "Días atraso": over})
    df = pd.DataFrame(rows)
    st.subheader("📋 Estado de miembros")
    st.table(df[["Miembro", "Días restantes", "Días atraso"]].astype(str))


def show_historial(conn, config):
    pagos_df = load_payments(conn)
    st.subheader("🗂️ Historial de pagos")
    members = ["Todos"] + sorted(config["Miembro"].unique())
    sel = st.selectbox("Filtrar por miembro", members, key="hist_member")
    if pagos_df.empty:
        min_val = max_val = date.today()
    else:
        min_raw = pagos_df["Fecha"].min()
        max_raw = pagos_df["Fecha"].max()
        min_val = min_raw if isinstance(min_raw, date) else date.today()
        max_val = max_raw if isinstance(max_raw, date) else date.today()
    dates = st.date_input("Rango de fechas", [min_val, max_val], key="hist_dates")
    if not (isinstance(dates, (list, tuple)) and len(dates) == 2):
        st.error("Por favor, selecciona fecha de inicio y fecha final para el filtro.")
        return
    lo, hi = dates
    df = pagos_df[(pagos_df["Fecha"] >= lo) & (pagos_df["Fecha"] <= hi)]
    if sel != "Todos":
        df = df[df["Miembro"] == sel]
    df_edit = df.copy()
    df_edit["Cantidad_fmt"] = df_edit["Cantidad"].apply(format_quantity)
    df_edit["Eliminar"] = False
    edited = st.data_editor(
        df_edit[["id", "Fecha", "Miembro", "Dias", "Cantidad_fmt", "Captura", "Eliminar"]],
        column_config={
            "id": {"hidden": True},
            "Captura": {"title": "Captura (ruta)", "type": "text", "disabled": True},
            "Cantidad_fmt": {"title": "Cantidad"},
            "Eliminar": {"type": "boolean"},
        }, use_container_width=True, key="hist_editor"
    )
    if st.button("Guardar cambios", key="hist_save"):
        keep = edited[~edited["Eliminar"]].copy()
        keep["Cantidad"] = keep["Cantidad_fmt"].apply(parse_quantity)
        keep["Fecha"] = pd.to_datetime(keep["Fecha"], errors="coerce").dt.date
        df_ids = set(df["id"])
        keep_ids = set(keep["id"])
        deleted_ids = df_ids - keep_ids
        old_df = load_payments(conn)
        for _, row in old_df[old_df["id"].isin(deleted_ids)].iterrows():
            if row["Captura"]:
                path = os.path.join(SCREENSHOT_DIR, row["Captura"])
                if os.path.exists(path):
                    os.remove(path)
        all_remaining = old_df[~old_df["id"].isin(deleted_ids)][RAW_COLS]
        conn.execute("DELETE FROM pagos")
        conn.commit()
        for _, row in all_remaining.iterrows():
            save_payment(conn, row["Fecha"], row["Miembro"], row["Dias"], row["Cantidad"], row["Captura"])
        st.success("📝 Cambios guardados")


def show_capturas(conn, config):
    pagos_df = load_payments(conn)
    st.subheader("📸 Capturas de pagos")
    members = ["Todos"] + sorted(config["Miembro"].unique())
    sel = st.selectbox("Mostrar capturas de:", members, key="cap_member")
    df = pagos_df if sel == "Todos" else pagos_df[pagos_df["Miembro"] == sel]
    df = df.sort_values("Fecha", ascending=False)
    if "show_all_caps" not in st.session_state:
        st.session_state["show_all_caps"] = False
    btn_text = "Mostrar todas capturas" if not st.session_state["show_all_caps"] else "Mostrar sólo últimas 5"
    if st.button(btn_text, key="cap_toggle"):
        st.session_state["show_all_caps"] = not st.session_state["show_all_caps"]
    display_df = df if st.session_state["show_all_caps"] else df.head(5)
    for _, row in display_df.iterrows():
        if not row["Captura"]:
            continue
        path = os.path.join(SCREENSHOT_DIR, row["Captura"])
        if not os.path.exists(path):
            continue
        c1, c2 = st.columns([1, 3])
        with c1:
            st.image(path, width=100)
        with c2:
            st.markdown(f"**Miembro:** {row['Miembro']}")
            st.markdown(f"**Fecha:** {row['Fecha']}")
            st.markdown(f"**Cantidad:** {format_quantity(row['Cantidad'])}")
            with st.expander("🔍 Ampliar captura"):
                st.image(path, use_container_width=True)
        st.markdown("---")


def main():
    conn = init_db()
    config = pd.read_csv(CONFIG_FILE)
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    st.set_page_config(layout="wide")
    st.title("💰 Control de Donaciones")
    role = st.sidebar.selectbox("¿Quién eres?", ["Miembro", "Administrador"])
    pw = st.sidebar.text_input("Contraseña de admin", type="password", key="admin")
    if role == "Miembro":
        member_view(conn, config)
        return
    if pw.strip() != st.secrets.get("admin_password", ""):
        st.error("Acceso denegado")
        return
    st.sidebar.success("👑 Acceso admin concedido")
    st_autorefresh(interval=5000, key="datarefresh")
    pagos_df = load_payments(conn)
    show_notifications(pagos_df)
    admin_dashboard(pagos_df, config)
    show_historial(conn, config)
    show_capturas(conn, config)


if __name__ == "__main__":
    main()
