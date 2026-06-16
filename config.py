import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
CREDENTIALS_PATH = os.path.join(DATA_DIR, "tricount_credentials.json")
RECURRING_DB_PATH = os.path.join(DATA_DIR, "recurring_expenses.db")
CONNECTIONS_DB_PATH = os.path.join(DATA_DIR, "tricount_connections.db")
SECRET_KEY = os.urandom(24).hex()
DEBUG = os.environ.get("FLASK_DEBUG", "1") == "1"
