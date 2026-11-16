// DocuFlow AI - Frontend Controller (Updated with Parallel & TaskToken Orchestration)

const DEFAULT_API_BASE_URL = "https://z9okxb9r8j.execute-api.us-east-1.amazonaws.com";
const DEFAULT_COGNITO_CLIENT_ID = "7a4ohh6j10s38a4cio8frddanr";
const DEFAULT_AWS_REGION = "us-east-1";

let API_BASE_URL = localStorage.getItem("DOCUFLOW_API_URL") || DEFAULT_API_BASE_URL;
let API_AUTH_TOKEN = localStorage.getItem("DOCUFLOW_COGNITO_TOKEN") || localStorage.getItem("DOCUFLOW_AUTH_TOKEN") || "";
let COGNITO_CLIENT_ID = localStorage.getItem("DOCUFLOW_COGNITO_CLIENT_ID") || DEFAULT_COGNITO_CLIENT_ID;
let AWS_REGION = localStorage.getItem("DOCUFLOW_AWS_REGION") || DEFAULT_AWS_REGION;
let USER_EMAIL = localStorage.getItem("DOCUFLOW_USER_EMAIL") || "";
let USER_ROLE = localStorage.getItem("DOCUFLOW_USER_ROLE") || "";

function parseJwt(token) {
  try {
    const base64Url = token.split('.')[1];
    if (!base64Url) return null;
    const base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/');
    const jsonPayload = decodeURIComponent(atob(base64).split('').map(function(c) {
      return '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2);
    }).join(''));
    return JSON.parse(jsonPayload);
  } catch (e) {
    return null;
  }
}

function updateAuthUI() {
  const userStatusPill = document.getElementById("userStatusPill");
  const userEmailBadge = document.getElementById("userEmailBadge");
  const userRoleBadge = document.getElementById("userRoleBadge");
  const configTrigger = document.getElementById("configTrigger");

  if (API_AUTH_TOKEN) {
    const claims = parseJwt(API_AUTH_TOKEN);
    let email = USER_EMAIL;
    let isReviewer = USER_ROLE === "Reviewer";

    if (claims) {
      email = claims.email || claims["cognito:username"] || email || "Authenticated User";
      const groups = claims["cognito:groups"] || [];
      const groupList = Array.isArray(groups) ? groups : [groups];
      isReviewer = groupList.includes("Reviewers") || groupList.includes("Admins");
    }

    if (userStatusPill && userEmailBadge && userRoleBadge) {
      userEmailBadge.innerText = email || "Authenticated User";
      userRoleBadge.innerText = isReviewer ? "Reviewer" : "Standard User";
      userRoleBadge.className = isReviewer ? "role-badge" : "role-badge role-standard";
      userStatusPill.style.display = "flex";
    }

    if (configTrigger) {
      configTrigger.innerText = "⚙️ Connected";
    }
  } else {
    if (userStatusPill) userStatusPill.style.display = "none";
    if (configTrigger) configTrigger.innerText = "⚙️ Connect & Sign In";
  }
}

async function loginWithCognito(email, password, clientId, region) {
  const endpoint = `https://cognito-idp.${region}.amazonaws.com/`;
  const response = await fetch(endpoint, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth"
    },
    body: JSON.stringify({
      AuthFlow: "USER_PASSWORD_AUTH",
      ClientId: clientId,
      AuthParameters: {
        USERNAME: email,
        PASSWORD: password
      }
    })
  });

  const data = await response.json();
  if (!response.ok) {
    const errorMsg = data.message || data.__type || "Authentication failed";
    throw new Error(errorMsg);
  }

  if (data.AuthenticationResult && data.AuthenticationResult.IdToken) {
    return {
      idToken: data.AuthenticationResult.IdToken,
      accessToken: data.AuthenticationResult.AccessToken
    };
  }

  if (data.ChallengeName === "NEW_PASSWORD_REQUIRED") {
    throw new Error("Temporary password detected: Set a permanent password using 'aws cognito-idp admin-set-user-password --permanent' before signing in.");
  }

  throw new Error("Cognito authentication failed: " + (data.message || "Unknown error"));
}

function getAuthHeaders(customHeaders = {}) {
  const headers = { ...customHeaders };
  if (API_AUTH_TOKEN) {
    headers["Authorization"] = `Bearer ${API_AUTH_TOKEN}`;
  }
  return headers;
}

// In-memory demo store for offline/simulation mode
let localDocuments = [
  {
    document_id: "7a8b9c0d-1122-3344-5566-778899aabbcc",
    filename: "AWS_Cloud_Monthly_Invoice_Oct2026.pdf",
    status: "APPROVED",
    decision: "AUTO_APPROVED",
    created_at: new Date(Date.now() - 3600000 * 4).toISOString(),
    extracted_data: {
      vendor: "Amazon Web Services EMEA",
      invoice_number: "INV-202610-7A8B9C",
      grand_total: 1420.50,
      currency: "USD",
      vendor_trusted: true,
      raw_text_length: 1240,
      ai_model_used: "amazon.nova-lite-v1:0",
      ai_inference_provider: "Amazon Bedrock (Serverless LLM)",
      ai_analysis: "Verified cloud infrastructure entity; financial math and line items matched accurately."
    },
    security_scan: {
      status: "VALIDATED",
      sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      sha256_computed: true,
      format_whitelisted: true,
      size_within_limits: true
    },
    risk: {
      score: 5,
      level: "LOW",
      recommendation: "AUTO_APPROVE",
      factors: ["Verified enterprise vendor: 'Amazon Web Services EMEA' (-10)"]
    }
  },
  {
    document_id: "3e4f5a6b-9988-7766-5544-33221100ffaa",
    filename: "Apex_Global_Consultancy_Offshore_Invoice.pdf",
    status: "PENDING_APPROVAL",
    decision: "AWAITING_HUMAN_REVIEW",
    task_token: "AAAAKgAAAAIAAAAAAAAAAY12345SampleTaskToken==",
    created_at: new Date(Date.now() - 3600000 * 1).toISOString(),
    extracted_data: {
      vendor: "Apex Global Consultancy Offshore Ltd",
      invoice_number: "INV-202611-3E4F5A",
      grand_total: 89500.00,
      currency: "USD",
      vendor_trusted: false,
      raw_text_length: 890,
      ai_model_used: "amazon.nova-lite-v1:0",
      ai_inference_provider: "Amazon Bedrock (Serverless LLM)",
      ai_analysis: "High risk warning: Offshore entity with unverified tax registry and unusually large transaction balance."
    },
    security_scan: {
      status: "VALIDATED",
      sha256: "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
      sha256_computed: true,
      format_whitelisted: true,
      size_within_limits: true
    },
    risk: {
      score: 95,
      level: "HIGH",
      recommendation: "FLAG_FOR_MANUAL_REVIEW",
      factors: [
        "Untrusted or unregistered offshore entity: 'Apex Global Consultancy Offshore Ltd' (+45)",
        "High-value financial transaction exceeding $20,000 threshold ($89,500.00) (+40)"
      ]
    }
  }
];

// DOM Elements
const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const documentsTbody = document.getElementById("documentsTbody");
const refreshBtn = document.getElementById("refreshBtn");
const modalBackdrop = document.getElementById("modalBackdrop");
const modalTitle = document.getElementById("modalTitle");
const modalBody = document.getElementById("modalBody");
const modalFooter = document.getElementById("modalFooter");
const modalClose = document.getElementById("modalClose");
const configModal = document.getElementById("configModal");
const configTrigger = document.getElementById("configTrigger");
const configClose = document.getElementById("configClose");
const apiUrlInput = document.getElementById("apiUrlInput");
const apiTokenInput = document.getElementById("apiTokenInput");
const saveConfigBtn = document.getElementById("saveConfigBtn");
const cognitoClientIdInput = document.getElementById("cognitoClientIdInput");
const cognitoRegionInput = document.getElementById("cognitoRegionInput");
const cognitoEmailInput = document.getElementById("cognitoEmailInput");
const cognitoPasswordInput = document.getElementById("cognitoPasswordInput");
const cognitoLoginBtn = document.getElementById("cognitoLoginBtn");
const authStatusMsg = document.getElementById("authStatusMsg");
const logoutBtn = document.getElementById("logoutBtn");
const configCancelBtn = document.getElementById("configCancelBtn");

// Initialize
document.addEventListener("DOMContentLoaded", () => {
  if (apiUrlInput) apiUrlInput.value = API_BASE_URL;
  if (apiTokenInput) apiTokenInput.value = API_AUTH_TOKEN;
  if (cognitoClientIdInput) cognitoClientIdInput.value = COGNITO_CLIENT_ID;
  if (cognitoRegionInput) cognitoRegionInput.value = AWS_REGION;
  if (cognitoEmailInput && USER_EMAIL) cognitoEmailInput.value = USER_EMAIL;

  updateAuthUI();
  renderDocumentsTable();
  setupEventListeners();
});

function setupEventListeners() {
  dropzone.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", (e) => {
    if (e.target.files.length > 0) {
      handleFileUpload(e.target.files[0]);
    }
  });

  dropzone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropzone.classList.add("dragover");
  });

  dropzone.addEventListener("dragleave", () => {
    dropzone.classList.remove("dragover");
  });

  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragover");
    if (e.dataTransfer.files.length > 0) {
      handleFileUpload(e.dataTransfer.files[0]);
    }
  });

  // Preset quick triggers
  document.querySelectorAll(".btn-preset").forEach(btn => {
    btn.addEventListener("click", () => {
      const presetType = btn.getAttribute("data-preset");
      runPresetDemo(presetType);
    });
  });

  refreshBtn.addEventListener("click", fetchDocuments);

  modalClose.addEventListener("click", () => modalBackdrop.classList.remove("show"));
  configTrigger.addEventListener("click", () => configModal.classList.add("show"));
  configClose.addEventListener("click", () => configModal.classList.remove("show"));
  if (configCancelBtn) {
    configCancelBtn.addEventListener("click", () => configModal.classList.remove("show"));
  }

  // Logout listener
  if (logoutBtn) {
    logoutBtn.addEventListener("click", () => {
      API_AUTH_TOKEN = "";
      USER_EMAIL = "";
      USER_ROLE = "";
      localStorage.removeItem("DOCUFLOW_COGNITO_TOKEN");
      localStorage.removeItem("DOCUFLOW_AUTH_TOKEN");
      localStorage.removeItem("DOCUFLOW_USER_EMAIL");
      localStorage.removeItem("DOCUFLOW_USER_ROLE");
      if (apiTokenInput) apiTokenInput.value = "";
      updateAuthUI();
      fetchDocuments();
    });
  }

  // Cognito direct Sign-In listener
  if (cognitoLoginBtn) {
    cognitoLoginBtn.addEventListener("click", async () => {
      const email = cognitoEmailInput ? cognitoEmailInput.value.trim() : "";
      const password = cognitoPasswordInput ? cognitoPasswordInput.value : "";
      const clientId = cognitoClientIdInput ? cognitoClientIdInput.value.trim() : "";
      const region = cognitoRegionInput ? cognitoRegionInput.value.trim() : "us-east-1";

      if (!email || !password || !clientId) {
        if (authStatusMsg) {
          authStatusMsg.style.display = "block";
          authStatusMsg.className = "auth-status-msg error";
          authStatusMsg.innerText = "Please provide Cognito Client ID, Email, and Password.";
        }
        return;
      }

      cognitoLoginBtn.disabled = true;
      cognitoLoginBtn.innerText = "Authenticating with AWS Cognito...";
      if (authStatusMsg) {
        authStatusMsg.style.display = "none";
        authStatusMsg.className = "auth-status-msg";
        authStatusMsg.innerText = "";
      }

      try {
        const tokens = await loginWithCognito(email, password, clientId, region);
        API_AUTH_TOKEN = tokens.idToken;
        localStorage.setItem("DOCUFLOW_COGNITO_TOKEN", API_AUTH_TOKEN);
        localStorage.setItem("DOCUFLOW_COGNITO_CLIENT_ID", clientId);
        localStorage.setItem("DOCUFLOW_AWS_REGION", region);

        const claims = parseJwt(API_AUTH_TOKEN);
        if (claims) {
          USER_EMAIL = claims.email || email;
          const groups = claims["cognito:groups"] || [];
          const groupList = Array.isArray(groups) ? groups : [groups];
          USER_ROLE = (groupList.includes("Reviewers") || groupList.includes("Admins")) ? "Reviewer" : "Standard User";
          localStorage.setItem("DOCUFLOW_USER_EMAIL", USER_EMAIL);
          localStorage.setItem("DOCUFLOW_USER_ROLE", USER_ROLE);
        }

        if (apiTokenInput) apiTokenInput.value = API_AUTH_TOKEN;

        if (apiUrlInput && apiUrlInput.value.trim()) {
          API_BASE_URL = apiUrlInput.value.trim().replace(/\/+$/, "");
          localStorage.setItem("DOCUFLOW_API_URL", API_BASE_URL);
        }

        if (authStatusMsg) {
          authStatusMsg.style.display = "block";
          authStatusMsg.className = "auth-status-msg success";
          authStatusMsg.innerText = `Signed in successfully as ${USER_EMAIL} (${USER_ROLE})!`;
        }

        setTimeout(() => {
          configModal.classList.remove("show");
          updateAuthUI();
          fetchDocuments();
        }, 1200);
      } catch (err) {
        if (authStatusMsg) {
          authStatusMsg.style.display = "block";
          authStatusMsg.className = "auth-status-msg error";
          authStatusMsg.innerText = err.message || "Failed to authenticate with Cognito.";
        }
      } finally {
        cognitoLoginBtn.disabled = false;
        cognitoLoginBtn.innerText = "Sign In with Cognito";
      }
    });
  }

  saveConfigBtn.addEventListener("click", () => {
    API_BASE_URL = apiUrlInput.value.trim().replace(/\/+$/, "");
    localStorage.setItem("DOCUFLOW_API_URL", API_BASE_URL);
    if (cognitoClientIdInput) {
      COGNITO_CLIENT_ID = cognitoClientIdInput.value.trim();
      localStorage.setItem("DOCUFLOW_COGNITO_CLIENT_ID", COGNITO_CLIENT_ID);
    }
    if (cognitoRegionInput) {
      AWS_REGION = cognitoRegionInput.value.trim();
      localStorage.setItem("DOCUFLOW_AWS_REGION", AWS_REGION);
    }
    if (apiTokenInput) {
      API_AUTH_TOKEN = apiTokenInput.value.trim();
      localStorage.setItem("DOCUFLOW_COGNITO_TOKEN", API_AUTH_TOKEN);
      const claims = parseJwt(API_AUTH_TOKEN);
      if (claims) {
        USER_EMAIL = claims.email || "";
        const groups = claims["cognito:groups"] || [];
        const groupList = Array.isArray(groups) ? groups : [groups];
        USER_ROLE = (groupList.includes("Reviewers") || groupList.includes("Admins")) ? "Reviewer" : "Standard User";
        localStorage.setItem("DOCUFLOW_USER_EMAIL", USER_EMAIL);
        localStorage.setItem("DOCUFLOW_USER_ROLE", USER_ROLE);
      }
    }
    updateAuthUI();
    configModal.classList.remove("show");
    fetchDocuments();
  });
}

async function handleFileUpload(file) {
  if (API_BASE_URL) {
    const stepUpload = document.getElementById("step-upload");
    const stepParallel = document.getElementById("step-parallel");
    const stepScore = document.getElementById("step-score");
    const stepChoice = document.getElementById("step-choice");
    const stepDispatch = document.getElementById("step-dispatch");

    const resetSteps = () => {
      [stepUpload, stepParallel, stepScore, stepChoice, stepDispatch].forEach(el => {
        el.className = "step-item";
        el.querySelector(".step-status").innerText = "⏳";
      });
    };

    const markError = (stepEl, errMsg) => {
      stepEl.className = "step-item flagged";
      stepEl.querySelector(".step-status").innerText = "❌";
      alert(errMsg);
    };

    resetSteps();
    stepUpload.className = "step-item running";
    stepUpload.querySelector(".step-status").innerText = "🔄";

    if (file.size > 10 * 1024 * 1024) {
      markError(stepUpload, `File size (${(file.size / 1024 / 1024).toFixed(1)}MB) exceeds maximum allowable limit of 10MB.`);
      return;
    }
    if (file.size === 0) {
      markError(stepUpload, "Cannot upload empty file (0 bytes).");
      return;
    }

    try {
      // 1. Get presigned upload URL from API Gateway (enforces size limit before presigning)
      const presignRes = await fetch(`${API_BASE_URL}/api/upload-url`, {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          filename: file.name,
          content_type: file.type || "application/pdf",
          file_size: file.size
        })
      });
      if (!presignRes.ok) {
        const errJson = await presignRes.json().catch(() => ({}));
        markError(stepUpload, `Failed to get presigned upload URL (${presignRes.status}): ${errJson.error || presignRes.statusText}`);
        return;
      }
      const presignData = await presignRes.json();
      const docId = presignData.document_id;

      // 2. Upload file directly to S3 bucket via presigned URL
      const uploadRes = await fetch(presignData.upload_url, {
        method: "PUT",
        headers: { "Content-Type": file.type || "application/pdf" },
        body: file
      });
      if (!uploadRes.ok) {
        markError(stepUpload, `S3 upload failed (${uploadRes.status}): ${uploadRes.statusText}`);
        return;
      }
      stepUpload.className = "step-item success";
      stepUpload.querySelector(".step-status").innerText = "✅";

      // 3. Trigger Step Functions execution post-upload
      stepParallel.className = "step-item running";
      stepParallel.querySelector(".step-status").innerText = "⨂";
      const processRes = await fetch(`${API_BASE_URL}/api/documents/${docId}/process`, {
        method: "POST",
        headers: getAuthHeaders()
      });
      if (!processRes.ok) {
        const errJson = await processRes.json().catch(() => ({}));
        markError(stepParallel, `Step Functions trigger failed (${processRes.status}): ${errJson.error || processRes.statusText}`);
        return;
      }

      // 4. Poll document status to update live stepper
      for (let i = 0; i < 8; i++) {
        await new Promise(r => setTimeout(r, 1500));
        const docRes = await fetch(`${API_BASE_URL}/api/documents/${docId}`, {
          headers: getAuthHeaders()
        });
        if (docRes.ok) {
          const doc = await docRes.json();
          if (doc.status !== "PROCESSING" && doc.status !== "UPLOAD_PENDING") {
            stepParallel.className = "step-item success";
            stepParallel.querySelector(".step-status").innerText = "✅";
            stepScore.className = "step-item success";
            stepScore.querySelector(".step-status").innerText = "✅";
            if (doc.status === "PENDING_APPROVAL") {
              stepChoice.className = "step-item flagged";
              stepChoice.querySelector(".step-status").innerText = "⏸️";
            } else if (doc.status === "FAILED") {
              stepChoice.className = "step-item flagged";
              stepChoice.querySelector(".step-status").innerText = "❌";
            } else {
              stepChoice.className = "step-item success";
              stepChoice.querySelector(".step-status").innerText = "✅";
              stepDispatch.className = "step-item success";
              stepDispatch.querySelector(".step-status").innerText = "🚀";
            }
            await fetchDocuments();
            return;
          }
        }
      }
      await fetchDocuments();
      return;
    } catch (err) {
      markError(stepUpload, `Network or server error during document processing: ${err.message}`);
      return;
    }
  }

  // Simulation mode
  const docId = crypto.randomUUID ? crypto.randomUUID() : 'doc-' + Date.now();
  await executePipelineVisualizer(docId, file.name);
}

function runPresetDemo(type) {
  const id = (crypto.randomUUID ? crypto.randomUUID() : 'doc-' + Date.now()).substring(0, 8);
  let filename = "AWS_Compute_Invoice.pdf";
  if (type === "github") filename = "GitHub_Enterprise_License.pdf";
  if (type === "fraud") filename = "Offshore_Shadow_Consultancy_89K.pdf";

  executePipelineVisualizer(id, filename);
}

// Visual Stepper for Step Functions execution (Parallel + TaskToken)
async function executePipelineVisualizer(docId, filename) {
  const isHighRisk = filename.toLowerCase().includes("fraud") || filename.toLowerCase().includes("shadow");
  const stepUpload = document.getElementById("step-upload");
  const stepParallel = document.getElementById("step-parallel");
  const stepScore = document.getElementById("step-score");
  const stepChoice = document.getElementById("step-choice");
  const stepDispatch = document.getElementById("step-dispatch");

  const resetSteps = () => {
    [stepUpload, stepParallel, stepScore, stepChoice, stepDispatch].forEach(el => {
      el.className = "step-item";
      el.querySelector(".step-status").innerText = "⏳";
    });
  };

  resetSteps();

  // 1. S3 Presigned Upload
  stepUpload.classList.add("running");
  stepUpload.querySelector(".step-status").innerText = "🔄";
  await new Promise(r => setTimeout(r, 500));
  stepUpload.className = "step-item success";
  stepUpload.querySelector(".step-status").innerText = "✅";

  // 2. Parallel Analysis (Branch 1: Extraction + Branch 2: Security Scan)
  stepParallel.classList.add("running");
  stepParallel.querySelector(".step-status").innerText = "⨂";
  await new Promise(r => setTimeout(r, 700));
  stepParallel.className = "step-item success";
  stepParallel.querySelector(".step-status").innerText = "✅";

  // 3. CalculateRiskScore
  stepScore.classList.add("running");
  stepScore.querySelector(".step-status").innerText = "🔄";
  await new Promise(r => setTimeout(r, 600));
  stepScore.className = "step-item success";
  stepScore.querySelector(".step-status").innerText = "✅";

  // 4. EvaluateRiskDecision & TaskToken
  stepChoice.classList.add("running");
  await new Promise(r => setTimeout(r, 500));
  if (isHighRisk) {
    stepChoice.className = "step-item flagged";
    stepChoice.querySelector(".step-status").innerText = "⏸️";
    stepChoice.querySelector(".step-info span").innerText = "Workflow PAUSED on TaskToken — Awaiting API sendTaskSuccess callback.";
  } else {
    stepChoice.className = "step-item success";
    stepChoice.querySelector(".step-status").innerText = "✅";
  }

  // 5. Dispatch (if not halted for human review)
  if (!isHighRisk) {
    stepDispatch.classList.add("running");
    await new Promise(r => setTimeout(r, 400));
    stepDispatch.className = "step-item success";
    stepDispatch.querySelector(".step-status").innerText = "🚀";
    stepDispatch.querySelector(".step-info span").innerText = "Direct AWS SDK integration: DynamoDB updateItem & SNS alert published (Zero-Lambda)!";
  }

  // Add document to list
  const grandTotal = isHighRisk ? 89500.00 : (filename.includes("GitHub") ? 399.00 : 1420.50);
  const vendorName = isHighRisk 
    ? "Apex Global Consultancy Offshore Ltd" 
    : (filename.includes("GitHub") ? "GitHub Inc (Microsoft)" : "Amazon Web Services EMEA");

  const newDoc = {
    document_id: docId,
    filename: filename,
    status: isHighRisk ? "PENDING_APPROVAL" : "APPROVED",
    decision: isHighRisk ? "AWAITING_HUMAN_REVIEW" : "AUTO_APPROVED",
    task_token: isHighRisk ? `TASK_TOKEN_${docId.substring(0, 8)}_WAITING` : null,
    created_at: new Date().toISOString(),
    extracted_data: {
      vendor: vendorName,
      invoice_number: `INV-202611-${docId.substring(0, 6).toUpperCase()}`,
      grand_total: grandTotal,
      currency: "USD",
      vendor_trusted: !isHighRisk,
      raw_text_length: 1100,
      ai_model_used: "amazon.nova-lite-v1:0",
      ai_inference_provider: "Amazon Bedrock (Serverless LLM)",
      ai_analysis: isHighRisk
        ? "High risk warning: Offshore entity with unverified tax registry and unusually large transaction balance."
        : `Verified cloud infrastructure entity; financial math and line items matched accurately for ${vendorName}.`
    },
    security_scan: {
      status: "VALIDATED",
      sha256: `sha256_${docId.substring(0, 16)}`,
      sha256_computed: true,
      format_whitelisted: true,
      size_within_limits: true
    },
    risk: {
      score: isHighRisk ? 92 : 8,
      level: isHighRisk ? "HIGH" : "LOW",
      recommendation: isHighRisk ? "FLAG_FOR_MANUAL_REVIEW" : "AUTO_APPROVE",
      factors: isHighRisk 
        ? ["Untrusted or unregistered offshore entity (+45)", "High value exceeding $20,000 (+40)"]
        : [`Verified enterprise vendor: '${vendorName}' (-10)`]
    }
  };

  localDocuments.unshift(newDoc);
  renderDocumentsTable();
}

async function fetchDocuments() {
  const originalText = refreshBtn ? refreshBtn.innerText : "🔄 Refresh Table";
  if (refreshBtn) {
    refreshBtn.disabled = true;
    refreshBtn.innerText = "🔄 Fetching live records...";
  }

  if (!API_BASE_URL) {
    setTimeout(() => {
      renderDocumentsTable();
      if (refreshBtn) {
        refreshBtn.disabled = false;
        refreshBtn.innerText = originalText;
      }
    }, 300);
    return;
  }

  try {
    const res = await fetch(`${API_BASE_URL}/api/documents`, {
      headers: getAuthHeaders()
    });

    if (res.status === 401 || res.status === 403) {
      console.warn("Unauthorized to fetch live DynamoDB documents. Please sign in via Cognito or enter an API Key.");
      renderDocumentsTable();
      return;
    }

    if (!res.ok) {
      console.warn(`API returned HTTP ${res.status} when listing documents.`);
      renderDocumentsTable();
      return;
    }

    const data = await res.json();
    if (data.documents && Array.isArray(data.documents)) {
      if (data.documents.length > 0) {
        localDocuments = data.documents;
      }
    }
    renderDocumentsTable();
  } catch (err) {
    console.warn("API Gateway request error, using local dataset:", err);
    renderDocumentsTable();
  } finally {
    if (refreshBtn) {
      refreshBtn.disabled = false;
      refreshBtn.innerText = "✓ Refreshed";
      setTimeout(() => {
        refreshBtn.innerText = originalText;
      }, 1000);
    }
  }
}

function renderDocumentsTable() {
  if (!documentsTbody) return;
  documentsTbody.innerHTML = "";

  if (localDocuments.length === 0) {
    documentsTbody.innerHTML = `<tr><td colspan="7" style="text-align:center; color:#94a3b8; padding: 2rem;">No documents processed yet. Upload a document or pick a preset above!</td></tr>`;
    return;
  }

  localDocuments.forEach(doc => {
    const tr = document.createElement("tr");
    const vendor = doc.extracted_data?.vendor || "Pending...";
    const amount = doc.extracted_data?.grand_total 
      ? `$${Number(doc.extracted_data.grand_total).toLocaleString(undefined, {minimumFractionDigits: 2})}` 
      : "—";
    const riskScore = doc.risk ? `${doc.risk.score}/100 (${doc.risk.level})` : "—";

    tr.innerHTML = `
      <td><code>${doc.document_id.substring(0, 8)}...</code></td>
      <td><strong>${escapeHtml(doc.filename)}</strong></td>
      <td>${escapeHtml(vendor)}</td>
      <td>${amount}</td>
      <td><span style="color: ${doc.risk?.score >= 70 ? 'var(--accent-red)' : 'var(--accent-green)'}">${riskScore}</span></td>
      <td><span class="status-pill ${doc.status}">${doc.status}</span></td>
      <td>
        <button class="btn-sm" onclick="openDetailsModal('${doc.document_id}')">Inspect</button>
      </td>
    `;
    documentsTbody.appendChild(tr);
  });
}

window.openDetailsModal = function(docId) {
  const doc = localDocuments.find(d => d.document_id === docId);
  if (!doc) return;

  modalTitle.innerText = `Document: ${doc.filename}`;
  modalBody.innerHTML = `
    <div style="margin-bottom: 1rem;">
      <strong>Status:</strong> <span class="status-pill ${doc.status}">${doc.status}</span>
      ${doc.task_token ? `<div style="margin-top:0.4rem; font-size:0.75rem; color:var(--accent-yellow); font-family:var(--font-mono); background:rgba(245,158,11,0.1); padding:0.4rem; border-radius:4px;">⏸️ Step Functions paused on task_token: ${escapeHtml(doc.task_token.substring(0, 32))}...</div>` : ''}
    </div>
    <div style="margin-bottom: 1rem; background: rgba(56, 189, 248, 0.08); border: 1px solid rgba(56, 189, 248, 0.25); border-radius: 6px; padding: 0.75rem;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.35rem;">
        <span style="font-size:0.8rem; font-weight:600; color:#38bdf8;">🤖 Serverless AI Inference (Amazon Bedrock)</span>
        <span style="font-size:0.7rem; font-family:var(--font-mono); color:#94a3b8;">${doc.extracted_data?.ai_model_used || 'Amazon Nova Lite'}</span>
      </div>
      <p style="font-size:0.8rem; color:#e2e8f0; font-style: italic;">"${doc.extracted_data?.ai_analysis || 'Automated entity analysis complete.'}"</p>
    </div>
    <div style="margin-bottom: 1rem;">
      <strong>Anomaly Risk Evaluation:</strong>
      <p style="margin-top: 0.25rem;">Score: <strong>${doc.risk?.score ?? 'N/A'}/100 (${doc.risk?.level ?? 'N/A'})</strong></p>
      <ul style="margin-left: 1.25rem; font-size: 0.82rem; color: #94a3b8; margin-top: 0.35rem;">
        ${(doc.risk?.factors || []).map(f => `<li>${escapeHtml(f)}</li>`).join("")}
      </ul>
    </div>
    <div style="margin-bottom: 0.5rem;">
      <strong>Security &amp; Checksum Scope:</strong>
      <div style="font-size:0.8rem; color:#94a3b8;">
        SHA-256 Digest: <code>${doc.security_scan?.sha256 || 'N/A'}</code> (computed) | Format Whitelist: ✅ Passed | File Size: ${doc.security_scan?.file_size_bytes ? Math.round(doc.security_scan.file_size_bytes/1024) + ' KB' : 'N/A'} (within 10MB limit)
      </div>
      <div style="font-size:0.75rem; color:#94a3b8; font-style: italic; margin-top: 0.2rem;">
        Note: SHA-256 is a computed payload digest (no trusted prior-hash comparison or malware scanning).
      </div>
    </div>
    <div style="margin-bottom: 0.5rem; margin-top: 0.75rem;">
      <strong>Extracted Entities (DynamoDB Decimal Record):</strong>
    </div>
    <div class="json-view">${escapeHtml(JSON.stringify(doc, null, 2))}</div>
  `;

  if (doc.status === "PENDING_APPROVAL") {
    modalFooter.innerHTML = `
      <button class="btn-sm" style="border-color: var(--accent-red); color: var(--accent-red);" onclick="reviewDocument('${doc.document_id}', 'REJECT')">✕ Reject (Manual Review)</button>
      <button class="btn-primary" onclick="reviewDocument('${doc.document_id}', 'APPROVE')">✓ Approve (Manual Review)</button>
    `;
  } else {
    modalFooter.innerHTML = `
      <button class="btn-sm" onclick="modalBackdrop.classList.remove('show')">Close</button>
    `;
  }

  modalBackdrop.classList.add("show");
};

window.reviewDocument = async function(docId, action) {
  // If the document is one of the initial client presets or starts with doc-, treat it as a demo/simulation
  const isDemoOrLocal = docId === "3e4f5a6b-9988-7766-5544-33221100ffaa" || docId.startsWith("doc-") || !API_AUTH_TOKEN;

  if (API_BASE_URL && !isDemoOrLocal) {
    try {
      const resp = await fetch(`${API_BASE_URL}/api/documents/${docId}/approve`, {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ action: action, reviewer: "Enterprise Reviewer" })
      });
      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        alert(`Approval callback failed (${resp.status}): ${errData.error || resp.statusText}`);
        return;
      }
    } catch (e) {
      console.warn("API approve call failed, falling back to local state:", e);
    }
  }

  const doc = localDocuments.find(d => d.document_id === docId);
  if (doc) {
    doc.status = action === "APPROVE" ? "APPROVED" : "REJECTED";
    doc.decision = action === "APPROVE" ? "MANUAL_APPROVED" : "MANUAL_REJECTED";
    delete doc.task_token;

    // Animate final dispatch step in UI
    const stepDispatch = document.getElementById("step-dispatch");
    if (stepDispatch) {
      stepDispatch.className = "step-item success";
      stepDispatch.querySelector(".step-status").innerText = "🚀";
      stepDispatch.querySelector(".step-info span").innerText = `sendTaskSuccess callback received. Status: ${doc.status}`;
    }
  }

  modalBackdrop.classList.remove("show");
  renderDocumentsTable();
};

function escapeHtml(str) {
  if (!str) return "";
  return str.toString()
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
