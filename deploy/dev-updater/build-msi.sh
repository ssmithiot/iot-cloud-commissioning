#!/usr/bin/env bash
# Build the IOT Edge Development Updater Windows MSI on Linux.
#
# WiX's own candle.exe and light.exe are 32-bit .NET binaries, which on Linux
# need a 32-bit Wine loader, which needs i386 multiarch, which needs root. This
# script takes the other route: `wixl` from msitools is a native Linux MSI
# writer. No Wine, no root.
#
# The MSI installs to its own Program Files folder under its own UpgradeCode. It
# does not know the Legacy Edge Upgrade Webapp exists, cannot upgrade it, and
# cannot remove it: the Legacy Updater is a .cmd launcher run from a Git
# checkout and is not an installed product at all.
#
# Usage: deploy/dev-updater/build-msi.sh [output-directory]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${1:-$REPO/dist}"

PRODUCT="IOT Edge Development Updater"
APP="IOTEdgeDevUpdater"
VERSION="$(python3 -c "import re,pathlib; print(re.search(r'APP_VERSION = \"([^\"]+)\"', pathlib.Path('$REPO/tools/dev_updater/identity.py').read_text()).group(1))")"
MANUFACTURER="The Internet of Team, LLC"
MSI="$OUT_DIR/IOTEdgeDevUpdater-$VERSION-x64.msi"

# Stable across releases: Windows recognises an upgrade by this code. Never
# regenerate it. It is this product's alone.
UPGRADE_CODE="90FF1484-46DC-4848-890C-432F735E079D"
DATA_DIR_GUID="409F9F49-6E32-4809-985E-60AA275E235C"
SHORTCUT_GUID="57F462A5-4939-4DD8-8867-D95E89CB89D9"
LOG_DIR_GUID="1D8C4E23-7A65-4B0F-9E14-3C6A82D57B90"
DESKTOP_GUID="6B39A0F7-2E4D-41C8-B573-8A0E1F94D26C"

command -v wixl >/dev/null || { echo "wixl not found. Install msitools, or add wixl to PATH." >&2; exit 1; }
command -v wixl-heat >/dev/null || { echo "wixl-heat not found. Install msitools, or add it to PATH." >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/stage"
mkdir -p "$STAGE/tools/dev_updater" "$STAGE/tools/releases/manifests" "$OUT_DIR"

echo "==> Staging $PRODUCT $VERSION"

# The application itself: updater_webapp.py is the copy of the working updater,
# plus the identity/runtime helpers that keep it separate from Jim's.
cp "$REPO"/tools/dev_updater/*.py            "$STAGE/tools/dev_updater/"

# The shared, side-effect-free helpers it imports. gateway_recovery is the
# proven checkpoint and restore builder; reusing it is deliberate. The Legacy
# Updater's own module is NOT staged - this product never loads it.
cp "$REPO"/tools/release_manifest.py         "$STAGE/tools/"
cp "$REPO"/tools/gateway_recovery.py         "$STAGE/tools/"
cp "$REPO"/tools/release_preflight.py        "$STAGE/tools/"
touch "$STAGE/tools/__init__.py"

# This product's own manifest copy. Jim's
# tools/releases/manifests/edge-0.2.0.json is neither staged nor read.
mkdir -p "$STAGE/tools/dev_updater/releases/manifests"
cp "$REPO"/tools/dev_updater/releases/manifests/edge-0.2.0-dev.json "$STAGE/tools/dev_updater/releases/manifests/"

# The approved release artifact, shipped offline so the updater works on a bench
# with no internet, staged at the path the manifest names relative to the
# install root. Only approved releases are staged.
cp "$REPO"/tools/releases/gw006-edge-ui-0.2.0-code.tar.gz "$STAGE/tools/releases/"

cp "$REPO"/deploy/dev-updater/README.md              "$STAGE/README.md"
cp "$REPO"/docs/dev-updater-operator-guide.md        "$STAGE/OPERATOR-GUIDE.md"
cp "$REPO"/deploy/dev-updater/IOTEdgeDevUpdater.cmd  "$STAGE/$APP.cmd"
cp "$REPO"/deploy/dev-updater/requirements.txt       "$STAGE/requirements.txt"
# Variable names and descriptions only. The real .env is never built into the
# MSI; Steve copies his own into %ProgramData% after installing.
cp "$REPO"/deploy/dev-updater/.env.example           "$STAGE/.env.example"

# A secret reaching the MSI would be shipped to every machine that installs it,
# so the build refuses rather than trusting the copy list above to stay correct.
if find "$STAGE" -name ".env" -print -quit | grep -q .; then
  echo "Refusing to build: a .env file is present in the staged tree." >&2
  exit 1
fi
if grep -rIlE '^[A-Z_]*(PASSWORD|TOKEN|SECRET|PASSPHRASE)[A-Z_]*=.+' "$STAGE" 2>/dev/null | grep -v '\.env\.example$' | grep -q .; then
  echo "Refusing to build: a populated credential variable is present in the staged tree." >&2
  exit 1
fi

# Read the pinned targets out of the staged manifest, so this file can never
# disagree with what the application will actually deploy.
DEV_MANIFEST="$STAGE/tools/dev_updater/releases/manifests/edge-0.2.0-dev.json"
UI_SHA="$(python3 -c "import json;print(json.load(open('$DEV_MANIFEST'))['edge_ui_tag'])")"
AGENT_SHA="$(python3 -c "import json;print(json.load(open('$DEV_MANIFEST'))['agent_source_commit'])")"
ARTIFACT_SHA="$(python3 -c "import json;print(json.load(open('$DEV_MANIFEST'))['sha256'])")"

printf '%s\n' \
  "$PRODUCT $VERSION" \
  "Built from commit: $(git -C "$REPO" rev-parse HEAD)" \
  "Branch: $(git -C "$REPO" rev-parse --abbrev-ref HEAD)" \
  "Edge UI commit:    $UI_SHA (${UI_SHA:0:7}) on release/edge-ui-0.2.0" \
  "Edge Agent commit: $AGENT_SHA (${AGENT_SHA:0:7}) on release/edge-agent-0.2.0" \
  "Edge UI artifact SHA-256: $ARTIFACT_SHA" \
  "Development Updater port: 8791" \
  "Legacy Updater port (never used): 8766" \
  > "$STAGE/VERSION.txt"

echo "==> Enumerating $(find "$STAGE" -type f | wc -l) files"
( cd "$STAGE" && find . -type f -printf '%P\n' | sort ) \
  | sed "s|^|$STAGE/|" \
  | wixl-heat --prefix "$STAGE/" \
              --directory-ref INSTALLDIR \
              --component-group AppFiles \
              --var var.SourceDir \
  > "$WORK/files.wxs"

# NOTE ON UNINSTALL: only INSTALLDIR and the two shortcut folders are removed.
# The data directory is created but logs are not removed on uninstall - it has no
# RemoveFolder entry - so logs and checkpoint records survive. Deleting them is
# the operator's explicit choice, documented in the README.
cat > "$WORK/product.wxs" <<XML
<?xml version="1.0" encoding="utf-8"?>
<Wix xmlns="http://schemas.microsoft.com/wix/2006/wi">
  <Product Id="*" Name="$PRODUCT" Language="1033" Version="$VERSION"
           Manufacturer="$MANUFACTURER" UpgradeCode="$UPGRADE_CODE">
    <Package InstallerVersion="200" Compressed="yes" InstallScope="perMachine"
             Comments="Manual-only updater for deploying Edge 0.2.0 release candidates to selected test gateways" />
    <Media Id="1" Cabinet="app.cab" EmbedCab="yes" />

    <!-- Product Id="*" mints a fresh ProductCode per build; the UpgradeCode
         above is what makes a later version replace this one rather than
         install beside it. -->
    <Upgrade Id="$UPGRADE_CODE">
      <UpgradeVersion Minimum="0.0.0" IncludeMinimum="yes" Maximum="$VERSION"
                      IncludeMaximum="no" Property="OLDERVERSIONFOUND" />
    </Upgrade>
    <InstallExecuteSequence>
      <RemoveExistingProducts After="InstallInitialize" />
    </InstallExecuteSequence>

    <Directory Id="TARGETDIR" Name="SourceDir">
      <Directory Id="ProgramFiles64Folder">
        <Directory Id="INSTALLDIR" Name="$PRODUCT" />
      </Directory>
      <Directory Id="CommonAppDataFolder">
        <Directory Id="IOTDataFolder" Name="IOT">
          <Directory Id="DEVUPDATERDATA" Name="EdgeDevUpdater">
            <Directory Id="DEVUPDATERLOGS" Name="logs" />
          </Directory>
        </Directory>
      </Directory>
      <Directory Id="ProgramMenuFolder">
        <Directory Id="AppMenuFolder" Name="$PRODUCT" />
      </Directory>
      <Directory Id="DesktopFolder" />
    </Directory>

    <!-- The data root and its logs folder are created, and carry no
         RemoveFolder: logs are not removed on uninstall. Deleting them is the
         operator's explicit choice. -->
    <DirectoryRef Id="DEVUPDATERDATA">
      <Component Id="DataDirectory" Guid="$DATA_DIR_GUID">
        <CreateFolder />
        <RegistryValue Root="HKLM" Key="Software\\IOT\\EdgeDevUpdater"
                       Name="DataDirectory" Type="string"
                       Value="[DEVUPDATERDATA]" KeyPath="yes" />
      </Component>
    </DirectoryRef>

    <DirectoryRef Id="DEVUPDATERLOGS">
      <Component Id="LogDirectory" Guid="$LOG_DIR_GUID">
        <CreateFolder />
        <RegistryValue Root="HKLM" Key="Software\\IOT\\EdgeDevUpdater"
                       Name="LogDirectory" Type="string"
                       Value="[DEVUPDATERLOGS]" KeyPath="yes" />
      </Component>
    </DirectoryRef>

    <DirectoryRef Id="AppMenuFolder">
      <Component Id="StartMenuShortcut" Guid="$SHORTCUT_GUID">
        <Shortcut Id="StartMenuLink" Name="$PRODUCT"
                  Description="Manual updater for Edge 0.2.0 release candidates on test gateways"
                  Target="[INSTALLDIR]$APP.cmd" WorkingDirectory="INSTALLDIR" />
        <RemoveFolder Id="AppMenuFolder" On="uninstall" />
        <RegistryValue Root="HKCU" Key="Software\\IOT\\EdgeDevUpdater"
                       Name="StartMenu" Type="integer" Value="1" KeyPath="yes" />
      </Component>
    </DirectoryRef>

    <DirectoryRef Id="DesktopFolder">
      <Component Id="DesktopShortcut" Guid="$DESKTOP_GUID">
        <Shortcut Id="DesktopLink" Name="$PRODUCT"
                  Description="Manual updater for Edge 0.2.0 release candidates on test gateways"
                  Target="[INSTALLDIR]$APP.cmd" WorkingDirectory="INSTALLDIR" />
        <RegistryValue Root="HKCU" Key="Software\\IOT\\EdgeDevUpdater"
                       Name="Desktop" Type="integer" Value="1" KeyPath="yes" />
      </Component>
    </DirectoryRef>

    <Feature Id="Complete" Title="$PRODUCT" Level="1">
      <ComponentGroupRef Id="AppFiles" />
      <ComponentRef Id="DataDirectory" />
      <ComponentRef Id="LogDirectory" />
      <ComponentRef Id="StartMenuShortcut" />
      <ComponentRef Id="DesktopShortcut" />
    </Feature>
  </Product>
</Wix>
XML

echo "==> Writing $MSI"
wixl -v --arch x64 -D SourceDir="$STAGE" -o "$MSI" "$WORK/product.wxs" "$WORK/files.wxs"

echo
echo "$PRODUCT $VERSION"
echo "  MSI    : $MSI"
echo "  size   : $(stat -c%s "$MSI") bytes"
echo "  sha256 : $(sha256sum "$MSI" | cut -d' ' -f1)"
echo "  commit : $(git -C "$REPO" rev-parse HEAD)"
