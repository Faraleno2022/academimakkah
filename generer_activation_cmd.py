#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MySchoolGN - Générateur de fichiers d'activation .cmd (usage unique)
====================================================================
Auteur  : GS Hadja Kanfing Dian
Usage   : Réservé au développeur (machine autorisée par license_manager)

Produit un fichier ``Activer_Licence_<ECOLE>_<ID>.cmd`` à envoyer au client.
Le client double-clique dessus : la licence est installée sans Python ni
manipulation de fichier, puis le .cmd s'efface.

Usage unique :
  - la licence embarquée est liée à l'ID machine du client (inutilisable
    ailleurs, même si le fichier est copié) ;
  - le .cmd vérifie l'ID machine avant d'écrire quoi que ce soit ;
  - un identifiant d'activation est mémorisé sur le PC : relancer une copie
    du même fichier est refusé ;
  - le fichier se supprime lui-même après l'activation.

Usages :
  python generer_activation_cmd.py <ID_MACHINE> <jours> "<École>" [Édition]
  python generer_activation_cmd.py            (questions interactives)
"""

import base64
import json
import re
import secrets
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

import license_manager as lm

DOSSIER_SORTIE = BASE / "activations_clients"
LICENSES_LOG = BASE / "licenses_emises.json"
EDITIONS = ("Standard", "Pro", "Premium")


def _b64(texte: str) -> str:
    return base64.b64encode(texte.encode("utf-8")).decode("ascii")


def _nom_fichier(ecole: str, machine_id: str) -> str:
    sans_accents = unicodedata.normalize("NFKD", ecole).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "_", sans_accents).strip("_")[:30] or "Client"
    return f"Activer_Licence_{slug}_{machine_id[:8]}.cmd"


def _journaliser(entree: dict):
    try:
        records = json.loads(LICENSES_LOG.read_text(encoding="utf-8")) if LICENSES_LOG.exists() else []
    except Exception:
        records = []
    records.append(entree)
    LICENSES_LOG.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")


# ─── Fichier .cmd envoyé au client ────────────────────────────────────────────
# Partie batch (ASCII) : extrait le script PowerShell placé après le marqueur,
# l'exécute, se relance en administrateur si besoin, puis s'efface.
# Codes retour PowerShell : 0 activé, 3 déjà utilisé (le fichier s'efface),
# 2 mauvais PC / annulé, 1 erreur, 5 droits administrateur nécessaires.
_CMD_BATCH = r"""@echo off
setlocal
title Activation de la licence MySchoolGN
set "MSG_ACT_SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$t=[IO.File]::ReadAllText($env:MSG_ACT_SELF,[Text.Encoding]::UTF8); $m='::'+'POWERSHELL::'; Invoke-Expression $t.Substring($t.IndexOf($m)+$m.Length)"
set "MSG_ACT_CODE=%errorlevel%"
if "%MSG_ACT_CODE%"=="5" (
    echo Autorisation administrateur demandee...
    powershell -NoProfile -Command "Start-Process -FilePath $env:MSG_ACT_SELF -Verb RunAs"
    exit /b 0
)
if "%MSG_ACT_CODE%"=="0" goto :supprimer
if "%MSG_ACT_CODE%"=="3" goto :supprimer
exit /b %MSG_ACT_CODE%
:supprimer
(goto) 2>nul & del /f /q "%~f0"
"""

_CMD_POWERSHELL = r"""
$ErrorActionPreference = 'Stop'
$ActivationId    = '__ACTIVATION_ID__'
$MachineAttendue = '__MACHINE_ID__'
$Ecole           = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__ECOLE_B64__'))
$Edition         = '__EDITION__'
$Expiration      = '__EXPIRATION__'
$LicenceOctets   = [Convert]::FromBase64String('__LICENCE_B64__')
$AppGuid         = '{B7E4A2D1-F3C8-4B91-A5E6-GS2024HADJA01}_is1'

Add-Type -AssemblyName System.Windows.Forms
function Message([string]$texte, [string]$icone = 'Information', [string]$boutons = 'OK') {
    if ($env:MYSCHOOLGN_SILENCIEUX) { return 'No' }   # sans fenêtre : refuse toute question
    $proprio = New-Object System.Windows.Forms.Form -Property @{ TopMost = $true }
    try {
        return [System.Windows.Forms.MessageBox]::Show($proprio, $texte, 'Activation MySchoolGN', $boutons, $icone)
    } finally { $proprio.Dispose() }
}

# Même calcul que license_manager.get_machine_id()
function Get-MachineIdMySchool {
    Add-Type -Namespace MySchoolGN -Name Rpc -MemberDefinition '[DllImport("rpcrt4.dll")] public static extern int UuidCreateSequential(out System.Guid g);'
    $parts = @()
    try { $parts += (Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Cryptography' -Name MachineGuid).MachineGuid } catch {}
    try { $parts += [System.Net.Dns]::GetHostName() } catch {}
    try {
        $g = [guid]::Empty
        [void][MySchoolGN.Rpc]::UuidCreateSequential([ref]$g)
        $b = $g.ToByteArray()
        [UInt64]$node = 0
        foreach ($i in 10..15) { $node = ($node -shl 8) -bor $b[$i] }
        $parts += $node.ToString()
    } catch {}
    $arch = $env:PROCESSOR_ARCHITEW6432
    if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
    $parts += 'Windows' + $arch
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $h = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes(($parts -join '|')))
    return (($h | ForEach-Object { $_.ToString('x2') }) -join '').Substring(0, 32).ToUpper()
}

function Get-DossierInstallation {
    if ($env:MYSCHOOLGN_DIR) { return $env:MYSCHOOLGN_DIR }
    $cles = @(
        "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppGuid",
        "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppGuid",
        "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\$AppGuid"
    )
    foreach ($cle in $cles) {
        try {
            $dossier = (Get-ItemProperty -Path $cle).InstallLocation
            if ($dossier -and (Test-Path -LiteralPath (Join-Path $dossier 'MySchoolGN.exe'))) { return $dossier }
        } catch {}
    }
    try {
        $proc = Get-Process -Name MySchoolGN -ErrorAction SilentlyContinue | Where-Object { $_.Path } | Select-Object -First 1
        if ($proc) { return (Split-Path -Parent $proc.Path) }
    } catch {}
    $candidats = @()
    if ($env:LOCALAPPDATA) { $candidats += Join-Path $env:LOCALAPPDATA 'Programs\MySchoolGN' }
    foreach ($pf in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($pf) { $candidats += Join-Path $pf 'MySchoolGN' }
    }
    $candidats += Split-Path -Parent $env:MSG_ACT_SELF
    foreach ($dossier in $candidats) {
        if (Test-Path -LiteralPath (Join-Path $dossier 'MySchoolGN.exe')) { return $dossier }
    }
    return $null
}

function Test-Ecriture([string]$dossier) {
    try {
        $essai = Join-Path $dossier ('.essai_' + [guid]::NewGuid().ToString('N'))
        [IO.File]::WriteAllText($essai, 'ok')
        Remove-Item -LiteralPath $essai -Force
        return $true
    } catch { return $false }
}

function Get-Journal([string]$dossier) { Join-Path $dossier '.activations_utilisees' }

try {
    Write-Host ''
    Write-Host '=========================================================='
    Write-Host '   MySchoolGN - Activation de la licence (usage unique)'
    Write-Host '=========================================================='
    Write-Host "   Ecole      : $Ecole"
    Write-Host "   Edition    : $Edition"
    Write-Host "   Expiration : $Expiration"
    Write-Host ''

    $dossierAppData = Join-Path $env:APPDATA 'MySchoolGN'
    $dossierApp = Get-DossierInstallation
    $dossiers = @($dossierAppData)
    if ($dossierApp) { $dossiers = @($dossierApp, $dossierAppData) }

    # 1. Fichier déjà utilisé sur ce PC ?
    foreach ($d in $dossiers) {
        $journal = Get-Journal $d
        if ((Test-Path -LiteralPath $journal) -and (Select-String -LiteralPath $journal -SimpleMatch $ActivationId -Quiet)) {
            Write-Host 'Ce fichier d''activation a deja ete utilise.'
            [void](Message "Ce fichier d'activation a déjà été utilisé sur cet ordinateur.`n`nIl ne peut servir qu'une seule fois et va être supprimé." 'Warning')
            exit 3
        }
    }

    # 2. Bon ordinateur ?
    $machine = Get-MachineIdMySchool
    Write-Host "   ID machine : $machine"
    if ($machine.Substring(0, 16) -ne $MachineAttendue.Substring(0, 16)) {
        $choix = Message ("Ce fichier d'activation a été préparé pour un autre ordinateur.`n`n" +
            "ID attendu : $MachineAttendue`nID de ce PC : $machine`n`n" +
            "Communiquez l'ID de ce PC à l'administrateur FARA LENO (+224622613559).`n`n" +
            "Installer la licence quand même ?") 'Warning' 'YesNo'
        if ($choix -ne 'Yes') { Write-Host 'Activation annulee.'; exit 2 }
    }

    # 3. Droits d'écriture dans le dossier d'installation
    if ($dossierApp -and -not (Test-Ecriture $dossierApp)) {
        $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if (-not $admin) { exit 5 }
        throw "Impossible d'écrire dans $dossierApp"
    }

    # 4. Installer la licence (sauvegarde de l'ancienne)
    $horodatage = Get-Date -Format 'yyyyMMdd_HHmmss'
    foreach ($d in $dossiers) {
        if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
        $cible = Join-Path $d 'license.dat'
        if (Test-Path -LiteralPath $cible) {
            Copy-Item -LiteralPath $cible -Destination (Join-Path $d "license.dat.avant_activation_$horodatage.bak") -Force
        }
        [IO.File]::WriteAllBytes($cible, $LicenceOctets)   # sans BOM (lu par json.load)
        Add-Content -LiteralPath (Get-Journal $d) -Value "$ActivationId|$(Get-Date -Format s)|$machine" -Encoding ASCII
        Write-Host "   Licence ecrite : $cible"
    }

    $suite = "Fermez puis relancez MySchoolGN pour en profiter."
    if (-not $dossierApp) {
        $suite = "Dossier d'installation de MySchoolGN introuvable : la licence a été placée dans le profil Windows.`n" + $suite
    }
    Write-Host ''
    Write-Host 'Licence activee avec succes. Ce fichier va etre supprime.'
    [void](Message "Licence activée avec succès !`n`nÉcole : $Ecole`nÉdition : $Edition`nExpire le : $Expiration`n`n$suite`n`nCe fichier d'activation va maintenant être supprimé.")
    exit 0
}
catch {
    Write-Host "ERREUR : $($_.Exception.Message)"
    [void](Message "L'activation a échoué :`n$($_.Exception.Message)`n`nContactez l'administrateur FARA LENO (+224622613559)." 'Error')
    exit 1
}
"""


def generer_cmd(machine_id: str, jours: int, ecole: str, edition: str = "Standard",
                dossier: Path = DOSSIER_SORTIE) -> Path:
    machine_id = machine_id.strip().upper()
    if not re.fullmatch(r"[0-9A-F]{16,64}", machine_id):
        raise ValueError("ID machine invalide : copiez l'ID complet affiché par MySchoolGN (32 caractères).")
    if jours <= 0:
        raise ValueError("La durée doit être d'au moins 1 jour.")

    lic = lm.generate_activation_file(machine_id, jours, ecole, edition)
    licence_json = json.dumps(lic, indent=2)
    expiration = json.loads(base64.b64decode(lic["license_data"]))["exp"]
    expiration = f"{expiration[6:8]}/{expiration[4:6]}/{expiration[:4]}"
    activation_id = secrets.token_hex(8).upper()

    script = (_CMD_POWERSHELL
              .replace("__ACTIVATION_ID__", activation_id)
              .replace("__MACHINE_ID__", machine_id)
              .replace("__ECOLE_B64__", _b64(ecole))
              .replace("__EDITION__", re.sub(r"[^\w -]", "", edition))
              .replace("__EXPIRATION__", expiration)
              .replace("__LICENCE_B64__", _b64(licence_json)))
    contenu = _CMD_BATCH + "::POWERSHELL::" + script

    dossier.mkdir(parents=True, exist_ok=True)
    chemin = dossier / _nom_fichier(ecole, machine_id)
    # CRLF obligatoire pour cmd.exe ; UTF-8 sans BOM (la partie batch reste ASCII)
    chemin.write_bytes(contenu.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))

    maintenant = datetime.now(timezone.utc)
    _journaliser({
        "school": ecole,
        "edition": edition,
        "days": jours,
        "contact": "",
        "issued": maintenant.strftime("%Y-%m-%d"),
        "expires": (maintenant + timedelta(days=jours)).strftime("%Y-%m-%d"),
        "machine_short": machine_id[:16],
        "filename": chemin.name,
        "activation_id": activation_id,
    })
    return chemin


def _demander(question: str, defaut: str = "") -> str:
    suffixe = f" [{defaut}]" if defaut else ""
    reponse = input(f"{question}{suffixe} : ").strip()
    return reponse or defaut


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    print("\nMySchoolGN - Fichier d'activation .cmd (usage unique)")
    print("=" * 55)
    try:
        if len(sys.argv) >= 4:
            mid, jours, ecole = sys.argv[1], int(sys.argv[2]), sys.argv[3]
            edition = sys.argv[4] if len(sys.argv) > 4 else "Standard"
        else:
            mid = _demander("ID machine du client (affiché dans MySchoolGN)")
            jours = int(_demander("Durée en jours", "365"))
            ecole = _demander("Nom de l'école")
            edition = _demander(f"Édition ({'/'.join(EDITIONS)})", "Standard")
        chemin = generer_cmd(mid, jours, ecole, edition)
    except PermissionError as e:
        print(f"\n✗ {e}")
        sys.exit(1)
    except ValueError as e:
        print(f"\n✗ {e}")
        sys.exit(1)

    print(f"\n✓ Fichier créé : {chemin}")
    print(f"  École   : {ecole}")
    print(f"  Édition : {edition}")
    print(f"  Durée   : {jours} jours")
    print("\nEnvoyez ce fichier au client (WhatsApp, clé USB...).")
    print("Il double-clique dessus : la licence s'installe et le fichier s'efface.")
