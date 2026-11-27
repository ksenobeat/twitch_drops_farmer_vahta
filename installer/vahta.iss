; Инсталлер «Вахты». Нужен Inno Setup 6.3 или новее.
;
;     env\Scripts\python.exe build.py
;     "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer\vahta.iss
;
; Путь именно такой: Inno Setup поставлен на пользователя, а не в Program Files.
;
; Ставит на пользователя, в %LOCALAPPDATA%\Programs\Vahta: без UAC, без
; «запустите от администратора» и без проблем с записью — «Вахта» держит свои
; данные в папке data рядом с exe, а в Program Files туда писать нельзя.
;
; Данные при обновлении не теряются: инсталлер удаляет только то, что клал
; сам, а папку data создаёт приложение. Спрашивают о ней ровно один раз —
; при удалении.
;
; Установка поверх — это обновление, а не «удалить и поставить заново»:
; тот же AppId, та же папка, прежние галочки, data на месте. Запущенную «Вахту»
; инсталлер закрывает сам (она умеет выйти по просьбе) и поднимает обратно,
; если она работала. Единственное, что стирается перед копированием, — папка
; _internal прошлой сборки: PyInstaller меняет её состав от версии к версии,
; и оставленный от старой .pyd однажды выстрелит.

#include "version.iss"     ; #define AppVersion — пишет build.py из vahta/__init__.py

#define Name "Вахта"
#define Exe "Вахта.exe"
; Один на всё: и в [Setup], и в ключе деинсталляции, который читает [Code].
; Меняться ему нельзя — со сменой прошлая установка перестанет быть «прошлой»,
; и обновление превратится во вторую копию рядом.
#define AppId "{E002C0B4-C585-41F6-82D6-46B91CD1813E}"
#define Publisher "Ksenobeat"
#define Site "https://github.com/ksenobeat/twitch_drops_farmer_vahta"

[Setup]
AppId={{#AppId}
AppName={#Name}
AppVersion={#AppVersion}
AppPublisher={#Publisher}
AppPublisherURL={#Site}
AppVerName={#Name} {#AppVersion}
UninstallDisplayName={#Name} {#AppVersion}
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Vahta
DefaultGroupName={#Name}
DisableProgramGroupPage=yes
; на пользователя: прав администратора не просим и не требуем
PrivilegesRequired=lowest
; Обновление ставится туда же и с теми же галочками, что и прошлый раз. Оба
; значения — умолчания Inno, но здесь они несущие: без них обновление
; спрашивало бы заново то, на что человек уже отвечал.
UsePreviousAppDir=yes
UsePreviousTasks=yes
; «Вахта» живёт в трее и держит свои файлы открытыми. Сначала её просят выйти
; по-человечески (см. [Code]); если не вышло — сработает эта проверка и
; инсталлер попросит закрыть руками. Имя мьютекса то же, что в vahta/single.py
AppMutex=Vahta.SingleInstance
OutputDir=..\dist
OutputBaseFilename=Vahta-Setup-{#AppVersion}
SetupIconFile=..\vahta\static\vahta.ico
UninstallDisplayIcon={app}\{#Exe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Ярлык на рабочем столе"; GroupDescription: "Ярлыки:"
Name: "autostart"; Description: "Запускать «Вахту» вместе с Windows"; \
  GroupDescription: "Автозапуск:"

[InstallDelete]
; Состав _internal меняется от сборки к сборке: PyInstaller переименовывает
; библиотеки, а зависимости приходят и уходят. Inno перезаписывает только то,
; что кладёт сам, поэтому файл из прошлой версии остался бы лежать — и однажды
; был бы подхвачен вместо нового. Папку data это не касается: она рядом,
; а не внутри, и создаёт её приложение.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
; Папка целиком: сборка onedir — exe и _internal рядом. `data` исключена
; намеренно: стоит один раз запустить собранную «Вахту» из dist, чтобы что-то
; проверить, и рядом с exe появится папка с чужим входом в Twitch — которая
; молча уехала бы в инсталлер и перезаписала данные у того, кто его поставит.
Source: "..\dist\Вахта\*"; DestDir: "{app}"; Excludes: "data,data\*"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#Name}"; Filename: "{app}\{#Exe}"
Name: "{autodesktop}\{#Name}"; Filename: "{app}\{#Exe}"; Tasks: desktopicon

[Registry]
; Тот же ключ и ровно тот же вид команды, что ставит сама «Вахта»
; (vahta/autostart.py): иначе переключатель в настройках не увидел бы галочку
; из инсталлера и показывал бы «выключено» при включённом автозапуске.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "Vahta"; ValueData: """{app}\{#Exe}"" --autostart"; \
  Flags: uninsdeletevalue; Tasks: autostart

[Run]
; Работала до обновления — поднимаем молча: человек не просил её выключать,
; это сделали мы. Не работала — предлагаем галочкой, как при первой установке.
Filename: "{app}\{#Exe}"; Flags: nowait; Check: WasRunning
Filename: "{app}\{#Exe}"; Description: "Запустить {#Name}"; \
  Flags: nowait postinstall skipifsilent; Check: not WasRunning

[Code]
const
  { то же имя, что в vahta/single.py }
  MUTEX = 'Vahta.SingleInstance';
  { Ставимся на пользователя, поэтому и ключ деинсталляции в HKCU.
    Суффикс _is1 добавляет сам Inno. }
  UNINST = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppId}_is1';
  { Рантайм WebView2 предустановлен на всех Windows 11 и на Windows 10 начиная
    с обновления 2018 года. «Тем немногим», у кого его нет, окно не откроется —
    интерфейс уедет в браузер, и это рабочий, но не тот сценарий. }
  WV2_CLIENT = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WV2_LINK = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703';

var
  WasRunningBefore: Boolean;   { работала ли «Вахта», когда запустили инсталлер }
  PreviousVersion: string;     { что стоит сейчас; пусто — ставим впервые }

function WasRunning: Boolean;
begin
  Result := WasRunningBefore;
end;

function AskVahtaToQuit: Boolean;
var
  Code: Integer;
  Dir, Exe: string;
begin
  { «Вахта» умеет выйти по просьбе — тем же путём, что «Выход» в трее: она
    останавливает фарм, закрывает базу и запоминает окно. Просить об этом
    человека значило бы превращать обновление в «закрой, потом поставь»,
    а прибивать процесс — не дать ему всё это сделать.

    Просим её же exe: про порт и HTTP знает он, инсталлеру это ни к чему.
    Путь берём из реестра, а не из константы папки установки: на этом шаге она
    ещё не выбрана и раскрыть её нечем. (Фигурные скобки внутри комментария
    Pascal Script закрывают комментарий — поэтому здесь словами.) }
  Result := False;
  if not RegQueryStringValue(HKCU, UNINST, 'Inno Setup: App Path', Dir) then
    Exit;
  Exe := Dir + '\{#Exe}';
  if not FileExists(Exe) then
    Exit;
  Result := Exec(Exe, '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code)
    and (Code = 0) and not CheckForMutexes(MUTEX);
end;

function InitializeSetup: Boolean;
begin
  Result := True;
  RegQueryStringValue(HKCU, UNINST, 'DisplayVersion', PreviousVersion);
  WasRunningBefore := CheckForMutexes(MUTEX);
  if WasRunningBefore then
    { Не вышло — не беда: дальше сработает AppMutex, и инсталлер попросит
      закрыть руками, как раньше. Хуже, чем было, от попытки не станет. }
    AskVahtaToQuit;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  { Человеку важно видеть, что это обновление, а не вторая копия рядом:
    данные и вход в Twitch останутся на месте. }
  if (CurPageID = wpReady) and (PreviousVersion <> '') then
    WizardForm.ReadyMemo.Lines.Insert(0,
      'Обновление: ' + PreviousVersion + '  ->  {#AppVersion}' + #13#10
      + 'Папка data остаётся как есть — вход в Twitch и история'
      + ' не тронуты.' + #13#10);
end;

function HasWebView2: Boolean;
var
  Version: string;
begin
  { Машинная установка лежит в WOW6432Node, пользовательская — в HKCU }
  Result :=
    (RegQueryStringValue(HKLM,
       'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WV2_CLIENT, 'pv', Version)
     or RegQueryStringValue(HKLM,
       'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WV2_CLIENT, 'pv', Version)
     or RegQueryStringValue(HKCU,
       'Software\Microsoft\EdgeUpdate\Clients\' + WV2_CLIENT, 'pv', Version))
    and (Version <> '') and (Version <> '0.0.0.0');
end;

procedure InstallWebView2;
var
  Code: Integer;
begin
  { Бутстраппер скачивается на месте, а не лежит внутри: он обновляется сам
    по себе, весит полтора мегабайта и нужен единицам. Нет интернета — не беда:
    «Вахта» переживёт, интерфейс просто откроется в браузере. }
  try
    DownloadTemporaryFile(WV2_LINK, 'MicrosoftEdgeWebview2Setup.exe', '', nil);
    Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'),
         '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, Code);
  except
    MsgBox('Не вышло поставить компонент WebView2. «Вахта» установится и будет'
      + #13#10 + 'работать, но интерфейс откроется в браузере.',
      mbInformation, MB_OK);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not HasWebView2 then
    InstallWebView2;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: string;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;

  { Ключ автозапуска мог поставить не инсталлер, а сама «Вахта» из настроек —
    тогда uninsdeletevalue про него не знает. Снимаем в любом случае: ярлык
    на удалённую программу — это ошибка при каждом входе в систему. }
  RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Vahta');

  Data := ExpandConstant('{app}\data');
  if not DirExists(Data) then
    Exit;
  { Там вход в Twitch и история фарма. Молча удалить
    такое нельзя, молча оставить — значит насорить в системе. Спрашиваем,
    по умолчанию «нет».

    Именно SuppressibleMsgBox, а не MsgBox: при тихом удалении
    (/VERYSILENT /SUPPRESSMSGBOXES) обычный MsgBox не показывается и отвечает
    «да» — проверено, данные сносило молча. Здесь ответ по умолчанию задаётся
    явно, и тихое удаление данные сохраняет. }
  if SuppressibleMsgBox('Удалить данные «Вахты» — вход в Twitch'
      + #13#10 + 'и историю фарма?' + #13#10#13#10
      + 'Их стоит оставить, если «Вахта» ещё вернётся.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
    DelTree(Data, True, True, True);
end;
