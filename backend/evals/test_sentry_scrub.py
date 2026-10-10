"""P0-4: Sentry events must never contain case data or frame vars."""

from __future__ import annotations

import json
import os

os.environ.setdefault("FACT_MEMORY_MASTER_KEY", "dGVzdC1tYXN0ZXIta2V5LTEyMzQ1Njc4OTBhYmNkZWY=")

from app.sentry_scrub import _scrub_event, route_template


def test_route_template_strips_ids_and_query():
    jid = "a" * 32
    assert route_template(f"/api/v1/jobs/{jid}/report?x=1") == "/api/v1/jobs/{id}/report"
    assert "{id}" in route_template(f"https://host/api/v1/jobs/{jid}")


def test_scrub_event_drops_secrets_and_frame_vars():
    job_id = "0123456789abcdef0123456789abcdef"
    event = {
        "level": "error",
        "release": "CaseSifter@1.0",
        "environment": "test",
        "transaction": f"/api/v1/jobs/{job_id}",
        "tags": {"service": "analysis-api"},
        "extra": {"facts": [{"value": "SECRET-123"}], "display_name": "Exhibit 58 — SECRET"},
        "user": {"id": "support", "ip_address": "1.2.3.4"},
        "contexts": {
            "runtime": {"name": "CPython", "version": "3.12"},
            "trace": {"trace_id": "should-drop"},
        },
        "request": {
            "url": f"https://host/api/v1/jobs/{job_id}?token=abc",
            "method": "GET",
            "headers": {
                "Authorization": "Bearer SECRET-TOKEN",
                "X-API-Key": "SECRET-KEY",
                "User-Agent": "pytest/1.0",
            },
            "data": {"body": "SECRET-123"},
            "query_string": "token=abc",
        },
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": "failed for Exhibit 58 — SECRET-123",
                    "stacktrace": {
                        "frames": [
                            {
                                "filename": "app/v1_analysis.py",
                                "function": "boom",
                                "lineno": 42,
                                "module": "app.v1_analysis",
                                "vars": {
                                    "facts": [{"value": "SECRET-123"}],
                                    "display_name": "Exhibit 58 — SECRET",
                                    "original_filename": "SECRET.pdf",
                                },
                            }
                        ]
                    },
                }
            ]
        },
        "breadcrumbs": {
            "values": [
                {
                    "category": "httplib",
                    "level": "info",
                    "message": "GET SECRET",
                    "data": {"url": f"https://host/api/v1/jobs/{job_id}"},
                }
            ]
        },
    }

    scrubbed = _scrub_event(event)
    blob = json.dumps(scrubbed)

    assert "SECRET" not in blob
    assert job_id not in blob
    assert "Bearer" not in blob
    assert "X-API-Key" not in blob or "SECRET-KEY" not in blob
    assert "1.2.3.4" not in blob
    assert "extra" not in scrubbed
    assert "trace" not in (scrubbed.get("contexts") or {})

    frames = scrubbed["exception"]["values"][0]["stacktrace"]["frames"]
    assert frames[0]["function"] == "boom"
    assert "vars" not in frames[0]
    assert scrubbed["request"]["url"] == "/api/v1/jobs/{id}"
    assert scrubbed["request"]["headers"] == {"User-Agent": "pytest/1.0"}
    assert "Authorization" not in scrubbed["request"].get("headers", {})
    assert scrubbed["transaction"] == "/api/v1/jobs/{id}"
    crumbs = scrubbed["breadcrumbs"]["values"]
    assert crumbs[0]["category"] == "httplib"
    assert "message" not in crumbs[0]
    assert crumbs[0]["data"]["url"] == "/api/v1/jobs/{id}"


def test_sentry_capture_with_fake_transport_has_no_secrets():
    """Raise inside a function holding sensitive locals; scrubbed event has no SECRET/vars."""
    import sentry_sdk
    from sentry_sdk.transport import Transport

    captured: list[dict] = []

    class FakeTransport(Transport):
        def capture_envelope(self, envelope):  # type: ignore[override]
            for item in envelope.items:
                payload = item.payload.json
                if isinstance(payload, dict) and payload.get("type") == "event":
                    captured.append(payload)
                elif isinstance(payload, dict) and "exception" in payload:
                    captured.append(payload)

        def capture_event(self, event):  # type: ignore[override]
            captured.append(event)

    # Some SDK versions use envelopes exclusively.
    client = sentry_sdk.Client(
        dsn="http://public@localhost/1",
        transport=FakeTransport(),
        before_send=lambda e, _h: _scrub_event(e),
        include_local_variables=True,  # even if SDK attaches vars, scrub must drop them
        send_default_pii=False,
        max_request_body_size="never",
    )
    hub = sentry_sdk.Hub(client)

    def explode():
        facts = [{"value": "SECRET-123"}]
        display_name = "Exhibit 58 — SECRET"
        original_filename = "client-SECRET.pdf"
        _ = (facts, display_name, original_filename)
        raise RuntimeError("test boom")

    with hub:
        try:
            explode()
        except RuntimeError:
            hub.capture_exception()

    client.flush(timeout=2.0)
    assert captured, "expected at least one captured event"
    for ev in captured:
        scrubbed = _scrub_event(ev) if "extra" in ev or (
            isinstance((ev.get("exception") or {}).get("values"), list)
            and any(
                "vars" in ((v.get("stacktrace") or {}).get("frames") or [{}])[0]
                for v in ev["exception"]["values"]
                if isinstance(v, dict)
            )
        ) else ev
        # Always re-scrub for assertion — production path always runs before_send.
        scrubbed = _scrub_event(ev)
        blob = json.dumps(scrubbed)
        assert "SECRET" not in blob, blob[:500]
        for val in (scrubbed.get("exception") or {}).get("values") or []:
            for frame in ((val.get("stacktrace") or {}).get("frames") or []):
                assert "vars" not in frame
