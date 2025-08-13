from flask import Flask, request, jsonify
import time
import requests

# =========================
# CONFIG
# =========================
TOKEN = "7577508576:AAHfLBilu1QlOwWT_ZWfIkXrf6pZrHvBo_s"
CHAT_ID = "-4813389883"
SESSION_EXPIRE_SECONDS = 1 * 60 * 60  # 1 hour
PORT = 5000  # Render will override this with $PORT

# Store checklist state per username (case-insensitive)
checklists = {}

app = Flask(__name__)

def _now(): 
    return time.time()

def _norm(s): 
    return (s or "").strip().lower()

def send_to_telegram(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload)
        r.raise_for_status()
    except Exception as e:
        print(f"Error sending to Telegram: {e}")

@app.route("/rollcall", methods=["POST"])
def rollcall():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    username = (data.get("username") or "").strip()
    machine_name = (data.get("machine") or "").strip()

    if not username or not machine_name:
        return jsonify({"error": "Missing username or machine"}), 400

    live_key = _norm(username)
    now = _now()

    state = checklists.get(live_key)
    if state is None or (now - state.get("last_update", 0)) >= SESSION_EXPIRE_SECONDS:
        state = {"machines": set(), "title": username, "last_update": now}
        checklists[live_key] = state

    if machine_name in state["machines"]:
        return jsonify({"status": "duplicate"}), 200

    state["machines"].add(machine_name)
    state["last_update"] = now

    # Build checklist
    checklist_lines = [f"✅ {m}" for m in sorted(state["machines"], key=str.lower)]
    text = f"🔴 Rollcall – {state['title']}\n" + "\n".join(checklist_lines)

    send_to_telegram(text)
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", PORT)))
