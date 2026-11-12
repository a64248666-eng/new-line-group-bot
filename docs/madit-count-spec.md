# Red Bot 3.0 — 마딧수 기능 사양

## 명령어
- `*마딧수`: 현재 LINE 그룹의 오늘(Asia/Seoul) 채팅 마딧수 순위
- 2마디 이상인 멤버만 출력
- 메시지 수 내림차순, 동점이면 이름 오름차순

## 집계
- LINE webhook으로 수신한 **텍스트 메시지 1개 = 1마디**
- `message_events` 테이블에서 `group_id`, `message_date`(YYYY-MM-DD, 한국 시간) 기준 집계
- `COUNT(DISTINCT event_id)`로 중복 웹훅 방지
- `HAVING COUNT(DISTINCT event_id) >= 2` 적용
- 00:00 KST부터 당일 23:59:59까지의 기록만 조회
- 그룹별 집계 분리. 이전 날짜 데이터는 유지하되 새 날짜 집계에는 미포함
- 봇이 들어오기 전 기록이나 수신하지 못한 메시지는 포함 불가

## 구현 SQL 예시
```sql
SELECT user_id, MAX(display_name) AS display_name,
       COUNT(DISTINCT event_id) AS message_count
FROM message_events
WHERE group_id = %s AND message_date = %s
GROUP BY user_id
HAVING COUNT(DISTINCT event_id) >= 2
ORDER BY message_count DESC, display_name ASC;
```

## 유의사항
- 메시지를 저장하기 전에 `webhookEventId` 기준으로 중복 여부를 확인한다.
- 개인정보 처리 안내 및 데이터 보존 정책을 적용한다.
- 운영 배포 및 LINE 실사용 검증은 별도 진행한다.
