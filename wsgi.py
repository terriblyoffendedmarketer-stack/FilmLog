"""
wsgi.py — PythonAnywhere WSGI entry point.

PythonAnywhere's WSGI config file imports `application` from here.
"""

import sys
import os
from pathlib import Path

# Add project directory to Python path
project_home = str(Path(__file__).parent)
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# Load environment variables from .env
from dotenv import load_dotenv
load_dotenv(Path(project_home) / ".env", override=True)

from app import app as application  # noqa: E402
