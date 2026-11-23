# 기기별 3회 연속 실행

현재 수집이 끝난 다음 적용한다. 대상 기기를 연결하고 `pyproject.toml`이 있는 프로젝트 폴더에서 PowerShell을 연다. 기존 가상환경과 이 배포본에 맞는 `session-config.json`을 준비한다. 반복 스크립트는 새 의존성이 없다.

```powershell
.\.venv-win\Scripts\python.exe .\tools\run_aura_repetitions.py chrome sbrowser google_drive notesnook telegram notion whatsapp --config .\session-config.json
```

기본값은 **새 세션 3회**다. 각 회차에서 설정에 선택된 device-only → controlled-online 전체 순서를 실행한다. 현재 실행 중이거나 이전에 완료한 세션은 자동으로 포함하지 않는다. 두 번만 더 실행하려면 같은 명령 끝에 `--count 2`를 붙인다. 한 앱만 반복하려면 앱 이름 하나만 지정한다. 다른 기기로 교체한 뒤에는 해당 기기의 버전·프로파일에 맞는 설정을 사용한다.

- 대상 시리얼을 지정하려면 명령 끝에 `--serial DEVICE_SERIAL`을 붙인다. 값은 `adb devices -l`에서 확인한다. 생략하면 승인된 연결 기기가 정확히 한 대여야 한다. 여러 기기가 연결되어 있어도 지정한 한 기기만 사용하며, 각 회차의 수집 프로세스에도 처음 선택한 시리얼을 전달한다. 대상이 사라지면 다른 기기로 전환하지 않고 중지한다.
- 한 실행은 한 기기를 대상으로 한다. 같은 프로젝트 폴더에서 다른 AURA 수집을 동시에 실행하지 않는다. 시리얼 지정은 동시 수집 지원을 추가하는 기능이 아니다.
- 입력 설정을 `experiment-runs/repetitions-<시각>/session-config.json`에 복사해 모든 회차에 동일하게 사용한다. 원래 설정은 변경하지 않는다. 코드·프로파일·앱 버전도 반복 중 변경하지 않는다.
- 캐시, 앱 데이터, 열린 탭과 이전 결과는 지우지 않는다. 캐시를 초기화한 동일 상태 반복이 아니라, 합의한 **캐시 유지 조건의 순차 반복**이다. 기존 CLI의 초기 상태 기록과 종료 복원을 그대로 사용한다.
- 회차가 끝나고 최종 상태와 Wi-Fi·DND·알림 숨김의 시작 상태 복원이 확인돼야 다음 회차를 시작한다. 별도 고정 대기나 앱 초기화는 추가하지 않는다.
- 정상 종료한 `partial`도 한 회차로 기록하고 계속한다. 실패 회차를 지우거나 성공 3회를 채우려고 재실행하지 않는다. 실행 실패·미완료 계획·복원 실패·누락/불일치 세션 기록과 WhatsApp Windows 수신/원본 보존 오류는 반복을 중지한다. 전송 재시도가 성공했다면 이전 시도의 오류만으로 반복을 중지하지 않는다.
- Ctrl+C를 한 번 누르면 다음 회차를 시작하지 않고 현재 AURA의 종료 처리를 기다린다. 종료 처리가 끝나기 전에 창을 닫거나 프로세스를 강제로 종료하면 복원/기록이 끝나지 않을 수 있다.
- WhatsApp의 새 PC 페어링 승인은 여전히 사용자 조작이다. 미리 페어링해둘 수 있다.

콘솔 출력은 영어이고 기존 AURA 진행 메시지를 그대로 볼 수 있다. 새 `repetitions.json`에는 회차 번호, 시리얼, 세션 ID/경로, 종료 코드, 앱별 계측과 중단 사유가 남는다. `completed`는 요청한 반복 횟수를 실행했다는 뜻이며 모든 항목 획득을 뜻하지 않는다. 스크립트 종료 코드는 전체 complete=0, 반복 완료하되 partial 포함=1, 중단/오류=2, 사용자 취소=130이다.

## 결과 번들 만들기

스크립트 시작/종료 시 표시되는 반복 기록 폴더를 아래 경로에 넣는다. 순서대로 저장된 `sessions.txt`를 기존 번들 생성기에 전달한다. 누락된 회차를 임의로 보충하지 않는다.

```powershell
$auraRepeatDir = '.\experiment-runs\repetitions-실제시각'
$auraSessions = Get-Content -Encoding UTF8 -LiteralPath "$auraRepeatDir\sessions.txt"
.\.venv-win\Scripts\python.exe .\tools\export_aura_review_bundle.py @auraSessions --runs-dir .\runs --notes "$auraRepeatDir\repetitions.json" --output .\S21-review.zip
```

기존 출력 ZIP 이름이 있으면 다른 이름을 쓴다. 원본 `runs`와 반복 기록 폴더를 보존한다. 앱별 시간 비교에는 `session.json`의 `elapsed_seconds`를 사용하고 스크립트 전체 경과 시간과 섞지 않는다.

## WhatsApp 기본 수신 폴더

Windows의 저장 경로 입력란을 읽거나 변경하지 않는다. 실제 저장된 파일은 기존 Documents/Downloads 및 발견된 Bluetooth 수신 폴더에서 찾는다. Finish 후 수신 창이 닫히고 파일이 안정화된 다음 ZIP·대화 형식을 확인한다. 기존 ArtifactStore로 보존하고 원본/보존본 SHA-256과 원본 변경 여부를 확인한 뒤, **이번 Windows 전송으로 새로 생성된 수신 파일만** 수신 폴더에서 제거한다. 두 경로·해시·제거 결과를 이벤트에 남긴다. 실패하거나 형식이 잘못된 수신본은 남겨둔다. 외부 호스트 수동 수신 방식은 기존 복사를 유지한다.

이전 실험에서 받은 같은 이름의 파일이 수신 폴더에 이미 있으면 Finish를 누르지 않고 `receive_filename_conflict`로 기록한다. 기존 파일은 사용자가 별도 보관 폴더로 옮긴 다음 새 실행을 시작한다. 현재 실행 중인 전송 파일은 옮기지 않는다. 임의의 첫 번째 입력란 선택이나 덮어쓰기 승인, 이미지 기반 판단은 사용하지 않는다.

macOS에서 모의 Windows 컨트롤과 실제 임시 파일/자식 프로세스로 검증한다. 실제 Windows UI와 장시간 3회 실행은 사용자 환경에서 확인할 부분이다. 최초 `receiver_ui_error`의 정확한 Windows UIA 실패 원인은 미확정이며, 후속 오류에는 실패 작업 단계를 기록한다.
