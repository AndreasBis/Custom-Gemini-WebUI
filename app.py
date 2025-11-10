import os
import time
import uuid
import markdown
import re
import io
import json
import subprocess
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, send_file
import database
import models
from agent_tools import get_available_tools, TOOLS_MAP

try:
    FLASK_KEY = os.environ['FLASK_SECRET_KEY']
except KeyError:
    raise RuntimeError('FATAL: FLASK_SECRET_KEY env var is not set. Run: export FLASK_SECRET_KEY=$(openssl rand -hex 16)')

GOOGLE_KEY_ERROR = None
try:
    genai.configure(api_key=os.environ['GOOGLE_API_KEY'])
except KeyError:
    GOOGLE_KEY_ERROR = 'Error: GOOGLE_API_KEY environment variable not set. Please set it and restart the server.'

app = Flask(__name__)
app.secret_key = FLASK_KEY
database.init_app(app)

VALID_MODELS = [
    'gemini-2.5-pro',
    'gemini-2.5-flash',
    'gemini-2.5-flash-lite',
]


def _get_metadata_footer(chat_id):
    chat = models.get_chat(chat_id)
    model_name = chat['model']
    msg_count = models.get_message_count(chat_id, 'user')
    counter_html = f'<span class="msg-counter">{msg_count}</span>'
    return f'\n\n<div class="msg-meta">(Model: {model_name} | {counter_html})</div>'


@app.route('/')
def index():
    latest_chat = models.get_all_chats()
    if latest_chat:
        return redirect(url_for('chat', chat_id=latest_chat[0]['id']))
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

    all_chats = models.get_all_chats()
    current_chat_messages = models.get_chat_messages(chat_id)
    current_chat = models.get_chat(chat_id)

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
        models.create_new_chat(chat_id, 'New Chat', choice)
        greeting_html = f'Model selected: <strong>{choice}</strong>. You can now begin your chat.'
        greeting_raw = f'Model selected: {choice}. You can now begin your chat.'
        models.add_message_to_db(chat_id, 'model', greeting_html, greeting_raw, 'chat')

    return redirect(url_for('chat', chat_id=chat_id))


@app.route('/delete_chat/<string:chat_id>', methods=['POST'])
def delete_chat(chat_id):
    models.delete_chat_from_db(chat_id)
    return redirect(url_for('index'))


@app.route('/rename_chat/<string:chat_id>', methods=['POST'])
def rename_chat(chat_id):
    new_title = request.form['new_title'].strip()
    if not new_title:
        new_title = 'Untitled Chat'

    models.rename_chat_in_db(chat_id, new_title)
    return redirect(url_for('chat', chat_id=chat_id))


def clean_html_for_download(raw_html):
    
    clean_text = re.sub(r'<div class="msg-meta".*?</div>', '', raw_html, flags=re.DOTALL)
    
    cleanr = re.compile('<.*?>')
    clean_text = re.sub(cleanr, '', clean_text)
    clean_text = clean_text.replace('<br>', '\n').replace('<p>', '').replace('</p>', '\n')

    clean_text = re.sub(r'<h3>Agent Plan</h3>', 'Agent Plan\n', clean_text)
    clean_text = re.sub(r'<h4>.*?</h4>', '', clean_text)
    clean_text = re.sub(r'<ol>(.*?)</ol>', lambda m: '\n'.join([f'  - {li.strip()}' for li in re.findall(r'<li>(.*?)</li>', m.group(1))]), clean_text, flags=re.DOTALL)
    clean_text = re.sub(r'<strong>Tool Call:</strong> <code>(.*?)</code>', r'Tool Call: \1', clean_text)
    clean_text = re.sub(r'<pre>(.*?)</pre>', r'Output:\n---\n\1\n---', clean_text, flags=re.DOTALL)
    clean_text = re.sub(r'&gt;', '>', clean_text)
    clean_text = re.sub(r'&lt;', '<', clean_text)
    clean_text = re.sub(r'&amp;', '&', clean_text)

    return clean_text.strip()


@app.route('/download_chat/<string:chat_id>')
def download_chat(chat_id):
    chat = models.get_chat(chat_id)
    messages = models.get_chat_messages(chat_id)

    if not chat:
        return redirect(url_for('index'))

    txt_output = io.StringIO()
    txt_output.write(f'Chat History: {chat['title']}\n')
    txt_output.write(f'Model: {chat['model']}\n')
    txt_output.write('='*30 + '\n\n')

    for msg in messages:
        if msg['message_type'] == 'chat':
            role = 'You' if msg['role'] == 'user' else 'Model'
            content = msg['raw_content']
            txt_output.write(f'--- {role} ---\n{content}\n\n')
        else:
            content = clean_html_for_download(msg['content'])
            if msg['message_type'] == 'agent_plan':
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


def build_chat_history(chat_id):
    history_list = []
    chat_history = models.get_chat_messages(chat_id)
    for msg in chat_history:
        history_list.append({'role': msg['role'], 'parts': [msg['raw_content']]})
    return history_list


@app.route('/api/chat', methods=['POST'])
def api_chat():
    if GOOGLE_KEY_ERROR or 'current_chat_id' not in session:
        return jsonify({'error': 'Session error or API key missing'}), 400

    chat_id = session['current_chat_id']
    prompt = request.json['prompt']
    agent_mode = request.json.get('agent_mode', False)

    chat = models.get_chat(chat_id)
    if not chat:
        return jsonify({'error': 'Chat not found'}), 404

    model_name = chat['model']
    models.add_message_to_db(chat_id, 'user', prompt, prompt, 'chat')

    if chat['title'] == 'New Chat':
        try:
            title_prompt = f'Generate a short, 3-5 word title for a chat that starts with this prompt: "{prompt}"'
            title_model = genai.GenerativeModel(model_name='gemini-2.5-flash-lite')
            title_response = title_model.generate_content(title_prompt)
            new_title = title_response.text.strip().replace('"', '').strip('*_#~')
            models.update_chat_title(chat_id, new_title)
        except Exception:
            pass

    response_text = ''
    raw_response_text = ''
    message_type = 'chat'
    raw_plan = None
    footer = _get_metadata_footer(chat_id)

    try:
        model_instance = genai.GenerativeModel(model_name=model_name)

        decision = 'CHAT'
        if agent_mode:
            classifier_prompt = f"""
Analyze the user's prompt: "{prompt}"

Respond with *only* "CHAT" or "TASK".
"CHAT" means a general conversation, question, or request for information that doesn't require using tools.
"TASK" means the user is asking to:
- list/read files ('list_directory', 'read_file_content')
- write/create/save a new file ('save_text_file')
- edit/modify/update an existing file ('edit_text_file')
- run code or a terminal command ('execute_python_script', 'run_terminal_command')
"""
            classifier_response = model_instance.generate_content(classifier_prompt)
            decision = classifier_response.text.strip().upper()

        if 'TASK' in decision and agent_mode:
            tools_list_str = json.dumps(get_available_tools(), indent=2)
            plan_prompt = f"""
User's Goal: "{prompt}"

Available Tools:
{tools_list_str}

You are an agent executor brain. Your task is to generate a multi-step plan to achieve the user's goal.
**CRITICAL RULES:**
1.  **ONLY use the tools provided.** Do not invent commands.
2.  To create a **new** file, you **MUST** use `save_text_file`.
3.  To **modify** an existing file, you **MUST** use `edit_text_file`.
4.  You **CANNOT** use `run_terminal_command` to write files (e.g., no 'echo', 'printf', 'mkdir', 'tee').
5.  All file writing happens **ONLY** inside the `code/` directory. The tools handle this automatically.
6.  Do not use `list_directory` on '.' or '~'. Always specify a subdirectory.

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
                        plan_html += f'<div class="plan-actions"><button class="approve-btn" onclick="approvePlan(\'{chat_id}\', this)">Approve</button><button class="edit-btn" onclick="editPlan(this, \'{chat_id}\')">Edit</button><button class="cancel-btn" onclick="cancelPlan()">Cancel</button></div>'

                        response_text = plan_html + footer
                        raw_response_text = raw_plan_text
                        message_type = 'agent_plan'
                        raw_plan = raw_plan_text
                    else:
                        raise ValueError('Invalid plan structure')
                except Exception as e:
                    response_text = f'--- ERROR: Model generated an invalid plan. Trying as chat instead. ---\n{e}\n{raw_plan_text}' + footer
                    raw_response_text = response_text
                    decision = 'CHAT'
            else:
                response_text = '--- ERROR: The agent plan was blocked by safety filters. ---' + footer
                raw_response_text = response_text

        if 'CHAT' in decision or not agent_mode:
            chat_history = build_chat_history(chat_id)
            chat_session = model_instance.start_chat(history=chat_history[:-1])
            response = chat_session.send_message(prompt)

            if response.candidates:
                if response.candidates[0].finish_reason == 'SAFETY':
                    response_text = '--- ERROR: The response was blocked by safety filters. ---'
                    raw_response_text = response_text
                else:
                    raw_response_text = response.text
                    html_output = markdown.markdown(response.text, extensions=['nl2br'])
                    response_text = html_output
            else:
                feedback = response.prompt_feedback
                block_reason = feedback.block_reason.name if feedback.block_reason else 'Unknown'
                response_text = f'--- ERROR: Your prompt was blocked by safety filters (Reason: {block_reason}). Please rephrase. ---'
                raw_response_text = response_text
            
            response_text += footer

    except ResourceExhausted:
        response_text = '--- ERROR: RATE LIMIT EXCEEDED ---\nWaiting for 60 seconds. Please resubmit your prompt after the wait.' + footer
        raw_response_text = response_text
    except Exception as e:
        response_text = f'An error occurred: {e}' + footer
        raw_response_text = response_text

    models.add_message_to_db(chat_id, 'model', response_text, raw_response_text, message_type)

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

        html_content = f'User edited and approved plan:\n<pre>{json.dumps(plan_data, indent=2)}</pre>'
        raw_content = f'User edited and approved plan: {plan_json_str}'
        models.add_message_to_db(chat_id, 'user', html_content, raw_content, 'user_confirmation')
        return jsonify({'status': 'saved'})

    except Exception as e:
        return jsonify({'error': f'Failed to save plan: {str(e)}'}), 500


@app.route('/api/execute_plan/<string:chat_id>', methods=['POST'])
def execute_plan(chat_id):
    if GOOGLE_KEY_ERROR or chat_id != session.get('current_chat_id'):
        return jsonify({'error': 'Session error or API key missing'}), 400

    plan = session.get(f'agent_plan_{chat_id}')
    current_step_index = session.get(f'agent_step_{chat_id}', 0)
    footer = _get_metadata_footer(chat_id)

    if plan is None or current_step_index >= len(plan):
        session.pop(f'agent_plan_{chat_id}', None)
        session.pop(f'agent_step_{chat_id}', None)
        session.pop(f'agent_goal_{chat_id}', None)
        final_message = 'Agent has completed the plan.' + footer
        models.add_message_to_db(chat_id, 'model', final_message, 'Agent has completed the plan.', 'chat')
        return jsonify({'status': 'complete', 'message': final_message})

    step_data = plan[current_step_index]
    tool_name = step_data['tool']
    parameters = step_data['parameters']

    if tool_name not in TOOLS_MAP:
        error_msg = f'Error: Tool "{tool_name}" not found.' + footer
        models.add_message_to_db(chat_id, 'model', error_msg, error_msg, 'chat')
        return jsonify({'status': 'error', 'message': error_msg})

    tool_call_html = f'<strong>Tool Call:</strong> <code>{tool_name}({json.dumps(parameters)})</code>' + footer
    tool_call_raw = f'Tool Call: {tool_name}({json.dumps(parameters)})'
    models.add_message_to_db(chat_id, 'model', tool_call_html, tool_call_raw, 'tool_call')

    try:
        tool_function = TOOLS_MAP[tool_name]

        if tool_name == 'read_directory_recursively' and 'selected_files' in request.json:
            parameters['selected_files'] = request.json['selected_files']

        tool_output_json = tool_function(**parameters)
        tool_output_data = json.loads(tool_output_json)

        if tool_output_data.get('status') == 'confirmation_pending':
            command_escaped = json.dumps(tool_output_data["command"])
            confirm_html = f'<strong>Deletion Confirmation Required</strong><p>The agent wants to run: <code>{tool_output_data["command"]}</code></p>'
            confirm_html += f'<div class="plan-actions"><button class="approve-btn" onclick="handleDeletion({command_escaped}, \'{chat_id}\', this)">Approve Deletion</button><button class="cancel-btn" onclick="cancelPlan()">Deny</button></div>'
            confirm_html += footer
            confirm_raw = f'Deletion Confirmation Required for command: {tool_output_data["command"]}'
            models.add_message_to_db(chat_id, 'model', confirm_html, confirm_raw, 'user_confirmation')
            return jsonify({'status': 'confirmation_pending', 'content': confirm_html})

        if tool_output_data.get('status') == 'file_selection_pending':
            file_list = tool_output_data['files']
            select_html = f'<strong>File Selection Required</strong><p>The agent found {len(file_list)} files ({tool_output_data["total_tokens"]} tokens), which exceeds the limit.</p>'
            select_html += f'<form id="file-select-form" onsubmit="handleFileSelection(event, \'{chat_id}\')">'
            select_html += '<div class="file-selection-list">'
            for f in file_list:
                select_html += f'<label><input type="checkbox" name="selected_files" value="{f["path"]}"> {f["path"]} ({f["tokens"]} tokens)</label><br>'
            select_html += '</div><button type="submit">Process Selected Files</button></form>'
            select_html += footer
            select_raw = f'File Selection Required: {len(file_list)} files, {tool_output_data["total_tokens"]} tokens.'
            models.add_message_to_db(chat_id, 'model', select_html, select_raw, 'user_confirmation')
            return jsonify({'status': 'file_selection_pending', 'content': select_html})

        tool_output_html = f'<pre>{json.dumps(tool_output_data, indent=2)}</pre>' + footer
        tool_output_raw = json.dumps(tool_output_data, indent=2)
        models.add_message_to_db(chat_id, 'model', tool_output_html, tool_output_raw, 'tool_output')

        session[f'agent_step_{chat_id}'] = current_step_index + 1

        history_for_model = build_chat_history(chat_id)
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

        chat = models.get_chat(chat_id)
        model_instance = genai.GenerativeModel(model_name=chat['model'])
        response = model_instance.generate_content(reasoning_prompt)

        final_answer_raw = 'Agent has completed the plan.'
        final_answer = 'Agent has completed the plan.'
        if response.candidates and response.candidates[0].finish_reason != 'SAFETY':
            final_answer_raw = response.text
            final_answer = markdown.markdown(response.text, extensions=['nl2br'])
        
        final_answer += footer
        
        models.add_message_to_db(chat_id, 'model', final_answer, final_answer_raw, 'chat')
        session.pop(f'agent_plan_{chat_id}', None)
        session.pop(f'agent_step_{chat_id}', None)
        session.pop(f'agent_goal_{chat_id}', None)

        return jsonify({'status': 'complete', 'message': final_answer})

    except Exception as e:
        error_msg = f'An error occurred during agent execution: {str(e)}' + footer
        models.add_message_to_db(chat_id, 'model', error_msg, str(e), 'chat')
        return jsonify({'status': 'error', 'message': error_msg})


@app.route('/api/agent_action', methods=['POST'])
def agent_action():
    if GOOGLE_KEY_ERROR or 'current_chat_id' not in session:
        return jsonify({'error': 'Session error or API key missing'}), 400

    chat_id = session['current_chat_id']
    data = request.json
    action_type = data.get('action_type')
    action_data = data.get('action_data', {})
    footer = _get_metadata_footer(chat_id)

    plan = session.get(f'agent_plan_{chat_id}')
    current_step_index = session.get(f'agent_step_{chat_id}', 0)

    if plan is None:
        return jsonify({'error': 'No active plan found'}), 400

    step_data = plan[current_step_index]
    tool_name = step_data['tool']

    if action_type == 'confirm_deletion' and tool_name == 'run_terminal_command':
        command = action_data.get('command')
        user_msg_html = f'User approved command: <code>{command}</code>'
        user_msg_raw = f'User approved command: {command}'
        models.add_message_to_db(chat_id, 'user', user_msg_html, user_msg_raw, 'user_confirmation')

        try:
            result = subprocess.run(
                command,
                shell=True,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=os.path.abspath('code'),
                timeout=30
            )
            tool_output_json = json.dumps({'stdout': result.stdout, 'stderr': result.stderr})
        except Exception as e:
            tool_output_json = json.dumps({'error': f'Error executing confirmed command: {str(e)}'})

        tool_output_html = f'<pre>{tool_output_json}</pre>' + footer
        models.add_message_to_db(chat_id, 'model', tool_output_html, tool_output_json, 'tool_output')

        session[f'agent_step_{chat_id}'] = current_step_index + 1
        return jsonify({'status': 'proceed', 'message': 'Deletion confirmed, proceeding...'})

    elif action_type == 'process_selected_files' and tool_name == 'read_directory_recursively':
        selected_files = action_data.get('selected_files', [])
        user_msg_html = f'User selected {len(selected_files)} files to process.'
        user_msg_raw = user_msg_html
        models.add_message_to_db(chat_id, 'user', user_msg_html, user_msg_raw, 'user_confirmation')

        tool_output_json = TOOLS_MAP['read_directory_recursively'](
            step_data['parameters']['path'],
            selected_files=selected_files
        )
        tool_output_html = f'<pre>{json.dumps(json.loads(tool_output_json), indent=2)}</pre>' + footer
        models.add_message_to_db(chat_id, 'model', tool_output_html, tool_output_json, 'tool_output')

        session[f'agent_step_{chat_id}'] = current_step_index + 1
        return jsonify({'status': 'proceed', 'message': 'Files processed, proceeding...'})

    return jsonify({'error': 'Invalid action or state'}), 400


if __name__ == '__main__':
    app.run(debug=True)