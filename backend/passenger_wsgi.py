#!/opt/alt/python39/bin/python3.9
import sys

# =============================================================================
# APP PATHS
# =============================================================================

# backend/ itself (for `import app`, `import config`, etc.)
sys.path.insert(0, '/home/medweight/chatbot-shorts/backend')
# repo root (for `from render.render import render`)
sys.path.insert(0, '/home/medweight/chatbot-shorts')

# =============================================================================
# ENVIRONMENT CONFIG
# =============================================================================
# Unlike the Twilio app, env loading happens inside backend/config.py itself
# (triggered the moment `app` below imports `config`) rather than here - one
# less thing to keep in sync between this file and config.py. Nothing to do
# in this section; config.py reads backend/.env automatically.

# =============================================================================
# Start Application
# =============================================================================
from app import app as application  # noqa: E402
