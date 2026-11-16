#!/usr/bin/env python3
"""Build outages.castafero.com.

Pulls live outage signals from around the world, pulls outage-related news, and links the two:
  * Internet connectivity: IODA (Georgia Tech), country- and region-level outage events
  * Cloud and online services: public status pages, AWS, Google Cloud, Azure, Slack
  * Natural hazards that knock out infrastructure: USGS earthquakes, GDACS alerts, NWS extreme alerts
  * News: major outlets, Google News outage searches, security news sites

Standard library only. Usage: python3 build.py [out_dir]
Writes index.html, data.json, and state.txt (fingerprint of active items, for change-only commits).
"""
import html, json, math, os, re, sys, time
import concurrent.futures as cf
import urllib.parse, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

UA = "Mozilla/5.0 (compatible; castafero-outages/1.0; status monitor)"
NOW = datetime.now(timezone.utc)
NEWS_WINDOW = timedelta(hours=36)

STATUSPAGES = [  # (name, base url, extra news aliases)
    ("GitHub", "https://www.githubstatus.com", []),
    ("Cloudflare", "https://www.cloudflarestatus.com", []),
    ("Atlassian", "https://status.atlassian.com", ["Jira", "Confluence", "Trello", "Bitbucket"]),
    ("Discord", "https://discordstatus.com", []),
    ("Reddit", "https://www.redditstatus.com", []),
    ("Zoom", "https://status.zoom.us", []),
    ("Dropbox", "https://status.dropbox.com", []),
    ("OpenAI", "https://status.openai.com", ["ChatGPT"]),
    ("Anthropic", "https://status.anthropic.com", ["Claude AI", "Claude.ai"]),
    ("Twilio", "https://status.twilio.com", []),
    ("DigitalOcean", "https://status.digitalocean.com", []),
    ("Akamai (Linode)", "https://status.linode.com", ["Akamai", "Linode"]),
    ("npm", "https://status.npmjs.org", []),
    ("Shopify", "https://status.shopify.com", []),
    ("Netlify", "https://www.netlifystatus.com", []),
    ("Box", "https://status.box.com", []),
    ("HubSpot", "https://status.hubspot.com", []),
    ("Epic Games", "https://status.epicgames.com", ["Fortnite"]),
    ("Figma", "https://status.figma.com", []),
    ("Twitch", "https://status.twitch.tv", []),
]

NEWS_FEEDS = [
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("BBC", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    ("BBC", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("NPR", "https://feeds.npr.org/1001/rss.xml"),
    ("New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
    ("Guardian", "https://www.theguardian.com/world/rss"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("CBS News", "https://www.cbsnews.com/latest/rss/main"),
    ("ABC News", "https://feeds.abcnews.com/abcnews/topstories"),
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("BleepingComputer", "https://www.bleepingcomputer.com/feed/"),
    ("The Record", "https://therecord.media/feed"),
    ("The Register", "https://www.theregister.com/headlines.atom"),
]
GNEWS_QUERIES = [
    "outage", "outages reported", "internet outage OR internet shutdown OR internet blackout",
    "power outage OR blackout OR power cut", "cyberattack OR ransomware OR DDoS",
    "service disruption OR systems down OR network outage",
]

OUT_RE = re.compile(r"\b(outages?|blackouts?|power cuts?|power failures?|without (power|electricity)|disruptions?|disrupted|"
                    r"offline|internet shutdowns?|goes down|went down|is down|are down|down for|cyber ?-?attacks?|ransomware|"
                    r"ddos|hacked|glitch(es)?|ground stops?|service (was |is )?restored|not working|connectivity|"
                    r"network (failure|issues?|problems?)|systems? (failure|issues?|problems?|down)|service issues?|"
                    r"login (issues?|problems?)|crash(ed|es)?)\b", re.I)
CATS = [  # first match wins
    ("cyber", re.compile(r"\b(cyber ?-?attacks?|ransomware|ddos|hack(ed|ers?)|breach)\b", re.I)),
    ("power", re.compile(r"\b(power|blackouts?|electricity|grid|utility|utilities)\b", re.I)),
    ("internet", re.compile(r"\b(internet|connectivity|telecom|mobile network|broadband|cable cut|subsea|isp)\b", re.I)),
]
HAZARD_RE = re.compile(r"\b(earthquakes?|quakes?|tsunamis?|cyclones?|typhoons?|hurricanes?|floods?|flooding|wildfires?|volcano(es)?|eruptions?|storms?|tornado(es)?)\b", re.I)
STOP = set("""a an the and or but of to in on at for from by with as is are was were be been has have had it its this that
after before over under into about amid says say said new news live update updates will would could can may not no than
then up out off what who why how when where their they we you our more most us u.s via vs report reports reported
outage outages down issues issue problems customers users thousands""".split())


# ---------------------------------------------------------------- fetching
def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(url):
    raw = get(url)
    if raw[:2] in (b"\xfe\xff", b"\xff\xfe"):
        raw = raw.decode("utf-16").encode()
    return json.loads(raw)


def localname(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def child(el, *names):
    for c in el:
        if localname(c.tag) in names:
            t = (c.text or "").strip()
            if t:
                return t
            if c.get("href"):
                return c.get("href").strip()
    return ""


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def ts(epoch):
    return datetime.fromtimestamp(int(epoch), timezone.utc)


def clean(s, limit=None):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"\s+", " ", html.unescape(html.unescape(s))).strip()
    s = re.sub(r"^#+\s*", "", s)
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",;:.") + "…"
    return s


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else ""


# ---------------------------------------------------------------- sources
IODA = "https://api.ioda.inetintel.cc.gatech.edu/v2/outages/events"
DS_LABEL = {"bgp": "Routing (BGP)", "ping-slash24": "Active probing", "gtr": "Google traffic", "gtr-norm": "Google traffic",
            "merit-nt": "Network telescope", "ucsd-nt": "Network telescope"}


def ioda(entity_type, limit):
    until = int(time.time())
    q = urllib.parse.urlencode({"from": until - 86400, "until": until, "limit": 500, "entityType": entity_type})
    data = get_json(f"{IODA}?{q}").get("data") or []
    by_loc = {}
    for ev in data:
        loc = ev["location"]
        start = ts(ev["start"])
        end = ts(ev["start"] + ev["duration"])
        ongoing = end >= NOW - timedelta(minutes=45)
        cur = by_loc.setdefault(loc, {"loc": loc, "name": ev["location_name"], "score": 0, "sources": set(),
                                      "start": start, "end": end, "ongoing": False})
        cur["score"] = max(cur["score"], ev["score"])
        cur["sources"].add(DS_LABEL.get(ev["datasource"], ev["datasource"]))
        cur["start"] = min(cur["start"], start)
        cur["end"] = max(cur["end"], end)
        cur["ongoing"] = cur["ongoing"] or ongoing
    out = sorted(by_loc.values(), key=lambda x: (x["ongoing"], x["score"]), reverse=True)[:limit]
    for x in out:
        kind, code = x["loc"].split("/", 1)
        x.update(kind=kind, code=code, sources=sorted(x["sources"]),
                 url=f"https://ioda.inetintel.cc.gatech.edu/{kind}/{urllib.parse.quote(code)}")
    return out


def statuspage(name, base, aliases):
    s = get_json(base + "/api/v2/summary.json")
    ind = s.get("status", {}).get("indicator", "none")
    incidents = []
    for inc in s.get("incidents", []):
        ups = inc.get("incident_updates") or []
        incidents.append({"title": inc.get("name", ""), "impact": inc.get("impact", "none"),
                          "status": inc.get("status", ""), "start": parse_date(inc.get("started_at") or inc.get("created_at")),
                          "updated": parse_date(inc.get("updated_at")),
                          "detail": clean(ups[0].get("body", ""), 260) if ups else "",
                          "url": inc.get("shortlink") or base})
    bad = [c["name"] for c in s.get("components", []) if c.get("status") not in ("operational", None) and not c.get("group")]
    return {"name": name, "url": base, "indicator": ind, "desc": s.get("status", {}).get("description", ""),
            "incidents": incidents, "components": bad[:6], "n_components": len(bad), "aliases": [name] + aliases}


def aws():
    out = []
    for ev in get_json("https://health.aws.amazon.com/public/currentevents"):
        log = ev.get("event_log") or []
        last = log[-1] if log else {}
        out.append({"title": f'{ev.get("service_name", "AWS")}: {ev.get("summary", "")}', "region": ev.get("region_name", ""),
                    "start": ts(ev["date"]) if str(ev.get("date", "")).isdigit() else None,
                    "updated": ts(last["timestamp"]) if last.get("timestamp") else None,
                    "detail": clean(last.get("message", ""), 260), "impact": "major" if str(ev.get("status")) == "3" else "minor",
                    "url": "https://health.aws.amazon.com/health/status"})
    return {"name": "AWS", "url": "https://health.aws.amazon.com/health/status", "incidents": out,
            "aliases": ["AWS", "Amazon Web Services"]}


def gcp():
    out = []
    for inc in get_json("https://status.cloud.google.com/incidents.json"):
        end = parse_date(inc.get("end"))
        if end and end < NOW - timedelta(hours=24):
            continue
        mru = inc.get("most_recent_update") or {}
        out.append({"title": clean(inc.get("external_desc", ""), 160), "start": parse_date(inc.get("begin")),
                    "updated": parse_date(mru.get("when")), "ended": end,
                    "detail": clean(mru.get("text", ""), 260), "impact": "major" if inc.get("severity") == "high" else "minor",
                    "url": "https://status.cloud.google.com/" + inc.get("uri", "")})
    return {"name": "Google Cloud", "url": "https://status.cloud.google.com/", "incidents": out,
            "aliases": ["Google Cloud", "GCP"]}


def azure():
    root = ET.fromstring(get("https://azure.status.microsoft/en-us/status/feed/"))
    out = [{"title": clean(child(it, "title")), "start": parse_date(child(it, "pubDate")), "updated": None,
            "detail": clean(child(it, "description"), 260), "impact": "major", "url": child(it, "link") or "https://azure.status.microsoft/"}
           for it in root.iter() if localname(it.tag) == "item"]
    return {"name": "Microsoft Azure", "url": "https://azure.status.microsoft/en-us/status/", "incidents": out, "aliases": ["Azure"]}


def slack():
    s = get_json("https://slack-status.com/api/v2.0.0/current")
    out = [{"title": i.get("title", ""), "start": parse_date(i.get("date_created")), "updated": parse_date(i.get("date_updated")),
            "detail": clean((i.get("notes") or [{}])[-1].get("body", ""), 260), "impact": "major" if i.get("type") == "outage" else "minor",
            "url": i.get("url") or "https://slack-status.com/"} for i in s.get("active_incidents", [])]
    return {"name": "Slack", "url": "https://slack-status.com/", "incidents": out, "aliases": ["Slack"]}


def quakes():
    d = get_json("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson")
    out = []
    for f in d.get("features", []):
        p = f["properties"]
        if (p.get("mag") or 0) < 5.0 and p.get("alert") not in ("orange", "red"):
            continue
        place = p.get("place") or ""
        out.append({"kind": "Earthquake", "title": f'M{p["mag"]:.1f} earthquake, {place}', "when": ts(p["time"] / 1000),
                    "level": {"red": "red", "orange": "orange", "yellow": "yellow"}.get(p.get("alert"), "green"),
                    "url": p.get("url", ""), "place": place.split(", ")[-1] if ", " in place else place,
                    "detail": "Tsunami possible" if p.get("tsunami") else ""})
    return out


def gdacs():
    root = ET.fromstring(get("https://www.gdacs.org/xml/rss.xml"))
    kinds = {"EQ": "Earthquake", "TC": "Tropical cyclone", "FL": "Flood", "VO": "Volcano", "WF": "Wildfire", "DR": "Drought"}
    out = []
    for it in root.iter():
        if localname(it.tag) != "item":
            continue
        level = child(it, "alertlevel").lower()
        etype = child(it, "eventtype")
        when = parse_date(child(it, "pubDate"))
        if level not in ("orange", "red") or etype == "EQ" or (when and when < NOW - timedelta(days=7)):
            continue
        out.append({"kind": kinds.get(etype, etype), "title": clean(child(it, "title"), 160), "when": when, "level": level,
                    "url": child(it, "link"), "place": child(it, "country"), "detail": clean(child(it, "description"), 200)})
    return out


def nws():
    d = get_json("https://api.weather.gov/alerts/active?severity=Extreme")
    out = []
    for f in d.get("features", [])[:12]:
        p = f["properties"]
        out.append({"kind": p.get("event", "Alert"), "title": f'{p.get("event", "Alert")}: {clean(p.get("areaDesc", ""), 120)}',
                    "when": parse_date(p.get("sent")), "level": "red", "url": "https://alerts.weather.gov/",
                    "place": "United States", "detail": clean(p.get("headline", ""), 200)})
    return out


def news_feed(src, url, gnews=False):
    root = ET.fromstring(get(url))
    out = []
    for el in root.iter():
        if localname(el.tag) not in ("item", "entry"):
            continue
        title = clean(child(el, "title"))
        link = ""
        for c in el:
            if localname(c.tag) == "link":
                link = (c.text or "").strip() or c.get("href", "")
                if link:
                    break
        if not title or not link.startswith("http"):
            continue
        pub = src
        if gnews:
            pub = child(el, "source") or src
            title = re.sub(r"\s+-\s+[^-]{2,60}$", "", title)
        date = parse_date(child(el, "pubDate", "date", "published", "updated"))
        if date and NOW - date > NEWS_WINDOW:
            continue
        out.append({"src": pub, "title": title, "link": link, "date": date,
                    "desc": "" if gnews else clean(child(el, "description", "summary"), 220)})
    return out[:60]


# ---------------------------------------------------------------- correlation helpers
def tokens(t):
    return {w for w in re.findall(r"[a-z0-9]+", t.lower()) if len(w) > 2 and w not in STOP}


def cluster(items):
    """Group headlines about the same event: leader clustering, rare words weighted higher."""
    df = {}
    for it in items:
        for w in tokens(it["title"]):
            df[w] = df.get(w, 0) + 1
    n = len(items)
    idf = lambda w: math.log((n + 1) / (df.get(w, 0) + 1))

    def sim(a, b):
        sh = a & b
        if len(sh) < 2:
            return 0.0
        return sum(map(idf, sh)) / max(1e-9, min(sum(map(idf, a)), sum(map(idf, b))))

    groups = []
    for it in items:
        tk = tokens(it["title"])
        best, bs = None, 0.0
        for g in groups:
            ls = sim(tk, g["lead"])
            if ls < 0.2:
                continue
            s = max([ls] + [sim(tk, t) for t in g["toks"]])
            if s > bs:
                best, bs = g, s
        if best and bs >= 0.45:
            best["items"].append(it)
            best["toks"].append(tk)
        else:
            groups.append({"lead": tk, "toks": [tk], "items": [it]})
    return [g["items"] for g in groups]


CAP_STOP = set("""are is the how why what did does who when where will can could major massive global new emergency fears
update updates live watch breaking report reports reported customers users outage outages power internet service services
after amid down state city county police federal government national north south east west week today monday tuesday
wednesday thursday friday saturday sunday january february march april may june july august september october november
december trump president officials company companies people thousands millions some this that these here there first
widespread nationwide issues problems explains reveals says apologizes blames blamed restores restored hit hits
blackout blackouts cut cuts planned scheduled rumour rumours rumor rumors warning warns urgent equipment failure
electricity grid outage leave leaves left without struggling access accounts experience face faces drawing among
school schools traffic early dismissal residents homes customers expected restoration crews storm weather""".split())


def entities(title):
    ents = set()
    for w in re.findall(r"[A-Za-z][A-Za-z&]+", title):
        lw = w.lower()
        if lw.startswith("bank") or lw in ("chase", "jpmorgan", "fargo", "citi", "citibank", "bofa", "pnc", "truist", "usaa"):
            ents.add("bank")          # Chase / BofA / "banking outage" stories are one event cluster
        elif w[0].isupper() and len(w) >= 3 and lw not in CAP_STOP and lw not in STOP:
            ents.add(lw.rstrip("s") if len(lw) > 4 else lw)
    return ents


def merge_by_entity(groups):
    """Second pass: join non-cyber groups whose main subject is the same company or institution
    (Microsoft, Chase, FAA...), which headline wording alone often fails to connect. Each group gets
    exactly one key (its most-mentioned name), so merges can't chain across unrelated stories."""
    total = sum(len(g) for g in groups) or 1
    freq = {}
    for g in groups:
        for it in g:
            for en in entities(it["title"]):
                freq[en] = freq.get(en, 0) + 1
    buckets, out = {}, []
    for g in groups:
        cat = max(("cyber", "power", "internet", "service"), key=lambda c: sum(x["cat"] == c for x in g))
        counts = {}
        for it in g:
            for en in entities(it["title"]):
                counts[en] = counts.get(en, 0) + 1
        key = None
        if cat != "cyber" and counts:
            key = max(counts, key=lambda en: (counts[en], -freq[en]))
            if counts[key] * 2 < len(g) or freq[key] < 2 or freq[key] / total > 0.4:
                key = None
        if key is None:
            out.append(g)
        else:
            buckets.setdefault((cat, key), []).extend(g)
    return out + list(buckets.values())


def alias_re(aliases):
    parts = []
    for a in aliases:
        a = a.strip()
        if not a:
            continue
        pat = re.escape(a)
        parts.append(pat if (a.isupper() and len(a) <= 4) else pat)
    flags = 0 if all(a.isupper() and len(a) <= 4 for a in aliases) else re.I
    return re.compile(r"(?<![\w.])(" + "|".join(parts) + r")(?![\w])", flags)


def category(text):
    for name, rx in CATS:
        if rx.search(text):
            return name
    return "service"


# ---------------------------------------------------------------- main
def main(out_dir):
    jobs = {
        "ioda_country": (ioda, ("country", 40)),
        "ioda_region": (ioda, ("region", 25)),
        "aws": (aws, ()), "gcp": (gcp, ()), "azure": (azure, ()), "slack": (slack, ()),
        "quakes": (quakes, ()), "gdacs": (gdacs, ()), "nws": (nws, ()),
    }
    for name, base, al in STATUSPAGES:
        jobs["sp:" + name] = (statuspage, (name, base, al))
    for i, (src, url) in enumerate(NEWS_FEEDS):
        jobs[f"news:{i}"] = (news_feed, (src, url))
    for i, q in enumerate(GNEWS_QUERIES):
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q + " when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
        jobs[f"gnews:{i}"] = (news_feed, ("Google News", url, True))

    res, failed = {}, []
    with cf.ThreadPoolExecutor(12) as ex:
        futs = {ex.submit(fn, *args): key for key, (fn, args) in jobs.items()}
        for fu in cf.as_completed(futs):
            k = futs[fu]
            try:
                res[k] = fu.result()
            except Exception as err:
                failed.append(k)
                print(f"source failed: {k}: {err}", file=sys.stderr)

    # ---- news pool (outage-related only), deduped
    seen, news = set(), []
    for k in sorted(res):
        if not k.startswith(("news:", "gnews:")):
            continue
        for it in res[k]:
            key = re.sub(r"\W+", " ", it["title"].lower()).strip()
            text = it["title"] + " " + it["desc"]
            if key in seen or not (OUT_RE.search(text) or HAZARD_RE.search(it["title"])):
                continue
            seen.add(key)
            it["cat"] = category(text)
            it["outage"] = bool(OUT_RE.search(text))
            news.append(it)
    news.sort(key=lambda x: x["date"] or NOW - NEWS_WINDOW, reverse=True)
    if not news and len(failed) > len(jobs) / 2:
        sys.exit("most sources failed; keeping the previous page")

    def related(aliases, need_outage=True, hazard=False, strict=False):
        rx = alias_re(aliases)
        hits = []
        for it in news:
            # the name must be in the headline itself; passing mentions in a summary are too noisy
            if not rx.search(it["title"]):
                continue
            if hazard and not HAZARD_RE.search(it["title"]):
                continue
            if need_outage and not hazard and not (OUT_RE.search(it["title"]) if strict else it["outage"]):
                continue
            hits.append(it)
        return hits

    used = set()

    def take(hits):
        for h in hits:
            used.add(id(h))
        return hits[:6]

    # ---- services
    services = []
    for name, base, al in STATUSPAGES:
        sp = res.get("sp:" + name)
        if sp is None:
            services.append({"name": name, "url": base, "indicator": "unknown", "incidents": [], "components": [],
                             "n_components": 0, "aliases": [name] + al, "desc": "Status page unreachable"})
        else:
            services.append(sp)
    for key in ("aws", "gcp", "azure", "slack"):
        c = res.get(key)
        if c is None:
            continue
        inc = c["incidents"]
        active = [i for i in inc if not i.get("ended")]
        c["indicator"] = ("major" if any(i["impact"] == "major" for i in active) else "minor") if active else "none"
        c["desc"] = f"{len(active)} active incident{'s' if len(active) != 1 else ''}" if active else "All systems operational"
        c["components"], c["n_components"] = [], 0
        services.append(c)
    for s in services:
        s["news"] = take(related(s["aliases"])) if (s["incidents"] or s["indicator"] not in ("none", "unknown")) else []
    sev = {"critical": 4, "major": 3, "minor": 2, "maintenance": 1, "unknown": 1, "none": 0}
    services.sort(key=lambda s: (-sev.get(s["indicator"], 0), -len(s["incidents"]), s["name"].lower()))

    # ---- internet
    countries = res.get("ioda_country", [])
    for c in countries:
        c["news"] = take(related([c["name"]], strict=True))
    regions = res.get("ioda_region", [])

    # ---- hazards
    hazards = sorted(res.get("quakes", []) + res.get("gdacs", []) + res.get("nws", []),
                     key=lambda h: ({"red": 3, "orange": 2, "yellow": 1}.get(h["level"], 0), h["when"] or NOW), reverse=True)[:20]
    for h in hazards:
        place = re.sub(r"^(near|off|the)\s+", "", h.get("place") or "", flags=re.I).strip()
        h["news"] = take(related([place], hazard=True)) if len(place) > 3 else []

    # ---- news-only outages: outage headlines not tied to anything above, grouped by event
    leftovers = [it for it in news if id(it) not in used and it["outage"]]
    groups = merge_by_entity(cluster(leftovers))
    reported = []
    for g in groups:
        pubs = {x["src"] for x in g}
        latest = max((x["date"] for x in g if x["date"]), default=None)
        reported.append({"items": g, "pubs": len(pubs), "latest": latest, "cat": max(
            ("cyber", "power", "internet", "service"), key=lambda c: sum(x["cat"] == c for x in g))})
    reported.sort(key=lambda r: (r["pubs"], r["latest"] or NOW - NEWS_WINDOW), reverse=True)
    reported = reported[:30]

    # ---- correlated summary: anything on a monitor that the news is also covering
    correlated = []
    for s in services:
        if s["news"]:
            correlated.append({"type": "Service", "name": s["name"], "status": s["desc"], "url": s["url"], "news": s["news"]})
    for c in countries:
        if c["news"]:
            correlated.append({"type": "Internet", "name": c["name"], "status": ("Ongoing" if c["ongoing"] else "Recent") + " connectivity drop",
                               "url": c["url"], "news": c["news"]})
    for h in hazards:
        if h["news"]:
            correlated.append({"type": h["kind"], "name": h["title"], "status": f'{h["level"].title()} alert', "url": h["url"], "news": h["news"]})

    counts = {
        "internet": sum(1 for c in countries if c["ongoing"]),
        "services": sum(1 for s in services if s["indicator"] not in ("none", "unknown")),
        "hazards": sum(1 for h in hazards if h["level"] in ("orange", "red")),
        "news": sum(1 for r in reported if r["pubs"] >= 2),
        "correlated": len(correlated),
    }
    os.makedirs(out_dir, exist_ok=True)
    page = render(countries, regions, services, hazards, reported, correlated, counts, failed, len(jobs))
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(page)

    def jsonable(o):
        if isinstance(o, datetime):
            return iso(o)
        if isinstance(o, set):
            return sorted(o)
        raise TypeError(type(o))
    data = {"updated": iso(NOW), "counts": counts, "internet": countries, "regions": regions,
            "services": [{k: v for k, v in s.items() if k != "aliases"} for s in services],
            "hazards": hazards, "news_reported": [{"pubs": r["pubs"], "cat": r["cat"], "headlines": r["items"][:6]} for r in reported],
            "failed_sources": failed}
    with open(os.path.join(out_dir, "data.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, default=jsonable, indent=1)
    state = sorted([f"net {c['loc']}" for c in countries if c["ongoing"]]
                   + [f"svc {s['name']} {s['indicator']} {len(s['incidents'])}" for s in services if s["indicator"] != "none"]
                   + [f"haz {h['title']}" for h in hazards if h["level"] in ("orange", "red")])
    with open(os.path.join(out_dir, "state.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(state) + "\n")
    print(f"built: {counts}, {len(news)} outage headlines, {len(failed)}/{len(jobs)} sources failed")


# ---------------------------------------------------------------- rendering
def e(s):
    return html.escape(str(s or ""), quote=True)


def t(d, cls="ago"):
    return f'<time class="{cls}" datetime="{iso(d)}">{iso(d)}</time>' if d else ""


def flag(code):
    return "".join(chr(0x1F1E6 + ord(ch) - 65) for ch in code.upper()) if re.fullmatch(r"[A-Za-z]{2}", code or "") else "🌐"


def news_list(items, limit=4):
    if not items:
        return ""
    lis = "".join(f'<li><a href="{e(n["link"])}" rel="noopener">{e(n["title"])}</a> <span class="pub">{e(n["src"])}</span> {t(n["date"])}</li>'
                  for n in items[:limit])
    return f'<ul class="news">{lis}</ul>'


def sev_bar(score, top):
    w = 8 + 92 * (math.log10(max(score, 1)) / max(1, math.log10(max(top, 10))))
    return f'<div class="bar"><div style="width:{min(100, w):.0f}%"></div></div>'


def render(countries, regions, services, hazards, reported, correlated, counts, failed, total):
    # correlated
    corr = "".join(f'''<article class="card corr"><div class="row"><span class="tag">{e(c["type"])}</span>
      <a class="name" href="{e(c["url"])}" rel="noopener">{e(c["name"])}</a><span class="muted">{e(c["status"])}</span></div>
      {news_list(c["news"], 5)}</article>''' for c in correlated) or '<p class="empty">No monitored outage is in the news right now.</p>'

    # news-only
    rep = []
    for r in reported:
        lead, rest = r["items"][0], r["items"][1:]
        more = news_list(rest, 4)
        rep.append(f'''<article class="card rep" data-cat="{r["cat"]}"><div class="row"><span class="tag c-{r["cat"]}">{r["cat"]}</span>
          <span class="muted">{r["pubs"]} source{"s" if r["pubs"] != 1 else ""}</span>{t(lead["date"])}</div>
          <h3><a href="{e(lead["link"])}" rel="noopener">{e(lead["title"])}</a></h3><div class="muted small">{e(lead["src"])}</div>
          {f'<details><summary>{len(rest)} more headline{"s" if len(rest) != 1 else ""}</summary>{more}</details>' if rest else ""}</article>''')
    rep_html = "".join(rep) or '<p class="empty">No outage headlines in the last 36 hours.</p>'

    # internet
    top = max([c["score"] for c in countries] + [10])
    net = "".join(f'''<tr class="{"on" if c["ongoing"] else "off"}"><td><span class="flag">{flag(c["code"])}</span>
        <a href="{e(c["url"])}" rel="noopener">{e(c["name"])}</a>{'<span class="badge news-b" title="In the news">news</span>' if c["news"] else ""}</td>
      <td>{sev_bar(c["score"], top)}</td><td class="muted small">{e(", ".join(c["sources"]))}</td>
      <td>{'<span class="live">ongoing</span>' if c["ongoing"] else "ended " + t(c["end"])}</td><td>{t(c["start"])}</td></tr>
      {f'<tr class="newsrow"><td colspan="5">{news_list(c["news"], 3)}</td></tr>' if c["news"] else ""}''' for c in countries)
    net_html = (f'<div class="tablewrap"><table><thead><tr><th>Country</th><th>Relative severity</th><th>Detected by</th><th>Status</th><th>Started</th></tr></thead>'
                f'<tbody>{net}</tbody></table></div>') if countries else '<p class="empty">No country-level internet outages detected in the last 24 hours.</p>'
    rtop = max([r["score"] for r in regions] + [10])
    reg = "".join(f'<li><a href="{e(r["url"])}" rel="noopener">{e(r["name"])}</a>{sev_bar(r["score"], rtop)}'
                  f'<span class="muted small">{"ongoing" if r["ongoing"] else "ended"}</span></li>' for r in regions[:15])

    # services
    dot = {"none": "ok", "minor": "minor", "maintenance": "maint", "major": "major", "critical": "major", "unknown": "unk"}
    svc_grid = "".join(f'<a class="svc {dot.get(s["indicator"], "unk")}" href="{e(s["url"])}" rel="noopener" title="{e(s["desc"])}">'
                       f'<span class="dot"></span>{e(s["name"])}</a>' for s in services)
    svc_cards = []
    for s in services:
        if not s["incidents"] and s["indicator"] in ("none", "unknown"):
            continue
        incs = "".join(f'''<li><b>{e(i["title"])}</b> <span class="imp i-{e(i["impact"])}">{e(i["impact"])}</span>
            {f'<span class="muted small">{e(i.get("region"))}</span>' if i.get("region") else ""}
            <div class="muted small">Started {t(i["start"])}{" · updated " + t(i["updated"]) if i.get("updated") else ""}{" · resolved " + t(i["ended"]) if i.get("ended") else ""}</div>
            {f'<div class="detail">{e(i["detail"])}</div>' if i["detail"] else ""}<a class="small" href="{e(i["url"])}" rel="noopener">Details</a></li>'''
                       for i in s["incidents"][:5])
        comps = f'<div class="muted small">Affected: {e(", ".join(s["components"]))}{" and more" if s["n_components"] > len(s["components"]) else ""}</div>' if s["components"] else ""
        svc_cards.append(f'''<article class="card"><div class="row"><span class="svc {dot.get(s["indicator"], "unk")}"><span class="dot"></span></span>
          <a class="name" href="{e(s["url"])}" rel="noopener">{e(s["name"])}</a><span class="muted">{e(s["desc"])}</span></div>
          {comps}{f'<ul class="incs">{incs}</ul>' if incs else ""}{news_list(s["news"], 3)}</article>''')

    # hazards
    hz = "".join(f'''<article class="card haz l-{e(h["level"])}"><div class="row"><span class="tag">{e(h["kind"])}</span>
        <span class="lvl">{e(h["level"])}</span>{t(h["when"])}</div><h3><a href="{e(h["url"])}" rel="noopener">{e(h["title"])}</a></h3>
        {f'<div class="muted small">{e(h["detail"])}</div>' if h["detail"] else ""}{news_list(h["news"], 3)}</article>''' for h in hazards) \
        or '<p class="empty">No significant hazard alerts.</p>'

    chips = [("correlated", "in the news", counts["correlated"]), ("internet", "countries losing connectivity", counts["internet"]),
             ("services", "services degraded", counts["services"]), ("news", "news-only outages (2+ sources)", counts["news"]),
             ("hazards", "orange/red hazard alerts", counts["hazards"])]
    chip_html = "".join(f'<a class="stat{" hot" if n else ""}" href="#{k}"><b>{n}</b> {lbl}</a>' for k, lbl, n in chips)
    return (TEMPLATE.replace("{{UPDATED}}", iso(NOW)).replace("{{CHIPS}}", chip_html).replace("{{CORR}}", corr)
            .replace("{{REPORTED}}", rep_html).replace("{{NET}}", net_html).replace("{{REG}}", reg or '<li class="muted">None</li>')
            .replace("{{SVCGRID}}", svc_grid).replace("{{SVCCARDS}}", "".join(svc_cards) or '<p class="empty">Every monitored service reports normal operation.</p>')
            .replace("{{HAZ}}", hz).replace("{{SRC}}", f"{total - len(failed)} of {total} sources responded"
                                                     + (f" (unavailable: {e(', '.join(failed))})" if failed else "")))


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Castafero Outages</title>
<meta name="description" content="Live internet, cloud, service and infrastructure outages worldwide, matched to breaking news. Updated every 15 minutes.">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@500;600&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#f5f6f8;--surface:#fff;--ink:#16181d;--muted:#5d6470;--line:#e1e4ea;--soft:#eef1f5;--accent:#d9480f;
  --ok:#2b8a3e;--minor:#e8a317;--major:#d6336c;--maint:#4263eb;--unk:#868e96;
  --f-body:"Inter",system-ui,sans-serif;--f-mono:"IBM Plex Mono",ui-monospace,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--surface:#171a20;--ink:#e8eaee;--muted:#9aa2ae;--line:#2a2f38;--soft:#1f232b;--accent:#ff8a4c;
  --ok:#51cf66;--minor:#fcc419;--major:#ff6b9a;--maint:#748ffc}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--f-body)}
a{color:inherit}
.wrap{max-width:1200px;margin:0 auto;padding:0 16px}
header{background:var(--surface);border-bottom:1px solid var(--line);padding:18px 0 14px;position:sticky;top:0;z-index:5}
.mast{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
h1{margin:0;font:700 1.7rem var(--f-body);letter-spacing:-.02em}
h1 span{color:var(--accent)}
.upd{color:var(--muted);font-size:.85rem;font-family:var(--f-mono)}
.stats{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.stat{text-decoration:none;font-size:.85rem;padding:5px 11px;border:1px solid var(--line);border-radius:999px;background:var(--soft);color:var(--muted)}
.stat b{font-family:var(--f-mono);color:var(--ink)}
.stat.hot{border-color:var(--accent);color:var(--ink)} .stat.hot b{color:var(--accent)}
main{padding:22px 0 40px;display:flex;flex-direction:column;gap:34px}
h2{font-size:1.15rem;margin:0 0 4px;display:flex;align-items:center;gap:8px}
.sub{color:var(--muted);font-size:.86rem;margin:0 0 14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,360px),1fr));gap:14px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-width:0}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.name{font-weight:600;text-decoration:none}
.card h3{font-size:1rem;margin:8px 0 2px;line-height:1.35}
.card h3 a{text-decoration:none} .card h3 a:hover,.name:hover,.news a:hover{text-decoration:underline}
.tag{font-family:var(--f-mono);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;padding:2px 7px;border-radius:4px;background:var(--soft);border:1px solid var(--line)}
.c-cyber{color:var(--major)} .c-power{color:var(--minor)} .c-internet{color:var(--maint)} .c-service{color:var(--accent)}
.muted{color:var(--muted)} .small{font-size:.82rem}
.ago{color:var(--muted);font-size:.8rem;font-family:var(--f-mono)}
.news{list-style:none;margin:10px 0 0;padding:10px 0 0;border-top:1px dashed var(--line);display:flex;flex-direction:column;gap:6px;font-size:.88rem}
.news a{text-decoration:none}
.pub{color:var(--muted);font-size:.78rem;font-weight:600}
.corr{border-left:4px solid var(--accent)}
details summary{cursor:pointer;color:var(--muted);font-size:.84rem;margin-top:8px}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.filters button{font:inherit;font-size:.84rem;padding:4px 12px;border-radius:999px;border:1px solid var(--line);background:var(--surface);color:var(--muted);cursor:pointer}
.filters button[aria-pressed="true"]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.tablewrap{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:10px}
table{width:100%;border-collapse:collapse;font-size:.9rem;min-width:640px}
th{text-align:left;font-weight:600;color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.04em;padding:10px 12px;border-bottom:1px solid var(--line)}
td{padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:middle}
tr.off td{opacity:.65}
tr.newsrow td{padding-top:0;opacity:1} tr.newsrow .news{margin-top:0;border-top:0}
.flag{font-size:1.1rem;margin-right:6px}
td a{text-decoration:none;font-weight:500}
.bar{height:8px;background:var(--soft);border-radius:999px;overflow:hidden;min-width:90px}
.bar div{height:100%;background:linear-gradient(90deg,var(--minor),var(--major))}
.live{color:var(--major);font-weight:600;font-size:.84rem}
.live::before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--major);margin-right:6px;animation:p 1.6s infinite}
@keyframes p{50%{opacity:.25}}
.badge{font-size:.7rem;margin-left:8px;padding:1px 6px;border-radius:4px;border:1px solid var(--accent);color:var(--accent)}
.regions{list-style:none;padding:0;margin:14px 0 0;display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,260px),1fr));gap:8px 18px}
.regions li{display:grid;grid-template-columns:minmax(0,1fr) 90px auto;gap:10px;align-items:center;font-size:.86rem}
.regions a{text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.svcgrid{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}
.svc{display:inline-flex;align-items:center;gap:7px;text-decoration:none;font-size:.86rem;padding:5px 11px;border:1px solid var(--line);border-radius:7px;background:var(--surface)}
.row .svc{padding:0;border:0;background:none}
.dot{width:9px;height:9px;border-radius:50%;background:var(--unk)}
.svc.ok .dot{background:var(--ok)} .svc.minor .dot{background:var(--minor)} .svc.major .dot{background:var(--major)} .svc.maint .dot{background:var(--maint)}
.svc.minor,.svc.major{border-color:currentColor} .svc.minor{color:var(--minor)} .svc.major{color:var(--major)}
.incs{list-style:none;padding:0;margin:10px 0 0;display:flex;flex-direction:column;gap:10px}
.imp{font-family:var(--f-mono);font-size:.7rem;padding:1px 6px;border-radius:4px;border:1px solid var(--line)}
.i-major,.i-critical{color:var(--major);border-color:var(--major)} .i-minor{color:var(--minor);border-color:var(--minor)}
.detail{font-size:.85rem;margin:4px 0}
.haz{border-left:4px solid var(--unk)} .haz.l-red{border-left-color:var(--major)} .haz.l-orange{border-left-color:var(--minor)} .haz.l-yellow{border-left-color:#fab005}
.lvl{font-family:var(--f-mono);font-size:.72rem;text-transform:uppercase}
.empty{color:var(--muted);background:var(--surface);border:1px dashed var(--line);border-radius:10px;padding:14px 16px;margin:0}
footer{border-top:1px solid var(--line);padding:18px 0 40px;color:var(--muted);font-size:.8rem}
footer a{color:var(--ink)}
</style>
</head>
<body>
<header><div class="wrap">
  <div class="mast"><h1>Castafero <span>Outages</span></h1>
  <div class="upd">Updated <time id="upd" datetime="{{UPDATED}}">{{UPDATED}}</time> · every 15 min</div></div>
  <div class="stats">{{CHIPS}}</div>
</div></header>
<main class="wrap">
  <section id="correlated"><h2>Outages in the news</h2>
    <p class="sub">Outages our monitors detected, matched to headlines that name the same service, country or event.</p>
    <div class="grid">{{CORR}}</div></section>

  <section id="news"><h2>Reported in the news</h2>
    <p class="sub">Outages the press is reporting that none of our monitors cover, such as banks, airlines, carriers and power utilities. Headlines about the same event are grouped together, and events covered by the most outlets come first.</p>
    <div class="filters" id="filters"><button aria-pressed="true" data-f="all">All</button><button aria-pressed="false" data-f="service">Services</button><button aria-pressed="false" data-f="internet">Internet</button><button aria-pressed="false" data-f="power">Power</button><button aria-pressed="false" data-f="cyber">Cyber</button></div>
    <div class="grid" id="reported">{{REPORTED}}</div></section>

  <section id="internet"><h2>Internet connectivity, worldwide</h2>
    <p class="sub">Country-level outage events from the last 24 hours, detected by <a href="https://ioda.inetintel.cc.gatech.edu/">IODA</a> (Georgia Tech) through routing data, active probing and Google traffic. Severity bars compare events on this page with each other, not with a fixed scale.</p>
    {{NET}}
    <h3 class="sub" style="margin-top:18px">Largest regional events</h3><ul class="regions">{{REG}}</ul></section>

  <section id="services"><h2>Cloud &amp; online services</h2>
    <p class="sub">Each provider's own status page. A green dot means the provider reports normal operation.</p>
    <div class="svcgrid">{{SVCGRID}}</div>
    <div class="grid">{{SVCCARDS}}</div></section>

  <section id="hazards"><h2>Natural hazards that can knock out infrastructure</h2>
    <p class="sub">USGS earthquakes of magnitude 5 and up in the last 24 hours, GDACS orange and red alerts worldwide, and U.S. National Weather Service extreme alerts.</p>
    <div class="grid">{{HAZ}}</div></section>
</main>
<footer><div class="wrap">Sources: IODA (Georgia Tech), provider status pages, AWS Health, Google Cloud, Azure, Slack, USGS, GDACS, NWS, and news RSS feeds (BBC, NPR, NYT, Guardian, Al Jazeera, CBS, ABC, CNBC, Ars Technica, The Verge, BleepingComputer, The Record, The Register, Google News). Outages and headlines are matched automatically on names and keywords, so some matches will be wrong. Check the linked source. {{SRC}}. Raw data: <a href="data.json">data.json</a>.</div></footer>
<script>
(function(){
  function ago(d){var m=Math.round((Date.now()-d)/60000);if(m<1)return"just now";if(m<60)return m+"m ago";var h=Math.floor(m/60);if(h<24)return h+"h "+(m%60)+"m ago";return Math.round(h/24)+"d ago"}
  document.querySelectorAll("time.ago").forEach(function(t){var d=new Date(t.getAttribute("datetime"));if(!isNaN(d)){t.textContent=ago(d);t.title=d.toLocaleString()}});
  var u=document.getElementById("upd"),d=new Date(u.getAttribute("datetime"));
  if(!isNaN(d))u.textContent=d.toLocaleString([], {month:"short",day:"numeric",hour:"numeric",minute:"2-digit",timeZoneName:"short"});
  var fb=document.querySelectorAll("#filters button");
  fb.forEach(function(b){b.onclick=function(){fb.forEach(function(x){x.setAttribute("aria-pressed",x===b)});var f=b.dataset.f;
    document.querySelectorAll("#reported .rep").forEach(function(c){c.hidden=f!=="all"&&c.dataset.cat!==f})}});
  setTimeout(function(){location.reload()},5*60*1000);
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "_site")
