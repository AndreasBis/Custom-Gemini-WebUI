import os
import time
import uuid
import sqlite3
import markdown
import click
import re
import io
import json
import subprocess
import pdfplumber
import docx
import nbformat
import shlex
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, g, send_file
from flask.cli import with_appcontext

try:
    FLASK_KEY = os.environ['FLASK_SECRET_KEY']
except KeyError:
    raise RuntimeError('FATAL: FLASK_SECRET_KEY environment variable not set. Run: export FLASK_SECRET_KEY=$(openssl rand -hex 16)')

GOOGLE_KEY_ERROR = None
try:
    genai.configure(api_key=os.environ['GOOGLE_API_KEY'])
except KeyError:
    GOOGLE_KEY_ERROR = 'Error: GOOGLE_API_KEY environment variable not set. Please set it in your terminal and restart the server.'

app = Flask(__name__)
app.secret_key = FLASK_KEY
DATABASE = 'app.db'
VALID_MODELS = [
    'gemini-2.5-pro',
    'gemini-2.5-flash',
    'gemini-2.5-flash-lite',
]
CODE_DIR = 'code'
HOME_DIR = os.path.expanduser('~')
WHITELISTED_EXTENSIONS = ['.pdf', '.txt', '.docx', '.py', '.c', '.ipynb']
CONTEXT_WINDOW_THRESHOLD = 65536
CACHE_EXPIRATION_SECONDS = 3600 # 1 hour

if not os.path.exists(CODE_DIR):
    os.makedirs(CODE_DIR)


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


def estimate_tokens(text):
    return len(text) / 4


def add_message_to_db(chat_id, role, content, message_type='chat'):
    db = get_db()
    db.execute(
        'INSERT INTO messages (chat_id, role, content, message_type) VALUES (?, ?, ?, ?)',
        (chat_id, role, content, message_type)
    )
    db.commit()


def resolve_path(path):
    path = os.path.normpath(path)
    if path.startswith('~'):
        path = path.replace('~', HOME_DIR, 1)
    
    full_path = os.path.normpath(os.path.join(HOME_DIR, path))
    
    if not os.path.realpath(full_path).startswith(os.path.realpath(HOME_DIR)):
        return None
        
    return full_path


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
        db.execute('INSERT INTO messages (chat_id, role, content, message_type) VALUES (?, ?, ?, ?)',
                   (chat_id, 'model', f'Model selected: <strong>{choice}</strong>. You can now begin your chat.', 'chat'))
        db.commit()
    
    return redirect(url_for('chat', chat_id=chat_id))


@app.route('/delete_chat/<string:chat_id>', methods=['POST'])
def delete_chat(chat_id):
    db = get_db()
    db.execute('DELETE FROM messages WHERE chat_id = ?', (chat_id,))
    db.execute('DELETE FROM file_cache WHERE chat_id = ?', (chat_id,))
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
    
    cleantext = re.sub(r'<h3>Agent Plan</h3>', 'Agent Plan\n', cleantext)
    cleantext = re.sub(r'<h4>.*?</h4>', '', cleantext)
    cleantext = re.sub(r'<ol>(.*?)</ol>', lambda m: '\n'.join([f'  - {li.strip()}' for li in re.findall(r'<li>(.*?)</li>', m.group(1))]), cleantext, flags=re.DOTALL)
    cleantext = re.sub(r'<strong>Tool Call:</strong> <code>(.*?)</code>', r'Tool Call: \1', cleantext)
    cleantext = re.sub(r'<pre>(.*?)</pre>', r'Output:\n---\n\1\n---', cleantext, flags=re.DOTALL)
    cleantext = re.sub(r'&gt;', '>', cleantext)
    cleantext = re.sub(r'&lt;', '<', cleantext)
    cleantext = re.sub(r'&amp;', '&', cleantext)
    
    return cleantext.strip()


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
        content = clean_html(msg['content'])
        
        if msg['message_type'] == 'chat':
            role = 'You' if msg['role'] == 'user' else 'Model'
            txt_output.write(f'--- {role} ---\n{content}\n\n')
        elif msg['message_type'] == 'agent_plan':
            txt_output.write(f'--- Model (Agent Plan) ---\n{content}\n\n')
        elif msg['message_type'] == 'tool_call':
            txt_output.write(f'--- Agent (Tool Call) ---\n{content}\n\n')
        elif msg['message_type'] == 'tool_output':
            txt_output.write(f'--- System (Tool Output) ---\n{content}\n\n')
        elif msg['message_type'] == 'user_confirmation':
            txt_output.write(f'--- User (Confirmation) ---\n{content}\n\n')
        
    txt_output.seek(0)
    
    mem_file = io.BytesIO()
    mem_file.write(txt_output.getvalue().encode('utf-8'))
    mem_file.seek(0)
    
    return send_file(
        mem_file,
        as_attachment=True,
        download_name=f'{chat['title'].replace(' ', '_')}.txt',
        mimetype='text/plain'
    )


def get_available_tools():
    return [
        {'name': 'list_directory', 'description': 'List all files and directories in a given path relative to home (~).', 'parameters': {'path': 'string'}},
        {'name': 'read_file_content', 'description': 'Read the text content of a single file from a path relative to home (~). Caches result per chat.', 'parameters': {'path': 'string'}},
        {'name': 'read_directory_recursively', 'description': 'Read content of all whitelisted files in a directory (relative to home ~) and its subdirectories. Uses file cache.', 'parameters': {'path': 'string'}},
        {'name': 'save_text_file', 'description': 'Save a text string to a file (e.g., test.txt, script.py) inside the code/ directory.', 'parameters': {'filename': 'string', 'content': 'string'}},
        {'name': 'delete_file_in_code', 'description': 'Delete a single file from the code/ directory.', 'parameters': {'filename': 'string'}},
        {'name': 'run_terminal_command', 'description': 'Execute a sandboxed terminal command (non-interactive) inside the code/ directory.', 'parameters': {'command': 'string'}},
        {'name': 'execute_python_script', 'description': 'Execute a Python script string in a sandbox (inside the code/ directory).', 'parameters': {'code_string': 'string'}},
    ]


def list_directory(path):
    try:
        full_path = resolve_path(path)
        if not full_path:
            return json.dumps({'error': 'Path traversal detected or path is invalid.'})
            
        if not os.path.exists(full_path) or not os.path.isdir(full_path):
            return json.dumps({'error': 'Path does not exist or is not a directory.'})
            
        entries = os.listdir(full_path)
        return json.dumps({'entries': entries})
    except Exception as e:
        return json.dumps({'error': f'Error listing directory: {str(e)}'})


def read_file_content(path):
    chat_id = session.get('current_chat_id')
    if not chat_id:
        return json.dumps({'error': 'Chat session not found, cannot use cache.'})
        
    db = get_db()
    
    # Check cache first
    cache_query = """
    SELECT content FROM file_cache 
    WHERE chat_id = ? AND path = ? AND
    created_at > datetime('now', ?)
    """
    cached = db.execute(cache_query, (chat_id, path, f'-{CACHE_EXPIRATION_SECONDS} seconds')).fetchone()
    
    if cached:
        content = cached['content']
        token_count = estimate_tokens(content)
        return json.dumps({'path': path, 'content': content, 'tokens': token_count, 'source': 'cache'})

    # If not in cache or expired, read from disk
    try:
        full_path = resolve_path(path)
        if not full_path:
            return json.dumps({'error': 'Path traversal detected or path is invalid.'})

        if not os.path.exists(full_path) or not os.path.isfile(full_path):
            return json.dumps({'error': 'File not found or is not a file.'})
        
        _, ext = os.path.splitext(path)
        if ext not in WHITELISTED_EXTENSIONS:
            return json.dumps({'error': f'File type {ext} is not whitelisted.'})
            
        content = ''
        if ext == '.pdf':
            with pdfplumber.open(full_path) as pdf:
                content = '\n'.join([page.extract_text() for page in pdf.pages if page.extract_text()])
        elif ext == '.docx':
            doc = docx.Document(full_path)
            content = '\n'.join([para.text for para in doc.paragraphs if para.text])
        elif ext == '.ipynb':
            with open(full_path, 'r', encoding='utf-8') as f:
                nb = nbformat.read(f, as_version=4)
                cells = [cell['source'] for cell in nb.cells if cell.cell_type == 'code' or cell.cell_type == 'markdown']
                content = '\n\n'.join(cells)
        else:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()

        token_count = estimate_tokens(content)
        if token_count > CONTEXT_WINDOW_THRESHOLD:
            return json.dumps({
                'error': f'File {path} is too large to be processed ({token_count} tokens). This file cannot be used.'
            })
        
        # Save to cache
        db.execute(
            'INSERT OR REPLACE INTO file_cache (chat_id, path, content, created_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)',
            (chat_id, path, content)
        )
        db.commit()
            
        return json.dumps({'path': path, 'content': content, 'tokens': token_count, 'source': 'disk'})
    except Exception as e:
        return json.dumps({'error': f'Error reading file: {str(e)}'})


def read_directory_recursively(path, selected_files=None):
    try:
        full_path = resolve_path(path)
        if not full_path:
            return json.dumps({'error': 'Path traversal detected or path is invalid.'})

        if not os.path.exists(full_path) or not os.path.isdir(full_path):
            return json.dumps({'error': 'Path does not exist or is not a directory.'})

        files_data = {}
        files_with_tokens = []
        total_token_count = 0
        
        for root, dirs, files in os.walk(full_path, topdown=True):
            # Filter out hidden directories
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            # Filter out hidden files
            files = [f for f in files if not f.startswith('.')]
            
            for file in files:
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, HOME_DIR)
                _, ext = os.path.splitext(file)
                
                if ext in WHITELISTED_EXTENSIONS:
                    if selected_files and rel_path not in selected_files:
                        continue
                        
                    file_content_json = read_file_content(rel_path)
                    file_content_data = json.loads(file_content_json)
                    
                    if 'content' in file_content_data:
                        token_count = file_content_data['tokens']
                        files_with_tokens.append({'path': rel_path, 'tokens': token_count})
                        files_data[rel_path] = file_content_data['content']
                        total_token_count += token_count
                    elif not selected_files:
                        files_with_tokens.append({
                            'path': rel_path,
                            'error': file_content_data.get('error', 'Unknown error'),
                            'tokens': 0
                        })

        if not selected_files and total_token_count > CONTEXT_WINDOW_THRESHOLD:
            return json.dumps({
                'status': 'file_selection_pending',
                'files': files_with_tokens,
                'total_tokens': total_token_count
            })

        return json.dumps({
            'status': 'success',
            'files': files_data,
            'total_tokens': total_token_count,
            'file_count': len(files_data)
        })
        
    except Exception as e:
        return json.dumps({'error': f'Error reading directory: {str(e)}'})


def command_validator(command):
    if '--no-preserve-root' in command:
        return {'status': 'error', 'message': 'Command blocked: --no-preserve-root is forbidden.'}
        
    if re.search(r'\b(rm|rm -r|rm -rf)\b', command):
        return {'status': 'confirmation_pending', 'command': command}
        
    return {'status': 'approved'}


def run_terminal_command(command):
    validation = command_validator(command)
    
    if validation['status'] == 'error':
        return json.dumps({'error': validation['message']})
        
    if validation['status'] == 'confirmation_pending':
        return json.dumps(validation)

    try:
        # Use shlex.split to handle spaces and quotes safely
        command_args = shlex.split(command)
        
        result = subprocess.run(
            command_args,
            shell=False, # Set shell=False for security
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=os.path.abspath(CODE_DIR),
            timeout=30
        )
        return json.dumps({'stdout': result.stdout, 'stderr': result.stderr})
    except subprocess.CalledProcessError as e:
        return json.dumps({'error': 'Command failed', 'stdout': e.stdout, 'stderr': e.stderr})
    except subprocess.TimeoutExpired:
        return json.dumps({'error': 'Command timed out after 30 seconds.'})
    except Exception as e:
        return json.dumps({'error': f'Error executing command: {str(e)}'})


def execute_python_script(code_string):
    script_name = f'temp_script_{uuid.uuid4()}.py'
    script_path = os.path.join(CODE_DIR, script_name)
    try:
        with open(script_path, 'w', encoding='utf-8') as f:
            f.write(code_string)
            
        result = subprocess.run(
            ['python', script_name],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=os.path.abspath(CODE_DIR),
            timeout=30
        )
        return json.dumps({'stdout': result.stdout, 'stderr': result.stderr})
    except subprocess.CalledProcessError as e:
        return json.dumps({'error': 'Script execution failed', 'stdout': e.stdout, 'stderr': e.stderr})
    except subprocess.TimeoutExpired:
        return json.dumps({'error': 'Script timed out after 30 seconds.'})
    except Exception as e:
        return json.dumps({'error': f'Error executing Python script: {str(e)}'})
    finally:
        if os.path.exists(script_path):
            os.remove(script_path)


def save_text_file(filename, content):
    file_path = os.path.join(CODE_DIR, os.path.basename(filename))
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return json.dumps({'status': 'success', 'path': file_path})
    except Exception as e:
        return json.dumps({'error': f'Error saving file: {str(e)}'})


def delete_file_in_code(filename):
    file_path = os.path.join(CODE_DIR, os.path.basename(filename))
    
    if not os.path.abspath(file_path).startswith(os.path.abspath(CODE_DIR)):
        return json.dumps({'error': 'Path traversal detected. Deletion only allowed in code/.'})
        
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            return json.dumps({'status': 'success', 'path': file_path})
        else:
            return json.dumps({'error': f'File not found: {filename}'})
    except Exception as e:
        return json.dumps({'error': f'Error deleting file: {str(e)}'})


TOOLS_MAP = {
    'list_directory': list_directory,
    'read_file_content': read_file_content,
    'read_directory_recursively': read_directory_recursively,
    'save_text_file': save_text_file,
    'delete_file_in_code': delete_file_in_code,
    'run_terminal_command': run_terminal_command,
    'execute_python_script': execute_python_script,
}


def format_history_for_model(db_messages):
    history = []
    for msg in db_messages:
        if msg['role'] == 'user':
            history.append({
                'role': 'user',
                'parts': [clean_html(msg['content'])]
            })
        elif msg['role'] == 'model' and msg['message_type'] == 'chat':
            # Only add successful model chat responses to history
            if 'ERROR' not in msg['content'] and 'Model selected' not in msg['content']:
                history.append({
                    'role': 'model',
                    'parts': [clean_html(msg['content'])]
                })
    
    # Prune empty messages
    history = [h for h in history if h['parts'][0].strip()]
    
    # Ensure history starts with a user message if possible
    if history and history[0]['role'] == 'model':
        history = history[1:]
        
    return history


@app.route('/api/chat', methods=['POST'])
def api_chat():
    if GOOGLE_KEY_ERROR or 'current_chat_id' not in session:
        return jsonify({'error': 'Session error or API key missing'}), 400

    chat_id = session['current_chat_id']
    prompt = request.json['prompt']
    agent_mode = request.json.get('agent_mode', False)
    
    db = get_db()
    chat = db.execute('SELECT * FROM chats WHERE id = ?', (chat_id,)).fetchone()
    
    if not chat:
        return jsonify({'error': 'Chat not found'}), 404
        
    model_name = chat['model']
    add_message_to_db(chat_id, 'user', prompt, 'chat')
    
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
    message_type = 'chat'
    raw_plan = None
    
    try:
        model_instance = genai.GenerativeModel(model_name=model_name)
        
        # Fetch and format history for conversational context
        db_messages = db.execute(
            'SELECT * FROM messages WHERE chat_id = ? ORDER BY created_at ASC', (chat_id,)
        ).fetchall()
        formatted_history = format_history_for_model(db_messages)
        
        chat_session = model_instance.start_chat(history=formatted_history)
        
        decision = 'CHAT'
        if agent_mode:
            classifier_prompt = f"""
Analyze the user's prompt: "{prompt}"

Respond with *only* "CHAT" or "TASK".
"CHAT" means this is a general conversation, question, or request for information that doesn't require accessing the local file system or running code.
"TASK" means this requires using tools like listing files, reading files, saving files, or executing code.
"""
            classifier_response = model_instance.generate_content(classifier_prompt)
            decision = classifier_response.text.strip().upper()

        if 'TASK' in decision and agent_mode:
            tools_list_str = json.dumps(get_available_tools(), indent=2)
            plan_prompt = f"""
User's Goal: "{prompt}"

Available Tools:
{tools_list_str}

You are an agent executor brain. Your task is to generate a multi-step plan to achieve the user's goal using *only* the provided tools.
Respond with *only* a JSON object in the following format:
{{
  "plan": [
    {{"step": 1, "tool": "tool_name", "parameters": {{"param1": "value1"}}, "reasoning": "Why this step is needed."}},
    {{"step": 2, "tool": "tool_name", "parameters": {{"param1": "value1"}}, "reasoning": "Why this step follows."}}
  ]
}}
"""
            response = model_instance.generate_content(plan_prompt)
            
            if response.candidates and response.candidates[0].finish_reason != 'SAFETY':
                raw_plan_text = response.text.strip().lstrip('```json').rstrip('```').strip()
                try:
                    plan_json = json.loads(raw_plan_text)
                    if 'plan' in plan_json and isinstance(plan_json['plan'], list):
                        session[f'agent_plan_{chat_id}'] = plan_json['plan']
                        session[f'agent_step_{chat_id}'] = 0
                        session[f'agent_goal_{chat_id}'] = prompt
                        
                        plan_html = '<h3>Agent Plan</h3><ol>'
                        for step in plan_json['plan']:
                            plan_html += f'<li><strong>{step["tool"]}</strong>: {step.get("reasoning", "No reasoning provided.")}</li>'
                        plan_html += '</ol>'
                        plan_html += f'<div class="plan-actions"><button class="approve-btn" onclick="executePlan(\'{chat_id}\')">Approve</button><button class="edit-btn" onclick="editPlan(this, \'{chat_id}\')">Edit</button><button class="cancel-btn" onclick="cancelPlan()">Cancel</button></div>'
                        
                        response_text = plan_html
                        message_type = 'agent_plan'
                        raw_plan = raw_plan_text
                    else:
                        raise ValueError('Invalid plan structure')
                except Exception as e:
                    response_text = f'--- ERROR: Model generated an invalid plan. Trying as chat instead. ---\n{e}\n{raw_plan_text}'
                    decision = 'CHAT' 
            else:
                response_text = '--- ERROR: The agent plan was blocked by safety filters. ---'
                
        if 'CHAT' in decision or not agent_mode:
            # Use the chat session which has history
            response = chat_session.send_message(prompt)
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
        
    add_message_to_db(chat_id, 'model', response_text, message_type)
    
    return jsonify({'role': 'model', 'content': response_text, 'message_type': message_type, 'chat_id': chat_id, 'raw_plan': raw_plan})


@app.route('/api/save_plan/<string:chat_id>', methods=['POST'])
def save_plan(chat_id):
    if GOOGLE_KEY_ERROR or chat_id != session.get('current_chat_id'):
        return jsonify({'error': 'Session error or API key missing'}), 400
        
    try:
        plan_json_str = request.json['plan_json']
        plan_data = json.loads(plan_json_str)
        
        if 'plan' not in plan_data or not isinstance(plan_data['plan'], list):
            raise ValueError('Invalid plan structure')
            
        session[f'agent_plan_{chat_id}'] = plan_data['plan']
        session[f'agent_step_{chat_id}'] = 0
        
        add_message_to_db(chat_id, 'user', f'User edited and approved plan:\n<pre>{json.dumps(plan_data, indent=2)}</pre>', 'user_confirmation')
        return jsonify({'status': 'saved'})
        
    except Exception as e:
        return jsonify({'error': f'Failed to save plan: {str(e)}'}), 500


@app.route('/api/execute_plan/<string:chat_id>', methods=['POST'])
def execute_plan(chat_id):
    if GOOGLE_KEY_ERROR or chat_id != session.get('current_chat_id'):
        return jsonify({'error': 'Session error or API key missing'}), 400

    plan = session.get(f'agent_plan_{chat_id}')
    current_step_index = session.get(f'agent_step_{chat_id}', 0)
    
    if plan is None or current_step_index >= len(plan):
        session.pop(f'agent_plan_{chat_id}', None)
        session.pop(f'agent_step_{chat_id}', None)
        session.pop(f'agent_goal_{chat_id}', None)
        final_message = 'Agent has completed the plan.'
        add_message_to_db(chat_id, 'model', final_message, 'chat')
        return jsonify({'status': 'complete', 'message': final_message})

    step_data = plan[current_step_index]
    tool_name = step_data['tool']
    parameters = step_data['parameters']
    
    if tool_name not in TOOLS_MAP:
        error_msg = f'Error: Tool "{tool_name}" not found.'
        add_message_to_db(chat_id, 'model', error_msg, 'chat')
        return jsonify({'status': 'error', 'message': error_msg})
        
    tool_call_html = f'<strong>Tool Call:</strong> <code>{tool_name}({json.dumps(parameters)})</code>'
    add_message_to_db(chat_id, 'model', tool_call_html, 'tool_call')
    
    try:
        tool_function = TOOLS_MAP[tool_name]
        
        if tool_name == 'read_directory_recursively' and 'selected_files' in request.json:
            parameters['selected_files'] = request.json['selected_files']
            
        tool_output_json = tool_function(**parameters)
        tool_output_data = json.loads(tool_output_json)

        if tool_output_data.get('status') == 'confirmation_pending':
            confirm_html = f'<strong>Deletion Confirmation Required</strong><p>The agent wants to run: <code>{tool_output_data["command"]}</code></p>'
            confirm_html += f'<div class="plan-actions"><button class="approve-btn" onclick="handleAgentAction(\'{chat_id}\', \'confirm_deletion\', {{ \'command\': \'{tool_output_data["command"]}\' }})">Approve Deletion</button><button class="cancel-btn" onclick="cancelPlan()">Deny</button></div>'
            add_message_to_db(chat_id, 'model', confirm_html, 'user_confirmation')
            return jsonify({'status': 'confirmation_pending', 'content': confirm_html})

        if tool_output_data.get('status') == 'file_selection_pending':
            file_list = tool_output_data['files']
            select_html = f'<strong>File Selection Required</strong><p>The agent found {len(file_list)} files, totaling {tool_output_data["total_tokens"]} tokens, which exceeds the limit.</p>'
            select_html += f'<form id="file-select-form" onsubmit="handleFileSelection(event, \'{chat_id}\')">'
            select_html += '<div class="file-selection-list">'
            for f in file_list:
                select_html += f'<label><input type="checkbox" name="selected_files" value="{f["path"]}"> {f["path"]} ({f["tokens"]} tokens)</label><br>'
            select_html += '</div><button type="submit">Process Selected Files</button></form>'
            add_message_to_db(chat_id, 'model', select_html, 'user_confirmation')
            return jsonify({'status': 'file_selection_pending', 'content': select_html})

        tool_output_html = f'<pre>{json.dumps(tool_output_data, indent=2)}</pre>'
        add_message_to_db(chat_id, 'model', tool_output_html, 'tool_output')
        
        session[f'agent_step_{chat_id}'] = current_step_index + 1
        
        db = get_db()
        chat_history = db.execute(
            'SELECT * FROM messages WHERE chat_id = ? ORDER BY created_at ASC', (chat_id,)
        ).fetchall()
        
        history_for_model = []
        for msg in chat_history:
            history_for_model.append({'role': msg['role'], 'content': msg['content']})
            
        goal = session.get(f'agent_goal_{chat_id}')
        
        if session.get(f'agent_step_{chat_id}') >= len(plan):
            reasoning_prompt = f"""
Goal: {goal}
Plan: {json.dumps(plan)}
History: {json.dumps(history_for_model)}

The plan is complete. Based on the full history and tool outputs, provide a final, comprehensive answer to the user's original goal.
"""
        else:
            return jsonify({'status': 'proceed', 'message': 'Proceeding to next step...'})

        chat = db.execute('SELECT * FROM chats WHERE id = ?', (chat_id,)).fetchone()
        model_instance = genai.GenerativeModel(model_name=chat['model'])
        response = model_instance.generate_content(reasoning_prompt)
        
        final_answer = 'Agent has completed the plan.'
        if response.candidates and response.candidates[0].finish_reason != 'SAFETY':
            final_answer = markdown.markdown(response.text, extensions=['nl2br'])
        
        add_message_to_db(chat_id, 'model', final_answer, 'chat')
        session.pop(f'agent_plan_{chat_id}', None)
        session.pop(f'agent_step_{chat_id}', None)
        session.pop(f'agent_goal_{chat_id}', None)
        
        return jsonify({'status': 'complete', 'message': final_answer})
        
    except Exception as e:
        error_msg = f'An error occurred during agent execution: {str(e)}'
        add_message_to_db(chat_id, 'model', error_msg, 'chat')
        return jsonify({'status': 'error', 'message': error_msg})


@app.route('/api/agent_action', methods=['POST'])
def agent_action():
    if GOOGLE_KEY_ERROR or 'current_chat_id' not in session:
        return jsonify({'error': 'Session error or API key missing'}), 400
        
    chat_id = session['current_chat_id']
    data = request.json
    action_type = data.get('action_type')
    action_data = data.get('action_data', {})
    
    plan = session.get(f'agent_plan_{chat_id}')
    current_step_index = session.get(f'agent_step_{chat_id}', 0)
    
    if plan is None:
        return jsonify({'error': 'No active plan found'}), 400
        
    step_data = plan[current_step_index]
    tool_name = step_data['tool']
    
    if action_type == 'confirm_deletion' and tool_name == 'run_terminal_command':
        command = action_data.get('command')
        add_message_to_db(chat_id, 'user', f'User approved command: {command}', 'user_confirmation')
        
        try:
            # Use shlex.split here as well for the confirmed command
            command_args = shlex.split(command)
            result = subprocess.run(
                command_args,
                shell=False,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=os.path.abspath(CODE_DIR),
                timeout=30
            )
            tool_output_json = json.dumps({'stdout': result.stdout, 'stderr': result.stderr})
        except Exception as e:
            tool_output_json = json.dumps({'error': f'Error executing confirmed command: {str(e)}'})
            
        tool_output_html = f'<pre>{tool_output_json}</pre>'
        add_message_to_db(chat_id, 'model', tool_output_html, 'tool_output')
        
        session[f'agent_step_{chat_id}'] = current_step_index + 1
        return jsonify({'status': 'proceed', 'message': 'Deletion confirmed, proceeding...'})

    elif action_type == 'process_selected_files' and tool_name == 'read_directory_recursively':
        selected_files = action_data.get('selected_files', [])
        add_message_to_db(chat_id, 'user', f'User selected {len(selected_files)} files to process.', 'user_confirmation')
        
        tool_output_json = read_directory_recursively(step_data['parameters']['path'], selected_files=selected_files)
        tool_output_html = f'<pre>{json.dumps(json.loads(tool_output_json), indent=2)}</pre>'
        add_message_to_db(chat_id, 'model', tool_output_html, 'tool_output')
        
        session[f'agent_step_{chat_id}'] = current_step_index + 1
        return jsonify({'status': 'proceed', 'message': 'Files processed, proceeding...'})

    return jsonify({'error': 'Invalid action or state'}), 400


if __name__ == '__main__':
    app.run(debug=True)