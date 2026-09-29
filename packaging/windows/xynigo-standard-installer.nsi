; Xynigo Sourcing per-user Windows installer.
; Compile with makensis and the defines supplied by 组装Windows标准安装包.sh.

Unicode True
RequestExecutionLevel user
SetCompressor /SOLID lzma

!ifndef APP_VERSION
  !error "APP_VERSION is required"
!endif
!ifndef APP_VERSION_QUAD
  !error "APP_VERSION_QUAD is required"
!endif
!ifndef APP_RUNTIME_ID
  !error "APP_RUNTIME_ID is required"
!endif
!ifndef PAYLOAD_DIR
  !error "PAYLOAD_DIR is required"
!endif
!ifndef OUTPUT_FILE
  !error "OUTPUT_FILE is required"
!endif
!ifndef LICENSE_FILE
  !error "LICENSE_FILE is required"
!endif
!ifndef INSTALLER_ICON
  !error "INSTALLER_ICON is required"
!endif
!ifndef INSTALLER_WELCOME_BITMAP
  !error "INSTALLER_WELCOME_BITMAP is required"
!endif
!ifndef INSTALLER_HEADER_BITMAP
  !error "INSTALLER_HEADER_BITMAP is required"
!endif
!ifndef STANDARD_GUI_LAUNCHER
  !error "STANDARD_GUI_LAUNCHER is required"
!endif
!ifndef STANDARD_LOGO_PNG
  !error "STANDARD_LOGO_PNG is required"
!endif
!ifndef STANDARD_ICON_ICO
  !error "STANDARD_ICON_ICO is required"
!endif
!ifndef STANDARD_LAUNCHER
  !error "STANDARD_LAUNCHER is required"
!endif
!ifndef STANDARD_PAIR_LAUNCHER
  !error "STANDARD_PAIR_LAUNCHER is required"
!endif
!ifndef MANAGED_EXECUTOR_STOP_SCRIPT
  !error "MANAGED_EXECUTOR_STOP_SCRIPT is required"
!endif

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "FileFunc.nsh"

!define APP_NAME "Xynigo Sourcing Production"
!define APP_PUBLISHER "Xynigo"
!define APP_ID "XynigoSourcing.Production.Executor"
!define APP_REG_KEY "Software\Xynigo\SourcingProduction"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}"
!define PROTOCOL_KEY "Software\Classes\xynigo-prod"

Name "${APP_NAME}"
OutFile "${OUTPUT_FILE}"
InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"
InstallDirRegKey HKCU "${APP_REG_KEY}" "InstallLocation"
BrandingText "${APP_NAME}"
Icon "${INSTALLER_ICON}"
UninstallIcon "${INSTALLER_ICON}"
ManifestDPIAware true
ShowInstDetails show
ShowUninstDetails show
AutoCloseWindow false

VIProductVersion "${APP_VERSION_QUAD}"
VIAddVersionKey /LANG=0 "ProductName" "${APP_NAME}"
VIAddVersionKey /LANG=0 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=0 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=0 "CompanyName" "${APP_PUBLISHER}"
VIAddVersionKey /LANG=0 "FileDescription" "${APP_NAME} Windows 安装程序"
VIAddVersionKey /LANG=0 "LegalCopyright" "Copyright Xynigo contributors"

!define MUI_ABORTWARNING
!define MUI_ICON "${INSTALLER_ICON}"
!define MUI_UNICON "${INSTALLER_ICON}"
!define MUI_WELCOMEFINISHPAGE_BITMAP "${INSTALLER_WELCOME_BITMAP}"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_RIGHT
!define MUI_HEADERIMAGE_BITMAP "${INSTALLER_HEADER_BITMAP}"
!define MUI_FINISHPAGE_NOAUTOCLOSE
!define MUI_UNFINISHPAGE_NOAUTOCLOSE
!define MUI_WELCOMEPAGE_TITLE "安装 Xynigo 桌面客户端"
!define MUI_WELCOMEPAGE_TEXT "连接 Xynigo 云端工作台与这台采购电脑。$\r$\n$\r$\n安装完成后，Xynigo 将通过桌面客户端和系统托盘运行；采购业务继续由云端工作台统一承载。"
!define MUI_FINISHPAGE_TITLE "Xynigo 已安装完成"
!define MUI_FINISHPAGE_TEXT "桌面客户端、本地执行器和托盘菜单已经安装。$\r$\n$\r$\n点击“完成”后可立即打开客户端并连接云端。"
!define MUI_FINISHPAGE_RUN "$INSTDIR\Xynigo.exe"
!define MUI_FINISHPAGE_RUN_TEXT "启动 Xynigo 桌面客户端"
!define MUI_FINISHPAGE_RUN_PARAMETERS "--show"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${LICENSE_FILE}"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

LangString CoreSectionName ${LANG_SIMPCHINESE} "Xynigo 本地执行器（必选）"
LangString CoreSectionName ${LANG_ENGLISH} "Xynigo local executor (required)"
LangString DesktopSectionName ${LANG_SIMPCHINESE} "创建桌面快捷方式"
LangString DesktopSectionName ${LANG_ENGLISH} "Create a desktop shortcut"
LangString UnsupportedArchitecture ${LANG_SIMPCHINESE} "当前安装包仅支持 64 位 Windows。"
LangString UnsupportedArchitecture ${LANG_ENGLISH} "This package requires 64-bit Windows."
LangString WebView2Missing ${LANG_SIMPCHINESE} "Xynigo 桌面客户端需要 Microsoft Edge WebView2 Runtime。点击“是”打开微软官方下载页；安装运行时后请重新运行本安装包。"
LangString WebView2Missing ${LANG_ENGLISH} "Xynigo Desktop requires Microsoft Edge WebView2 Runtime. Click Yes to open Microsoft's official download page, install the runtime, and then run this setup again."
Var OnlineUpdate
Function .onInit
  SetShellVarContext current
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/ONLINEUPDATE=" $OnlineUpdate
  ${If} ${Errors}
    StrCpy $OnlineUpdate "0"
    ClearErrors
  ${EndIf}
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "$(UnsupportedArchitecture)"
    Abort
  ${EndIf}
  SetRegView 32
  ReadRegStr $1 HKLM "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ${If} $1 == ""
    ReadRegStr $1 HKCU "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ${EndIf}
  ${If} $1 == ""
    MessageBox MB_ICONSTOP|MB_YESNO "$(WebView2Missing)" IDNO webview2_abort
    ExecShell "open" "https://developer.microsoft.com/microsoft-edge/webview2/"
    webview2_abort:
    Abort
  ${EndIf}
FunctionEnd

Section "$(CoreSectionName)" SEC_CORE
  SectionIn RO
  SetShellVarContext current

  ; An existing production install is stopped by its own path-scoped helper.
  ; A fresh install never touches a test launcher with the same executable name.
  IfFileExists "$INSTDIR\stop-managed-executors.ps1" 0 production_stopped
    nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\stop-managed-executors.ps1" -InstallDir "$INSTDIR"'
    Pop $0
    Pop $1
    ${If} $0 != 0
      DetailPrint "$1"
      Abort
    ${EndIf}
  production_stopped:

  ; Each immutable package revision has its own directory. This matters when a
  ; hotfix keeps the public APP_VERSION: reinstalling must still replace the
  ; Python executor instead of silently reusing an older same-version payload.
  ; current-version.txt selects the next runtime; user data stays outside it.
  SetOutPath "$INSTDIR\versions\${APP_RUNTIME_ID}"
  SetOverwrite off
  File /r "${PAYLOAD_DIR}\*.*"

  SetOutPath "$INSTDIR"
  SetOverwrite on
  File /oname=Xynigo.exe "${STANDARD_GUI_LAUNCHER}"
  File /oname=xynigo-logo.png "${STANDARD_LOGO_PNG}"
  File /oname=xynigo-x.ico "${STANDARD_ICON_ICO}"
  File /oname=Xynigo.cmd "${STANDARD_LAUNCHER}"
  File /oname=配对本地执行器.cmd "${STANDARD_PAIR_LAUNCHER}"
  File /oname=stop-managed-executors.ps1 "${MANAGED_EXECUTOR_STOP_SCRIPT}"
  FileOpen $0 "$INSTDIR\current-version.txt" w
  FileWrite $0 "${APP_RUNTIME_ID}"
  FileClose $0
  WriteUninstaller "$INSTDIR\卸载 Xynigo Sourcing.exe"

  ; Production starts with empty local data. Do not import test runtime state.

  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" \
    "$INSTDIR\Xynigo.exe" "--show" "$INSTDIR\xynigo-x.ico"
  Delete "$SMPROGRAMS\${APP_NAME}\本地执行器状态中心.lnk"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk" \
    "$INSTDIR\卸载 Xynigo Sourcing.exe"

  WriteRegStr HKCU "${APP_REG_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${APP_REG_KEY}" "Version" "${APP_VERSION}"
  WriteRegStr HKCU "${APP_REG_KEY}" "RuntimeId" "${APP_RUNTIME_ID}"
  WriteRegStr HKCU "${APP_REG_KEY}" "InstallType" "per_user_standard"

  ; Register the low-risk launcher protocol for cloud Web → local executor.
  WriteRegStr HKCU "${PROTOCOL_KEY}" "" "URL:Xynigo Launcher Protocol"
  WriteRegStr HKCU "${PROTOCOL_KEY}" "URL Protocol" ""
  WriteRegStr HKCU "${PROTOCOL_KEY}\DefaultIcon" "" \
    "$INSTDIR\xynigo-x.ico"
  WriteRegStr HKCU "${PROTOCOL_KEY}\shell\open\command" "" \
    '$\"$INSTDIR\Xynigo.exe$\" --protocol $\"%1$\"'

  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" \
    "$INSTDIR\xynigo-x.ico"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" \
    '$\"$INSTDIR\卸载 Xynigo Sourcing.exe$\"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" \
    '$\"$INSTDIR\卸载 Xynigo Sourcing.exe$\" /S'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Function .onInstSuccess
  ${If} $OnlineUpdate == "1"
    Exec '"$INSTDIR\Xynigo.exe" --show'
  ${EndIf}
FunctionEnd

Section /o "$(DesktopSectionName)" SEC_DESKTOP
  SetShellVarContext current
  CreateShortcut "$DESKTOP\${APP_NAME}.lnk" \
    "$INSTDIR\Xynigo.exe" "--show" "$INSTDIR\xynigo-x.ico"
SectionEnd

Section "Uninstall"
  SetShellVarContext current

  nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\stop-managed-executors.ps1" -InstallDir "$INSTDIR"'
  Pop $0
  Pop $1
  ${If} $0 != 0
    DetailPrint "$1"
    SetErrorLevel 1
    Abort
  ${EndIf}

  ; Keep the registered installation intact when a managed file is still locked.
  ClearErrors
  RMDir /r "$INSTDIR\versions"
  ${If} ${Errors}
    SetErrorLevel 1
    Abort
  ${EndIf}

  Delete "$DESKTOP\${APP_NAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APP_NAME}"

  DeleteRegKey HKCU "${PROTOCOL_KEY}"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  DeleteRegKey HKCU "${APP_REG_KEY}"
  DeleteRegKey /ifempty HKCU "Software\Xynigo"

  ; Remove managed application versions only. Deliberately preserve
  ; config.json, 查询日志, 日志, logs, 运行数据, data, 数据, imports and 导入文件.
  Delete "$INSTDIR\Xynigo.cmd"
  Delete "$INSTDIR\Xynigo.exe"
  Delete "$INSTDIR\xynigo-logo.png"
  Delete "$INSTDIR\xynigo-x.ico"
  Delete "$INSTDIR\配对本地执行器.cmd"
  Delete "$INSTDIR\current-version.txt"
  Delete "$INSTDIR\stop-managed-executors.ps1"
  Delete "$INSTDIR\卸载 Xynigo Sourcing.exe"
  RMDir "$INSTDIR"
SectionEnd
