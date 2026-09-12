"""Config loading. Mirrors the hand-rolled env-file loader already used in
the medweight.ca Twilio app (`load_env_file` in its Passenger entry script)
rather than introducing python-dotenv as a new dependency style."""
import os


def load_env_file(path: str, override: bool = True) -> None:
    if not os.path.exists(path):
        return  # optional here (unlike the Twilio app) - real deploys may set env vars another way
    with open(path, "r") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if value and ((value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")):
                value = value[1:-1]
            if key and (override or key not in os.environ):
                os.environ[key] = value


_here = os.path.dirname(os.path.abspath(__file__))
load_env_file(os.path.join(_here, ".env"))

REPO_ROOT = os.path.dirname(_here)
DATA_DIR = os.environ.get("WATERWAY_DATA_DIR", os.path.join(REPO_ROOT, "data"))

DB_HOST = os.environ.get("WATERWAY_DB_HOST", "localhost")
DB_USER = os.environ.get("WATERWAY_DB_USER", "")
DB_PASSWORD = os.environ.get("WATERWAY_DB_PASSWORD", "")
DB_NAME = os.environ.get("WATERWAY_DB_NAME", "waterway_narrator")
DB_PORT = int(os.environ.get("WATERWAY_DB_PORT", "3306"))

MANUS_API_KEY = os.environ.get("MANUS_API_KEY", "")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Overridable so tests can point at a local mock instead of the real APIs.
MANUS_BASE_URL = os.environ.get("MANUS_BASE_URL", "https://api.manus.ai")
ELEVENLABS_BASE_URL = os.environ.get("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io")
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

WORKER_LOCK_FILE = os.environ.get("WATERWAY_WORKER_LOCK", os.path.join(DATA_DIR, "worker.lock"))
