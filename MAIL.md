# Render email

The independent mail worker emails every saved output of completed jobs to
the address in `MAIL_TO`, including existing renders, stills, animations,
reels, and delivery encodes. It does not restart or write to the render worker.

Configure `data/mail/.env` (gitignored, permissions 600):

```dotenv
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USER=
SMTP_PASS=
# Where renders are sent. Defaults to SMTP_USER if left blank.
MAIL_TO=
# Optional sender override; otherwise SMTP_USER is used.
SMTP_FROM=
```

For Gmail, use the sending account's app password. Credentials are reread every
15 seconds. No restart is needed after editing this file.

Start with `docker compose -f compose.mail.yaml up -d --build`. This service
publishes no ports and joins only the default network, not `web`.
Watch `docker logs --tail 30 photodump-mailer`.

`data/mail/outbox.sqlite3` records delivery for each output/part. Ordinary SMTP
failures retry with backoff up to one hour; accepted messages are not resent on
restart. If a connection dies during sending, delivery can be ambiguous: the
record is marked `uncertain` for manual checking before retrying. This avoids
silently sending duplicates. SMTP acceptance does not prove inbox placement.

Original files up to 17 MiB are attached directly. Larger originals are emailed
unchanged in numbered 17 MiB parts, with reassembly instructions, to fit the
encoded message size limit. The complete file remains in the gallery.

The smoke test's separate data directory is not watched. Deleted files and
outputs whose job has not completed are never sent.
