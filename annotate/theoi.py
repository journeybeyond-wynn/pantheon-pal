"""Check every family link in the trees against the Theoi Project (theoi.com), and fix the ones it shows are wrong.

    python -m annotate.theoi fetch              read Theoi's family listings for every matched character (free, slow:
                                                about one page a second; cached in data/theoi.json, so a rerun only
                                                fetches pages it has not read)
    python -m annotate.theoi check <slug>       compare the book's tree with them and print the report; writes nothing
    python -m annotate.theoi check <slug> --yes ... and save the fixes to data/<slug>/curated/theoi.json, then
                                                rebuild the tree (python -m annotate.merge reads that file)
    python -m annotate.theoi check --all --yes  every book

Theoi's page on a god, creature or hero has a "Family of ..." block: each parent, child and consort, with the
ancient passages that name them ("Zephyros & Podarge (Homer Iliad 16.148, Quintus Smyrnaeus 3.743)"). Characters
are found through Wikidata, which records each figure's Theoi page (property P3545). Only the relationships and
their citations are kept, never Theoi's text. For each family link of a tree:
  - confirmed: Theoi lists it. The link is marked with the citations; those from books in this library link to the
    passage.
  - wrong: Theoi lists the child's parents and this one is not among them, nor named anywhere on the child's page.
    The link is removed, and the parents Theoi cites to this same book are added in its place.
  - unclear: not among the listed parents, but named on the page (often a variant told in its encyclopedia text).
    Left as it is.
  - not checked: no Theoi page, or no parents listed. Left as it is.
Spouse and sibling links are only ever confirmed, never removed: Theoi's lists of consorts are not complete.
"""
import argparse
import html as htmlmod
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

from annotate.merge import ROOT, merge

SITE = "https://www.theoi.com/"
USER_AGENT = "LitAnalyzer/0.1 (personal reading companion; checks family trees against cited sources)"
PAUSE = 1.2                 # seconds between page requests
CACHE = ROOT / "data" / "theoi.json"
KINDS = {"PARENT": "parents", "OFFSPRING": "offspring", "CHILDREN": "offspring", "SONS": "offspring",
         "DAUGHTERS": "offspring", "CONSORT": "consorts", "LOVER": "consorts", "WIFE": "consorts", "WIVES": "consorts",
         "HUSBAND": "consorts", "SIBLING": "siblings", "BROTHER": "siblings", "SISTER": "siblings"}


# ---------- reading Theoi ----------

def get(url: str, accept: str = "text/html") -> str | None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    for attempt in range(4):
        try:
            return urllib.request.urlopen(request, timeout=60).read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code != 429 or attempt == 3:
                raise
            time.sleep(20 * (attempt + 1))
    return None


def theoi_ids(qids: list[str]) -> dict[str, list[str]]:
    """The Theoi page of each Wikidata entry that has one: {qid: [theoi id such as "Olympios/Zeus"]}. Asked of
    Wikidata's ordinary API, 50 entries at a time (its query service is often rate-limited)."""
    found = {}
    for start in range(0, len(qids), 50):
        answer = json.loads(get("https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode({
            "action": "wbgetentities", "ids": "|".join(qids[start:start + 50]), "props": "claims", "format": "json"}),
            "application/json"))
        for q, entity in answer.get("entities", {}).items():
            ids = [c["mainsnak"]["datavalue"]["value"] for c in entity.get("claims", {}).get("P3545", [])
                   if c["mainsnak"].get("datavalue")]
            if ids:
                found[q] = ids
        time.sleep(0.5)
    return found


def page_id(href: str, base: str) -> str:
    """"../Titan/AnemosZephyros.html#x" read on Olympios/Zeus -> "Titan/AnemosZephyros"."""
    url = urllib.parse.urljoin(SITE + base + ".html", href)
    path = urllib.parse.urlparse(url).path.lstrip("/")
    return re.sub(r"\.html?$", "", path)


def strip(fragment: str) -> str:
    return htmlmod.unescape(re.sub(r"<[^>]+>", " ", fragment))


CAPS = re.compile(r"\b[A-Z][A-Z'’\-]{2,}(?:\s+[A-Z][A-Z'’\-]{2,})*\b")


def entries(fragment: str, base: str) -> list[dict]:
    """The entries of one list ("[1] ZEUS & HERA (Homer Iliad 1.578)"): the people named and the passages cited.
    A partner named in "(by ...)" is kept apart from the people the entry is about."""
    found = []
    for part in re.split(r"<br\s*/?>|</p>|<p[^>]*>|</li>|<li[^>]*>", fragment):
        cites = []
        for span in re.findall(r'<span class="family-refs">(.*?)</span>', part, flags=re.S):
            work = ""
            for c in re.split(r"[,;&]", " ".join(strip(span).split()).strip(" ()")):
                c = c.strip()
                if not c:
                    continue
                if c[0].isdigit() and work:          # "Apollodorus 3.141, 3.146": the second is Apollodorus too
                    c = f"{work} {c}"
                work = re.sub(r"\s*[\d.]+.*$", "", c) or work
                cites.append(c)
        part = re.sub(r'<span class="family-refs">.*?</span>', " ", part, flags=re.S)
        partners = re.findall(r"\(by ([^)]*)\)", part)
        part = re.sub(r"\(by [^)]*\)", " ", part)
        people = [{"name": " ".join(strip(text).split()), "id": page_id(href, base)}
                  for href, text in re.findall(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', part, flags=re.S)
                  if strip(text).strip() and strip(text).strip() == strip(text).strip().upper()]     # names, not "See Family of ..."
        named = {p["name"] for p in people}
        bare = re.sub(r"<a\s[^>]*>.*?</a>", " ", part, flags=re.S)
        people += [{"name": n, "id": ""} for n in CAPS.findall(strip(bare)) if n not in named
                   and n not in {"SEE", "AND", "THE", "OR", "OF", "BY"}]
        if people:
            found.append({"people": people, "cites": cites,
                          "partners": [p.strip() for x in partners for p in CAPS.findall(strip(x))]})
    return found


def read_page(theoi_id: str) -> dict | None:
    """One Theoi page's family block: {parents, offspring, consorts, siblings: [entry]} plus `named`, every name the
    page mentions anywhere (in capitals or as a link), for telling a wrong link from a variant told in its text."""
    html = get(SITE + theoi_id + ".html")
    if html is None:
        return None
    found = {"parents": [], "offspring": [], "consorts": [], "siblings": []}
    start = html.find('id="Family"')
    if start >= 0:
        block = html[start:html.find("InstanceEndEditable", start)]
        parts = re.split(r"<h3[^>]*>(.*?)</h3>", block, flags=re.S)
        for heading, body in zip(parts[1::2], parts[2::2]):
            kind = next((v for k, v in KINDS.items() if k in strip(heading).upper()), None)
            if kind:
                found[kind] += entries(body, theoi_id)
                # "See Family of Hephaistos": the full list of children is on a page of its own.
                if kind == "offspring":
                    for href in re.findall(r'<a\s+href="([^"]*Family[^"]*)"', body):
                        found["family_page"] = page_id(href, theoi_id)
    text = strip(re.sub(r"<script.*?</script>", " ", html, flags=re.S))
    found["named"] = sorted({skeleton(n) for n in CAPS.findall(text)} | {skeleton(w) for w in re.findall(r"\b[A-Z][a-z]{2,}\b", text)}
                            | {page_id(h, theoi_id) for h in re.findall(r'<a\s+href="([^"#]+\.html)', html)})
    return found


def read_family_page(theoi_id: str) -> list[dict]:
    """A god's own "Family" page: the entries under its headings about children."""
    html = get(SITE + theoi_id + ".html")
    if html is None:
        return []
    found = []
    parts = re.split(r"<h[23][^>]*>(.*?)</h[23]>", html, flags=re.S)
    for heading, body in zip(parts[1::2], parts[2::2]):
        if re.search(r"OFFSPRING|CHILDREN|SONS|DAUGHTERS", strip(heading).upper()):
            found += entries(body, theoi_id)
    return found


# ---------- names ----------

def skeleton(name: str) -> str:
    """A spelling-blind form of a name, so Theoi's ZEPHYROS meets Butler's Zephyrus and PEIRITHOOS meets Pirithous."""
    s = unicodedata.normalize("NFKD", name.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("æ", "ae").replace("œ", "oe")
    s = re.sub(r"[^a-z ]", "", s)
    for a, b in (("kh", "ch"), ("k", "c"), ("ph", "f"), ("ae", "e"), ("oe", "e"), ("ai", "e"), ("oi", "e"),
                 ("ei", "i"), ("ou", "u"), ("y", "i"), ("j", "i")):
        s = s.replace(a, b)
    s = re.sub(r"(.)\1", r"\1", s)
    words = [re.sub(r"(eus|eos|os|us|on|um|es|as|e|a)$", "", w) or w for w in s.split()]
    return " ".join(words)


# ---------- citations in this library ----------

WORKS = [  # (pattern on Theoi's citation, book of this library, how to find the section)
    (r"^Homer,? Iliad\s+(\d+)", "iliad", "book"),
    (r"^Homer,? Odyssey\s+(\d+)", "odyssey", "book"),
    (r"^(?:Virgil|Vergil),? Aeneid\s+(\d+)", "aeneid", "book"),
    (r"^Ovid,? Metamorphoses\s+(\d+)", "metamorphoses", "book"),
    (r"^Apollonius Rhodius\s+(\d+)", "argonautica", "book"),
    (r"^Quintus Smyrnaeus\s+(\d+)", "fall-of-troy", "book"),
    (r"^Statius,? Thebaid\s+(\d+)", "statius-thebaid", "book"),
    (r"^Apollodorus\s+(?:E\.?\s*)?(\d+)", "apollodorus", "book"),
    (r"^Pausanias\s+(\d+)\.(\d+)", "pausanias", "chapter"),
    (r"^Hesiod,? Theogony\s+(\d+)", "hesiod", "Theogony"),
    (r"^Hesiod,? Works and Days\s+(\d+)", "hesiod", "Works and Days"),
    (r"^Hesiod,? Shield of Heracles\s+(\d+)", "hesiod", "Shield"),
    (r"^Hesiod,? Catalogues? of Women", "hesiod", "Catalogues"),
    (r"^Homeric Hymn\s+(\d+)\.?(\d+)?", "hesiod", "hymn"),
    (r"^Pindar,? (Olympian|Pythian|Nemean|Isthmian)\D*(\d+)", "pindar-odes", "ode"),
    (r"^Aeschylus,? (Agamemnon|Libation|Eumenides|Prometheus|Persians|Seven|Suppliant)", "aeschylus", "play"),
    (r"^Euripides,? Bacchae", "bacchae", "first"),
    (r"^Sophocles,? Oedipus (?:Tyrannus|the King|Rex)", "oedipus-the-king", "first"),
]
ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII", 8: "VIII", 9: "IX", 10: "X", 11: "XI", 12: "XII",
         13: "XIII", 14: "XIV", 15: "XV", 16: "XVI", 17: "XVII", 18: "XVIII", 19: "XIX", 20: "XX", 21: "XXI",
         22: "XXII", 23: "XXIII", 24: "XXIV", 25: "XXV", 26: "XXVI", 27: "XXVII", 28: "XXVIII", 29: "XXIX",
         30: "XXX", 31: "XXXI", 32: "XXXII", 33: "XXXIII"}
_body: dict[str, list[dict]] = {}


def body_of(slug: str) -> list[dict]:
    if slug not in _body:
        file = ROOT / "data" / slug / "output" / "sections.json"
        _body[slug] = [s for s in json.loads(file.read_text(encoding="utf-8"))["sections"] if s["category"] == "body"] \
            if file.exists() else []
    return _body[slug]


def lines_of(s: dict) -> tuple[int, int] | None:
    m = re.match(r"Lines (\d+)-(\d+)", s["path"][-1])
    return (int(m.group(1)), int(m.group(2))) if m else None


def place(cite: str) -> dict | None:
    """Where a citation points in this library: {"book", "section"}, or None for a work the library does not have."""
    for pattern, slug, how in WORKS:
        m = re.match(pattern, cite, flags=re.I)
        if not m:
            continue
        body = body_of(slug)
        if not body:
            return None
        pick = None
        if how == "book":
            pick = next((s for s in body if s["path"][0] == f"Book {int(m.group(1))}"), None)
        elif how == "chapter":
            pick = next((s for s in body if s["path"][0] == f"Book {int(m.group(1))}" and s["path"][-1] == f"Chapter {int(m.group(2))}"), None) \
                or next((s for s in body if s["path"][0] == f"Book {int(m.group(1))}"), None)
        elif how in ("Theogony", "Works and Days", "Shield", "Catalogues"):
            work = [s for s in body if how.lower() in s["path"][0].lower()]
            line = int(m.group(1)) if m.groups() else None
            pick = next((s for s in work if line and lines_of(s) and lines_of(s)[0] <= line <= lines_of(s)[1]), work[0] if work else None)
        elif how == "hymn":
            number, line = int(m.group(1)), int(m.group(2)) if m.group(2) else None
            work = [s for s in body if s["path"][0].startswith(ROMAN.get(number, "?") + ".")]
            pick = next((s for s in work if line and lines_of(s) and lines_of(s)[0] <= line <= lines_of(s)[1]), work[0] if work else None)
        elif how == "ode":
            kind, number = m.group(1).lower(), int(m.group(2))
            pick = next((s for s in body if kind in s["path"][0].lower() and s["path"][-1].startswith(ROMAN.get(number, "?") + ".")), None)
        elif how == "play":
            pick = next((s for s in body if m.group(1).lower() in s["path"][0].lower()), None)
        else:
            pick = body[0]
        return {"book": slug, "section": pick["id"]} if pick else {"book": slug, "section": body[0]["id"]}
    return None


# ---------- the check ----------

# Links settled by hand after review: {(parent, child): "remove" or "keep"}.
_decisions_file = ROOT / "data" / "theoi_decisions.json"
_decided = json.loads(_decisions_file.read_text(encoding="utf-8")) if _decisions_file.exists() else {}
REMOVE = {tuple(pair) for pair in _decided.get("remove", [])}                       # wrong in every book
KEEP = {(a, b, book) for a, b, books in _decided.get("keep", []) for book in books}   # right in those books only

# English names a translation gives Greek gods and personifications, with the Greek names Theoi uses for them.
ENGLISH = {"night": "nyx", "earth": "gaia ge", "heaven": "ouranos", "sky": "ouranos", "dawn": "eos", "sun": "helios",
           "moon": "selene", "ocean": "okeanos", "strife": "eris", "love": "eros", "death": "thanatos", "sleep": "hypnos",
           "darkness": "erebos", "day": "hemera", "time": "khronos", "fate": "moira", "fates": "moirai", "justice": "dike",
           "victory": "nike", "peace": "eirene", "folly": "ate", "rumour": "pheme", "fame": "pheme", "fear": "phobos",
           "terror": "deimos", "panic": "phobos", "youth": "hebe", "graces": "kharites", "furies": "erinyes",
           "hours": "horai", "seasons": "horai", "muses": "mousai", "rainbow": "iris", "memory": "mnemosyne",
           "north wind": "boreas", "west wind": "zephyros", "south wind": "notos", "east wind": "euros",
           "sea": "pontos", "underworld": "haides", "chaos": "khaos", "nemesis": "nemesis", "old age": "geras",
           "pluto": "plouton", "dis": "haides", "proserpine": "persephone", "saturn": "kronos", "ops": "rhea"}

def check(slug: str, pages: dict, ids: dict[str, list[str]]) -> dict:
    tree = merge(slug, theoi=False)            # the tree as the book and the other steps make it, before these fixes
    people = {p["id"]: p for p in tree["people"]}
    by_qid = defaultdict(set)
    by_skel = defaultdict(set)
    # Wikidata's "said to be the same as" joins a Roman god to the Greek one Theoi writes about (Aurora and Eos).
    wd = ROOT / "data" / slug / "output" / "wikidata.json"
    entities = json.loads(wd.read_text(encoding="utf-8")).get("entities", {}) if wd.exists() else {}
    for p in tree["people"]:
        for q in p.get("wikidata", []):
            for same in [q, *entities.get(q, {}).get("same", [])]:
                by_qid[same].add(p["id"])
        for n in [p["name"], *p["aliases"]]:
            by_skel[skeleton(n)].add(p["id"])
            for greek in ENGLISH.get(n.lower(), "").split():
                by_skel[skeleton(greek)].add(p["id"])
    qid_of = {tid: q for q, tids in ids.items() for tid in tids}

    def means(person: dict, pid: str) -> bool:
        """Can an entry's name mean this person? Generous, for confirming a link: a name two people share (two
        Clymenes) counts for both, as does the page the name links to."""
        page = re.sub(r"(Family|Loves|Myths)\d*$", "", person["id"])
        return pid in by_skel.get(skeleton(person["name"]), set()) or (page and pid in by_qid.get(qid_of.get(page, ""), set()))

    def who(person: dict) -> set[str]:
        """The one person of this tree an entry's name means, for adding a parent: only a name that fits a single
        person (a link to a page shared by a group, such as the Kouretes', is not enough)."""
        exact = {p["id"] for p in tree["people"] if person["name"].lower() in {n.lower() for n in [p["name"], *p["aliases"]]}}
        hit = exact if len(exact) == 1 else by_skel.get(skeleton(person["name"]), set())     # Podarge, not Podargus
        return hit if len(hit) == 1 else set()

    def family(pid: str) -> dict:
        """Theoi's family listing for a person of the tree, from every Theoi page their Wikidata entries have."""
        merged = {"parents": [], "offspring": [], "consorts": [], "siblings": [], "named": set(), "pages": []}
        for q in people[pid].get("wikidata", []):
            for tid in ids.get(q, []):
                page = pages.get(tid)
                if not page:
                    continue
                merged["pages"].append(tid)
                for k in ("parents", "offspring", "consorts", "siblings"):
                    merged[k] += page[k]
                merged["offspring"] += pages.get(page.get("family_page", ""), {}).get("offspring", [])
                merged["named"] |= set(page["named"])
        return merged

    cache = {}
    fam = lambda pid: cache.setdefault(pid, family(pid))

    def listed_parent(a: dict, b: dict) -> bool:
        fa, fb = fam(a["id"]), fam(b["id"])
        return any(means(p, a["id"]) for x in fb["parents"] for p in x["people"]) or \
            any(means(p, b["id"]) for x in fa["offspring"] for p in x["people"])

    # A child the book itself (or the outside step) gives a parent Theoi does not list follows another tradition,
    # or is a namesake of the figure Theoi writes about (Hesiod's Oceanid Calypso, not Homer's daughter of Atlas):
    # none of that child's parents is removed.
    other_tradition = {e["b"] for e in tree["relationships"] if e["relation"] == "parent_of" and e.get("note") != "Wikidata"
                       and fam(e["b"])["parents"] and not listed_parent(people[e["a"]], people[e["b"]])}
    report = {"confirmed": [], "wrong": [], "review": [], "unclear": [], "unchecked": 0, "added": [], "kept": 0}
    remove, add = [], []
    for e in tree["relationships"]:
        if e["relation"] not in ("parent_of", "spouse_of", "sibling_of"):
            continue
        a, b = people[e["a"]], people[e["b"]]
        fa, fb = fam(a["id"]), fam(b["id"])
        cites = []
        if e["relation"] == "parent_of":
            listed = [x for x in fb["parents"] if any(means(p, a["id"]) for p in x["people"])]
            listed += [x for x in fa["offspring"] if any(means(p, b["id"]) for p in x["people"])]
        else:
            kind = "consorts" if e["relation"] == "spouse_of" else "siblings"
            listed = [x for x in fb[kind] if any(means(p, a["id"]) for p in x["people"])]
            listed += [x for x in fa[kind] if any(means(p, b["id"]) for p in x["people"])]
        for x in listed:
            cites += [c for c in x["cites"] if c not in cites]
        label = [a["name"], e["relation"], b["name"]]
        if (a["name"], b["name"]) in REMOVE:           # judged wrong by hand: removed whether or not Theoi has a page
            report["wrong"].append(label + [e["source"], "decided after review", [], []])
            remove.append(label)
            continue
        if listed:
            report["confirmed"].append(label + [cites])
            continue
        if e["relation"] != "parent_of" or not fb["parents"]:
            report["unchecked"] += 1
            continue
        mentioned = {skeleton(n) for n in [a["name"], *a["aliases"]]} | \
                    {tid for q in a.get("wikidata", []) for tid in ids.get(q, [])}
        # A link the book states may be a variant told in the page's text; a link from Wikidata or from outside the
        # book has no such claim, and a great god (Zeus) is named on almost every page, so for those only the
        # listed parents count.
        if e["source"] == "text" and mentioned & fb["named"]:
            report["unclear"].append(label + [e["source"]])
            continue
        theirs = sorted({p["name"] for x in fb["parents"] for p in x["people"]})
        # A link the book's own text states is only listed for review: the Theoi page may be about a namesake (the
        # Iliad's Ascalaphus, son of Ares, is not the underworld Ascalaphus, son of Acheron). A link added from
        # Wikidata or from outside the book is removed.
        # Only a link the Wikidata step added is removed. A link from the outside-the-book step or added by hand
        # usually says why ("Hesiod makes her an Oceanid"), and Theoi's page may be about a namesake (Homer's
        # Calypso, daughter of Atlas, is not Hesiod's Oceanid Calypso): those are listed for review too.
        # And Theoi's word counts only where it rests on a book of this library: a child whose listed parents cite none
        # of them (Pasithea's rest on Nonnus) is listed for review, not changed.
        from_library = any(place(c) for x in fb["parents"] for c in x["cites"])
        decided = "remove" if (a["name"], b["name"]) in REMOVE else "keep" if (a["name"], b["name"], slug) in KEEP else None
        if decided == "keep":
            report["kept"] += 1
            continue
        if decided != "remove" and (e["source"] == "text" or e.get("note") != "Wikidata" or b["id"] in other_tradition
                                    or not from_library):
            report["review"].append(label + [e["source"], e.get("note", ""), theirs, fb["pages"]])
            continue
        report["wrong"].append(label + [e["source"], e.get("note", ""), theirs, fb["pages"]])
        remove.append(label)
    # In place of a wrong parent, the parents Theoi cites to this same book, if they are in its tree.
    for a_name, _, b_name in remove:
        b = next(p for p in tree["people"] if p["name"] == b_name)
        for x in fam(b["id"])["parents"]:
            here = [c for c in x["cites"] if (place(c) or {}).get("book") == slug]
            if not here:
                continue
            for person in x["people"]:
                for pid in who(person):
                    entry = [people[pid]["name"], "parent_of", b_name, here[0]]
                    if pid != b["id"] and entry[:3] not in [r[:3] for r in add] and entry[:3] not in remove:
                        add.append(entry)
    report["added"] = add
    places = {c: place(c) for row in report["confirmed"] for c in row[3]} | {r[3]: place(r[3]) for r in add}
    return {"source": "Theoi Project (theoi.com), family listings with their ancient citations",
            "remove": remove, "add": add, "confirmed": report["confirmed"], "places": {c: p for c, p in places.items() if p},
            "report": {"links": len(report["confirmed"]) + len(report["wrong"]) + len(report["review"]) + len(report["unclear"])
                       + report["unchecked"],
                       "confirmed": len(report["confirmed"]), "wrong": report["wrong"], "review": report["review"],
                       "unclear": report["unclear"],
                       "unchecked": report["unchecked"], "added": add, "kept_by_decision": report["kept"]}}


# ---------- commands ----------

def fetch() -> None:
    saved = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {"ids": {}, "pages": {}}
    qids = sorted({q for tree_file in (ROOT / "data").glob("*/output/tree.json")
                   for p in json.loads(tree_file.read_text(encoding="utf-8"))["people"] for q in p.get("wikidata", [])})
    print(f"Asking Wikidata which of {len(qids)} matched characters have a Theoi page ...", flush=True)
    saved["ids"] = theoi_ids(qids)
    wanted = {tid for q in qids for tid in saved["ids"].get(q, [])}
    todo = sorted(t for t in wanted if t not in saved["pages"])
    print(f"{len(wanted)} Theoi pages for the characters of this library; {len(todo)} not read yet.", flush=True)
    for n, tid in enumerate(todo, 1):
        page = read_page(tid)
        time.sleep(PAUSE)
        if page and page.get("family_page") and page["family_page"] not in saved["pages"]:
            saved["pages"][page["family_page"]] = {"offspring": read_family_page(page["family_page"]), "parents": [],
                                                   "consorts": [], "siblings": [], "named": []}
            time.sleep(PAUSE)
        saved["pages"][tid] = page or {"parents": [], "offspring": [], "consorts": [], "siblings": [], "named": [], "missing": True}
        if n % 25 == 0 or n == len(todo):
            CACHE.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
            print(f"  read {n} of {len(todo)}", flush=True)
    CACHE.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["fetch", "check"])
    ap.add_argument("slug", nargs="?")
    ap.add_argument("--all", action="store_true", help="every book with a family tree")
    ap.add_argument("--yes", action="store_true", help="save the fixes and rebuild the tree")
    args = ap.parse_args()
    if args.command == "fetch":
        return fetch()
    saved = json.loads(CACHE.read_text(encoding="utf-8"))
    slugs = sorted(f.parent.parent.name for f in (ROOT / "data").glob("*/output/tree.json")) if args.all else [args.slug]
    for slug in slugs:
        result = check(slug, saved["pages"], saved["ids"])
        r = result["report"]
        print(f"\n{slug}: {r['links']} family links; {r['confirmed']} confirmed by Theoi, {len(r['wrong'])} wrong (removed), "
              f"{len(r['review'])} from the book or the outside step that Theoi does not list (for review, kept), "
              f"{len(r['unclear'])} unclear (named on the page but not as a parent), {r['unchecked']} not checked; "
              f"{len(r['added'])} parents added in place of wrong ones")
        for a, rel, b, source, note, theirs, pages in r["wrong"]:
            print(f"   wrong: {a} {rel.replace('_', ' ')} {b}  [{'from ' + note if note else source}]  Theoi: {', '.join(theirs)}")
        for a, rel, b, source, note, theirs, pages in r["review"]:
            why = "the book says so" if source == "text" else f"from outside the book: {note}" if note else "from outside the book"
            print(f"   review: {a} {rel.replace('_', ' ')} {b}  [{why}]  Theoi ({', '.join(pages)}): {', '.join(theirs)}")
        for row in r["added"]:
            print(f"   added: {row[0]} parent of {row[2]}  ({row[3]})")
        if args.yes:
            target = ROOT / "data" / slug / "curated" / "theoi.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
            tree = merge(slug)
            (ROOT / "data" / slug / "output" / "tree.json").write_text(json.dumps(tree, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"   saved {target.relative_to(ROOT)} and rebuilt the tree")


if __name__ == "__main__":
    main()
