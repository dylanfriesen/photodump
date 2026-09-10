"""Persistent email outbox, run separately from the GPU/render worker.

Only reads completed renders from the app database. SMTP configuration is read
from a private .env file every poll, so credentials can be supplied or fixed
without restarting the app or interrupting a render.
"""
import email.policy
import fcntl
import hashlib
import logging
import mimetypes
import os
import smtplib
import sqlite3
import ssl
import time
from contextlib import closing
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

log = logging.getLogger("photodump.mail")
# Where renders are sent. Kept out of the repo: set MAIL_TO in the private
# mail .env beside the SMTP credentials, and it is reread each poll like the
# rest. Falls back to the sending account, so "mail them to myself" needs no
# extra configuration.
def recipient(conf):
    return (conf.get("MAIL_TO") or conf.get("SMTP_USER") or "").strip()
STATE = Path(os.getenv("MAIL_STATE_DIR", "/srv/mailstate"))
DATA = Path(os.getenv("DATA_DIR", "/srv/data"))
# Leave room for base64 expansion and message headers under a 25 MB limit.
PART_BYTES = 17 * 1024 * 1024


def settings(path):
    result = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                result[key.strip()] = value.strip().strip("\"'")
    return result


def init_outbox(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS deliveries (
        render_key TEXT NOT NULL, part INTEGER NOT NULL,
        filename TEXT NOT NULL, message_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
        sent_at TEXT, PRIMARY KEY(render_key, part))""")
    # A process could have died after DATA was accepted. Do not blindly resend.
    conn.execute("UPDATE deliveries SET status='uncertain', error='worker stopped during SMTP send; check Sent/mailbox before retrying' WHERE status='sending'")
    conn.commit()


def completed(db_path):
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)) as app:
        app.row_factory = sqlite3.Row
        return app.execute("""SELECT i.id, i.job_id, i.filename, i.created_at
            FROM images i JOIN jobs j ON j.id=i.job_id
            WHERE j.status='done' ORDER BY i.id""").fetchall()


def attachment_parts(path, max_bytes=PART_BYTES):
    """Keep originals byte-for-byte; oversized files travel in numbered parts."""
    size = path.stat().st_size
    if not size:
        raise ValueError("render file is empty")
    return max(1, (size + max_bytes - 1) // max_bytes)


def make_message(path, job_id, part, total, message_id, sender, to, max_bytes=PART_BYTES):
    msg = EmailMessage(policy=email.policy.SMTP)
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = f"Photodump render #{job_id}: {path.name}" + (f" (part {part}/{total})" if total > 1 else "")
    msg["Message-ID"] = message_id
    msg["Date"] = formatdate(localtime=False)
    body = f"Your Photodump render #{job_id} is finished. The render is attached.\n"
    filename = path.name
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if total > 1:
        filename += f".part{part:04d}"
        content_type = "application/octet-stream"
        body += (f"\nThe original exceeds the email attachment limit, so it is sent "
                 f"unchanged in {total} numbered parts, one email per part. "
                 "Save all parts in the same folder and concatenate them in order.\n"
                 f"Linux/macOS: cat {path.name}.part* > {path.name}\n"
                 "The complete original is also available in your Photodump gallery.\n")
    msg.set_content(body)
    with path.open("rb") as source:
        source.seek((part - 1) * max_bytes)
        payload = source.read(max_bytes)
    major, minor = content_type.split("/", 1)
    msg.add_attachment(payload, maintype=major, subtype=minor, filename=filename)
    return msg


def send(msg, conf, before_data):
    host = conf.get("SMTP_HOST", "smtp.gmail.com")
    mode = conf.get("SMTP_SECURITY", "starttls")
    if mode not in ("starttls", "ssl"):
        raise ValueError("SMTP_SECURITY must be starttls or ssl")
    port = int(conf.get("SMTP_PORT") or (465 if mode == "ssl" else 587))
    ctx = ssl.create_default_context()
    client = (smtplib.SMTP_SSL(host, port, timeout=60, context=ctx)
              if mode == "ssl" else smtplib.SMTP(host, port, timeout=60))
    try:
        if mode == "starttls":
            client.starttls(context=ctx)
        client.login(conf["SMTP_USER"], conf["SMTP_PASS"])
        before_data()
        rejected = client.send_message(msg, from_addr=msg["From"], to_addrs=[msg["To"]])
        if rejected:
            raise smtplib.SMTPRecipientsRefused(rejected)
    finally:
        # A disconnect during QUIT does not invalidate an accepted message.
        client.close()


def tick(conn, data, conf, sender=send, max_bytes=PART_BYTES):
    ready = bool(conf.get("SMTP_USER") and conf.get("SMTP_PASS") and recipient(conf))
    for render in completed(data / "photodump.db"):
        path = (data / "out" / render["filename"]).resolve()
        if not path.is_relative_to((data / "out").resolve()) or not path.is_file():
            continue  # deleted renders are not sent
        key = hashlib.sha256(f"{render['id']}:{render['created_at']}:{render['filename']}".encode()).hexdigest()
        try:
            total = attachment_parts(path, max_bytes)
        except (OSError, ValueError):
            log.warning("render %s file unavailable; will retry", render['job_id'])
            continue
        for part in range(1, total + 1):
            message_id = f"<photodump.{key}.{part}@photodump.local>"
            conn.execute("INSERT OR IGNORE INTO deliveries(render_key,part,filename,message_id) VALUES (?,?,?,?)",
                         (key, part, path.name, message_id))
            conn.commit()
            row = conn.execute("SELECT status,attempts,next_attempt FROM deliveries WHERE render_key=? AND part=?", (key, part)).fetchone()
            if not ready or row[0] != "pending" or row[2] > time.time():
                continue
            in_data = False

            def before_data():
                nonlocal in_data
                conn.execute("UPDATE deliveries SET status='sending' WHERE render_key=? AND part=?", (key, part))
                conn.commit()
                in_data = True

            try:
                msg = make_message(path, render["job_id"], part, total, message_id,
                                   conf.get("SMTP_FROM") or conf["SMTP_USER"],
                                   recipient(conf), max_bytes)
                sender(msg, conf, before_data)
            except Exception as exc:
                # Never log provider responses or credentials. Refused SMTP
                # commands are safe to retry; a lost final ACK is ambiguous.
                uncertain = in_data and not isinstance(exc, (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused))
                status = "uncertain" if uncertain else "pending"
                delay = min(3600, 30 * 2 ** min(row[1], 7))
                error = type(exc).__name__
                conn.execute("UPDATE deliveries SET status=?,attempts=attempts+1,next_attempt=?,error=? WHERE render_key=? AND part=?",
                             (status, time.time() + delay, error, key, part))
                log.warning("render %s part %s: %s (%s)", render["job_id"], part, status, error)
            else:
                conn.execute("UPDATE deliveries SET status='sent',attempts=attempts+1,error='',sent_at=datetime('now') WHERE render_key=? AND part=?", (key, part))
                log.info("render %s part %s/%s accepted by SMTP for %s", render["job_id"], part, total, msg["To"])
            conn.commit()
    return ready


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    STATE.mkdir(parents=True, exist_ok=True)
    # One worker owns this outbox, including across accidental duplicate starts.
    lock = (STATE / "worker.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    conn = sqlite3.connect(STATE / "outbox.sqlite3")
    init_outbox(conn)
    last_status = None
    while True:
        try:
            ready = tick(conn, DATA, settings(STATE / ".env"))
            status = "SMTP configured; watching completed renders" if ready else "waiting for SMTP_USER, SMTP_PASS and MAIL_TO in private mail .env; completed renders remain queued"
        except Exception as exc:
            status = f"poll will retry: {type(exc).__name__}"
        if status != last_status:
            log.info(status)
            last_status = status
        time.sleep(15)


if __name__ == "__main__":
    main()
