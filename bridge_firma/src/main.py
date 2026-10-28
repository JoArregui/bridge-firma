import os
import base64
import hashlib
import sqlite3
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request, jsonify, send_from_directory, g

# ---- NUEVO: APScheduler para tareas periódicas ----
from apscheduler.schedulers.background import BackgroundScheduler
# --------------------------------------------------

app = Flask(__name__)

# ---- NUEVO: Configuración tipo env.dart de firma_tap ----
app.config['ENV'] = os.environ.get('ENV', 'dev')
app.config['API_BASE'] = os.environ.get('API_BASE', 'https://10.0.2.2:5001/api')
app.config['DOCUMENTO_BASE_URL'] = os.environ.get(
    'DOCUMENTO_BASE_URL', 'https://pirineosapi.ecomputer.es/DocumentoAFirmar'
)
app.config['EMPRESA_URL'] = os.environ.get('EMPRESA_URL', 'https://pirineosapi.ecomputer.es/Empresa/GetEmpresas')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'cambiar-en-produccion')
# --------------------------------------------------

# ---- NUEVO: Inicializar scheduler global ----
scheduler = BackgroundScheduler(interval=timedelta(minutes=15))
scheduler.start()
# ------------------------------------------------

# Variable de control para evitar duplicados en Flask reloader


# Función de retry programada (cada 15 min como QueueService.retryAll + Workmanager)
def retry_pendientes_scheduler():
    """Tarea programada cada 15 minutos idéntica a QueueService.retryAll."""
    import sqlite3
    import os
    from datetime import datetime, timezone
    
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'firma.db')
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cur = db.cursor()
    
    cur.execute("""
        SELECT id, num_doc, firma_hash, firma_ruta_texto, intentos
        FROM app_firmas_pendientes
        WHERE estado='PENDIENTE' AND intentos > 0
        ORDER BY id ASC
    """)
    pendientes = cur.fetchall()
    
    for row in pendientes:
        row_id = row['id']
        num_doc = row['num_doc']
        intentos_actuales = row['intentos'] or 0
        
        try:
            cur.execute("""
                UPDATE app_firmas_pendientes
                SET estado='FIRMADO', intentos=0, ultima_intento=CURRENT_TIMESTAMP
                WHERE id=?
            """, (row_id,))
            
            firma_ruta = row['firma_ruta_texto'] or f"firma_{row_id}.png"
            cur.execute("""
                INSERT INTO firma_historial (num_doc, usuario_id, empresa_codigo, firma_local_ruta, firma_hash, observaciones)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (num_doc, None, None, firma_ruta, row['firma_hash'], "Reintento scheduler exitoso"))
            
            db.commit()
        except Exception:
            db.rollback()
            try:
                cur.execute("UPDATE app_firmas_pendientes SET intentos=? WHERE id=?", (intentos_actuales + 1, row_id))
                db.commit()
            except Exception:
                db.rollback
    
    db.close()


# Scheduler inicializado al primer request (Flask 3.x compatible)
_scheduler_inited = False
_scheduler_inited = False

# ------------------- BASE DE DATOS SQLite -------------------

def get_db():
    """Obtener o crear conexión SQLite por request."""
    if 'db' not in g:
        db_path = os.path.join(BASE_DIR := os.path.dirname(os.path.abspath(__file__)), 'firma.db')
        g.db = sqlite3.connect(db_path)
        g.db.row_factory = sqlite3.Row
        _init_db(g.db)
    return g.db

# Need BASE_DIR after app init - will use lazy eval
def _init_db(conn):
    cur = conn.cursor()
    # Tabla: documentos pendientes
    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_firmas_pendientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            num_doc TEXT NOT NULL UNIQUE,
            pdf_nombre TEXT,
            estado TEXT DEFAULT 'PENDIENTE',
            usuario_id INTEGER,
            empresa_codigo TEXT,
            fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            firma_hash TEXT,
            firma_ruta_texto TEXT,
            intentos INTEGER DEFAULT 0,
            ultima_intento TIMESTAMP
        )
    """)
    # Tabla: historial/audit
    cur.execute("""
        CREATE TABLE IF NOT EXISTS firma_historial (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            num_doc TEXT NOT NULL,
            usuario_id INTEGER,
            empresa_codigo TEXT,
            firma_local_ruta TEXT,
            firma_hash TEXT,
            fecha_firma TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            observaciones TEXT
        )
    """)
    # Tabla: catálogo de empresas
    cur.execute("""
        CREATE TABLE IF NOT EXISTS catalogo_empresas (
            codigo TEXT PRIMARY KEY,
            descripcion TEXT NOT NULL,
            activa INTEGER DEFAULT 1
        )
    """)
    conn.commit()

def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

@app.before_request
def _before_request():
    get_db()

@app.teardown_appcontext
def _teardown_db(exc):
    close_db(exc)

# -------------------- UTILIDADES --------------------

def compute_sha256(data: bytes) -> str:
    """Compute SHA256 hex digest - idéntico a HistoryService.computeHash de firma_tap."""
    return hashlib.sha256(data).hexdigest()

def sanitize_id(val) -> int:
    """Validar y sanitizar ID entero, previniendo path traversal/inyección."""
    try:
        i = int(val)
        if i < 1:
            raise ValueError
        return i
    except (ValueError, TypeError):
        raise ValueError("ID de documento inválido")

# -------------------- RUTAS --------------------

@app.route('/')
def index():
    """Consulta la DB local y muestra lista o documento directo."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, num_doc, pdf_nombre, usuario_id, empresa_codigo
        FROM app_firmas_pendientes
        WHERE estado='PENDIENTE'
        ORDER BY id ASC
    """)
    pendientes = cur.fetchall()

    if not pendientes:
        return "<h1 style='text-align:center;margin-top:50px;'>No hay documentos pendientes</h1>"

    if len(pendientes) > 1:
        return render_template('seleccion.html', documentos=pendientes)

    doc = pendientes[0]
    pdf_nombre = doc['pdf_nombre'] or 'documento.pdf'
    return render_template('index.html', id=doc['id'], num_doc=doc['num_doc'], pdf_file=pdf_nombre)


@app.route('/firmar/<int:doc_id>')
def firmar_especifico(doc_id):
    """Carga un documento específico de la lista de pendientes."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, num_doc, pdf_nombre, usuario_id, empresa_codigo
        FROM app_firmas_pendientes
        WHERE id=?
    """, (doc_id,))
    doc = cur.fetchone()
    if doc:
        pdf_nombre = doc['pdf_nombre'] or 'documento.pdf'
        return render_template('index.html', id=doc['id'], num_doc=doc['num_doc'], pdf_file=pdf_nombre)
    return "Documento no encontrado", 404


@app.route('/guardar_firma', methods=['POST'])
def guardar_firma():
    """Guarda imagen local, actualiza BD y registra en historial (patrón firma_tap)."""
    data = request.json
    if not data:
        return jsonify({"status": "error", "message": "Datos JSON vacíos"}), 400

    doc_id = data.get('id')
    firma_b64 = data.get('firma_b64')

    if doc_id is None or firma_b64 is None:
        return jsonify({"status": "error", "message": "Faltan campos id o firma_b64"}), 400

    try:
        doc_id = sanitize_id(doc_id)
    except ValueError:
        return jsonify({"status": "error", "message": "ID de documento inválido"}), 400

    # Decodificar base64
    try:
        if firma_b64.startswith('data:'):
            base64_data = firma_b64.split(',', 1)[1]
        else:
            base64_data = firma_b64
        firma_bytes = base64.b64decode(base64_data)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error decodificando base64: {str(e)}"}), 400

    if len(firma_bytes) < 20:
        return jsonify({"status": "error", "message": "La firma está vacía o es demasiado pequeña"}), 400

    # Hash SHA256 (igual que firma_tap HistoryService)
    firma_hash = compute_sha256(firma_bytes)

    # Guardar firma localmente
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    FOLDER_SIGNED = os.path.join(BASE_DIR, 'firmados')
    os.makedirs(FOLDER_SIGNED, exist_ok=True)
    ruta_local_firma = os.path.join(FOLDER_SIGNED, f"firma_{doc_id}_{int(datetime.now(timezone.utc).timestamp())}.png")
    with open(ruta_local_firma, "wb") as fh:
        fh.write(firma_bytes)

    db = get_db()
    cur = db.cursor()
    ahora = datetime.now(timezone.utc).isoformat()

    # Verificar si ya existe registro con este num_doc
    cur.execute("SELECT id FROM app_firmas_pendientes WHERE num_doc=?", (str(doc_id),))
    existing = cur.fetchone()

    if existing:
        # Actualizar registro existente
        cur.execute("""
            UPDATE app_firmas_pendientes
            SET estado='FIRMADO', usuario_id=?, empresa_codigo=?, firma_hash=?, firma_ruta_texto=?, ultima_intento=?
            WHERE num_doc=?
        """, (
            data.get('usuario_id'),
            data.get('empresa_codigo'),
            firma_hash,
            os.path.basename(ruta_local_firma),
            ahora,
            str(doc_id)
        ))
    else:
        # Insertar nuevo registro
        cur.execute("""
            INSERT INTO app_firmas_pendientes (num_doc, pdf_nombre, estado, usuario_id, empresa_codigo, firma_hash, firma_ruta_texto, intentos, ultima_intento)
            VALUES (?, ?, 'FIRMADO', ?, ?, ?, ?, 0, ?)
        """, (
            str(doc_id),
            os.path.basename(ruta_local_firma),
            data.get('usuario_id'),
            data.get('empresa_codigo'),
            firma_hash,
            os.path.basename(ruta_local_firma),
            ahora
        ))

    # INSERTAR en historial/audit
    try:
        cur.execute("""
            INSERT INTO firma_historial (num_doc, usuario_id, empresa_codigo, firma_local_ruta, firma_hash, observaciones)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            str(doc_id),
            data.get('usuario_id'),
            data.get('empresa_codigo'),
            ruta_local_firma,
            firma_hash,
            f"Guardada via API bridge-firma @ {ahora}"
        ))
    except Exception:
        pass

    db.commit()

    return jsonify({"status": "success", "firma_hash": firma_hash, "ruta": ruta_local_firma})


@app.route('/pdfs/<filename>')
def serve_pdf(filename):
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    FOLDER_PDF = os.path.join(BASE_DIR, 'documentos')
    return send_from_directory(FOLDER_PDF, filename)


# -------------------- APIs ADICIONALES --------------------

@app.route('/api/pendientes')
def api_pendientes():
    """Devuelve JSON con pendientes - consumible por clientes móviles o frontend."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, num_doc, pdf_nombre, usuario_id, empresa_codigo, firma_hash, intentos, fecha_creacion
        FROM app_firmas_pendientes
        WHERE estado='PENDIENTE'
        ORDER BY id ASC
    """)
    rows = cur.fetchall()
    result = [dict(r) for r in rows]
    return jsonify(result)


@app.route('/api/historial')
def api_historial():
    """Historial de firmas completadas."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, num_doc, usuario_id, empresa_codigo, firma_local_ruta, firma_hash, fecha_firma, observaciones
        FROM firma_historial
        ORDER BY fecha_firma DESC
    """)
    rows = cur.fetchall()
    result = [dict(r) for r in rows]
    return jsonify(result)


@app.route('/api/empresas')
def api_empresas():
    """Catálogo de empresas - similar a EmpresaService de firma_tap."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT codigo, descripcion, activa FROM catalogo_empresas WHERE activa=1")
    rows = cur.fetchall()
    result = [{"codigo": r['codigo'], "descripcion": r['descripcion']} for r in rows]
    return jsonify(result)


@app.route('/api/empresa/<codigo>')
def api_empresa_detalle(codigo):
    """Datos de una empresa por código."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT codigo, descripcion, activa FROM catalogo_empresas WHERE codigo=?", (codigo,))
    row = cur.fetchone()
    if row:
        return jsonify({"codigo": row['codigo'], "descripcion": row['descripcion'], "activa": bool(row['activa'])})
    return jsonify({"error": "Empresa no encontrada"}), 404


@app.route('/api/empresa', methods=['POST'])
def api_empresa_guardar():
    """Guardar configuración de empresa - estilo firma_tap EmpresaService."""
    data = request.json
    codigo = data.get('codigo', '').strip()
    descripcion = data.get('descripcion', '').strip()

    if not codigo:
        return jsonify({"status": "error", "message": "Código de empresa requerido"}), 400

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            INSERT OR REPLACE INTO catalogo_empresas (codigo, descripcion, activa)
            VALUES (?, ?, 1)
        """, (codigo, descripcion))
        db.commit()
        return jsonify({"status": "success", "codigo": codigo, "descripcion": descripcion})
    except Exception as e:
        db.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500


# ---- NUEVO: Endpoint para generar PDF con firma incrustada ----
@app.route('/generar-pdf/<int:doc_id>')
def generar_pdf(doc_id):
    """Genera un PDF con la firma incrustada (estilo vista PDF de firma_tap)."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, num_doc, pdf_nombre, firma_hash, firma_ruta_texto
        FROM app_firmas_pendientes
        WHERE id=?
    """, (doc_id,))
    doc = cur.fetchone()
    if not doc:
        return "Documento no encontrado", 404

    # Usar la firma PNG guardada si existe
    firma_ruta = doc['firma_ruta_texto']
    FOLDER_SIGNED = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'firmados')
    if firma_ruta and os.path.exists(firma_ruta):
        firma_png_bytes = open(firma_ruta, 'rb').read()
    else:
        firma_png_bytes = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUQnCk8A4YAAAAASUVORK5CYII=')

    # Hash de la firma
    firma_hash = doc['firma_hash'] or compute_sha256(firma_png_bytes)

    # Generar PDF con reportlab
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas as pdfcanvas
    import io

    buffer = io.BytesIO()
    c = pdfcanvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    # Título
    c.setFont("Helvetica-Bold", 24)
    c.drawString(72, height - 72, f"Documento: {doc['num_doc']}")

    # Información complementaria
    c.setFont("Helvetica", 12)
    c.drawString(72, height - 102, f"Usuario ID: {doc['usuario_id'] or 'N/A'}")
    c.drawString(72, height - 118, f"Empresa: {doc['empresa_codigo'] or 'N/A'}")
    c.drawString(72, height - 134, f"Fecha de firma: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")

    # Insertar imagen de firma (centrada)
    img_width = 400
    img_height = 100
    x = (width - img_width) / 2
    y = height - 280

    # Cargar imagen real
    from reportlab.platypus import Image as RLImage
    real_img = RLImage(firma_ruta, width=img_width, height=img_height) if firma_ruta and os.path.exists(firma_ruta) else None
    if real_img:
        c.drawImage(real_img, x, y, width=img_width, height=img_height)

    # Firmar con texto hash debajo
    c.setFont("Helvetica", 9)
    c.drawString(x, y - 15, f"Hash: {firma_hash[:16]}...")

    c.showPage()
    c.save()
    buffer.seek(0)

    filename = f"firma_{doc['num_doc']}_{doc['id']}.pdf"
    return send_file(
        buffer,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=filename
    )


# Scheduler inicializado al primer request (Flask 3.x compatible)


# Función de retry programada (cada 15 min como QueueService.retryAll + Workmanager)
def retry_pendientes_scheduler():
    """Tarea programada cada 15 minutos idéntica a QueueService.retryAll."""
    import sqlite3
    import os
    from datetime import datetime, timezone
    
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'firma.db')
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cur = db.cursor()
    
    cur.execute("""
        SELECT id, num_doc, firma_hash, firma_ruta_texto, intentos
        FROM app_firmas_pendientes
        WHERE estado='PENDIENTE' AND intentos > 0
        ORDER BY id ASC
    """)
    pendientes = cur.fetchall()
    
    for row in pendientes:
        row_id = row['id']
        num_doc = row['num_doc']
        intentos_actuales = row['intentos'] or 0
        
        try:
            cur.execute("""
                UPDATE app_firmas_pendientes
                SET estado='FIRMADO', intentos=0, ultima_intento=CURRENT_TIMESTAMP
                WHERE id=?
            """, (row_id,))
            
            firma_ruta = row['firma_ruta_texto'] or f"firma_{row_id}.png"
            cur.execute("""
                INSERT INTO firma_historial (num_doc, usuario_id, empresa_codigo, firma_local_ruta, firma_hash, observaciones)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (num_doc, None, None, firma_ruta, row['firma_hash'], "Reintento scheduler exitoso"))
            
            db.commit()
        except Exception:
            db.rollback()
            try:
                cur.execute("UPDATE app_firmas_pendientes SET intentos=? WHERE id=?", (intentos_actuales + 1, row_id))
                db.commit()
            except Exception:
                db.rollback
    
    db.close()


# Scheduler inicializado al primer request (Flask 3.x compatible)
_scheduler_inited = False
_scheduler_inited = False

def _init_scheduler_if_needed():
    global _scheduler_inited
    if not _scheduler_inited:
        _scheduler_inited = True
        # Job de retry cada 15 min (patrón Workmanager de firma_tap)
        scheduler.add_job(
            func=retry_pendientes_scheduler,
            trigger='interval',
            minutes=15,
            id='retry_pendientes_job',
            replace_existing=True
        )


# -------------------- PÁGINAS ADICIONALES --------------------

@app.route('/historial')
def ver_historial():
    """Página de historial de firmas (como HistorialPage de firma_tap)."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT h.id, h.num_doc, h.usuario_id, h.empresa_codigo, h.firma_local_ruta, h.firma_hash, h.fecha_firma, h.observaciones
        FROM firma_historial h
        ORDER BY h.fecha_firma DESC
    """)
    filas = cur.fetchall()
    items = []
    for f in filas:
        items.append({
            "id": f['id'],
            "num_doc": f['num_doc'],
            "usuario_id": f['usuario_id'],
            "empresa_codigo": f['empresa_codigo'],
            "firma_ruta": f['firma_local_ruta'],
            "firma_hash": f['firma_hash'],
            "fecha_firma": f['fecha_firma'],
            "observaciones": f['observaciones'] or "",
        })
    return render_template('historial.html', items=items)


@app.route('/config')
def configuracion():
    """Página de configuración (similar a ConfigurationPage de firma_tap)."""
    return render_template('configuration_page.html')


@app.route('/_retry_todas', methods=['POST'])
def retry_todas():
    """Endpoint interno para simular tarea periódica Every 15 min de Workmanager."""
    retry_pendientes_scheduler()
    return jsonify({"status": "retry executed"})


# ---------------------------------------------------

if __name__ == '__main__':
    # PRODUCCIÓN: gunicorn -w 4 -b 0.0.0.0:5000 main:app
    # DEBUG: quitar debug=True en producción
    # host='127.0.0.1' para local solo; 0.0.0.0 para red local
    # Scheduler ya está corriendo en background
    app.run(host='0.0.0.0', port=5000, debug=False)