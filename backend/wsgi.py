#!/opt/alt/python39/bin/python3.9
"""Passenger entry point, matching the existing medweight.ca Twilio app's
wrapper script structure. Adjust the path below to wherever this repo
actually lives on the server."""
import sys

sys.path.insert(0, "/home/medweight/waterway-narrator/backend")
sys.path.insert(0, "/home/medweight/waterway-narrator")  # repo root, for render/

from app import app as application  # noqa: E402
