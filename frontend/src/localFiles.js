/**
 * Upload hook and page-open helper. Bytes live in the local cache adapter.
 */

import { sha256OfBlob } from "./storage/hashFile.js";
import { localCacheAdapter } from "./storage/localCache.js";
import { locateOneExhibit } from "./storage/locate.js";
import { resolveExhibitFile } from "./storage/resolver.js";

export async function sha256OfFile(file) {
  return sha256OfBlob(file);
}

/** Store the original upload bytes. A denied persist() request is not an error. */
export async function rememberScanFile(file, sha256) {
  const digest = String(sha256 || (await sha256OfBlob(file))).toLowerCase();
  await localCacheAdapter.saveFile(null, digest, file);
  return digest;
}

export async function openLocalDocumentAtPage({ sha256, page, displayName, exhibitId } = {}) {
  if (!sha256) {
    throw new Error("Missing document fingerprint (sha256).");
  }
  const pageNum = Math.max(1, Number(page) || 1);
  let blob = await resolveExhibitFile(exhibitId, sha256);
  if (!blob) {
    blob = await locateOneExhibit({ exhibitId, sha256 });
  }
  if (!blob) {
    throw new Error("Could not locate the local file for this finding.");
  }
  const head = new Uint8Array(await blob.slice(0, 5).arrayBuffer());
  const magic = String.fromCharCode(...head);
  const url = URL.createObjectURL(blob);
  const withPage = magic.startsWith("%PDF-") ? `${url}#page=${pageNum}` : url;
  window.open(withPage, "_blank", "noopener,noreferrer");
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
  return { sha256, page: pageNum, name: displayName || "" };
}
