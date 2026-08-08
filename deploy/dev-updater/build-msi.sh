#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${1:-$REPO/dist}"
VERSION="$(python3 -c "from tools.dev_updater.identity import MSI_PRODUCT_VERSION; print(MSI_PRODUCT_VERSION)")"
APP=IOTEdgeDevUpdater
PRODUCT='IOT Edge Development Updater'
UPGRADE_CODE='AECCDF45-A1D2-43A5-9142-32E6A984A66E'
MSI="$OUT_DIR/$APP-0.2.1-x64.msi"
command -v wixl >/dev/null
command -v wixl-heat >/dev/null
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/stage"
mkdir -p "$STAGE/tools/dev_updater/releases/manifests" "$STAGE/tools/releases" "$OUT_DIR"
cp "$REPO"/tools/dev_updater/*.py "$STAGE/tools/dev_updater/"
cp "$REPO"/tools/gateway_recovery.py "$REPO"/tools/release_manifest.py "$REPO"/tools/release_preflight.py "$STAGE/tools/"
touch "$STAGE/tools/__init__.py"
cp "$REPO"/tools/dev_updater/releases/manifests/edge-0.2.0.json "$STAGE/tools/dev_updater/releases/manifests/"
cp "$REPO"/tools/releases/gw006-edge-ui-0.2.0-code.tar.gz "$STAGE/tools/releases/"
cp "$REPO"/deploy/dev-updater/IOTEdgeDevUpdater.cmd "$STAGE/$APP.cmd"
cp "$REPO"/deploy/dev-updater/requirements.txt "$REPO"/deploy/dev-updater/.env.example "$STAGE/"
if find "$STAGE" -name .env -print -quit | grep -q .; then echo 'Refusing to package .env' >&2; exit 1; fi
if grep -rIlE '^[A-Z_]*(PASSWORD|TOKEN|SECRET|KEY)[A-Z_]*=.+' "$STAGE" 2>/dev/null | grep -v '\.env\.example$' | grep -q .; then echo 'Refusing to package populated credentials' >&2; exit 1; fi
printf '%s\n' "IOT Edge Development Updater 0.2.1" "MSI ProductVersion: $VERSION" "Source commit: $(git -C "$REPO" rev-parse HEAD)" > "$STAGE/VERSION.txt"
(cd "$STAGE" && find . -type f -printf '%P\n' | sort) | sed "s|^|$STAGE/|" | wixl-heat --prefix "$STAGE/" --directory-ref INSTALLDIR --component-group AppFiles --var var.SourceDir > "$WORK/files.wxs"
cat > "$WORK/product.wxs" <<XML
<Wix xmlns="http://schemas.microsoft.com/wix/2006/wi"><Product Id="*" Name="$PRODUCT" Language="1033" Version="$VERSION" Manufacturer="The Internet of Team, LLC" UpgradeCode="$UPGRADE_CODE"><Package InstallerVersion="200" Compressed="yes" InstallScope="perMachine"/><Media Id="1" Cabinet="app.cab" EmbedCab="yes"/><Upgrade Id="$UPGRADE_CODE"><UpgradeVersion Minimum="0.0.0" IncludeMinimum="yes" Maximum="$VERSION" IncludeMaximum="no" Property="OLDERVERSIONFOUND"/></Upgrade><InstallExecuteSequence><RemoveExistingProducts After="InstallInitialize"/></InstallExecuteSequence><Directory Id="TARGETDIR" Name="SourceDir"><Directory Id="ProgramFiles64Folder"><Directory Id="INSTALLDIR" Name="$PRODUCT"/></Directory><Directory Id="CommonAppDataFolder"><Directory Id="IOTData" Name="IOT"><Directory Id="DEVData" Name="EdgeDevUpdater"><Directory Id="DEVLogs" Name="logs"/></Directory></Directory></Directory><Directory Id="ProgramMenuFolder"><Directory Id="MenuDir" Name="$PRODUCT"/></Directory><Directory Id="DesktopFolder"/></Directory><DirectoryRef Id="DEVData"><Component Id="Data" Guid="E7D0E38D-6D1E-461D-B3F9-6C74E4F75B4A"><CreateFolder/><RegistryValue Root="HKLM" Key="Software\IOT\EdgeDevUpdater" Name="DataDirectory" Type="string" Value="[DEVData]" KeyPath="yes"/></Component></DirectoryRef><DirectoryRef Id="DEVLogs"><Component Id="Logs" Guid="F0EE211B-B0E9-4AE8-871E-FCD70A892974"><CreateFolder/><RegistryValue Root="HKLM" Key="Software\IOT\EdgeDevUpdater" Name="LogDirectory" Type="string" Value="[DEVLogs]" KeyPath="yes"/></Component></DirectoryRef><DirectoryRef Id="MenuDir"><Component Id="Menu" Guid="C78664D4-78E8-43BD-8AED-59F925E55A30"><Shortcut Id="MenuShortcut" Name="$PRODUCT" Target="[INSTALLDIR]$APP.cmd"/><RemoveFolder Id="MenuDir" On="uninstall"/><RegistryValue Root="HKCU" Key="Software\IOT\EdgeDevUpdater" Name="Menu" Type="integer" Value="1" KeyPath="yes"/></Component></DirectoryRef><DirectoryRef Id="DesktopFolder"><Component Id="Desktop" Guid="EF104C90-E9E0-4E66-ACB2-1556A4B1F93E"><Shortcut Id="DesktopShortcut" Name="$PRODUCT" Target="[INSTALLDIR]$APP.cmd"/><RegistryValue Root="HKCU" Key="Software\IOT\EdgeDevUpdater" Name="Desktop" Type="integer" Value="1" KeyPath="yes"/></Component></DirectoryRef><Feature Id="Complete" Level="1"><ComponentGroupRef Id="AppFiles"/><ComponentRef Id="Data"/><ComponentRef Id="Logs"/><ComponentRef Id="Menu"/><ComponentRef Id="Desktop"/></Feature></Product></Wix>
XML
wixl --arch x64 -D SourceDir="$STAGE" -o "$MSI" "$WORK/product.wxs" "$WORK/files.wxs"
sha256sum "$MSI"
