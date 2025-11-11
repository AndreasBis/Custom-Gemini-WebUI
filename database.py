import sqlite3
import click
from flask import current_app, g
from flask.cli import with_appcontext


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(
            current_app.config['DATABASE'],
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    with current_app.open_resource('schema.sql', mode='r') as f:
        db.cursor().executescript(f.read())


def add_message_to_db(chat_id, role, content, message_type='chat'):
    db = get_db()
    db.execute(
        'INSERT INTO messages (chat_id, role, content, message_type) '
        'VALUES (?, ?, ?, ?)',
        (chat_id, role, content, message_type)
    )
    db.commit()