import streamlit as st
import pandas as pd
import os
import random
import string
import requests
import re
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh
from supabase import create_client

ESP = ZoneInfo("Europe/Madrid")
SUFFIX_MAP = {"qi": 1, "sx": 1000, "sp": 1000000}
RAW_COLS = ["Fecha", "Miembro", "Dias", "Cantidad", "Captura"]

supabase = create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])
supabase_admin = create_client(
    st.secrets["SUPABASE_URL"],
    st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", st.secrets["SUPABASE_KEY"]),
)
BUCKET = "screenshots"

if "admin_pw" not in st.session_state:
    st.session_state["admin_pw"] = ""


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


def compute_expiry(group):
    exp = None
    for f, d in zip(group["Fecha"], group["Dias"]):
        f = pd.to_datetime(f)
        if exp is None or f > exp:
            exp = f + pd.to_timedelta(d - 1, unit="d")
        else:
            exp += pd.to_timedelta(d, unit="d")
    return exp


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


def upload_capture_to_storage(fecha, miembro, captura):
    ext = os.path.splitext(captura.name)[1] if hasattr(captura, "name") else ".png"
    ascii_nombre = str(miembro).encode("ascii", "ignore").decode()
    safe_nombre = re.sub(r"[^A-Za-z0-9_-]", "_", ascii_nombre)[:50]
    uid = uuid.uuid4().hex
    fecha_str = fecha.strftime("%Y%m%d")
    filename = (
        f"{fecha_str}_{safe_nombre}_{uid}{ext}"
        if safe_nombre
        else f"{fecha_str}_{uid}{ext}"
    )
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


def save_payment(fecha, miembro, dias, cantidad, captura):
    supabase_admin.from_("pagos").insert(
        {
            "fecha": fecha.isoformat(),
            "miembro": miembro,
            "dias": dias,
            "cantidad": cantidad,
            "captura": captura,
        }
    ).execute()


def start_challenge():
    if st.session_state.get("step", 1) != 1:
        return
    with st.form("start_challenge", clear_on_submit=True):
        user_id = st.text_input("🔑 Escribe tu Discord user ID")
        submit = st.form_submit_button("Enviar")
    if submit and user_id:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        st.session_state["challenge"] = code
        st.session_state["candidate_id"] = user_id
        st.session_state["step"] = 2
        send_challenge_dm(user_id, code)
        st.stop()


def send_challenge_dm(user_id, code):
    token = st.secrets["DISCORD_BOT_TOKEN"]
    dm = requests.post(
        "https://discord.com/api/v10/users/@me/channels",
        headers={"Authorization": f"Bot {token}"},
        json={"recipient_id": user_id},
    ).json()
    channel_id = dm["id"]
    msg = requests.post(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        headers={"Authorization": f"Bot {token}"},
        json={"content": f"Tu código de autenticación es: **{code}**"},
    ).json()
    st.session_state["challenge_channel_id"] = channel_id
    st.session_state["challenge_message_id"] = msg["id"]


def verify_challenge():
    if st.session_state.get("step") != 2:
        return
    with st.form("verify_challenge", clear_on_submit=True):
        entry = st.text_input("📩 Escribe el código que recibiste por DM")
        submit = st.form_submit_button("Verificar")
    if not submit:
        return
    if entry != st.session_state.get("challenge", ""):
        st.error("Código incorrecto. Reintenta.")
        st.stop()
    token = st.secrets["DISCORD_BOT_TOKEN"]
    guild_id = st.secrets["DISCORD_GUILD_ID"]
    uid = st.session_state["candidate_id"]
    member = requests.get(
        f"https://discord.com/api/v10/guilds/{guild_id}/members/{uid}",
        headers={"Authorization": f"Bot {token}"},
    ).json()
    if st.secrets["DISCORD_ROLE_ID"] not in member.get("roles", []):
        st.error("No tienes el rol requerido.")
        st.stop()
    channel_id = st.session_state.get("challenge_channel_id")
    message_id = st.session_state.get("challenge_message_id")
    if channel_id and message_id:
        requests.delete(
            f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}",
            headers={"Authorization": f"Bot {token}"},
        )
    nick = member.get("nick") or member["user"]["username"]
    st.session_state["user_id"] = uid
    st.session_state["nick"] = nick
    return uid, nick


def authenticate_discord():
    if "user_id" in st.session_state and "nick" in st.session_state:
        return st.session_state["user_id"], st.session_state["nick"]
    st.session_state.setdefault("step", 1)
    if st.session_state["step"] == 1:
        start_challenge()
    if st.session_state["step"] == 2:
        result = verify_challenge()
        if result:
            return result
    st.stop()


def render_payment_form(user_id, nick, config):
    st.write(f"👋 Hola **{nick}**")
    fecha = st.date_input("Fecha de pago", datetime.now(tz=ESP).date())
    opciones = ["Yo"] + sorted(config["nick"].tolist())
    seleccion = st.multiselect("Pagar donación para", opciones, default=["Yo"])
    id_map = {n: u for u, n in zip(config["user_id"], config["nick"])}
    paid_str = st.text_input(
        "Cantidad pagada", value="1sx", help="Ejemplo: 1sx, 1.5sp, 500qi"
    )
    rate_str = st.text_input(
        "SX por día", value="1sx", help="Ejemplo: 1sx, 1.5sp, 500qi"
    )
    try:
        paid_qi = parse_quantity(paid_str)
        rate_qi = parse_quantity(rate_str)
        if rate_qi > 0:
            dias_calc = paid_qi / rate_qi
            dias_disp = (
                int(dias_calc) if float(dias_calc).is_integer() else round(dias_calc, 2)
            )
            st.info(f"Corresponde a {dias_disp} días")
    except ValueError:
        pass
    captura = st.file_uploader("Captura")
    if st.button("Registrar pago"):
        for destino in seleccion:
            dest_id = user_id if destino == "Yo" else id_map[destino]
            handle_new_payment(fecha, dest_id, paid_str, rate_str, captura)


def handle_new_payment(fecha, user_id, paid_str, rate_str, captura):
    paid_qi = parse_quantity(paid_str)
    rate_qi = parse_quantity(rate_str)
    if rate_qi <= 0:
        st.error("El coste diario debe ser mayor que 0.")
        return
    dias = round(paid_qi / rate_qi, 1)
    if dias < 1:
        st.error("La cantidad pagada es insuficiente para generar al menos 1 día.")
    elif not captura:
        st.error("Debes subir una captura.")
    else:
        filename = upload_capture_to_storage(fecha, user_id, captura)
        save_payment(fecha, user_id, dias, paid_qi, filename)
        st.success("Pago registrado.")


def compute_and_show_metrics(user_payments, today):
    if user_payments.empty:
        st.info("No hay pagos registrados")
        return
    total_paid_qi = user_payments["Cantidad"].sum()
    exp = compute_expiry(user_payments)
    dias_rest = max((exp.date() - today).days, 0)
    dias_atra = max((today - exp.date()).days, 0)
    if dias_rest > 0:
        rest_display = dias_rest
    elif dias_atra == 0:
        rest_display = "Vas al día"
    else:
        rest_display = "Llevas retraso de donaciones"
    c1, c2, c3 = st.columns(3)
    c1.metric("Total pagado", format_quantity(total_paid_qi))
    c2.metric("Días restantes", rest_display)
    c3.metric("Días de atraso", dias_atra)


def render_payment_history(user_payments):
    if user_payments.empty:
        st.table(pd.DataFrame([], columns=["Fecha", "Dias", "Cantidad"]))
        return
    df = user_payments.copy()
    df["Cantidad"] = df["Cantidad"].apply(format_quantity)
    df["Dias"] = df["Dias"].apply(
        lambda d: int(d) if float(d).is_integer() else round(d, 1)
    )
    st.subheader("Historial de pagos")
    st.table(df[["Fecha", "Dias", "Cantidad"]])


def member_view_authenticated(config):
    user_id, nick = authenticate_discord()
    pagos_df = load_payments()
    user_payments = pagos_df[pagos_df["Miembro"] == user_id]
    today = datetime.now(tz=ESP).date()
    render_payment_form(user_id, nick, config)
    compute_and_show_metrics(user_payments, today)
    render_payment_history(user_payments)


def show_notifications(pagos_df, config):
    id_to_nick = dict(zip(config["user_id"], config["nick"]))
    if "last_count" not in st.session_state:
        st.session_state["last_count"] = len(pagos_df)
        st.session_state["pending_notifications"] = []
    new_count = len(pagos_df)
    if new_count > st.session_state["last_count"]:
        ps = pagos_df.sort_values("Fecha").reset_index(drop=True)
        for i in range(st.session_state["last_count"], new_count):
            p = ps.iloc[i]
            ph = st.sidebar.empty()
            nick = id_to_nick.get(p["Miembro"], p["Miembro"])
            st.session_state["pending_notifications"].append(
                {
                    "time": datetime.now(tz=ESP),
                    "nick": nick,
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
                f"🔔 Pago: **{n['nick']}** — {format_quantity(n['Cantidad'])} ({n['Dias']} días)"
            )
            kept.append(n)
        else:
            n["placeholder"].empty()
    st.session_state["pending_notifications"] = kept


def admin_dashboard(pagos_df, config):
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
            rows.append(
                {
                    "Miembro": nick,
                    "Días restantes": max((exp.date() - today).days, 0),
                    "Días atraso": max((today - exp.date()).days, 0),
                }
            )
    df = pd.DataFrame(rows)
    st.subheader("📋 Estado de miembros")
    st.table(df)


def show_historial(config):
    pagos_df = load_payments()
    st.subheader("🗂️ Historial de pagos")
    members = ["Todos"] + sorted(config["nick"].tolist())
    sel = st.selectbox("Filtrar por miembro", members, key="hist_member")
    lo, hi = (
        (date.today(), date.today())
        if pagos_df.empty
        else (pagos_df["Fecha"].min(), pagos_df["Fecha"].max())
    )
    dates = st.date_input("Rango de fechas", [lo, hi], key="hist_dates")
    if len(dates) != 2:
        st.error("Seleccione rango")
        return
    df = pagos_df[(pagos_df["Fecha"] >= dates[0]) & (pagos_df["Fecha"] <= dates[1])]
    if sel != "Todos":
        uid = config.loc[config["nick"] == sel, "user_id"].iat[0]
        df = df[df["Miembro"] == uid]
    df_edit = df.copy()
    df_edit["Cantidad_fmt"] = df_edit["Cantidad"].apply(format_quantity)
    df_edit["Eliminar"] = False
    id_to_nick = dict(zip(config["user_id"], config["nick"]))
    df_edit["nick"] = df_edit["Miembro"].map(id_to_nick).fillna(df_edit["Miembro"])
    edited = st.data_editor(
        df_edit[
            [
                "id",
                "Miembro",
                "nick",
                "Fecha",
                "Dias",
                "Cantidad_fmt",
                "Captura",
                "Eliminar",
            ]
        ],
        column_config={
            "id": {"hidden": True},
            "Miembro": {"hidden": True},
            "nick": {"title": "Miembro"},
            "Cantidad_fmt": {"title": "Cantidad"},
            "Captura": {"disabled": True},
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
    members = ["Todos"] + sorted(config["nick"].tolist())
    sel = st.selectbox("Mostrar capturas de:", members, key="cap_member")
    df = (
        pagos_df
        if sel == "Todos"
        else pagos_df[
            pagos_df["Miembro"] == config.loc[config["nick"] == sel, "user_id"].iat[0]
        ]
    )
    df = df.sort_values("Fecha", ascending=False)
    if "show_all" not in st.session_state:
        st.session_state["show_all"] = False
    if st.button(
        "Mostrar todas" if not st.session_state["show_all"] else "Ocultar",
        key="cap_toggle",
    ):
        st.session_state["show_all"] = not st.session_state["show_all"]
    display_df = df if st.session_state["show_all"] else df.head(5)
    id_to_nick = dict(zip(config["user_id"], config["nick"]))
    for _, r in display_df.iterrows():
        if not r["Captura"]:
            continue
        url = get_signed_url(r["Captura"])
        c1, c2 = st.columns([1, 3])
        nick = id_to_nick.get(r["Miembro"], r["Miembro"])
        with c1:
            st.image(url, width=100)
        with c2:
            st.markdown(f"**Miembro:** {nick}")
            st.markdown(f"**Fecha:** {r['Fecha']}")
            st.markdown(f"**Cantidad:** {format_quantity(r['Cantidad'])}")
            with st.expander("🔍 Ampliar captura"):
                st.image(url, use_container_width=True)
        st.markdown("---")


def main():
    config = load_config()
    st.set_page_config(layout="wide")
    st.title("💰 Control de Donaciones")
    role = st.sidebar.selectbox("¿Quién eres?", ["Miembro", "Administrador"])
    st.sidebar.text_input(
        "Contraseña admin",
        type="password",
        value=st.session_state["admin_pw"],
        key="admin_pw",
    )
    st.sidebar.button("🔄 Refrescar datos", key="refresh")
    if role == "Miembro":
        member_view_authenticated(config)
        return
    if st.session_state["admin_pw"] != st.secrets.get("admin_password", ""):
        st.error("Acceso denegado")
        return
    st.sidebar.success("👑 Acceso admin concedido")
    st_autorefresh(interval=30000, key="datarefresh")
    pagos_df = load_payments()
    show_notifications(pagos_df, config)
    admin_dashboard(pagos_df, config)
    show_historial(config)
    show_capturas(config)


if __name__ == "__main__":
    main()
