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

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "FileFunc.nsh"
!include "nsDialogs.nsh"

!define APP_NAME "Xynigo Sourcing"
!define APP_PUBLISHER "Xynigo"
!define APP_ID "XynigoSourcing.Executor"
!define APP_REG_KEY "Software\Xynigo\Sourcing"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}"
!define PROTOCOL_KEY "Software\Classes\xynigo"

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
Page custom MigrationPageCreate MigrationPageLeave
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
LangString InvalidMigrationDir ${LANG_SIMPCHINESE} "指定的绿色包迁移目录无效；必须包含 VERSION.json。"
LangString InvalidMigrationDir ${LANG_ENGLISH} "The selected green-package migration directory is invalid; VERSION.json is required."
LangString MigrationFailed ${LANG_SIMPCHINESE} "旧绿色包数据迁移失败。安装已停止，原目录不会被修改。"
LangString MigrationFailed ${LANG_ENGLISH} "Green-package data migration failed. Setup has stopped and the source directory was not modified."
LangString MigrationPageTitle ${LANG_SIMPCHINESE} "迁移旧版数据"
LangString MigrationPageTitle ${LANG_ENGLISH} "Migrate existing data"
LangString MigrationPageSubtitle ${LANG_SIMPCHINESE} "可选迁移绿色包中的配置、日志和运行数据"
LangString MigrationPageSubtitle ${LANG_ENGLISH} "Optionally migrate configuration, logs, and runtime data"
LangString MigrationCheckboxText ${LANG_SIMPCHINESE} "从旧绿色包迁移现有数据（推荐已有用户使用）"
LangString MigrationCheckboxText ${LANG_ENGLISH} "Migrate data from an existing green package"
LangString MigrationExplain ${LANG_SIMPCHINESE} "只复制 config.json、日志、运行数据和导入文件；不会复制旧程序、Python 运行时或删除源目录。"
LangString MigrationExplain ${LANG_ENGLISH} "Only config, logs, runtime data and imports are copied. The source is never changed."
LangString MigrationBrowseText ${LANG_SIMPCHINESE} "浏览…"
LangString MigrationBrowseText ${LANG_ENGLISH} "Browse..."
LangString MigrationBrowseTitle ${LANG_SIMPCHINESE} "选择包含 VERSION.json 的旧绿色包目录"
LangString MigrationBrowseTitle ${LANG_ENGLISH} "Select the existing package folder containing VERSION.json"

Var MigrateDir
Var MigrationCheckbox
Var MigrationDirectory
Var MigrationBrowse
Var OnlineUpdate

Function MigrationPageCreate
  !insertmacro MUI_HEADER_TEXT "$(MigrationPageTitle)" "$(MigrationPageSubtitle)"
  nsDialogs::Create 1018
  Pop $0
  ${If} $0 == error
    Abort
  ${EndIf}

  ${NSD_CreateCheckbox} 0 4u 100% 14u "$(MigrationCheckboxText)"
  Pop $MigrationCheckbox
  ${NSD_OnClick} $MigrationCheckbox MigrationToggle

  ${NSD_CreateLabel} 0 24u 100% 26u "$(MigrationExplain)"
  Pop $0

  ${NSD_CreateDirRequest} 0 58u 76% 13u "$MigrateDir"
  Pop $MigrationDirectory
  ${NSD_CreateBrowseButton} 79% 57u 21% 15u "$(MigrationBrowseText)"
  Pop $MigrationBrowse
  ${NSD_OnClick} $MigrationBrowse MigrationBrowseClick

  ${If} $MigrateDir != ""
    ${NSD_Check} $MigrationCheckbox
    EnableWindow $MigrationDirectory 1
    EnableWindow $MigrationBrowse 1
  ${Else}
    EnableWindow $MigrationDirectory 0
    EnableWindow $MigrationBrowse 0
  ${EndIf}
  nsDialogs::Show
FunctionEnd

Function MigrationToggle
  ${NSD_GetState} $MigrationCheckbox $0
  ${If} $0 == ${BST_CHECKED}
    EnableWindow $MigrationDirectory 1
    EnableWindow $MigrationBrowse 1
  ${Else}
    EnableWindow $MigrationDirectory 0
    EnableWindow $MigrationBrowse 0
  ${EndIf}
FunctionEnd

Function MigrationBrowseClick
  nsDialogs::SelectFolderDialog "$(MigrationBrowseTitle)" "$MigrateDir"
  Pop $0
  ${If} $0 != error
    StrCpy $MigrateDir $0
    ${NSD_SetText} $MigrationDirectory "$MigrateDir"
  ${EndIf}
FunctionEnd

Function MigrationPageLeave
  ${NSD_GetState} $MigrationCheckbox $0
  ${If} $0 != ${BST_CHECKED}
    StrCpy $MigrateDir ""
    Return
  ${EndIf}
  ${NSD_GetText} $MigrationDirectory $MigrateDir
  GetFullPathName $MigrateDir "$MigrateDir"
  IfFileExists "$MigrateDir\VERSION.json" valid
    MessageBox MB_ICONSTOP "$(InvalidMigrationDir)"
    Abort
  valid:
FunctionEnd

!macro MigrateDirectory ID NAME
  IfFileExists "$MigrateDir\${NAME}\*.*" migrate_${ID} migrate_done_${ID}
  migrate_${ID}:
  CreateDirectory "$INSTDIR\${NAME}"
  ExecWait '$\"$SYSDIR\robocopy.exe$\" $\"$MigrateDir\${NAME}$\" $\"$INSTDIR\${NAME}$\" /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /XJ /XO /XN /XC' $1
  ${If} $1 >= 8
    SetErrors
    Return
  ${EndIf}
  migrate_done_${ID}:
!macroend

Function .onInit
  SetShellVarContext current
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/MIGRATEDIR=" $MigrateDir
  ${If} ${Errors}
    StrCpy $MigrateDir ""
    ClearErrors
  ${EndIf}
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
FunctionEnd

Function MigrateGreenPackageData
  ClearErrors
  StrCmp $MigrateDir "" done
  GetFullPathName $MigrateDir "$MigrateDir"
  StrCmp $MigrateDir "$INSTDIR" done
  IfFileExists "$MigrateDir\VERSION.json" valid
    MessageBox MB_ICONSTOP "$(InvalidMigrationDir)"
    SetErrors
    Return
  valid:
  IfFileExists "$INSTDIR\config.json" config_done
    IfFileExists "$MigrateDir\config.json" 0 config_done
      CopyFiles /SILENT "$MigrateDir\config.json" "$INSTDIR"
      IfErrors 0 config_done
        Return
  config_done:
  !insertmacro MigrateDirectory qlog "查询日志"
  !insertmacro MigrateDirectory zhlog "日志"
  !insertmacro MigrateDirectory logs "logs"
  !insertmacro MigrateDirectory runtime "运行数据"
  !insertmacro MigrateDirectory data "data"
  !insertmacro MigrateDirectory zhdata "数据"
  !insertmacro MigrateDirectory imports "imports"
  !insertmacro MigrateDirectory zhimports "导入文件"
  done:
FunctionEnd

Section "$(CoreSectionName)" SEC_CORE
  SectionIn RO
  SetShellVarContext current

  ; A user-initiated upgrade may run while the tray owns the executor.
  ; Stop only this user's Xynigo launcher/process tree before replacement.
  ${If} $OnlineUpdate == "1"
    ; The verified installer is launched by the Python child. Killing the
    ; whole launcher tree would also kill this installer, so online mode only
    ; stops the status-center process after the child has requested exit.
    nsExec::ExecToStack '"$SYSDIR\taskkill.exe" /IM Xynigo.exe /F'
    Sleep 800
  ${Else}
    nsExec::ExecToStack '"$SYSDIR\taskkill.exe" /IM Xynigo.exe /T /F'
  ${EndIf}
  Pop $0
  Pop $1

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
  FileOpen $0 "$INSTDIR\current-version.txt" w
  FileWrite $0 "${APP_RUNTIME_ID}"
  FileClose $0
  WriteUninstaller "$INSTDIR\卸载 Xynigo Sourcing.exe"

  Call MigrateGreenPackageData
  ${If} ${Errors}
    MessageBox MB_ICONSTOP "$(MigrationFailed)"
    Abort
  ${EndIf}

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

  nsExec::ExecToStack '"$SYSDIR\taskkill.exe" /IM Xynigo.exe /T /F'
  Pop $0
  Pop $1

  Delete "$DESKTOP\${APP_NAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APP_NAME}"

  DeleteRegKey HKCU "${PROTOCOL_KEY}"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  DeleteRegKey HKCU "${APP_REG_KEY}"
  DeleteRegKey /ifempty HKCU "Software\Xynigo"

  ; Remove managed application versions only. Deliberately preserve
  ; config.json, 查询日志, 日志, logs, 运行数据, data, 数据, imports and 导入文件.
  RMDir /r "$INSTDIR\versions"
  Delete "$INSTDIR\Xynigo.cmd"
  Delete "$INSTDIR\Xynigo.exe"
  Delete "$INSTDIR\xynigo-logo.png"
  Delete "$INSTDIR\xynigo-x.ico"
  Delete "$INSTDIR\配对本地执行器.cmd"
  Delete "$INSTDIR\current-version.txt"
  Delete "$INSTDIR\卸载 Xynigo Sourcing.exe"
  RMDir "$INSTDIR"
SectionEnd
