from flask import Flask, request, jsonify
import time
import requests
import os
from math import ceil

# =========================
# CONFIG
# =========================
TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SESSION_EXPIRE_SECONDS = 60 * 40  # 40 mins
PORT = 5000  # Render will override with $PORT

# Fixed 10 devices in the order you want them displayed
DEVICE_ORDER = []

# In-memory state per live (keyed by normalized live name)
# state = {
#   live_key: {
#       "title": str,                # first-seen, permanent display title
#       "machines": set[str],        # which devices have checked in
#       "last_update": float,        # timestamp
#       "message_id": int|None       # Telegram message to EDIT (single message per live)
#   }
# }
checklists = {}

app = Flask(__name__)

def _now() -> float:
    return time.time()

def _norm(s: str) -> str:
    return (s or "").strip().lower()

def _render_checklist_text(title: str, machines: set[str]) -> str:
    """
    Build a clean, two-column checklist for the 10 devices.
    Unknown devices (not in DEVICE_ORDER) appear at the bottom.
    """
    # Base devices (fixed order)
    ordered = list(DEVICE_ORDER)
    # Any extra devices not in DEVICE_ORDER appear (sorted) after
    extras = sorted([m for m in machines if m not in DEVICE_ORDER], key=str.lower)
    display = ordered + extras

    # Build cells with check mark or empty box
    cells = []
    for name in display:
        mark = "✅" if name in machines else "☐"
        cells.append(f"{mark} {name}")

    # Column formatting (2 columns) inside <pre> to keep alignment
    cols = 2
    col_width = max(len(c) for c in cells) + 2 if cells else 10
    rows = ceil(len(cells) / cols)
    lines = []
    for r in range(rows):
        left = cells[r] if r < len(cells) else ""
        right_index = r + rows
        right = cells[right_index] if right_index < len(cells) else ""
        # pad left column so right aligns nicely in monospace
        line = left.ljust(col_width) + right
        lines.append(line)

    grid = "\n".join(lines) if lines else "(no devices yet)"
    # Use HTML parse mode; wrap table in <pre> to preserve spaces
    header = f"🔴 Rollcall – {title}"
    return f"{header}\n<pre>{grid}</pre>"

def _telegram_send(text: str) -> int | None:
    """Send a new Telegram message. Return message_id or None on failure."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("ok") and "message_id" in data.get("result", {}):
            return data["result"]["message_id"]
        else:
            print("Telegram send failed payload:", data)
    except Exception as e:
        print(f"Error sending to Telegram: {e}")
    return None

def _telegram_edit(message_id: int, text: str) -> bool:
    """Edit an existing Telegram message. Return True on success."""
    url = f"https://api.telegram.org/bot{TOKEN}/editMessageText"
    payload = {
        "chat_id": CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            return True
        # Log error for visibility
        try:
            print("Edit failed:", r.status_code, r.text)
        except Exception:
            pass
    except Exception as e:
        print(f"Error editing Telegram message: {e}")
    return False

def _telegram_delete(message_id: int) -> None:
    """Best-effort delete of an old message (used only on replacement)."""
    url = f"https://api.telegram.org/bot{TOKEN}/deleteMessage"
    payload = {"chat_id": CHAT_ID, "message_id": message_id}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Delete failed (ignored): {e}")

def _update_single_message(live_key: str) -> None:
    """Ensure exactly one message per live: EDIT when possible, SEND once if needed, delete old if replaced."""
    state = checklists[live_key]
    text = _render_checklist_text(state["title"], state["machines"])

    # Try edit first if we have a message_id
    if state.get("message_id"):
        if _telegram_edit(state["message_id"], text):
            return
        # If edit fails (deleted message, etc.), fall through to send new and delete old.

    # Send new message
    old_id = state.get("message_id")
    new_id = _telegram_send(text)
    if new_id:
        state["message_id"] = new_id
        if old_id and old_id != new_id:
            _telegram_delete(old_id)

@app.route("/rollcall", methods=["POST"])
def rollcall():
    # Parse JSON
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
    # Start/refresh session if none or expired
    if (state is None) or ((now - state.get("last_update", 0)) >= SESSION_EXPIRE_SECONDS):
        state = {
            "title": username,       # permanent first-seen display name
            "machines": set(),
            "last_update": now,
            "message_id": None
        }
        checklists[live_key] = state

    # Dedupe per machine
    if machine_name in state["machines"]:
        # Still update timestamp to keep the session alive
        state["last_update"] = now
        return jsonify({"status": "duplicate"}), 200

    # Track new machine and update the single Telegram message
    state["machines"].add(machine_name)
    state["last_update"] = now
    _update_single_message(live_key)

    return jsonify({"status": "ok"}), 200

@app.route("/logout", methods=["POST"])
def logout():

    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"error": "Missing username"}), 400

    username = _norm(username)

    

    # Send a separate warning message to Telegram
    warning_text = f"⚠️ Tài khoản <b>{username}</b> bị đăng xuất."
    _telegram_send(warning_text)

    print(f"Logout warning sent for '{username}'.")
    return jsonify({"status": "logout_warning_sent"}), 200
# =================================================================


@app.route("/api/healthcheck")
def healthcheck():
    return jsonify({"status": "alive"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", PORT)))
