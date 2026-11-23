# Windows에서 AURA 실행하기

이 안내는 Windows에서 AURA를 설치하고 승인된 Android 기기에서 수집을 실행하는 절차를 설명한다. 공개 데이터와 출처 추적 예제는 [데이터셋 안내](public-evaluation/README.md)에서 확인할 수 있다.

기존 결과는 유지하고 **새 폴더**에 압축을 푼 뒤 `pyproject.toml`이 있는 폴더에서 PowerShell을 연다. 프로파일이 변경됐으므로 이 배포본에서 `configure`로 세션 설정을 새로 생성한다. 이전 가상환경은 복사하지 않는다. Python과 ADB, 설치할 때의 인터넷 연결이 필요하다.

## 1. Python과 가상환경

Python 3.13 64-bit를 사용한다. 기존 Windows 시험에서 사용한 Python 3.13.1도 가능하다.

```powershell
py -3.13 --version
py -3.13 -m venv .venv-win
.\.venv-win\Scripts\python.exe -m pip install -e ".[windows,test]"
.\.venv-win\Scripts\python.exe -m pip check
```

설치할 때 인터넷 연결이 필요하다. `No broken requirements found.`가 나오면 의존성 확인이 끝난다. 가상환경의 Python을 직접 호출하므로 Activate.ps1이나 실행 정책 변경은 필요 없다. Python과 Android SDK Platform-Tools(ADB)는 별도로 설치한다.

```powershell
.\.venv-win\Scripts\python.exe -c "import aura, pywinauto; print(aura.__file__); print('Windows dependencies imported')"
adb version
adb devices -l
```

ADB가 PATH에 없다면 실제 설치한 `platform-tools` 경로를 현재 PowerShell에 추가한다. 예:

```powershell
$env:Path = "$env:USERPROFILE\Downloads\platform-tools;$env:Path"
adb devices -l
```

대상 기기를 USB로 연결하고 잠금을 해제한다. USB 디버깅을 켜고 기기에 표시되는 디버깅 허용 창을 승인한다. 대상 앱은 평가 계정에 로그인된 상태여야 한다. `unauthorized`가 아닌 `device` 상태를 확인한다.

`adb devices -l`에 표시된 대상 시리얼을 아래 변수에 넣는다. `DEVICE_SERIAL`은 실제 값으로 바꾼다.

```powershell
$auraSerial = 'DEVICE_SERIAL'
```

`--serial`을 생략하면 승인된 연결 기기가 정확히 한 대여야 한다. 지정한 기기가 없으면 다른 기기로 전환하지 않고 중단한다. 선택한 시리얼은 결과 `session.json`에 기록된다. 이 기능이 같은 프로젝트 폴더에서 동시 수집을 지원하는 것은 아니다.

## 2. 설치 버전과 프로파일 확인

```powershell
.\.venv-win\Scripts\python.exe -m aura inspect --serial $auraSerial
```

대상 앱의 프로파일 상태가 `exact`인지 확인한다. `nearest` 또는 `no_profile`은 수집 가능한 상태라는 뜻이 아니다. 현재 포함된 앱 프로파일은 25개이며, 기기와 앱 버전에 맞는 프로파일을 사용한다.

## 3. 세션 설정 생성과 실행

Chrome만 device-only로 먼저 확인하는 예:

```powershell
.\.venv-win\Scripts\python.exe -m aura configure chrome --environment device_only --output chrome-session-config.json --serial $auraSerial
.\.venv-win\Scripts\python.exe -m aura acquire chrome --config .\chrome-session-config.json --serial $auraSerial
```

`configure`가 제시하는 번호 중 실제 실행할 조합을 선택하고 근거·참조·확인자를 입력한다. 선택한 수집 범위와 프로파일 해시를 확인한 뒤 승인하면 JSON이 생성된다. 실제 확인을 대신하는 사전 승인 문서는 포함하지 않았다. 같은 파일명이 있으면 덮어쓰지 않으므로 다른 `--output` 이름을 사용한다.

전체 7개 앱의 실행 후보를 확인하려면:

```powershell
.\.venv-win\Scripts\python.exe -m aura configure chrome sbrowser google_drive notesnook telegram notion whatsapp --environment all --output session-config.json --serial $auraSerial
```

프로파일에 정의된 환경별 대상이 있는 조합만 표시된다. Drive는 device-only에서 목록·기본 메타데이터만, Notion은 계정·워크스페이스·홈의 페이지 목록만 수집한다. Drive 활동 내역·원본, Notion 멤버·본문·DB·첨부·Export는 controlled-online에만 표시된다. 나머지 앱은 구현된 대상에 두 환경을 유지한다.

원하는 번호만 선택한다. WhatsApp Export만 실행하려면 WhatsApp Materialize 번호는 선택하지 않는다. `all`은 표시된 모든 조합을 선택하므로 실제 시험 범위를 확인한 경우에만 입력한다. 온라인 조합은 원격 획득 허용 여부와 외부 트래픽 제한 상태도 입력한다. 이 PoC가 AP 트래픽 제한을 직접 구현하지는 않는다.

```powershell
.\.venv-win\Scripts\python.exe -m aura acquire chrome sbrowser google_drive notesnook telegram notion whatsapp --config .\session-config.json --serial $auraSerial
```

이번 배포본은 현재 작업 트리의 25개 앱 버전 프로파일과 3개 시스템 UI 프로파일을 포함한다. 설정의 해시를 수동으로 고치지 말고 현재 기기·앱 버전으로 `configure`를 실행한다. 기존 형식의 권한 JSON도 `--config`로 읽을 수 있으나 현재 프로파일 해시와 실행 범위가 일치해야 한다.

입력 세션 설정에는 `execution_scopes`, `authorization`, `network_control`이 들어간다. 결과 `session.json`에는 설정 원문과 해시, 실제 실행·복원 결과를 보존한다. 기존 `authorize`와 `--authorization`도 호환 목적으로 유지한다.

`sbrowser`는 `samsung_browser`의 CLI 별칭이다. 프로파일이 바뀌면 세션 설정 JSON을 새로 생성한다.

두 환경의 WhatsApp Export만 먼저 확인하는 예:

```powershell
.\.venv-win\Scripts\python.exe -m aura configure whatsapp --method export --environment all --output whatsapp-session-config.json --serial $auraSerial
.\.venv-win\Scripts\python.exe -m aura acquire whatsapp --config .\whatsapp-session-config.json --serial $auraSerial
```

Windows 기본 수신 위치를 그대로 사용한다. 새 수신 파일은 결과 폴더에 보존·해시 검증한 뒤 수신 폴더에서 제거한다. 이전 실험의 동명 Export가 Downloads 등에 남아 있다면 먼저 별도 폴더에 보관한다. AURA는 기존 파일을 자동으로 이동·삭제하거나 덮어쓰지 않는다. 이번 패키지는 macOS에서 코드·프로파일과 Windows 수신 모의 테스트를 검사한다. 이 배포본의 실제 Windows 수신은 Windows PC에서 확인한다.

Windows Bluetooth를 켜고 PC의 페어링 확인 창을 승인할 수 있는 상태로 둔다. 이미 페어링돼 있어도 된다. 수신 대상 이름은 실행 중인 Windows의 호스트 이름을 사용한다. 수집 중에는 기기를 직접 조작하지 않는다.

Samsung·generic Android/Pixel·Huawei 프로파일에서 공통 Bluetooth 준비·정리 절차를 사용한다. 기존 페어링은 유지하고 해당 실행에서 만든 휴대폰 측 페어링만 정리한 뒤 전원을 직전 상태로 복원한다. 휴대폰의 대상 일치 Pair 확인은 자동 처리하며, Windows 코드는 사용자가 확인한다. 이전 실험 결과·세션 설정·사용 배포본은 수정하지 않고 별도로 보존한다. 이번 추가 기록은 환경 확인 시점의 SSID 관찰이며, 지정 네트워크를 강제하거나 통신을 연속 감시하는 기능은 아니다.

## 4. 결과 보존과 검증

`runs`에 앱·방식·환경별 ZIP과 세션 기록이 생성된다. 최종 출력의 `session_record` 및 해당 세션의 모든 ZIP·해시 파일을 함께 보존한다. `partial`은 일부 항목 미획득 또는 미확정을 뜻하므로 항목별 사유를 확인한다. 환경 정책으로 제외한 대상은 `acquisition.json`의 `excluded_targets`에 `not_attempted` / null 획득 상태로 구분하며 실패로 계산하지 않는다.

```powershell
.\.venv-win\Scripts\python.exe tools\validate_aura_results.py --help
.\.venv-win\Scripts\python.exe tools\evaluate_aura_results.py --help
.\.venv-win\Scripts\python.exe tools\review_aura_results.py --help
```

정답지와 실제 수집 패키지는 이 코드 배포본에 포함하지 않았다. 공개 요약과 두 검토 예제는 `public-evaluation/`에 있다.

## 5. 기기 없이 코드 검사

```powershell
.\.venv-win\Scripts\python.exe -m pytest -q
.\.venv-win\Scripts\python.exe tools\audit_aura_profiles.py --json profile-audit.json
```

수집 관련 테스트와 테스트용 fixture를 포함했다. 논문 초안 빌드 전용 테스트는 배포 범위에서 제외했다. 공개 테스트와 실험 자료의 검증 범위는 [검증 안내](docs/VALIDATION.md)에 정리했다.

## 6. 같은 기기에서 3회 순차 실행

현재 실행이 끝난 뒤, `$auraSerial`에 지정한 기기가 연결된 상태에서:

```powershell
.\.venv-win\Scripts\python.exe .\tools\run_aura_repetitions.py chrome sbrowser google_drive notesnook telegram notion whatsapp --config .\session-config.json --serial $auraSerial
```

기본 3회이며 `--count 2`로 새 실행 횟수를 바꿀 수 있다. 캐시·기존 결과를 유지한다. 이 일반 반복 스크립트는 휴대폰의 비브라우저 첨부 수신본을 회차 사이에 자동 정리하지 않는다. 앞선 Mac 실험의 수신본 정리 조건을 재현하려면 별도 정리 절차가 필요하다. 회차별 종료/복원을 확인하고 일반적인 partial은 계속하지만 실행·수신·복원 오류나 Ctrl+C에는 멈춘다. [상세 안내와 결과 번들 명령](docs/aura-repetitions-ko.md)을 확인한다.
