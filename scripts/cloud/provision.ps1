[CmdletBinding()]
param(
    [string]$Project = 'synthetic-release-gate-2026',
    [string]$Location = 'US',
    [string]$Operator = '',
    [switch]$IncludeAuditDataset
)

$ErrorActionPreference = 'Stop'
$gcloud = (Get-Command gcloud.cmd -ErrorAction Stop).Source
$bq = (Get-Command bq.cmd -ErrorAction Stop).Source

function Invoke-Checked([string]$Command, [string[]]$Arguments) {
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Command $($Arguments -join ' ')" }
}

function Test-ServiceAccountExists([string]$Email) {
    $savedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $gcloud iam service-accounts describe $Email "--project=$Project" 2>&1)
    } finally {
        $ErrorActionPreference = $savedErrorActionPreference
    }
    if ($LASTEXITCODE -eq 0) {
        return $true
    }

    $message = ($output | ForEach-Object { $_.ToString() }) -join ' '
    if ($message -match '(?i)(not[ _-]?found|does not exist|404)') {
        return $false
    }
    if ($message -match '(?i)(permission[ _-]?denied|access denied|403|unauthorized)') {
        throw "Permission denied while inspecting service account $Email`: $message"
    }
    throw "Unexpected service-account lookup failure for $Email`: $message"
}

function Wait-ServiceAccountReady([string]$Email) {
    for ($attempt = 1; $attempt -le 15; $attempt++) {
        if (Test-ServiceAccountExists $Email) {
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "Service account $Email was created but did not become readable within the consistency window."
}

function Get-DatasetJson([string]$Reference) {
    $savedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $bq show --format=prettyjson $Reference 2>&1)
    } finally {
        $ErrorActionPreference = $savedErrorActionPreference
    }
    if ($LASTEXITCODE -eq 0) {
        $json = ($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
        return ($json | ConvertFrom-Json)
    }

    $message = ($output | ForEach-Object { $_.ToString() }) -join ' '
    if ($message -match '(?i)(not[ _-]?found|does not exist|404)') {
        return $null
    }
    if ($message -match '(?i)(permission[ _-]?denied|access denied|403|unauthorized)') {
        throw "Permission denied while reading dataset $Reference`: $message"
    }
    throw "Unexpected bq show failure for $Reference`: $message"
}

function Get-AccessKey($Entry) {
    $parts = @(
        "role=$($Entry.role)",
        "specialGroup=$($Entry.specialGroup)",
        "userByEmail=$($Entry.userByEmail)",
        "groupByEmail=$($Entry.groupByEmail)",
        "domain=$($Entry.domain)",
        "view=$($Entry.view | ConvertTo-Json -Compress -Depth 20)",
        "routine=$($Entry.routine | ConvertTo-Json -Compress -Depth 20)"
    )
    return ($parts -join '|')
}

function Assert-AccessConfiguration($DatasetJson, $Reference, $ExpectedEntries, $OriginalPreservedEntries, $ManagedEmails) {
    $actualEntries = @($DatasetJson.access)
    $broadDefaults = @($actualEntries | Where-Object {
        $_.specialGroup -in @('projectReaders', 'projectWriters')
    })
    if ($broadDefaults.Count -ne 0) {
        throw "Dataset ACL validation failed for $Reference`: projectReaders/projectWriters entries remain."
    }

    $expectedKeys = @($ExpectedEntries | ForEach-Object { Get-AccessKey $_ })
    $unexpectedManaged = @($actualEntries | Where-Object {
        $_.userByEmail -and $ManagedEmails -contains $_.userByEmail -and
        $expectedKeys -notcontains (Get-AccessKey $_)
    })
    if ($unexpectedManaged.Count -gt 0) {
        throw "Dataset ACL validation failed for $Reference`: unexpected managed service-account entry exists."
    }

    foreach ($entry in $OriginalPreservedEntries) {
        $key = Get-AccessKey $entry
        $count = @($actualEntries | Where-Object { (Get-AccessKey $_) -eq $key }).Count
        if ($count -lt 1) {
            throw "Dataset ACL validation failed for $Reference`: an unrelated existing ACL entry was not preserved."
        }
    }

    foreach ($expected in $ExpectedEntries) {
        $key = Get-AccessKey $expected
        $count = @($actualEntries | Where-Object { (Get-AccessKey $_) -eq $key }).Count
        if ($count -ne 1) {
            throw "Dataset ACL validation failed for $Reference`: expected exactly one $key entry, found $count."
        }
    }
}

if (-not $Operator) {
    $Operator = (& $gcloud auth list --filter='status:ACTIVE' --format='value(account)').Trim()
}
if (-not $Operator -or $Operator -match "`n|`r") { throw 'Exactly one active operator account is required.' }
if ($Project -cne 'synthetic-release-gate-2026') {
    throw 'Provisioning is restricted to project synthetic-release-gate-2026.'
}
if ($Location -cne 'US') {
    throw 'Provisioning is restricted to BigQuery location US.'
}

$projectInfo = (& $gcloud projects describe $Project --format='json(projectId,lifecycleState,parent)').Trim() | ConvertFrom-Json
if ($projectInfo.projectId -ne $Project -or $projectInfo.lifecycleState -ne 'ACTIVE') {
    throw "Target project is not active: $Project"
}

# Enable only APIs needed for service-account creation and short-lived impersonation.
$enabled = @(& $gcloud services list --project=$Project --enabled --format='value(config.name)')
foreach ($service in @('iam.googleapis.com', 'iamcredentials.googleapis.com')) {
    if ($enabled -notcontains $service) {
        Invoke-Checked $gcloud @('services', 'enable', $service, "--project=$Project")
    }
}

$serviceAccounts = @('srg-evaluator', 'srg-publisher', 'srg-consumer')
$emails = @{}
foreach ($name in $serviceAccounts) {
    $email = "$name@$Project.iam.gserviceaccount.com"
    if (-not (Test-ServiceAccountExists $email)) {
        Invoke-Checked $gcloud @('iam', 'service-accounts', 'create', $name, "--project=$Project", '--display-name', $name)
        Wait-ServiceAccountReady $email
    }
    $emails[$name] = $email
    Invoke-Checked $gcloud @('projects', 'add-iam-policy-binding', $Project,
        '--member', "serviceAccount:$email", '--role', 'roles/bigquery.jobUser', '--condition=None')
    Invoke-Checked $gcloud @('iam', 'service-accounts', 'add-iam-policy-binding', $email,
        '--project', $Project, '--member', "user:$Operator", '--role', 'roles/iam.serviceAccountTokenCreator')
}

$datasets = @('source_private', 'candidate_private', 'published_synthetic')
if ($IncludeAuditDataset) {
    $datasets += 'audit_private'
}
foreach ($dataset in $datasets) {
    $reference = "$Project`:$dataset"
    $datasetJson = Get-DatasetJson $reference
    if ($null -eq $datasetJson) {
        Invoke-Checked $bq @('mk', '--dataset', "--data_location=$Location", $reference)
        $datasetJson = Get-DatasetJson $reference
        if ($null -eq $datasetJson) {
            throw "Dataset creation reported success but $reference could not be read back."
        }
    }

    # Remove only broad legacy defaults. Preserve projectOwners, the creator,
    # and every other existing ACL entry. The managed service-account entries
    # are added only when absent, so reruns cannot create duplicates.
    $access = @($datasetJson.access)
    $preserved = @($access | Where-Object {
        $_.specialGroup -notin @('projectReaders', 'projectWriters')
    })

    $desired = @()
    if ($dataset -eq 'source_private') {
        $desired += [pscustomobject]@{ role = 'READER'; userByEmail = $emails['srg-evaluator'] }
    } elseif ($dataset -eq 'candidate_private') {
        $desired += [pscustomobject]@{ role = 'WRITER'; userByEmail = $emails['srg-evaluator'] }
        $desired += [pscustomobject]@{ role = 'READER'; userByEmail = $emails['srg-publisher'] }
    } elseif ($dataset -eq 'published_synthetic') {
        $desired += [pscustomobject]@{ role = 'WRITER'; userByEmail = $emails['srg-publisher'] }
        $desired += [pscustomobject]@{ role = 'READER'; userByEmail = $emails['srg-consumer'] }
    } elseif ($dataset -eq 'audit_private') {
        # Publisher WRITER is intentional but is not append-only or immutable.
        # Evaluator and consumer receive no dataset ACL entries.
        $desired += [pscustomobject]@{ role = 'WRITER'; userByEmail = $emails['srg-publisher'] }
    }

    $finalAccess = @($preserved)
    foreach ($expected in $desired) {
        $key = Get-AccessKey $expected
        $matches = @($finalAccess | Where-Object { (Get-AccessKey $_) -eq $key })
        if ($matches.Count -gt 1) {
            throw "Duplicate managed ACL entry already exists for $reference`: $key"
        }
        if ($matches.Count -eq 0) {
            $finalAccess += $expected
        }
    }

    $desiredKeys = @($desired | ForEach-Object { Get-AccessKey $_ })
    $managedEmails = @($emails.Values)
    $unexpectedManaged = @($finalAccess | Where-Object {
        $_.userByEmail -and $managedEmails -contains $_.userByEmail -and
        $desiredKeys -notcontains (Get-AccessKey $_)
    })
    if ($unexpectedManaged.Count -gt 0) {
        throw "Unexpected managed service-account ACL entry exists for $reference."
    }

    $finalKeys = @($finalAccess | ForEach-Object { Get-AccessKey $_ } | Sort-Object)
    $accessChanged = (($access | ForEach-Object { Get-AccessKey $_ } | Sort-Object) -join "`n") -ne ($finalKeys -join "`n")
    if ($accessChanged) {
        $temp = Join-Path ([System.IO.Path]::GetTempPath()) ("srg-$dataset-access.json")
        $datasetJson.access = @($finalAccess)
        $json = $datasetJson | ConvertTo-Json -Depth 20
        [System.IO.File]::WriteAllText(
            $temp,
            $json,
            (New-Object System.Text.UTF8Encoding($false))
        )
        try {
            Invoke-Checked $bq @('update', '--dataset', '--source', $temp, $reference)
        } finally {
            Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
        }
    }

    $updatedDatasetJson = Get-DatasetJson $reference
    if ($null -eq $updatedDatasetJson) {
        throw "Dataset $reference disappeared during ACL update."
    }
    Assert-AccessConfiguration $updatedDatasetJson $reference $desired $preserved $managedEmails
}

Write-Output "Provisioning complete for $Project in $Location. Operator impersonation: $Operator"
