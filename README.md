# Red Bot 3.0 ⚙️

LINE 그룹용 챗봇. **기능 구현 코드**이며 실제 LINE/Render 배포는 별도로 진행해야 합니다.

## 명령어

| 명령 | 기능 |
| --- | --- |
| `*마딧수` | 오늘(한국 시간) 2마디 이상 채팅한 사람의 순위 |
| `*261111마딧수` | 2026-11-11의 저장된 채팅 기록 조회 |
| `*남초`, `*여초` | 관리자 지정 명단을 LINE 멘션 |
| `/남초추가 @상대`, `/여초추가 @상대` | 관리자 전용 멘션 명단 추가 |
| `/남초삭제 @상대`, `/여초삭제 @상대` | 관리자 전용 멘션 명단 삭제 |
| `/남초목록`, `/여초목록` | 관리자 전용 명단 조회 |
| `!질문` | Gemini AI 응답 |

메시지 1개 = 1마디이며 **텍스트 메시지만** 계산합니다. 봇이 수신한 시점부터 저장하고 날짜가 바뀌어도 과거 기록을 삭제하지 않습니다. 그룹마다 기록과 명단이 분리됩니다.

## Render 배포

1. 이 저장소를 Render에 연결하고 `render.yaml` Blueprint로 Web Service를 생성합니다.
2. Render PostgreSQL 데이터베이스를 별도로 생성하고 Internal Database URL을 `DATABASE_URL`로 설정합니다. Render 무료 DB는 만료/제약이 있으므로 장기 기록 보관용으로는 지속형 DB를 선택하세요.
3. 환경변수 `LINE_CHANNEL_SECRET`, `LINE_CHANNEL_ACCESS_TOKEN`, `DATABASE_URL`, `ADMIN_USER_IDS`(관리자 LINE userId, 여러 명은 쉼표로 구분), `GEMINI_API_KEY`를 등록합니다. API 키를 GitHub에 올리지 마세요.
4. LINE Developers Messaging API 채널에서 Webhook URL을 `https://<Render 도메인>/callback`로 등록하고 **Use webhook**을 켭니다. LINE 그룹 참여를 허용합니다.
5. 봇을 그룹에 초대하고 실제 LINE 사용자 멘션 및 채팅 집계가 정상인지 확인합니다.

**주의:** Gemini 무료 API 사용량에는 제한이 있습니다. Gemini 무료 등급에서 처리되는 질문은 Google의 데이터 사용 조건을 확인하세요. Render 24시간 상시 실행 및 지속형 PostgreSQL은 비용이 발생할 수 있습니다. Gemini는 유료 모델로 자동 전환하지 않습니다.

**현재 제약:** LINE API 멘션 한도에 맞춰 `*남초`/`*여초` 명령 한 번에 최대 20명만 멘션합니다. 관리자 추가/삭제 명령은 LINE 웹훅이 전달한 실제 멘션 userId가 필요합니다. 서버가 수신하지 못한 과거 메시지는 복원할 수 없습니다.
