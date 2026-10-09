import { createSHA256 } from "hash-wasm";

/** Stream SHA-256 so a large PDF is not copied into one buffer. */
export async function sha256OfBlob(blob) {
  const hasher = await createSHA256();
  hasher.init();
  if (blob && typeof blob.stream === "function") {
    const reader = blob.stream().getReader();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value && value.byteLength) hasher.update(value);
      }
    } finally {
      reader.releaseLock?.();
    }
  } else {
    hasher.update(new Uint8Array(await blob.arrayBuffer()));
  }
  return hasher.digest("hex");
}
