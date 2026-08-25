"""
네이버 뉴스 기반 '분야별 이슈 키워드 순위' 텔레그램 봇 (단순 빈도 + 연관어 방식)
--------------------------------------------------------------------------
경제 / IT·과학 / 생활·문화 3개 분야에서 각각 헤드라인을 모은 뒤,
분야별로 등장 빈도가 높은 단어 top 10을 뽑아 텔레그램으로 보냅니다.

각 키워드 옆에는 "왜 많이 나왔는지" 짐작할 수 있도록, 그 키워드가 들어간
헤드라인들 안에서 가장 자주 같이 등장한 다른 단어(연관어)를 괄호로 붙여줍니다.
예: "경찰 (23회 언급) — 제주, 실종"
※ 이건 AI가 뜻을 이해해서 요약한 게 아니라 '같이 자주 나온 단어'를 붙인
것뿐이라 완벽한 설명은 아닙니다. 대략적인 힌트로 봐주세요.

Claude API나 별도 형태소 분석기 없이 순수 파이썬만으로 동작합니다.

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
CONTEXT_WORDS = 2  # 키워드마다 연관어를 몇 개까지 붙일지

CATEGORIES = {
    "경제": "https://news.naver.com/main/ranking/popularDay.naver?sid1=101",
    "IT·과학": "https://news.naver.com/main/ranking/popularDay.naver?sid1=105",
    "생활·문화": "https://news.naver.com/main/ranking/popularDay.naver?sid1=103",
}
SELECTORS = [
    ".rankingnews_list .list_title",
    "a.list_title",
]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

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
def fetch_headlines(url: str) -> list[str]:
    """언론사별 많이 본 뉴스 랭킹 페이지에서 헤드라인 목록을 모은다."""
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    for selector in SELECTORS:
        titles = [
            el.get_text(strip=True)
            for el in soup.select(selector)
            if el.get_text(strip=True)
        ]
        if titles:
            return titles

    return []


# ------------------------------------------------------------------
# 2. 단순 빈도 + 연관어 기반 키워드 추출
# ------------------------------------------------------------------
def tokenize(headline: str) -> list[str]:
    """헤드라인을 의미 있는 단어 단위로 쪼갠다 (조사/특수문자 제거 수준)."""
    cleaned = re.sub(r"[^가-힣a-zA-Z0-9\s]", " ", headline)
    tokens = []
    for token in cleaned.split():
        if len(token) < 2:
            continue
        if token in STOPWORDS:
            continue
        if token.isdigit():
            continue
        tokens.append(token)
    return tokens


def extract_issue_keywords(headlines: list[str]) -> list[dict]:
    """
    헤드라인들을 분석해 top N 키워드를 뽑고, 각 키워드마다
    같이 자주 등장한 연관어를 붙여서 반환한다.
    """
    headline_tokens = [tokenize(h) for h in headlines]

    total_counter = Counter()
    for tokens in headline_tokens:
        total_counter.update(tokens)

    top_keywords = total_counter.most_common(TOP_N)

    results = []
    for word, count in top_keywords:
        context_counter = Counter()
        for tokens in headline_tokens:
            if word not in tokens:
                continue
            for t in tokens:
                if t != word:
                    context_counter[t] += 1

        context_words = [w for w, _ in context_counter.most_common(CONTEXT_WORDS)]
        results.append({
            "word": word,
            "count": count,
            "context": context_words,
        })

    return results


# ------------------------------------------------------------------
# 3. 텔레그램 메시지 포맷 & 전송
# ------------------------------------------------------------------
def format_message(results: dict[str, list[dict]]) -> str:
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    lines = [f"📊 분야별 이슈 키워드 순위 ({now} 기준)"]

    for category, keywords in results.items():
        lines.append("")
        lines.append(f"■ {category}")
        if not keywords:
            lines.append("(헤드라인을 가져오지 못했습니다 - 셀렉터 확인 필요)")
            continue
        for rank, item in enumerate(keywords, start=1):
            line = f"{rank}. {item['word']} ({item['count']}회 언급)"
            if item["context"]:
                line += f" — {', '.join(item['context'])}"
            lines.append(line)

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
    for category, url in CATEGORIES.items():
        try:
            headlines = fetch_headlines(url)
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
