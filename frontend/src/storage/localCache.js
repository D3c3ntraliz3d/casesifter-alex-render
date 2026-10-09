/**
 * Browser cache of original exhibit bytes. Not the file's home.
 * Keyed by SHA-256. A future vault adapter is a separate slot.
 */

const DB_NAME = "casesifter-local-files";
const DB_VERSION = 2;
const BLOB_STORE = "blobs";
const PERSIST_KEY = "casesifter_storage_persist";

const memory = new Map();
let persistAsked = false;

function openDb() {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === "undefined") {
      reject(new Error("IndexedDB unavailable"));
      return;
    }
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("handles")) {
        db.createObjectStore("handles", { keyPath: "sha256" });
      }
      if (!db.objectStoreNames.contains(BLOB_STORE)) {
        db.createObjectStore(BLOB_STORE, { keyPath: "sha256" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error || new Error("IndexedDB open failed"));
  });
}

function requestToPromise(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error || new Error("IndexedDB request failed"));
  });
}

function transactionDone(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error || new Error("IndexedDB transaction failed"));
    tx.onabort = () => reject(tx.error || new Error("IndexedDB transaction aborted"));
  });
}

async function withStore(mode, fn) {
  const db = await openDb();
  try {
    const tx = db.transaction(BLOB_STORE, mode);
    const done = transactionDone(tx);
    const value = await fn(tx.objectStore(BLOB_STORE));
    await done;
    return value;
  } finally {
    db.close();
  }
}

function idbGet(sha256) {
  return withStore("readonly", (store) => requestToPromise(store.get(sha256)));
}

function idbPut(record) {
  return withStore("readwrite", (store) => requestToPromise(store.put(record)));
}

function idbDelete(sha256) {
  return withStore("readwrite", (store) => requestToPromise(store.delete(sha256)));
}

function idbAll() {
  return withStore("readonly", (store) => requestToPromise(store.getAll()));
}

function idbClear() {
  return withStore("readwrite", (store) => requestToPromise(store.clear()));
}

function keyOf(sha256) {
  return String(sha256 || "").toLowerCase();
}

async function requestPersistentStorage() {
  if (persistAsked) return;
  persistAsked = true;
  let granted = false;
  try {
    if (typeof navigator !== "undefined" && navigator.storage?.persist) {
      granted = Boolean(await navigator.storage.persist());
    }
  } catch {
    granted = false;
  }
  try {
    localStorage.setItem(PERSIST_KEY, granted ? "granted" : "denied");
  } catch {
    /* private mode */
  }
}

export function storagePersistStatus() {
  try {
    return localStorage.getItem(PERSIST_KEY);
  } catch {
    return null;
  }
}

export const localCacheAdapter = {
  async getFile(_exhibitId, sha256) {
    const key = keyOf(sha256);
    if (!key) return null;
    if (memory.has(key)) return memory.get(key);
    try {
      const row = await idbGet(key);
      const blob = row?.blob || null;
      if (blob) memory.set(key, blob);
      return blob;
    } catch {
      return null;
    }
  },

  async saveFile(exhibitId, sha256, blob) {
    const key = keyOf(sha256);
    if (!key || !blob) return;
    memory.set(key, blob);
    await requestPersistentStorage();
    try {
      await idbPut({
        sha256: key,
        exhibitId: exhibitId || null,
        blob,
        byteLength: blob.size || 0,
        mimeType: blob.type || "",
        cachedAt: Date.now(),
      });
    } catch {
      /* private window or blocked storage: same-tab memory still works */
    }
  },

  async hasFile(exhibitId, sha256) {
    const blob = await this.getFile(exhibitId, sha256);
    return Boolean(blob);
  },

  async removeFile(_exhibitId, sha256) {
    const key = keyOf(sha256);
    if (!key) return;
    memory.delete(key);
    try {
      await idbDelete(key);
    } catch {
      /* ignore */
    }
  },
};

export async function cacheUsage() {
  let bytes = 0;
  let count = 0;
  try {
    const rows = await idbAll();
    for (const row of rows) {
      count += 1;
      bytes += Number(row?.byteLength || row?.blob?.size || 0);
    }
  } catch {
    for (const blob of memory.values()) {
      count += 1;
      bytes += blob?.size || 0;
    }
  }
  return { count, bytes };
}

export function formatCacheGb(bytes) {
  const gb = Number(bytes || 0) / (1024 * 1024 * 1024);
  return `${gb.toFixed(2)} GB`;
}

export async function cacheUsageLabel() {
  const { bytes } = await cacheUsage();
  return `Cached exhibits on this computer: ${formatCacheGb(bytes)}`;
}

export async function clearCachedExhibits() {
  memory.clear();
  try {
    await idbClear();
  } catch {
    /* already empty */
  }
}
