from database import get_db


def get_all_chats():
    db = get_db()
    return db.execute('SELECT * FROM chats ORDER BY created_at DESC').fetchall()


def get_chat_messages(chat_id):
    db = get_db()
    return db.execute(
        'SELECT * FROM messages WHERE chat_id = ? ORDER BY created_at ASC', (chat_id,)
    ).fetchall()


def get_chat(chat_id):
    db = get_db()
    return db.execute('SELECT * FROM chats WHERE id = ?', (chat_id,)).fetchone()


def create_new_chat(chat_id, title, model):
    db = get_db()
    db.execute('INSERT INTO chats (id, title, model) VALUES (?, ?, ?)',
               (chat_id, title, model))
    db.commit()


def add_message_to_db(chat_id, role, html_content, raw_content, message_type='chat'):
    db = get_db()
    db.execute(
        'INSERT INTO messages (chat_id, role, content, raw_content, message_type) VALUES (?, ?, ?, ?, ?)',
        (chat_id, role, html_content, raw_content, message_type)
    )
    db.commit()


def delete_chat_from_db(chat_id):
    db = get_db()
    db.execute('DELETE FROM messages WHERE chat_id = ?', (chat_id,))
    db.execute('DELETE FROM chats WHERE id = ?', (chat_id,))
    db.commit()


def rename_chat_in_db(chat_id, new_title):
    db = get_db()
    db.execute('UPDATE chats SET title = ? WHERE id = ?', (new_title, chat_id))
    db.commit()


def update_chat_title(chat_id, new_title):
    db = get_db()
    db.execute('UPDATE chats SET title = ? WHERE id = ?', (new_title, chat_id))
    db.commit()


def get_message_count(chat_id, role='user'):
    db = get_db()
    count = db.execute(
        'SELECT COUNT(id) FROM messages WHERE chat_id = ? AND role = ?',
        (chat_id, role)
    ).fetchone()[0]
    return count