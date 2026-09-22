# DMS ingest (Phase 2)

CaseSifter does **not** store firm DMS files in a cloud vault.

## Target flow

1. Firm DMS (iManage / NetDocuments / SharePoint / Box) emits a webhook or the desktop agent polls for new files.
2. Control plane accepts the notification at `POST /v1/dms/webhook` (stub implemented — metadata only).
3. **Desktop agent** downloads the file into the local CaseSifter case folder.
4. Desktop calls the ephemeral analysis API (`POST /api/v1/jobs`), saves the report locally, and the server deletes the upload.

## Stub today

[`control_plane/main.py`](../control_plane/main.py) logs webhook JSON to `control_plane/data/dms_events.jsonl` and returns `accepted: true` without storing document bytes.

## Next implementation steps

- OAuth connectors per DMS vendor
- Map DMS matter id → local CaseSifter case id
- Background desktop watcher for `dms_events.jsonl` or push channel
- Optional write-back of gotcha summary as DMS comment/metadata
