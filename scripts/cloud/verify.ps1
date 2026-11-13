[CmdletBinding()]
param(
    [string]$Project = 'synthetic-release-gate-2026',
    [string]$Location = 'US',
    [string]$Report = 'artifacts/iam_verification_report.json',
    [switch]$IncludeAuditDataset
)

$ErrorActionPreference = 'Stop'
$gcloud = (Get-Command gcloud.cmd -ErrorAction Stop).Source
$bq = (Get-Command bq.cmd -ErrorAction Stop).Source
$table = '_srg_iam_probe'
$operator = (& $gcloud auth list --filter='status:ACTIVE' --format='value(account)').Trim()
if (-not $operator -or $operator -match "`n|`r") { throw 'Exactly one active operator account is required.' }

$accounts = @{
    evaluator = "srg-evaluator@$Project.iam.gserviceaccount.com"
    publisher = "srg-publisher@$Project.iam.gserviceaccount.com"
    consumer = "srg-consumer@$Project.iam.gserviceaccount.com"
}
$results = [System.Collections.Generic.List[object]]::new()
$cleanupTables = [System.Collections.Generic.List[string]]::new()

function Get-AccessToken([string]$Identity = '') {
    if ($Identity) { $token = (& $gcloud auth print-access-token "--impersonate-service-account=$Identity").Trim() }
    else { $token = (& $gcloud auth print-access-token).Trim() }
    if (-not $token -or $token -match "`n|`r") { throw 'Authentication failed; the operation was not counted.' }
    return $token
}

function Add-Result([string]$Name, [string]$Expected, [int]$Status) {
    $results.Add([pscustomobject]@{ name = $Name; expected = $Expected; observed = $Expected; status = $Status }) | Out-Null
    Write-Output "$Name $Expected HTTP $Status"
}

function Invoke-SqlAs([string]$Identity, [string]$Sql, [string]$Name, [bool]$ExpectAllowed, [string]$ExpectedPayload = '') {
    $token = Get-AccessToken $Identity
    $body = @{ query = $Sql; useLegacySql = $false; location = $Location; timeoutMs = 30000; maximumBytesBilled = '10485760'; useQueryCache = $true } | ConvertTo-Json -Compress
    try {
        $response = Invoke-RestMethod -Method Post -Uri "https://bigquery.googleapis.com/bigquery/v2/projects/$Project/queries" -Headers @{ Authorization = "Bearer $token" } -ContentType 'application/json' -Body $body
        if (-not $response.jobComplete) { throw "SQL job did not complete for $Name." }
        if ($response.errorResult) { throw "SQL job failed for ${Name}: $($response.errorResult.message)" }
        if (-not $ExpectAllowed) { throw "Unexpected authorization success for $Name." }
        if ($ExpectedPayload) {
            $rows = @($response.rows)
            if ($response.totalRows -ne '1' -or $rows.Count -ne 1) { throw "Unexpected row count for $Name." }
            $values = @($rows[0].f | ForEach-Object { [string]$_.v })
            if ($values.Count -ne 2 -or $values[0] -ne '2026' -or $values[1] -ne $ExpectedPayload) { throw "Unexpected fixture row for ${Name}: $($values -join ',')." }
        }
        Add-Result $Name 'ALLOW' 200
    } catch {
        $status = $null
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        if ($ExpectAllowed) { throw "Expected allowed SQL operation failed for $Name (HTTP $status): $($_.Exception.Message)" }
        if ($status -ne 403) { throw "Negative test $Name was not an explicit authorization denial (HTTP $status)." }
        Add-Result $Name 'DENY' $status
    }
}

function Invoke-SqlAdmin([string]$Sql) {
    $token = Get-AccessToken
    $body = @{ query = $Sql; useLegacySql = $false; location = $Location; timeoutMs = 30000; maximumBytesBilled = '10485760' } | ConvertTo-Json -Compress
    $response = Invoke-RestMethod -Method Post -Uri "https://bigquery.googleapis.com/bigquery/v2/projects/$Project/queries" -Headers @{ Authorization = "Bearer $token" } -ContentType 'application/json' -Body $body
    if (-not $response.jobComplete) { throw 'Administrator fixture SQL did not complete.' }
    if ($response.errorResult) { throw "Administrator fixture SQL failed: $($response.errorResult.message)" }
}

function Add-CleanupTable([string]$Dataset, [string]$TableName) { $cleanupTables.Add("$Project`:$Dataset.$TableName") | Out-Null }

function Wait-TableVisible([string]$Dataset, [string]$TableName) {
    $token = Get-AccessToken
    $uri = "https://bigquery.googleapis.com/bigquery/v2/projects/$Project/datasets/$Dataset/tables/$TableName"
    for ($attempt = 1; $attempt -le 15; $attempt++) {
        try {
            Invoke-RestMethod -Method Get -Uri $uri -Headers @{ Authorization = "Bearer $token" } | Out-Null
            return
        } catch {
            if ($attempt -eq 15) { throw "Temporary fixture $Dataset.$TableName did not become visible." }
            Start-Sleep -Seconds 2
        }
    }
}

try {
    Invoke-SqlAdmin "CREATE OR REPLACE TABLE ``$Project.source_private.$table`` AS SELECT 2026 AS probe_id, 'source_private' AS payload"
    Invoke-SqlAdmin "CREATE OR REPLACE TABLE ``$Project.candidate_private.$table`` AS SELECT 2026 AS probe_id, 'candidate_private' AS payload"
    Invoke-SqlAdmin "CREATE OR REPLACE TABLE ``$Project.published_synthetic.$table`` AS SELECT 2026 AS probe_id, 'published_synthetic' AS payload"
    Wait-TableVisible 'source_private' $table
    Wait-TableVisible 'candidate_private' $table
    Wait-TableVisible 'published_synthetic' $table
    foreach ($dataset in @('source_private', 'candidate_private', 'published_synthetic')) { Add-CleanupTable $dataset $table }

    $readCases = @(
        @{ name = 'evaluator-read-source'; identity = $accounts.evaluator; dataset = 'source_private'; allowed = $true; payload = 'source_private' }
        @{ name = 'evaluator-read-candidate'; identity = $accounts.evaluator; dataset = 'candidate_private'; allowed = $true; payload = 'candidate_private' }
        @{ name = 'evaluator-read-published'; identity = $accounts.evaluator; dataset = 'published_synthetic'; allowed = $false; payload = '' }
        @{ name = 'publisher-read-source'; identity = $accounts.publisher; dataset = 'source_private'; allowed = $false; payload = '' }
        @{ name = 'publisher-read-candidate'; identity = $accounts.publisher; dataset = 'candidate_private'; allowed = $true; payload = 'candidate_private' }
        @{ name = 'publisher-read-published'; identity = $accounts.publisher; dataset = 'published_synthetic'; allowed = $true; payload = 'published_synthetic' }
        @{ name = 'consumer-read-source'; identity = $accounts.consumer; dataset = 'source_private'; allowed = $false; payload = '' }
        @{ name = 'consumer-read-candidate'; identity = $accounts.consumer; dataset = 'candidate_private'; allowed = $false; payload = '' }
        @{ name = 'consumer-read-published'; identity = $accounts.consumer; dataset = 'published_synthetic'; allowed = $true; payload = 'published_synthetic' }
    )
    foreach ($case in $readCases) { Invoke-SqlAs $case.identity "SELECT probe_id, payload FROM ``$Project.$($case.dataset).$table`` WHERE probe_id = 2026" $case.name $case.allowed $case.payload }

    $writeCases = @(
        @{ name = 'evaluator-write-candidate'; identity = $accounts.evaluator; dataset = 'candidate_private'; allowed = $true }
        @{ name = 'evaluator-write-published'; identity = $accounts.evaluator; dataset = 'published_synthetic'; allowed = $false }
        @{ name = 'publisher-write-published'; identity = $accounts.publisher; dataset = 'published_synthetic'; allowed = $true }
        @{ name = 'publisher-write-candidate'; identity = $accounts.publisher; dataset = 'candidate_private'; allowed = $false }
        @{ name = 'consumer-write-published'; identity = $accounts.consumer; dataset = 'published_synthetic'; allowed = $false }
    )
    foreach ($case in $writeCases) {
        $writeTable = "_srg_write_$($case.name -replace '-', '_')"
        Add-CleanupTable $case.dataset $writeTable
        Invoke-SqlAs $case.identity "CREATE TABLE ``$Project.$($case.dataset).$writeTable`` AS SELECT 2026 AS probe_id, '$($case.name)' AS payload" $case.name $case.allowed
    }

    if ($IncludeAuditDataset) {
        Invoke-SqlAdmin "CREATE OR REPLACE TABLE ``$Project.audit_private.$table`` AS SELECT 2026 AS probe_id, 'audit_private' AS payload"
        Add-CleanupTable 'audit_private' $table
        Wait-TableVisible 'audit_private' $table
        Invoke-SqlAs $accounts.evaluator "SELECT 1 FROM ``$Project.audit_private.$table`` LIMIT 1" 'evaluator-read-audit' $false
        Invoke-SqlAs $accounts.consumer "SELECT 1 FROM ``$Project.audit_private.$table`` LIMIT 1" 'consumer-read-audit' $false
        $auditWriteTable = '_srg_write_publisher_audit'
        Add-CleanupTable 'audit_private' $auditWriteTable
        Invoke-SqlAs $accounts.publisher "CREATE TABLE ``$Project.audit_private.$auditWriteTable`` AS SELECT 2026 AS probe_id" 'publisher-write-audit' $true
    }

    $reportDocument = [ordered]@{
        project = $Project; location = $Location; operator = $operator; identities = $accounts
        sql_read_tests = 9; audit_tests_executed = [bool]$IncludeAuditDataset
        results = @($results); all_tests_passed = $true
        notes = @('Negative tests pass only on HTTP 403.', 'SQL jobs use maximumBytesBilled=10485760, the BigQuery minimum accepted limit.', 'BigQuery-managed anonymous query results are not manually deleted and expire under BigQuery retention.')
    }
    $reportPath = [System.IO.Path]::GetFullPath($Report)
    $parent = Split-Path -Parent $reportPath
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    [System.IO.File]::WriteAllText($reportPath, ($reportDocument | ConvertTo-Json -Depth 10), [System.Text.UTF8Encoding]::new($false))
    Write-Output "SQL IAM verification passed: $($results.Count) checks; report: $reportPath"
} finally {
    foreach ($reference in $cleanupTables) { & $bq rm -f -t $reference *> $null }
}
