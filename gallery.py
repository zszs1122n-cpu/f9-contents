"""디시인사이드 프로미스나인 마이너 갤러리 '자료' 말머리 글 중 SNS 사진 글(멤버 인스타·스토리, 공식 X, 학교·방송사 인스타 등)을 목록에 넣는다.

사용법
- build.py가 매일 새 글만 받는다(첫 페이지부터, 이미 아는 글만 있는 페이지가 나올 때까지).
- python gallery.py      처음 한 번(또는 가끔) 자료탭 전체를 받는다. 중간에 끊겨도 다시 실행하면 이어서 받는다.
받은 글 목록(번호·작성일·제목)은 .cache/gallery-posts.json에 모아 두고 저장소에 함께 커밋한다.
영상·쇼츠·릴스는 캘린더에 이미 있으니 넣지 않고, 일정·티켓 안내 같은 공지 글도 뺀다.
"""
import html
import json
import re
import sys
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
CACHE_FILE = HERE / ".cache" / "gallery-posts.json"
GALL = "fromis"
HEAD = 20  # '자료' 말머리
LIST_URL = f"https://gall.dcinside.com/mgallery/board/lists/?id={GALL}&search_head={HEAD}&page={{}}"
VIEW_URL = f"https://gall.dcinside.com/mgallery/board/view/?id={GALL}&no={{}}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

# 제목을 ' + ', ' & ' 등으로 나눈 조각 중 하나라도 SNS 글이면 넣는다(그 조각에 영상 말이 없을 때).
PHOTO = re.compile(r"인스타|인스스|인별|스토리|[공일]\s?[Xx](?![a-z])|공트|일본\s?공트|트위터|트윗|위버스|웨이보|공카"
                   r"|페북|페이스북|포스트|게시물|게시글|프사|배사|프로필|사이트|화보"
                   r"|(?:[꿀헌빵냥젼챙꽹송꼬롬션센쎈귤공갠]|지센|지쎈)+(?:스타|토리|버스)", re.I)
# 릴스도 캘린더에 이미 있는 경우가 많아 영상으로 친다('인스타 릴스'는 빠지고 '인스타 + 릴스'의 인스타는 남음)
VIDEO = re.compile(r"릴스|쇼츠|shorts|직캠|유튜브|공튜브|공튭|예고|선공개|하이라이트|풀버전|기사", re.I)
# 공지·일정 글은 통째로 뺀다
NOTICE = re.compile(r"안내|공지|일정|티켓|예매|판매|오픈|배치도|타임\s?테이블|혜택|이벤트|추첨|모금")

# 제목에 쓰이는 멤버 애칭 → 이름 (꿀탄절 4/17 지헌, 빵탄절 9/29 하영, 나꼬, 지쎈 등)
NICK = {"꿀": "지헌", "헌": "지헌", "빵": "하영", "냥": "하영", "젼": "지원", "챙": "채영", "꼬": "나경",
        "꽹": "채영", "송": "하영", "롬": "새롬", "션": "서연", "센": "지선", "쎈": "지선", "귤": "규리"}
NICK_WORDS = {"하냥": "하영", "하빵": "하영", "나꼬": "나경", "져니": "지원", "꿀깅": "지헌", "챙이": "채영",
              "지센": "지선", "지쎈": "지선", "메건": "지원"}
MEMBERS = ["새롬", "하영", "규리", "지원", "지선", "서연", "채영", "나경", "지헌"]


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9",
                                               "Referer": f"https://gall.dcinside.com/mgallery/board/lists/?id={GALL}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def list_page(page):
    """자료탭 한 페이지의 글: {번호: [작성 시각 'YYYY-MM-DD HH:MM', 제목, 종류]}
    종류는 목록 아이콘: img(사진), mov(동영상 첨부), txt(글·링크만)."""
    s = fetch(LIST_URL.format(page))
    out = {}
    for row in re.findall(r'<tr class="ub-content us-post"(.*?)</tr>', s, re.S):
        no = re.search(r'data-no="(\d+)"', row)
        title = re.search(r'class="gall_tit ub-word">\s*<a[^>]*>(.*?)</a>', row, re.S)
        when = re.search(r'class="gall_date" title="([^"]+)"', row)
        icon = re.search(r'<em class="icon_img (\w+)"', row)
        icon = icon.group(1) if icon else ""
        kind = "mov" if "movie" in icon else "txt" if "txt" in icon else "img"
        if no and title and when:
            out[no.group(1)] = [when.group(1)[:16], html.unescape(re.sub(r"<[^>]+>", "", title.group(1))).strip(), kind]
    return out


def load():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"posts": {}}


def save(state):
    CACHE_FILE.parent.mkdir(exist_ok=True)
    # 한 줄에 글 하나 → 매일 바뀌는 부분만 커밋 차이로 남는다
    posts = state["posts"]
    lines = [f"{json.dumps(k)}:{json.dumps(posts[k], ensure_ascii=False)}" for k in sorted(posts, key=int, reverse=True)]
    rest = {k: v for k, v in state.items() if k != "posts"}
    CACHE_FILE.write_text(json.dumps(rest, ensure_ascii=False)[:-1] + (", " if rest else "")
                          + '"posts": {\n' + ",\n".join(lines) + "\n}}\n", encoding="utf-8")


def update(max_pages=40):
    """새 글만 받는다: 이미 아는 글뿐인 페이지가 나오면 멈춘다. 실패해도 지금까지 받은 목록으로 계속 간다."""
    state = load()
    posts = state["posts"]
    added = 0
    try:
        for page in range(1, max_pages + 1):
            got = list_page(page)
            if not got:
                if page == 1:
                    print("갤러리 자료탭이 빈 페이지를 줌(잠시 막힘?) — 저장된 목록만 씀")
                break
            new = {k: v for k, v in got.items() if posts.get(k) != v}
            posts.update(got)
            added += len(new)
            if page > 1 and not new:
                break
            time.sleep(0.5)
    except OSError as err:
        print(f"갤러리 자료탭을 못 받음 ({err}) — 저장된 목록만 씀")
    save(state)
    if added:
        print(f"갤러리 자료 새 글·바뀐 제목 {added}개")
    return posts


def backfill():
    """자료탭 전체를 처음부터 끝 페이지까지 받는다. 진행 상황(next_page)을 저장해 끊겨도 이어서 받는다."""
    state = load()
    page = state.get("next_page", 1)
    while True:
        try:
            got = list_page(page)
        except OSError as err:
            print(f"{page}페이지에서 멈춤 ({err}) — 다시 실행하면 이어서 받음")
            save(state)
            return
        state["posts"].update(got)
        if len(got) < 50:  # 마지막 페이지 (그 뒤 페이지 번호도 마지막 페이지를 준다)
            break
        page += 1
        state["next_page"] = page
        if page % 20 == 0:
            print(f"  {page}페이지 · 글 {len(state['posts'])}개")
            save(state)
        time.sleep(0.7)
    state.pop("next_page", None)
    save(state)
    print(f"갤러리 자료 글 {len(state['posts'])}개")


def members_of(title):
    found = {m for m in MEMBERS if m in title}
    for word, m in NICK_WORDS.items():
        if word in title:
            found.add(m)
    for combo in re.findall(r"((?:[꿀헌빵냥젼챙꽹송꼬롬션센쎈귤공갠])+)(?:스타|토리|버스|댓|탄절|스스)", title):
        found.update(NICK[c] for c in combo if c in NICK)
    return [m for m in MEMBERS if m in found]


def is_photo(title, kind="img"):
    """SNS 사진·스토리 글인가. 글·링크만 있는 글(대개 유튜브 링크)과 공지는 뺀다."""
    if kind == "txt" or NOTICE.search(title):
        return False
    parts = re.split(r"\s*(?:\+|&|,|/|\|)\s*", title)
    return any(PHOTO.search(p) and not VIDEO.search(p) for p in parts)


def content_date(title, posted):
    """제목 앞의 YYMMDD를 날짜로 쓴다. 없거나 작성일과 너무 멀면 작성일."""
    posted_d = date.fromisoformat(posted[:10])
    m = re.match(r"\s*(\d{2})\.?(\d{2})\.?(\d{2})(?!\d)", title)
    if m:
        try:
            d = date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if posted_d - timedelta(days=45) <= d <= posted_d + timedelta(days=1):
                return d.isoformat()
        except ValueError:
            pass
    return posted_d.isoformat()


def events(posts):
    """목록에 넣을 일정들. 링크 플랫폼은 'gallery'."""
    out = []
    for no, (posted, title, kind) in posts.items():
        if not is_photo(title, kind):
            continue
        name = re.sub(r"^\s*(?:\d{6}\s+)+", "", title).strip() or title
        e = {"d": content_date(title, posted), "s": name, "n": "",
             "l": [{"u": VIEW_URL.format(no), "t": "자료 보기", "p": "gallery"}]}
        mem = members_of(title)
        if mem:
            e["m"] = " ".join(mem)
        out.append(e)
    return out


if __name__ == "__main__":
    if "--update" in sys.argv:
        update()
    else:
        backfill()
