"""Outbox behavior without sending mail or touching live render data."""
import smtplib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app import mailer


class MailerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        (self.data / 'out').mkdir()
        self.payload = b'render-content'
        (self.data / 'out' / 'clip.mp4').write_bytes(self.payload)
        with closing(sqlite3.connect(self.data / 'photodump.db')) as app:
            app.executescript("""
                CREATE TABLE jobs(id INTEGER, status TEXT);
                CREATE TABLE images(id INTEGER, job_id INTEGER, filename TEXT, created_at TEXT);
                INSERT INTO jobs VALUES(6, 'running');
                INSERT INTO images VALUES(1, 6, 'clip.mp4', 'today');
            """)
        self.conn = sqlite3.connect(self.data / 'outbox.sqlite3')
        self.addCleanup(self.conn.close)
        mailer.init_outbox(self.conn)
        self.conf = {'SMTP_USER': 'sender@example.com', 'SMTP_PASS': 'test-only'}
        self.sent = []

    def finish(self):
        with closing(sqlite3.connect(self.data / 'photodump.db')) as app:
            app.execute("UPDATE jobs SET status='done'")
            app.commit()

    def send(self, msg, conf, before):
        before()
        self.sent.append(msg)

    def test_completion_attachment_recipient_and_restart(self):
        mailer.tick(self.conn, self.data, self.conf, self.send)
        self.assertEqual(self.sent, [])
        self.finish()
        mailer.tick(self.conn, self.data, self.conf, self.send)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(str(self.sent[0]['To']), mailer.recipient(self.conf))
        self.assertIsNone(self.sent[0]['Cc'])
        self.assertEqual(next(self.sent[0].iter_attachments()).get_payload(decode=True), self.payload)
        mailer.init_outbox(self.conn)
        mailer.tick(self.conn, self.data, self.conf, self.send)
        self.assertEqual(len(self.sent), 1)

    def test_missing_credentials_keep_backlog(self):
        self.finish()
        self.assertFalse(mailer.tick(self.conn, self.data, {}, self.send))
        self.assertEqual(self.conn.execute('SELECT status FROM deliveries').fetchone()[0], 'pending')
        mailer.tick(self.conn, self.data, self.conf, self.send)
        self.assertEqual(len(self.sent), 1)

    def test_smtp_rejection_retries_without_rerender(self):
        self.finish()
        def rejected(msg, conf, before):
            before()
            raise smtplib.SMTPDataError(451, b'temporary failure')
        mailer.tick(self.conn, self.data, self.conf, rejected)
        self.assertEqual(self.conn.execute('SELECT status,attempts FROM deliveries').fetchone(), ('pending', 1))
        self.conn.execute('UPDATE deliveries SET next_attempt=0')
        mailer.tick(self.conn, self.data, self.conf, self.send)
        self.assertEqual(len(self.sent), 1)

    def test_lost_smtp_ack_does_not_silently_duplicate(self):
        self.finish()
        def lost(msg, conf, before):
            before()
            raise ConnectionResetError()
        mailer.tick(self.conn, self.data, self.conf, lost)
        mailer.tick(self.conn, self.data, self.conf, self.send)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.conn.execute('SELECT status FROM deliveries').fetchone()[0], 'uncertain')

    def test_oversize_attachment_parts_reassemble_exactly(self):
        self.finish()
        mailer.tick(self.conn, self.data, self.conf, self.send, max_bytes=5)
        chunks = [next(msg.iter_attachments()).get_payload(decode=True) for msg in self.sent]
        self.assertEqual(b''.join(chunks), self.payload)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(len({m['Message-ID'] for m in self.sent}), 3)


if __name__ == '__main__':
    unittest.main()


class BatchTests(unittest.TestCase):
    """One email per request, and never a second copy of what already went."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        (self.data / 'out').mkdir()
        self.conf = {'SMTP_USER': 'sender@example.com', 'SMTP_PASS': 'test-only',
                     'MAIL_TO': 'me@example.com'}
        self.sent = []

    def send(self, msg, conf, before):
        before()
        self.sent.append(msg)

    def build(self, jobs):
        """jobs: (job_id, batch_id, status, filename or None)"""
        with closing(sqlite3.connect(self.data / 'photodump.db')) as app:
            app.executescript("""
                CREATE TABLE IF NOT EXISTS jobs(id INTEGER, status TEXT, batch_id TEXT);
                CREATE TABLE IF NOT EXISTS images(id INTEGER, job_id INTEGER,
                                                  filename TEXT, created_at TEXT);
            """)
            n = 0
            for job_id, batch, status, name in jobs:
                app.execute('INSERT INTO jobs VALUES(?,?,?)', (job_id, status, batch))
                if name:
                    n += 1
                    (self.data / 'out' / name).write_bytes(b'x' * 1024)
                    app.execute('INSERT INTO images VALUES(?,?,?,?)',
                                (n, job_id, name, 'today'))
            app.commit()
        conn = sqlite3.connect(self.data / 'outbox.sqlite3')
        self.addCleanup(conn.close)
        mailer.init_outbox(conn)
        return conn

    def test_one_request_sends_one_email(self):
        # count=N creates N single-image jobs sharing a batch id.
        conn = self.build([(1, 'B', 'done', 'a.png'),
                           (2, 'B', 'done', 'b.png'),
                           (3, 'B', 'done', 'c.png')])
        mailer.tick(conn, self.data, self.conf, self.send)
        self.assertEqual(len(self.sent), 1)
        names = sorted(a.get_filename() for a in self.sent[0].iter_attachments())
        self.assertEqual(names, ['a.png', 'b.png', 'c.png'])

    def test_batch_waits_for_the_whole_request(self):
        # Sending as soon as the first job lands would defeat the batching.
        conn = self.build([(1, 'B', 'done', 'a.png'),
                           (2, 'B', 'done', 'b.png'),
                           (3, 'B', 'running', None)])
        mailer.tick(conn, self.data, self.conf, self.send)
        self.assertEqual(self.sent, [])

    def test_already_delivered_files_are_not_resent(self):
        # Regression: changing the per-file delivery key once made every
        # previously sent render look new and remailed the whole gallery.
        conn = self.build([(1, '', 'done', 'a.png')])
        mailer.tick(conn, self.data, self.conf, self.send)
        self.assertEqual(len(self.sent), 1)
        self.sent.clear()
        mailer.tick(conn, self.data, self.conf, self.send)
        self.assertEqual(self.sent, [], 'a delivered render was sent twice')
