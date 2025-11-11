import os
import uuid
import json
import re
import io
import markdown
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted
from flask import (
    Blueprint, render_template, request, session, redirect, url_for,
    jsonify, g, current_app, send_file
)
from database import get_db, add_message_to_db
from agent_tools import get_available_tools, TOOLS_MAP, resolve_code_path

main_bp = Blueprint('main', __name__)


def clean_html_for_model(raw_html):
    cleanr = re.compile('<.*?>')
    cleantext = re.sub(cleanr, '', raw_html)
    cleantext = cleantext.replace('<br>', '\n').replace('<p>', '').replace('</p>', '\n')
    cleantext = re.sub(r'<h3>Agent Plan</h3>', 'Agent Plan\n', cleantext)
    cleantext = re.sub(r'<h4>.*?</h4>', '', cleantext)
    cleantext = re.sub(r'<ol>(.*?)</ol>', lambda m: '\n'.join([f'  - {li.strip()}' for li in re.findall(r'<li>(.*?)</li>', m.group(1))]), cleantext, flags=re.DOTALL)
    # This lambda function fixes the newline bug that was crashing the server
    cleantext = re.sub(r'<pre>(.*?)</pre>', lambda m: m.group(1).replace('\n', ' '), cleantext, flags=re.DOTALL)
    cleantext = re.sub(r'&gt;', '>', cleantext)
    cleantext = re.sub(r'&lt;', '<', cleantext)
    cleantext = re.sub(r'&amp;', '&', cleantext)
    return cleantext.strip()


@main_bp.route('/')
def index():
    return render_template('welcome.html')


@main_bp.route('/new_chat')
def new_chat():
    new_chat_id = str(uuid.uuid4())
    session['current_chat_id'] = new_chat_id
    return redirect(url_for('main.chat', chat_id=new_chat_id))


@main_bp.route('/chat/<string:chat_id>')
def chat(chat_id):
    session['current_chat_id'] = chat_id
    db = get_db()

    all_chats = db.execute('SELECT * FROM chats ORDER BY created_at DESC').fetchall()
    current_chat_messages = db.execute(
        'SELECT * FROM messages WHERE chat_id = ? ORDER BY created_at ASC', (chat_id,)
    ).fetchall()
    current_chat = db.execute('SELECT * FROM chats WHERE id = ?', (chat_id,)).fetchone()

    model_is_selected = current_chat is not None
    google_key_error = current_app.config['GOOGLE_KEY_ERROR']

    fallback_chat_id = all_chats[0]['id'] if all_chats else None

    return render_template('chat.html',
                           all_chats=all_chats,
                           current_chat_messages=current_chat_messages,
                           current_chat_id=chat_id,
                           model_is_selected=model_is_selected,
                           current_chat=current_chat,
                           fallback_chat_id=fallback_chat_id,
                           error=google_key_error,
                           valid_models=current_app.config['VALID_MODELS'])


@main_bp.route('/select_model', methods=['POST'])
def select_model():
    if current_app.config['GOOGLE_KEY_ERROR']:
        return redirect(url_for('main.index'))

    choice = request.form['model_choice']
    chat_id = session.get('current_chat_id')

    if not chat_id:
        return redirect(url_for('main.index'))

    if choice in current_app.config['VALID_MODELS']:
        db = get_db()
        db.execute('INSERT INTO chats (id, title, model) VALUES (?, ?, ?)',
                   (chat_id, 'New Chat', choice))
        db.execute('INSERT INTO messages (chat_id, role, content, message_type) VALUES (?, ?, ?, ?)',
                   (chat_id, 'model', f'Model selected: <strong>{choice}</strong>. You can now begin your chat.', 'chat'))
        db.commit()

    return redirect(url_for('main.chat', chat_id=chat_id))


@main_bp.route('/delete_chat/<string:chat_id>', methods=['POST'])
def delete_chat(chat_id):
    db = get_db()
    db.execute('DELETE FROM messages WHERE chat_id = ?', (chat_id,))
    db.execute('DELETE FROM chats WHERE id = ?', (chat_id,))
    db.commit()
    return redirect(url_for('main.index'))


@main_bp.route('/rename_chat/<string:chat_id>', methods=['POST'])
def rename_chat(chat_id):
    new_title = request.form['new_title'].strip()
    if not new_title:
        new_title = 'Untitled Chat'

    db = get_db()
    db.execute('UPDATE chats SET title = ? WHERE id = ?', (new_title, chat_id))
    db.commit()
    return redirect(url_for('main.chat', chat_id=chat_id))


@main_bp.route('/download_chat/<string:chat_id>')
def download_chat(chat_id):
    db = get_db()
    chat = db.execute('SELECT * FROM chats WHERE id = ?', (chat_id,)).fetchone()
    messages = db.execute('SELECT * FROM messages WHERE chat_id = ? ORDER BY created_at ASC', (chat_id,)).fetchall()

    if not chat:
        return redirect(url_for('main.index'))

    txt_output = io.StringIO()
    txt_output.write(f"Chat History: {chat['title']}\n")
    txt_output.write(f"Model: {chat['model']}\n")
    txt_output.write('=' * 30 + '\n\n')

    for msg in messages:
        content = clean_html_for_model(msg['content'])
        role = 'You' if msg['role'] == 'user' else 'Model'
        txt_output.write(f'--- {role} ({msg["message_type"]}) ---\n{content}\n\n')

    txt_output.seek(0)
    mem_file = io.BytesIO()
    mem_file.write(txt_output.getvalue().encode('utf-8'))
    mem_file.seek(0)

    # Sanitize filename to remove newlines, fixing the ValueError
    safe_title = chat['title'].replace(' ', '_').replace('\n', ' ').replace('\r', '')
    
    return send_file(
        mem_file,
        as_attachment=True,
        download_name=f'{safe_title}.txt',
        mimetype='text/plain'
    )


@main_bp.route('/api/chat', methods=['POST'])
def api_chat():
    google_key_error = current_app.config['GOOGLE_KEY_ERROR']
    if google_key_error or 'current_chat_id' not in session:
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
        
        db_messages = db.execute(
            'SELECT * FROM messages WHERE chat_id = ? ORDER BY created_at ASC', (chat_id,)
        ).fetchall()
        
        history_for_model = []
        for msg in db_messages:
            if msg['message_type'] in ['chat', 'tool_call', 'tool_output']:
                role = 'user' if msg['role'] == 'user' else 'model'
                content = clean_html_for_model(msg['content'])
                history_for_model.append({'role': role, 'parts': [content]})

        chat_session = model_instance.start_chat(history=history_for_model)

        decision = 'CHAT'
        if agent_mode:
            classifier_prompt = f"""
Analyze the user's prompt: "{prompt}"
Respond with *only* "CHAT" or "TASK".
"CHAT" means this is a general conversation, question, or request for information.
"TASK" means this requires using tools like listing, reading, writing, appending, or deleting files in the code/ directory.
"""
            classifier_response = chat_session.send_message(classifier_prompt)
            decision = classifier_response.text.strip().upper()

        if 'TASK' in decision and agent_mode:
            tools_list_str = json.dumps(get_available_tools(), indent=2)
            plan_prompt = f"""
User's Goal: "{prompt}"
Available Tools (scoped *only* to the 'code/' directory):
{tools_list_str}

Generate a multi-step plan to achieve the user's goal using *only* the provided tools.
Respond with *only* a JSON object in the following format:
{{
  "plan": [
    {{"step": 1, "tool": "tool_name", "parameters": {{"param1": "value1"}}, "reasoning": "Why this step is needed."}}
  ]
}}
"""
            response = chat_session.send_message(plan_prompt)
            
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
                            plan_html += f'<li><strong>{step["tool"]}</strong>: {step.get("reasoning", "N/A")}</li>'
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
            response = chat_session.send_message(prompt)
            if response.candidates:
                if response.candidates[0].finish_reason == 'SAFETY':
                    response_text = '--- ERROR: The response was blocked by safety filters. ---'
                else:
                    html_output = markdown.markdown(
                        response.text, 
                        extensions=['nl2br', 'fenced_code']
                    ) 
                    response_text = f'{html_output}\n\n<div class="msg-meta">(Model: {model_name})</div>'
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


@main_bp.route('/api/save_plan/<string:chat_id>', methods=['POST'])
def save_plan(chat_id):
    google_key_error = current_app.config['GOOGLE_KEY_ERROR']
    if google_key_error or chat_id != session.get('current_chat_id'):
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


@main_bp.route('/api/execute_plan/<string:chat_id>', methods=['POST'])
def execute_plan(chat_id):
    google_key_error = current_app.config['GOOGLE_KEY_ERROR']
    if google_key_error or chat_id != session.get('current_chat_id'):
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
    parameters = step_data.get('parameters', {})
    
    if tool_name not in TOOLS_MAP:
        error_msg = f'Error: Tool "{tool_name}" not found.'
        add_message_to_db(chat_id, 'model', error_msg, 'chat')
        return jsonify({'status': 'error', 'message': error_msg})
    
    try:
        tool_function = TOOLS_MAP[tool_name]
        tool_output_json = tool_function(**parameters)
        tool_output_data = json.loads(tool_output_json)

        # Removed the 'confirmation_pending' block
        
        tool_output_html = f'<pre>{json.dumps(tool_output_data, indent=2)}</pre>'
        add_message_to_db(chat_id, 'model', tool_output_html, 'tool_output')
        
        session[f'agent_step_{chat_id}'] = current_step_index + 1
        
        goal = session.get(f'agent_goal_{chat_id}')
        
        if session.get(f'agent_step_{chat_id}') >= len(plan):
            
            db = get_db()
            chat_history = db.execute(
                'SELECT * FROM messages WHERE chat_id = ? ORDER BY created_at ASC', (chat_id,)
            ).fetchall()
            
            history_for_model = []
            for msg in chat_history:
                if msg['message_type'] in ['chat', 'tool_call', 'tool_output']:
                    role = 'user' if msg['role'] == 'user' else 'model'
                    content = clean_html_for_model(msg['content'])
                    history_for_model.append({'role': role, 'parts': [content]})
            
            chat = db.execute('SELECT * FROM chats WHERE id = ?', (chat_id,)).fetchone()
            model_instance = genai.GenerativeModel(model_name=chat['model'])
            chat_session = model_instance.start_chat(history=history_for_model)
            
            reasoning_prompt = f"""
The multi-step plan is now complete.
Based on the chat history (especially the last few tool outputs), provide a final, comprehensive answer to the user's original goal: "{goal}"
Do not mention the raw JSON tool outputs in your answer; just describe the outcome (e.g., 'I have successfully saved the file' or 'The file has been deleted').
"""
        else:
            return jsonify({'status': 'proceed', 'message': 'Proceeding to next step...'})

        response = chat_session.send_message(reasoning_prompt)
        
        final_answer = 'Agent has completed the plan.'
        if response.candidates and response.candidates[0].finish_reason != 'SAFETY':
            final_answer = markdown.markdown(
                response.text, 
                extensions=['nl2br', 'fenced_code']
            )
        
        add_message_to_db(chat_id, 'model', final_answer, 'chat')
        session.pop(f'agent_plan_{chat_id}', None)
        session.pop(f'agent_step_{chat_id}', None)
        session.pop(f'agent_goal_{chat_id}', None)
        
        return jsonify({'status': 'complete', 'message': final_answer})
        
    except Exception as e:
        error_msg = f'An error occurred during agent execution: {e}'
        add_message_to_db(chat_id, 'model', error_msg, 'chat')
        return jsonify({'status': 'error', 'message': error_msg})


# The /api/agent_action route is now completely removed.