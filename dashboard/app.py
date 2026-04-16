from flask import Flask, render_template, jsonify
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import database.db as db
from broker import alpaca

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/portfolio")
def api_portfolio():
    try:
        portfolio  = alpaca.get_portfolio()
        positions  = alpaca.get_positions()
        snapshots  = db.get_snapshots(30)
        return jsonify({"portfolio": portfolio, "positions": positions, "history": snapshots})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trades")
def api_trades():
    return jsonify(db.get_trades(100))


@app.route("/api/signals")
def api_signals():
    return jsonify(db.get_signals(50))


if __name__ == "__main__":
    db.init()
    app.run(debug=True, port=5000)
