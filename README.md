# apt-subscription-advisor

서울·경기 무순위·임의공급 청약 공고를 매일 자동 수집해서, 국토부 실거래가와 비교하고,
내 예산/청약자격/회사위치를 반영해 Claude가 "신청할 만한지"까지 판단해주는
개인용 GitHub Actions 파이프라인입니다.

## 전체 흐름

```
[매일 정해진 시각]
  1. 청약홈 API → 신규 무순위/임의공급 공고 수집 (서울+경기, 전용 46㎡ 이상)
  2. 국토부 실거래가 API → 공고 지역 최근 실거래가 조회
  3. analyzer.py → 분양가 vs 실거래가 격차("안전마진") 계산, DSR 대략치 계산
  4. claude_advisor.py → 위 데이터 + 내 프로필을 Claude에 넘겨 개인화 추천 생성
  5. notifier.py → Slack(또는 텔레그램)으로 결과 전송
```

## 처음 설정하는 법

### 1) API 키 발급 (모두 무료, 즉시 승인)

1. [공공데이터포털](https://www.data.go.kr) 가입
2. **"한국부동산원_청약홈 분양정보 조회 서비스"** 활용신청 → 서비스키 발급
3. **"국토교통부_아파트 매매 실거래가 상세 자료"** 활용신청 → 서비스키 발급
   (참고: 데이터포털은 계정당 서비스키가 보통 공용이라, 위 두 개가 같은 키일 수도 있어요)
4. [Anthropic Console](https://console.anthropic.com)에서 Claude API 키 발급
5. Slack에서 [Incoming Webhook](https://api.slack.com/messaging/webhooks) URL 발급 (또는 텔레그램 봇 토큰)

### 2) ⚠️ 청약홈 엔드포인트 확인 (필수, 5분)

`src/cheongyak_api.py`의 `ENDPOINTS` 딕셔너리에 엔드포인트 3개가 있는데,
**`apt_general`(일반분양)만 확인된 값**이고 나머지 두 개(무순위/잔여세대, 임의공급)는
추정치라 `# TODO: Swagger에서 확인` 주석이 달려있어요.

확인 방법:
1. data.go.kr에서 위 서비스 페이지 → "활용신청" 후 "미리보기/Swagger" 탭 열기
2. 로그인하면 나오는 Swagger UI에서 왼쪽 오퍼레이션 목록 확인
   (예: `getAPTLttotPblancDetail`, `getRemndrLttotPblancDetail` 등 이름 형태)
3. "무순위" "잔여세대" "임의공급" 관련 오퍼레이션을 찾아서 정확한 경로명을
   `cheongyak_api.py`의 `ENDPOINTS`에 채워넣기
4. "Try it out"으로 한 번 호출해보고 응답 JSON의 필드명(지역, 면적, 가격 등)을
   `cheongyak_api.py`의 `parse_notice()` 함수에 맞춰 조정

### 3) 개인 프로필 준비

`config/profile.example.json`을 참고해서 본인 값으로 채운 뒤,
**절대 레포에 커밋하지 말고** GitHub Secrets에 그대로 붙여넣으세요 (아래 참고).

`preferences.prefer_regions`로 관심 지역을 고를 수 있어요:
- `["서울", "경기"]` (기본값, 예시 파일 기준) → 서울+경기 전체
- `["서울"]` → 서울만
- `["경기"]` → 경기만

> `config/profile.example.json` 자체는 실제 값이 아니라 자리표시자(placeholder)만
> 들어있는 **예시 파일**이라서, 이 파일을 그대로 두고 계속 git commit/push 해도
> 안전합니다 — 오히려 레포에 계속 있어야 다른 사람(미래의 나 포함)이 어떤 값을
> 채워야 하는지 알 수 있어요. 위험한 건 이 파일이 아니라 "이 파일에 진짜 예산·연봉·
> 청약점수 값을 넣고 커밋"하는 경우입니다. 로컬에서 진짜 값으로 테스트하고 싶으면
> 이 파일을 복사해서 `config/profile.json`(이미 `.gitignore`에 등록되어 커밋되지
> 않음)을 만들어 거기에 채우세요. 만약 실수로 `profile.example.json`에 진짜 값을
> 채운 채로 커밋한 적이 있다면 `git checkout -- config/profile.example.json`으로
> 되돌리고, 이미 push까지 됐다면 그 커밋 이력에 남으니 값(특히 청약점수·예산 등)을
> 바꾸는 걸 권장합니다.

### 4) GitHub Secrets 등록

레포 Settings → Secrets and variables → Actions → New repository secret:

| Secret 이름 | 값 |
|---|---|
| `CHEONGYAK_SERVICE_KEY` | 청약홈 API 서비스키 |
| `RTMS_SERVICE_KEY` | 국토부 실거래가 API 서비스키 |
| `CLAUDE_API_KEY` | Anthropic API 키 |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook URL |
| `MY_PROFILE_JSON` | profile.example.json 내용을 본인 값으로 채운 한 줄 JSON 문자열 |

### 5) 로컬에서 먼저 테스트

```bash
pip install -r requirements.txt
export CHEONGYAK_SERVICE_KEY=... RTMS_SERVICE_KEY=... CLAUDE_API_KEY=... SLACK_WEBHOOK_URL=...
export MY_PROFILE_JSON="$(cat config/profile.example.json)"
python src/main.py
```

잘 되면 GitHub에 push하고 Secrets만 등록하면 끝입니다. Actions는 매일 자동 실행돼요.

## 폴더 구조

```
.github/workflows/daily-check.yml   # 스케줄 정의
src/main.py                         # 오케스트레이터
src/cheongyak_api.py                # 청약홈 API 클라이언트
src/rtms_api.py                     # 국토부 실거래가 API 클라이언트
src/analyzer.py                     # 안전마진·DSR 계산
src/claude_advisor.py               # Claude 프롬프트 조립·호출
src/notifier.py                     # Slack 알림
config/profile.example.json         # 개인 프로필 예시 (실제 값은 절대 커밋 금지)
```

## 주의사항

- `MY_PROFILE_JSON`에는 예산·소득·청약점수 같은 민감정보가 들어가니 **반드시 Secrets로만** 관리하세요.
- 이 도구의 판단은 참고용입니다. 실제 신청 전 반드시 청약홈 원문 공고를 확인하세요.
- 무료 API 트래픽 한도(개발계정 하루 40,000건)는 이 정도 사용량으로는 절대 초과하지 않아요.
