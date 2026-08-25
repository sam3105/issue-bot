"""
네이버 뉴스 기반 '분야별 이슈 키워드 순위' 텔레그램 봇 (단순 빈도 + 연관어 방식)
--------------------------------------------------------------------------
경제 / IT·과학 / 생활·문화 3개 분야 뉴스 목록 페이지에서 각각 헤드라인을 모은 뒤,
분야별로 등장 빈도가 높은 단어 top 10을 뽑아 텔레그램으로 보냅니다.

[주의] 이전 버전은 news.naver.com/main/ranking/popularDay.naver 페이지를 썼는데,
이 페이지는 2020년 개편 이후 분야별 필터링을 지원하지 않아 모든 분야에 같은
(뒤섞인) 뉴스가 나오는 문제가 있었습니다. 이번 버전은 실제로 분야별 필터링이
되는 뉴스 목록 페이지(news.naver.com/main/list.naver)로 바꿨습니다.

각 키워드 옆에는 "왜 많이 나왔는지" 짐작할 수 있도록, 그 키워드가 들어간
헤드라인들 안에서 가장 자주 같이 등장한 다른 단어(연관어)를 붙여줍니다.
예: "현대차 (12회 언급) — 노조, 파업"
※ AI가 뜻을 이해해서 요약한 게 아니라 '같이 자주 나온 단어'를 붙인 것뿐이라
완벽한 설명은 아닙니다. 대략적인 힌트로 봐주세요.

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

# 분야별 뉴스 목록 페이지 (실제로 sid1 값에 따라 분야가 필터링됨)
CATEGORIES = {
    "경제": "https://news.naver.com/main/list.naver?mode=LSD&mid=sec&sid1=101",
    "IT·과학": "https://news.naver.com/main/list.naver?mode=LSD&mid=sec&sid1=105",
    "생활·문화": "https://news.naver.com/main/list.naver?mode=LSD&mid=sec&sid1=103",
}

# 기사로 연결되는 링크인지 판별할 때 쓰는 href 패턴
ARTICLE_HREF_PATTERNS = ("/article/", "article_id=", "aid=")

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
    """
    분야별 뉴스 목록 페이지에서, 기사로 연결되는 링크(href 패턴 기준)의
    텍스트를 헤드라인으로 모은다. class 이름이 아니라 링크 패턴 기준이라
    페이지 디자인이 바뀌어도 비교적 안정적으로 동작한다.
    """
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    titles = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not any(pattern in href for pattern in ARTICLE_HREF_PATTERNS):
            continue
        text = a.get_text(strip=True)
        if not (8 <= len(text) <= 60):
            continue
        if text in seen:
            continue
        seen.add(text)
        titles.append(text)

    return titles


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
            lines.append("(헤드라인을 가져오지 못했습니다 - 페이지 구조 확인 필요)")
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
    raw_headlines = {}
    for category, url in CATEGORIES.items():
        try:
            raw_headlines[category] = fetch_headlines(url)
            print(f"[{category}] 헤드라인 {len(raw_headlines[category])}개 수집 완료")
        except Exception as e:
            print(f"[{category}] 수집 실패: {e}", file=sys.stderr)
            raw_headlines[category] = []

    # 2개 이상의 분야에 똑같이 등장하는 헤드라인은 "공통 위젯(사이드바 등)"일
    # 가능성이 높으므로 제거한다. 진짜 그 분야 고유 기사만 남긴다.
    headline_appearance = Counter()
    for headlines in raw_headlines.values():
        for h in set(headlines):
            headline_appearance[h] += 1
    shared_headlines = {h for h, count in headline_appearance.items() if count > 1}

    for category, headlines in raw_headlines.items():
        unique_headlines = [h for h in headlines if h not in shared_headlines]
        print(f"[{category}] 공통 위젯 제거 후 {len(unique_headlines)}개 남음")
        results[category] = extract_issue_keywords(unique_headlines)

    message = format_message(results)
    send_telegram_message(message)
    print("텔레그램 전송 완료")
    print(message)


if __name__ == "__main__":
    main()
