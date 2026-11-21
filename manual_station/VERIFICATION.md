# 수동 제어 검증 기록 · 2026-11-21

상태: **SOFTWARE_READY / HARDWARE_PENDING**

## 확인된 범위

- Windows Python3.11.2: 74개 시험, 73통과·POSIX 잠금 1개 건너뜀.
- Raspberry Pi5 Debian13 ARM64 Python3.13.5: 동일 74개 모두 통과, 파일 잠금 포함.
- `--check-config`: `hardware_accessed:false`; 미확정 바퀴·리프트 설정은 거부 이유를 반환.
- 브라우저 MOCK: ARM 자체 출력0, 전진 키 누름 명령8건 후 해제 정지, 리프트 상승 누름 명령6건 후 해제 정지, ESTOP·RESET·관찰 기록1건·제어권 반납 확인. 마지막 상태 DISARMED, 모든 출력0.
- 모의 화면 상태 조회의 짧은 대기시간으로 발생한 간헐적 연결 끊김을 보정했다. 상태 조회만 최대1초이며 조작 요청250ms, 서버 명령만료300ms는 유지한다. 보정 후 연속 조작과 대기에서 연결 유지·콘솔 오류 없음 확인.
- 안전 시험에는 독립 watchdog, 2초 연속입력 제한, 오래된 명령과 ARM/RESET, 로그 지연·실패, 부분 GPIO 실패, PWM 완료 경합, 제어권/프로세스 경쟁, 반복되는 PWM 큐 방지, 리프트 리미트가 포함된다. 실제 장치 대신 모의 객체를 사용했다.

## 배치와 보존

배치: `/srv/jongsul/forklift/manual_station`.
그룹 `jongsul-team`, 디렉터리2770, 파일660, 실행 스크립트770. 프로세스 잠금의 부모 `runtime/`도2770이다.

배치 파일은 SHA-256으로 로컬과 일치 확인했다. 아래 기존 파일5개도 배치 전후 동일하다.

- `README.md`
- `motor_control/manual_control.py`
- `motor_control/forward_wheels.py`
- `mot_linear/linear_control.py`
- `forklift-runtime/config/pi5-wheel-wiring.yaml`

모의 UI 서버는 시험 후 종료했다. 이 설치는 실물 서버나 systemd/cron 자동시작을 실행하지 않았다.

## 남은 현장 확인

실제 바퀴 핀·좌우·극성, EN 비활성 유지, 전원·공통접지, 물리 차단, 리프트 입력·리미트·하중 유지가 미확정이다. `pin_candidates.json`의 충돌을 추측으로 해결하지 않았다. 센서·지도·물류 전체 흐름의 실물 인수도 남아 있다.

수동 소프트웨어 검증은 실제 모터 구동 성공이나 전체 자율 물류 완성을 뜻하지 않는다. 시작·종료·개인 설정 방법은 [실행 안내](README.md)를 따른다.

원본 시험 출력·배치 해시·모의 UI 검증 결과와 화면은 개인 개발 환경에 보관한다. GitHub에는 이 검증 요약과 재실행 가능한 시험 코드를 제공하며, 개인 실행 로그·세션 토큰·장치별 설정은 포함하지 않는다.
