# Entry point for Vercel Serverless Functions
import os
import sys

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app


# Mirror all routes with /api/index.py prefix for Vercel rewrites
for rule in list(app.url_map.iter_rules()):
    if not rule.rule.startswith("/static") and rule.endpoint in app.view_functions:
        api_rule = "/api/index.py" + rule.rule
        try:
            app.add_url_rule(
                api_rule,
                endpoint=f"v_{rule.endpoint}",
                view_func=app.view_functions[rule.endpoint],
                methods=rule.methods
            )
        except Exception:
            pass

        if rule.rule == "/":
            try:
                app.add_url_rule(
                    "/api/index.py",
                    endpoint="v_root_bare",
                    view_func=app.view_functions[rule.endpoint],
                    methods=rule.methods
                )
            except Exception:
                pass
