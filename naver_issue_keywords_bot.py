"""
네이버 뉴스 기반 '분야별 이슈 키워드 순위' 텔레그램 봇 (단순 빈도 방식)
------------------------------------------------------------------
연예 / 경제 / IT·과학 / 생활·문화 4개 분야에서 각각 헤드라인을 모은 뒤,
분야별로 등장 빈도가 높은 단어 top 10을 뽑아 텔레그램으로 보냅니다.
Claude API나 별도 형태소 분석기 없이 순수 파이썬만으로 동작합니다.

한계: 조사가 붙은 채로 카운트되거나("정부는" vs "정부가"), 진짜 이슈가 아닌
흔한 단어가 섞여 들어올 수 있습니다. STOPWORDS 목록을 계속 다듬어가며 정확도를
높이는 방식입니다. 또한 '연예' 분야는 네이버 뉴스 본체가 아니라 별도 사이트
(entertain.naver.com)라서 페이지 구조가 바뀌면 셀렉터 조정이 필요할 수 있습니다.

필요한 환경변수 (GitHub Actions Secrets에 등록):
- TELEGRAM_BOT_TOKEN : 텔레그램 봇 토큰
- TELEGRAM_CHAT_ID   : 메시지를 보낼 채팅방 ID
"""

import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

# ------------------------------------------------------------------
# 설정
# ------------------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}
KST = timezone(timedelta(hours=9))
TOP_N = 10

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# 분야별 수집 대상 페이지와, 헤드라인을 찾을 때 순서대로 시도해볼 CSS 셀렉터 목록.
# 첫 번째 셀렉터로 못 찾으면 다음 셀렉터로 넘어간다 (사이트 구조 변경 대비).
CATEGORIES = {
    "연예": {
        "url": "https://entertain.naver.com/ranking",
        "selectors": [
            ".rank_lst .tit",
            ".list_ranking_news .tit",
            "a.title",
            "a.tit_link",
        ],
    },
    "경제": {
        "url": "https://news.naver.com/main/ranking/popularDay.naver?sid1=101",
        "selectors": [
            ".rankingnews_list .list_title",
            "a.list_title",
        ],
    },
    "IT·과학": {
        "url": "https://news.naver.com/main/ranking/popularDay.naver?sid1=105",
        "selectors": [
            ".rankingnews_list .list_title",
            "a.list_title",
        ],
    },
    "생활·문화": {
        "url": "https://news.naver.com/main/ranking/popularDay.naver?sid1=103",
        "selectors": [
            ".rankingnews_list .list_title",
            "a.list_title",
        ],
    },
}

# 빈도 계산에서 제외할 흔한 단어 / 조사·어미가 붙기 쉬운 일반 단어들
# 실행해보면서 여기에 계속 단어를 추가해 정확도를 다듬으면 됩니다.
STOPWORDS = {
    "오늘", "관련", "발표", "이후", "위해", "대해", "통해", "가운데",
    "기자", "종합", "속보", "단독", "영상", "사진", "논란", "이유",
    "결국", "역시", "한편", "지난", "올해", "내년", "예정", "확인",
    "밝혔다", "전했다", "말했다", "나섰다", "밝혀", "관측", "분석",
    "이날", "당일", "오전", "오후", "이번", "최근", "현재",
}


# ------------------------------------------------------------------
# 1. 분야별 헤드라인 수집
# ------------------------------------------------------------------
def fetch_headlines(url: str, selectors: list[str]) -> list[str]:
    """주어진 페이지에서 후보 셀렉터를 순서대로 시도해 헤드라인 목록을 모은다."""
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    for selector in selectors:
        titles = [
            el.get_text(strip=True)
            for el in soup.select(selector)
            if el.get_text(strip=True)
        ]
        if titles:
            return titles

    return []


# ------------------------------------------------------------------
# 2. 단순 빈도 기반 키워드 추출
# ------------------------------------------------------------------
def extract_issue_keywords(headlines: list[str]) -> list[tuple[str, int]]:
    """헤드라인을 단어로 쪼개서 등장 빈도가 높은 순으로 top N을 뽑는다."""
    counter = Counter()

    for headline in headlines:
        cleaned = re.sub(r"[^가-힣a-zA-Z0-9\s]", " ", headline)
        for token in cleaned.split():
            if len(token) < 2:
                continue
            if token in STOPWORDS:
                continue
            if token.isdigit():
                continue
            counter[token] += 1

    return counter.most_common(TOP_N)


# ------------------------------------------------------------------
# 3. 텔레그램 메시지 포맷 & 전송
# ------------------------------------------------------------------
def format_message(results: dict[str, list[tuple[str, int]]]) -> str:
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    lines = [f"📊 분야별 이슈 키워드 순위 ({now} 기준)"]

    for category, keywords in results.items():
        lines.append("")
        lines.append(f"■ {category}")
        if not keywords:
            lines.append("(헤드라인을 가져오지 못했습니다 - 셀렉터 확인 필요)")
            continue
        for rank, (word, count) in enumerate(keywords, start=1):
            lines.append(f"{rank}. {word} ({count}회 언급)")

    return "\n".join(lines)


def send_telegram_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=15,
    )
    resp.raise_for_status()


# ------------------------------------------------------------------
# main
# ------------------------------------------------------------------
def main():
    missing = [
        name
        for name, val in [
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
        ]
        if not val
    ]
    if missing:
        print(f"환경변수가 설정되지 않았습니다: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    results = {}
    for category, config in CATEGORIES.items():
        try:
            headlines = fetch_headlines(config["url"], config["selectors"])
            print(f"[{category}] 헤드라인 {len(headlines)}개 수집 완료")
            results[category] = extract_issue_keywords(headlines)
        except Exception as e:
            print(f"[{category}] 수집 실패: {e}", file=sys.stderr)
            results[category] = []

    message = format_message(results)
    send_telegram_message(message)
    print("텔레그램 전송 완료")
    print(message)


if __name__ == "__main__":
    main()
