import os
import uuid
import subprocess
import re
import json
import pdfplumber
import docx
from flask import current_app

CODE_DIR = 'code'
WHITELISTED_EXTENSIONS = ['.pdf', '.txt', '.docx', '.py', '.c']


def resolve_code_path(filename):
    if '..' in filename or filename.startswith('/'):
        return None

    code_dir_abs = os.path.abspath(CODE_DIR)
    file_path_abs = os.path.normpath(os.path.join(code_dir_abs, filename))

    if not file_path_abs.startswith(code_dir_abs):
        return None

    return file_path_abs


def get_available_tools():
    return [
        {
            'name': 'list_code_directory',
            'description': f'List all files and directories within the {CODE_DIR}/ directory.',
            'parameters': {}
        },
        {
            'name': 'read_from_code_file',
            'description': f'Read the text content of a single whitelisted file from the {CODE_DIR}/ directory.',
            'parameters': {'filename': 'string'}
        },
        {
            'name': 'write_to_code_file',
            'description': f'Save or *overwrite* a text file (e.g., test.txt, script.py) inside the {CODE_DIR}/ directory.',
            'parameters': {'filename': 'string', 'content': 'string'}
        },
        {
            'name': 'append_to_code_file',
            'description': f'Add content to the *end* of an existing file (e.g., add a new function or comment) inside the {CODE_DIR}/ directory.',
            'parameters': {'filename': 'string', 'content': 'string'}
        },
        {
            'name': 'delete_code_file',
            'description': f'Delete a single file from the {CODE_DIR}/ directory.',
            'parameters': {'filename': 'string'}
        },
    ]


def list_code_directory():
    try:
        code_dir_abs = os.path.abspath(CODE_DIR)
        entries = os.listdir(code_dir_abs)
        return json.dumps({'entries': entries})
    except Exception as e:
        return json.dumps({'error': f'Error listing directory: {str(e)}'})


def read_from_code_file(filename):
    try:
        full_path = resolve_code_path(filename)
        if not full_path:
            return json.dumps({'error': 'Path traversal detected or path is invalid.'})

        if not os.path.exists(full_path) or not os.path.isfile(full_path):
            return json.dumps({'error': 'File not found or is not a file.'})

        _, ext = os.path.splitext(filename)
        if ext not in WHITELISTED_EXTENSIONS:
            return json.dumps({'error': f'File type {ext} is not whitelisted.'})

        content = ''
        if ext == '.pdf':
            with pdfplumber.open(full_path) as pdf:
                content = '\n'.join([page.extract_text() for page in pdf.pages if page.extract_text()])
        elif ext == '.docx':
            doc = docx.Document(full_path)
            content = '\n'.join([para.text for para in doc.paragraphs if para.text])
        else:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()

        return json.dumps({'filename': filename, 'content': content})
    except Exception as e:
        return json.dumps({'error': f'Error reading file: {str(e)}'})


def write_to_code_file(filename, content):
    full_path = resolve_code_path(filename)
    if not full_path:
        return json.dumps({'error': 'Path traversal detected or path is invalid.'})

    try:
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return json.dumps({'status': 'success', 'filename': filename})
    except Exception as e:
        return json.dumps({'error': f'Error saving file: {str(e)}'})


def append_to_code_file(filename, content):
    full_path = resolve_code_path(filename)
    if not full_path:
        return json.dumps({'error': 'Path traversal detected or path is invalid.'})
        
    if not os.path.exists(full_path):
        return json.dumps({'error': f'File not found: {filename}. Cannot append.'})

    try:
        with open(full_path, 'a', encoding='utf-8') as f:
            f.write(content)
        return json.dumps({'status': 'success', 'filename': filename})
    except Exception as e:
        return json.dumps({'error': f'Error appending to file: {str(e)}'})


def delete_code_file(filename):
    full_path = resolve_code_path(filename)
    if not full_path:
        return json.dumps({'error': 'Path traversal detected or path is invalid.'})

    if not os.path.exists(full_path):
        return json.dumps({'error': f'File not found: {filename}'})
    
    try:
        os.remove(full_path)
        return json.dumps({'status': 'success', 'deleted_file': filename})
    except Exception as e:
        return json.dumps({'error': f'Error deleting file: {str(e)}'})


TOOLS_MAP = {
    'list_code_directory': list_code_directory,
    'read_from_code_file': read_from_code_file,
    'write_to_code_file': write_to_code_file,
    'append_to_code_file': append_to_code_file,
    'delete_code_file': delete_code_file,
}