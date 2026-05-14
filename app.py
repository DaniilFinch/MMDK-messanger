import os
import sqlite3
import json
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'mmdk_ultra_secret_key'

UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def get_db_connection():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn

def convert_to_local_time(utc_time_str):
    if not utc_time_str: return ""
    try:
        dt = datetime.strptime(utc_time_str, '%Y-%m-%d %H:%M:%S') if isinstance(utc_time_str, str) else utc_time_str
        return (dt + timedelta(hours=4)).strftime('%H:%M')
    except:
        return str(utc_time_str)

# --- ИНИЦИАЛИЗАЦИЯ БД ---
def init_db():
    conn = get_db_connection()

    # Существующие таблицы
    conn.execute(
        'CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    conn.execute(
        'CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id INTEGER NOT NULL, receiver_id INTEGER NOT NULL, message TEXT NOT NULL, is_read BOOLEAN DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')

    # НОВЫЕ ТАБЛИЦЫ ДЛЯ ГРУПП
    conn.execute('''
        CREATE TABLE IF NOT EXISTS group_chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            avatar TEXT DEFAULT '👥',
            created_by INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (created_by) REFERENCES users(id)
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS group_chat_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            is_admin INTEGER DEFAULT 0,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES group_chats(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(group_id, user_id)
        )
    ''')

    # Новая таблица для банов
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            banned_by INTEGER NOT NULL,
            reason TEXT DEFAULT '',
            ban_until TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (group_id) REFERENCES group_chats(id),
            FOREIGN KEY (banned_by) REFERENCES users(id)
        )
    ''')

    # Обновляем messages: receiver_id > 0 — личные, receiver_id = -group_id — групповые
    # или создаём новую колонку chat_type
    try:
        conn.execute('ALTER TABLE messages ADD COLUMN chat_type TEXT DEFAULT "private"')
    except:
        pass

    conn.commit()
    conn.close()

def create_group_members_table():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS group_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            is_admin INTEGER DEFAULT 0,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def migrate_db():
    conn = get_db_connection()
    try:
        conn.execute('ALTER TABLE users ADD COLUMN last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
        print("✅ Добавлена колонка last_seen")
    except: pass
    try:
        conn.execute('ALTER TABLE users ADD COLUMN privacy_settings TEXT DEFAULT "{}"')
        print("✅ Добавлена колонка privacy_settings")
    except: pass
    try:
        conn.execute('ALTER TABLE users ADD COLUMN status_type TEXT DEFAULT "online"')
        print("✅ Добавлена колонка status_type")
    except: pass
    conn.commit()
    conn.close()

# --- МАРШРУТЫ ---
@app.route('/')
def index():
    if 'user_id' in session: return redirect(url_for('profile'))
    return render_template('index.html')


@app.route('/ban_user', methods=['POST'])
def ban_user():
    """Забанить пользователя в группе"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Не авторизован'})

    data = request.json
    group_id = data.get('group_id')
    user_id = data.get('user_id')
    duration = data.get('duration', 60)  # по умолчанию 60 секунд
    reason = data.get('reason', '')

    print(f"Бан: group_id={group_id}, user_id={user_id}, duration={duration}")  # Отладка

    if not group_id or not user_id:
        return jsonify({'success': False, 'error': 'Не указаны данные'})

    conn = get_db_connection()

    # Проверяем, является ли текущий пользователь админом
    admin_check = conn.execute('''
        SELECT is_admin FROM group_chat_members 
        WHERE group_id = ? AND user_id = ?
    ''', (group_id, session['user_id'])).fetchone()

    if not admin_check or admin_check['is_admin'] != 1:
        conn.close()
        return jsonify({'success': False, 'error': 'Только администратор может банить'})

    # Нельзя забанить себя
    if user_id == session['user_id']:
        conn.close()
        return jsonify({'success': False, 'error': 'Нельзя забанить самого себя'})

    # Рассчитываем время окончания бана
    ban_until = datetime.now() + timedelta(seconds=duration)

    # Добавляем бан
    try:
        conn.execute('''
            INSERT INTO bans (user_id, group_id, banned_by, reason, ban_until)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, group_id, session['user_id'], reason, ban_until))
        conn.commit()
        print(f"✅ Бан добавлен для {user_id} до {ban_until}")
    except Exception as e:
        print(f"Ошибка добавления бана: {e}")
        conn.close()
        return jsonify({'success': False, 'error': str(e)})

    # Удаляем пользователя из группы
    conn.execute('DELETE FROM group_chat_members WHERE group_id = ? AND user_id = ?',
                 (group_id, user_id))

    # Системное сообщение
    admin = conn.execute('SELECT username FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    banned = conn.execute('SELECT username FROM users WHERE id = ?', (user_id,)).fetchone()

    duration_str = format_duration(duration)
    reason_str = f" Причина: {reason}" if reason else ""

    conn.execute('''
        INSERT INTO messages (sender_id, receiver_id, message, chat_type, group_id) 
        VALUES (0, ?, ?, 'group', ?)
    ''', (group_id, f"🔨 {admin['username']} забанил(а) {banned['username']} на {duration_str}{reason_str}", group_id))

    conn.commit()
    conn.close()

    return jsonify({'success': True, 'ban_until': ban_until.isoformat()})


@app.route('/check_ban', methods=['GET'])
def check_ban():
    """Проверить, забанен ли пользователь в группе"""
    if 'user_id' not in session:
        return jsonify({'is_banned': False})

    group_id = request.args.get('group_id', 0, type=int)
    user_id = session['user_id']

    if group_id == 0:
        return jsonify({'is_banned': False})

    conn = get_db_connection()
    ban = conn.execute('''
        SELECT * FROM bans 
        WHERE user_id = ? AND group_id = ? AND ban_until > ?
    ''', (user_id, group_id, datetime.now())).fetchone()
    conn.close()

    if ban:
        return jsonify({'is_banned': True, 'ban_until': ban['ban_until']})
    return jsonify({'is_banned': False})


def format_duration(seconds):
    """Форматирует секунды в человекочитаемый вид"""
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    parts = []
    if days > 0: parts.append(f"{days}д")
    if hours > 0: parts.append(f"{hours}ч")
    if minutes > 0: parts.append(f"{minutes}м")
    if secs > 0 or not parts: parts.append(f"{secs}с")

    return ' '.join(parts)


def parse_duration(duration_str):
    """Парсит строку типа '1d2h30m' в секунды"""
    import re
    total = 0
    patterns = {
        'd': 86400,
        'ч': 3600,
        'h': 3600,
        'м': 60,
        'm': 60,
        'с': 1,
        's': 1
    }

    for unit, seconds in patterns.items():
        match = re.search(r'(\d+)\s*' + unit, duration_str)
        if match:
            total += int(match.group(1)) * seconds

    return total

@app.route('/register', methods=['POST'])
def register():
    username, email, password = request.form.get('username'), request.form.get('email'), request.form.get('password')
    conn = get_db_connection()
    try:
        conn.execute('INSERT INTO users (username, email, password) VALUES (?, ?, ?)',
                     (username, email, generate_password_hash(password)))
        conn.commit()
        flash('Регистрация успешна!')
    except:
        flash('Ошибка: Email уже занят')
    finally:
        conn.close()
    return redirect(url_for('index'))


@app.route('/login', methods=['POST'])
def login():
    email, password = request.form.get('email'), request.form.get('password')
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()

    if user and check_password_hash(user['password'], password):
        session.update({'user_id': user['id'], 'username': user['username'], 'email': user['email']})

        # Обновляем last_seen при входе
        conn.execute('UPDATE users SET last_seen = ? WHERE id = ?', (datetime.now(), user['id']))
        conn.commit()

        conn.close()
        return redirect(url_for('profile'))

    conn.close()
    flash('Неверные данные')
    return redirect(url_for('index'))


@app.route('/create_group', methods=['POST'])
def create_group():
    """Создать новый групповой чат"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Не авторизован'})

    data = request.json
    group_name = data.get('name', 'Новая группа')

    conn = get_db_connection()

    # Создаём группу
    cursor = conn.execute('INSERT INTO group_chats (name, created_by) VALUES (?, ?)',
                          (group_name, session['user_id']))
    group_id = cursor.lastrowid

    # Добавляем создателя как админа (ТОЛЬКО СОЗДАТЕЛЯ!)
    conn.execute('INSERT INTO group_chat_members (group_id, user_id, is_admin) VALUES (?, ?, 1)',
                 (group_id, session['user_id']))


    conn.commit()
    conn.close()

    return jsonify({'success': True, 'group_id': group_id, 'name': group_name})


@app.route('/get_my_groups')
def get_my_groups():
    """Получить все группы, где состоит пользователь"""
    if 'user_id' not in session:
        return jsonify([])

    conn = get_db_connection()
    groups = conn.execute('''
        SELECT gc.id, gc.name, gc.avatar, gc.created_at,
               (SELECT COUNT(*) FROM group_chat_members WHERE group_id = gc.id) as member_count
        FROM group_chats gc
        INNER JOIN group_chat_members gcm ON gc.id = gcm.group_id
        WHERE gcm.user_id = ?
        ORDER BY gc.created_at DESC
    ''', (session['user_id'],)).fetchall()

    conn.close()
    return jsonify([dict(g) for g in groups])


@app.route('/group/<int:group_id>')
def group_detail(group_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))

    conn = get_db_connection()

    # ПРОВЕРКА: не забанен ли пользователь
    ban = conn.execute('''
        SELECT * FROM bans 
        WHERE user_id = ? AND group_id = ? AND ban_until > ?
    ''', (session['user_id'], group_id, datetime.now())).fetchone()

    if ban:
        # Исправленный парсинг даты (поддержка микросекунд)
        ban_until_str = ban['ban_until']
        if isinstance(ban_until_str, str):
            # Убираем микросекунды если есть
            if '.' in ban_until_str:
                ban_until_str = ban_until_str.split('.')[0]
            ban_until = datetime.strptime(ban_until_str, '%Y-%m-%d %H:%M:%S')
        else:
            ban_until = ban_until_str
        conn.close()
        return f"❌ Вы забанены в этом чате до {ban_until.strftime('%d.%m.%Y %H:%M')}", 403

    # Проверяем, состоит ли пользователь в группе
    member = conn.execute('SELECT * FROM group_chat_members WHERE group_id = ? AND user_id = ?',
                          (group_id, session['user_id'])).fetchone()

    if not member:
        conn.close()
        return "Вы не состоите в этой группе", 403

    group = conn.execute('SELECT * FROM group_chats WHERE id = ?', (group_id,)).fetchone()

    messages = conn.execute('''
        SELECT m.*, u.username 
        FROM messages m 
        LEFT JOIN users u ON m.sender_id = u.id 
        WHERE m.chat_type = 'group' AND m.group_id = ?
        ORDER BY m.created_at ASC
    ''', (group_id,)).fetchall()

    conn.close()
    return render_template('group_chat.html', group=group, messages=messages, group_id=group_id)


@app.route('/send_group_message', methods=['POST'])
def send_group_message():
    """Отправить сообщение в группу"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Не авторизован'})

    data = request.json
    group_id = data.get('group_id')
    message = data.get('message')

    if not group_id or not message:
        return jsonify({'success': False, 'error': 'Не указаны данные'})

    conn = get_db_connection()

    # Проверяем, состоит ли пользователь в группе
    member = conn.execute('''
        SELECT * FROM group_chat_members 
        WHERE group_id = ? AND user_id = ?
    ''', (group_id, session['user_id'])).fetchone()

    if not member:
        conn.close()
        return jsonify({'success': False, 'error': 'Вы не состоите в этой группе'})

    # ВАЖНО: chat_type = 'group', receiver_id = group_id
    conn.execute('''
        INSERT INTO messages (sender_id, receiver_id, message, chat_type, group_id) 
        VALUES (?, ?, ?, 'group', ?)
    ''', (session['user_id'], group_id, message, group_id))

    conn.commit()
    conn.close()

    return jsonify({'success': True})


@app.route('/get_bans')
def get_bans():
    if 'user_id' not in session:
        return jsonify([])

    group_id = request.args.get('group_id', 0, type=int)

    conn = get_db_connection()
    bans = conn.execute('''
        SELECT b.*, u.username 
        FROM bans b
        JOIN users u ON b.user_id = u.id
        WHERE b.group_id = ? AND b.ban_until > ?
        ORDER BY b.ban_until DESC
    ''', (group_id, datetime.now())).fetchall()
    conn.close()

    return jsonify([dict(b) for b in bans])


@app.route('/get_group_messages')
def get_group_messages():
    if 'user_id' not in session:
        return jsonify([])

    last_id = request.args.get('last_id', 0, type=int)
    group_id = request.args.get('group_id', 0, type=int)

    if group_id == 0:
        return jsonify([])

    conn = get_db_connection()

    # Получаем ТОЛЬКО сообщения этой группы
    messages = conn.execute('''
        SELECT m.*, u.username 
        FROM messages m 
        LEFT JOIN users u ON m.sender_id = u.id 
        WHERE m.chat_type = 'group' AND m.group_id = ? AND m.id > ? 
        ORDER BY m.id ASC
    ''', (group_id, last_id)).fetchall()

    conn.close()
    return jsonify([dict(m) for m in messages])


@app.route('/invite_to_group_chat', methods=['POST'])
def invite_to_group_chat():
    """Пригласить пользователя в конкретную группу"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Не авторизован'})

    data = request.json
    group_id = data.get('group_id')
    invited_user_id = data.get('user_id')

    if not group_id or not invited_user_id:
        return jsonify({'success': False, 'error': 'Не указаны данные'})

    conn = get_db_connection()

    # ПРОВЕРКА: не запретил ли пользователь приглашения
    invited_user = conn.execute('SELECT id, username, privacy_settings FROM users WHERE id = ?',
                                (invited_user_id,)).fetchone()

    if invited_user:
        disable_invites = False
        if invited_user['privacy_settings']:
            try:
                settings = json.loads(invited_user['privacy_settings'])
                disable_invites = settings.get('disable_invites', False)
            except:
                pass

        if disable_invites:
            conn.close()
            return jsonify({'success': False, 'error': 'Пользователь запретил приглашения'})

    # Проверяем, не состоит ли уже пользователь в группе
    existing = conn.execute('SELECT * FROM group_chat_members WHERE group_id = ? AND user_id = ?',
                            (group_id, invited_user_id)).fetchone()

    if existing:
        conn.close()
        return jsonify({'success': False, 'error': 'Пользователь уже в группе'})

    # Добавляем пользователя в группу
    conn.execute('INSERT INTO group_chat_members (group_id, user_id, is_admin) VALUES (?, ?, 0)',
                 (group_id, invited_user_id))

    group = conn.execute('SELECT name FROM group_chats WHERE id = ?', (group_id,)).fetchone()
    inviter = conn.execute('SELECT username FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    invited = conn.execute('SELECT username FROM users WHERE id = ?', (invited_user_id,)).fetchone()

    # Системное сообщение о приглашении
    message = f"🔔 {inviter['username']} пригласил(а) {invited['username']} в группу '{group['name']}'"
    conn.execute('''
        INSERT INTO messages (sender_id, receiver_id, message, chat_type, group_id) 
        VALUES (?, ?, ?, 'group', ?)
    ''', (session['user_id'], group_id, message, group_id))

    conn.commit()
    conn.close()

    return jsonify({'success': True})

@app.route('/profile')
def profile():
    if 'user_id' not in session: return redirect(url_for('index'))
    user_id = session['user_id']
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    active_users = conn.execute('''
        SELECT DISTINCT u.id, u.username FROM users u
        JOIN messages m ON (u.id = m.sender_id OR u.id = m.receiver_id)
        WHERE (m.sender_id = ? OR m.receiver_id = ?) AND u.id != ? AND m.receiver_id != 0
    ''', (user_id, user_id, user_id)).fetchall()
    dialogs = []
    for other in active_users:
        last = conn.execute('SELECT message, sender_id FROM messages WHERE (sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?) ORDER BY created_at DESC LIMIT 1',
            (user_id, other['id'], other['id'], user_id)).fetchone()
        unread = conn.execute('SELECT COUNT(*) as count FROM messages WHERE receiver_id=? AND sender_id=? AND is_read=0',
            (user_id, other['id'])).fetchone()
        msg_preview = last['message'] if last else ""
        if msg_preview.startswith('[IMAGE]'): msg_preview = "📷 Фотография"
        dialogs.append({'user': other, 'last_message': msg_preview, 'unread': unread['count']})
    conn.close()
    return render_template('profile.html', user=user, dialogs=dialogs)

@app.route('/chat/<int:user_id>')
def chat(user_id):
    if 'user_id' not in session: return "401", 401
    conn = get_db_connection()
    other_user = conn.execute('SELECT id, username FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.execute('UPDATE messages SET is_read = 1 WHERE sender_id = ? AND receiver_id = ?', (user_id, session['user_id']))
    messages = conn.execute('SELECT * FROM messages WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?) ORDER BY created_at ASC',
        (session['user_id'], user_id, user_id, session['user_id'])).fetchall()
    msg_list = [dict(m) for m in messages]
    for m in msg_list: m['created_at'] = convert_to_local_time(m['created_at'])
    conn.commit()
    conn.close()
    return render_template('chat.html', other_user=other_user, messages=msg_list)


@app.route('/send_msg', methods=['POST'])
def send_msg():
    """Отправить личное сообщение"""
    if 'user_id' not in session:
        return jsonify({'success': False})

    data = request.json
    receiver_id = data.get('receiver_id')
    message = data.get('message')

    if not receiver_id or not message:
        return jsonify({'success': False})

    conn = get_db_connection()
    conn.execute('''
        INSERT INTO messages (sender_id, receiver_id, message, chat_type) 
        VALUES (?, ?, ?, 'private')
    ''', (session['user_id'], receiver_id, message))
    conn.commit()
    conn.close()

    return jsonify({'success': True})

@app.route('/send_group_msg_ajax', methods=['POST'])
def send_group_msg_ajax():
    data = request.json
    message = data.get('message')
    group_id = data.get('group_id', 0)

    conn = get_db_connection()
    conn.execute('''
        INSERT INTO messages (sender_id, receiver_id, message, chat_type, group_id) 
        VALUES (?, 0, ?, 'group', ?)
    ''', (session['user_id'], message, group_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/upload_file', methods=['POST'])
def upload_file():
    file = request.files.get('file')
    group_id = request.form.get('group_id')
    reply_to = request.form.get('reply_to')

    # Обработка reply_to: если пришла пустая строка или null, записываем None (NULL в БД)
    if not reply_to or reply_to == 'null':
        reply_to = None

    if file and 'user_id' in session:
        # Убедимся, что папка существует, чтобы сервер не падал
        upload_path = os.path.join('static', 'uploads')
        if not os.path.exists(upload_path):
            os.makedirs(upload_path)

        filename = secure_filename(f"{datetime.now().timestamp()}_{file.filename}")
        file.save(os.path.join(upload_path, filename))

        conn = get_db_connection()
        try:
            # ИСПРАВЛЕНО: Теперь ровно 6 колонок и 6 значений в кортеже
            conn.execute('''
                INSERT INTO messages (sender_id, receiver_id, group_id, message, chat_type, reply_to) 
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (session['user_id'], group_id, group_id, f"[IMAGE]{filename}", 'group', reply_to))
            conn.commit()
            return jsonify(success=True)
        except Exception as e:
            print(f"Ошибка БД при фото: {e}")
            return jsonify(success=False, error=str(e)), 500
        finally:
            conn.close()

    return jsonify(success=False, error="Файл не найден или сессия истекла"), 400


@app.route('/logout')
def logout():
    if 'user_id' in session:
        # Обновляем last_seen при выходе, чтобы статус стал офлайн
        conn = get_db_connection()
        conn.execute('UPDATE users SET last_seen = ? WHERE id = ?',
                     (datetime.now(), session['user_id']))
        conn.commit()
        conn.close()

    session.clear()
    return redirect(url_for('index'))

@app.route('/qw')
def qw():
    now = datetime.now() + timedelta(hours=4)
    return render_template('qw.html', current_date=now.strftime("%d.%m.%Y"), current_year=now.year)

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/search_users')
def search_users():
    if 'user_id' not in session:
        return jsonify([])
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])
    conn = get_db_connection()
    users = conn.execute('SELECT id, username FROM users WHERE id != ? AND (username LIKE ? OR CAST(id AS TEXT) LIKE ?) LIMIT 20',
        (session['user_id'], f'%{query}%', f'%{query}%')).fetchall()
    conn.close()
    return jsonify([{'id': u['id'], 'username': u['username']} for u in users])

@app.route('/get_dialogs')
def get_dialogs():
    if 'user_id' not in session: return jsonify([])
    user_id = session['user_id']
    conn = get_db_connection()
    dialogs = conn.execute('''
        SELECT DISTINCT 
            CASE WHEN m.sender_id = ? THEN m.receiver_id ELSE m.sender_id END as other_id,
            u.username
        FROM messages m
        JOIN users u ON u.id = CASE WHEN m.sender_id = ? THEN m.receiver_id ELSE m.sender_id END
        WHERE (m.sender_id = ? OR m.receiver_id = ?) AND m.receiver_id != 0 AND u.id != ?
    ''', (user_id, user_id, user_id, user_id, user_id)).fetchall()
    result = []
    for d in dialogs:
        last_msg = conn.execute('SELECT message FROM messages WHERE ((sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)) ORDER BY created_at DESC LIMIT 1',
            (user_id, d['other_id'], d['other_id'], user_id)).fetchone()
        unread = conn.execute('SELECT COUNT(*) as cnt FROM messages WHERE sender_id = ? AND receiver_id = ? AND is_read = 0',
            (d['other_id'], user_id)).fetchone()
        msg_preview = last_msg['message'] if last_msg else ""
        if msg_preview.startswith('[IMAGE]'): msg_preview = "📷 Фото"
        result.append({'id': d['other_id'], 'username': d['username'], 'last_message': msg_preview, 'unread': unread['cnt'] if unread else 0})
    conn.close()
    return jsonify(result)


@app.route('/get_private_messages/<int:other_id>')
def get_private_messages(other_id):
    if 'user_id' not in session:
        return jsonify([])
    last_id = request.args.get('last_id', 0, type=int)
    my_id = session['user_id']

    conn = get_db_connection()

    # Отмечаем как прочитанные ТОЛЬКО личные сообщения
    conn.execute('''
        UPDATE messages SET is_read = 1 
        WHERE sender_id = ? AND receiver_id = ? AND chat_type = 'private' AND is_read = 0
    ''', (other_id, my_id))

    # Получаем ТОЛЬКО личные сообщения
    messages = conn.execute('''
        SELECT id, sender_id, receiver_id, message, is_read, created_at 
        FROM messages 
        WHERE chat_type = 'private'
        AND ((sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?))
        AND id > ?
        ORDER BY created_at ASC
    ''', (my_id, other_id, other_id, my_id, last_id)).fetchall()

    conn.commit()
    conn.close()

    return jsonify([dict(m) for m in messages])


@app.route('/get_group_members')
def get_group_members():
    if 'user_id' not in session: return jsonify([])
    conn = get_db_connection()
    members = conn.execute('SELECT u.id, u.username, gm.is_admin FROM users u INNER JOIN group_members gm ON u.id = gm.user_id').fetchall()
    conn.close()
    return jsonify([{'id': m['id'], 'username': m['username'], 'is_admin': m['is_admin']} for m in members])


@app.route('/get_users_status')
def get_users_status():
    """Получить статусы всех пользователей (простая версия)"""
    if 'user_id' not in session:
        return jsonify({})

    # Получаем список всех пользователей
    conn = get_db_connection()
    users = conn.execute('SELECT id, username, status_type, privacy_settings FROM users').fetchall()
    conn.close()

    result = {}

    for u in users:
        # Проверяем настройки приватности
        privacy = {}
        if u['privacy_settings']:
            try:
                privacy = json.loads(u['privacy_settings'])
            except:
                pass

        # Если скрыт статус - офлайн
        if privacy.get('hide_online_status', False):
            result[u['id']] = 'offline'
            continue

        # Ручной статус
        status_type = u['status_type'] if u['status_type'] else 'auto'

        if status_type == 'dnd':
            result[u['id']] = 'dnd'
        elif status_type == 'offline':
            result[u['id']] = 'offline'
        elif status_type == 'idle':
            result[u['id']] = 'idle'
        elif status_type == 'online':
            result[u['id']] = 'online'

    # Для отладки
    print(f"Статусы: {result}")

    return jsonify(result)


@app.route('/set_offline', methods=['POST'])
def set_offline():
    """Установить статус офлайн для текущего пользователя"""
    if 'user_id' in session:
        conn = get_db_connection()
        conn.execute("UPDATE users SET status_type = 'offline' WHERE id = ?", (session['user_id'],))
        conn.commit()
        conn.close()
    return jsonify({'success': True})

@app.route('/get_privacy_settings')
def get_privacy_settings():
    if 'user_id' not in session:
        return jsonify({'hide_online_status': False, 'disable_invites': False, 'status_type': 'online'})
    conn = get_db_connection()
    user = conn.execute('SELECT privacy_settings, status_type FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    conn.close()
    result = {'hide_online_status': False, 'disable_invites': False, 'status_type': 'online'}
    if user and user['privacy_settings']:
        try:
            settings = json.loads(user['privacy_settings'])
            result['hide_online_status'] = settings.get('hide_online_status', False)
            result['disable_invites'] = settings.get('disable_invites', False)
        except: pass
    if user and user['status_type']:
        result['status_type'] = user['status_type']
    return jsonify(result)

@app.route('/update_activity', methods=['POST'])
def update_activity():
    """Обновить время последней активности пользователя"""
    if 'user_id' in session:
        try:
            conn = get_db_connection()
            # Используйте datetime.now() вместо CURRENT_TIMESTAMP
            conn.execute('UPDATE users SET last_seen = ? WHERE id = ?',
                        (datetime.now(), session['user_id']))
            conn.commit()
            conn.close()
            print(f"✅ Обновлён last_seen для {session['user_id']}")
        except Exception as e:
            print(f"Ошибка update_activity: {e}")
    return jsonify({'success': True})



@app.route('/update_status', methods=['POST'])
def update_status():
    """Обновить статус пользователя"""
    if 'user_id' not in session:
        return jsonify({'success': False})

    data = request.json
    status_type = data.get('status_type', 'online')

    print(f"📝 Получен запрос: user_id={session['user_id']}, status={status_type}")

    conn = get_db_connection()

    # Обновляем
    conn.execute('UPDATE users SET status_type = ?, last_seen = CURRENT_TIMESTAMP WHERE id = ?',
                 (status_type, session['user_id']))
    conn.commit()

    # Проверяем
    check = conn.execute('SELECT status_type FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    print(f"✅ После обновления: status_type={check['status_type']}")

    conn.close()
    return jsonify({'success': True})

@app.route('/invite_to_group', methods=['POST'])
def invite_to_group():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Не авторизован'})
    data = request.json
    invited_user_id = data.get('user_id')
    if not invited_user_id:
        return jsonify({'success': False, 'error': 'Не указан пользователь'})
    conn = get_db_connection()
    invited_user = conn.execute('SELECT id, username, privacy_settings FROM users WHERE id = ?', (invited_user_id,)).fetchone()
    if invited_user:
        disable_invites = False
        if invited_user['privacy_settings']:
            try:
                settings = json.loads(invited_user['privacy_settings'])
                disable_invites = settings.get('disable_invites', False)
            except: pass
        if disable_invites:
            conn.close()
            return jsonify({'success': False, 'error': 'Пользователь запретил приглашения'})
    try:
        conn.execute('INSERT OR IGNORE INTO group_members (user_id, is_admin) VALUES (?, 0)', (invited_user_id,))
    except: pass
    try:
        conn.execute('INSERT OR IGNORE INTO group_members (user_id, is_admin) VALUES (?, 0)', (session['user_id'],))
    except: pass
    inviter = conn.execute('SELECT username FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    invited = conn.execute('SELECT username FROM users WHERE id = ?', (invited_user_id,)).fetchone()
    message = f"🔔 {inviter['username']} пригласил(а) {invited['username']} в групповой чат!"
    conn.execute('INSERT INTO messages (sender_id, receiver_id, message) VALUES (0, 0, ?)', (message,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/invite_user/<int:user_id>')
def invite_user(user_id):
    if 'user_id' not in session:
        return "❌ Вы не авторизованы", 401
    if user_id == session['user_id']:
        return "❌ Нельзя пригласить самого себя", 400
    conn = get_db_connection()
    invited_user = conn.execute('SELECT id, username, privacy_settings FROM users WHERE id = ?', (user_id,)).fetchone()
    if not invited_user:
        conn.close()
        return "❌ Пользователь не найден", 404
    disable_invites = False
    if invited_user['privacy_settings']:
        try:
            settings = json.loads(invited_user['privacy_settings'])
            disable_invites = settings.get('disable_invites', False)
        except: pass
    if disable_invites:
        conn.close()
        return "❌ Пользователь запретил приглашения", 403
    try:
        conn.execute('INSERT OR IGNORE INTO group_members (user_id, is_admin) VALUES (?, 0)', (user_id,))
    except: pass
    try:
        conn.execute('INSERT OR IGNORE INTO group_members (user_id, is_admin) VALUES (?, 0)', (session['user_id'],))
    except: pass
    inviter = conn.execute('SELECT username FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    message = f"🔔 {inviter['username']} пригласил(а) {invited_user['username']} в групповой чат!"
    conn.execute('INSERT INTO messages (sender_id, receiver_id, message) VALUES (0, 0, ?)', (message,))
    conn.commit()
    conn.close()
    return f"✅ Пользователь {invited_user['username']} приглашён в групповой чат! <a href='/profile'>Вернуться</a>"


@app.route('/kick_from_group_chat', methods=['POST'])
def kick_from_group_chat():
    """Исключить пользователя из группового чата"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Не авторизован'})

    data = request.json
    group_id = data.get('group_id')
    kicked_user_id = data.get('user_id')

    if not group_id or not kicked_user_id:
        return jsonify({'success': False, 'error': 'Не указаны данные'})

    conn = get_db_connection()

    # Проверяем, имеет ли текущий пользователь права админа
    admin_check = conn.execute('''
        SELECT is_admin FROM group_chat_members 
        WHERE group_id = ? AND user_id = ?
    ''', (group_id, session['user_id'])).fetchone()

    if not admin_check or admin_check['is_admin'] != 1:
        conn.close()
        return jsonify({'success': False, 'error': 'Только администратор может исключать'})

    # Нельзя кикнуть самого себя
    if kicked_user_id == session['user_id']:
        conn.close()
        return jsonify({'success': False, 'error': 'Нельзя исключить самого себя'})

    # Удаляем пользователя
    conn.execute('DELETE FROM group_chat_members WHERE group_id = ? AND user_id = ?',
                 (group_id, kicked_user_id))

    # Системное сообщение
    admin = conn.execute('SELECT username FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    kicked = conn.execute('SELECT username FROM users WHERE id = ?', (kicked_user_id,)).fetchone()
    group = conn.execute('SELECT name FROM group_chats WHERE id = ?', (group_id,)).fetchone()

    conn.execute('''
        INSERT INTO messages (sender_id, receiver_id, message, chat_type, group_id) 
        VALUES (?, ?, ?, 'group', ?)
    ''', (
    session['user_id'], group_id, f"👢 {admin['username']} исключил(а) {kicked['username']} из группы '{group['name']}'",
    group_id))

    conn.commit()
    conn.close()

    return jsonify({'success': True})


@app.route('/make_group_admin', methods=['POST'])
def make_group_admin():
    """Сделать пользователя администратором в группе"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Не авторизован'})

    data = request.json
    group_id = data.get('group_id')
    user_id = data.get('user_id')

    if not group_id or not user_id:
        return jsonify({'success': False, 'error': 'Не указаны данные'})

    conn = get_db_connection()

    # Проверяем права текущего пользователя
    admin_check = conn.execute('''
        SELECT is_admin FROM group_chat_members 
        WHERE group_id = ? AND user_id = ?
    ''', (group_id, session['user_id'])).fetchone()

    if not admin_check or admin_check['is_admin'] != 1:
        conn.close()
        return jsonify({'success': False, 'error': 'Только администратор может выдавать права'})

    # Обновляем права
    conn.execute('''
        UPDATE group_chat_members SET is_admin = 1 
        WHERE group_id = ? AND user_id = ?
    ''', (group_id, user_id))

    conn.commit()
    conn.close()

    return jsonify({'success': True})


@app.route('/remove_group_admin', methods=['POST'])
def remove_group_admin():
    """Забрать права администратора у пользователя в группе"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Не авторизован'})

    data = request.json
    group_id = data.get('group_id')
    user_id = data.get('user_id')

    if not group_id or not user_id:
        return jsonify({'success': False, 'error': 'Не указаны данные'})

    conn = get_db_connection()

    # Проверяем права текущего пользователя
    admin_check = conn.execute('''
        SELECT is_admin FROM group_chat_members 
        WHERE group_id = ? AND user_id = ?
    ''', (group_id, session['user_id'])).fetchone()

    if not admin_check or admin_check['is_admin'] != 1:
        conn.close()
        return jsonify({'success': False, 'error': 'Только администратор может забирать права'})

    # Нельзя забрать админку у самого себя
    if user_id == session['user_id']:
        conn.close()
        return jsonify({'success': False, 'error': 'Нельзя забрать права у самого себя'})

    # Обновляем права
    conn.execute('''
        UPDATE group_chat_members SET is_admin = 0 
        WHERE group_id = ? AND user_id = ?
    ''', (group_id, user_id))

    conn.commit()
    conn.close()

    return jsonify({'success': True})


@app.route('/leave_group_chat', methods=['POST'])
def leave_group_chat():
    """Выйти из группового чата"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Не авторизован'})

    data = request.json
    group_id = data.get('group_id')

    conn = get_db_connection()

    # Проверяем, есть ли группа
    group = conn.execute('SELECT * FROM group_chats WHERE id = ?', (group_id,)).fetchone()
    if not group:
        conn.close()
        return jsonify({'success': False, 'error': 'Группа не найдена'})

    # Проверяем, является ли пользователь создателем группы
    is_creator = (group['created_by'] == session['user_id'])

    # Если пользователь создатель, нужно передать права
    if is_creator:
        # Получаем список других участников
        other_members = conn.execute('''
            SELECT u.id, u.username FROM users u
            INNER JOIN group_chat_members gcm ON u.id = gcm.user_id
            WHERE gcm.group_id = ? AND u.id != ?
        ''', (group_id, session['user_id'])).fetchall()

        if len(other_members) > 0:
            conn.close()
            return jsonify({
                'success': False,
                'need_transfer': True,
                'members': [{'id': m['id'], 'username': m['username']} for m in other_members],
                'message': 'Вы создатель чата. Назначьте нового администратора перед выходом.'
            })

    # Обычный выход
    conn.execute('DELETE FROM group_chat_members WHERE group_id = ? AND user_id = ?',
                 (group_id, session['user_id']))

    # Если участников не осталось, удаляем группу
    remaining = conn.execute('SELECT COUNT(*) as count FROM group_chat_members WHERE group_id = ?',
                             (group_id,)).fetchone()

    if remaining['count'] == 0:
        conn.execute('DELETE FROM group_chats WHERE id = ?', (group_id,))
        conn.execute('DELETE FROM messages WHERE group_id = ?', (group_id,))

    conn.commit()
    conn.close()

    return jsonify({'success': True})


@app.route('/set_username', methods=['POST'])
def set_username():
    """Установить юзернейм пользователя"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Не авторизован'})

    data = request.json
    new_username = data.get('username', '').strip()

    if not new_username:
        return jsonify({'success': False, 'error': 'Юзернейм не может быть пустым'})

    if len(new_username) < 3:
        return jsonify({'success': False, 'error': 'Юзернейм должен содержать минимум 3 символа'})

    if len(new_username) > 20:
        return jsonify({'success': False, 'error': 'Юзернейм не может быть длиннее 20 символов'})

    # Проверяем на уникальность
    conn = get_db_connection()
    existing = conn.execute('SELECT id FROM users WHERE username = ? AND id != ?',
                            (new_username, session['user_id'])).fetchone()
    if existing:
        conn.close()
        return jsonify({'success': False, 'error': 'Этот юзернейм уже занят'})

    # Обновляем
    conn.execute('UPDATE users SET username = ? WHERE id = ?', (new_username, session['user_id']))
    conn.commit()
    session['username'] = new_username
    conn.close()

    return jsonify({'success': True, 'username': new_username})


@app.route('/get_user_info/<int:user_id>')
def get_user_info(user_id):
    """Получить информацию о пользователе по ID"""
    if 'user_id' not in session:
        return jsonify({'error': 'Не авторизован'})

    conn = get_db_connection()
    user = conn.execute('SELECT id, username, is_verified FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()

    if user:
        return jsonify({
            'id': user['id'],
            'username': user['username'] or f"user_{user['id']}",
            'is_verified': user['is_verified'] == 1
        })
    return jsonify({'error': 'Пользователь не найден'})

@app.route('/transfer_admin', methods=['POST'])
def transfer_admin():
    """Передать права администратора другому пользователю"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Не авторизован'})

    data = request.json
    group_id = data.get('group_id')
    new_admin_id = data.get('new_admin_id')

    if not group_id or not new_admin_id:
        return jsonify({'success': False, 'error': 'Не указаны данные'})

    conn = get_db_connection()

    # Проверяем, что текущий пользователь - создатель
    group = conn.execute('SELECT created_by FROM group_chats WHERE id = ?', (group_id,)).fetchone()
    if not group or group['created_by'] != session['user_id']:
        conn.close()
        return jsonify({'success': False, 'error': 'Только создатель может передать права'})

    # Назначаем нового админа
    conn.execute('''
        UPDATE group_chat_members SET is_admin = 1 
        WHERE group_id = ? AND user_id = ?
    ''', (group_id, new_admin_id))

    # Убираем админку у себя (если нужно)
    conn.execute('''
        UPDATE group_chat_members SET is_admin = 0 
        WHERE group_id = ? AND user_id = ?
    ''', (group_id, session['user_id']))

    # Обновляем создателя группы
    conn.execute('UPDATE group_chats SET created_by = ? WHERE id = ?', (new_admin_id, group_id))

    # Системное сообщение
    new_admin = conn.execute('SELECT username FROM users WHERE id = ?', (new_admin_id,)).fetchone()
    old_admin = conn.execute('SELECT username FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    group_name = conn.execute('SELECT name FROM group_chats WHERE id = ?', (group_id,)).fetchone()

    conn.execute('''
        INSERT INTO messages (sender_id, receiver_id, message, chat_type, group_id) 
        VALUES (?, ?, ?, 'group', ?)
    ''', (session['user_id'], group_id,
          f"👑 {old_admin['username']} передал(а) права администратора {new_admin['username']} в группе '{group_name['name']}'",
          group_id))

    conn.commit()
    conn.close()

    return jsonify({'success': True})


@app.route('/verify_user', methods=['POST'])
def verify_user():
    """Верифицировать пользователя (только для админа ID 15)"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Не авторизован'})

    if session['user_id'] == 1 or session['user_id'] == 2:
        return jsonify({'success': False, 'error': 'Только главный администратор может верифицировать'})

    data = request.json
    user_id = data.get('user_id')
    is_verified = data.get('is_verified', True)

    conn = get_db_connection()
    conn.execute('UPDATE users SET is_verified = ? WHERE id = ?', (1 if is_verified else 0, user_id))
    conn.commit()
    conn.close()

    return jsonify({'success': True})


@app.route('/get_verification_status')
def get_verification_status():
    """Получить статус верификации пользователя"""
    if 'user_id' not in session:
        return jsonify({})

    user_id = request.args.get('user_id', type=int)
    if not user_id:
        user_id = session['user_id']

    conn = get_db_connection()
    user = conn.execute('SELECT id, username, is_verified FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()

    if user:
        return jsonify({'user_id': user['id'], 'username': user['username'], 'is_verified': user['is_verified'] == 1})
    return jsonify({'error': 'Пользователь не найден'})

@app.route('/get_user_by_username')
def get_user_by_username():
    """Найти пользователя по username"""
    if 'user_id' not in session:
        return jsonify({'error': 'Не авторизован'})

    username = request.args.get('username', '')
    if not username:
        return jsonify({'error': 'Не указан username'})

    conn = get_db_connection()
    user = conn.execute('SELECT id, username, is_verified FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()

    if user:
        return jsonify({'id': user['id'], 'username': user['username'], 'is_verified': user['is_verified'] == 1})
    return jsonify({'error': 'Пользователь не найден'})

@app.route('/delete_message', methods=['POST'])
def delete_message():
    mid = request.json.get('message_id')
    uid = session.get('user_id')
    conn = get_db_connection()
    # Удалять может только автор
    res = conn.execute('DELETE FROM messages WHERE id = ? AND sender_id = ?', (mid, uid))
    conn.commit()
    conn.close()
    return jsonify(success=res.rowcount > 0)


@app.route('/reply_to_message', methods=['POST'])
def reply_to_message():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Не авторизован'})
    data = request.json
    original_msg_id = data.get('original_msg_id')
    reply_text = data.get('reply_text')
    receiver_id = data.get('receiver_id', 0)
    if not original_msg_id or not reply_text:
        return jsonify({'success': False, 'error': 'Не указаны данные'})
    conn = get_db_connection()
    original = conn.execute('SELECT m.*, u.username FROM messages m LEFT JOIN users u ON m.sender_id = u.id WHERE m.id = ?', (original_msg_id,)).fetchone()
    if not original:
        conn.close()
        return jsonify({'success': False, 'error': 'Оригинальное сообщение не найдено'})
    original_text = original['message']
    if original_text.startswith('[IMAGE]'):
        original_text = '📷 Фото'
    elif len(original_text) > 50:
        original_text = original_text[:47] + '...'
    author = original['username'] if original['username'] else 'Пользователь'
    reply_message = f"📎 Ответ для {author}: \"{original_text}\"\n➡️ {reply_text}"
    conn.execute('INSERT INTO messages (sender_id, receiver_id, message) VALUES (?, ?, ?)',
                 (session['user_id'], receiver_id, reply_message))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/get_all_users_for_invite')
def get_all_users_for_invite():
    """Получить пользователей для приглашения (исключая себя, запретивших и уже в группе)"""
    if 'user_id' not in session:
        return jsonify([])

    group_id = request.args.get('group_id', 0, type=int)

    conn = get_db_connection()

    if group_id == 0:
        # Для старого общего чата - просто исключаем себя
        users = conn.execute('''
            SELECT id, username, privacy_settings FROM users 
            WHERE id != ?
        ''', (session['user_id'],)).fetchall()
    else:
        # Для группового чата - исключаем себя, тех кто уже в группе и запретивших
        users = conn.execute('''
            SELECT u.id, u.username, u.privacy_settings FROM users u
            WHERE u.id != ?
            AND u.id NOT IN (SELECT user_id FROM group_chat_members WHERE group_id = ?)
        ''', (session['user_id'], group_id)).fetchall()

    conn.close()

    result = []
    for u in users:
        # Проверяем запрет приглашений
        disable_invites = False
        if u['privacy_settings']:
            try:
                settings = json.loads(u['privacy_settings'])
                disable_invites = settings.get('disable_invites', False)
            except:
                pass

        if not disable_invites:
            result.append({'id': u['id'], 'username': u['username']})

    return jsonify(result)

@app.route('/mark_as_read', methods=['POST'])
def mark_as_read():
    if 'user_id' not in session:
        return jsonify({'success': False})
    data = request.json
    message_id = data.get('message_id')
    if message_id:
        conn = get_db_connection()
        conn.execute('UPDATE messages SET is_read = 1 WHERE id = ? AND receiver_id = ?', (message_id, session['user_id']))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    return jsonify({'success': False})

@app.route('/mark_messages_read/<int:sender_id>', methods=['POST'])
def mark_messages_read(sender_id):
    if 'user_id' not in session:
        return jsonify({'success': False})
    conn = get_db_connection()
    conn.execute('UPDATE messages SET is_read = 1 WHERE sender_id = ? AND receiver_id = ? AND is_read = 0', (sender_id, session['user_id']))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/ping', methods=['POST'])
def ping():
    """Обновить время последней активности"""
    if 'user_id' in session:
        try:
            conn = get_db_connection()
            conn.execute('UPDATE users SET last_seen = ? WHERE id = ?',
                        (datetime.now(), session['user_id']))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Ошибка ping: {e}")
    return jsonify({'success': True})


@app.route('/get_group_members_list')
def get_group_members_list():
    """Получить список участников конкретной группы"""
    if 'user_id' not in session:
        return jsonify([])

    group_id = request.args.get('group_id', 0, type=int)

    if group_id == 0:
        return jsonify([])

    conn = get_db_connection()
    members = conn.execute('''
        SELECT u.id, u.username, gcm.is_admin 
        FROM users u
        INNER JOIN group_chat_members gcm ON u.id = gcm.user_id
        WHERE gcm.group_id = ?
    ''', (group_id,)).fetchall()
    conn.close()

    return jsonify([{'id': m['id'], 'username': m['username'], 'is_admin': m['is_admin']} for m in members])

@app.before_request
def update_last_seen():
    if 'user_id' in session:
        try:
            conn = get_db_connection()
            conn.execute('UPDATE users SET last_seen = ? WHERE id = ?',
                        (datetime.now(), session['user_id']))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Ошибка update_last_seen: {e}")

if __name__ == '__main__':
    init_db()
    create_group_members_table()
    migrate_db()
    app.run(debug=True, host='0.0.0.0', port=5100)