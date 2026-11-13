# 🎬 3-Minute Demo Video Script: DocuFlow AI
### AWS Builder Center Weekend Challenge: "No Pipeline, No Problem"

**Target Duration**: 2 minutes 45 seconds to 3 minutes  
**Format**: Screen recording with voiceover (Loom, OBS, or YouTube Unlisted)

---

### [0:00 - 0:30] Hook & Problem Statement
* **Visual**: Show the DocuFlow AI title banner and architectural diagram in `README.md`.
* **Voiceover**:
> *"Hi everyone! Welcome to our submission for the AWS Builder Center 'No Pipeline, No Problem' Weekend Challenge: **DocuFlow AI**.  
> In modern enterprises, processing invoices and financial documents usually means babysitting container clusters, maintaining fragile polling queues, and dealing with broken asynchronous approvals.  
> We asked: What if you could ingest, parse, validate, risk-score, and coordinate human approvals with **zero servers to manage and zero operational pipelines**? That's exactly what DocuFlow AI delivers."*

---

### [0:30 - 1:15] Serverless Architecture Breakdown
* **Visual**: Zoom in on the architecture visualizer ribbon on the web dashboard.
* **Voiceover**:
> *"Our architecture leverages core AWS serverless services in a resilient, event-driven flow:  
> 1. **Amazon S3** provides direct-to-bucket upload using secure, time-limited presigned URLs — compute is never blocked while transferring large files.  
> 2. **Amazon API Gateway** handles incoming HTTP requests with built-in CORS and zero load-balancer overhead.  
> 3. **Amazon Bedrock** provides serverless generative AI inference — invoking Claude 3 Haiku on-demand to extract structured fields and analyze fraud risk without managing GPU clusters or model pipelines.  
> 4. **AWS Step Functions** serves as our central orchestration nervous system. Instead of a linear script, it executes a **Parallel State** running Bedrock AI extraction and security scanning concurrently, with native exponential retries.  
> 5. For high-risk documents, Step Functions utilizes **`waitForTaskToken`** via **Amazon SNS** — pausing execution completely at zero compute cost until a human approves via API Gateway!  
> 6. **Direct AWS SDK Integrations**: Step Functions writes directly to **Amazon DynamoDB** and publishes alerts to **Amazon SNS** without any glue Lambda functions.  
> 7. **AWS X-Ray** provides distributed tracing across the entire pipeline, generating an automatic visual service map from API Gateway down to Bedrock and DynamoDB."*

---

### [1:15 - 2:15] Live Demo: Safe vs Anomaly Workflow
* **Visual**: Show `frontend/index.html` in action.

* **Scenario A: Clean Enterprise Invoice (Auto-Approved)**
  - Click **"☁️ AWS Cloud Invoice ($1,420.50)"**.
  - Show the Step Functions stepper animate through:
    1. S3 Presigned Upload & Verification
    2. `ParallelAnalysis` (Branch A: Entity Extraction ⨂ Branch B: Security & Compliance Scan)
    3. `CalculateRiskScore` (Merges parallel results, Low risk: 5/100)
    4. `EvaluateRiskDecision` (Choice State auto-routes to green)
    5. Direct SDK: DynamoDB updateItem & SNS alert published (Status: `APPROVED`)
  - Show the table auto-updating with the new record.

* **Scenario B: Fraud / High-Risk Anomaly (TaskToken Suspension & Resumption)**
  - Click **"⚠️ Offshore Shadow Vendor ($89,500.00)"**.
  - Point out the stepper:
    - Vendor 'Apex Global Consultancy Offshore Ltd' is untrusted (+45 risk)
    - Invoice amount is $89,500.00 (+40 risk)
    - Risk Score reaches 95/100!
    - The Step Functions Choice State detects score >= 70, branches to `RequestManualApproval` using **`waitForTaskToken`**, and **pauses execution**!
  - Click **Inspect** on the table row.
  - Show the stored `task_token` and risk factors.
  - Click **"✓ Approve (sendTaskSuccess)"**.
  - Show the API Gateway callback sending `SendTaskSuccess(TaskToken)` directly to Step Functions — the state machine instantly wakes up, finalizes the audit record, and completes the workflow!

---

### [2:15 - 2:45] "No Pipeline, No Problem" & SAM Deployment
* **Visual**: Open terminal / VS Code, show `template.yaml` and `statemachine/docuflow.asl.json`.
* **Voiceover**:
> *"Everything you see here is packaged into an infrastructure-as-code **AWS SAM template**. With a single command — `sam build && sam deploy` — the S3 buckets, DynamoDB single-table schema, API routes, Lambda functions, and Step Functions state machine are automatically spun up.  
> When there are no invoices coming in, our compute cost is literally **$0.00**. When 10,000 invoices arrive at once, it scales instantly and handles every single one.  
> That is true serverless engineering — **No Pipeline, No Problem**."*

---

### [2:45 - 3:00] Conclusion & Submission Links
* **Visual**: Show GitHub repo link and live demo URL.
* **Voiceover**:
> *"Thank you to the AWS Builder Center team! You can find the full source code, architecture writeup, and unit test suite in our repository. Thank you for watching!"*
