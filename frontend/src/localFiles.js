/**
 * IndexedDB map: document sha256 → File System Access API handle.
 * Used to open local PDFs at a finding's page without sending paths to the server.
 */

const DB_NAME = "casesifter-local-files";
const STORE = "handles";
const DB_VERSION = 1;

function openDb() {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === "undefined") {
      reject(new Error("IndexedDB unavailable"));
      return;
    }
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE, { keyPath: "sha256" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error || new Error("IndexedDB open failed"));
  });
}

async function withStore(mode, fn) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, mode);
    const store = tx.objectStore(STORE);
    Promise.resolve(fn(store))
      .then((value) => {
        tx.oncomplete = () => {
          db.close();
          resolve(value);
        };
        tx.onerror = () => {
          db.close();
          reject(tx.error);
        };
      })
      .catch((err) => {
        db.close();
        reject(err);
      });
  });
}

export async function storeFileHandle(sha256, handle, meta = {}) {
  if (!sha256 || !handle) return;
  await withStore("readwrite", (store) => {
    store.put({
      sha256: String(sha256).toLowerCase(),
      handle,
      name: meta.name || handle.name || "",
      storedAt: Date.now(),
    });
  });
}

export async function getFileHandle(sha256) {
  if (!sha256) return null;
  return withStore("readonly", (store) => {
    return new Promise((resolve, reject) => {
      const req = store.get(String(sha256).toLowerCase());
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error);
    });
  });
}

export async function removeFileHandle(sha256) {
  if (!sha256) return;
  await withStore("readwrite", (store) => {
    store.delete(String(sha256).toLowerCase());
  });
}

/** SHA-256 hex of a File / Blob (Web Crypto). */
export async function sha256OfFile(file) {
  const buf = await file.arrayBuffer();
  const hash = await crypto.subtle.digest("SHA-256", buf);
  return [...new Uint8Array(hash)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * Remember a browser File for later page open.
 * Uses File System Access when the picker provided a handle; otherwise stores nothing
 * (Blob handles cannot be persisted across reloads without showOpenFilePicker).
 */
export async function rememberScanFile(file, sha256, handle = null) {
  const digest = sha256 || (await sha256OfFile(file));
  if (handle && typeof handle.getFile === "function") {
    await storeFileHandle(digest, handle, { name: file?.name || handle.name });
  }
  // Also keep an in-memory fallback for this session.
  if (typeof window !== "undefined") {
    window.__casesifterFileCache = window.__casesifterFileCache || new Map();
    window.__casesifterFileCache.set(digest.toLowerCase(), file);
  }
  return digest;
}

export function getSessionCachedFile(sha256) {
  if (typeof window === "undefined" || !sha256) return null;
  return window.__casesifterFileCache?.get(String(sha256).toLowerCase()) || null;
}

/**
 * Open (or locate) the local file for a finding ref and jump to page.
 * Never sends local paths to the server.
 */
export async function openLocalDocumentAtPage({ sha256, page, displayName } = {}) {
  if (!sha256) {
    throw new Error("Missing document fingerprint (sha256).");
  }
  const pageNum = Math.max(1, Number(page) || 1);

  // 1) Session cache (same browser tab after upload)
  let file = getSessionCachedFile(sha256);

  // 2) Persisted File System Access handle
  if (!file) {
    const row = await getFileHandle(sha256).catch(() => null);
    if (row?.handle) {
      try {
        const perm = await row.handle.queryPermission?.({ mode: "read" });
        if (perm !== "granted") {
          const next = await row.handle.requestPermission?.({ mode: "read" });
          if (next !== "granted") throw new Error("Permission denied");
        }
        file = await row.handle.getFile();
        const digest = await sha256OfFile(file);
        if (digest.toLowerCase() !== String(sha256).toLowerCase()) {
          await removeFileHandle(sha256);
          file = null;
        }
      } catch {
        file = null;
      }
    }
  }

  // 3) Prompt locate-and-verify
  if (!file) {
    file = await locateAndVerifyHash(sha256, displayName);
  }

  if (!file) {
    throw new Error("Could not locate the local file for this finding.");
  }

  const url = URL.createObjectURL(file);
  // PDF page fragment is supported by most Chromium PDF viewers.
  const withPage = file.type === "application/pdf" || /\.pdf$/i.test(file.name || "")
    ? `${url}#page=${pageNum}`
    : url;
  window.open(withPage, "_blank", "noopener,noreferrer");
  // Revoke later so the new tab can load.
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
  return { sha256, page: pageNum, name: file.name };
}

/**
 * showOpenFilePicker (Chromium) or <input type=file> fallback; verify sha256.
 */
export async function locateAndVerifyHash(expectedSha256, suggestedName) {
  const expected = String(expectedSha256).toLowerCase();
  let file = null;
  let handle = null;

  if (typeof window.showOpenFilePicker === "function") {
    try {
      const [picked] = await window.showOpenFilePicker({
        multiple: false,
        excludeAcceptAllOption: false,
        types: [
          {
            description: "Case documents",
            accept: {
              "application/pdf": [".pdf"],
              "image/*": [".png", ".jpg", ".jpeg", ".gif"],
              "text/plain": [".txt"],
            },
          },
        ],
      });
      handle = picked;
      file = await picked.getFile();
    } catch (err) {
      if (err?.name === "AbortError") return null;
      // fall through to input
    }
  }

  if (!file) {
    file = await new Promise((resolve) => {
      const input = document.createElement("input");
      input.type = "file";
      input.accept = ".pdf,.png,.jpg,.jpeg,.gif,.txt,.docx";
      input.style.display = "none";
      document.body.appendChild(input);
      input.addEventListener("change", () => {
        const f = input.files?.[0] || null;
        document.body.removeChild(input);
        resolve(f);
      });
      input.click();
    });
  }

  if (!file) return null;
  const digest = await sha256OfFile(file);
  if (digest.toLowerCase() !== expected) {
    throw new Error(
      `That file does not match this finding (expected ${expected.slice(0, 12)}…, got ${digest.slice(0, 12)}…).`
    );
  }
  if (handle) {
    await storeFileHandle(expected, handle, { name: file.name || suggestedName });
  }
  if (typeof window !== "undefined") {
    window.__casesifterFileCache = window.__casesifterFileCache || new Map();
    window.__casesifterFileCache.set(expected, file);
  }
  return file;
}
