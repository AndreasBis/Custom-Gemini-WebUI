import os
import time
import uuid
import sqlite3
import markdown
import click
import re
import io
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, g, send_file
from flask.cli import with_appcontext

try:
    FLASK_KEY = os.environ['FLASK_SECRET_KEY']
except KeyError:
    raise RuntimeError('FATAL: FLASK_SECRET_KEY environment variable is not set. Run: export FLASK_SECRET_KEY=$(openssl rand -hex 16)')

GOOGLE_KEY_ERROR = None
try:
    genai.configure(api_key=os.environ['GOOGLE_API_KEY'])
except KeyError:
    GOOGLE_KEY_ERROR = 'Error: GOOGLE_API_KEY environment variable not set. Please set it in your terminal and restart the server.'

app = Flask(__name__)
app.secret_key = FLASK_KEY
DATABASE = 'app.db'
VALID_MODELS = ['gemini-2.5-pro', 'gemini-2.5-flash-lite', 'gemini-2.5-flash']


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    with app.open_resource('schema.sql', mode='r') as f:
        db.cursor().executescript(f.read())


@click.command('init-db')
@with_appcontext
def init_db_command():
    init_db()
    click.echo('Initialized the database.')


app.cli.add_command(init_db_command)


@app.route('/')
def index():
    db = get_db()
    latest_chat = db.execute('SELECT * FROM chats ORDER BY created_at DESC LIMIT 1').fetchone()
    if latest_chat:
        return redirect(url_for('chat', chat_id=latest_chat['id']))
    else:
        return redirect(url_for('new_chat'))


@app.route('/new_chat')
def new_chat():
    new_chat_id = str(uuid.uuid4())
    session['current_chat_id'] = new_chat_id
    return redirect(url_for('chat', chat_id=new_chat_id))


@app.route('/chat/<string:chat_id>')
def chat(chat_id):
    session['current_chat_id'] = chat_id
    db = get_db()
    
    all_chats = db.execute('SELECT * FROM chats ORDER BY created_at DESC').fetchall()
    current_chat_messages = db.execute(
        'SELECT * FROM messages WHERE chat_id = ? ORDER BY created_at ASC', (chat_id,)
    ).fetchall()
    current_chat = db.execute('SELECT * FROM chats WHERE id = ?', (chat_id,)).fetchone()
    
    model_is_selected = current_chat is not None
    
    fallback_chat_id = None
    if not model_is_selected and all_chats:
        fallback_chat_id = all_chats[0]['id']
    elif all_chats:
        fallback_chat_id = all_chats[0]['id']
    
    return render_template('index.html',
                           all_chats=all_chats,
                           current_chat_messages=current_chat_messages,
                           current_chat_id=chat_id,
                           model_is_selected=model_is_selected,
                           current_chat=current_chat, 
                           fallback_chat_id=fallback_chat_id,
                           error=GOOGLE_KEY_ERROR)


@app.route('/select_model', methods=['POST'])
def select_model():
    if GOOGLE_KEY_ERROR:
        return redirect(url_for('index'))
        
    choice = request.form['model_choice']
    chat_id = session.get('current_chat_id')
    
    if not chat_id:
        return redirect(url_for('index'))

    if choice in VALID_MODELS:
        db = get_db()
        db.execute('INSERT INTO chats (id, title, model) VALUES (?, ?, ?)',
                   (chat_id, 'New Chat', choice))
        db.execute('INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)',
                   (chat_id, 'model', f'Model selected: <strong>{choice}</strong>. You can now begin your chat.'))
        db.commit()
    
    return redirect(url_for('chat', chat_id=chat_id))


@app.route('/delete_chat/<string:chat_id>', methods=['POST'])
def delete_chat(chat_id):
    db = get_db()
    db.execute('DELETE FROM messages WHERE chat_id = ?', (chat_id,))
    db.execute('DELETE FROM chats WHERE id = ?', (chat_id,))
    db.commit()
    
    return redirect(url_for('index'))


@app.route('/rename_chat/<string:chat_id>', methods=['POST'])
def rename_chat(chat_id):
    new_title = request.form['new_title'].strip()
    if not new_title:
        new_title = 'Untitled Chat'
        
    db = get_db()
    db.execute('UPDATE chats SET title = ? WHERE id = ?', (new_title, chat_id))
    db.commit()
    
    return redirect(url_for('chat', chat_id=chat_id))


def clean_html(raw_html):
    cleanr = re.compile('<.*?>')
    cleantext = re.sub(cleanr, '', raw_html)
    cleantext = cleantext.replace('<br>', '\n').replace('<p>', '').replace('</p>', '\n')
    return cleantext


@app.route('/download_chat/<string:chat_id>')
def download_chat(chat_id):
    db = get_db()
    chat = db.execute('SELECT * FROM chats WHERE id = ?', (chat_id,)).fetchone()
    messages = db.execute('SELECT * FROM messages WHERE chat_id = ? ORDER BY created_at ASC', (chat_id,)).fetchall()
    
    if not chat:
        return redirect(url_for('index'))
        
    txt_output = io.StringIO()
    txt_output.write(f'Chat History: {chat['title']}\n')
    txt_output.write(f'Model: {chat['model']}\n')
    txt_output.write('='*30 + '\n\n')
    
    for msg in messages:
        role = 'You' if msg['role'] == 'user' else 'Model'
        content = clean_html(msg['content'])
        txt_output.write(f'--- {role} ---\n{content}\n\n')
        
    txt_output.seek(0)
    
    mem_file = io.BytesIO()
    mem_file.write(txt_output.getvalue().encode('utf-8'))
    mem_file.seek(0)
    
    return send_file(
        mem_file,
        as_attachment=True,
        download_name=f'{chat['title'].replace(" ", "_")}.txt',
        mimetype='text/plain'
    )


@app.route('/api/chat', methods=['POST'])
def api_chat():
    if GOOGLE_KEY_ERROR or 'current_chat_id' not in session:
        return jsonify({'error': 'Session error or API key missing'}), 400

    chat_id = session['current_chat_id']
    prompt = request.json['prompt']
    
    db = get_db()
    chat = db.execute('SELECT * FROM chats WHERE id = ?', (chat_id,)).fetchone()
    
    if not chat:
        return jsonify({'error': 'Chat not found'}), 404
        
    model_name = chat['model']
    
    db.execute('INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)',
               (chat_id, 'user', prompt))
    db.commit()
    
    if chat['title'] == 'New Chat':
        try:
            title_prompt = f'Generate a short, 3-5 word title for a chat that starts with this prompt: "{prompt}"'
            title_model = genai.GenerativeModel(model_name='gemini-2.5-flash-lite')
            title_response = title_model.generate_content(title_prompt)
            
            new_title = title_response.text.strip().replace('"', '').strip('*_#~')
            
            db.execute('UPDATE chats SET title = ? WHERE id = ?', (new_title, chat_id))
            db.commit()
        except Exception:
            pass 
            
    response_text = ''
    try:
        model_instance = genai.GenerativeModel(model_name=model_name)
        response = model_instance.generate_content(prompt)
        
        if response.candidates:
            if response.candidates[0].finish_reason == 'SAFETY':
                response_text = '--- ERROR: The response was blocked by safety filters. ---'
            else:
                current_count_query = db.execute('SELECT COUNT(id) FROM messages WHERE chat_id = ? AND role = ?', (chat_id, 'user'))
                msg_count = current_count_query.fetchone()[0]
                
                html_output = markdown.markdown(response.text, extensions=['nl2br']) 
                counter_html = f'<span class="msg-counter">{msg_count}</span>'
                response_text = f'{html_output}\n\n<div class="msg-meta">(Model: {model_name} | {counter_html})</div>'
        else:
            feedback = response.prompt_feedback
            block_reason = feedback.block_reason.name if feedback.block_reason else 'Unknown'
            response_text = f'--- ERROR: Your prompt was blocked by safety filters (Reason: {block_reason}). Please rephrase. ---'

    except ResourceExhausted:
        response_text = '--- ERROR: RATE LIMIT EXCEEDED ---\nWaiting for 60 seconds. Please resubmit your prompt after the wait.'
    except Exception as e:
        response_text = f'An error occurred: {e}'
        
    db.execute('INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)',
               (chat_id, 'model', response_text))
    db.commit()
    
    return jsonify({'role': 'model', 'content': response_text})


if __name__ == '__main__':
    app.run(debug=True)