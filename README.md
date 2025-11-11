# Custom Gemini WebUI

This project is a web-based chat application that allows users to interact with various Google generative AI models. Key features include creating new chats, selecting from a list of available AI models, storing chat history, and downloading chat logs. The application is built with Flask and uses a SQLite database to manage conversations. It also includes non-stable agentic capabilities that allow the AI to perform file operations inside a sandboxed `code/` directory.

## Setup and Usage

To set up and run the project on a Linux terminal, follow these steps:

0. Navigate to the project directory:
   ```bash
   cd /path/to/your/project/directory
   ```
1. Create the virtual environment:
   ```bash
   python3 -m venv venv
   ```
2. Activate the virtual environment and set the environment variables:
   ```bash
   source venv/bin/activate
   ```
   ```bash
   export GOOGLE_API_KEY='ai-studio-api-key-here'
   ```
   ```bash
   export FLASK_SECRET_KEY=$(openssl rand -hex 16)
   ```
3. Install the required packages:
   ```bash
   pip install -r requirements.txt
   ```
4. Initialize the database:
   ```bash
   flask init-db
   ```
5. Run the application:
   ```bash
   flask --app app run --debug
   ```

## File Summaries

- **app.py**: The main Flask application file, responsible for initializing the application and its blueprints.
- **views.py**: Handles the primary routing, including chat interactions, model selection, and the new agent execution loop.
- **agent_tools.py**: Defines the sandboxed agentic capabilities, such as reading, writing, and listing files within the `code/` directory.
- **database.py**: Manages all database interactions, including creating a connection and adding messages.
- **requirements.txt**: Lists all the Python libraries and dependencies required to run the project.
- **schema.sql**: Contains the SQL statements to create the database schema for the chat application, including the `chats` and `messages` tables.
- **static/style.css**: The stylesheet for the application, which controls the visual appearance of the chat interface.
- **templates/index.html**: The main HTML template for the application, which defines the structure of the user interface.
- **.gitignore**: Specifies which files and directories should be ignored by Git.

## Library Versions

```
annotated-types==0.7.0
blinker==1.9.0
cachetools==6.2.1
certifi==2025.10.5
charset-normalizer==3.4.4
click==8.3.0
Flask==3.1.2
google-ai-generativelanguage==0.6.15
google-api-core==2.28.1
google-api-python-client==2.187.0
google-auth==2.43.0
google-auth-httplib2==0.2.1
google-generativeai==0.8.5
googleapis-common-protos==1.72.0
grpcio==1.76.0
grpcio-status==1.71.2
httplib2==0.31.0
idna==3.11
itsdangerous==2.2.0
Jinja2==3.1.6
Markdown==3.10
MarkupSafe==3.0.3
proto-plus==1.26.1
protobuf==5.29.5
pyasn1==0.6.1
pyasn1_modules==0.4.2
pydantic==2.12.4
pydantic_core==2.41.5
pyparsing==3.2.5
requests==2.32.5
rsa==4.9.1
tqdm==4.67.1
typing-inspection==0.4.2
typing_extensions==4.15.0
uritemplate==4.2.0
urllib3==2.5.0
Werkzeug==3.1.3
```
