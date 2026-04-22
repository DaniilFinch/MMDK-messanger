import sqlite3
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = 'mmdk_ultra_secret_key'


# Функция для конвертации UTC времени в локальное (Москва UTC+3)
def convert_to_local_time(utc_time_str):
    """Конвертирует UTC время из БД в московское время"""
    if not utc_time_str:
        return ""

    try:
        # Парсим время из БД (оно в UTC)
        if isinstance(utc_time_str, str):
            utc_time = datetime.strptime(utc_time_str, '%Y-%m-%d %H:%M:%S')
        else:
            utc_time = utc_time_str


        local_time = utc_time + timedelta(hours=4)

        return local_time.strftime('%d.%m.%Y %H:%M:%S')
    except:
        return str(utc_time_str)


# Функция для подключения к БД
def get_db_connection():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn


# Создание таблиц при старте
def init_db():
    conn = get_db_connection()

    # Таблица пользователей
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Таблица сообщений
    conn.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            is_read BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (sender_id) REFERENCES users (id),
            FOREIGN KEY (receiver_id) REFERENCES users (id)
        )
    ''')

    conn.commit()
    conn.close()


init_db()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/qw')
def qw():
    now = datetime.now() + timedelta(hours=3)  # Московское время
    return render_template('qw.html', current_date=now.strftime("%d.%m.%Y"), current_year=now.year)


@app.route('/register', methods=['POST'])
def register():
    username = request.form.get('username')
    email = request.form.get('email')
    password = request.form.get('password')

    hashed_password = generate_password_hash(password)

    conn = get_db_connection()
    try:
        conn.execute('INSERT INTO users (username, email, password) VALUES (?, ?, ?)',
                     (username, email, hashed_password))
        conn.commit()
        flash('Регистрация успешна! Теперь войдите.')
    except sqlite3.IntegrityError:
        flash('Ошибка: этот Email уже зарегистрирован.')
    finally:
        conn.close()

    return redirect(url_for('index'))


@app.route('/login', methods=['POST'])
def login():
    email = request.form.get('email')
    password = request.form.get('password')

    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
    conn.close()

    if user and check_password_hash(user['password'], password):
        session['user_id'] = user['id']
        session['username'] = user['username']
        return redirect(url_for('profile'))
    else:
        flash('Неверный email или пароль.')
        return redirect(url_for('index'))


@app.route('/logout')
def logout():
    session.clear()
    flash('Вы вышли из системы.')
    return redirect(url_for('index'))


@app.route('/profile')
def profile():
    if 'user_id' not in session:
        flash("Пожалуйста, войдите в аккаунт.")
        return redirect(url_for('index'))

    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()

    # Конвертируем время регистрации в локальное
    if user and user['created_at']:
        user = dict(user)
        user['created_at'] = convert_to_local_time(user['created_at'])

    # Получаем список пользователей для чата (кроме текущего)
    users = conn.execute('SELECT id, username FROM users WHERE id != ?', (session['user_id'],)).fetchall()

    # Получаем последние сообщения для каждого диалога
    dialogs = []
    for other_user in users:
        last_message = conn.execute('''
            SELECT message, created_at, sender_id 
            FROM messages 
            WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)
            ORDER BY created_at DESC LIMIT 1
        ''', (session['user_id'], other_user['id'], other_user['id'], session['user_id'])).fetchone()

        # Конвертируем время последнего сообщения
        if last_message:
            last_message = dict(last_message)
            if last_message['created_at']:
                last_message['created_at'] = convert_to_local_time(last_message['created_at'])

        # Считаем непрочитанные сообщения
        unread_count = conn.execute('''
            SELECT COUNT(*) as count FROM messages 
            WHERE receiver_id = ? AND sender_id = ? AND is_read = 0
        ''', (session['user_id'], other_user['id'])).fetchone()

        dialogs.append({
            'user': other_user,
            'last_message': last_message,
            'unread_count': unread_count['count']
        })

    conn.close()
    return render_template('profile.html', user=user, dialogs=dialogs)


@app.route('/chat/<int:user_id>')
def chat(user_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))

    conn = get_db_connection()

    # Получаем информацию о собеседнике
    other_user = conn.execute('SELECT id, username FROM users WHERE id = ?', (user_id,)).fetchone()
    if not other_user:
        flash('Пользователь не найден')
        return redirect(url_for('profile'))

    # Помечаем все сообщения от этого пользователя как прочитанные
    conn.execute('''
        UPDATE messages SET is_read = 1 
        WHERE sender_id = ? AND receiver_id = ?
    ''', (user_id, session['user_id']))
    conn.commit()

    # Получаем все сообщения между пользователями
    messages = conn.execute('''
        SELECT * FROM messages 
        WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)
        ORDER BY created_at ASC
    ''', (session['user_id'], user_id, user_id, session['user_id'])).fetchall()

    # Конвертируем время каждого сообщения в локальное
    messages_list = []
    for msg in messages:
        msg_dict = dict(msg)
        if msg_dict['created_at']:
            msg_dict['created_at'] = convert_to_local_time(msg_dict['created_at'])
        messages_list.append(msg_dict)

    conn.close()

    return render_template('chat.html', other_user=other_user, messages=messages_list)


@app.route('/send_message', methods=['POST'])
def send_message():
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    receiver_id = request.form.get('receiver_id')
    message = request.form.get('message')

    if not message or not message.strip():
        return jsonify({'error': 'Message is empty'}), 400

    conn = get_db_connection()
    conn.execute('''
        INSERT INTO messages (sender_id, receiver_id, message)
        VALUES (?, ?, ?)
    ''', (session['user_id'], receiver_id, message.strip()))
    conn.commit()
    conn.close()

    return jsonify({'success': True})


@app.route('/get_messages/<int:user_id>')
def get_messages(user_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    last_id = request.args.get('last_id', 0, type=int)

    conn = get_db_connection()
    messages = conn.execute('''
        SELECT id, sender_id, message, created_at 
        FROM messages 
        WHERE ((sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?))
        AND id > ?
        ORDER BY created_at ASC
    ''', (session['user_id'], user_id, user_id, session['user_id'], last_id)).fetchall()

    # Помечаем новые сообщения как прочитанные
    for msg in messages:
        if msg['sender_id'] == user_id:
            conn.execute('UPDATE messages SET is_read = 1 WHERE id = ?', (msg['id'],))
    conn.commit()

    # Конвертируем время каждого сообщения
    messages_list = []
    for msg in messages:
        msg_dict = dict(msg)
        if msg_dict['created_at']:
            msg_dict['created_at'] = convert_to_local_time(msg_dict['created_at'])
        messages_list.append(msg_dict)

    conn.close()

    return jsonify(messages_list)


@app.route('/search_users')
def search_users():
    if 'user_id' not in session:
        return jsonify([])

    query = request.args.get('q', '')
    if not query:
        return jsonify([])

    conn = get_db_connection()
    users = conn.execute('''
        SELECT id, username, email 
        FROM users 
        WHERE id != ? AND (username LIKE ? OR email LIKE ?)
        LIMIT 10
    ''', (session['user_id'], f'%{query}%', f'%{query}%')).fetchall()
    conn.close()

    return jsonify([dict(user) for user in users])


@app.route('/get_unread_count')
def get_unread_count():
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    conn = get_db_connection()
    unread = conn.execute('''
        SELECT sender_id, COUNT(*) as count 
        FROM messages 
        WHERE receiver_id = ? AND is_read = 0
        GROUP BY sender_id
    ''', (session['user_id'],)).fetchall()
    conn.close()

    return jsonify([dict(u) for u in unread])


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
