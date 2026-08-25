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

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

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

# 분야별 뉴스 목록 페이지 (실제로 sid1 값에 따라 분야가 필터링됨)
CATEGORIES = {
    "경제": "https://news.naver.com/main/list.naver?mode=LSD&mid=sec&sid1=101",
    "IT·과학": "https://news.naver.com/main/list.naver?mode=LSD&mid=sec&sid1=105",
    "생활·문화": "https://news.naver.com/main/list.naver?mode=LSD&mid=sec&sid1=103",
}

# 조사/어미로 흔히 붙는 꼬리들 - 길이가 긴 것부터 시도해서 먼저 잘라낸다
# (완벽한 형태소 분석은 아니지만 "정부는"/"정부가"를 "정부"로 합쳐주는 수준의 효과)
TRAILING_SUFFIXES = sorted([
    "이라며", "라면서", "이라고", "라고는", "했다는", "된다는", "한다는",
    "에서는", "에서도", "부터는", "까지도", "이라는", "라는",
    "에서", "부터", "까지", "이며", "으로", "에는", "에도", "했다", "된다",
    "한다", "는다", "이다", "이고", "하고", "라며", "이나",
    "와", "과", "의", "은", "는", "이", "가", "을", "를", "에", "로", "도", "만",
], key=len, reverse=True)

DUPLICATE_SIMILARITY_THRESHOLD = 0.5  # 이 비율 이상 단어가 겹치면 "같은 기사"로 간주
MIN_COUNT = 2  # 이 횟수 미만은 노이즈일 확률이 높아 top 10 후보에서 제외
CLUSTER_OVERLAP_THRESHOLD = 0.7  # 이 비율 이상 같은 헤드라인 묶음에서 나오면 한 이슈로 합침

# 텔레그램을 보낼 시각(KST, 24시간제) - 새벽엔 안 보냄
SEND_HOURS = {6, 8, 10, 12, 14, 16, 18, 20, 22}
# 이 시각에는 직전 기록을 무시하고 무조건 전체를 다시 보여줌 (4시간마다 리셋)
RESET_HOURS = {6, 10, 14, 18, 22}
# 직전 실행과 비교할 때, 이 비율 이상 단어가 겹치면 "같은 뉴스"로 보고 숨김
CROSS_RUN_OVERLAP_THRESHOLD = 0.5

STATE_FILE = "state.json"
LATEST_JSON_FILE = "latest.json"  # 웹페이지가 읽어갈 최신 결과 파일

# 웹 대시보드에서 분야별 색상을 구분하기 위한 클래스 이름
CATEGORY_CSS_CLASS = {
    "경제": "eco",
    "IT·과학": "tech",
    "생활·문화": "life",
}

# 기사로 연결되는 링크인지 판별할 때 쓰는 href 패턴
ARTICLE_HREF_PATTERNS = ("/article/", "article_id=", "aid=")

# 분야별로 몇 페이지까지 더 가져와서 표본을 늘릴지
PAGES_PER_CATEGORY = 3

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
def fetch_headlines(url: str) -> list[tuple[str, str]]:
    """
    분야별 뉴스 목록 페이지에서 여러 페이지(PAGES_PER_CATEGORY장)를 가져와,
    기사로 연결되는 링크(href 패턴 기준)의 (텍스트, 기사 주소)를 모은다.
    class 이름이 아니라 링크 패턴 기준이라 페이지 디자인이 바뀌어도
    비교적 안정적으로 동작하고, 여러 페이지를 모아서 표본을 늘린다.
    """
    titles: list[tuple[str, str]] = []
    seen = set()

    for page in range(1, PAGES_PER_CATEGORY + 1):
        page_url = f"{url}&page={page}"
        resp = requests.get(page_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

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
            absolute_url = urljoin(page_url, href)  # 상대주소(/mnews/... 등)를 절대주소로 변환
            titles.append((text, absolute_url))

    return titles


# ------------------------------------------------------------------
# 2. 단순 빈도 + 연관어 기반 키워드 추출
# ------------------------------------------------------------------
def strip_trailing_suffix(token: str) -> str:
    """흔한 조사/어미 꼬리를 잘라낸다. 자른 결과가 2글자 미만이면 원본 유지."""
    for suffix in TRAILING_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            return token[: -len(suffix)]
    return token


def tokenize(headline: str) -> list[str]:
    """헤드라인을 의미 있는 단어 단위로 쪼갠다 (조사/어미/특수문자 제거 수준)."""
    cleaned = re.sub(r"[^가-힣a-zA-Z0-9\s]", " ", headline)
    tokens = []
    for raw_token in cleaned.split():
        token = strip_trailing_suffix(raw_token)
        if len(token) < 2:
            continue
        if token in STOPWORDS:
            continue
        if token.isdigit():
            continue
        tokens.append(token)
    return tokens


def dedupe_similar_headlines(headlines: list[str], tokens_list: list[list[str]]) -> list[tuple[str, list[str]]]:
    """
    단어 구성이 많이 겹치는 헤드라인들은 '사실상 같은 기사(보도자료 반복 게재 등)'로
    보고 하나만 남긴다. Jaccard 유사도(겹치는 단어 비율) 기준.
    (원본 헤드라인 텍스트, 토큰 목록) 쌍으로 반환한다 - 나중에 실제 헤드라인을
    그대로 보여줄 때 쓰기 위함.
    """
    kept: list[tuple[str, list[str]]] = []
    kept_sets = []
    for headline, tokens in zip(headlines, tokens_list):
        token_set = set(tokens)
        if not token_set:
            continue
        is_duplicate = False
        for existing_set in kept_sets:
            intersection = len(token_set & existing_set)
            union = len(token_set | existing_set)
            similarity = intersection / union if union else 0
            if similarity >= DUPLICATE_SIMILARITY_THRESHOLD:
                is_duplicate = True
                break
        if not is_duplicate:
            kept.append((headline, tokens))
            kept_sets.append(token_set)
    return kept


def extract_issue_keywords(headlines: list[str]) -> list[dict]:
    """
    헤드라인들을 분석해 이슈 top N을 뽑는다. 같은 기사/같은 이슈에서 나온
    단어들(예: KT, 채용, 대졸, 신입)은 따로 세지 않고 한 이슈로 묶고,
    그 이슈를 대표하는 실제 헤드라인 원문을 설명으로 붙여서 보여준다.
    """
    all_tokens = [tokenize(h) for h in headlines]
    deduped = dedupe_similar_headlines(headlines, all_tokens)  # [(headline, tokens), ...]

    # 단어 -> 그 단어가 등장한 헤드라인 인덱스 집합
    word_headline_idx: dict[str, set[int]] = {}
    for idx, (_, tokens) in enumerate(deduped):
        for t in set(tokens):
            word_headline_idx.setdefault(t, set()).add(idx)

    word_counts = Counter({w: len(idxs) for w, idxs in word_headline_idx.items()})
    candidates = [w for w, c in word_counts.most_common(TOP_N * 5) if c >= MIN_COUNT]

    clusters: list[dict] = []  # [{"words": [...], "idxs": set(...)}]
    for word in candidates:
        idxs = word_headline_idx[word]
        target_cluster = None
        for cluster in clusters:
            smaller = min(len(idxs), len(cluster["idxs"]))
            if smaller == 0:
                continue
            overlap = len(idxs & cluster["idxs"]) / smaller
            if overlap >= CLUSTER_OVERLAP_THRESHOLD:
                target_cluster = cluster
                break
        if target_cluster is not None:
            target_cluster["words"].append(word)
            target_cluster["idxs"] |= idxs
        else:
            clusters.append({"words": [word], "idxs": set(idxs)})

    clusters.sort(key=lambda c: len(c["idxs"]), reverse=True)

    results = []
    for cluster in clusters[:TOP_N]:
        keyword = cluster["words"][0]  # 가장 많이 등장한 대표 단어
        # 이 이슈에 속한 헤드라인들 중 가장 짧은 걸 대표 설명으로 사용
        # (짧을수록 핵심만 담겨있어 읽기 편한 경향이 있음)
        cluster_headlines = [deduped[i][0] for i in cluster["idxs"]]
        representative_headline = min(cluster_headlines, key=len)
        results.append({
            "word": keyword,
            "count": len(cluster["idxs"]),
            "headline": representative_headline,
        })

    return results


# ------------------------------------------------------------------
# 3. 텔레그램 메시지 포맷 & 전송
# ------------------------------------------------------------------
def format_message(results: dict[str, list[dict]], is_reset: bool) -> str:
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    mode_label = "전체" if is_reset else "새 소식만"
    lines = [f"📊 분야별 이슈 키워드 순위 ({now} 기준 · {mode_label})"]

    for category, keywords in results.items():
        lines.append("")
        lines.append(f"■ {category}")
        if not keywords:
            if is_reset:
                lines.append("(헤드라인을 가져오지 못했습니다 - 페이지 구조 확인 필요)")
            else:
                lines.append("(지난 2시간과 겹치는 뉴스뿐이라 새 소식 없음)")
            continue
        for rank, item in enumerate(keywords, start=1):
            lines.append(f"{rank}. {item['word']}")
            lines.append(f"   → {item['headline']}")
            if item.get("url"):
                lines.append(f"   {item['url']}")

    return "\n".join(lines)


def save_latest_json(results: dict[str, list[dict]], is_reset: bool) -> None:
    """웹 대시보드(index.html)가 fetch()로 읽어갈 최신 결과 파일을 저장한다."""
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    payload = {
        "updatedAt": now,
        "mode": "전체 갱신" if is_reset else "새 소식만",
        "categories": [
            {
                "name": category,
                "cls": CATEGORY_CSS_CLASS.get(category, "eco"),
                "items": [
                    {"keyword": item["word"], "headline": item["headline"], "url": item.get("url", "")}
                    for item in keywords
                ],
            }
            for category, keywords in results.items()
        ],
    }
    with open(LATEST_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def send_telegram_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=15,
    )
    resp.raise_for_status()


# ------------------------------------------------------------------
# 4. 직전 실행 기록(state.json) 관리
# ------------------------------------------------------------------
def load_state() -> dict:
    """직전 실행 때 저장해둔 기록을 읽어온다. 없으면 빈 기록으로 시작."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_similar_to_any(headline: str, previous_headlines: list[str]) -> bool:
    """헤드라인이 이전 기록의 헤드라인들과 많이 겹치면 True (=이미 보여준 뉴스)."""
    tokens = set(tokenize(headline))
    if not tokens:
        return False
    for prev in previous_headlines:
        prev_tokens = set(tokenize(prev))
        if not prev_tokens:
            continue
        union = len(tokens | prev_tokens)
        if union == 0:
            continue
        similarity = len(tokens & prev_tokens) / union
        if similarity >= CROSS_RUN_OVERLAP_THRESHOLD:
            return True
    return False


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

    now = datetime.now(KST)
    hour = now.hour

    # 안전장치: 예정된 발송 시각이 아니면(예: 수동 실행 등) 조용히 종료
    if hour not in SEND_HOURS:
        print(f"현재 KST {hour}시는 발송 시각이 아니라서 건너뜁니다. (발송 시각: {sorted(SEND_HOURS)})")
        return

    is_reset = hour in RESET_HOURS
    print(f"KST {hour}시 실행 - {'전체 리셋' if is_reset else '새 소식만 필터링'} 모드")

    raw_headlines = {}
    headline_url_map: dict[str, str] = {}  # 헤드라인 텍스트 -> 실제 기사 주소
    for category, url in CATEGORIES.items():
        try:
            fetched = fetch_headlines(url)  # [(headline, article_url), ...]
            raw_headlines[category] = [h for h, _ in fetched]
            for h, article_url in fetched:
                headline_url_map.setdefault(h, article_url)
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

    state = {} if is_reset else load_state()
    results = {}

    for category, headlines in raw_headlines.items():
        unique_headlines = [h for h in headlines if h not in shared_headlines]
        print(f"[{category}] 공통 위젯 제거 후 {len(unique_headlines)}개 남음")
        keywords = extract_issue_keywords(unique_headlines)

        if not is_reset:
            previous = state.get(category, [])
            before = len(keywords)
            keywords = [
                item for item in keywords
                if not is_similar_to_any(item["headline"], previous)
            ]
            print(f"[{category}] 직전 기록과 비교: {before}개 중 {len(keywords)}개가 새 소식")

        results[category] = keywords

        # 대표 헤드라인에 해당하는 실제 기사 주소를 붙여준다
        for item in keywords:
            item["url"] = headline_url_map.get(item["headline"], "")

        # 이번에 보여준 헤드라인을 기록에 추가(리셋 모드면 새로 시작)
        shown_headlines = [item["headline"] for item in keywords]
        state[category] = (state.get(category, []) if not is_reset else []) + shown_headlines

    save_state(state)
    save_latest_json(results, is_reset)

    message = format_message(results, is_reset)
    send_telegram_message(message)
    print("텔레그램 전송 완료")
    print(message)


if __name__ == "__main__":
    main()
