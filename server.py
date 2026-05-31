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
        cells.append((mark, name))

    # Two-column layout with fixed width and text wrapping
    COL_WIDTH = 14  # Max chars per column (adjust for mobile)
    cols = 2
    rows = ceil(len(cells) / cols)
    
    def wrap_cell(mark: str, name: str, width: int) -> list[str]:
        """Wrap a cell into multiple lines, mark on first line only."""
        # First line has the mark
        first_line_space = width - 2  # "✅ " takes ~2 chars visually
        result = []
        if len(name) <= first_line_space:
            result.append(f"{mark} {name}")
        else:
            # Wrap the name
            result.append(f"{mark} {name[:first_line_space]}")
            remaining = name[first_line_space:]
            while remaining:
                chunk = remaining[:width]
                result.append(f"   {chunk}")  # Indent continuation
                remaining = remaining[width:]
        return result
    
    output_lines = []
    for r in range(rows):
        left_idx = r
        right_idx = r + rows
        
        left_lines = wrap_cell(*cells[left_idx], COL_WIDTH) if left_idx < len(cells) else [""]
        right_lines = wrap_cell(*cells[right_idx], COL_WIDTH) if right_idx < len(cells) else [""]
        
        # Combine left and right, padding to align columns
        max_lines = max(len(left_lines), len(right_lines))
        for i in range(max_lines):
            left = left_lines[i] if i < len(left_lines) else ""
            right = right_lines[i] if i < len(right_lines) else ""
            output_lines.append(f"{left:<{COL_WIDTH + 2}}{right}")

    grid = "\n".join(output_lines) if output_lines else "(no devices yet)"
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

@app.route("/banned", methods=["POST"])
def banned():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    # We don't strictly need a username, but it helps if the extension sends it
    # If not sent, we can just say "A bot was banned"
    username = (data.get("username") or "").strip()
    
    
    # Send a critical warning message to Telegram
    ban_text = f"🚨Tài khoản <b>{username}</b> đã bị cấm dùng."
    _telegram_send(ban_text)

    print(f"BAN warning sent for '{username}'.")
    return jsonify({"status": "ban_warning_sent"}), 200

@app.route("/code", methods=["POST"])
def code_endpoint():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    website = (data.get("website") or "").strip()
    is_batch = data.get("batch", False)

    # Batch mode: multiple codes in one notification
    if is_batch and data.get("codes"):
        codes = data["codes"]
        
        # Pick the right redeem URL
        if website == "C168_TG":
            redeem_url = "https://nhapma-c168.pages.dev/"
            redeem_label = "C168"
        elif website == "SC88_TG":
            redeem_url = "https://nhap-code-sc88.pages.dev/"
            redeem_label = "SC88"
        elif website == "F168":
            redeem_url = "https://f168km.info/"
            redeem_label = "F168"
        elif website == "FLY88":
            redeem_url = "https://fly88code.com/"
            redeem_label = "FLY88"
        else:
            redeem_url = ""
            redeem_label = website
        # Build code list - split into 2 columns for C168_TG and SC88_TG to be nicer
        if website in ["C168_TG", "SC88_TG"]:
            code_lines = []
            for i in range(0, len(codes), 2):
                if i + 1 < len(codes):
                    code_lines.append(f"<code>{codes[i]}</code>      <code>{codes[i+1]}</code>")
                else:
                    code_lines.append(f"<code>{codes[i]}</code>")
        else:
            code_lines = [f"<code>{c}</code>" for c in codes]
        
        # Telegram messages max 4096 chars, split if needed
        header = f"🎁 <b>{len(codes)} CODE MỚI</b> ({redeem_label})\n"
        if redeem_url:
            footer = f"\n📍 WEB: <a href='{redeem_url}'>{redeem_label}</a>"
        else:
            footer = f"\n📍 WEB: {redeem_label}"
        
        # Send in chunks to stay under Telegram limit
        chunk_size = 50  # codes per message
        for i in range(0, len(code_lines), chunk_size):
            chunk = code_lines[i:i + chunk_size]
            chunk_num = f" ({i // chunk_size + 1})" if len(codes) > chunk_size else ""
            code_text = header.replace(")", f"){chunk_num}") if chunk_num else header
            code_text += "\n".join(chunk)
            code_text += footer
            _telegram_send(code_text)
        
        print(f"Batch code notification sent: {len(codes)} codes for {website}")
        return jsonify({"status": "batch_sent", "count": len(codes)}), 200

    # Single code mode (F168, FLY88, etc.)
    code = (data.get("code") or "").strip()
    
    if website == "F168":
        code_text = f"🎁 CODE MỚI: <code>{code}</code>\n📍 WEB: <a href='https://f168km.info/'>F168</a>"
    elif website == "FLY88":
        code_text = f"🎁 CODE MỚI: <code>{code}</code>\n📍 WEB: <a href='https://fly88code.com/'>FLY88</a>"
    elif website == "C168_TG":
        code_text = f"🎁 CODE MỚI: <code>{code}</code>\n📍 WEB: <a href='https://nhapma-c168.pages.dev/'>C168</a>"
    elif website in ["SC88_TG", "SC88"]:
        code_text = f"🎁 CODE MỚI: <code>{code}</code>\n📍 WEB: <a href='https://nhap-code-sc88.pages.dev/'>SC88</a>"
    else:
        code_text = f"🎁 CODE MỚI: <code>{code}</code>\n📍 WEB: {website}"

    _telegram_send(code_text)

    print(f"Code notification sent: '{code}'")
    return jsonify({"status": "code_sent"}), 200
# =================================================================

@app.route("/winner", methods=["POST"])
def winner():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    username = (data.get("username") or "").strip()
    money = (data.get("money") or "").strip()

    if not username or not money:
        return jsonify({"error": "Missing username or money"}), 400

    username = _norm(username)
    money = _norm(money)

    # Build winner message
    winner_text = f"💲 Tài khoản <b>{username}</b> trúng thưởng <b>{money}</b>."
    _telegram_send(winner_text)

    print(f"Winner message sent for '{username}' with prize '{money}'.")
    return jsonify({"status": "winner_sent"}), 200


@app.route("/api/healthcheck")
def healthcheck():
    return jsonify({"status": "alive"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", PORT)))
