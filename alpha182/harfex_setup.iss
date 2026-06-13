; Harfex Setup Script — Inno Setup 6
; Bu dosyayı Inno Setup ile açıp Compile edin → HarfexSetup.exe oluşur

#define MyAppName      "Harfex"
#define MyAppVersion   "1.8.2"
#define MyAppPublisher "R. Degerli"
#define MyAppURL       "https://harfex3d.com"
#define MyAppExeName   "Harfex.exe"
#define SourceDir      "C:\Users\rafet\Downloads\claud\LetterFormer_Alpha182_solid\alpha182\dist\Harfex"
#define OutputDir      "C:\Users\rafet\Downloads\claud\LetterFormer_Alpha182_solid\Setup"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\Harfex
DefaultGroupName=Harfex
AllowNoIcons=no
OutputDir={#OutputDir}
OutputBaseFilename=HarfexSetup_v{#MyAppVersion}
SetupIconFile={#SourceDir}\harfex.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
UninstallDisplayIcon={app}\Harfex.exe
UninstallDisplayName=Harfex {#MyAppVersion}
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription=Harfex Channel Letter Former
VersionInfoProductName=Harfex

[Languages]
Name: "turkish";  MessagesFile: "compiler:Languages\Turkish.isl"
Name: "english";  MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";    Description: "Masaüstüne kısayol oluştur";     GroupDescription: "Ek görevler:"; Flags: checkedonce
Name: "quicklaunchicon"; Description: "Görev çubuğuna sabitle";         GroupDescription: "Ek görevler:"; Flags: unchecked

[Files]
; Tüm dist/Harfex içeriğini kopyala
Source: "{#SourceDir}\Harfex.exe";    DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\harfex.ico";    DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\_internal\*";   DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Başlat menüsü
Name: "{group}\Harfex";               Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\harfex.ico"
Name: "{group}\Harfex Kaldır";        Filename: "{uninstallexe}"
; Masaüstü kısayolu (görev seçildiyse)
Name: "{autodesktop}\Harfex";         Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\harfex.ico"; Tasks: desktopicon

[Run]
; Kurulum bitince programı başlat (isteğe bağlı)
Filename: "{app}\{#MyAppExeName}"; Description: "Harfex'i şimdi başlat"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Kaldırırken kullanıcı verilerini silme — profiller korunsun
Type: filesandordirs; Name: "{app}\_internal"
