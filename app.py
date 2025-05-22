from logging import config
import streamlit as st
import pandas as pd
import os
import re
import requests
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh
from supabase import create_client

CONFIG_FILE = "config.csv"
ESP = ZoneInfo("Europe/Madrid")
SUFFIX_MAP = {"qi": 1, "sx": 1000, "sp": 1000000}
RAW_COLS = ["Fecha", "Miembro", "Dias", "Cantidad", "Captura"]

SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_ANON_KEY = st.secrets["SUPABASE_KEY"]
SUPABASE_SERVICE_KEY = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", SUPABASE_ANON_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
supabase_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
BUCKET = "screenshots"


def init_session():
    if "admin_pw" not in st.session_state:
        st.session_state["admin_pw"] = ""
    if "last_count" not in st.session_state:
        st.session_state["last_count"] = 0
    if "pending_notifications" not in st.session_state:
        st.session_state["pending_notifications"] = []


init_session()


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


def load_config():
    token = st.secrets["DISCORD_BOT_TOKEN"]
    guild_id = st.secrets["DISCORD_GUILD_ID"]
    role_id = st.secrets["DISCORD_ROLE_ID"]
    url = f"https://discord.com/api/v10/guilds/{guild_id}/members"
    headers = {"Authorization": f"Bot {token}"}
    params = {"limit": 1000}
    rows = []

    while True:
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        for m in data:
            if role_id in m.get("roles", []):
                u = m["user"]
                nick = m.get("nick") or u["username"]
                rows.append({"user_id": u["id"], "nick": nick})
        if len(data) < 1000:
            break
        params["after"] = data[-1]["user"]["id"]

    return pd.DataFrame(rows)


def load_payments():
    res = (
        supabase_admin.from_("pagos")
        .select("id, fecha, miembro, dias, cantidad, captura")
        .execute()
    )
    data = res.data or []
    df = pd.DataFrame(
        data, columns=["id", "fecha", "miembro", "dias", "cantidad", "captura"]
    )
    df = df.rename(
        columns={
            "fecha": "Fecha",
            "miembro": "Miembro",
            "dias": "Dias",
            "cantidad": "Cantidad",
            "captura": "Captura",
        }
    )
    df["Fecha"] = pd.to_datetime(df["Fecha"], errors="coerce").dt.date
    return df


def save_payment(fecha, miembro, dias, cantidad, captura_path):
    supabase_admin.from_("pagos").insert(
        {
            "fecha": fecha.isoformat(),
            "miembro": miembro,
            "dias": dias,
            "cantidad": cantidad,
            "captura": captura_path,
        }
    ).execute()


def delete_all_and_insert(df_full):
    supabase_admin.from_("pagos").delete().neq("id", 0).execute()
    records = [
        {
            "fecha": row["Fecha"].isoformat(),
            "miembro": row["Miembro"],
            "dias": row["Dias"],
            "cantidad": row["Cantidad"],
            "captura": row["Captura"],
        }
        for _, row in df_full.iterrows()
    ]
    if records:
        supabase_admin.from_("pagos").insert(records).execute()


def compute_expiry(group):
    exp = None
    for f, d in zip(group["Fecha"], group["Dias"]):
        f = pd.to_datetime(f)
        if exp is None or f > exp:
            exp = f + pd.to_timedelta(d - 1, unit="d")
        else:
            exp += pd.to_timedelta(d, unit="d")
    return exp


def upload_capture_to_storage(fecha, miembro, captura):
    ext = os.path.splitext(captura.name)[1] if hasattr(captura, "name") else ".png"
    raw = miembro
    ascii_nombre = raw.encode("ascii", "ignore").decode()
    safe_nombre = re.sub(r"[^A-Za-z0-9_-]", "_", ascii_nombre)[:50]
    uid = uuid.uuid4().hex
    fecha_str = fecha.strftime("%Y%m%d")
    if safe_nombre:
        filename = f"{fecha_str}_{safe_nombre}_{uid}{ext}"
    else:
        filename = f"{fecha_str}_{uid}{ext}"

    tmp_dir = "/tmp/supabase_uploads"
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, filename)
    with open(tmp_path, "wb") as f:
        f.write(captura.getbuffer())

    supabase_admin.storage.from_(BUCKET).upload(filename, tmp_path)
    try:
        os.remove(tmp_path)
    except OSError:
        pass

    return filename


def get_signed_url(path, expires=3600):
    url_data = supabase_admin.storage.from_(BUCKET).create_signed_url(path, expires)
    return url_data.get("signedURL", "")


def member_view(config):
    choice = st.selectbox("Tu nombre", options=config["nick"].tolist())
    user_id = config.loc[config["nick"] == choice, "user_id"].iat[0]

    fecha = st.date_input("Fecha de la donación", datetime.now(tz=ESP).date())
    cantidad_str = st.text_input("Cantidad pagada (ej. 1sx)", "1sx")
    qi_dia_str = st.text_input("Sx por día (ej. 1sx)", "1sx")
    try:
        q = parse_quantity(cantidad_str)
        qd = parse_quantity(qi_dia_str)
        est = q / qd if qd > 0 else 0
        days_str = str(int(est)) if float(est).is_integer() else f"{est:.2f}"
        st.info(f"{format_quantity(q)} equivale a {days_str} día(s)")
    except ValueError:
        st.error("Error al calcular la cantidad.")

    captura = st.file_uploader("Comprobante (PNG/JPG)", type=["png", "jpg", "jpeg"])
    if st.button("Registrar pago"):
        try:
            q = parse_quantity(cantidad_str)
            dias = q / parse_quantity(qi_dia_str)
            if dias < 1:
                st.error("La cantidad no cubre ni un día")
            else:
                captura_path = ""
                if captura:
                    captura_path = upload_capture_to_storage(fecha, user_id, captura)
                save_payment(fecha, user_id, dias, q, captura_path)
                st.success("✅ Pago registrado")
        except ValueError:
            st.error("Error al registrar.")
    st.stop()


def show_notifications(pagos_df):
    new_count = len(pagos_df)
    if new_count > st.session_state["last_count"]:
        ps = pagos_df.sort_values("Fecha").reset_index(drop=True)
        for i in range(st.session_state["last_count"], new_count):
            p = ps.iloc[i]
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
            nick = config.loc[config["user_id"] == n["Miembro"], "nick"].iat[0]
            n["placeholder"].info(
                f"🔔 Pago: **{nick}** — {format_quantity(n['Cantidad'])} ({n['Dias']} días)"
            )
            kept.append(n)
        else:
            n["placeholder"].empty()
    st.session_state["pending_notifications"] = kept


def admin_dashboard(pagos_df, config):
    st.header("🔑 Panel de Administración")
    today = datetime.now(tz=ESP).date()
    rows = []
    for uid, nick in zip(config["user_id"], config["nick"]):
        grp = pagos_df[pagos_df["Miembro"] == uid]
        if grp.empty:
            rows.append(
                {
                    "Miembro": nick,
                    "Días restantes": "Sin pagos",
                    "Días atraso": "Sin pagos",
                }
            )
        else:
            exp = compute_expiry(grp)
            left = max((exp.date() - today).days, 0)
            over = max((today - exp.date()).days, 0)
            rows.append({"Miembro": nick, "Días restantes": left, "Días atraso": over})
    df = pd.DataFrame(rows)
    st.subheader("📋 Estado de miembros")
    st.table(df)


def show_historial(config):
    pagos_df = load_payments()
    pagos_df["nick"] = pagos_df["Miembro"].map(
        lambda uid: (
            config.loc[config["user_id"] == uid, "nick"].iat[0]
            if uid in config["user_id"].values
            else uid
        )
    )
    st.subheader("🗂️ Historial de pagos")
    members = ["Todos"] + sorted(config["nick"].unique())
    sel = st.selectbox("Filtrar por miembro", members, key="hist_member")
    if pagos_df.empty:
        lo = hi = date.today()
    else:
        lo, hi = pagos_df["Fecha"].min(), pagos_df["Fecha"].max()
    dates = st.date_input("Rango de fechas", [lo, hi], key="hist_dates")
    if not isinstance(dates, (list, tuple)) or len(dates) != 2:
        st.error("Seleccione rango")
        return
    df = pagos_df[(pagos_df["Fecha"] >= dates[0]) & (pagos_df["Fecha"] <= dates[1])]
    if sel != "Todos":
        df = df[df["nick"] == sel]

    df_edit = df.copy()
    df_edit["Cantidad_fmt"] = df_edit["Cantidad"].apply(format_quantity)
    df_edit["Eliminar"] = False
    df_edit["Miembro_nick"] = df_edit["nick"]
    editor = st.data_editor(
        df_edit[
            [
                "id",
                "Fecha",
                "Miembro_nick",
                "Dias",
                "Cantidad_fmt",
                "Captura",
                "Eliminar",
            ]
        ],
        column_config={
            "id": {"hidden": True},
            "Miembro_nick": {"title": "Miembro", "disabled": True},
            "Captura": {"disabled": True},
            "Cantidad_fmt": {"title": "Cantidad"},
        },
        use_container_width=True,
        key="hist_editor",
    )
    if st.button("Guardar cambios", key="hist_save"):
        keep_ids = editor.loc[~editor["Eliminar"], "id"]
        orig = load_payments()
        outside = orig[~orig["id"].isin(df["id"])]
        keep = orig[orig["id"].isin(keep_ids)]
        new_full = pd.concat([outside[RAW_COLS], keep[RAW_COLS]], ignore_index=True)
        delete_all_and_insert(new_full)
        st.success("📝 Cambios guardados")


def show_capturas(config):
    pagos_df = load_payments()
    pagos_df["nick"] = pagos_df["Miembro"].map(
        lambda uid: (
            config.loc[config["user_id"] == uid, "nick"].iat[0]
            if uid in config["user_id"].values
            else uid
        )
    )
    st.subheader("📸 Capturas de pagos")
    members = ["Todos"] + sorted(config["nick"].unique())
    sel = st.selectbox("Mostrar capturas de:", members, key="cap_member")
    if sel != "Todos":
        pagos_df = pagos_df[pagos_df["nick"] == sel]
    pagos_df = pagos_df.sort_values("Fecha", ascending=False)
    if "show_all" not in st.session_state:
        st.session_state["show_all"] = False
    btn = "Mostrar todas" if not st.session_state["show_all"] else "Ocultar"
    if st.button(btn, key="cap_toggle"):
        st.session_state["show_all"] = not st.session_state["show_all"]
    display = pagos_df if st.session_state["show_all"] else pagos_df.head(5)
    for _, r in display.iterrows():
        if not r["Captura"]:
            continue
        url = get_signed_url(r["Captura"])
        c1, c2 = st.columns([1, 3])
        with c1:
            st.image(url, width=100)
        with c2:
            st.markdown(f"**Miembro:** {r['nick']}")
            st.markdown(f"**Fecha:** {r['Fecha']}")
            st.markdown(f"**Cantidad:** {format_quantity(r['Cantidad'])}")
            with st.expander("🔍 Ampliar captura"):
                st.image(url, use_container_width=True)
        st.markdown("---")


def main():
    config = load_config()
    st.set_page_config(layout="wide")
    st.title("💰 Control de Donaciones de HunterArise")
    role = st.sidebar.selectbox("¿Quién eres?", ["Miembro", "Administrador"])
    st.sidebar.text_input(
        "Contraseña admin",
        type="password",
        value=st.session_state["admin_pw"],
        key="admin_pw",
    )
    st.sidebar.button("🔄 Refrescar datos")

    if role == "Miembro":
        member_view(config)
        return
    if st.session_state["admin_pw"] != st.secrets.get("admin_password", ""):
        st.error("Acceso denegado")
        return
    st.sidebar.success("👑 Acceso admin concedido")
    st_autorefresh(interval=30000, key="datarefresh")
    pagos_df = load_payments()
    show_notifications(pagos_df)
    admin_dashboard(pagos_df, config)
    show_historial(config)
    show_capturas(config)


if __name__ == "__main__":
    main()
