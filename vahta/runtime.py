"""Explicit server mode; desktop defaults remain unchanged."""
import os

SERVER_MODE = os.environ.get("VAHTA_SERVER") == "1"
