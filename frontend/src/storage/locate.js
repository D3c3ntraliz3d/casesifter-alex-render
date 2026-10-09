import { sha256OfBlob } from "./hashFile.js";
import { localCacheAdapter } from "./localCache.js";
import { resolveExhibitFile } from "./resolver.js";

function sizeOf(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : null;
}

async function filesFromDirectoryHandle(dirHandle, out) {
  for await (const entry of dirHandle.values()) {
    if (entry.kind === "file") {
      out.push(await entry.getFile());
    } else if (entry.kind === "directory") {
      await filesFromDirectoryHandle(entry, out);
    }
  }
}

function filesFromInput() {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = true;
    input.webkitdirectory = true;
    input.style.display = "none";
    document.body.appendChild(input);
    const finish = (files) => {
      input.remove();
      resolve(files);
    };
    input.addEventListener("change", () => {
      finish(Array.from(input.files || []));
    });
    input.addEventListener("cancel", () => finish(null));
    input.click();
  });
}

async function chooseFolder() {
  if (typeof window !== "undefined" && typeof window.showDirectoryPicker === "function") {
    try {
      const dir = await window.showDirectoryPicker();
      const files = [];
      await filesFromDirectoryHandle(dir, files);
      return files;
    } catch (err) {
      if (err?.name === "AbortError") return null;
    }
  }
  return filesFromInput();
}

function chooseOneFile() {
  return new Promise((resolve) => {
    if (typeof window.showOpenFilePicker === "function") {
      window
        .showOpenFilePicker({ multiple: false })
        .then(async (handles) => {
          const file = handles?.[0] ? await handles[0].getFile() : null;
          resolve(file);
        })
        .catch((err) => {
          if (err?.name === "AbortError") resolve(null);
          else resolve(pickOneInput());
        });
      return;
    }
    resolve(pickOneInput());
  });
}

function pickOneInput() {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.style.display = "none";
    document.body.appendChild(input);
    const finish = (file) => {
      input.remove();
      resolve(file);
    };
    input.addEventListener("change", () => finish(input.files?.[0] || null));
    input.addEventListener("cancel", () => finish(null));
    input.click();
  });
}

/**
 * Walk one folder and match every missing exhibit in the case by hash.
 * Files whose size matches none of the missing exhibits are not hashed.
 * Non-matches are ignored and never uploaded.
 */
export async function locateCaseFiles(exhibits, { onProgress } = {}) {
  const all = new Map();
  for (const item of exhibits || []) {
    const sha = String(item?.sha256 || "").toLowerCase();
    if (!sha || all.has(sha)) continue;
    all.set(sha, {
      exhibitId: item.exhibitId || null,
      sha256: sha,
      byteLength: sizeOf(item.byteLength),
      name: item.name || "",
    });
  }
  const total = all.size;
  const pending = new Map();
  let already = 0;
  for (const [sha, item] of all) {
    const cached = await localCacheAdapter.hasFile(item.exhibitId, sha).catch(() => false);
    if (cached) already += 1;
    else pending.set(sha, item);
  }
  if (!pending.size) {
    return {
      cancelled: false,
      found: already,
      total,
      stillMissing: 0,
      summary: `Found ${already} of ${total} exhibits. 0 still missing.`,
    };
  }

  const files = await chooseFolder();
  if (!files) {
    return {
      cancelled: true,
      found: already,
      total,
      stillMissing: pending.size,
      summary: "",
    };
  }

  const knownSizes = new Set();
  let unknownSize = false;
  for (const item of pending.values()) {
    if (item.byteLength) knownSizes.add(item.byteLength);
    else unknownSize = true;
  }

  let scanned = 0;
  let foundNow = 0;
  for (const file of files) {
    if (!pending.size) break;
    scanned += 1;
    const size = file.size || 0;
    const sizeHit = knownSizes.has(size);
    if (!sizeHit && !unknownSize) {
      onProgress?.({ scanned, found: already + foundNow, total });
      continue;
    }
    let digest = "";
    try {
      digest = String(await sha256OfBlob(file)).toLowerCase();
    } catch {
      onProgress?.({ scanned, found: already + foundNow, total });
      continue;
    }
    const hit = pending.get(digest);
    if (hit) {
      await localCacheAdapter.saveFile(hit.exhibitId, hit.sha256, file);
      pending.delete(digest);
      foundNow += 1;
    }
    onProgress?.({ scanned, found: already + foundNow, total });
  }

  const found = already + foundNow;
  const stillMissing = pending.size;
  return {
    cancelled: false,
    found,
    total,
    stillMissing,
    summary: `Found ${found} of ${total} exhibits. ${stillMissing} still missing.`,
  };
}

/** One exhibit. A same-name file with different bytes is rejected. */
export async function locateOneExhibit({ exhibitId, sha256, byteLength } = {}) {
  const expected = String(sha256 || "").toLowerCase();
  if (!expected) throw new Error("Missing document fingerprint (sha256).");
  const file = await chooseOneFile();
  if (!file) return null;
  const expectedSize = sizeOf(byteLength);
  if (expectedSize && file.size !== expectedSize) {
    throw new Error("That file does not match this exhibit.");
  }
  const digest = String(await sha256OfBlob(file)).toLowerCase();
  if (digest !== expected) {
    throw new Error("That file does not match this exhibit.");
  }
  await localCacheAdapter.saveFile(exhibitId, expected, file);
  return resolveExhibitFile(exhibitId, expected);
}
