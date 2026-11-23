# AURA 평가 실행 및 검토

논문에 보고한 네 기기의 결과와 집계 기준은 [공개 데이터셋 안내](../public-evaluation/README.md)에 있다. 공개 예제 두 개는 기기 연결 없이 저장소 루트에서 [README의 검토 명령](../README.md#try-a-provenance-example)으로 확인할 수 있다.

## 기기에서 새로 실행하기

1. Python 3.13 이상과 ADB를 준비한다. 대상 기기의 잠금을 해제하고 USB 디버깅 연결을 승인한다. 계정이 필요한 앱에는 연구에 사용할 계정으로 로그인한다.
2. 저장소 루트에서 `python3 -m pip install -e .`로 설치하고 `python3 -m aura inspect`로 앱 버전과 프로파일을 확인한다. Windows의 Bluetooth 수신 준비는 [Windows 실행 안내](../START-WINDOWS-KO.md)를 따른다.
3. 예를 들어 Chrome의 device-only 획득은 `python3 -m aura configure chrome --environment device_only --output chrome-config.json`으로 구성한다. 권한 범위와 실행 조건을 확인한 후 `python3 -m aura acquire chrome --config chrome-config.json`으로 시작한다.
4. 두 환경을 선택하면 계획된 device-only 획득을 모두 마친 뒤 controlled-online 획득으로 전환한다. 회차별 결과를 보존하며 반복하려면 [반복 실행 안내](aura-repetitions-ko.md)를 따른다.

## 결과 검토

각 세션의 `session.json`과 해시, 실행 ZIP 및 해시 파일을 함께 보존한다. [결과 검증 및 추적](../README.md#validation)에서 실행할 명령을 확인할 수 있다. 검증 결과와 함께 시도별 절차 상태, 실제 획득 상태, 화면 근거를 확인한다. 반복 획득 결과를 비교할 때는 동일한 기기·앱·방법·환경의 회차들을 묶는다.
