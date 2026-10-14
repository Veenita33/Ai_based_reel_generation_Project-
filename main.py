"""
Local development entrypoint.

Run with:   python main.py
Then open:  http://127.0.0.1:5000
"""
import os
from app import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
