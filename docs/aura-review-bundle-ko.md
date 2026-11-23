# 3회 실험 결과에서 비교용 파일만 묶기

`export_aura_review_bundle.py`는 완료한 세션을 지정하면 JSON·로그만 별도 ZIP으로 복사한다. Python 3.13과 표준 라이브러리만 사용하므로 추가 설치나 AURA 업데이트가 필요 없다. 원본 결과를 삭제·이동·수정하지 않으며 기존 출력 ZIP과 체크섬도 덮어쓰지 않는다.

이 도구는 내부 검토용 결과를 복사한다. JSON에 포함된 계정 정보와 파일명은 유지되므로 공개 데이터셋에는 [별도로 작성한 비식별 요약](../public-evaluation/README.md)을 사용한다.

## Windows 실행

내려받은 스크립트를 `pyproject.toml`과 `.venv-win`이 있는 AURA 프로젝트 폴더에 둔다. PowerShell도 그 폴더에서 연다. 저장소의 `tools`에 있는 파일을 사용한다면 아래 스크립트 경로를 `.\tools\export_aura_review_bundle.py`로 바꾼다.

먼저 세션 폴더 이름을 확인한다.

```powershell
Get-ChildItem .\runs\sessions -Directory | Sort-Object Name | Select-Object -ExpandProperty Name
```

아래 `SESSION_ID_1`부터 `SESSION_ID_3`을 **본 실험의 실제 세션 ID**로 바꾼다. 파일럿이 섞이지 않도록 최신 세 개를 자동 선택하지 않는다. 입력 순서대로 1·2·3회 자료를 지정한다.

```powershell
$auraSessions = @(
    ".\runs\sessions\SESSION_ID_1\session.json"
    ".\runs\sessions\SESSION_ID_2\session.json"
    ".\runs\sessions\SESSION_ID_3\session.json"
)
.\.venv-win\Scripts\python.exe .\export_aura_review_bundle.py @auraSessions --runs-dir .\runs --output .\S21-review.zip
```

다른 기기는 출력 이름을 `S8-review.zip`, `Pixel5-review.zip`, `P30Lite-review.zip`처럼 바꾼다. 세션 수는 고정하지 않아 필요하면 한 회차만 또는 여러 기기를 함께 지정할 수도 있다. 기존 출력이 있으면 다른 `--output` 이름을 사용한다. 출력 폴더는 미리 존재해야 한다.

정답지와 회차 메모도 함께 넣으려면 다음 옵션을 붙인다. 선택 사항이다.

```powershell
.\.venv-win\Scripts\python.exe .\export_aura_review_bundle.py @auraSessions --runs-dir .\runs --reference .\reference.json --notes .\experiment-notes.txt --output .\S21-review-with-reference.zip
```

`--reference`는 여러 번 지정할 수 있다. 메모에는 사용한 배포본, 회차별 세션 ID, 캐시 유지 여부, 수동 개입·특이사항을 적는다. 결과 ZIP을 다른 위치로 옮겼다면 `--runs-dir`에 ZIP들이 모인 실제 폴더를 지정한다. 이 옵션을 생략하면 세션의 기록 경로와 일반적인 `runs` 구조를 조회한다. 원본 `session.json`의 경로는 수정하지 않는다.

## 포함하는 자료

- 각 세션의 `session.json`, 존재하는 `session.sha256`
- 각 앱 실행 ZIP의 `acquisition.json`, `events.jsonl`, `files.json`
- 하위 폴더의 JSON·JSONL 결과와 존재하는 원본 ZIP 체크섬
- 선택한 정답지 JSON과 회차 메모
- 파일별 크기·SHA-256, 입력 세션 순서, 원본 위치, 누락 경고를 담는 `bundle-manifest.json`

`acquisition.json`에서 원본 획득 파일로 분류한 항목은 확장자가 JSON이어도 제외한다. PNG·XML·영상·PDF·내보낸 원본 ZIP도 제외하며, 중첩 ZIP은 열지 않는다. 앱 실행 ID와 ZIP 내부의 경로는 유지한다.

```text
S21-review.zip
  README.txt
  bundle-manifest.json
  sessions/<session-id>/
    session.json
    session.sha256
    runs/<app-id>/<run-id>/
      acquisition.json
      events.jsonl
      files.json
      <기존 ZIP 내부의 앱별 하위 폴더와 JSON·JSONL>
      <run-id>.zip.sha256
  references/01/<정답지 파일명>.json
  notes/<메모 파일명>.txt
```

## 완료와 경고

- 종료 코드 `0`: 선택한 자료를 복사했고 경고가 없다.
- 종료 코드 `1`: ZIP은 생성했지만 누락 자료 등이 있다. 출력의 `warnings`와 ZIP 내부 `bundle-manifest.json`을 확인한다. 해당 ZIP도 검토용으로 공유할 수 있다.
- 종료 코드 `2`: 잘못된 경로, 해시 불일치, 손상된 ZIP 등의 오류. 이번 실행에서 만들던 출력은 제거하고 원본과 기존 출력은 보존한다.
- Ctrl+C: 만들던 출력만 정리하고 종료한다.

수집 ZIP이 없고 실행 폴더가 남아 있으면 그 폴더의 메타데이터를 복사한다. `files.json`은 정상 패키징 시 ZIP에만 들어가므로 실행 폴더에서 없으면 그 사실을 경고로 남긴다. ZIP과 실행 폴더가 모두 없으면 해당 실행을 누락으로 기록하고 나머지는 계속 묶는다. 식별자가 다르거나 존재하는 ZIP이 손상된 경우에는 다른 자료로 대체하지 않고 오류로 종료한다.

`session.sha256`이 있으면 세션 해시를 확인한다. ZIP의 `files.json`이 있으면 **복사하는 파일**의 크기·해시를 대조한다. 큰 원본 ZIP 전체의 해시는 새로 계산하지 않는다. 체크섬이 맞는 비교용 ZIP을 만들었다는 것은 빠진 사진·영상의 무결성이나 실제 획득의 완전성을 입증한다는 뜻이 아니다. 전체 결과 검증기는 원본 결과에 실행한다.

**생성된 `S21-review.zip`과 `S21-review.zip.sha256`을 공유하고, 원본 전체 결과는 PC에 그대로 보존한다.** 수집 코드·앱 프로파일·기기 설정은 변경하지 않는다.

검증: macOS에서 실제 ZIP·폴더를 사용하는 자동 검사로 세 세션 취합, 원본 내용·수정 시각 보존, 한글 경로, 원본 JSON 제외, 출력 덮어쓰기 방지와 오류 시 정리를 확인했다. Windows 네이티브 실행은 별도로 수행하지 않았다.
