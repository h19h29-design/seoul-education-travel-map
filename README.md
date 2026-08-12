# Seoul Education Travel Map

서울 소재 교육기관을 출발지로 선택하고 출장지까지의 복수 경로, 예상 이동시간·거리·비용과 근무지 내외 여비 판단을 함께 확인하는 공개 지도 서비스입니다.

- 로그인 없는 공개 웹 MVP
- 서울교육청 소속기관, 국·공·사립 학교와 유치원 출발지 지원
- 최단시간·최단거리·최저비용 경로 비교
- 서울 행정경계와 12 km 지도 지원영역 표시
- 법적 판정은 일반적인 실제 경로의 왕복거리와 출장시간을 사용
- 기관 위치와 사용자의 신분·적용 여비규정을 분리

실행, 데이터 동기화, 보안 설정과 배포 절차는 [운영 안내](apps/travel-map/README.md)를 참고하세요. 구현 계획은 [MVP 계획](docs/superpowers/plans/2026-08-10-seoul-education-travel-map-mvp.md)에 기록되어 있습니다.

## Repository layout

- `apps/travel-map`: FastAPI 애플리케이션, 공개 지도 UI, 테스트 및 릴리스 도구
- `docs/superpowers/plans`: MVP와 공공 자동차·도보 경로엔진 후속 계획

## Release status

코드와 오프라인 회귀 검증은 완료됐습니다. 실제 공개 배포는 다음 조건이 충족될 때까지 의도적으로 차단됩니다.

1. 운영 API 키가 배포 비밀 저장소에 등록됨
2. 공식 원천으로 생성하고 검토자가 승인한 기관 스냅샷이 존재함
3. 25개 자치구를 포괄하는 대표 경로 30쌍을 수동 검수함
4. Docker 이미지 빌드와 `/healthz` 운영 확인을 완료함

테스트 fixture나 합성 기관 데이터를 운영 스냅샷으로 승격해서는 안 됩니다.

## Mirroring

GitHub 저장소가 원본입니다. `.github/workflows/mirror-to-gitlab.yml`은 `main`과 태그를 GitLab 미러로 전송합니다. GitHub 저장소 비밀값 `GITLAB_MIRROR_TOKEN`에는 대상 GitLab 프로젝트에 쓰기 권한만 가진 토큰을 등록해야 합니다.
