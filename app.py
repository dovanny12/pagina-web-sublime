import os
import time
import json
import sqlite3
import urllib.request
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from models import db, User, Product, Order

app = Flask(__name__)
app.secret_key = 'super_secret_key_for_sublime'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SHARED_DB_PATH = os.path.abspath(os.path.join(BASE_DIR, 'Sublime', 'BD', 'database.db'))
SHARED_SQL_PATH = os.path.abspath(os.path.join(BASE_DIR, 'Sublime', 'BD', 'database.sql'))

# Cache para la tasa BCV
BCV_RATE_CACHE = {'rate': 40.0, 'updated': 0}
CACHE_TTL = 3600  # 1 hora

def fetch_bcv_rate():
    now = time.time()
    if now - BCV_RATE_CACHE['updated'] < CACHE_TTL:
        return BCV_RATE_CACHE['rate']
    try:
        req = urllib.request.Request(
            'https://rates.dolarvzla.com/bcv/current.json',
            headers={'User-Agent': 'SublimeApp/1.0'}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            rate = float(data['current']['usd'])
            if rate > 0:
                BCV_RATE_CACHE['rate'] = rate
                BCV_RATE_CACHE['updated'] = now
    except Exception:
        pass
    return BCV_RATE_CACHE['rate']

app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{SHARED_DB_PATH}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
os.makedirs(os.path.dirname(SHARED_DB_PATH), exist_ok=True)


def ensure_shared_db():
    # Crear la DB si no existe y siempre aplicar el schema SQL
    os.makedirs(os.path.dirname(SHARED_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(SHARED_DB_PATH)
    conn.execute('PRAGMA foreign_keys = ON')
    if os.path.exists(SHARED_SQL_PATH):
        with open(SHARED_SQL_PATH, 'r', encoding='utf-8') as f:
            conn.executescript(f.read())
    conn.commit()
    conn.close()


def get_shared_db():
    ensure_shared_db()
    conn = sqlite3.connect(SHARED_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def map_product_row(row):
    return {
        'id': row['id_producto'],
        'name': row['nombre'],
        'category': row['categoria'] or 'General',
        'price': float(row['precio_venta']),
        'image_url': row['ruta_imagen'] or 'placeholder.png',
        'description': row['descripcion'] or ''
    }


def fetch_products(categoria=None, search_query=None, sort_option='newest', limit=None):
    conn = get_shared_db()
    sql = (
        'SELECT p.id_producto, p.nombre, p.descripcion, p.precio_venta, '
        'c.nombre AS categoria, '
        'COALESCE((SELECT ruta_imagen FROM imagenes_productos ip WHERE ip.id_producto = p.id_producto ORDER BY ip.id_imagen LIMIT 1), ?) AS ruta_imagen '
        'FROM productos p '
        'LEFT JOIN categorias c ON p.id_categoria = c.id_categoria '
        'WHERE p.activo = 1 AND p.id_categoria IS NOT NULL '
    )
    params = ['placeholder.png']

    if categoria:
        sql += ' AND c.nombre = ? '
        params.append(categoria)

    if search_query:
        sql += ' AND (p.nombre LIKE ? OR p.descripcion LIKE ?) '
        term = f'%{search_query}%'
        params.extend([term, term])

    if sort_option == 'price_asc':
        sql += ' ORDER BY p.precio_venta ASC '
    elif sort_option == 'price_desc':
        sql += ' ORDER BY p.precio_venta DESC '
    elif sort_option == 'name_asc':
        sql += ' ORDER BY p.nombre ASC '
    else:
        sql += ' ORDER BY p.id_producto DESC '

    if limit:
        sql += ' LIMIT ? '
        params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [map_product_row(row) for row in rows]


def fetch_product_by_id(product_id):
    conn = get_shared_db()
    row = conn.execute(
        'SELECT p.id_producto, p.nombre, p.descripcion, p.precio_venta, c.nombre AS categoria, '
        'COALESCE((SELECT ruta_imagen FROM imagenes_productos ip WHERE ip.id_producto = p.id_producto ORDER BY ip.id_imagen LIMIT 1), ?) AS ruta_imagen '
        'FROM productos p '
        'LEFT JOIN categorias c ON p.id_categoria = c.id_categoria '
        'WHERE p.activo = 1 AND p.id_producto = ? ',
        ['placeholder.png', product_id]
    ).fetchone()
    conn.close()
    return map_product_row(row) if row else None


def user_can_review(user_email, product_id):
    if not user_email:
        return False
    conn = get_shared_db()
    row = conn.execute(
        'SELECT COUNT(*) AS total FROM pedidos p '
        'JOIN clientes c ON p.id_cliente = c.id_cliente '
        'JOIN detalle_pedidos dp ON p.id_pedido = dp.id_pedido '
        'JOIN estados_pedido ep ON p.id_estado = ep.id_estado '
        'WHERE c.correo = ? AND dp.id_producto = ? AND ep.nombre = "Entregado"',
        (user_email, product_id)
    ).fetchone()
    conn.close()
    return row['total'] > 0


def fetch_reviews(product_id):
    conn = get_shared_db()
    rows = conn.execute(
        'SELECT r.puntuacion, r.comentario, r.fecha, u.nombre AS usuario '
        'FROM reseñas r '
        'JOIN usuarios u ON r.id_usuario = u.id_usuario '
        'WHERE r.id_producto = ? '
        'ORDER BY r.fecha DESC',
        (product_id,)
    ).fetchall()
    conn.close()
    return [
        {
            'puntuacion': row['puntuacion'],
            'comentario': row['comentario'],
            'fecha': row['fecha'],
            'usuario': row['usuario']
        } for row in rows
    ]


def get_or_create_client(conn, email, nombre, direccion):
    if email:
        cliente = conn.execute('SELECT id_cliente FROM clientes WHERE correo = ? LIMIT 1', (email,)).fetchone()
        if cliente:
            return cliente['id_cliente']

    cliente = conn.execute('SELECT id_cliente FROM clientes WHERE nombre = ? AND direccion = ? LIMIT 1', (nombre, direccion)).fetchone()
    if cliente:
        return cliente['id_cliente']

    cursor = conn.execute(
        'INSERT INTO clientes VALUES (NULL, ?, ?, ?, ?)',
        (nombre, email, '', direccion)
    )
    conn.commit()
    return cursor.lastrowid


def ensure_order_statuses(conn):
    count = conn.execute('SELECT COUNT(*) AS total FROM estados_pedido').fetchone()['total']
    if count == 0:
        conn.executemany('INSERT INTO estados_pedido (nombre) VALUES (?)', [('Pendiente',), ('Procesando',), ('Enviado',), ('Entregado',)])
        conn.commit()


def get_or_create_cart(conn, cliente_id):
    carrito = conn.execute('SELECT id_carrito FROM carrito WHERE id_cliente = ? AND fecha_creacion >= datetime("now", "-1 day") ORDER BY fecha_creacion DESC LIMIT 1', (cliente_id,)).fetchone()
    if carrito:
        return carrito['id_carrito']
    
    cursor = conn.execute('INSERT INTO carrito (id_cliente) VALUES (?)', (cliente_id,))
    conn.commit()
    return cursor.lastrowid


def get_or_create_custom_product(conn, name='Producto Personalizado', description='Producto personalizado', price=30.0):
    # Siempre crear un nuevo registro para cada personalización
    # Dejar un nombre legible pero único para evitar que distintos diseños compartan el mismo id
    import time
    unique_name = f"{name} - {int(time.time()*1000)}"
    cursor = conn.execute(
        'INSERT INTO productos (nombre, descripcion, costo, precio_venta, activo) VALUES (?, ?, ?, ?, 1)',
        (unique_name, description, price, price)
    )
    conn.commit()
    return cursor.lastrowid


def load_cart_from_db():
    if 'user_id' not in session:
        return []
    
    conn = get_shared_db()
    cliente_id = session['user_id']
    carrito_id = get_or_create_cart(conn, cliente_id)
    
    items = conn.execute(
        'SELECT dc.id_detalle, dc.id_producto, dc.cantidad, dc.precio_unitario, p.nombre AS name, p.descripcion '
        'FROM detalle_carrito dc '
        'LEFT JOIN productos p ON dc.id_producto = p.id_producto '
        'WHERE dc.id_carrito = ? '
        'ORDER BY dc.id_detalle ASC',
        (carrito_id,)
    ).fetchall()
    conn.close()
    
    return [
        {
            'id': item['id_producto'],
            'name': item['name'] or 'Producto personalizado',
            'price': float(item['precio_unitario']),
            'quantity': item['cantidad'],
            'details': item['descripcion'] or ''
        }
        for item in items
    ]


def save_cart_to_db(cart_items):
    if 'user_id' not in session:
        return
    
    conn = get_shared_db()
    cliente_id = session['user_id']
    carrito_id = get_or_create_cart(conn, cliente_id)
    
    # Limpiar carrito anterior
    conn.execute('DELETE FROM detalle_carrito WHERE id_carrito = ?', (carrito_id,))
    
    # Insertar nuevos items
    for item in cart_items:
        conn.execute(
            'INSERT INTO detalle_carrito (id_carrito, id_producto, cantidad, precio_unitario) VALUES (?, ?, ?, ?)',
            (carrito_id, item.get('id'), item.get('quantity', 1), item['price'])
        )
    
    conn.commit()
    conn.close()


def get_cart_count():
    return len(load_cart_from_db()) if 'user_id' in session else len(session.get('cart', []))


def load_current_user():
    if 'user_id' not in session:
        return None
    conn = get_shared_db()
    row = conn.execute(
        'SELECT id_usuario, nombre, correo FROM usuarios WHERE id_usuario = ? LIMIT 1',
        (session['user_id'],)
    ).fetchone()
    conn.close()
    if not row:
        return None

    return {
        'id': row['id_usuario'],
        'username': row['nombre'],
        'email': row['correo'],
        'phone': ''
    }


def merge_guest_cart_into_db(guest_cart):
    if 'user_id' not in session or not guest_cart:
        return
    if not isinstance(guest_cart, list):
        return

    db_cart = load_cart_from_db()
    for item in guest_cart:
        if not isinstance(item, dict):
            continue
        item_id = item.get('id')
        quantity = item.get('quantity', 1)
        price = item.get('price', 0)
        if item_id is None:
            continue

        existing = next((ci for ci in db_cart if ci.get('id') == item_id), None)
        if existing:
            existing['quantity'] = existing.get('quantity', 1) + quantity
        else:
            db_cart.append({
                'id': item_id,
                'name': item.get('name', 'Producto'),
                'price': price,
                'quantity': quantity,
                'details': item.get('details', '')
            })

    save_cart_to_db(db_cart)
    session.pop('cart', None)
    session.modified = True


# Configurar SQLAlchemy y asegurar existencia de la DB compartida.
db.init_app(app)
ensure_shared_db()

# Si creamos una DB vacía, permitir que SQLAlchemy cree las tablas definidas en models.py
with app.app_context():
    try:
        db.create_all()
    except Exception:
        # Ignorar errores aquí; la app seguirá intentando usar la DB compartida mediante sqlite3 directo
        pass

@app.context_processor
def inject_exchange_rate():
    tasa_cambio = fetch_bcv_rate()
    def format_price(usd_val):
        if usd_val is None:
            usd_val = 0.0
        try:
            usd_val = float(usd_val)
        except ValueError:
            usd_val = 0.0
        bs_val = usd_val * tasa_cambio
        formatted_usd = f"${usd_val:,.2f}"
        formatted_bs = f"{bs_val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"{formatted_usd} / {formatted_bs} Bs"

    return dict(
        tasa_cambio=tasa_cambio,
        format_price=format_price,
        cart_count=get_cart_count(),
        current_username=session.get('username')
    )


@app.route('/api/tasa-cambio')
def api_tasa_cambio():
    return jsonify({'tasa': fetch_bcv_rate()})


# Semillas en la base de datos compartida (esquema SQL en español)
conn = get_shared_db()
try:
    total = conn.execute('SELECT COUNT(*) AS total FROM productos').fetchone()['total']
except Exception:
    total = 0

if total == 0:
    # Crear categoría 'Taza' si no existe
    cat = conn.execute('SELECT id_categoria FROM categorias WHERE nombre = ? LIMIT 1', ('Taza',)).fetchone()
    if cat:
        cat_id = cat['id_categoria']
    else:
        cur = conn.execute('INSERT INTO categorias (nombre) VALUES (?)', ('Taza',))
        cat_id = cur.lastrowid

    sample_products = [
        ('Taza Baki', 'Taza Baki personalizada', 15.00, 'taza baki.jpeg'),
        ('Taza con Flores', 'Taza con flores', 12.00, 'taza con flores.jpeg'),
        ('Taza Hollow Knight', 'Taza Hollow Knight', 15.00, 'taza hollow knight.jpeg'),
        ('Taza Mensaje Motivador', 'Taza con mensaje motivador', 10.00, 'taza mensaje 1.jpeg'),
        ('Taza Poo Emoji', 'Taza Poo Emoji', 12.00, 'taza poo.jpeg'),
        ('Taza Sabra Pepe', 'Taza Sabra Pepe', 15.00, 'taza sabra pepe.jpeg'),
        ('Taza Spiderman', 'Taza Spiderman', 15.00, 'taza spiderman.jpeg'),
        ('Taza Trofeo #1 Mamá', 'Taza Trofeo Mamá', 18.00, 'taza trofeo #1 mama.jpeg')
    ]

    for nombre, descripcion, precio, imagen in sample_products:
        cur = conn.execute(
            'INSERT INTO productos (nombre, descripcion, costo, precio_venta, id_categoria, activo) VALUES (?, ?, ?, ?, ?, 1)',
            (nombre, descripcion, precio, precio, cat_id)
        )
        prod_id = cur.lastrowid
        conn.execute('INSERT INTO imagenes_productos (id_producto, ruta_imagen) VALUES (?, ?)', (prod_id, imagen))

    conn.commit()

conn.close()
@app.route('/')
def home():
    trending = fetch_products(sort_option='newest', limit=8)
    cart_count = len(load_cart_from_db()) if 'user_id' in session else len(session.get('cart', []))
    return render_template('index.html', trending=trending, cart_count=cart_count)

@app.route('/catalogo')
def catalogo():
    categoria = request.args.get('categoria')
    search_query = request.args.get('q')
    sort_option = request.args.get('sort')
    productos = fetch_products(categoria=categoria, search_query=search_query, sort_option=sort_option)
    
    # Obtener categorías para el filtro
    conn = get_shared_db()
    categorias = conn.execute('SELECT nombre FROM categorias ORDER BY nombre').fetchall()
    conn.close()
    categorias_list = [row['nombre'] for row in categorias]
    
    cart_count = len(load_cart_from_db()) if 'user_id' in session else len(session.get('cart', []))
    return render_template('catalogo.html', productos=productos, categorias=categorias_list, cart_count=cart_count)

@app.route('/producto/<int:id>')
def producto(id):
    p = fetch_product_by_id(id)
    if not p:
        if '404.html' in os.listdir(os.path.join(BASE_DIR, 'templates')):
            return render_template('404.html'), 404
        return 'Producto no encontrado', 404
    
    reviews = fetch_reviews(id)
    can_review = False
    if 'user_id' in session:
        can_review = user_can_review(session.get('user_email'), id)
        
    return render_template('producto.html', producto=p, reviews=reviews, can_review=can_review)

@app.route('/personalizar', methods=['GET', 'POST'])
def personalizar():
    if request.method == 'POST':
        # Capturar todos los detalles de personalización
        product_type = request.form.get('product_type')
        size = request.form.get('size')
        color = request.form.get('product_color')
        material = request.form.get('material')
        text = request.form.get('custom_text')
        font = request.form.get('font_style')
        placement = request.form.get('placement')
        
        custom_details = f"({product_type.capitalize()}, {size}, {color}, {material})"
        if text:
            custom_details += f" con texto: '{text}' ({font}) en {placement}"
            
        # Crear producto personalizado en DB con nombre identificable
        conn = get_shared_db()
        product_id = get_or_create_custom_product(conn, name=f"Personalizado - {product_type.capitalize()}", description=custom_details, price=30.0)
        conn.close()
        
        flash('Diseño personalizado añadido al carrito.', 'success')
        cart = load_cart_from_db() if 'user_id' in session else session.get('cart', [])
        if not isinstance(cart, list):
            cart = []
        
        cart.append({
            'id': product_id,
            'name': f'Personalizado: {product_type.capitalize()}', 
            'details': custom_details,
            'price': 30.00,
            'quantity': 1
        })
        
        if 'user_id' in session:
            save_cart_to_db(cart)
        else:
            session['cart'] = cart
            session.modified = True
        
        return redirect(url_for('carrito'))
    cart_count = len(load_cart_from_db()) if 'user_id' in session else len(session.get('cart', []))
    return render_template('personalizar.html', cart_count=cart_count)

@app.route('/agregar_carrito/<int:id>')
def agregar_carrito(id):
    p = fetch_product_by_id(id)
    if not p:
        flash('Producto no encontrado.', 'error')
        return redirect(url_for('catalogo'))

    cart = load_cart_from_db() if 'user_id' in session else session.get('cart', [])
    if not isinstance(cart, list):
        cart = []
    
    # Verificar si ya está en carrito
    existing = next((item for item in cart if item.get('id') == id), None)
    if existing:
        existing['quantity'] += 1
    else:
        cart.append({'id': p['id'], 'name': p['name'], 'price': p['price'], 'quantity': 1})
    
    if 'user_id' in session:
        save_cart_to_db(cart)
    else:
        session['cart'] = cart
        session.modified = True
    
    flash(f"{p['name']} añadido al carrito.", 'success')
    return redirect(url_for('catalogo'))

@app.route('/carrito')
def carrito():
    if 'user_id' in session:
        cart = load_cart_from_db()
    else:
        cart = session.get('cart', [])
    
    total = sum(item['price'] * item.get('quantity', 1) for item in cart)
    return render_template('carrito.html', cart=cart, total=total, cart_count=len(cart))

@app.route('/eliminar_carrito/<int:index>')
def eliminar_carrito(index):
    if 'user_id' in session:
        cart = load_cart_from_db()
        if isinstance(cart, list) and 0 <= index < len(cart):
            cart.pop(index)
            save_cart_to_db(cart)
    else:
        cart = session.get('cart', [])
        if isinstance(cart, list) and 0 <= index < len(cart):
            cart.pop(index)
            session['cart'] = cart
            session.modified = True

    return redirect(url_for('carrito'))

@app.route('/checkout', methods=['GET', 'POST'])
def checkout():
    if 'user_id' in session:
        cart = load_cart_from_db()
    else:
        cart = session.get('cart', [])
    
    if not cart:
        flash('Tu carrito está vacío.', 'error')
        return redirect(url_for('catalogo'))

    total = sum(item['price'] * item.get('quantity', 1) for item in cart)
    if request.method == 'POST':
        name = request.form.get('name')
        address = request.form.get('address')
        payment_method = request.form.get('payment_method')
        reference = request.form.get('reference')

        conn = get_shared_db()
        ensure_order_statuses(conn)
        cliente_id = get_or_create_client(conn, session.get('user_email'), session.get('username') or name, address)
        status = conn.execute('SELECT id_estado FROM estados_pedido WHERE nombre = ? LIMIT 1', ('Pendiente',)).fetchone()
        status_id = status['id_estado'] if status else 1

        pedido_cursor = conn.execute(
            'INSERT INTO pedidos (id_cliente, id_estado, total) VALUES (?, ?, ?)',
            (cliente_id, status_id, total)
        )
        pedido_id = pedido_cursor.lastrowid

        for item in cart:
            product_id = item.get('id')
            if not product_id:
                product_id = get_or_create_custom_product(conn)
            cantidad = item.get('quantity', 1)
            conn.execute(
                'INSERT INTO detalle_pedidos (id_pedido, id_producto, cantidad, precio_unitario) VALUES (?, ?, ?, ?)',
                (pedido_id, product_id, cantidad, item['price'])
            )

        conn.execute(
            'INSERT INTO envios (id_pedido, direccion_envio, empresa_envio, numero_guia, estado_envio, fecha_envio) VALUES (?, ?, ?, ?, ?, datetime("now"))',
            (pedido_id, address, payment_method or 'Pendiente', reference or '', 'Pendiente',)
        )
        conn.commit()
        conn.close()

        # Limpiar carrito
        if 'user_id' in session:
            save_cart_to_db([])
        else:
            session.pop('cart', None)
        
        return redirect(url_for('factura', order_id=pedido_id))

    cart_count = len(cart)
    return render_template('checkout.html', total=total, cart_count=cart_count)

@app.route('/factura/<int:order_id>')
def factura(order_id):
    conn = get_shared_db()
    order_row = conn.execute(
        'SELECT p.id_pedido AS id, p.total, p.fecha, e.nombre AS estado, c.nombre AS cliente, c.correo, env.direccion_envio AS address, env.empresa_envio AS payment_method, env.numero_guia AS reference '
        'FROM pedidos p '
        'LEFT JOIN estados_pedido e ON p.id_estado = e.id_estado '
        'LEFT JOIN clientes c ON p.id_cliente = c.id_cliente '
        'LEFT JOIN envios env ON env.id_pedido = p.id_pedido '
        'WHERE p.id_pedido = ? LIMIT 1',
        (order_id,)
    ).fetchone()

    if not order_row:
        conn.close()
        if '404.html' in os.listdir(os.path.join(BASE_DIR, 'templates')):
            return render_template('404.html'), 404
        return 'Pedido no encontrado', 404

    items = conn.execute(
        'SELECT dp.cantidad, dp.precio_unitario, pr.nombre AS name '
        'FROM detalle_pedidos dp '
        'LEFT JOIN productos pr ON dp.id_producto = pr.id_producto '
        'WHERE dp.id_pedido = ?',
        (order_id,)
    ).fetchall()
    conn.close()

    items_list = [
        {
            'name': item['name'] or 'Producto personalizado',
            'price': item['precio_unitario'] * item['cantidad'],
            'details': f'Cantidad: {item["cantidad"]}' if item['cantidad'] and item['cantidad'] > 1 else ''
        }
        for item in items
    ]

    order = {
        'id': order_row['id'],
        'total': float(order_row['total']),
        'address': order_row['address'] or '',
        'payment_method': order_row['payment_method'] or '',
        'reference': order_row['reference'] or '',
    }
    return render_template('factura.html', order=order, items=items_list)

@app.route('/login', methods=['GET', 'POST'])
def login():
    cart_count = get_cart_count()

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        conn = get_shared_db()
        user = conn.execute(
            'SELECT id_usuario, nombre, correo, "contraseña" AS password FROM usuarios WHERE correo = ? OR nombre = ? LIMIT 1',
            (username, username)
        ).fetchone()
        conn.close()

        if user and user['password'] == password:
            session['user_id'] = user['id_usuario']
            session['username'] = user['nombre']
            session['user_email'] = user['correo']

            guest_cart = session.get('cart', [])
            merge_guest_cart_into_db(guest_cart)

            flash(f'¡Bienvenido de nuevo, {user["nombre"]}!', 'success')
            return redirect(url_for('home'))
        else:
            flash('Usuario o contraseña incorrectos.', 'error')

    return render_template('login.html', cart_count=cart_count)


@app.route('/registro', methods=['GET', 'POST'])
def registro():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        email = request.form.get('email')

        conn = get_shared_db()
        existing_user = conn.execute(
            'SELECT id_usuario FROM usuarios WHERE nombre = ? OR correo = ? LIMIT 1',
            (username, email)
        ).fetchone()

        if existing_user:
            flash('El nombre de usuario o email ya está en uso.', 'error')
        else:
            # Asignar rol Trabajador (2) si existe, o Administrador por defecto
            role = conn.execute('SELECT id_rol FROM roles WHERE nombre = ? LIMIT 1', ('Trabajador',)).fetchone()
            role_id = role['id_rol'] if role else 2
            conn.execute(
                'INSERT INTO usuarios VALUES (NULL, ?, ?, ?, ?)',
                (username, email, password, role_id)
            )
            conn.commit()
            flash('Cuenta creada exitosamente. ¡Bienvenido!', 'success')
            conn.close()
            return redirect(url_for('login'))

        conn.close()

    return render_template('registro.html')


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('username', None)
    session.pop('user_email', None)
    flash('Has cerrado sesión.', 'success')
    return redirect(url_for('home'))


@app.route('/contacto', methods=['GET', 'POST'])
def contacto():
    if request.method == 'POST':
        nombre = request.form.get('nombre')
        email = request.form.get('email')
        asunto = request.form.get('asunto')
        mensaje = request.form.get('mensaje')
        # Aquí podríamos guardar el mensaje en la DB o enviarlo por correo.
        flash('Mensaje enviado. Gracias por contactarnos.', 'success')
        return redirect(url_for('contacto'))
    cart_count = len(load_cart_from_db()) if 'user_id' in session else len(session.get('cart', []))
    return render_template('contacto.html', cart_count=cart_count)


@app.route('/ayuda')
def ayuda():
    cart_count = len(load_cart_from_db()) if 'user_id' in session else len(session.get('cart', []))
    return render_template('ayuda.html', cart_count=cart_count)


@app.route('/terminos')
def terminos():
    cart_count = len(load_cart_from_db()) if 'user_id' in session else len(session.get('cart', []))
    return render_template('terminos.html', cart_count=cart_count)


@app.route('/privacidad')
def privacidad():
    cart_count = len(load_cart_from_db()) if 'user_id' in session else len(session.get('cart', []))
    return render_template('privacidad.html', cart_count=cart_count)


@app.route('/perfil', methods=['GET', 'POST'])
def perfil():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    user = load_current_user()
    if not user:
        flash('No se encontró información de usuario. Inicia sesión nuevamente.', 'error')
        return redirect(url_for('logout'))
        
    conn = get_shared_db()
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'cambiar_password':
            current_pass = request.form.get('current_password')
            new_pass = request.form.get('new_password')
            confirm_pass = request.form.get('confirm_password')
            
            user_db = conn.execute('SELECT contraseña FROM usuarios WHERE id_usuario = ? LIMIT 1', (session['user_id'],)).fetchone()
            
            if new_pass != confirm_pass:
                flash('La nueva contraseña y la confirmación no coinciden.', 'error')
            elif user_db and user_db['contraseña'] != current_pass:
                flash('La contraseña actual es incorrecta.', 'error')
            else:
                conn.execute('UPDATE usuarios SET contraseña = ? WHERE id_usuario = ?', (new_pass, session['user_id']))
                conn.commit()
                flash('Contraseña actualizada exitosamente.', 'success')
                
        elif action == 'cambiar_email':
            new_email = request.form.get('new_email')
            existing = conn.execute('SELECT id_usuario FROM usuarios WHERE correo = ? AND id_usuario != ? LIMIT 1', (new_email, session['user_id'])).fetchone()
            if existing:
                flash('El correo electrónico ya está registrado por otro usuario.', 'error')
            else:
                conn.execute('UPDATE usuarios SET correo = ? WHERE id_usuario = ?', (new_email, session['user_id']))
                conn.execute('UPDATE clientes SET correo = ? WHERE correo = ?', (new_email, user['email']))
                conn.commit()
                session['user_email'] = new_email
                flash('Correo electrónico actualizado exitosamente.', 'success')
                
        elif action == 'cambiar_telefono':
            flash('Número de teléfono actualizado exitosamente.', 'success')
            
        conn.close()
        return redirect(url_for('perfil'))
        
    # GET method
    cliente = conn.execute('SELECT id_cliente FROM clientes WHERE correo = ? LIMIT 1', (user['email'],)).fetchone()
    orders_with_items = []
    if cliente:
        orders = conn.execute(
            'SELECT p.id_pedido, p.total, p.fecha, ep.nombre AS status, env.direccion_envio AS address, env.empresa_envio AS payment_method '
            'FROM pedidos p '
            'LEFT JOIN estados_pedido ep ON p.id_estado = ep.id_estado '
            'LEFT JOIN envios env ON env.id_pedido = p.id_pedido '
            'WHERE p.id_cliente = ? '
            'ORDER BY p.id_pedido DESC',
            (cliente['id_cliente'],)
        ).fetchall()
        
        for order in orders:
            items = conn.execute(
                'SELECT dp.cantidad, dp.precio_unitario, pr.nombre AS name '
                'FROM detalle_pedidos dp '
                'LEFT JOIN productos pr ON dp.id_producto = pr.id_producto '
                'WHERE dp.id_pedido = ?',
                (order['id_pedido'],)
            ).fetchall()
            
            items_list = [
                {
                    'name': item['name'] or 'Producto personalizado',
                    'price': float(item['precio_unitario'])
                } for item in items
            ]
            
            orders_with_items.append({
                'order': {
                    'id': order['id_pedido'],
                    'status': order['status'] or 'Pendiente',
                    'total': float(order['total']),
                    'address': order['address'] or '',
                    'payment_method': order['payment_method'] or 'N/A'
                },
                'items': items_list
            })
            
    links = conn.execute('SELECT proveedor, proveedor_correo FROM cuentas_vinculadas WHERE id_usuario = ?', (session['user_id'],)).fetchall()
    linked_accounts = {row['proveedor']: row['proveedor_correo'] for row in links}
    conn.close()
    
    cart_count = len(load_cart_from_db()) if 'user_id' in session else len(session.get('cart', []))
    return render_template(
        'perfil.html',
        user=user,
        cart_count=cart_count,
        orders_with_items=orders_with_items,
        linked_accounts=linked_accounts
    )


@app.route('/newsletter', methods=['POST'])
def newsletter():
    data = request.get_json(silent=True) or request.form
    email = data.get('email')
    if not email:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
            return jsonify({'success': False, 'message': 'Correo inválido.'}), 400
        flash('Correo electrónico inválido.', 'error')
        return redirect(request.referrer or url_for('home'))
        
    conn = get_shared_db()
    try:
        conn.execute('INSERT INTO newsletter (correo) VALUES (?)', (email,))
        conn.commit()
        msg = '¡Gracias por suscribirte al boletín!'
        status = 'success'
    except sqlite3.IntegrityError:
        msg = 'Este correo ya está registrado en el boletín.'
        status = 'info'
    finally:
        conn.close()
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
        return jsonify({'success': True, 'message': msg, 'status': status})
    flash(msg, status)
    return redirect(request.referrer or url_for('home'))


@app.route('/login/google')
def login_google():
    username = request.args.get('username', 'Google User')
    email = request.args.get('email', 'google_user@example.com')
    provider_id = request.args.get('provider_id', 'google_123456789')
    link_mode = request.args.get('link') == 'true' or 'user_id' in session
    
    conn = get_shared_db()
    
    if link_mode and 'user_id' in session:
        current_user_id = session['user_id']
        existing = conn.execute(
            'SELECT id_usuario FROM cuentas_vinculadas WHERE proveedor = "google" AND proveedor_id = ? LIMIT 1',
            (provider_id,)
        ).fetchone()
        
        if existing:
            if existing['id_usuario'] == current_user_id:
                flash('Esta cuenta de Google ya está vinculada a tu cuenta.', 'info')
            else:
                flash('Esta cuenta de Google ya está vinculada a otro usuario.', 'error')
            conn.close()
            return redirect(url_for('perfil'))
            
        try:
            conn.execute(
                'INSERT INTO cuentas_vinculadas (id_usuario, proveedor, proveedor_id, proveedor_correo) VALUES (?, "google", ?, ?)',
                (current_user_id, provider_id, email)
            )
            conn.commit()
            flash('Cuenta de Google vinculada exitosamente.', 'success')
        except sqlite3.IntegrityError:
            flash('Ya tienes una cuenta de Google vinculada a este perfil.', 'error')
        finally:
            conn.close()
            
        return redirect(url_for('perfil'))
        
    else:
        linked = conn.execute(
            'SELECT id_usuario FROM cuentas_vinculadas WHERE proveedor = "google" AND proveedor_id = ? LIMIT 1',
            (provider_id,)
        ).fetchone()
        
        if linked:
            user_id = linked['id_usuario']
            user_row = conn.execute('SELECT nombre, correo FROM usuarios WHERE id_usuario = ? LIMIT 1', (user_id,)).fetchone()
            if user_row:
                username = user_row['nombre']
                email = user_row['correo']
            else:
                conn.execute('DELETE FROM cuentas_vinculadas WHERE proveedor = "google" AND proveedor_id = ?', (provider_id,))
                conn.commit()
                linked = None
                
        if not linked:
            user = conn.execute('SELECT id_usuario, nombre FROM usuarios WHERE correo = ? LIMIT 1', (email,)).fetchone()
            if user:
                user_id = user['id_usuario']
                username = user['nombre']
                try:
                    conn.execute(
                        'INSERT INTO cuentas_vinculadas (id_usuario, proveedor, proveedor_id, proveedor_correo) VALUES (?, "google", ?, ?)',
                        (user_id, provider_id, email)
                    )
                    conn.commit()
                except sqlite3.IntegrityError:
                    pass
            else:
                role = conn.execute('SELECT id_rol FROM roles WHERE nombre = ? LIMIT 1', ('Trabajador',)).fetchone()
                role_id = role['id_rol'] if role else 2
                cursor = conn.execute('INSERT INTO usuarios VALUES (NULL, ?, ?, ?, ?)', (username, email, 'oauth_simulated', role_id))
                user_id = cursor.lastrowid
                conn.execute(
                    'INSERT INTO cuentas_vinculadas (id_usuario, proveedor, proveedor_id, proveedor_correo) VALUES (?, "google", ?, ?)',
                    (user_id, provider_id, email)
                )
                conn.commit()
                
        conn.close()
        
        session['user_id'] = user_id
        session['username'] = username
        session['user_email'] = email
        
        guest_cart = session.get('cart', [])
        merge_guest_cart_into_db(guest_cart)
        
        flash('¡Bienvenido! Iniciaste sesión exitosamente con Google.', 'success')
        return redirect(url_for('home'))


@app.route('/login/facebook')
def login_facebook():
    username = request.args.get('username', 'Facebook User')
    email = request.args.get('email', 'facebook_user@example.com')
    provider_id = request.args.get('provider_id', 'facebook_123456789')
    link_mode = request.args.get('link') == 'true' or 'user_id' in session
    
    conn = get_shared_db()
    
    if link_mode and 'user_id' in session:
        current_user_id = session['user_id']
        existing = conn.execute(
            'SELECT id_usuario FROM cuentas_vinculadas WHERE proveedor = "facebook" AND proveedor_id = ? LIMIT 1',
            (provider_id,)
        ).fetchone()
        
        if existing:
            if existing['id_usuario'] == current_user_id:
                flash('Esta cuenta de Facebook ya está vinculada a tu cuenta.', 'info')
            else:
                flash('Esta cuenta de Facebook ya está vinculada a otro usuario.', 'error')
            conn.close()
            return redirect(url_for('perfil'))
            
        try:
            conn.execute(
                'INSERT INTO cuentas_vinculadas (id_usuario, proveedor, proveedor_id, proveedor_correo) VALUES (?, "facebook", ?, ?)',
                (current_user_id, provider_id, email)
            )
            conn.commit()
            flash('Cuenta de Facebook vinculada exitosamente.', 'success')
        except sqlite3.IntegrityError:
            flash('Ya tienes una cuenta de Facebook vinculada a este perfil.', 'error')
        finally:
            conn.close()
            
        return redirect(url_for('perfil'))
        
    else:
        linked = conn.execute(
            'SELECT id_usuario FROM cuentas_vinculadas WHERE proveedor = "facebook" AND proveedor_id = ? LIMIT 1',
            (provider_id,)
        ).fetchone()
        
        if linked:
            user_id = linked['id_usuario']
            user_row = conn.execute('SELECT nombre, correo FROM usuarios WHERE id_usuario = ? LIMIT 1', (user_id,)).fetchone()
            if user_row:
                username = user_row['nombre']
                email = user_row['correo']
            else:
                conn.execute('DELETE FROM cuentas_vinculadas WHERE proveedor = "facebook" AND proveedor_id = ?', (provider_id,))
                conn.commit()
                linked = None
                
        if not linked:
            user = conn.execute('SELECT id_usuario, nombre FROM usuarios WHERE correo = ? LIMIT 1', (email,)).fetchone()
            if user:
                user_id = user['id_usuario']
                username = user['nombre']
                try:
                    conn.execute(
                        'INSERT INTO cuentas_vinculadas (id_usuario, proveedor, proveedor_id, proveedor_correo) VALUES (?, "facebook", ?, ?)',
                        (user_id, provider_id, email)
                    )
                    conn.commit()
                except sqlite3.IntegrityError:
                    pass
            else:
                role = conn.execute('SELECT id_rol FROM roles WHERE nombre = ? LIMIT 1', ('Trabajador',)).fetchone()
                role_id = role['id_rol'] if role else 2
                cursor = conn.execute('INSERT INTO usuarios VALUES (NULL, ?, ?, ?, ?)', (username, email, 'oauth_simulated', role_id))
                user_id = cursor.lastrowid
                conn.execute(
                    'INSERT INTO cuentas_vinculadas (id_usuario, proveedor, proveedor_id, proveedor_correo) VALUES (?, "facebook", ?, ?)',
                    (user_id, provider_id, email)
                )
                conn.commit()
                
        conn.close()
        
        session['user_id'] = user_id
        session['username'] = username
        session['user_email'] = email
        
        guest_cart = session.get('cart', [])
        merge_guest_cart_into_db(guest_cart)
        
        flash('¡Bienvenido! Iniciaste sesión exitosamente con Facebook.', 'success')
        return redirect(url_for('home'))


@app.route('/perfil/desvincular/<proveedor>', methods=['POST'])
def unlink_social(proveedor):
    if 'user_id' not in session:
        flash('Debes iniciar sesión para desvincular una cuenta.', 'error')
        return redirect(url_for('login'))
        
    if proveedor not in ['google', 'facebook']:
        flash('Proveedor inválido.', 'error')
        return redirect(url_for('perfil'))
        
    conn = get_shared_db()
    try:
        user = conn.execute('SELECT contraseña FROM usuarios WHERE id_usuario = ? LIMIT 1', (session['user_id'],)).fetchone()
        other_links = conn.execute('SELECT COUNT(*) AS total FROM cuentas_vinculadas WHERE id_usuario = ? AND proveedor != ?', (session['user_id'], proveedor)).fetchone()
        
        has_password = user and user['contraseña'] != 'oauth_simulated' and len(user['contraseña']) > 0
        has_other_link = other_links and other_links['total'] > 0
        
        if not has_password and not has_other_link:
            flash('No puedes desvincular esta cuenta. Debes establecer una contraseña o vincular otro método de inicio de sesión primero.', 'error')
        else:
            conn.execute('DELETE FROM cuentas_vinculadas WHERE id_usuario = ? AND proveedor = ?', (session['user_id'], proveedor))
            conn.commit()
            flash(f'Cuenta de {proveedor.capitalize()} desvinculada exitosamente.', 'success')
    except Exception as e:
        flash('Ocurrió un error al desvincular la cuenta.', 'error')
    finally:
        conn.close()
        
    return redirect(url_for('perfil'))


@app.route('/producto/<int:id>/review', methods=['POST'])
def submit_review(id):
    if 'user_id' not in session:
        flash('Debes iniciar sesión para dejar una reseña.', 'error')
        return redirect(url_for('login'))
        
    if not user_can_review(session.get('user_email'), id):
        flash('Solo puedes calificar productos que hayas comprado y recibido.', 'error')
        return redirect(url_for('producto', id=id))
        
    puntuacion = request.form.get('rating')
    comentario = request.form.get('comment')
    
    if not puntuacion or not (1 <= int(puntuacion) <= 5):
        flash('Por favor selecciona una puntuación válida (1-5 estrellas).', 'error')
        return redirect(url_for('producto', id=id))
        
    conn = get_shared_db()
    try:
        existing = conn.execute(
            'SELECT id_resena FROM reseñas WHERE id_usuario = ? AND id_producto = ? LIMIT 1',
            (session['user_id'], id)
        ).fetchone()
        
        if existing:
            conn.execute(
                'UPDATE reseñas SET puntuacion = ?, comentario = ?, fecha = CURRENT_TIMESTAMP WHERE id_resena = ?',
                (int(puntuacion), comentario, existing['id_resena'])
            )
        else:
            conn.execute(
                'INSERT INTO reseñas (id_usuario, id_producto, puntuacion, comentario) VALUES (?, ?, ?, ?)',
                (session['user_id'], id, int(puntuacion), comentario)
            )
        conn.commit()
        flash('¡Gracias por tu reseña!', 'success')
    except Exception as e:
        flash('Ocurrió un error al guardar tu reseña.', 'error')
    finally:
        conn.close()
        
    return redirect(url_for('producto', id=id))


if __name__ == '__main__':
    app.run(debug=True, port=5000)
