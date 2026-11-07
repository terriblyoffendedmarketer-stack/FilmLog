# Entry point for Vercel Serverless Functions
import os
import sys

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app


# Add routes for /api/index and /api/index.py root if Vercel hits them directly
@app.route("/api/index")
@app.route("/api/index/")
@app.route("/api/index.py")
@app.route("/api/index.py/")
def _vercel_index_route():
    return app.view_functions["index"]()


# Middleware to ensure SCRIPT_NAME is clean and strip any /api/index prefix
class VercelWSGIMiddleware:
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        import json
        path_info = environ.get("PATH_INFO", "")
        req_uri = environ.get("REQUEST_URI", "")
        if path_info.endswith("/_env") or "/_env" in req_uri:
            start_response("200 OK", [("Content-Type", "application/json")])
            info = {k: str(v) for k, v in environ.items() if isinstance(v, (str, int, float, bool))}
            return [json.dumps(info, indent=2).encode("utf-8")]

        path = environ.get("PATH_INFO", "")
        for prefix in ["/api/index.py", "/api/index"]:
            if path.startswith(prefix):
                path = path[len(prefix):]
                break

        if not path or not path.startswith("/"):
            path = "/" + path

        environ["PATH_INFO"] = path
        environ["SCRIPT_NAME"] = ""
        return self.wsgi_app(environ, start_response)


app.wsgi_app = VercelWSGIMiddleware(app.wsgi_app)
