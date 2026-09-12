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
DATA_DIR = os.environ.get("CHATBOT_SHORTS_DATA_DIR", os.path.join(REPO_ROOT, "data"))

DB_HOST = os.environ.get("CHATBOT_SHORTS_DB_HOST", "localhost")
DB_USER = os.environ.get("CHATBOT_SHORTS_DB_USER", "")
DB_PASSWORD = os.environ.get("CHATBOT_SHORTS_DB_PASSWORD", "")
DB_NAME = os.environ.get("CHATBOT_SHORTS_DB_NAME", "chatbot_shorts")
DB_PORT = int(os.environ.get("CHATBOT_SHORTS_DB_PORT", "3306"))

MANUS_API_KEY = os.environ.get("MANUS_API_KEY", "")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Overridable so tests can point at a local mock instead of the real APIs.
MANUS_BASE_URL = os.environ.get("MANUS_BASE_URL", "https://api.manus.ai")
ELEVENLABS_BASE_URL = os.environ.get("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io")
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

WORKER_LOCK_FILE = os.environ.get("CHATBOT_SHORTS_WORKER_LOCK", os.path.join(DATA_DIR, "worker.lock"))

# Rendering runs on AWS Fargate instead of in-process - the cPanel shared
# host's CPU throttling made local moviepy/ffmpeg encoding take 50+ minutes
# for a 33-second test video (confirmed live). AWS_ACCESS_KEY_ID/
# AWS_SECRET_ACCESS_KEY are read directly by boto3 from the environment (set
# via .env like everything else here) - no need to reference them by name.
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
RENDER_S3_BUCKET = os.environ.get("RENDER_S3_BUCKET", "")
RENDER_ECS_CLUSTER = os.environ.get("RENDER_ECS_CLUSTER", "chatbot-shorts-cluster")
RENDER_ECS_TASK_DEFINITION = os.environ.get("RENDER_ECS_TASK_DEFINITION", "chatbot-shorts-render")
RENDER_ECS_SUBNETS = [s for s in os.environ.get("RENDER_ECS_SUBNETS", "").split(",") if s]
RENDER_ECS_SECURITY_GROUPS = [s for s in os.environ.get("RENDER_ECS_SECURITY_GROUPS", "").split(",") if s]
RENDER_TIMEOUT_MINUTES = int(os.environ.get("RENDER_TIMEOUT_MINUTES", "15"))
