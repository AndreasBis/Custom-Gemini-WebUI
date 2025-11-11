import os
import click
import google.generativeai as genai
from flask import Flask
from flask.cli import with_appcontext
from database import init_db, close_db
from views import main_bp

try:
    FLASK_KEY = os.environ['FLASK_SECRET_KEY']
except KeyError:
    raise RuntimeError(
        'FATAL: FLASK_SECRET_KEY environment variable is not set. '
        'Run: export FLASK_SECRET_KEY=$(openssl rand -hex 16)'
    )

GOOGLE_KEY_ERROR = None
try:
    genai.configure(api_key=os.environ['GOOGLE_API_KEY'])
except KeyError:
    GOOGLE_KEY_ERROR = (
        'Error: GOOGLE_API_KEY environment variable not set. '
        'Please set it in your terminal and restart the server.'
    )

VALID_MODELS = [
    'gemini-2.5-pro',
    'gemini-2.5-flash',
    'gemini-2.5-flash-lite',
]

CODE_DIR = 'code'


def create_app():
    app = Flask(__name__)
    app.secret_key = FLASK_KEY
    app.config['DATABASE'] = 'app.db'
    app.config['GOOGLE_KEY_ERROR'] = GOOGLE_KEY_ERROR
    app.config['VALID_MODELS'] = VALID_MODELS
    app.config['CODE_DIR'] = CODE_DIR

    if not os.path.exists(CODE_DIR):
        os.makedirs(CODE_DIR)

    @click.command('init-db')
    @with_appcontext
    def init_db_command():
        init_db()
        click.echo('Initialized the database.')

    app.cli.add_command(init_db_command)
    app.teardown_appcontext(close_db)
    app.register_blueprint(main_bp)

    return app


if __name__ == '__main__':
    app = create_app()
    app.run(debug=True)