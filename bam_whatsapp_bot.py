"""
BAM farmer registration bot for the WhatsApp Business Cloud API (Render-ready).
Asks the nine questions one at a time inside the chat; stores answers in Postgres.

Environment variables:
  WA_TOKEN       permanent System User access token
  WA_PHONE_ID    WhatsApp phone number ID
  VERIFY_TOKEN   any secret string you choose (also entered in Meta's webhook setup)
  DATABASE_URL   Render Postgres "Internal Database URL"
  APP_SECRET     (optional) Meta app secret, enables webhook signature checking
  EXPORT_KEY     (optional) secret for downloading registrations at /export.csv?key=...

Start command on Render:  gunicorn bam_whatsapp_bot:app
"""
import csv, hashlib, hmac, io, json, os, re
import psycopg2
import requests
from flask import Flask, Response, request

WA_TOKEN = os.environ["WA_TOKEN"]
WA_PHONE_ID = os.environ["WA_PHONE_ID"]
VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
APP_SECRET = os.environ.get("APP_SECRET")
EXPORT_KEY = os.environ.get("EXPORT_KEY")
API = f"https://graph.facebook.com/v21.0/{WA_PHONE_ID}/messages"

REGIONS = ["Central", "Chobe", "Ghanzi", "Kgalagadi", "Kgatleng", "Kweneng",
           "North-East", "North-West", "South-East", "Southern"]
ACTIVITIES = ["Crop farming", "Livestock farming", "Mixed farming", "Poultry",
              "Horticulture/Vegetables", "Other"]

# (field, kind, question)
STEPS = [
    ("full_name", "text", "Welcome to BAM! 🌱\nWhat is your *name and surname*?"),
    ("region", "list:" + "|".join(REGIONS), "Which *region* are you in?"),
    ("village_town", "text", "Which *village or town*?"),
    ("phone", "phone", "Should we use this WhatsApp number as your *phone number*?"),
    ("farming_activity", "list:" + "|".join(ACTIVITIES), "What is your *farming activity*?"),
    ("crop_livestock_type", "text", "What *crop or livestock* do you farm? (e.g. maize, goats, cattle)"),
    ("farm_size", "number", "What is your *farm size* in hectares? (numbers only, e.g. 5)"),
    ("irrigation_required", "yesno", "Do you need *irrigation*?"),
    ("bulk_buying", "yesno", "Are you *interested in bulk buying*?"),
]
FIELDS = [s[0] for s in STEPS]

app = Flask(__name__)


# ---------- storage (Postgres) ----------
def run(sql, params=(), fetch=False):
    con = psycopg2.connect(DATABASE_URL)
    try:
        with con, con.cursor() as cur:
            cur.execute(sql, params)
            if fetch:
                return cur.fetchall()
            return cur.rowcount
    finally:
        con.close()


def init_db():
    run("""CREATE TABLE IF NOT EXISTS sessions (
        wa_id TEXT PRIMARY KEY, step INTEGER, answers TEXT)""")
    run("""CREATE TABLE IF NOT EXISTS registrations (
        id SERIAL PRIMARY KEY, wa_id TEXT, answers TEXT,
        created_at TIMESTAMPTZ DEFAULT now())""")
    run("""CREATE TABLE IF NOT EXISTS seen_messages (
        id TEXT PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT now())""")


def get_session(wa_id):
    rows = run("SELECT step, answers FROM sessions WHERE wa_id=%s", (wa_id,), fetch=True)
    return (rows[0][0], json.loads(rows[0][1])) if rows else (None, {})


def save_session(wa_id, step, answers):
    run("""INSERT INTO sessions (wa_id, step, answers) VALUES (%s,%s,%s)
           ON CONFLICT (wa_id) DO UPDATE SET step=EXCLUDED.step, answers=EXCLUDED.answers""",
        (wa_id, step, json.dumps(answers)))


def finish(wa_id, answers):
    run("INSERT INTO registrations (wa_id, answers) VALUES (%s,%s)", (wa_id, json.dumps(answers)))
    run("DELETE FROM sessions WHERE wa_id=%s", (wa_id,))


def first_time_seen(msg_id):
    """WhatsApp retries webhooks; process each message id only once."""
    return run("INSERT INTO seen_messages (id) VALUES (%s) ON CONFLICT DO NOTHING", (msg_id,)) == 1


# ---------- sending ----------
def send(payload):
    requests.post(API, headers={"Authorization": f"Bearer {WA_TOKEN}"},
                  json={"messaging_product": "whatsapp", **payload}, timeout=15)


def send_text(to, body):
    send({"to": to, "type": "text", "text": {"body": body}})


def send_buttons(to, body, buttons):  # max 3 buttons, titles up to 20 chars
    send({"to": to, "type": "interactive", "interactive": {
        "type": "button", "body": {"text": body},
        "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t}} for i, t in buttons]}}})


def send_list(to, body, options):  # max 10 rows, titles up to 24 chars
    send({"to": to, "type": "interactive", "interactive": {
        "type": "list", "body": {"text": body},
        "action": {"button": "Choose", "sections": [
            {"title": "Options", "rows": [{"id": o, "title": o} for o in options]}]}}})


def ask(to, step):
    field, kind, question = STEPS[step]
    if kind.startswith("list:"):
        send_list(to, question, kind[5:].split("|"))
    elif kind == "yesno":
        send_buttons(to, question, [("Yes", "Yes"), ("No", "No")])
    elif kind == "phone":
        send_buttons(to, question, [("use_this", "Yes, use this"), ("other", "Enter another")])
    else:
        send_text(to, question)


# ---------- validation ----------
def validate(kind, text):
    """Returns (ok, cleaned_value, error_message)."""
    t = text.strip()
    if kind == "text":
        return (len(t) >= 2, t, "Please type your answer.")
    if kind == "number":
        try:
            n = float(t.replace(",", "."))
            return (n > 0, n, "Please send a number greater than 0, e.g. 5")
        except ValueError:
            return (False, None, "Please send a number greater than 0, e.g. 5")
    if kind == "phone_text":
        digits = re.sub(r"[\s-]", "", t)
        return (bool(re.fullmatch(r"(\+?267)?\d{7,8}", digits)), digits,
                "Please send a valid number, e.g. 71234567")
    if kind.startswith("list:"):
        opts = kind[5:].split("|")
        match = next((o for o in opts if o.lower() == t.lower()), None)
        return (match is not None, match, "Please tap *Choose* and pick from the list.")
    if kind == "yesno":
        v = {"yes": "Yes", "no": "No"}.get(t.lower())
        return (v is not None, v, "Please tap Yes or No.")
    return (False, None, "Sorry, I didn't understand that.")


# ---------- conversation logic ----------
def handle(wa_id, text):
    step, answers = get_session(wa_id)

    if text.lower() in ("restart", "start", "register", "hi", "hello", "dumela") or step is None:
        save_session(wa_id, 0, {})
        return ask(wa_id, 0)

    field, kind, _ = STEPS[step]
    awaiting_other_phone = answers.pop("_awaiting_phone", False)

    if kind == "phone" and not awaiting_other_phone:
        if text == "use_this":
            answers[field] = wa_id
        elif text == "other":
            answers["_awaiting_phone"] = True
            save_session(wa_id, step, answers)
            return send_text(wa_id, "Please type your phone number, e.g. 71234567")
        else:
            return ask(wa_id, step)
    else:
        ok, value, err = validate("phone_text" if kind == "phone" else kind, text)
        if not ok:
            if kind == "phone":
                answers["_awaiting_phone"] = True
            save_session(wa_id, step, answers)
            return send_text(wa_id, err)
        answers[field] = value

    step += 1
    if step < len(STEPS):
        save_session(wa_id, step, answers)
        return ask(wa_id, step)

    finish(wa_id, answers)
    send_text(wa_id, "✅ Thank you, " + answers["full_name"].split()[0] +
              "! You are now registered with BAM. We will contact you soon.\n\n"
              "To register again, send *restart*.")


# ---------- webhook ----------
def signature_ok():
    if not APP_SECRET:
        return True
    sig = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(APP_SECRET.encode(), request.get_data(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


@app.get("/")
def health():
    return "BAM bot is running", 200


@app.get("/webhook")
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge", ""), 200
    return "Forbidden", 403


@app.post("/webhook")
def incoming():
    if not signature_ok():
        return "Forbidden", 403
    data = request.get_json(silent=True) or {}
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                if not first_time_seen(msg["id"]):
                    continue
                wa_id = msg["from"]
                if msg["type"] == "text":
                    text = msg["text"]["body"]
                elif msg["type"] == "interactive":
                    i = msg["interactive"]
                    text = (i.get("button_reply") or i.get("list_reply"))["id"]
                else:
                    send_text(wa_id, "Please reply with text or tap one of the options.")
                    continue
                handle(wa_id, text)
    return "ok", 200


@app.get("/export.csv")
def export():
    if not EXPORT_KEY or request.args.get("key") != EXPORT_KEY:
        return "Forbidden", 403
    rows = run("SELECT wa_id, answers, created_at FROM registrations ORDER BY id", fetch=True)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["whatsapp_id"] + FIELDS + ["registered_at"])
    for wa_id, answers, created in rows:
        a = json.loads(answers)
        w.writerow([wa_id] + [a.get(f, "") for f in FIELDS] + [created.isoformat()])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=bam_registrations.csv"})


init_db()

if __name__ == "__main__":
    app.run(port=5000)
