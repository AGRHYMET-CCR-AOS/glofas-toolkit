[CmdletBinding()]
param(
    [string]$EnvironmentName = "glofas-ewds"
)

$ErrorActionPreference = "Stop"
$ProjectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvironmentFile = Join-Path $ProjectDirectory "environment.yml"

function Find-CondaExecutable {
    $CondaCommand = Get-Command conda -ErrorAction SilentlyContinue
    if ($CondaCommand) {
        return $CondaCommand.Source
    }

    $Candidates = @()
    if ($env:CONDA_EXE) {
        $Candidates += $env:CONDA_EXE
    }
    if ($env:USERPROFILE) {
        $Candidates += (Join-Path $env:USERPROFILE "anaconda3\Scripts\conda.exe")
        $Candidates += (Join-Path $env:USERPROFILE "Anaconda3\Scripts\conda.exe")
        $Candidates += (Join-Path $env:USERPROFILE "miniconda3\Scripts\conda.exe")
        $Candidates += (Join-Path $env:USERPROFILE "Miniconda3\Scripts\conda.exe")
    }
    if ($env:LOCALAPPDATA) {
        $Candidates += (Join-Path $env:LOCALAPPDATA "anaconda3\Scripts\conda.exe")
        $Candidates += (Join-Path $env:LOCALAPPDATA "Continuum\anaconda3\Scripts\conda.exe")
        $Candidates += (Join-Path $env:LOCALAPPDATA "miniconda3\Scripts\conda.exe")
    }
    if ($env:ProgramData) {
        $Candidates += (Join-Path $env:ProgramData "anaconda3\Scripts\conda.exe")
        $Candidates += (Join-Path $env:ProgramData "Anaconda3\Scripts\conda.exe")
        $Candidates += (Join-Path $env:ProgramData "miniconda3\Scripts\conda.exe")
    }
    $Candidates += "C:\Anaconda3\Scripts\conda.exe"
    $Candidates += "C:\Miniconda3\Scripts\conda.exe"

    foreach ($Candidate in $Candidates) {
        if ($Candidate -and (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
    }
    return $null
}

$CondaExecutable = Find-CondaExecutable
if (-not $CondaExecutable) {
    throw @"
Conda est installé mais n'a pas été trouvé dans les emplacements Windows habituels.
Ouvrez « Anaconda Prompt », exécutez where.exe conda, puis relancez ce script depuis
Anaconda Prompt ou ajoutez le chemin retourné à la variable PATH.
"@
}

Write-Host "Conda détecté : $CondaExecutable"

Write-Host "Création ou mise à jour de l'environnement Conda '$EnvironmentName'..."

$ExistingEnvironments = & $CondaExecutable env list --json | ConvertFrom-Json
$EnvironmentExists = $false

foreach ($EnvironmentPath in $ExistingEnvironments.envs) {
    if ((Split-Path $EnvironmentPath -Leaf) -eq $EnvironmentName) {
        $EnvironmentExists = $true
        break
    }
}

if ($EnvironmentExists) {
    & $CondaExecutable env update `
        --name $EnvironmentName `
        --file $EnvironmentFile `
        --prune
} else {
    & $CondaExecutable env create `
        --name $EnvironmentName `
        --file $EnvironmentFile
}

if ($LASTEXITCODE -ne 0) {
    throw "La création ou la mise à jour de l'environnement Conda a échoué."
}

Write-Host "Vérification de Python, cdsapi, des dépendances d'extraction (xarray/cfgrib), de sélection géospatiale (geopandas/pyogrio/shapely), de visualisation (folium/plotly) et de Jupyter..."
& $CondaExecutable run --name $EnvironmentName python -c "import sys, cdsapi, xarray, cfgrib, pandas, folium, plotly, geopandas, pyogrio, shapely, jupyterlab, ipykernel, ipywidgets; from importlib.metadata import version; print('Python', sys.version.split()[0]); print('cdsapi', version('cdsapi')); print('xarray', version('xarray')); print('cfgrib', version('cfgrib')); print('folium', version('folium')); print('plotly', version('plotly')); print('geopandas', version('geopandas')); print('pyogrio', version('pyogrio')); print('jupyterlab', version('jupyterlab')); print('ipywidgets', version('ipywidgets'))"

if ($LASTEXITCODE -ne 0) {
    throw "L'environnement existe, mais la vérification de cdsapi/xarray/cfgrib/folium/plotly/geopandas/pyogrio/shapely/jupyterlab a échoué."
}

Write-Host ""
Write-Host "Environnement prêt. Commandes suivantes :"
Write-Host "  $CondaExecutable run -n $EnvironmentName python glofas_download.py --years 1980-1989"
Write-Host "  $CondaExecutable run -n $EnvironmentName jupyter lab"
Write-Host ""
Write-Host "Pour rendre 'conda activate' disponible dans PowerShell :"
Write-Host "  & '$CondaExecutable' init powershell"
Write-Host "Puis fermez et rouvrez PowerShell."
