import os
import json
import re
import uuid
import subprocess
import pdfplumber
import docx
import nbformat
from functools import lru_cache

CODE_DIR = 'code'
HOME_DIR = os.path.expanduser('~')
WHITELISTED_EXTENSIONS = ['.pdf', '.txt', '.docx', '.py', '.c', '.ipynb']
CONTEXT_WINDOW_THRESHOLD = 65536
MAX_FILES_BEFORE_SELECTION = 64


@lru_cache(maxsize=1)
def get_home_dir():
    return os.path.expanduser('~')


def resolve_path(path):
    home = get_home_dir()
    path = os.path.normpath(path)
    if path.startswith('~'):
        path = path.replace('~', home, 1)

    full_path = os.path.normpath(os.path.join(home, path))

    if not os.path.realpath(full_path).startswith(os.path.realpath(home)):
        return None

    return full_path


def estimate_tokens(text):
    return len(text) / 4


def list_directory(path):
    try:
        if path == '.' or path == '~' or path == '~/':
            return json.dumps({'error': "Listing the entire home directory ('.' or '~') is not allowed. Please specify a subdirectory (e.g., 'Documents/')."})

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
                'error': f'File {path} is too large ({token_count} tokens). This file cannot be used.'
            })

        return json.dumps({'path': path, 'content': content, 'tokens': token_count})
    except Exception as e:
        return json.dumps({'error': f'Error reading file: {str(e)}'})


def read_directory_recursively(path, selected_files=None):
    try:
        if path == '.' or path == '~' or path == '~/':
            return json.dumps({'error': "Reading the entire home directory ('.' or '~') is not allowed. Please specify a subdirectory (e.g., 'Documents/')."})

        full_path = resolve_path(path)
        if not full_path:
            return json.dumps({'error': 'Path traversal detected or path is invalid.'})

        if not os.path.exists(full_path) or not os.path.isdir(full_path):
            return json.dumps({'error': 'Path does not exist or is not a directory.'})

        files_data = {}
        files_with_tokens = []
        total_token_count = 0
        file_scan_count = 0
        home = get_home_dir()

        for root, _, files in os.walk(full_path):
            if not selected_files and file_scan_count > MAX_FILES_BEFORE_SELECTION:
                break

            for file in files:
                if not selected_files and file_scan_count > MAX_FILES_BEFORE_SELECTION:
                    break

                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, home)
                _, ext = os.path.splitext(file)

                if ext in WHITELISTED_EXTENSIONS:
                    file_scan_count += 1
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

        if not selected_files and (total_token_count > CONTEXT_WINDOW_THRESHOLD or file_scan_count > MAX_FILES_BEFORE_SELECTION):
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

    if re.search(r'\b(mkdir|printf|echo|tee)\b', command):
        return {'status': 'error', 'message': f"Command '{command}' is blocked. Use 'save_text_file' or 'edit_text_file' to write files."}

    return {'status': 'approved'}


def run_terminal_command(command):
    validation = command_validator(command)

    if validation['status'] == 'error':
        return json.dumps({'error': validation['message']})

    if validation['status'] == 'confirmation_pending':
        return json.dumps(validation)

    try:
        result = subprocess.run(
            command,
            shell=True,
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


def edit_text_file(filename, content):
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(CODE_DIR, safe_filename)

    if not os.path.exists(file_path):
        return json.dumps({'error': f'Error editing file: File {safe_filename} does not exist in code/ directory. Use save_text_file to create it first.'})

    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return json.dumps({'status': 'success', 'path': file_path})
    except Exception as e:
        return json.dumps({'error': f'Error editing file: {str(e)}'})


def get_available_tools():
    return [
        {'name': 'list_directory', 'description': "List files/dirs in a path relative to home (~). Cannot be the home directory '.' or '~' itself.", 'parameters': {'path': 'string'}},
        {'name': 'read_file_content', 'description': 'Read text content of one file (relative to home ~).', 'parameters': {'path': 'string'}},
        {'name': 'read_directory_recursively', 'description': "Read content of all whitelisted files in a directory (relative to home ~) and its subdirs. Cannot be the home dir '.' or '~' itself.", 'parameters': {'path': 'string'}},
        {'name': 'save_text_file', 'description': "Save a text string to a **new file** (e.g., script.py) inside the code/ directory. This is the ONLY tool for creating new files.", 'parameters': {'filename': 'string', 'content': 'string'}},
        {'name': 'edit_text_file', 'description': "Edit/overwrite an **existing file** (e.g., script.py) inside the code/ directory. This is the ONLY tool for modifying existing files.", 'parameters': {'filename': 'string', 'content': 'string'}},
        {'name': 'run_terminal_command', 'description': "Execute a sandboxed terminal command (non-interactive) inside the code/ directory. Cannot be used to write files (e.g., 'mkdir', 'printf', 'echo').", 'parameters': {'command': 'string'}},
        {'name': 'execute_python_script', 'description': 'Execute a Python script string in a sandbox (inside the code/ directory).', 'parameters': {'code_string': 'string'}},
    ]


TOOLS_MAP = {
    'list_directory': list_directory,
    'read_file_content': read_file_content,
    'read_directory_recursively': read_directory_recursively,
    'save_text_file': save_text_file,
    'edit_text_file': edit_text_file,
    'run_terminal_command': run_terminal_command,
    'execute_python_script': execute_python_script,
}