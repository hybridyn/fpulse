# -----------------------------------------------------------------------------
#  F-Pulse — Test User Seeder (4-Role RBAC)
#
#  Creates one test account per canonical role so you can validate every
#  permission boundary end-to-end:
#
#      +--------------------------+-------------------+-------------------+
#      | Email                    | Password          | Role              |
#      +--------------------------+-------------------+-------------------+
#      | sa@fpulse.local          | SuperAdmin1!      | super_admin       |
#      | admin@fpulse.local       | admin             | super_admin (seed)|
#      | manager@fpulse.local     | Manager1!         | admin             |
#      | developer@fpulse.local   | Developer1!       | developer         |
#      | viewer@fpulse.local      | Viewer1!          | viewer            |
#      +--------------------------+-------------------+-------------------+
#
#  Usage:
#    .\seed-test-users.ps1                  # create 4 test users
#    .\seed-test-users.ps1 -ActivatePlus    # + activate 10-seat Plus license
#    .\seed-test-users.ps1 -DeactivatePlus  # revert to free tier
#    .\seed-test-users.ps1 -Clean           # delete all test users & start fresh
#
#  The seeded admin@fpulse.local (password: admin) is created by the backend
#  on first boot. This script adds 4 more accounts for testing.
#
#  Idempotent: re-running will not duplicate users.
# -----------------------------------------------------------------------------

param(
    [string]$BaseUrl = "http://localhost:8001",
    [string]$AdminEmail = "admin@fpulse.local",
    [string]$AdminPassword = "admin",
    [switch]$ActivatePlus,
    [switch]$DeactivatePlus,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

function Write-Header($text) {
    Write-Host ""
    Write-Host "=== $text ===" -ForegroundColor Cyan
}
function Write-Ok($text)   { Write-Host "  [OK] $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "  [!]  $text" -ForegroundColor Yellow }
function Write-Err($text)  { Write-Host "  [X]  $text" -ForegroundColor Red }

function Invoke-Api {
    param(
        [string]$Method,
        [string]$Path,
        [hashtable]$Body,
        [string]$Token
    )
    $headers = @{ "Content-Type" = "application/json" }
    if ($Token) { $headers["Authorization"] = "Bearer $Token" }

    $params = @{
        Method  = $Method
        Uri     = "$BaseUrl$Path"
        Headers = $headers
    }
    if ($Body) { $params.Body = ($Body | ConvertTo-Json -Depth 6) }

    try {
        return Invoke-RestMethod @params
    } catch {
        $msg = $_.ErrorDetails.Message
        if ($msg) {
            try { $msg = ($msg | ConvertFrom-Json).detail } catch {}
        }
        if (-not $msg) { $msg = $_.Exception.Message }
        throw "API $Method $Path failed: $msg"
    }
}

# --- 1. Check backend is running ---
Write-Header "Checking backend at $BaseUrl"
try {
    $health = Invoke-RestMethod -Uri "$BaseUrl/api/health" -Method Get -TimeoutSec 5
    Write-Ok "Backend is up"
} catch {
    Write-Err "Cannot reach backend at $BaseUrl. Is it running?"
    Write-Err $_.Exception.Message
    exit 1
}

# --- 2. Login as bootstrap super_admin ---
Write-Header "Authenticating as $AdminEmail"
try {
    $auth = Invoke-Api -Method Post -Path "/api/auth/login" -Body @{
        email    = $AdminEmail
        password = $AdminPassword
    }
    $adminToken = $auth.token
    Write-Ok "Logged in as $($auth.user.email) [role=$($auth.user.role)]"
} catch {
    Write-Err "Login failed for $AdminEmail. Check password."
    Write-Err $_
    exit 1
}

# --- 3. (Optional) license management ---
if ($ActivatePlus) {
    Write-Header "Activating Plus license (10 seats)"
    try {
        $expires = (Get-Date).AddYears(1).ToString("yyyy-MM-ddTHH:mm:ss")
        $resp = Invoke-Api -Method Post -Path "/api/plus/license/activate" -Token $adminToken -Body @{
            org        = "F-Pulse Test Organization"
            email      = "test@fpulse.local"
            tier       = "plus"
            seats      = 10
            expires_at = $expires
        }
        Write-Ok "Plus license activated: $($resp.tier) | $($resp.seats) seats"
    } catch {
        Write-Err "License activation failed: $_"
    }
}

if ($DeactivatePlus) {
    Write-Header "Deactivating Plus license"
    try {
        $resp = Invoke-Api -Method Post -Path "/api/plus/license/deactivate" -Token $adminToken
        Write-Ok "License deactivated: tier=$($resp.tier)"
    } catch {
        Write-Err "Deactivation failed: $_"
    }
}

# --- 4. (Optional) Clean existing test users ---
if ($Clean) {
    Write-Header "Cleaning existing test users"
    $cleanEmails = @("sa@fpulse.local", "manager@fpulse.local", "developer@fpulse.local", "viewer@fpulse.local",
                     "dev_free@fpulse.local", "adm_free@fpulse.local", "dev_plus@fpulse.local", "adm_plus@fpulse.local")
    try {
        $allUsers = Invoke-Api -Method Get -Path "/api/auth/users" -Token $adminToken
        foreach ($email in $cleanEmails) {
            $existing = $allUsers | Where-Object { $_.email -eq $email } | Select-Object -First 1
            if ($existing) {
                try {
                    Invoke-Api -Method Delete -Path "/api/auth/users/$($existing.id)" -Token $adminToken | Out-Null
                    Write-Ok "Deleted $email"
                } catch {
                    Write-Warn "Could not delete $email : $_"
                }
            }
        }
    } catch {
        Write-Err "Could not list users: $_"
    }
}

# --- 5. Create 4 test users (one per role) ---
# Each user has a clear, memorable name and password.
# The role hierarchy is: viewer < developer < admin < super_admin
$users = @(
    @{
        email    = "sa@fpulse.local"
        name     = "Sarah Admin"
        role     = "super_admin"
        password = "SuperAdmin1!"
        envs     = @("dev", "prod")
        desc     = "Full system control, license management"
    }
    @{
        email    = "manager@fpulse.local"
        name     = "Mike Manager"
        role     = "admin"
        password = "Manager1!"
        envs     = @("dev", "prod")
        desc     = "Project & user management, full PROD access"
    }
    @{
        email    = "developer@fpulse.local"
        name     = "Dana Developer"
        role     = "developer"
        password = "Developer1!"
        envs     = @("dev")
        desc     = "Build & execute in DEV, no PROD by default"
    }
    @{
        email    = "viewer@fpulse.local"
        name     = "Victor Viewer"
        role     = "viewer"
        password = "Viewer1!"
        envs     = @("dev")
        desc     = "Read-only access to DEV"
    }
)

Write-Header "Creating 4 test users (one per role)"
foreach ($u in $users) {
    Write-Host ""
    Write-Host "  -> $($u.email) [$($u.role)]" -ForegroundColor White

    # Try invite first
    $tempPassword = $null
    try {
        $invite = Invoke-Api -Method Post -Path "/api/auth/invite" -Token $adminToken -Body @{
            email    = $u.email
            name     = $u.name
            role     = $u.role
            projects = @()
        }
        $tempPassword = $invite.temp_password
        Write-Ok "Created (id=$($invite.user_id))"
    } catch {
        if ($_ -match "already registered") {
            # User exists - check if password already works
            try {
                $relogin = Invoke-Api -Method Post -Path "/api/auth/login" -Body @{
                    email    = $u.email
                    password = $u.password
                }
                Write-Ok "Already exists and password OK [role=$($relogin.user.role)]"

                # Update role in case it changed
                try {
                    $allUsers = Invoke-Api -Method Get -Path "/api/auth/users" -Token $adminToken
                    $existing = $allUsers | Where-Object { $_.email -eq $u.email } | Select-Object -First 1
                    if ($existing -and $existing.role -ne $u.role) {
                        Invoke-Api -Method Put -Path "/api/plus/users/$($existing.id)/role" -Token $adminToken -Body @{
                            role         = $u.role
                            environments = $u.envs
                        } | Out-Null
                        Write-Ok "Updated role to $($u.role)"
                    }
                } catch {}
                continue
            } catch {
                Write-Warn "Exists but password differs - recreating"
                try {
                    $allUsers = Invoke-Api -Method Get -Path "/api/auth/users" -Token $adminToken
                    $existing = $allUsers | Where-Object { $_.email -eq $u.email } | Select-Object -First 1
                    if ($existing) {
                        Invoke-Api -Method Delete -Path "/api/auth/users/$($existing.id)" -Token $adminToken | Out-Null
                        Write-Ok "Deleted old account"
                    }
                    $invite = Invoke-Api -Method Post -Path "/api/auth/invite" -Token $adminToken -Body @{
                        email    = $u.email
                        name     = $u.name
                        role     = $u.role
                        projects = @()
                    }
                    $tempPassword = $invite.temp_password
                    Write-Ok "Recreated (id=$($invite.user_id))"
                } catch {
                    Write-Err "Recreate failed: $_"
                    continue
                }
            }
        } else {
            Write-Err $_
            continue
        }
    }

    # Set the memorable password
    if ($tempPassword) {
        try {
            $login = Invoke-Api -Method Post -Path "/api/auth/login" -Body @{
                email    = $u.email
                password = $tempPassword
            }
            $userToken = $login.token

            Invoke-Api -Method Post -Path "/api/plus/users/change-password" -Token $userToken -Body @{
                current_password = $tempPassword
                new_password     = $u.password
            } | Out-Null
            Write-Ok "Password set to: $($u.password)"

            # Update role + environments via admin API
            try {
                $allUsers = Invoke-Api -Method Get -Path "/api/auth/users" -Token $adminToken
                $thisUser = $allUsers | Where-Object { $_.email -eq $u.email } | Select-Object -First 1
                if ($thisUser) {
                    Invoke-Api -Method Put -Path "/api/plus/users/$($thisUser.id)/role" -Token $adminToken -Body @{
                        role         = $u.role
                        environments = $u.envs
                    } | Out-Null
                    Write-Ok "Set role=$($u.role) envs=[$($u.envs -join ', ')]"
                }
            } catch {
                Write-Warn "Could not update role: $_"
            }

            # Verify
            $final = Invoke-Api -Method Post -Path "/api/auth/login" -Body @{
                email    = $u.email
                password = $u.password
            }
            Write-Ok "Verified login OK"
        } catch {
            Write-Err "Password setup failed: $_"
        }
    }
}

# --- 6. Print credentials ---
Write-Header "Test Accounts Ready"
Write-Host ""
Write-Host "  Open http://localhost:5174 and sign in:" -ForegroundColor White
Write-Host ""
Write-Host "  +--------------------------+-------------------+-------------------+---------------------------------------------+" -ForegroundColor DarkGray
Write-Host "  | Email                    | Password          | Role              | Access                                      |" -ForegroundColor DarkGray
Write-Host "  +--------------------------+-------------------+-------------------+---------------------------------------------+" -ForegroundColor DarkGray
Write-Host "  | admin@fpulse.local       | admin             | super_admin (seed)| Full: system + license + all projects       |" -ForegroundColor Gray
Write-Host "  | sa@fpulse.local          | SuperAdmin1!      | super_admin       | Full: system + license + all projects       |" -ForegroundColor White
Write-Host "  | manager@fpulse.local     | Manager1!         | admin             | Manage users, projects, full DEV+PROD       |" -ForegroundColor White
Write-Host "  | developer@fpulse.local   | Developer1!       | developer         | Build & run in DEV only (no PROD)           |" -ForegroundColor White
Write-Host "  | viewer@fpulse.local      | Viewer1!          | viewer            | Read-only DEV (no PROD)                     |" -ForegroundColor White
Write-Host "  +--------------------------+-------------------+-------------------+---------------------------------------------+" -ForegroundColor DarkGray
Write-Host ""

# Show current tier
try {
    $status = Invoke-RestMethod -Uri "$BaseUrl/api/plus/license" -Method Get -Headers @{ Authorization = "Bearer $adminToken" }
    if ($status.is_plus) {
        Write-Host "  Server tier: PLUS" -ForegroundColor Yellow
        Write-Host "    - Admin page visible for sa@ and manager@" -ForegroundColor DarkGray
        Write-Host "    - PROD toggle visible for sa@ and manager@" -ForegroundColor DarkGray
        Write-Host "    - developer@ can get PROD access via Admin > Users > PROD Access button" -ForegroundColor DarkGray
    } else {
        Write-Host "  Server tier: FREE (DEV only)" -ForegroundColor Green
        Write-Host "    - Re-run with -ActivatePlus to enable PROD environment & Plus features" -ForegroundColor DarkGray
    }
} catch {}

Write-Host ""
Write-Host "  Testing checklist:" -ForegroundColor Cyan
Write-Host "    1. Login as viewer@       -> should see DEV read-only, no create/edit buttons" -ForegroundColor DarkGray
Write-Host "    2. Login as developer@    -> should create/edit/run pipelines in DEV" -ForegroundColor DarkGray
Write-Host "    3. Login as manager@      -> should see Admin page, manage projects & users" -ForegroundColor DarkGray
Write-Host "    4. Login as sa@           -> should see everything including license management" -ForegroundColor DarkGray
Write-Host "    5. As manager@, go to Admin > Users > PROD Access for developer@ -> grant can_view_prod" -ForegroundColor DarkGray
Write-Host "    6. Login as developer@    -> should now see PROD toggle (view only)" -ForegroundColor DarkGray
Write-Host "    7. Export a pipeline as developer@ -> import as manager@ into a different project" -ForegroundColor DarkGray
Write-Host ""
