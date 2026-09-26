# Detector restore points

## `restore-2026-09-22-detector` → `3c41141`

**Date:** 2026-09-22  
**Commit:** `3c411419d35ceec37852079d568701f497e1a150` — *Add documentary lie detector: grounded fact graph + consistency*

This is the tip that still caught fraud on **EXHIBIT-named** uploads (full forensic lane) and had just shipped the documentary lie detector for inconsistencies.

At that snapshot, blank/unknown kind and NYSCEF caption peeks still defaulted to `court_filing` (the packaging fast path). Later scan-first work on the main line fixes that without rolling back noise discipline.

### Restore / compare

```bash
# Read-only look at the 9-22 tree
git fetch origin tag restore-2026-09-22-detector
git checkout restore-2026-09-22-detector

# Return to current work
git checkout cursor/cloud-agent-1790049794515-xh7tp   # or main
```

Do **not** use `desktop-v1.0.0` as a stand-in for 9-22 — that tag is already 9-24+ and quieter on logo/math materiality.

### What HEAD should match or beat

| Shape | 9-22 (`3c41141`) | Current scan-first HEAD |
| --- | --- | --- |
| `EXHIBIT_*` / kind=exhibit bill | forensic | forensic |
| Caption + scan-heavy NYSCEF PDF | court_filing (miss) | forensic |
| Blank + caption + native-text PDF | court_filing (skips Gemini) | forensic (Gemini runs) |
| Explicit `affidavit` kind, native-text | court_filing | court_filing |
| Wayne occupancy peers | lie detector / consistency | fact conflict + risk elevate |
