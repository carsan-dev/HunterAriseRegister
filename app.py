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
from urllib.parse import urlencode

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
    ascii_nombre = miembro.encode("ascii", "ignore").decode()
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


def _get_authenticated_user():
    if "user_id" in st.session_state and "nick" in st.session_state:
        return st.session_state["user_id"], st.session_state["nick"]
    return None


def _prompt_discord_login():
    params = {
        "client_id": st.secrets["DISCORD_CLIENT_ID"],
        "redirect_uri": st.secrets["DISCORD_REDIRECT_URI"],
        "response_type": "code",
        "scope": "identify",
    }
    url = "https://discord.com/api/oauth2/authorize?" + urlencode(params)
    st.markdown(f"[🔐 Iniciar sesión con Discord]({url})")


def _exchange_code_for_token(code):
    data = {
        "client_id": st.secrets["DISCORD_CLIENT_ID"],
        "client_secret": st.secrets["DISCORD_CLIENT_SECRET"],
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": st.secrets["DISCORD_REDIRECT_URI"],
    }
    resp = requests.post(
        "https://discord.com/api/oauth2/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        st.query_params = {}
        st.error("Token exchange falló:\n" + resp.text)
        return None
    st.query_params = {}
    return resp.json().get("access_token")


def _fetch_discord_user(token):
    return requests.get(
        "https://discord.com/api/users/@me",
        headers={"Authorization": f"Bearer {token}"},
    ).json()


def _fetch_guild_member(user_id):
    return requests.get(
        f"https://discord.com/api/v10/guilds/{st.secrets['DISCORD_GUILD_ID']}/members/{user_id}",
        headers={"Authorization": f"Bot {st.secrets['DISCORD_BOT_TOKEN']}"},
    ).json()


def _member_has_role(member):
    return st.secrets["DISCORD_ROLE_ID"] in member.get("roles", [])


def _finalize_auth(user, member):
    nick = member.get("nick") or user["username"]
    st.session_state["user_id"] = user["id"]
    st.session_state["nick"] = nick
    return user["id"], nick


def authenticate_discord():
    existing = _get_authenticated_user()
    if existing:
        return existing
    code = st.query_params.get("code", [None])[0]
    if not code:
        _prompt_discord_login()
        st.stop()
    access_token = _exchange_code_for_token(code)
    if not access_token:
        st.stop()
    user = _fetch_discord_user(access_token)
    member = _fetch_guild_member(user["id"])
    if not _member_has_role(member):
        st.stop()
    return _finalize_auth(user, member)


def load_payments_for(user_id):
    df = load_payments()
    return df[df["Miembro"] == user_id]


def _render_payment_form(user_id):
    fecha = st.date_input("Fecha de pago", datetime.now(tz=ESP).date())
    paid_str = st.text_input(
        "Cantidad pagada", value="1sx", help="Ejemplo: 1sx, 1.5sp, 500qi"
    )
    rate_str = st.text_input(
        "SX por día", value="1sx", help="Ejemplo: 1sx, 1.5sp, 500qi"
    )
    captura = st.file_uploader("Captura")
    if not st.button("Registrar pago"):
        return
    paid_qi = parse_quantity(paid_str)
    rate_qi = parse_quantity(rate_str)
    if rate_qi <= 0:
        st.error("El coste diario debe ser mayor que 0.")
        return
    dias = round(paid_qi / rate_qi, 1)
    if dias < 1:
        st.error("La cantidad pagada es insuficiente para generar al menos 1 día.")
        return
    if not captura:
        st.error("Debes subir una captura.")
        return
    filename = upload_capture_to_storage(fecha, user_id, captura)
    save_payment(fecha, user_id, dias, paid_qi, filename)
    st.success("Pago registrado.")


def _compute_payment_stats(payments):
    total_qi = payments["Cantidad"].sum()
    exp = compute_expiry(payments)
    today = datetime.now(tz=ESP).date()
    days_left = max((exp.date() - today).days, 0)
    days_over = max((today - exp.date()).days, 0)
    return total_qi, days_left, days_over


def _left_label(days_left, days_over):
    if days_left > 0:
        return days_left
    if days_over > 0:
        return "Llevas retraso de donaciones"
    return "Vas al día"


def _render_metrics(total_qi, days_left, days_over):
    c1, c2, c3 = st.columns(3)
    c1.metric("Total pagado", format_quantity(total_qi))
    c2.metric("Días restantes", _left_label(days_left, days_over))
    c3.metric("Días de atraso", days_over)


def _render_history_table(payments):
    df = payments.copy()
    df["Cantidad"] = df["Cantidad"].apply(format_quantity)
    df["Dias"] = df["Dias"].apply(
        lambda d: int(d) if float(d).is_integer() else round(d, 1)
    )
    st.subheader("Historial de pagos")
    st.table(df[["Fecha", "Dias", "Cantidad"]])


def member_view_authenticated():
    user_id, nick = authenticate_discord()
    st.write(f"👋 Hola **{nick}**")
    payments = load_payments_for(user_id)
    _render_payment_form(user_id)
    if payments.empty:
        st.info("Aún no tienes pagos registrados.")
        return
    total_qi, days_left, days_over = _compute_payment_stats(payments)
    _render_metrics(total_qi, days_left, days_over)
    _render_history_table(payments)


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
    if pagos_df.empty:
        lo = hi = date.today()
    else:
        lo, hi = pagos_df["Fecha"].min(), pagos_df["Fecha"].max()
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
        member_view_authenticated()
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
