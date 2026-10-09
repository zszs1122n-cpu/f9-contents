"""프로미스나인 영상 캘린더(구글 캘린더 공개 ICS)를 받아 컨텐츠 목록 페이지(index.html)를 만든다.

사용법:  python build.py
- 링크가 하나도 없는 일정(오프라인 행사 등)은 제외한다.
- 캘린더는 2022년부터라서, 그 이전 컨텐츠와 TV·라디오 출연은 archive/archive.json(archive/collect.py가 만듦)에서 합친다.
- 유튜브가 아닌 링크는 대표 이미지를 받아 작게 줄여 .cache/thumbs/ 에 두고 site/thumbs/ 로 함께 올린다(Pillow 필요).
- 결과는 site/index.html (데이터가 내장되어 있어 더블클릭으로도 열림). GitHub Actions가 매일 이걸 GitHub Pages에 올린다.
- .cache/ 에는 프롬 짧은 링크 → 영상 번호 캐시, 썸네일, 마지막 빌드 정보가 남는다(저장소에 함께 커밋).
"""
import concurrent.futures as cf
import hashlib
import html
import io
import json
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CAL_ID = "c04c6ea6c1d803550616182c4ef41f596fc5fbe4d5b51d2294b2003c3c14b1ce@group.calendar.google.com"
ICS_URL = f"https://calendar.google.com/calendar/ical/{CAL_ID.replace('@', '%40')}/public/basic.ics"
HERE = Path(__file__).parent
CACHE = HERE / ".cache"
SITE = HERE / "site"
KST = timezone(timedelta(hours=9))
BS = "\\"


def unescape_ics(s):
    return s.replace(BS + "n", "\n").replace(BS + ",", ",").replace(BS + ";", ";").replace(BS + BS, BS)


def platform(url):
    host = re.sub(r"^https?://(www\.|m\.)?", "", url).split("/")[0].lower()
    if "youtu" in host:
        return "youtube"
    if "weverse" in host:
        return "weverse"
    if "instagram" in host:
        return "instagram"
    if "tiktok" in host:
        return "tiktok"
    if "naver" in host:
        return "naver"
    if host in ("x.com", "twitter.com"):
        return "x"
    if "fromm" in host:
        return "fromm"
    return "etc"


def youtube_id(url):
    m = re.search(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([\w-]{11})", url)
    return m.group(1) if m else None


def url_key(url):
    """같은 영상이면 같은 키: 유튜브는 영상 ID, 나머지는 주소."""
    y = youtube_id(url) if "youtu" in url else None
    return y or re.sub(r"^https?://(www\.|m\.)?", "", url).rstrip("/")


def parse(ics):
    ics = re.sub(r"\r?\n[ \t]", "", ics.replace("\r\n", "\n"))
    events = []
    for block in re.findall(r"BEGIN:VEVENT\n(.*?)END:VEVENT", ics, re.S):
        props = {}
        for line in block.split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                props[k.split(";")[0]] = v
        if props.get("STATUS") == "CANCELLED":
            continue
        desc = unescape_ics(props.get("DESCRIPTION", ""))
        links = []
        for url, text in re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', desc, re.S):
            links.append((html.unescape(url), html.unescape(re.sub("<[^>]+>", "", text)).strip()))
        rest = re.sub(r"<a [^>]*>.*?</a>", " ", desc, flags=re.S)
        for url in re.findall(r'https?://[^\s<>"]+', rest):
            links.append((html.unescape(url), ""))
        if not links:
            continue  # 링크 없는 일정 = 오프라인 일정 등 → 제외
        note = re.sub(r'https?://[^\s<>"]+', " ", rest)
        note = html.unescape(re.sub(r"<[^>]+>", " ", note))
        note = re.sub(r"\s+", " ", note).strip()
        d = props.get("DTSTART", "")[:8]
        seen, items = set(), []
        for url, text in links:
            if url in seen:
                continue
            seen.add(url)
            item = {"u": url, "t": text, "p": platform(url)}
            yid = youtube_id(url) if item["p"] == "youtube" else None
            if yid:
                item["y"] = yid
            items.append(item)
        events.append({
            "d": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
            "s": unescape_ics(props.get("SUMMARY", "")).strip(),
            "n": note[:200],
            "l": items,
        })
    # 같은 날짜끼리는 캘린더가 주는 순서가 매번 달라서, 제목·주소로 순서를 고정한다.
    events.sort(key=lambda e: (e["d"], e["s"], [l["u"] for l in e["l"]]), reverse=True)
    return events


def resolve_fromm(events):
    """fromm.my 짧은 링크를 따라가 프롬 영상 번호를 알아내 링크에 적는다(f). 결과는 캐시한다."""
    cache_file = CACHE / "fromm-links.json"
    try:
        known = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}

    class NoFollow(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoFollow)
    for e in events:
        for l in e["l"]:
            if l["p"] != "fromm":
                continue
            if l["u"] not in known:
                try:
                    opener.open(l["u"], timeout=15)
                    known[l["u"]] = None
                except urllib.error.HTTPError as err:
                    target = urllib.parse.unquote(urllib.parse.unquote(err.headers.get("Location", "")))
                    m = re.search(r"/media/(\d+)", target)
                    known[l["u"]] = m.group(1) if m else None
                except OSError:
                    continue  # 다음 빌드 때 다시 시도
            if known.get(l["u"]):
                l["f"] = known[l["u"]]
    cache_file.write_text(json.dumps(known, indent=0, sort_keys=True), encoding="utf-8")


def program_key(text):
    return re.sub(r"[\W_]+", "", text).lower()


def merge_archive(events):
    """archive.json(2022년 이전 컨텐츠 + TV·라디오 출연)을 캘린더 목록에 합친다.
    캘린더와 겹치는 링크는 빼고, 캘린더 기간의 방송 출연은 앞뒤 3일 안에 같은 프로그램 일정이 있으면 뺀다."""
    try:
        archive = json.loads((HERE / "archive" / "archive.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return events
    start = min((e["d"] for e in events), default="9999")
    keys = {url_key(l["u"]) for e in events for l in e["l"]}
    titles = {}
    for e in events:
        titles.setdefault(e["d"], []).append(program_key(e["s"]))
    out = list(events)
    for a in archive:
        if a.get("b") and a["d"] >= start and len(a.get("k", "")) >= 2:
            first = date.fromisoformat(a["d"]) - timedelta(days=3)
            span = (date.fromisoformat(a.get("e") or a["d"]) - first).days + 4
            days = [(first + timedelta(days=i)).isoformat() for i in range(span)]
            if any(a["k"] in t for d in days for t in titles.get(d, [])):
                continue
        links = [l for l in a["l"] if l["p"] == "tv" or url_key(l["u"]) not in keys]
        if not any(l["p"] != "tv" for l in links) and not a.get("b"):
            continue
        note = a.get("n", "")
        if a.get("e"):
            note = f"~{a['e'][5:7]}.{a['e'][8:]} 방송" + (" · " + note if note else "")
        out.append({"d": a["d"], "s": a["s"], "n": note, "l": links})
    out.sort(key=lambda e: (e["d"], e["s"], [l["u"] for l in e["l"]]), reverse=True)
    return out


THUMBS = CACHE / "thumbs"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
BOT_UA = "facebookexternalhit/1.1"  # 위버스·인스타 등은 미리보기용 봇에게만 대표 이미지를 준다


def http_get(url, ua=UA, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Language": "ko-KR,ko;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def thumb_source(l):
    """링크의 대표 이미지 주소. 없으면 None."""
    p, u = l["p"], l["u"]
    if p == "tiktok":
        j = json.loads(http_get("https://www.tiktok.com/oembed?url=" + urllib.parse.quote(u, safe="")))
        return j.get("thumbnail_url")
    if p == "x":
        m = re.search(r"/status(?:es)?/(\d+)", u)
        if not m:
            return None
        j = json.loads(http_get(f"https://api.fxtwitter.com/i/status/{m.group(1)}"))
        for media in ((j.get("tweet") or {}).get("media") or {}).get("all") or []:
            return media.get("thumbnail_url") or media.get("url")
        return None
    page = http_get(u, UA if p == "naver" else BOT_UA).decode("utf-8", "replace")
    m = (re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page)
         or re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', page))
    return html.unescape(m.group(1)) if m else None


def make_thumb(l, dest):
    """대표 이미지를 받아 16:9로 잘라 240×135 JPEG로 저장한다. 성공하면 True."""
    from PIL import Image
    src = thumb_source(l)
    if not src:
        return False
    im = Image.open(io.BytesIO(http_get(src))).convert("RGB")
    w, h = im.size
    if w / h < 16 / 9:  # 세로로 긴 사진은 얼굴이 있는 위쪽을 조금 더 남긴다
        nh = int(w * 9 / 16)
        top = (h - nh) // 3
        im = im.crop((0, top, w, top + nh))
    else:
        nw = int(h * 16 / 9)
        im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    im.resize((240, 135), Image.LANCZOS).save(dest, "JPEG", quality=75, optimize=True, progressive=True)
    return True


def attach_thumbs(events):
    """유튜브가 아닌 링크에 썸네일 파일 이름(i)을 단다. 실패한 링크는 30일 뒤 다시 시도한다."""
    cache_file = CACHE / "thumbs.json"
    try:
        known = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    THUMBS.mkdir(parents=True, exist_ok=True)
    today = date.today()
    links = {url_key(l["u"]): l for e in events for l in e["l"]
             if not l.get("y") and l["p"] not in ("tv", "fromm") and l["u"].startswith("http")}

    def due(k):
        v = known.get(k)
        if isinstance(v, str):
            return not (THUMBS / v).exists()
        return v is None or (today - date.fromisoformat(v["miss"])).days >= 30

    todo = [k for k in links if due(k)]
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("Pillow가 없어 새 썸네일은 건너뜀 (pip install pillow)")
        todo = []
    if todo:
        print(f"썸네일 {len(todo)}개 받는 중…")

    def one(k):
        name = hashlib.sha1(k.encode("utf-8")).hexdigest()[:16] + ".jpg"
        try:
            return k, name if make_thumb(links[k], THUMBS / name) else None
        except Exception:  # 막힌 곳·지워진 글·이상한 이미지 등은 모두 '없음'으로
            return k, None

    with cf.ThreadPoolExecutor(8) as ex:
        for n, (k, name) in enumerate(ex.map(one, todo), 1):
            known[k] = name or {"miss": today.isoformat()}
            if n % 100 == 0:
                print(f"  {n}/{len(todo)}")
                cache_file.write_text(json.dumps(known, indent=0, sort_keys=True), encoding="utf-8")
    cache_file.write_text(json.dumps(known, indent=0, sort_keys=True), encoding="utf-8")

    used = set()
    for k, l in links.items():
        v = known.get(k)
        if isinstance(v, str) and (THUMBS / v).exists():
            used.add(v)
    for e in events:
        for l in e["l"]:
            v = known.get(url_key(l["u"])) if not l.get("y") else None
            if isinstance(v, str) and v in used:
                l["i"] = v
    out_dir = SITE / "thumbs"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    for v in used:
        shutil.copyfile(THUMBS / v, out_dir / v)
    print(f"썸네일 {len(used)}개")


def main():
    print("캘린더 받는 중…")
    with urllib.request.urlopen(ICS_URL, timeout=60) as r:
        ics = r.read().decode("utf-8").replace("�", "")  # 원본 캘린더에 섞인 깨진 글자 제거
    events = parse(ics)
    CACHE.mkdir(exist_ok=True)
    SITE.mkdir(exist_ok=True)
    resolve_fromm(events)
    events = merge_archive(events)
    attach_thumbs(events)
    template = (HERE / "template.html").read_text(encoding="utf-8")
    data = json.dumps(events, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    out = template.replace("/*__DATA__*/[]", data).replace(
        "__UPDATED__", datetime.now(KST).strftime("%Y-%m-%d %H:%M"))
    (SITE / "index.html").write_text(out, encoding="utf-8")
    print(f"완료: 컨텐츠 {len(events)}개 → site/index.html")

    # 내용이 바뀐 날만 기록이 바뀐다 → 저장소에 커밋이 쌓여 GitHub의 예약 실행이 멈추지 않는다.
    digest = hashlib.sha256((data + template).encode("utf-8")).hexdigest()
    state_file = CACHE / "state.json"
    try:
        previous = json.loads(state_file.read_text(encoding="utf-8")).get("hash")
    except (OSError, ValueError):
        previous = None
    if digest != previous:
        state_file.write_text(json.dumps({"hash": digest, "events": len(events),
                                          "updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M")}, indent=1), encoding="utf-8")
    print("변경 있음" if digest != previous else "변경 없음")


if __name__ == "__main__":
    main()
