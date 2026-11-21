# 지게차 첫 강화학습 실험

독립된 PC 모의 실험 패키지입니다. `manual_station/`, 팀원 드라이버, Pi 서비스와 GPIO를 호출하지 않습니다. 목표 상대 거리·각도의 합성 관측으로 목표 접근 → 방향 정렬 → STOP을 학습합니다. NumPy tabular Q-learning을 실제로 수행하며 체크포인트, 전이 보상 기록과 별도 평가 결과를 저장합니다.

## 실행

프로젝트 루트에서 Python 3.11+와 NumPy가 필요합니다. 현재 검증 환경은 Python 3.11.2 / NumPy 2.3.1입니다.

```powershell
python -m autonomy_lab --output outputs/my_rl_run --episodes 2500
python -m autonomy_lab.server --run-dir outputs/my_rl_run --observations outputs/rl_resume_20261121 --port 8766
```

브라우저에서 `http://127.0.0.1:8766`을 엽니다. 화면은 기록 열람 전용이며 학습·모터 실행 버튼이 없습니다. 서버를 종료하려면 실행 터미널에서 Ctrl+C를 누릅니다. 포트가 사용 중이면 다른 포트를 지정합니다.

동일 출력 폴더·설정으로 `--episodes 5000`처럼 총 횟수를 늘리면 마지막 정상 체크포인트에서 이어갑니다. 코드/설정/seed가 바뀌면 새 출력 폴더를 사용해야 합니다. 완료된 결과를 덮어 재평가하면 기존 평가 파일이 교체되므로 보존할 실험에는 새 폴더를 사용합니다.

```powershell
python -m unittest discover -s autonomy_lab/tests -v
```

## 학습·평가의 의미

- 행동: STOP, FORWARD, REVERSE, LEFT, RIGHT. 리프트 행동은 없습니다.
- 1.8×1.8m 공간, 차체 0.26×0.18m, 전방 포크 0.10m는 **실측하지 않은 가정**입니다. 기본 실험은 내부 장애물이 없는 공간입니다.
- 입력은 합성 목표 상대 거리·방향·최종 헤딩입니다. 화면의 라이다 광선/초음파/카메라 라벨은 합성 표시이며 정책에 장애물 인식이나 센서 융합을 학습시키지 않습니다.
- 명령 응답/노이즈/누락을 달리하지만 실제 모터 식별값은 아닙니다. STOP은 즉시 정지로 이상화합니다. 실제 제동·미끄러짐·전원·전도·하중·충격을 검증하지 않습니다.
- 외곽 충돌 예상 요청은 모의 감독기가 거부합니다. 따라서 물리 충돌 0은 안전 인증이 아니며 거부 횟수를 별도로 읽어야 합니다.
- 요청과 실제 적용 행동을 모두 기록합니다. 강제 STOP 전이는 요청 행동의 실제 차단 결과와 벌점으로 학습하며 STOP을 잘했다고 보상하지 않습니다.
- 실제 성공 종료에서는 bootstrap하지 않고 외부 시간 제한에서는 bootstrap합니다. 평가는 epsilon=0, Q 갱신 금지 및 전후 해시 일치를 검사합니다.
- 학습 환경 seed는 0~999999, 개발 평가는 2000000대입니다. 개발 결과를 보고 튜닝했다면 해당 평가는 최종 평가가 아닙니다. 코드를 동결한 뒤 미사용 seed로 `--eval-seed-start 3000000 --evaluation-label final_holdout`을 사용합니다. 이미 본 최종 조건은 새 후보의 개발 자료로 간주하고 다음 최종 seed를 새로 정합니다.
- 학습 정책, 미학습 Q=0 정책(STOP), 비학습 기하 제어기를 같은 환경 seed/감독기 조건으로 비교합니다. 미학습 STOP보다 좋다는 것만으로 기존 제어기보다 우수하다고 주장하지 않습니다.
- 재생은 첫 성공 사례(없으면 첫 사례)를 선택합니다. 전체 분포는 `evaluation.json`의 `episodes_detail`에 있습니다.

## 결과 파일

`training.jsonl`은 에피소드별 보상·성공·차단·TD 변화량, `checkpoint.json`은 RNG/설정/해시/진행 지점, `q_table*.npz`는 pickle 없는 Q 값입니다. `evaluation.json`에는 전체 비교와 실패 이유가, `representative_trace.json`에는 표시용 센서/행동 궤적이 있습니다. 학습기와 환경 소스 해시가 달라지면 재개가 거절됩니다.

`outputs/rl_resume_20261121`의 실제 카메라 사진은 별도 1회 관측이며 학습 데이터로 사용하지 않았습니다. 실제 LiDAR/초음파가 없으면 화면에 미수신으로 표시합니다. 카메라 표시 180° 회전은 원본 또는 차체 좌표 보정이 아닙니다.

## 실패 재생과 개선 후보

```powershell
python -m autonomy_lab.diagnose --run-dir outputs/my_rl_run
python -m autonomy_lab.candidate --output outputs/my_candidate --episodes 2500
```

`diagnose`는 원본 Q와 환경 소스 해시를 확인하고 실패 조건만 같은 정책으로 재생합니다. 기존 평가·체크포인트·학습 로그를 변경하지 않습니다. 이 진단은 위 기본 `autonomy_lab` 실험 전용이며 후보 실험에는 사용하지 않습니다.

`candidate`는 접근 → 1회 STOP → 방향 정렬을 명시한 **단계 규칙+RL**입니다. 정렬 진입 0.065m/이탈 0.12m는 모의 설계 가정이고, 정렬 중에는 STOP/좌우회전만 허용합니다. 기존 실패 7건의 전후 왕복을 겨냥한 별도 후보이며 원본 환경과 보상을 바꾸지 않습니다. 학습의 Bellman bootstrap에도 다음 단계의 허용 행동만 사용합니다. 실제 리프트나 센서 융합 학습은 포함하지 않습니다.

확정 실행 결과는 [VERIFICATION.md](VERIFICATION.md)를 참고하세요. 새 평가에서 기존 정책보다 성공률이 높았지만, 기하 기준기·동일 단계 규칙도 모두 성공했습니다. RL 단독의 우월성이나 실물 성능 향상을 입증하지 않았습니다. 여러 학습 seed와 비이상적 동역학 시험은 남아 있습니다.

이 PC에서 완료한 두 실험을 함께 열려면:

```powershell
python -m autonomy_lab.server --run-dir outputs/autonomy_rl_run_20261121 --candidate-dir outputs/autonomy_rl_candidate_run_20261121 --observations outputs/rl_resume_20261121 --port 8766
```

이 출력 폴더들은 로컬 실행 증거입니다. GitHub에서 새로 받은 팀원은 위의 학습 명령으로 자신의 출력 폴더를 생성하세요. 실물 사진·SSH 인증·개인 로그는 코드 패키지에 포함하지 않습니다.

## 실물 전환 전 남은 것

단일 제어 프로세스 확인, 바퀴 채널/전진/STOP 실측, 완성 차체·포크 외곽, 센서 장착 방향/시간/전압, 리프트 초기 위치·상하한·하중 유지, 보호정지와 재개 계약이 필요합니다. 이 패키지는 이 조건을 우회해 실물에 정책을 적용하는 기능을 제공하지 않습니다.

작업지시서: https://app.notion.com/p/41f1cdebb6a581dfb891d81c66aec43b
