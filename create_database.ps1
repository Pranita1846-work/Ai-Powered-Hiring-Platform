# PowerShell script to create PostgreSQL database
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  Hiring Platform - Database Setup" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""

# Add PostgreSQL to PATH
$env:Path += ";C:\Program Files\PostgreSQL\17\bin"

# Prompt for password
$password = Read-Host "Enter your PostgreSQL password" -AsSecureString
$BSTR = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($password)
$PlainPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($BSTR)

Write-Host "Creating database 'hiring_platform'..." -ForegroundColor Yellow

# Set password environment variable
$env:PGPASSWORD = $PlainPassword

# Create database
& psql -U postgres -h localhost -p 5432 -c "CREATE DATABASE hiring_platform;" 2>&1 | Out-String | ForEach-Object {
    if ($_ -match "already exists") {
        Write-Host "Database already exists!" -ForegroundColor Green
    } elseif ($_ -match "CREATE DATABASE") {
        Write-Host "Database created successfully!" -ForegroundColor Green
    } elseif ($_ -match "password authentication failed") {
        Write-Host "Error: Incorrect password!" -ForegroundColor Red
        Write-Host "Please check your password and try again." -ForegroundColor Red
        exit 1
    } else {
        Write-Host $_
    }
}

# Clear password from environment
$env:PGPASSWORD = ""

Write-Host ""
Write-Host "Next step: Run 'python init_database.py' to create tables" -ForegroundColor Cyan
Write-Host ""
pause
