# 지게차 수동 시험 스테이션

기본 실행은 **MOCK / 모의 출력**입니다. 사용자가 직접 서버를 시작하고 제어권·ARM·누름 입력을 순서대로 수행합니다. 프로그램 시작이나 화면 열기만으로 움직이지 않습니다. 실물 배선과 극성이 확인되지 않아 기본 설정으로 실물 출력은 허용되지 않습니다.

## 팀원이 처음 받기

Git과 Python 3.11 이상을 준비합니다. 모의 실행은 Python 표준 라이브러리만 사용하며 추가 패키지 설치가 필요하지 않습니다. Windows Python 3.11과 Pi5 Linux Python 3.13에서 검증했습니다.

```bash
git clone https://github.com/kduez5/forklift.git
cd forklift
```

개인 PC에서 실물 없이 화면을 확인하려면 저장소 루트에서 실행합니다.

Windows PowerShell:

```powershell
python -m manual_station
```

Linux:

```bash
python3 -m manual_station
```

터미널에 출력된 `http://127.0.0.1:8765/#token=...` 주소를 같은 PC의 브라우저에서 엽니다. 시작만으로 조작자 제어권이나 ARM을 획득하지 않습니다. 종료는 서버 터미널에서 Ctrl+C입니다.

## 공동 파이에서 화면과 조작 흐름 확인

각자 개인 Linux 계정으로 VS Code Remote SSH에 접속합니다. 공동 파이에는 이미 `/srv/jongsul/forklift/manual_station`이 배치되어 있습니다. 같은 공동 배치를 사용하며 다른 팀원이 서버를 사용 중이면 협의 후 시작합니다.

파이의 VS Code 터미널에서:

```bash
cd /srv/jongsul/forklift
sh manual_station/run_mock.sh
```

Windows PowerShell 별도 터미널에서:

```powershell
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 YOUR_PI_SSH_ALIAS
```

`YOUR_PI_SSH_ALIAS`는 본인의 개인 계정으로 연결하도록 설정한 SSH 별칭으로 바꿉니다. 공용 로그인이나 다른 팀원의 인증 정보를 복사하지 않습니다.

서버 터미널에 출력된 `http://127.0.0.1:8765/#token=...` 주소를 Windows 브라우저에서 엽니다. 토큰은 해당 서버 실행 동안만 유효하며 종료 후 재사용할 수 없습니다. 주소를 공개하지 마세요. VS Code Ports를 사용한다면 로컬 포트도8765로 맞추세요. 모의 서버의 포트 충돌 시 서버의 `--port`와 SSH 양쪽 포트를 같은 새 번호로 바꿉니다. 실물 제어는 아래 공동 배치 규칙을 따릅니다.

Windows에서 파이 없이 UI만 확인하려면 이 폴더의 `run_mock.ps1`을 실행하거나, 부모 디렉터리에서 `python -m manual_station`을 실행합니다.

1. 화면에서 MOCK 표시를 확인합니다.
2. 조작자 이름을 입력하고 제어권을 가져옵니다.
3. 바퀴/리프트 시험 종류를 선택하고 준비 확인 후 ARM을 누릅니다. 아직 출력은0입니다.
4. 버튼을 **누르고 있는 동안** 또는 WASD/방향키로 조작합니다. 단일 채널은 선택한 채널의 전기적 정/역방향입니다.
5. 키·버튼을 놓거나 Space/STOP으로 정지합니다. 계속하려면 다시 ARM을 누릅니다. 연속2초·입력만료·창이탈도 정지와 재무장을 요구합니다.
6. ESTOP은 래치됩니다. RESET만으로 움직이지 않으며 별도 ARM이 필요합니다.
7. 조작을 마치면 제어권을 반납하고 서버 터미널에서 Ctrl+C로 종료합니다.

출력20%는 PWM 명령 비율입니다. 실제 속도나 토크 측정값이 아닙니다. 모의 바퀴 위치·리미트 상태는 화면의 모의 표시와 함께 읽습니다.

## 실물 실행 전 설정

`config.template.json`을 **개인 홈의 별도 파일**로 복사하고 재조립 배선에 맞춰 작성합니다. `pin_candidates.json`은 서로 충돌하는 현재 코드·과거 문서의 후보 목록이며 실행 설정으로 쓰지 않습니다. `confirmed`와 안전 확인 항목을 근거 없이 true로 바꾸지 마세요.

```bash
mkdir -p "$HOME/.config/forklift-manual"
cp -n manual_station/config.template.json "$HOME/.config/forklift-manual/wiring.json"
```

이 복사는 설정 초안만 만들며 기존 파일은 덮어쓰지 않습니다. 실제 핀·극성·안전 확인 항목을 확인한 뒤 개인 파일을 편집합니다.

필수 확인 항목:

- 바퀴별 IN1/IN2/EN의 실제 BCM 번호, 좌우 위치, 정방향 극성
- 다른 GPIO 프로그램 종료, 핀/버스 중복 없음, 전원·공통접지·EN 점퍼·OFF 유지
- 바퀴 무부하 및 주변 통제, 현장에서 즉시 전원을 차단할 수 있는 수단
- 리프트는 별도로 드라이버 방식·상승극성·상하한 입력·입력 정상·정전 시 하중 유지

이 스테이션은 첫 무부하 시험용입니다. 확인되지 않은 센서나 상단 하중 조건을 정상으로 간주하지 않습니다. 같은 Pi의 소프트웨어 watchdog는 OS정지·전원손실·기계적 하중 유지에 대한 보증이 아닙니다.

설정 검사만 하기(장치 접근 없음):

```bash
cd /srv/jongsul/forklift
python3 -m manual_station --backend real --config "$HOME/.config/forklift-manual/wiring.json" --check-config
```

현장 확인을 마친 **사용자가 직접** 실물 대기 서버를 시작하는 명령:

```bash
sh manual_station/run_real.sh --config "$HOME/.config/forklift-manual/wiring.json"
```

이 명령은 REAL 모드의 DISARMED 화면을 엽니다. 실제 GPIO는 확인된 scope의 ARM 때 LOW로 확보하며, 새 조작 명령 전에는 움직이지 않습니다. 설정 오류·점유중 GPIO·확인되지 않은 좌우/리프트는 거부 이유를 표시합니다. 에이전트는 위 실물 명령과 ARM을 실행하지 않았습니다.

## 팀 사용과 기록

- 각자 개인 SSH 계정으로 접속합니다. 실물 출력은 단일 프로세스 잠금과 한 명의 조작자 제어권으로 제한합니다. 기존 팀 스크립트는 이 잠금을 사용하지 않으므로 함께 실행하지 마세요.
- 실물은 공동 경로 `/srv/jongsul/forklift/manual_station` 한 곳에서 실행합니다. 잠금 파일은 패키지의 `runtime/controller.lock`이므로, 같은 파이에 개인별 clone을 만들고 각각 실물 모드를 실행하면 공용 잠금이 분리됩니다. 개인 clone은 개발·모의 시험에 사용합니다.
- 현재 공동 배치의 `runtime/`은 `jongsul-team` 그룹과2770 권한으로 준비되어 있습니다. Git에는 실행 중 잠금 파일을 올리지 않습니다. 새 공동 배치를 만드는 관리자는 아래 준비 절차를 따릅니다.
- SQLite 기록은 서버를 실행한 사용자 홈 `~/.local/state/forklift-manual/events.sqlite3`에 저장됩니다. 명령·정지·거부·관찰 기록이며 실제 물류 성공을 자동 판정하지 않습니다.
- 카메라·LiDAR·NFC·초음파는 이번 패키지에서 실물 취득을 시작하지 않습니다. UI의 미연결 표시는 정상값이나 센서 정상 판정이 아닙니다.
- 설치로 systemd·cron·로그인 자동시작을 추가하지 않습니다. 패키지에 계정 인증 파일을 넣지 않습니다.

새 공동 배치를 준비할 때만, 모든 제어 서버가 종료된 상태에서 `jongsul-team` 그룹의 관리자가 실행합니다. 이 명령은 디렉터리와 권한만 준비하며 서버나 장치를 실행하지 않습니다.

```bash
cd /srv/jongsul/forklift
mkdir -p manual_station/runtime
chgrp jongsul-team manual_station/runtime
chmod 2770 manual_station/runtime
```

팀원은 같은 공동 경로에 접근할 그룹 권한을 받아야 합니다. 잠금 파일을 삭제하여 다른 서버의 제어권을 빼앗지 않습니다. 코드 업데이트도 모든 조작을 종료한 뒤 반영합니다.

## 하드웨어 없는 검증

부모 폴더에서:

```bash
python3 -m unittest discover -s manual_station/tests -v
python3 -m manual_station --check-config
```

단위시험은 가짜 backend/GPIO만 사용합니다. mock 웹 시험은 실물 배선과 무관하며, 실물 인수는 별도 기록이 필요합니다.

## 전체 목적과 다음 단계

[작업지시서](WORK_ORDER.md), [실행 프롬프트](GOAL_PROMPT.md), [검증 기록](VERIFICATION.md)을 함께 제공합니다. 현재 단계는 수동 조작·정지·기록입니다. 센서만 읽기 → 지도/영상과 수동 주행 → 하단 한 건 집기·운반·하역 → 반복·실패 대응 → 상단/작업 큐 순서로 확장하며 각 단계의 실물 증거를 따로 남깁니다.

[노션 작업지시서](https://app.notion.com/p/41f1cdebb6a58101a4ecedc61e57485c)는 별도의 Notion 접근 권한이 필요합니다. GitHub 저장소 접근 권한이 Notion이나 파이 SSH 접근 권한을 자동으로 부여하지 않습니다.
