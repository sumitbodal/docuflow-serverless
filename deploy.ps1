# DocuFlow AI - Automated Deployment Script
# AWS Builder Center: "No Pipeline, No Problem" Weekend Challenge

Write-Host "`n⚡ ========================================================" -ForegroundColor Cyan
Write-Host "   DocuFlow AI: Serverless Stack Deployment to AWS" -ForegroundColor Cyan
Write-Host "========================================================`n" -ForegroundColor Cyan

# 1. Check prerequisites
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    Write-Host "❌ AWS CLI not found in PATH." -ForegroundColor Red
    Write-Host "👉 Install it via: winget install Amazon.AWSCLI" -ForegroundColor Yellow
    Write-Host "   Then run: aws configure" -ForegroundColor Yellow
    exit 1
}

if (-not (Get-Command sam -ErrorAction SilentlyContinue)) {
    Write-Host "❌ AWS SAM CLI not found in PATH." -ForegroundColor Red
    Write-Host "👉 Install it via: winget install Amazon.SAMCLI" -ForegroundColor Yellow
    exit 1
}

# 2. Build SAM stack
Write-Host "📦 Step 1/3: Building Serverless Application with SAM..." -ForegroundColor Green
sam build
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Build failed. Please check errors above." -ForegroundColor Red
    exit 1
}

# 3. Deploy SAM stack
Write-Host "`n🚀 Step 2/3: Deploying CloudFormation Stack to AWS..." -ForegroundColor Green
sam deploy `
    --stack-name docuflow-ai `
    --resolve-s3 `
    --capabilities CAPABILITY_IAM `
    --no-confirm-changeset

if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Deployment failed." -ForegroundColor Red
    exit 1
}

# 4. Extract Stack Outputs
Write-Host "`n🔍 Step 3/3: Querying Stack Outputs & Syncing Web UI..." -ForegroundColor Green
$outputs = aws cloudformation describe-stacks `
    --stack-name docuflow-ai `
    --query "Stacks[0].Outputs" `
    --output json | ConvertFrom-Json

$apiUrl = ($outputs | Where-Object { $_.OutputKey -eq "HttpApiUrl" }).OutputValue
$webUrl = ($outputs | Where-Object { $_.OutputKey -eq "WebDashboardUrl" }).OutputValue
$frontendBucket = ($outputs | Where-Object { $_.OutputKey -eq "FrontendBucketName" }).OutputValue

if ($frontendBucket) {
    Write-Host "📤 Syncing frontend files to S3 bucket: $frontendBucket..." -ForegroundColor Yellow
    aws s3 sync frontend/ "s3://$frontendBucket/" --delete
}

Write-Host "`n🎉 ========================================================" -ForegroundColor Green
Write-Host "   DEPLOYMENT COMPLETE! YOUR APP IS LIVE ON AWS" -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Green
Write-Host "🌐 Live Web Dashboard: $webUrl" -ForegroundColor Cyan
Write-Host "🔗 API Gateway URL:    $apiUrl" -ForegroundColor Cyan
Write-Host "🗄️ Documents Bucket:   $frontendBucket" -ForegroundColor Gray
Write-Host "`nNext Step: Record your 3-minute video using submission/DEMO_SCRIPT.md!`n" -ForegroundColor Yellow
