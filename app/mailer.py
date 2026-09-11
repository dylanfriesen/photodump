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
    """Finished renders, plus which batches still have work outstanding.

    A batch is one /api/generate request; count=N makes N single-image jobs.
    Sending as soon as the first finishes would defeat the point, so a batch is
    held until none of its jobs are still in flight.
    """
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)) as app:
        app.row_factory = sqlite3.Row
        try:
            rows = app.execute("""SELECT i.id, i.job_id, i.filename, i.created_at,
                       COALESCE(j.batch_id,'') AS batch_id
                FROM images i JOIN jobs j ON j.id=i.job_id
                WHERE j.status='done' ORDER BY i.id""").fetchall()
            busy = {r[0] for r in app.execute(
                "SELECT DISTINCT batch_id FROM jobs WHERE batch_id<>'' "
                "AND status IN ('queued','running','paused')").fetchall()}
        except sqlite3.OperationalError:
            # Older database without batch_id; fall back to per-job grouping.
            rows = app.execute("""SELECT i.id, i.job_id, i.filename, i.created_at,
                       '' AS batch_id
                FROM images i JOIN jobs j ON j.id=i.job_id
                WHERE j.status='done' ORDER BY i.id""").fetchall()
            busy = set()
        return rows, busy


def attachment_parts(path, max_bytes=PART_BYTES):
    """Keep originals byte-for-byte; oversized files travel in numbered parts."""
    size = path.stat().st_size
    if not size:
        raise ValueError("render file is empty")
    return max(1, (size + max_bytes - 1) // max_bytes)


def batch_key(job_id, paths, created):
    """Stable id for one job's whole set of renders."""
    names = ":".join(sorted(p.name for p in paths))
    return hashlib.sha256(f"job:{job_id}:{created}:{names}".encode()).hexdigest()


def already_sent_individually(conn, paths):
    """True if every file here was delivered under the old per-file scheme.

    Without this, moving to one-email-per-job would make every previously
    delivered render look new and send the whole gallery again.
    """
    if not paths:
        return False
    for path in paths:
        row = conn.execute(
            "SELECT 1 FROM deliveries WHERE filename=? AND status='sent' LIMIT 1",
            (path.name,)).fetchone()
        if not row:
            return False
    return True


def make_batch_message(paths, job_id, message_id, sender, to):
    """One email carrying every render from a single job."""
    msg = EmailMessage(policy=email.policy.SMTP)
    msg["From"] = sender
    msg["To"] = to
    n = len(paths)
    # job_id is a short batch hash for grouped requests, which reads as noise in
    # a subject line; the file count is what is useful there.
    msg["Subject"] = f"Photodump: {n} render{'s' if n != 1 else ''} ready"
    msg["Message-ID"] = message_id
    msg["Date"] = formatdate(localtime=False)
    msg.set_content(
        f"Your Photodump render #{job_id} is finished — {n} file"
        f"{'s' if n != 1 else ''} attached.\n\n"
        + "".join(f"  {p.name}\n" for p in sorted(paths, key=lambda x: x.name)))
    for path in sorted(paths, key=lambda x: x.name):
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        major, minor = ctype.split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=major, subtype=minor,
                           filename=path.name)
    return msg


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


def _deliver(conn, key, part, total, job_id, conf, sender, row, build):
    """Send one message and record the outcome. Shared by both routes.

    Kept in one place so the retry semantics cannot drift: a refused SMTP
    command is safe to retry, but an error after DATA was accepted is
    ambiguous and must not be resent blindly.
    """
    in_data = False

    def before_data():
        nonlocal in_data
        conn.execute("UPDATE deliveries SET status='sending' WHERE render_key=? AND part=?",
                     (key, part))
        conn.commit()
        in_data = True

    try:
        msg = build(f"<photodump.{key}.{part}@photodump.local>",
                    conf.get("SMTP_FROM") or conf["SMTP_USER"], recipient(conf))
        sender(msg, conf, before_data)
    except Exception as exc:
        # Never log provider responses or credentials.
        uncertain = in_data and not isinstance(
            exc, (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused))
        status = "uncertain" if uncertain else "pending"
        delay = min(3600, 30 * 2 ** min(row[1], 7))
        conn.execute("UPDATE deliveries SET status=?,attempts=attempts+1,next_attempt=?,error=? "
                     "WHERE render_key=? AND part=?",
                     (status, time.time() + delay, type(exc).__name__, key, part))
        log.warning("render %s part %s: %s (%s)", job_id, part, status, type(exc).__name__)
    else:
        conn.execute("UPDATE deliveries SET status='sent',attempts=attempts+1,error='',"
                     "sent_at=datetime('now') WHERE render_key=? AND part=?", (key, part))
        log.info("render %s part %s/%s accepted by SMTP for %s", job_id, part, total, msg["To"])
    conn.commit()


def tick(conn, data, conf, sender=send, max_bytes=PART_BYTES):
    ready = bool(conf.get("SMTP_USER") and conf.get("SMTP_PASS") and recipient(conf))
    out = (data / "out").resolve()

    # One email per job rather than per image. Files are grouped by job, and a
    # job whose renders all fit under the attachment limit travels as a single
    # message; anything oversized falls back to the old per-file path, which
    # still chunks a large video across numbered parts.
    rows, busy = completed(data / "photodump.db")
    jobs = {}
    for render in rows:
        path = (data / "out" / render["filename"]).resolve()
        if not path.is_relative_to(out) or not path.is_file():
            continue  # deleted renders are not sent
        batch = render["batch_id"]
        if batch and batch in busy:
            continue   # the rest of this request is still rendering
        # Group by request where one exists, else by job as before.
        gid = f"batch:{batch}" if batch else f"job:{render['job_id']}"
        jobs.setdefault(gid, {"created": render["created_at"], "paths": [],
                              "label": batch[:8] if batch else render["job_id"]})
        # image id is kept because the per-file delivery key is hashed from it;
        # substituting anything else makes every already-sent file look new.
        jobs[gid]["paths"].append(path)
        jobs[gid].setdefault("ids", {})[path] = (render["id"], render["job_id"])

    singles = []
    for gid, info in jobs.items():
        job_id = info["label"]
        paths = info["paths"]
        try:
            size = sum(p.stat().st_size for p in paths)
        except OSError:
            log.warning("render %s file unavailable; will retry", job_id)
            continue
        if len(paths) > 1 and size <= max_bytes:
            key = batch_key(job_id, paths, info["created"])
            message_id = f"<photodump.{key}.1@photodump.local>"
            conn.execute("INSERT OR IGNORE INTO deliveries(render_key,part,filename,message_id) "
                         "VALUES (?,?,?,?)",
                         (key, 1, f"{len(paths)} files from job {job_id}", message_id))
            conn.commit()
            row = conn.execute("SELECT status,attempts,next_attempt FROM deliveries "
                               "WHERE render_key=? AND part=1", (key,)).fetchone()
            if row[0] == "pending" and already_sent_individually(conn, paths):
                conn.execute("UPDATE deliveries SET status='sent',error='already delivered individually',"
                             "sent_at=datetime('now') WHERE render_key=? AND part=1", (key,))
                conn.commit()
                continue
            if not ready or row[0] != "pending" or row[2] > time.time():
                continue
            _deliver(conn, key, 1, 1, job_id, conf, sender, row,
                     lambda mid, frm, to: make_batch_message(paths, job_id, mid, frm, to))
        else:
            singles.extend((info["ids"][p][1], info["ids"][p][0], info["created"], p)
                           for p in paths)

    for job_id, image_id, created, path in singles:
        render = {"job_id": job_id, "created_at": created, "id": image_id}
        # Identical to the original formula - do not change it without a
        # migration, or every delivered render is sent again.
        key = hashlib.sha256(f"{image_id}:{created}:{path.name}".encode()).hexdigest()
        try:
            total = attachment_parts(path, max_bytes)
        except (OSError, ValueError):
            log.warning("render %s file unavailable; will retry", job_id)
            continue
        for part in range(1, total + 1):
            message_id = f"<photodump.{key}.{part}@photodump.local>"
            conn.execute("INSERT OR IGNORE INTO deliveries(render_key,part,filename,message_id) VALUES (?,?,?,?)",
                         (key, part, path.name, message_id))
            conn.commit()
            row = conn.execute("SELECT status,attempts,next_attempt FROM deliveries WHERE render_key=? AND part=?", (key, part)).fetchone()
            if not ready or row[0] != "pending" or row[2] > time.time():
                continue
            _deliver(conn, key, part, total, render["job_id"], conf, sender, row,
                     lambda mid, frm, to: make_message(path, render["job_id"], part, total,
                                                       mid, frm, to, max_bytes))
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
