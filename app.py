import streamlit as st
import pandas as pd
import os
import re
from datetime import date

CONFIG_FILE = 'config.csv'
EXCEL_FILE = 'payments.xlsx'
SCREENSHOTS_DIR = 'screenshots'
DEFAULT_QI_POR_DIA = 50

SUFFIX_MAP = {
    'qi': 1,        # unidad base
    'sx': 1_000,    # 1 sx = 1.000 qi
    'sp': 1_000_000 # 1 sp = 1.000.000 qi
}

def parse_quantity(q_str: str) -> int:
    s = q_str.strip().lower().replace(' ', '')
    m = re.fullmatch(r"(\d+)([a-z]{0,2})", s)
    if not m:
        raise ValueError("Formato inválido. Ejemplo: '50qi', '20sx', '3sp' o '100' sin sufijo.")
    num, suf = m.groups()
    n = int(num)
    if suf:
        if suf not in SUFFIX_MAP:
            raise ValueError(f"Sufijo desconocido: {suf}. Usa qi, sx o sp.")
        return n * SUFFIX_MAP[suf]
    return n

if not os.path.exists(CONFIG_FILE):
    st.error(f"Falta el archivo {CONFIG_FILE}")
    st.stop()
config = pd.read_csv(CONFIG_FILE)

if not os.path.exists(EXCEL_FILE):
    df0 = pd.DataFrame(columns=['Fecha','Miembro','Dias','Cantidad','Captura'])
    with pd.ExcelWriter(EXCEL_FILE, engine='openpyxl') as writer:
        df0.to_excel(writer, sheet_name='Pagos', index=False)

pagos_df = pd.read_excel(EXCEL_FILE, sheet_name='Pagos', parse_dates=['Fecha'])
pagos_df = pd.read_excel(EXCEL_FILE, sheet_name='Pagos', parse_dates=['Fecha'])
if 'Dias' not in pagos_df:
    pagos_df['Dias'] = 1

role = st.sidebar.selectbox("¿Quién eres?", ['Miembro', 'Administrador'])

if role == 'Miembro':
    st.title("📥 Registro de tu Donación")
    miembro = st.selectbox("Selecciona tu nombre", config['Miembro'])
    q_input = st.text_input(
        "Cantidad pagada (ej: 50qi, 20sx, 3sp o sin sufijo)",
        value=str(DEFAULT_QI_POR_DIA)
    )
    try:
        cantidad = parse_quantity(q_input)
        st.write(f"Cantidad parseada: **{cantidad:,}** qi")
    except ValueError as e:
        st.error(str(e))
        st.stop()
    qi_input = st.text_input("Qi por día (ej: 50qi, 20sx, 3sp)", value=f"{DEFAULT_QI_POR_DIA}qi")
    try:
        qi_por_dia = parse_quantity(qi_input)
        st.write(f"Qi por día parseado: **{qi_por_dia:,}**")
    except ValueError as e:
        st.error(str(e))
        st.stop()
    dias = cantidad // qi_por_dia
    if cantidad % qi_por_dia != 0:
        st.warning(f"Pago no múltiplo de {qi_por_dia}; se dan {dias} días completos.")
    st.write(f"Días cubiertos: **{dias}**")
    fecha = st.date_input("Fecha del pago", value=date.today())
    archivo = st.file_uploader("📸 Comprobante (PNG/JPG)", type=['png','jpg','jpeg'])
    if st.button("Enviar Pago"):
        cap = ''
        if archivo:
            os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
            cap = f"{fecha}_{miembro}.png"
            with open(os.path.join(SCREENSHOTS_DIR, cap), 'wb') as f:
                f.write(archivo.getbuffer())
            st.success(f"Comprobante guardado: {cap}")
        fila = pd.DataFrame([{
            'Fecha': fecha,
            'Miembro': miembro,
            'Dias': dias,
            'Cantidad': cantidad,
            'Captura': cap
        }])
        pagos_df = pd.concat([pagos_df, fila], ignore_index=True)
        pagos_df.to_excel(EXCEL_FILE, sheet_name='Pagos', index=False)
        st.success("Pago enviado correctamente.")

elif role == 'Administrador':
    pwd = st.sidebar.text_input("Contraseña admin", type='password')
    stored_pwd = st.secrets["admin_password"]
    if pwd.strip() == stored_pwd.strip():
        st.title("📊 Panel de Administración")
        st.header("📅 Resumen de Pagos")
        pivot = pagos_df.pivot_table(
            index='Fecha', columns='Miembro', values='Cantidad', aggfunc='sum'
        ).fillna(0).sort_index(ascending=False)
        def format_qty(n):
            try:
                n = int(n)
            except ValueError:
                return n
            if n % SUFFIX_MAP['sp'] == 0:
                return f"{n // SUFFIX_MAP['sp']}sp"
            if n % SUFFIX_MAP['sx'] == 0:
                return f"{n // SUFFIX_MAP['sx']}sx"
            return f"{n}qi"
        formatted = pivot.applymap(format_qty)
        st.dataframe(formatted)

        st.header("⏳ Pagos pendientes hoy")
        pagos_df['Expiracion'] = pagos_df['Fecha'] + pd.to_timedelta(pagos_df['Dias'], unit='D')
        ult = pagos_df.groupby('Miembro')['Expiracion'].max().reset_index()
        hoy = pd.Timestamp(date.today())
        pend = [m for m in config['Miembro'] if (
            ult[ult['Miembro']==m]['Expiracion'].empty or
            ult[ult['Miembro']==m]['Expiracion'].iloc[0] < hoy
        )]
        if pend:
            st.warning("Tienen que pagar hoy: " + ", ".join(pend))
        else:
            st.success("¡Todos al día! 🎉")
    else:
        st.error("🔒 Contraseña incorrecta.")