# Entry point for Vercel Serverless Functions
import os
import sys

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app


# Fix for Vercel WSGI environment:
# When Vercel rewrites to /api/index.py, environ['PATH_INFO'] can contain /api/index.py
# or the original path is in HTTP_X_NOW_ROUTE_MATCHES or HTTP_X_MATCHED_PATH.
class VercelWSGIMiddleware:
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")

        # If PATH_INFO has the /api/index.py or /api/index prefix, strip it
        if path.startswith("/api/index.py"):
            path = path[len("/api/index.py"):] or "/"
        elif path.startswith("/api/index"):
            path = path[len("/api/index"):] or "/"
        elif path.startswith("/api"):
            path = path[len("/api"):] or "/"

        environ["PATH_INFO"] = path
        return self.wsgi_app(environ, start_response)


app.wsgi_app = VercelWSGIMiddleware(app.wsgi_app)
