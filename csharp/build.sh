#!/usr/bin/env bash
# Build KspAutomationBridge.dll with the .NET Framework C# compiler (csc) directly.
# No msbuild / no Roslyn required — only the in-box v4.0.30319 csc.exe (C# 5).
#
#   bash csharp/build.sh                       # -> C:\tmp\KspAutomationBridge.dll
#   OUT='C:\some\path.dll' bash csharp/build.sh
#
# Git Bash / MSYS gotchas that this script handles (learned the hard way):
#   * Use -flag NOT /flag.  MSYS rewrites a leading-slash argument into a fake
#     'C:/Program Files/Git/<flag>' path, so /target etc. break. The dash form is
#     accepted by csc and is left alone by MSYS.
#   * Export MSYS2_ARG_CONV_EXCL='*' and MSYS_NO_PATHCONV=1 so MSYS does NOT mangle
#     the Windows path arguments (it was silently collapsing the source path).
#   * Pass Windows-style BACKSLASH absolute paths to csc.
#   * This csc only supports C# 5 (Default langversion). The source is written to
#     stay within C# 5 — do not add C#6+ syntax (string interpolation, ?., expression
#     -bodied members, etc.) or this compiler will reject it.
#
# The output DLL is NOT installed into GameData by this script.
set -euo pipefail
export MSYS2_ARG_CONV_EXCL='*'
export MSYS_NO_PATHCONV=1

CSC='C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
KSP='C:\Program Files (x86)\Steam\steamapps\common\Kerbal Space Program'
M="$KSP\\KSP_x64_Data\\Managed"
MJ="$KSP\\GameData\\MechJeb2\\Plugins"

# Absolute repo path (Windows backslash form), derived from this script's location.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ_UNIX="$SCRIPT_DIR/KspAutomationBridge"
# /c/... -> C:\... conversion for the source files.
PROJ_WIN="$(printf '%s' "$PROJ_UNIX" | sed -E 's#^/([a-zA-Z])/#\1:\\#; s#/#\\#g')"

OUT="${OUT:-C:\\tmp\\KspAutomationBridge.dll}"

"$CSC" -target:library -nologo -optimize+ "-out:$OUT" \
  "-reference:$M\\Assembly-CSharp.dll" \
  "-reference:$M\\Assembly-CSharp-firstpass.dll" \
  "-reference:$M\\UnityEngine.dll" \
  "-reference:$M\\UnityEngine.CoreModule.dll" \
  "-reference:$M\\UnityEngine.UI.dll" \
  "-reference:$M\\UnityEngine.IMGUIModule.dll" \
  "-reference:$M\\UnityEngine.InputLegacyModule.dll" \
  "-reference:$M\\UnityEngine.AnimationModule.dll" \
  "-reference:$M\\UnityEngine.TextRenderingModule.dll" \
  "-reference:$M\\UnityEngine.PhysicsModule.dll" \
  "-reference:$MJ\\MechJeb2.dll" \
  "-reference:$MJ\\MechJebLib.dll" \
  "$PROJ_WIN\\KspAutomationBridge.cs" \
  "$PROJ_WIN\\Properties\\AssemblyInfo.cs"

echo "Built: $OUT"
