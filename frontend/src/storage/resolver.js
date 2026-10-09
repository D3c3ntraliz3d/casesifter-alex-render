import { sha256OfBlob } from "./hashFile.js";
import { localCacheAdapter } from "./localCache.js";
import { vaultSlotAdapter } from "./vaultSlot.js";

const adapters = [localCacheAdapter, vaultSlotAdapter];

/** Local cache, then the empty vault slot. A hash mismatch is treated as missing. */
export async function resolveExhibitFile(exhibitId, sha256) {
  const expected = String(sha256 || "").toLowerCase();
  if (!expected) return null;
  for (const adapter of adapters) {
    let blob = null;
    try {
      blob = await adapter.getFile(exhibitId, expected);
    } catch {
      blob = null;
    }
    if (!blob) continue;
    let digest = "";
    try {
      digest = String(await sha256OfBlob(blob)).toLowerCase();
    } catch {
      continue;
    }
    if (digest !== expected) {
      try {
        await adapter.removeFile(exhibitId, expected);
      } catch {
        /* ignore */
      }
      continue;
    }
    return blob;
  }
  return null;
}
