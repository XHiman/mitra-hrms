"""Entry point.

Development:  python run.py           (http://127.0.0.1:5000)
Production:   waitress-serve --listen=127.0.0.1:8080 --call run:create_app   (behind an HTTPS reverse proxy)
"""
import os

from hrms import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host=os.environ.get("HRMS_HOST", "127.0.0.1"), port=int(os.environ.get("HRMS_PORT", "5000")), debug=False)
