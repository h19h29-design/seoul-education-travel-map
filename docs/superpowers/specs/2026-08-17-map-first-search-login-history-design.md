# 지도 중심 검색·편도 출장·카카오 이력 설계

**작성일:** 2026-08-17

**대상 서비스:** Seoul Education Travel Map

**기준 커밋:** `e335c6f`

**상태:** 사용자 승인 완료

## 1. 목표

현재 공개 출장지도 MVP를 다음 방향으로 확장한다.

1. 기관 필터와 기관 목록을 운영 데이터 기준으로 정상화한다.
2. 네이버 지도 길찾기처럼 출발기관과 출장지를 검색하고 목록에서 선택하면 지도에 즉시 표시한다.
3. 왕복, 일정 후 퇴근, 출장지로 바로 출근 후 근무지 복귀를 각각 실제 이동 방향에 맞게 계산한다.
4. 출장시간 입력으로 종료시각을 자동 계산하고 수동 종료시각 변경 시 출장시간을 역산한다.
5. 공개 서비스의 적용 규정을 서울특별시교육청 공무원 여비 기준으로 고정한다.
6. 사용안내, 관련규정, 계산이력, 설정을 실제 기능으로 만든다.
7. 계산은 로그인 없이 유지하고, 카카오 로그인 사용자에게만 기본 근무지·설정과 7일 계산이력을 제공한다.
8. 로그인 사용자 데이터는 NAS의 10 TB 볼륨에 최소·암호화 형태로 보관한다.

이 기능은 지급 승인 시스템이나 재직 확인 시스템이 아니다. 결과는 예상값이며 기관의 최종 판단을 대체하지 않는다.

## 2. 현재 상태와 확인된 결함

### 2.1 기관 필터

- 프론트엔드는 검색어가 2자 미만이면 기관 API를 호출하지 않고 목록을 숨긴다.
- 필터 변경도 검색 입력 이벤트만 다시 발생시키므로 빈 검색어 상태에서는 필터만으로 목록을 볼 수 없다.
- 기관 API와 저장소는 이미 빈 검색어와 필터 조합을 지원한다.
- 화면의 기관유형, 교육지원청, 자치구 선택지가 하드코딩돼 운영 스냅샷의 값을 대부분 누락한다.
- 운영 활성 사이트 대부분의 `siteName`은 `main`인데 화면은 `officialName` 대신 `siteName`을 기본 표시한다.
- 검색 결과가 0건일 때 목록을 숨겨 0건과 오류를 구분할 수 없다.

### 2.2 출장지 검색

- 장소명 키워드 검색과 결과 클릭 선택은 존재한다.
- 일반 도로명·지번주소 검색은 공개 장소 검색 흐름에 결합돼 있지 않다.
- 지도 클릭 역지오코딩 결과는 선택 확인을 요구하지만, 검색 후보 위치를 선택하기 전 지도에서 미리 확인하는 흐름은 부족하다.

### 2.3 시간과 복귀 의미

- 현재 `returnsAt`은 여비 산정의 업무 종료시각이자 복귀 경로 출발시각이다.
- 이 계약으로는 복귀 경로가 없는 “일정 후 퇴근”과 출발 경로가 없는 “출장지로 바로 출근”을 명확히 표현할 수 없다.

### 2.4 헤더 기능

- 사용안내, 관련규정, 계산이력, 설정은 현재 실제 기능이 아니라 앵커 또는 동작 없는 버튼이다.
- 인증, 세션, 사용자 저장소, 브라우저 영구 저장소는 존재하지 않는다.

## 3. 확정된 제품 원칙

### 3.1 로그인 정책

- 모든 검색·지도·경로·여비 계산은 로그인 없이 가능해야 한다.
- 카카오 로그인은 7일 계산이력과 사용자 설정을 위한 선택 기능이다.
- 카카오 로그인 성공을 서울특별시교육청 재직 또는 공무원 신분 확인으로 해석하지 않는다.
- 카카오 이메일, 닉네임, 프로필 이미지, 액세스 토큰, 리프레시 토큰은 저장하지 않는다.

### 3.2 저장 정책

- 계산이력은 생성 후 정확히 168시간 동안만 조회할 수 있다.
- 만료 이력은 읽기·쓰기 시 정리하고, 매시간 물리 삭제한다.
- 사용자 설정은 사용자가 “내 데이터 삭제”를 실행할 때까지 유지한다.
- 이력 DB는 10 TB 볼륨에 두되 장기 백업에는 포함하지 않는다.
- 집 주소, 자택 좌표, 전체 경로선, 제공자 원문 응답은 저장하지 않는다.

### 3.3 기술 방향

- React 전면 재작성 없이 기존 FastAPI와 바닐라 JavaScript를 유지한다.
- 커진 프론트엔드 책임은 기능별 ES 모듈로 분리한다.
- 단일 NAS·소규모 테스트 환경에 맞춰 서버측 불투명 세션과 SQLite를 사용한다.
- 사용자 관련 민감 필드는 애플리케이션 계층에서 AES-GCM으로 암호화한다.

## 4. 사용자 경험

### 4.1 지도 중심 통합 검색

데스크톱은 왼쪽 검색·결과 패널과 오른쪽 지도를 사용한다. 모바일은 검색, 후보 목록, 일정, 결과, 접이식 지도 순으로 배치한다.

상단의 길찾기 검색 영역에는 다음 두 입력이 항상 보인다.

1. 출발기관
2. 출장지

두 입력 모두 자유 텍스트 그 자체는 선택으로 인정하지 않는다. 서버가 반환한 후보를 클릭하거나 키보드로 확정해야 계산할 수 있다.

### 4.2 출발기관 검색

- 입력 포커스 시 빈 검색어로 첫 20개 기관을 표시한다.
- 필터만 선택해도 즉시 첫 20개 결과를 표시한다.
- 결과에는 `officialName`을 기본 이름으로 표시한다.
- `siteName`이 `main` 또는 그에 준하는 기본 사이트면 별도 표기하지 않는다.
- 본관·분관처럼 실제 구분이 필요한 다중 사이트만 기관명 뒤에 사이트명을 붙인다.
- 결과 부가정보는 기관유형, 설립구분, 자치구, 도로명주소 순으로 표시한다.
- 결과 총수와 “더 보기”를 제공한다.
- 로딩, 0건, 네트워크 오류를 서로 다른 상태로 표시한다.

기관 필터는 현재 승인 스냅샷에서 만든 facet을 사용한다.

- 기관유형
- 설립구분
- 교육지원청
- 자치구

화면에 필터값을 하드코딩하지 않는다. 교육지원청은 정규화된 식별값과 한국어 표시명을 분리해 논리적으로 같은 명칭이 중복되지 않게 한다.

### 4.3 출장지 검색

- Kakao 키워드 검색과 Kakao 주소 검색을 동시에 수행한다.
- 장소명, 도로명주소, 지번주소를 검색할 수 있다.
- 제공자 ID가 같거나 좌표와 정규화 주소가 같은 후보는 하나로 합친다.
- 이름 또는 주소의 정확 일치, 접두 일치, 부분 일치 순으로 안정 정렬한다.
- 후보에 장소명, 도로명주소, 지번주소를 표시한다.
- 후보를 선택하면 즉시 지도에 핀을 놓고 해당 위치로 이동한다.
- 지도 클릭의 역지오코딩 후보도 동일한 후보 목록에 넣고 사용자가 다시 선택해야 확정한다.
- 검색 문자열, 선택 전 후보, 지도 클릭 위치는 서버 로그에 남기지 않는다.

### 4.4 출장 형태

`TripPattern`은 다음 세 값만 허용한다.

```text
ROUND_TRIP
OUTBOUND_ONLY_END_AFTER_SCHEDULE
RETURN_ONLY_DIRECT_TO_DESTINATION
```

사용자 표시와 경로 호출은 다음과 같다.

| 표시명 | 이동 방향 | 출발 경로 | 복귀 경로 |
| --- | --- | --- | --- |
| 일반 왕복 | 근무지 → 출장지 → 근무지 | `startsAt` 출발 | `endsAt` 출발 |
| 일정 후 퇴근 | 근무지 → 출장지 | `startsAt` 출발 | 호출하지 않음 |
| 출장지로 바로 출근 후 근무지 복귀 | 출장지 → 근무지 | 호출하지 않음 | `endsAt` 출발 |

자택과 출장지 사이의 이동은 모델링하거나 저장하지 않는다.

### 4.5 출장시간

화면은 다음 값을 제공한다.

- 출장 시작 일시
- 출장 종료 일시
- 출장시간

동기화 규칙은 다음과 같다.

1. 사용자가 “5시간”을 입력하면 `endsAt = startsAt + 5시간`으로 계산한다.
2. 사용자가 종료일시를 바꾸면 출장시간을 분 단위로 역산한다.
3. 시작일시를 바꾸면 현재 출장시간을 유지하며 종료일시를 다시 계산한다.
4. 자정을 넘으면 종료 날짜를 다음 날로 자동 이동한다.
5. 1분 초과 24시간 이하만 허용한다.
6. 1시간, 2시간, 4시간, 5시간, 8시간 빠른 선택과 직접 시간·분 입력을 제공한다.

여비의 출장시간은 모든 형태에서 `startsAt`부터 `endsAt`까지다. 이동 경로와 이동비만 실제 편도·왕복 형태에 따라 달라진다.

### 4.6 적용 규정

- 공개 화면의 규정 선택창을 제거한다.
- `SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED`를 서버가 고정 적용한다.
- 화면에는 “서울특별시교육청 공무원 여비 기준” 읽기 전용 배지를 표시한다.
- 카카오 로그인은 신분 확인이 아니라는 문구를 함께 표시한다.
- 공개 계산 요청에서 `policyProfile` 필드를 제거한다. 클라이언트가 해당 필드를 보내면 strict extra-field 검증으로 422를 반환한다.
- 서울 밖 편도처럼 현재 근거로 지급액을 단정하기 어려운 경우 경로는 표시하되 지급액은 `REVIEW_REQUIRED`로 둔다.

### 4.7 보조 패널

헤더의 네 항목은 접근 가능한 우측 패널 또는 모바일 전체화면 패널로 연다.

#### 사용안내

- 기관 선택
- 출장지 선택
- 세 출장 형태의 차이
- 출장시간 자동 입력
- 경로와 예상 여비 해석
- 로그인과 7일 보관 정책

#### 관련규정

- 적용 규정명
- 시행일
- 현재 규칙 버전
- 공식 출처 링크
- 예상값이며 지급 확정이 아니라는 안내

#### 계산이력

- 비로그인 상태는 카카오 로그인 안내만 표시한다.
- 로그인 상태는 최근 168시간의 계산을 최신순으로 표시한다.
- 목록은 출발기관, 출장지, 출장 형태, 계산시각, 대표 결과만 표시한다.
- 상세는 저장 당시 요약을 표시한다.
- “다시 계산”은 저장 결과를 재사용하지 않고 현재 기관 스냅샷·규정·제공자 데이터로 새 계산을 실행한다.
- 개별 삭제와 전체 삭제를 제공한다.

#### 설정

로그인 사용자는 다음 값을 저장할 수 있다.

- 기본 근무지 1곳
- 기본 출장 형태
- 기본 출장시간
- 차량 구분
- 유종
- 연비
- 기본 주차비
- 경로 정렬 기준

출장지와 구체 날짜·시간은 설정에 저장하지 않는다.

기본 근무지는 검증된 `siteId`만 암호화 저장한다. 다음 접속에서 현재 기관 스냅샷의 활성 사이트로 다시 해석한다. 사이트가 없거나 비활성 상태면 자동 선택하지 않고 재선택을 안내한다.

## 5. 시스템 구조

```text
Browser
  ├─ map-first search UI
  ├─ schedule/trip-pattern UI
  ├─ help/rules/history/settings panels
  └─ same-origin session cookie
         │
FastAPI  ├─ public institution/place/trip APIs
         ├─ Kakao OIDC callback and session service
         ├─ history/settings APIs
         └─ existing route and policy engines
                 │
                 ├─ Kakao/Seoul/Opinet providers
                 ├─ approved institution/rule snapshots
                 └─ encrypted SQLite on /volume2
```

기존 `app/static/app.js`는 조정자 역할만 남기고 다음 모듈로 분리한다.

```text
app/static/search.js
app/static/schedule.js
app/static/trip-form.js
app/static/auth.js
app/static/history.js
app/static/settings.js
app/static/help.js
```

백엔드는 다음 경계를 추가한다.

```text
app/auth/oauth.py
app/auth/session.py
app/storage/database.py
app/storage/crypto.py
app/storage/history.py
app/storage/user_settings.py
app/api/auth.py
app/api/me.py
app/api/policy.py
```

## 6. API 계약

### 6.1 기관 facet

```http
GET /api/v1/institutions/facets
```

응답은 현재 승인 스냅샷 ID와 각 정규화 facet의 값·한국어 표시명·활성 사이트 수를 반환한다. 값과 개수는 스냅샷 교체 시 다시 생성한다.

### 6.2 기관 검색

```http
GET /api/v1/institutions?q=&institution_type=&foundation_type=&education_office=&district=&limit=20&offset=0
```

응답:

```json
{
  "items": [],
  "total": 0,
  "nextOffset": null,
  "snapshotId": "20260814T004744Z"
}
```

정렬은 검색 일치 등급, 정규화 공식명, `siteId` 순으로 안정적이어야 한다.

### 6.3 출장지 검색

```http
GET /api/v1/places?q=<name-or-address>
```

키워드·주소 검색을 bounded concurrency로 수행하고 동일한 `PlaceCandidate` 계약으로 정규화한다. 한 제공자 호출이 실패하면 성공한 결과와 안전한 warning을 반환한다. 두 호출이 모두 실패한 경우에만 전체 검색을 unavailable로 처리한다.

### 6.4 계산 요청

```json
{
  "originSiteId": "neis:B10:7081418:main",
  "destination": {
    "name": "서울특별시청",
    "address": "서울 중구 세종대로 110",
    "latitude": 37.5668242,
    "longitude": 126.9786523
  },
  "startsAt": "2026-08-17T09:00:00+09:00",
  "endsAt": "2026-08-17T14:00:00+09:00",
  "tripPattern": "ROUND_TRIP",
  "vehicleUse": "NONE",
  "carAssumptions": {
    "fuelType": "GASOLINE",
    "efficiencyKmPerLiter": 10.0,
    "parkingCostKrw": 0
  },
  "hasOtherLocalTripsToday": false,
  "previousAllowanceKrw": 0
}
```

`returnsAt`은 `endsAt`으로 대체한다. 동일 이미지 안에서 프론트엔드와 API를 함께 배포하므로 공개 UI에 구 계약을 유지하지 않는다.

### 6.5 인증과 사용자 API

```text
GET    /auth/kakao/start
GET    /auth/kakao/callback
GET    /api/v1/me
POST   /api/v1/auth/logout
DELETE /api/v1/me/data

GET    /api/v1/me/history
GET    /api/v1/me/history/{id}
DELETE /api/v1/me/history/{id}
DELETE /api/v1/me/history

GET    /api/v1/me/settings
PUT    /api/v1/me/settings

GET    /api/v1/policy/current
```

비로그인 `POST /api/v1/trips/preview`는 기존처럼 계산만 반환한다. 로그인 세션이 있으면 응답 생성 후 최소 이력 저장을 시도한다. 저장 실패는 계산 응답을 실패시키지 않고 `HISTORY_NOT_SAVED` warning을 추가한다.

## 7. 카카오 로그인과 세션

카카오 공식 REST/OIDC 흐름을 사용한다.

1. `/auth/kakao/start`가 암호학적 난수 `state`, `nonce`, 로그인 시도 ID를 만든다.
2. 로그인 시도는 10분 만료·1회용으로 서버 DB에 해시 저장한다.
3. 카카오 인가 요청에 `state`, `nonce`, `scope=openid`를 포함한다.
4. 콜백은 `state`를 먼저 검증하고 인가 코드를 서버에서 토큰으로 교환한다.
5. ID 토큰의 서명, `iss`, `aud`, `iat`, `exp`, `nonce`를 검증한다.
6. `sub`는 별도 비밀키로 HMAC 처리해 내부 사용자 키를 만든다.
7. 카카오 토큰과 선택 프로필 정보는 검증 직후 폐기한다.
8. 32바이트 세션 토큰을 만들고 DB에는 SHA-256 해시만 저장한다.
9. 쿠키 이름은 `__Host-travel_session`으로 고정하고 `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`을 사용한다.
10. 세션은 절대 7일 만료이며 서버에서 즉시 폐기할 수 있다.

상태 변경 API는 정확한 HTTPS Origin과 세션별 CSRF 토큰을 모두 검증한다. 전역 CORS credential 허용으로 범위를 넓히지 않는다.

카카오 콘솔 설정:

- Kakao Login 활성화
- OpenID Connect 활성화
- REST API 키의 Client secret 활성화
- Redirect URI: `https://travel.h19h19.com/auth/kakao/callback`

공식 참고:

- <https://developers.kakao.com/docs/ko/kakaologin/rest-api>
- <https://developers.kakao.com/docs/ko/kakaologin/prerequisite>
- <https://developers.kakao.com/docs/ko/kakaologin/utilize>

## 8. 데이터 모델과 암호화

SQLite 파일:

```text
/volume2/docker-1/seoul-education-travel-map/data/travel-map.sqlite3
```

컨테이너 마운트:

```text
/volume2/docker-1/seoul-education-travel-map/data:/data
```

테이블:

```text
users
- id INTEGER PRIMARY KEY
- kakao_subject_hmac BLOB UNIQUE NOT NULL
- created_at TEXT NOT NULL
- last_login_at TEXT NOT NULL

oauth_login_attempts
- attempt_hash BLOB PRIMARY KEY
- state_hash BLOB NOT NULL
- nonce_hash BLOB NOT NULL
- created_at TEXT NOT NULL
- expires_at TEXT NOT NULL
- consumed_at TEXT

sessions
- token_hash BLOB PRIMARY KEY
- user_id INTEGER NOT NULL
- csrf_token_hash BLOB NOT NULL
- created_at TEXT NOT NULL
- expires_at TEXT NOT NULL

calculation_history
- id TEXT PRIMARY KEY
- user_id INTEGER NOT NULL
- created_at TEXT NOT NULL
- expires_at TEXT NOT NULL
- encrypted_input BLOB NOT NULL
- encrypted_summary BLOB NOT NULL
- encryption_version INTEGER NOT NULL
- INDEX(user_id, created_at DESC)
- INDEX(expires_at)

user_settings
- user_id INTEGER PRIMARY KEY
- encrypted_payload BLOB NOT NULL
- encryption_version INTEGER NOT NULL
- updated_at TEXT NOT NULL
```

보안 규칙:

- `kakao_subject_hmac`에는 `sub` 원문을 저장하지 않는다.
- `encrypted_input`, `encrypted_summary`, `encrypted_payload`는 AES-256-GCM을 사용한다.
- 각 레코드는 새 nonce와 인증된 부가정보를 사용한다.
- 암호화 키와 사용자 HMAC 키는 `runtime.env`의 별도 비밀값으로 주입한다.
- DB에는 전체 경로 geometry, 제공자 원문, 카카오 토큰, 이메일, 닉네임, 프로필 이미지를 넣지 않는다.
- SQLite는 WAL, foreign keys, busy timeout, `secure_delete=ON`을 사용한다.
- 만료 정리 후 WAL checkpoint truncate를 수행한다.
- 계산 이력 DB 경로를 `/volume2` 일반 백업 작업에서 명시적으로 제외한다.
- DB 디렉터리는 컨테이너 UID `10001`만 읽고 쓸 수 있게 한다.

저장 이력은 다음만 포함한다.

- 출발기관 식별자와 당시 표시명
- 출장지 이름과 주소
- 출장 형태
- 시작·종료 일시
- 지역 분류
- 예상 지급액 상태와 금액
- 대표 경로별 모드·시간·거리·비용
- 규칙 세트 ID와 시행일
- 계산 시각과 만료 시각

## 9. 장애와 안전한 저하

- 기관 facet 실패 시 검색어 기반 기관 검색은 유지하고 필터 영역에 재시도 상태를 표시한다.
- Kakao 키워드 또는 주소 검색 중 하나가 실패하면 성공한 후보를 반환한다.
- 오래된 검색 응답은 요청 ID와 `AbortController`로 폐기한다.
- 인증 DB가 열리지 않으면 로그인·이력·설정 API만 503으로 응답하고 공개 계산은 유지한다.
- 로그인 사용자의 이력 저장이 실패해도 계산 결과는 반환하고 `HISTORY_NOT_SAVED`를 표시한다.
- 저장된 기본 근무지가 현재 활성 사이트가 아니면 자동 선택하지 않는다.
- 사용하지 않는 편도 방향의 제공자는 호출하지 않는다.
- 편도 경로가 지원 범위 밖이거나 규정 근거가 불충분하면 지급액을 생성하지 않고 검토 필요를 반환한다.
- 사용자 검색어, 주소, 좌표, 세션, CSRF, 카카오 식별자, 이력 평문은 로그에 남기지 않는다.

## 10. 접근성·반응형

- 검색 입력은 ARIA combobox와 listbox 패턴을 유지한다.
- 위·아래 화살표, Enter, Escape로 후보를 선택·닫을 수 있다.
- 로딩·0건·오류·선택 상태는 스크린리더에 전달한다.
- 패널은 포커스 이동, Escape 닫기, 닫은 뒤 원래 버튼으로 포커스 복귀를 지원한다.
- 모바일에서 검색 결과가 지도 뒤에 가려지지 않는다.
- 모바일 지도는 펼치기와 접기가 모두 가능해야 한다.
- 색상만으로 출발·도착·경로 상태를 구분하지 않는다.

## 11. 테스트 요구사항

### 11.1 기관 검색

- 빈 검색어로 입력 포커스 시 목록 표시
- 필터만 변경해도 API 호출과 목록 갱신
- 모든 활성 facet 값과 개수 노출
- 운영형 `{officialName, siteName: "main"}`에서 공식명 표시·선택
- 다중 사이트만 사이트명으로 구분
- 0건, 로딩, 오류 상태 구분
- pagination의 중복·누락 방지

### 11.2 출장지 검색

- 키워드와 주소 검색 결과 병합
- 도로명·지번주소 검색
- 후보 중복 제거와 안정 정렬
- 후보 선택 시 핀·지도 이동
- 자유입력 상태 계산 차단
- 지도 클릭 결과 재확인

### 11.3 일정과 경로

- 5시간 입력으로 종료 자동 계산
- 종료 수동 변경으로 출장시간 역산
- 시작 변경 시 기간 유지
- 자정 넘김
- 24시간 초과 거부
- 왕복은 두 방향 호출
- 일정 후 퇴근은 출발 방향만 호출
- 바로 출근 후 복귀는 복귀 방향만 호출
- 편도 이동비가 반대 방향을 포함하지 않음
- 편도·서울 밖의 지급액 검토 필요

### 11.4 규정

- 공개 UI에 규정 선택창이 없음
- 서버가 서울교육 규정을 고정 적용
- 공개 요청의 `policyProfile` 필드는 422
- 관련규정 패널의 규칙 버전·시행일·공식 링크 일치

### 11.5 인증·저장

- OIDC `state`, `nonce`, 서명, issuer, audience, 만료 검증
- 로그인 시도 1회 사용과 10분 만료
- 세션 쿠키 속성과 서버측 폐기
- CSRF·Origin 차단
- 비로그인 계산 성공과 미저장
- 로그인 계산 자동 저장
- 정확한 168시간 만료
- 개별·전체 이력 삭제
- 내 데이터 삭제 시 이력·설정·세션 삭제
- 기본 근무지 저장·복구·변경·해제
- 비활성 근무지 자동 적용 금지
- DB에서 출장지·설정 암호문만 확인
- DB·로그·백업에 토큰·프로필·평문 주소가 없음
- 저장소 장애 시 계산 성공과 이력 warning

### 11.6 E2E

- 데스크톱 지도 중심 검색 흐름
- 375×812 모바일 검색→후보→일정→결과→지도 흐름
- 세 출장 형태
- 로그인·로그아웃
- 7일 이력 목록·상세·다시 계산·삭제
- 설정과 기본 근무지 복구
- 사용안내·관련규정 패널
- 키보드 전용 조작과 포커스 복귀
- 콘솔의 애플리케이션 오류 0건

## 12. 운영과 배포

- 앱 이미지와 컨테이너는 SSD `/volume1`에 유지한다.
- 사용자 DB만 10 TB `/volume2`에 둔다.
- Compose는 `/data` 쓰기 볼륨을 추가하고 나머지 루트 파일시스템은 읽기 전용으로 유지한다.
- DB 디렉터리를 기존 이미지·설정 백업과 분리하고 백업 제외 규칙을 검증한다.
- 운영 비밀값에 Kakao Client secret, 세션 비밀, 사용자 HMAC 키, 데이터 암호화 키를 추가한다.
- 키 값은 Git, 이미지, Compose, Notion, 로그, 스크린샷에 기록하지 않는다.
- 배포 전 DB 마이그레이션을 별도 검증하고 실패 시 이전 이미지와 빈 DB 상태로 롤백할 수 있어야 한다.
- 배포 후 비로그인 계산, 로그인, 근무지 저장, 이력 생성·삭제, 168시간 정리 시뮬레이션을 확인한다.

## 13. 범위 제외

- 서울특별시교육청 재직 검증
- 지급 승인과 전자결재
- 카카오 이메일·프로필 수집
- 자택 주소·자택 경로 저장
- 여러 기본 근무지 또는 즐겨찾기
- 7일을 넘는 계산이력
- 경로 geometry 이력 저장
- PostgreSQL·Redis 추가
- React 전면 재작성
- 서울 밖 편도 출장의 법적 지급액 자동 확정

## 14. 인수 기준

다음 조건을 모두 만족해야 완료로 본다.

1. 기관명 입력 없이 필터만으로 운영 기관 목록이 표시된다.
2. 운영 데이터에서 `main` 대신 공식 기관명이 표시된다.
3. 기관명·장소명·도로명·지번주소 검색 결과를 클릭해 출발지·출장지를 확정할 수 있다.
4. 선택 위치가 지도에 즉시 표시되고 자유입력만으로는 계산되지 않는다.
5. 5시간 입력으로 종료시각이 자동 채워지고 세 출장 형태가 정확한 방향만 계산한다.
6. 서울교육 규정이 자동 적용되며 사용자가 다른 규정을 주입할 수 없다.
7. 비로그인 계산이 유지된다.
8. 로그인 사용자만 `/volume2` 암호화 DB에 이력·설정을 저장한다.
9. 로그인 사용자는 기본 근무지를 저장하고 다음 접속에서 복구할 수 있다.
10. 계산이력은 168시간 뒤 조회와 물리 저장 모두에서 제거된다.
11. 사용안내·관련규정·계산이력·설정이 실제 접근 가능한 패널로 동작한다.
12. 카카오 토큰·프로필·평문 민감정보가 DB·로그·백업·클라이언트에 남지 않는다.
13. 전체 Python 경고 엄격 테스트, Ruff, mypy, Playwright, 보안 회귀가 통과한다.
