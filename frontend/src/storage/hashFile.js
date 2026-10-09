import { createSHA256 } from "hash-wasm";

let hasherFactory = null;

function getHasher() {
  if (!hasherFactory) hasherFactory = createSHA256();
  return hasherFactory;
}

/** Stream SHA-256 so a large PDF is not copied into one buffer. */
export async function sha256OfBlob(blob) {
  const hasher = await getHasher();
  hasher.init();
  if (blob && typeof blob.stream === "function") {
    const reader = blob.stream().getReader();
    let chunks = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value && value.byteLength) hasher.update(value);
        chunks += 1;
        // Keep the register UI alive on multi-hundred-MB exhibits.
        if (chunks % 8 === 0) {
          await new Promise((resolve) => setTimeout(resolve, 0));
        }
      }
    } finally {
      reader.releaseLock?.();
    }
  } else {
    hasher.update(new Uint8Array(await blob.arrayBuffer()));
  }
  return hasher.digest("hex");
}
