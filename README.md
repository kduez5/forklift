# forklift

Raspberry Pi 5 기반 지게차 프로젝트의 팀 공유 저장소입니다.

## 수동 제어 1단계

현재 상태: **SOFTWARE_READY / HARDWARE_PENDING** (2026-11-21).
기본 실행은 모의 모드이며, 시작하거나 화면을 여는 것만으로 실제 장치를 구동하지 않습니다. 실물 배선·극성·리프트 리미트는 현장에서 확인해야 합니다.

- [팀원 실행 안내](manual_station/README.md)
- [목적·전체 동작·작업지시서](manual_station/WORK_ORDER.md)
- [Codex /goal 실행 프롬프트](manual_station/GOAL_PROMPT.md)
- [검증 결과와 남은 확인](manual_station/VERIFICATION.md)
- [노션 작업지시서·검증 기록](https://app.notion.com/p/41f1cdebb6a58101a4ecedc61e57485c) — 별도 Notion 접근 권한 필요

모의 시험은 Windows Python 3.11에서 73개 통과·Linux 전용 1개 건너뜀, Pi5 Linux Python 3.13에서 74개 모두 통과했습니다. 실제 장치 구동 성공을 뜻하지 않습니다.

처음 받는 팀원은 Git과 Python 3.11 이상을 준비한 뒤:

```bash
git clone https://github.com/kduez5/forklift.git
cd forklift
```

Windows에서는 `python -m manual_station`, Linux에서는 `python3 -m manual_station`으로 모의 화면을 시작합니다. 터미널에 출력된 세션 주소를 브라우저에서 엽니다. 자세한 SSH 접속·조작·종료 방법은 [팀원 실행 안내](manual_station/README.md)를 따릅니다.

## 기존 휠 제어 자료

아래 ZIP은 기존에 공유한 자료입니다. 새 수동 제어 패키지의 검증 결과가 ZIP 내부 코드에 적용되는 것은 아닙니다.

[휠 제어 ZIP 다운로드](./wheel-control-90e7734.zip?raw=true)
