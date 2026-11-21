# 오프라인 센서 입력 계약

초음파·LiDAR·카메라·리프트의 관측 정보를 통합하기 전에 식별자, 단위, 시각, 유효성, 보정 근거를 검사하는 독립 모듈입니다. 센서 데이터를 취득하거나 GPIO·직렬 포트·네트워크·모터에 접근하지 않습니다. 기존 팀원의 드라이버·서비스·배선 설정과 `manual_station`을 변경하지 않습니다.

현재 상태는 **오프라인 계약 검사 구현·합성 시험 완료 / 실물 센서 검증 미완료**입니다. 배터리 충전 중 실물 구동 금지 조건을 유지합니다.

## 실행

저장소 루트에서 Python 3.11 이상으로 실행합니다. 이 모듈은 Python 표준 라이브러리만 사용합니다.

```powershell
python -m unittest discover -s integration_contracts/tests -v
python -c "from pathlib import Path; from integration_contracts.observations import run_fixture_validation; r=run_fixture_validation(Path('outputs/my_contract_run')); print(r['passed'], r['total'], r['artifact'])"
```

두 번째 명령은 가짜 입력 15건을 검사하고 지정한 로컬 디렉터리에 `observation_contract_validation.json`을 저장합니다. 같은 디렉터리에서 다시 실행하면 이 보고서를 갱신하므로 보존할 시험은 새 디렉터리를 사용합니다. 보고서 생성은 실제 센서의 정상 판정이 아닙니다.

## 입력 스키마와 등록 정보

`Observation`은 불변 데이터 클래스이며 같은 필드의 사전도 입력할 수 있습니다. 알 수 없는 필드, 비문자 키, 누락된 필수 필드는 거절합니다.

| 필드 | 의미·검사 |
| --- | --- |
| `schema_version` | 현재 버전 1 |
| `source_id`, `sensor_kind` | 등록된 생산자와 종류: range/lidar/camera/lift; pose payload는 아직 미구현 |
| `physical_mount_id`, `frame_id` | 실제 장착 위치의 ID와 좌표계 ID. 코드 이름만으로 실제 방향을 확정하지 않음 |
| `sequence` | 스트림별 증가하는 정수. 중복·역순 입력 거절 |
| `sample_monotonic_s`, `receive_monotonic_s` | 측정 시각과 수신 시각. 음수·비유한·시간 역행·허용 범위를 넘는 미래 시각 거절 |
| `clock_domain`, `session_id` | 동일 단조 시계 영역과 부팅/실행 세대. 다른 세대끼리 시각 차를 계산하지 않음 |
| `units` | 등록 단위와 일치해야 함. 거리·리프트의 canonical 단위는 m; `distance_m`을 cm 값으로 사용하지 않음 |
| `validity` | `valid`, `unknown`, `no_return`, `invalid`. 무응답은 거리0이나 최대거리로 바꾸지 않음 |
| `quality`, `quality_kind` | 0~1 품질값과 `score`/`probability`의 구분. 확률값 자체가 정확도 보증은 아님 |
| `calibration_version` | 등록한 보정 버전과 일치하며 등록·보정 확인 근거가 필요 |
| `distance_m`, `uncertainty_m` | 거리와 불확실성. NaN/무한대/음수·불확실성 누락을 정상 측정으로 사용하지 않음 |
| `direction_rad`, `fov_rad` | 등록된 방향과 시야각. 방향·FOV 미확정은 UNKNOWN |
| `raw_evidence_id` | 같은 스캔/프레임에서 파생한 출처를 연결하는 ID. 카메라에는 프레임 참조가 필수 |
| `height_m`, `lift_reference_id` | 리프트 높이와 기준점 참조. 검증된 같은 세션의 기준점·리미트 증거 없이는 UNKNOWN |
| `measurement_basis` | measured/estimated/derived/replay/synthetic/synthetic_fixture. 추정값과 실측값을 구별 |

`SourceSpec`은 허용 source·mount·frame·calibration·단위, 최대 age, 품질/거리 범위, 방향/FOV, 필수 여부를 등록합니다. `registration_verified`와 `calibration_verified`의 기본값은 false입니다. 현물 확인 없이 true로 바꾸어 통과시키지 않습니다.

시계 허용 오차와 비교 오차는 생성자 설정이며 현재 기본값은 합성 계약 시험용입니다. 실물 지연·정확도·안전 한계를 입증한 숫자가 아닙니다. 재부팅 시 새 `session_id`의 새 monitor를 만들고, 이전 세션 관측을 이어 쓰지 않습니다.

## API 예시

다음은 **가짜 입력만 사용하는 예시**입니다. fixture의 확인 플래그는 합성 등록 정보에 한정됩니다.

```python
from integration_contracts.observations import (
    ObservationMonitor, fixture_spec, fixture_packet,
)

monitor = ObservationMonitor(
    [fixture_spec()],
    clock_domain="fixture_clock",
    session_id="fixture_boot_1",
)
result = monitor.ingest(fixture_packet(), now_monotonic_s=10.03)
snapshot = monitor.snapshot(now_monotonic_s=10.03)
print(result.status)                         # VALID: 입력 계약만 충족
print(snapshot["input_evidence_ready"])      # 필수 입력 계약 충족 여부
print(snapshot["actual_motion_permission"])  # 항상 False
print(monitor.snapshot(now_monotonic_s=11)["status"])  # STALE
```

`ingest()`는 스트림 순서와 최신 결과를 기록합니다. `snapshot()`은 읽기 전용이며 순번을 증가시키거나 오래된 측정 시각을 새로고침하지 않습니다. 최신 입력이 무효라면 이전 정상값을 계속 정상으로 표시하지 않습니다.

## 통합 결과의 의미

- `VALID`: 등록된 입력 계약을 충족했습니다. 관측 내용의 진실성이나 이동 안전성을 증명하지 않습니다.
- `STALE`: 마지막 측정이 등록한 유효기간을 넘었습니다.
- `UNKNOWN`: 필수 출처·품질·방향·기준점 등의 근거가 부족합니다.
- `CONFLICT`: frame/단위/세대/순서가 충돌하거나 비교 가능한 거리 증거가 일치하지 않습니다.

거리 비교는 등록된 `comparison_key`가 같고, 같은 표적·공통 frame·m 단위로 정규화된 비교라는 근거와 시각 조건이 충족될 때만 수행합니다. 서로 다른 센서 원점의 원시 거리를 바로 비교하지 않습니다. 불확실성 구간을 보존하며 불일치를 단순 평균으로 없애지 않습니다.

같은 `physical_mount_id`, 같은 `raw_evidence_id`, 명시된 `correlation_groups`를 공유하는 출처는 하나의 연결 그룹으로 묶습니다. 전원·USB 등의 공통원인은 등록된 정보에만 반영됩니다. 그룹 수를 통계적으로 독립인 센서 수로 해석하지 않으며 `statistical_independence_verified`는 항상 false입니다.

`input_evidence_ready`는 모든 필수 입력 계약이 충족되었다는 표시입니다. **`action_readiness`와 `actual_motion_permission`은 항상 false**이며 출력 허가 기능이 없습니다.

카메라 검사는 식별자·시각·보정·프레임 참조 메타데이터까지입니다. 이미지 픽셀·물체 검출·영상 정확도를 검증하지 않습니다. pose payload는 아직 구현하지 않아 UNKNOWN을 반환합니다. 리프트의 검증된 기준점이 있더라도 `estimated` 높이를 실측 높이로 바꾸지 않습니다.

## 검증한 합성 사례

| ID | 사례 | 기대 결과 |
| --- | --- | --- |
| O01 | 정상 등록·신선한 합성 관측 | VALID |
| O02 | 측정 시각 필드 누락 | UNKNOWN |
| O03 | 미등록 source | UNKNOWN |
| O04 | frame 불일치 | CONFLICT |
| O05 | 무한대 거리 | UNKNOWN |
| O06 | 음수 거리 | UNKNOWN |
| O07 | 측정/수신 시각 충돌 | CONFLICT |
| O08 | 다른 부팅 세대 | CONFLICT |
| O09 | 중복 순번 | CONFLICT |
| O10 | 측정 만료 | STALE |
| O11 | 방향 미확정 | UNKNOWN |
| O12 | 무응답 | UNKNOWN |
| O13 | 비교 가능한 거리 구간 불일치 | CONFLICT, 평균하지 않음 |
| O14 | 같은 장착에서 나온 파생 출처 | VALID, 증거 그룹 1개 |
| O15 | 리프트 기준점/리미트 근거 없음 | UNKNOWN |

별도의 15개 단위시험은 NaN, 단위·보정 불일치, 역순, 시계 역행, 공통 원시 프레임·전원 그룹, 잘못된 자료형, 필수 출처 누락 등 추가 하위 사례를 검증합니다. 이 결과는 전기적 안전, 실제 센서 취득, 위치 추정, 센서 융합 정확도, 실물 고장 진단 또는 하드웨어 안전인증의 완료 증거가 아닙니다.
