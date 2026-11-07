# Entry point for Vercel Serverless Functions
import os
import sys

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app


# Fix for Vercel WSGI environment:
class VercelWSGIMiddleware:
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")

        # Also check HTTP_X_MATCHED_PATH if present
        matched = environ.get("HTTP_X_MATCHED_PATH")
        if matched and not matched.startswith("/api"):
            path = matched

        for prefix in ["/api/index.py", "/api/index", "/api"]:
            if path.startswith(prefix):
                path = path[len(prefix):]
                break

        if not path or not path.startswith("/"):
            path = "/" + path

        environ["PATH_INFO"] = path
        environ["SCRIPT_NAME"] = ""
        return self.wsgi_app(environ, start_response)


app.wsgi_app = VercelWSGIMiddleware(app.wsgi_app)
