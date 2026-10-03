const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("casesifter", {
  isDesktop: true,
  getConfig: () => ipcRenderer.invoke("cs:get-config"),
  setConfig: (patch) => ipcRenderer.invoke("cs:set-config", patch),
  listCases: () => ipcRenderer.invoke("cs:list-cases"),
  createCase: (payload) => ipcRenderer.invoke("cs:create-case", payload),
  getCase: (id) => ipcRenderer.invoke("cs:get-case", id),
  archiveCase: (id) => ipcRenderer.invoke("cs:archive-case", id),
  unarchiveCase: (id) => ipcRenderer.invoke("cs:unarchive-case", id),
  deleteCase: (id) => ipcRenderer.invoke("cs:delete-case", id),
  deleteDocument: (caseId, documentId) => ipcRenderer.invoke("cs:delete-doc", caseId, documentId),
  pickAndAddDocuments: (caseId) => ipcRenderer.invoke("cs:pick-docs", caseId),
  analyzeDocument: (caseId, documentId) => ipcRenderer.invoke("cs:analyze", caseId, documentId),
  getLocalReport: (caseId, documentId) => ipcRenderer.invoke("cs:get-report", caseId, documentId),
  listCaseChat: (caseId) => ipcRenderer.invoke("cs:list-chat", caseId),
  askCaseChat: (caseId, question, focusDocumentId) =>
    ipcRenderer.invoke("cs:case-chat", caseId, question, focusDocumentId),
  listCaseFactConflicts: (caseId) => ipcRenderer.invoke("cs:case-fact-conflicts", caseId),
  openDocument: (caseId, documentId) => ipcRenderer.invoke("cs:open-doc", caseId, documentId),
  login: (email, password) => ipcRenderer.invoke("cs:login", email, password),
  signup: (email, password, name, acceptedTerms, inviteCode) =>
    ipcRenderer.invoke("cs:signup", email, password, name, acceptedTerms, inviteCode),
  entitlement: () => ipcRenderer.invoke("cs:entitlement"),
  checkout: (trial, quantity = 1) => ipcRenderer.invoke("cs:checkout", trial, quantity),
  findingFeedback: (payload) => ipcRenderer.invoke("cs:finding-feedback", payload),
  reportProblem: (note) => ipcRenderer.invoke("cs:report-problem", note),
});
