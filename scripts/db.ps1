# Startet/stoppt die lokale portable PostgreSQL-Instanz (ohne Docker/Admin).
# Verwendung: .\scripts\db.ps1 start | stop | status
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("start", "stop", "status")]
    [string]$Action
)

$PgBin = "C:\dev\tools\pgsql\bin"
$DataDir = Join-Path $PSScriptRoot "..\.pgdata"
$LogFile = Join-Path $PSScriptRoot "..\.pgdata\pg.log"

switch ($Action) {
    "start" {
        & "$PgBin\pg_ctl.exe" -D $DataDir -l $LogFile -w start
        & "$PgBin\pg_isready.exe" -h 127.0.0.1 -p 5433
    }
    "stop" {
        & "$PgBin\pg_ctl.exe" -D $DataDir stop
    }
    "status" {
        & "$PgBin\pg_ctl.exe" -D $DataDir status
        & "$PgBin\pg_isready.exe" -h 127.0.0.1 -p 5433
    }
}
