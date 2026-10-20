# CaseSifter data flow and retention

This is the record an auditor should compare to the running configuration.
The public summary is [marketing/security.html](../marketing/security.html).

## Who holds what

| Data | Where it lives | How long | Who can read it |
| --- | --- | --- | --- |
| Account email, argon2id password hash, session id | Control-plane Postgres (`cp_users`, `cp_sessions`) | Until the account is removed. Sessions expire after `SESSION_TTL_DAYS` (30) or logout. | That user, via their session. Operators with database access. |
| Session cookie `cs_session` | Browser, host-only, `HttpOnly` | Same TTL. Not readable by page script. | Sent only to the host that set it (control plane, and separately the analysis API). |
| Uploaded exhibit bytes | Analysis job dir, or R2 when `BLOB_BACKEND=s3` | Deleted when the scan finishes (`SOURCE_FILE_RETENTION=immediate`). Disk leftovers are swept after `EPHEMERAL_TTL_HOURS`. | The scan worker for that job. Not kept as a document library. |
| Encrypted facts, findings, short quotes, report JSON | Postgres fact memory, per-firm Fernet key | Until the firm deletes the matter or the firm is purged. | Members of that firm. Another firm's queries return not found. |
| Firm data key | `firms.wrapped_data_key`, wrapped by `FACT_MEMORY_MASTER_KEY` | Same as the firm row. | Unreadable without the master key. |
| Access events | `audit_log` (analysis) and `cp_auth_audit` (sign-in, admin trial grant) | Until the firm row is purged (analysis) or the auth database is removed. | Firm-scoped reads return only that firm's rows. Events store actor, action, target id, time, and request id. They do not store file bytes, passwords, or session tokens. |
| Gemini | Google, only when Tier 3 runs | Deleted after the call; Google's abuse logs can last up to 48 hours if CaseSifter's delete fails. | Google, under paid API terms. Not used to train their models. |
| Claude | Anthropic, short claim and evidence quotes only | Commercial API retention, not the whole file. | Anthropic, under commercial API terms. |
| Sentry | Error events after scrubbing | Per the Sentry project retention. | Operators of that Sentry project. Document bytes and paths are scrubbed. |
| Stripe | Customer, subscription, seat count | Stripe's retention for the billing account. | Stripe and the firm's billing admin. |

The browser keeps the lasting PDF. CaseSifter does not.

## Tier 3 (native file to Gemini)

`contextual_multimodal` defaults to **false** in [backend/app/config.py](../backend/app/config.py).

The hosted Render service sets `CONTEXTUAL_MULTIMODAL=true` in [render.yaml](../render.yaml). When that is on, a non-court-filing exhibit that the triage marks for a full visual review is sent to Gemini. Court filings are skipped (`skipped_court_filing`) and the file is not sent.

Do not upload sealed or privileged evidence to the hosted service unless the court's rules allow that transfer. Turning the env var off stops native-file upload for every job on that service.

## Source-file retention check

When `EPHEMERAL_JOBS` is true, `delete_ephemeral_source` removes the uploaded file and leaves only progress, report, and the persist marker. Fact rows, if the firm persists them, stay in Postgres under the firm key until the matter is deleted. The check lives in `backend/evals/test_source_retention.py`.

## Key rotation

`SESSION_SECRET` signs session tokens. To rotate it, set the old value as `SESSION_SECRET_PREVIOUS`, deploy the new `SESSION_SECRET`, and remove `SESSION_SECRET_PREVIOUS` after the session TTL. Tokens signed with either secret verify during the overlap. New tokens use only the current secret. Removing the current secret without a replacement signs everyone out; the service refuses to boot in production if it is empty.

`FACT_MEMORY_MASTER_KEY` wraps per-firm keys. Replacing it without rewrapping makes every fact row unreadable. Rotation is: deploy code that can see both secrets, call `rewrap_firm_key` for each `firms.wrapped_data_key` (old secret, then new secret), then remove the old secret. Fact ciphertext does not change. The helper is `backend/app/fact_memory/encryption.py`.

## Mail

Verify and reset links are sent when a sender (`SMTP_FROM` or `EMAIL_FROM`) is set and either `RESEND_API_KEY` or `SMTP_HOST` is set. Resend is used when the API key is present, because Render can reach it over HTTPS. Production signup fails closed with 503 when mail is not configured or the send fails, and the new account is not kept. The raw token is stored only as a hash (`cp_email_tokens`). It is not returned in the HTTP body when a message was sent. `/health` reports `email: true` only when delivery is configured. Firm owners and admins can read their own access log from Account; sign-in rows are limited to that account.

## Rate limits

Login, signup, reset, and admin trial grant use Redis when `REDIS_URL` is set (the control plane reads the `casesifter-queue` connection). If Redis is down in production, those routes return 503 rather than dropping the limit. Local tests with no `REDIS_URL` use process memory.
