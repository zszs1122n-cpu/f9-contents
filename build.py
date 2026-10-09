"""프로미스나인 영상 캘린더(구글 캘린더 공개 ICS)를 받아 컨텐츠 목록 페이지(index.html)를 만든다.

사용법:  python build.py
- 링크가 하나도 없는 일정(오프라인 행사 등)은 제외한다.
- 결과는 site/index.html (데이터가 내장되어 있어 더블클릭으로도 열림). GitHub Actions가 매일 이걸 GitHub Pages에 올린다.
- .cache/ 에는 프롬 짧은 링크 → 영상 번호 캐시와 마지막 빌드 정보가 남는다(저장소에 함께 커밋).
"""
import hashlib
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
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


def main():
    print("캘린더 받는 중…")
    with urllib.request.urlopen(ICS_URL, timeout=60) as r:
        ics = r.read().decode("utf-8").replace("�", "")  # 원본 캘린더에 섞인 깨진 글자 제거
    events = parse(ics)
    CACHE.mkdir(exist_ok=True)
    SITE.mkdir(exist_ok=True)
    resolve_fromm(events)
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
