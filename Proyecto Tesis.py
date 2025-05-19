# Se importan las bibliotecas necesarias
import PySimpleGUI as sg
import cv2
import time
from datetime import datetime
import requests
from supabase import create_client
import threading
from flask import Flask, jsonify, Response, render_template
from PIL import Image
import os

# Se crea la conexión con Supabase
url = "https://vzgoxegupsjkiwlxkete.supabase.co"
key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZ6Z294ZWd1cHNqa2l3bHhrZXRlIiwicm9sZSI6ImFub24iLCJpYXQiOjE3MzkzMzg0MjksImV4cCI6MjA1NDkxNDQyOX0.HLowX56y38MOOXNvWVpndSzqXBUqZoiksz0P_H0fauc"
supabase = create_client(url, key)


# Se configura la aplicación Flask
app = Flask(__name__)

# Variables globales para control de placa
ultima_placa_registrada = None
hora_ultimo_registro = 0

# Se abre la cámara
cap = cv2.VideoCapture(0)

# Ruta principal del servidor web
@app.route('/')
def index():
    return render_template('index.html')

# Ruta para mostrar el estado del último registro
@app.route('/estado')
def estado():
    tiempo = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(hora_ultimo_registro)) if hora_ultimo_registro else "N/A"
    return jsonify({
        "ultima_placa": ultima_placa_registrada,
        "hora_ultimo_registro": tiempo
    })

# Se generan los frames del video para transmitir por Flask
def gen_frames():
    while True:
        success, frame = cap.read()
        if not success:
            break
        else:
            ret, buffer = cv2.imencode('.jpg', frame)
            frame_bytes = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

# Ruta del video en vivo
@app.route('/video_feed')
def video_feed():
    return Response(gen_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

# Se lanza el servidor Flask
def lanzar_servidor_flask():
    app.run(port=5000, debug=False, use_reloader=False)

# Función para enviar imagen a la API de reconocimiento de placas
def leer_placa(img):
    regions = ["mx", "us-ca"]
    with open(img, 'rb') as fp:
        response = requests.post(
            'https://api.platerecognizer.com/v1/plate-reader/',
            data=dict(regions=regions),
            files=dict(upload=fp),
            headers={'Authorization': 'Token 2bebd371106a422539be68645b7b67ac46ffc319'})
    return response.json()

# Extrae el texto de la placa detectada
def capturar_placa(data):
    if data['results']:
        return data['results'][0]['plate']
    return None

# Valida si la placa es válida
def es_placa_valida(placa):
    return len(placa) == 7 and placa.isalnum()

# Recorta la imagen de la placa y la sube a Supabase
def recortar_y_subir_placa(imagen_original, nombre_destino, data):
    try:
        box = data.get("box") or data.get("results", [{}])[0].get("box", {})
        x1 = box.get("xmin")
        y1 = box.get("ymin")
        x2 = box.get("xmax")
        y2 = box.get("ymax")
        imagen = Image.open(imagen_original)
        recorte = imagen.crop((x1, y1, x2, y2))
        imagen_temporal = "placa_recortada.jpg"
        recorte.save(imagen_temporal)
        subir_imagen_storage(imagen_temporal, nombre_destino)
        os.remove(imagen_temporal)
    except Exception as e:
        print(f"Error al recortar o subir la placa: {e}")

# Sube una imagen al storage de Supabase
def subir_imagen_storage(ruta_local, nombre_destino):
    try:
        with open(ruta_local, "rb") as file:
            datos_imagen = file.read()
        supabase.storage.from_("accesos").remove([nombre_destino])
        supabase.storage.from_("accesos").upload(nombre_destino, datos_imagen, {"content-type": "image/jpeg"})
        print("Imagen subida exitosamente a Storage.")
    except Exception as e:
        print(f"Error al subir la imagen: {e}")

# Valida la placa detectada y registra el acceso
def validar_placa(data, supabase, window):
    now = datetime.now()
    now_timestamp = time.time()

    primera_placa = capturar_placa(data)
    if not primera_placa or not es_placa_valida(primera_placa):
        print("Placa inválida o no detectada.")
        return

    try:
        # Verificar si la placa está en whitelist
        consulta_whitelist = supabase.table("vehicles").select("plate").eq("plate", primera_placa).execute()
        esta_en_whitelist = bool(consulta_whitelist.data)
    except Exception as e:
        print(f"Error consultando whitelist: {e}")
        return

    if not esta_en_whitelist:
        print(f"Vehículo no autorizado: {primera_placa}")
        tipo_acceso = "Intento"
        resultado_acceso = "Denegado"
    else:
        try:
            # Buscar último registro de esta placa
            response = supabase.table("registros_accesos") \
                .select("*") \
                .eq("placa", primera_placa) \
                .order("timestamp", desc=True) \
                .limit(1) \
                .execute()

            registros = response.data

            # Buscar último registro general
            ultimo_global = supabase.table("registros_accesos") \
                .select("placa, timestamp, tipo_acceso") \
                .order("timestamp", desc=True) \
                .limit(1) \
                .execute()
            
            registrar = False
            tipo_acceso = "Entrada"  # Valor por defecto

            if not registros:
                registrar = True  # Nunca se ha registrado esta placa
            else:
                ultimo = registros[0]
                hora_ultimo_registro = datetime.fromisoformat(ultimo["timestamp"])
                tipo_anterior = ultimo["tipo_acceso"]
                segundos_desde_ultimo = (now - hora_ultimo_registro).total_seconds()

                # Comparar con la última placa registrada en general
                ultima_placa_global = ultimo_global.data[0]["placa"] if ultimo_global.data else None

                if segundos_desde_ultimo >= 120:
                    registrar = True
                elif ultima_placa_global != primera_placa:
                    registrar = True
                else:
                    print("Menos de 2 minutos y misma placa fue la última registrada. No se registra nuevamente.")
                    return

                tipo_acceso = "Salida" if tipo_anterior == "Entrada" else "Entrada"

            if registrar:
                resultado_acceso = "Permitido"

        except Exception as e:
            print(f"Error al consultar último acceso: {e}")
            return

    # Mostrar mensaje visual en interfaz
    color_fondo = 'green' if resultado_acceso == "Permitido" else 'red'
    texto_status = "Acceso Permitido" if resultado_acceso == "Permitido" else "Acceso Denegado"
    window['status_text'].update(texto_status, text_color='white', background_color=color_fondo)
    window.refresh()

    if resultado_acceso == "Permitido" and tipo_acceso == "Entrada":
        recortar_y_subir_placa("temp.jpg", "ultimo_acceso.jpg", data)


    # Datos para insertar en la base
    datos = {
        "placa": primera_placa,
        "tipo_acceso": tipo_acceso,
        "resultado": resultado_acceso,
        "observaciones": "Acceso normal" if esta_en_whitelist else "Vehículo no autorizado",
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S")
    }

    # Insertar registro
    try:
        response = supabase.table("registros_accesos").insert(datos).execute()
        if response.data:
            print(f"Registro insertado: {datos}")
    except Exception as e:
        print(f"Error insertando en base de datos: {e}")


# Se crea la interfaz gráfica con PySimpleGUI
layout = [
    [sg.Text('Imagen en tiempo real', size=(40, 1), justification='center', font='Helvetica 20')],
    [sg.Image(filename='', key='image', size=(600, 400))],
    [sg.Text('Esperando...', size=(20, 1), key='status_text', text_color='black', background_color='lightgray')],
    [sg.Button('Detener sistema', size=(14, 1), font='Helvetica 14', expand_x=True)],
]

window = sg.Window('Algoritmo de lectura', layout, location=(350, 50))

# Se lanza el servidor Flask en segundo plano
threading.Thread(target=lanzar_servidor_flask, daemon=True).start()

# Se inicia el ciclo principal del sistema
hora_ultima_captura = time.time()
while True:
    event, values = window.read(timeout=20)
    if event == 'Detener sistema' or event is None:
        break

    ret, frame = cap.read()
    if ret:
        hora_actual = time.time()
        if hora_actual - hora_ultima_captura >= 5:
            foto = "temp.jpg"
            cv2.imwrite(foto, frame)
            data = leer_placa(foto)

            if data['results']:
                threading.Thread(target=validar_placa, args=(data, supabase, window), daemon=True).start()

            hora_ultima_captura = hora_actual

        imgbytes = cv2.imencode('.png', frame)[1].tobytes()
        window['image'].update(data=imgbytes)

# Se cierra todo cuando se detiene el sistema
window.close()
cap.release()
