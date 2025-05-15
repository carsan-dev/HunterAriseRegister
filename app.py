import streamlit as st
import pandas as pd
import os
from datetime import date

CONFIG_FILE = 'config.csv'
EXCEL_FILE = 'payments.xlsx'
SCREENSHOTS_DIR = 'screenshots'
DEFAULT_QI_POR_DIA = 50

def load_config():
    if not os.path.exists(CONFIG_FILE):
        st.error(f"Falta el archivo de configuración {CONFIG_FILE}")
        st.stop()
    return pd.read_csv(CONFIG_FILE)

config = load_config()

if not os.path.exists(EXCEL_FILE):
    df0 = pd.DataFrame(columns=['Fecha','Miembro','Dias','Cantidad','Captura'])
    with pd.ExcelWriter(EXCEL_FILE, engine='openpyxl') as writer:
        df0.to_excel(writer, sheet_name='Pagos', index=False)

pagos_df = pd.read_excel(EXCEL_FILE, sheet_name='Pagos', parse_dates=['Fecha'])
if 'Dias' not in pagos_df.columns:
    pagos_df['Dias'] = 1

role = st.sidebar.selectbox("¿Quién eres?", ['Miembro', 'Administrador'])

if role == 'Miembro':
    st.title("📥 Registro de tu Donación")
    miembro = st.selectbox("Selecciona tu nombre", config['Miembro'])
    cantidad = st.number_input("Cantidad pagada (qi)", min_value=0, step=1, value=DEFAULT_QI_POR_DIA)
    qi_por_dia = st.number_input("Qi por día", min_value=1, step=1, value=DEFAULT_QI_POR_DIA)
    dias = cantidad // qi_por_dia
    if cantidad % qi_por_dia != 0:
        st.warning(f"El pago no es múltiplo de {qi_por_dia} qi; se asignarán {dias} días completos.")
    st.write(f"Días cubiertos: **{dias}**")
    fecha = st.date_input("Fecha del pago", value=date.today())

    archivo = st.file_uploader("📸 Subir comprobante (PNG/JPG)", type=['png','jpg','jpeg'])
    if st.button("Enviar Pago"):
        cap = ''
        if archivo:
            os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
            cap = f"{fecha}_{miembro}.png"
            ruta = os.path.join(SCREENSHOTS_DIR, cap)
            with open(ruta, 'wb') as f:
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
        with pd.ExcelWriter(EXCEL_FILE, engine='openpyxl', mode='w') as w:
            pagos_df.to_excel(w, sheet_name='Pagos', index=False)
        st.success("Pago enviado correctamente. ¡Gracias!")

elif role == 'Administrador':
    pwd = st.sidebar.text_input("Contraseña de administrador", type='password')
    if pwd == st.secrets.get('admin_password'):
        st.title("📊 Panel de Administración")
        st.header("📅 Resumen de Pagos")
        pivot = (
            pagos_df.pivot_table(index='Fecha', columns='Miembro', values='Cantidad', aggfunc='sum')
            .fillna(0).sort_index(ascending=False)
        )
        st.dataframe(pivot)

        st.header("⏳ Pagos pendientes hoy")
        pagos_df['Expiracion'] = pagos_df['Fecha'] + pd.to_timedelta(pagos_df['Dias'], unit='D')
        ult = pagos_df.groupby('Miembro')['Expiracion'].max().reset_index()
        hoy = pd.Timestamp(date.today())
        pend = [m for m in config['Miembro'] if (ult[ult['Miembro']==m]['Expiracion'].empty or ult[ult['Miembro']==m]['Expiracion'].iloc[0] < hoy)]
        if pend:
            st.warning("Tienen que pagar hoy: " + ", ".join(pend))
        else:
            st.success("¡Todos al día! 🎉")
    else:
        st.error("🔒 Acceso denegado. Contraseña incorrecta.")