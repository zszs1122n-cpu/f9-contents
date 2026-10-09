"""캘린더에 없는 컨텐츠(2022년 이전 전부 + 모든 해의 TV·라디오 출연)를 모아 archive/archive.json을 만든다.

사용법:  python archive/collect.py        (가끔 손으로 실행, 결과 archive.json은 저장소에 커밋)
출처
- 나무위키 '프로미스나인/…' 하위 문서 (TV 방송, 라디오, 네이버 나우, 음악 방송, V LIVE, 라이브 방송, 콘텐츠, 유튜브, 활동)
- 프롬피디아 fromispedia.com (영문 팬 아카이브, 유튜브·위버스 링크 보충용)
- 공식 유튜브 채널 @Officialfromis9 (위 두 곳과 캘린더에 없는 영상 보충)
받은 원본은 archive/raw/ 에 캐시한다(저장소에는 올리지 않음). 새로 받으려면 해당 파일을 지우고 다시 실행.

TV·라디오 출연은 다시보기 링크가 없어도 '방송' 항목(p="tv")으로 넣는다. 2022년 이후 출연은
캘린더에 같은 프로그램이 이미 있으면 build.py가 빼 준다(b=1, k=프로그램 키, e=마지막 방송일).
"""
import concurrent.futures as cf
import html
import html.parser
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
RAW = HERE / "raw"
sys.path.insert(0, str(HERE.parent))
import build  # noqa: E402  (캘린더 파싱·플랫폼 판별 재사용)

CUTOFF = "2022-01-01"  # 이 날부터는 캘린더(f9_video_calendar)에 있다
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
NAMU_PAGES = ["TV 방송", "라디오", "네이버 나우", "음악 방송", "V LIVE", "라이브 방송", "콘텐츠", "유튜브", "활동"]
BROADCAST_PAGES = {"TV 방송", "라디오", "네이버 나우"}

MEMBERS = {"이새롬": "새롬", "송하영": "하영", "장규리": "규리", "박지원": "지원", "노지선": "지선",
           "이서연": "서연", "이채영": "채영", "이나경": "나경", "백지헌": "지헌"}
MEMBERS_EN = {"saerom": "새롬", "hayoung": "하영", "gyuri": "규리", "jiwon": "지원", "jisun": "지선",
              "seoyeon": "서연", "chaeyoung": "채영", "nagyung": "나경", "jiheon": "지헌"}
# 링크로 넣지 않는 곳: 팬 업로드(구글 드라이브 등)·불법 스트리밍·서비스가 끝난 V LIVE·사진 포스트
BLOCKED = ("drive.google.com", "kshow123", "dramacool", "terabox", "dailymotion.com", "captionfy", "vlive.tv",
           "vlivearchive", "naver.me", "namu.wiki", "post.naver.com", "fromisubs")


def fetch(url, timeout=30, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def cached(path, url):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        print("받는 중", url)
        path.write_text(fetch(url), encoding="utf-8")
        time.sleep(1)
    return path.read_text(encoding="utf-8")


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def norm_url(u):
    u = html.unescape(u.strip())
    u = re.sub(r"^http://", "https://", u)
    u = re.sub(r"^https://m\.tv\.naver\.com/", "https://tv.naver.com/", u)
    if "weverse.io" in u:
        u = u.split("?")[0].rstrip("/")
    return u


url_key = build.url_key


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def allowed(u):
    return u.startswith("http") and not any(b in u for b in BLOCKED)


def members_in(text):
    """'-이새롬, 노지선-' 같은 출연 표기에서 멤버 짧은 이름을 뽑는다. '전원'이면 빈 목록."""
    if re.search(r"전원|all members|ot9", text, re.I) and "외 전원" not in text:
        return []
    if "외 전원" in text:
        return []
    found = [short for full, short in MEMBERS.items() if full in text]
    found += [short for en, short in MEMBERS_EN.items() if re.search(rf"\b{en}\b", text, re.I) and short not in found]
    order = list(MEMBERS.values())
    return sorted(set(found), key=order.index)


def with_members(title, mems):
    extra = [m for m in mems if m not in title]
    return (title + " " + " ".join(extra)).strip()


def program_key(name):
    return re.sub(r"[\W_]+", "", name.split(" - ")[0]).lower()


# ---------------- 나무위키 ----------------

class TableParser(html.parser.HTMLParser):
    """표의 행마다 칸 글자·링크(몇 번째 칸인지)와 바로 앞 제목을 모은다. 이미지는 alt를 {…}로 남긴다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.head, self.htext, self.inh = [], "", "", False
        self.tables, self.tstack, self.row, self.cell, self.a, self.skip = 0, [], None, None, None, 0

    def handle_starttag(self, t, a):
        a = dict(a)
        if t in ("sup", "script", "style", "noscript"):
            self.skip += 1
        if re.fullmatch(r"h[1-6]", t):
            self.inh, self.htext = True, ""
        if t == "table":
            self.tstack.append(self.tables)
            self.tables += 1
        if t == "tr" and self.tstack:
            self.row = {"tab": self.tstack[-1], "h": self.head, "cells": [], "links": []}
        if t in ("td", "th") and self.row is not None:
            self.cell = ""
        if t == "br" and self.cell is not None:
            self.cell += " / "
        if t == "img" and self.cell is not None and not self.skip and a.get("alt"):
            self.cell += " {" + a["alt"] + "} "
        if t == "a" and self.row is not None:
            self.a = [a.get("href", ""), ""]

    def handle_endtag(self, t):
        if t in ("sup", "script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)
        if re.fullmatch(r"h[1-6]", t) and self.inh:
            self.inh = False
            self.head = re.sub(r"\s+", " ", self.htext).replace("[편집]", "").strip()
        if t in ("td", "th") and self.cell is not None and self.row is not None:
            self.row["cells"].append(re.sub(r"\s+", " ", self.cell).strip())
            self.cell = None
        if t == "a" and self.a is not None:
            if self.row is not None and self.a[0].startswith("http"):
                self.row["links"].append((self.a[0], re.sub(r"\s+", " ", self.a[1]).strip(), len(self.row["cells"])))
            self.a = None
        if t == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        if t == "table" and self.tstack:
            self.tstack.pop()

    def handle_data(self, d):
        if self.skip:
            return
        if self.inh:
            self.htext += d
        if self.cell is not None:
            self.cell += d
        if self.a is not None:
            self.a[1] += d


def plain(cell):
    """{로고 alt} 표시와 빈 구분자를 걷어낸 글자."""
    t = re.sub(r"\{[^}]*\}", " ", cell)
    parts = [p.strip() for p in t.split(" / ")]
    return " / ".join(p for p in parts if p and p != "/").strip(" /")


def alt_names(seg):
    """로고 이미지 alt에서 이름만 (흰색판·아이콘·파일명 같은 것은 뺀다)."""
    out = []
    for a in re.findall(r"\{([^}]*)\}", seg):
        if re.search(r"화이트|White|컬러|아이콘", a):
            continue
        a = re.sub(r"\s*로고.*$", "", a).strip()
        if a and not (re.fullmatch(r"[A-Za-z0-9 ]+", a) and re.search(r"\d", a)):
            out.append(a)
    return out


def last_segment(cell):
    """'방송사 로고 / 프로그램 로고 / 프로그램' 같은 칸에서 마지막 이름(글자가 없으면 로고 alt)."""
    for seg in reversed(cell.split(" / ")):
        t = plain(seg)
        if t:
            return t
        names = alt_names(seg)
        if names:
            return names[0]
    return ""


def parse_date(cell, year):
    """'02. 10.' / '02. 10. ~ / 02. 17.' / '09. 04. ~ / 24. 06. 17.'(다른 해) → (시작일, 끝일)."""
    def one(text, y):
        m = re.match(r"(?:(\d{2})\. ?)?(\d{1,2})\. ?(\d{1,2})\.", text) if re.match(r"\d{2}\. ?\d{1,2}\. ?\d{1,2}\.", text)             else re.match(r"()(\d{1,2})\. ?(\d{1,2})\.?", text)
        if not m:
            return None
        y = 2000 + int(m.group(1)) if m.group(1) else y
        try:
            return date(y, int(m.group(2)), int(m.group(3)))
        except (TypeError, ValueError):
            return None

    if not year:
        return None, None
    first = one(cell.strip(), year)
    if not first:
        return None, None
    end = None
    if "~ / " in cell:
        end = one(cell.split("~ / ", 1)[1].strip(), first.year)
        if end and end < first:
            end = end.replace(year=end.year + 1)
    return first.isoformat(), end.isoformat() if end else None


def heading_year(h):
    m = re.search(r"(20\d\d)년", h) or re.search(r"\((20\d\d)\. \d\d\. \d\d\.\)", h)
    return int(m.group(1)) if m else None


def namu_events(page):
    raw = cached(RAW / "namu" / (page.replace(" ", "_") + ".html"),
                 "https://namu.wiki/w/" + urllib.parse.quote("프로미스나인/" + page))
    p = TableParser()
    p.feed(raw)
    out = []
    tab = head = header = year = last = None
    prev = []
    for row in p.rows:
        if row["tab"] != tab or row["h"] != head:
            if row["tab"] != tab:
                header = None
            tab, head = row["tab"], row["h"]
            year = heading_year(head) or (year if row["tab"] == tab and header else None)
        cells = row["cells"]
        if not cells:
            continue
        if cells[0] == "날짜":
            header, prev = cells, []
            continue
        if len(cells) == 1:
            m = re.fullmatch(r"(20\d\d)년", plain(cells[0]))
            if m:
                year = int(m.group(1))
            continue
        links = [(norm_url(u), t, i) for u, t, i in row["links"]]
        if header is None:
            if not row["h"][:1].isdigit():
                continue
            # 콘텐츠 문서의 바둑판 표: 칸마다 '제목 / 2018. 02. 27. / 시리즈'
            for i, c in enumerate(cells):
                m = re.match(r"(.+?) / (20\d\d)\. ?(\d{1,2})\. ?(\d{1,2})\.(?: / (.+))?$", plain(c))
                if m:
                    title, series = m.group(1), (m.group(5) or "")
                    d = f"{m.group(2)}-{int(m.group(3)):02d}-{int(m.group(4)):02d}"
                    s = title if series.strip("[] ") in title or not series else f"{title} ({series})"
                    out.append({"d": d, "s": s, "links": [(u, "") for u, t, j in links if j == i], "src": page})
            continue
        d, e = parse_date(cells[0], year)
        missing = len(header) - len(cells)
        if missing == 1 and d and header[-1] in ("비고", "설명"):
            cells = cells + [""]  # 맨 끝 비고 칸이 비어 생략된 행
            missing = 0
        if missing > 0 and prev:
            # 위 행과 합쳐진 칸(rowspan)은 위 행 값을 빌려 온다: 날짜가 있으면 그 다음 칸들, 없으면 앞 칸들
            at = 1 if d else 0
            cells = cells[:at] + prev[at:at + missing] + cells[at:]
            links = [(u, t, i + missing if i >= at else i) for u, t, i in links]
        if d:
            last = (d, e)
        elif last and missing > 0:
            d, e = last
        else:
            continue
        prev = cells
        col = {name: i for i, name in enumerate(header)}

        def get(*names):
            for n in names:
                if n in col and col[n] < len(cells):
                    return cells[col[n]]
            return ""

        title_cell = get("프로그램", "방송 프로그램", "방송명", "제목", "콘텐츠")
        ev = {"d": d, "src": page}
        if e:
            ev["e"] = e
        mem_text = title_cell + " " + get("출연", "멤버")
        if page in ("TV 방송", "라디오", "네이버 나우"):
            segs = [s for s in plain(title_cell).split(" / ") if s]
            prog = segs[0] if segs and not segs[0].startswith("-") else ""
            prog = prog or (alt_names(title_cell) or [""])[0]
            if not prog:
                continue
            if page == "네이버 나우":
                prog = "NOW. " + prog
            chan = last_segment(get("방송사", "플랫폼")) if "방송사" in col else ""
            note = plain(get("비고"))
            for _, t, _ in links:
                note = note.replace(t, "")
            note = re.sub(r"\s*/\s*", " ", note).strip(" /")
            ev.update(s=with_members(prog, members_in(mem_text)), prog=prog, b=1,
                      n=" · ".join(x for x in [chan, note] if x))
            ev["links"] = [(u, f"{prog} · {t}" if t else prog) for u, t, i in links]
        elif page == "음악 방송":
            prog = last_segment(get("방송 프로그램"))
            stage_cell = get("무대 영상")
            songs = [s for s in plain(stage_cell).split(" / ") if s]
            stage_links = [u for u, t, i in links if i == col.get("무대 영상")]
            ev["s"] = f"{prog} {' · '.join(songs)}".strip()
            ev["n"] = plain(get("비고"))
            ev["links"] = [(u, f"{prog} - {songs[k]}" if k < len(songs) else prog) for k, u in enumerate(stage_links)]
        else:
            title = plain(title_cell) or last_segment(title_cell)
            title = re.sub(r"\s*/\s*-[^-]*-\s*$", "", title)  # 끝의 '/ -출연-' 표기
            chan = last_segment(get("채널", "채널명", "플랫폼"))
            if page == "활동" and ("배역" in col or "역할" in col):
                # 드라마·리얼리티 출연도 방송으로. 제목은 로고 alt에만 있다
                title = last_segment(title_cell) or title
                role = plain(get("배역", "역할"))
                ev["n"] = " · ".join(x for x in [plain(title_cell).split(" / ")[0], role, plain(get("비고"))] if x)
                ev["b"], ev["prog"] = 1, title
            else:
                ev["n"] = " · ".join(x for x in [chan, plain(get("설명", "비고"))] if x and x != "-")
            ev["s"] = with_members(title, members_in(mem_text))
            ev["links"] = [(u, t if t and not re.fullmatch(r"[\d/①-⑳ ]*", t) else "") for u, t, i in links]
        out.append(ev)
    return out


# ---------------- 프롬피디아 ----------------

def fromispedia_events():
    pages = sorted((RAW / "fromispedia").glob("*.html"))
    out = []
    for f in pages:
        s = f.read_text(encoding="utf-8")
        s = re.sub(r"<script.*?</script>|<style.*?</style>", "", s, flags=re.S)
        s = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>', lambda m: " [L " + html.unescape(m.group(1)) + "] ", s)
        s = re.sub(r"</(p|div|li|h\d|tr)>|<br[^>]*>", "\n", s)
        s = html.unescape(re.sub(r"<[^>]+>", "", s))
        for line in s.split("\n"):
            m = re.search(r"(\d{2})(\d{2})(\d{2}) ?\.\s*(.*)", line)
            if not m or "[L http" not in line:
                continue
            d = f"20{m.group(1)}-{m.group(2)}-{m.group(3)}"
            if d >= CUTOFF:
                continue
            rest = m.group(4)
            links = []
            for u, label in re.findall(r"\[L (http[^\]]+)\]\s*([^\[|]*)", rest):
                links.append((norm_url(u), re.sub(r"\s*-\s*$", "", label.strip())))
            if any(l[1].startswith("Source") for l in links):  # '자막본 | Source' → 원본만
                links = [l for l in links if l[1].startswith("Source")]
            show = re.sub(r"\s*-\s*$", "", rest.split("[L ")[0]).strip()
            text = re.sub(r"\[L [^\]]+\]", " ", rest)
            out.append({"d": d, "s": show, "en": text, "mem": members_in(text), "links": links,
                        "src": "fromispedia/" + f.stem})
    return out


# ---------------- 유튜브·위버스 제목 ----------------

def youtube_titles(ids):
    """oEmbed로 원래(한국어) 제목과 채널을 얻는다. 지워진 영상은 None."""
    cache_file = RAW / "youtube-oembed.json"
    known = load_json(cache_file, {})
    todo = [i for i in ids if i not in known]

    def one(i):
        url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(f"https://www.youtube.com/watch?v={i}")
        for attempt in range(3):
            try:
                j = json.loads(fetch(url, 20))
                return {"t": j.get("title", ""), "a": j.get("author_name", "")}
            except urllib.error.HTTPError as e:
                if e.code in (400, 404):
                    return None  # 삭제됨
                if e.code in (401, 403):
                    return {"t": "", "a": ""}  # 비공개·퍼가기 금지: 있다고 본다
                time.sleep(3 * (attempt + 1))
            except OSError:
                time.sleep(3)
        return "retry"

    print(f"유튜브 제목 {len(todo)}개 확인 중…")
    with cf.ThreadPoolExecutor(6) as ex:
        for n, (i, r) in enumerate(zip(todo, ex.map(one, todo))):
            if r != "retry":
                known[i] = r
            if n % 200 == 199:
                cache_file.write_text(json.dumps(known, ensure_ascii=False), encoding="utf-8")
    cache_file.write_text(json.dumps(known, ensure_ascii=False), encoding="utf-8")
    return known


def weverse_titles(urls):
    cache_file = RAW / "weverse-og.json"
    known = load_json(cache_file, {})
    todo = [u for u in urls if u not in known]

    def one(u):
        try:
            h = fetch(u, 20, {"User-Agent": "facebookexternalhit/1.1"})
        except OSError:
            return "retry"
        m = re.search(r'og:description" content="([^"]*)"', h)
        return html.unescape(m.group(1)).strip() if m else ""

    print(f"위버스 제목 {len(todo)}개 확인 중…")
    with cf.ThreadPoolExecutor(6) as ex:
        for u, r in zip(todo, ex.map(one, todo)):
            if r != "retry":
                known[u] = r
    cache_file.write_text(json.dumps(known, ensure_ascii=False), encoding="utf-8")
    return known


def channel_videos():
    """공식 채널 전체 목록 (yt-dlp가 있으면 새로 받는다)."""
    out = []
    for tab in ("videos", "shorts", "streams"):
        f = RAW / f"{tab}.tsv"
        if not f.exists():
            try:
                r = subprocess.run(["yt-dlp", "--encoding", "utf-8", "--flat-playlist", "--print", "%(id)s\t%(title)s",
                                    f"https://www.youtube.com/@Officialfromis9/{tab}"], capture_output=True, timeout=900)
                f.write_bytes(r.stdout)
            except (OSError, subprocess.SubprocessError):
                continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if "\t" in line:
                out.append(tuple(line.split("\t", 1)))
    return out


def upload_dates(ids):
    cache_file = RAW / "youtube-dates.json"
    known = load_json(cache_file, {})
    todo = [i for i in ids if i not in known]
    print(f"유튜브 업로드 날짜 {len(todo)}개 확인 중…")
    if todo:
        try:
            r = subprocess.run(["yt-dlp", "--encoding", "utf-8", "--skip-download", "--ignore-errors", "--sleep-requests", "1",
                                "--print", "%(id)s	%(upload_date)s", *[f"https://youtu.be/{i}" for i in todo]],
                               capture_output=True, timeout=3600)
            for line in r.stdout.decode("utf-8", "replace").splitlines():
                i, _, d = line.partition("	")
                if re.fullmatch(r"\d{8}", d):
                    known[i] = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            cache_file.write_text(json.dumps(known), encoding="utf-8")
            return known
        except (OSError, subprocess.SubprocessError):
            pass
    for n, i in enumerate(todo):
        try:
            h = fetch(f"https://www.youtube.com/watch?v={i}", 30)
            m = re.search(r'"(?:uploadDate|publishDate)":"(\d{4}-\d\d-\d\d)', h)
            known[i] = m.group(1) if m else None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("  유튜브가 잠시 막음 — 나중에 다시 실행하면 이어서 받는다")
                break
        except OSError:
            pass
        if n % 20 == 19:
            cache_file.write_text(json.dumps(known), encoding="utf-8")
        time.sleep(1.5)
    cache_file.write_text(json.dumps(known), encoding="utf-8")
    return known


# ---------------- 합치기 ----------------

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    print("캘린더 받는 중…")
    with urllib.request.urlopen(build.ICS_URL, timeout=60) as r:
        calendar = build.parse(r.read().decode("utf-8").replace("�", ""))
    seen = {url_key(l["u"]) for e in calendar for l in e["l"]}

    namu = [ev for page in NAMU_PAGES for ev in namu_events(page)]
    pedia = fromispedia_events()
    raw_events = [ev for ev in namu if ev["d"] < CUTOFF or ev.get("b")] + pedia

    yt_ids = {url_key(u) for ev in raw_events for u, _ in ev["links"] if "youtu" in u and build.youtube_id(u)}
    channel = channel_videos()
    yt = youtube_titles(sorted(yt_ids | {i for i, _ in channel}))
    wv = weverse_titles(sorted({u for ev in pedia for u, _ in ev["links"] if "weverse.io" in u}))

    events = []

    def add(ev, prefer_titles=False):
        items = []
        for u, t in ev["links"]:
            if not allowed(u):
                continue
            k = url_key(u)
            if k in seen:
                continue
            y = build.youtube_id(u) if "youtu" in u else None
            if y and yt.get(y, {"t": ""}) is None:
                continue  # 지워진 영상
            if y and yt.get(y) and yt[y]["a"] and "fromisubs" in yt[y]["a"].lower():
                continue
            title = (yt.get(y) or {}).get("t") if y else wv.get(u)
            if y or prefer_titles or not t:
                t = title or t  # 유튜브는 실제 영상 제목이 가장 알아보기 쉽다
            seen.add(k)
            if t in ("Source", "Part 1", "Part 2", "Part 3"):
                t = ""
            item = {"u": u, "t": clean(t), "p": build.platform(u)}
            if y:
                item["y"] = y
            items.append(item)
        if ev.get("b"):
            items.append({"u": f"tv:{ev['d']}:{ev['s']}", "t": "", "p": "tv"})
        if not items:
            return
        out = {"d": ev["d"], "s": clean(ev["s"]), "n": clean(ev.get("n", ""))[:200], "l": items}
        if ev.get("b"):
            out.update(b=1, k=program_key(ev.get("prog") or ev["s"]))
            if ev.get("e"):
                out["e"] = ev["e"]
        events.append(out)

    for ev in namu:
        if ev["d"] < CUTOFF or ev.get("b"):
            add(ev)
    for ev in pedia:
        ev = dict(ev)
        first_title = ""
        for u, _ in ev["links"]:
            y = build.youtube_id(u) if "youtu" in u else None
            first_title = ((yt.get(y) or {}).get("t") if y else wv.get(u)) or ""
            if first_title:
                break
        en = re.sub(r"^(Facebook|Twitter|Instagram|V LIVE)\s*-\s*", "", clean(ev["en"]).strip(" -|"))
        ev["s"] = with_members(first_title or en, ev["mem"])
        add(ev, prefer_titles=True)

    # 공식 채널에서 아직 어디에도 없는 영상: 날짜를 알아내 2022년 이전 것만
    rest = [(i, t) for i, t in channel if i not in seen and yt.get(i) is not None]
    dates = upload_dates([i for i, _ in rest])
    for i, t in rest:
        d = dates.get(i)
        if d and d < CUTOFF:
            add({"d": d, "s": (yt.get(i) or {}).get("t") or t, "links": [(f"https://www.youtube.com/watch?v={i}", "")]})

    events.sort(key=lambda e: (e["d"], e["s"]), reverse=True)
    out = HERE / "archive.json"
    out.write_text(json.dumps(events, ensure_ascii=False, indent=0), encoding="utf-8")
    pre = sum(e["d"] < CUTOFF for e in events)
    print(f"완료: {len(events)}개 (2022년 이전 {pre}개, 그 뒤 방송 {len(events) - pre}개) → {out}")


if __name__ == "__main__":
    main()
