/**
 * Slot for a future firm-vault adapter (OneDrive, SharePoint, or Google Drive).
 * Not implemented. The resolver treats a miss here as "locate the file".
 */
export const vaultSlotAdapter = {
  async getFile() {
    return null;
  },
  async saveFile() {},
  async hasFile() {
    return false;
  },
  async removeFile() {},
};
