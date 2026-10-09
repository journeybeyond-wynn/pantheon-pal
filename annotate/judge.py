"""Have Claude review the family and identity of every character with a Wikipedia page, so fixes come in batches.

    python -m annotate.judge                      what judging would cost (counts the tokens; spends nothing)
    python -m annotate.judge --yes --limit 10     judge the first 10 now, one by one, and save data/judge.json
    python -m annotate.judge --batch --yes        send everyone not yet judged as one Message Batch (half price)
    python -m annotate.judge --collect            fetch a finished batch's answers into data/judge.json

Who is judged: every character a book names who has a Wikipedia page (through their Wikidata match) and at least
one family link. Claude is shown, for one character in one book: their Wikipedia description; every family link
the tree has for them with where it came from (the book's own sentence, quoted; Wikidata; the outside step with its
note; or the other books of the site it was brought from); the Theoi Project's family listing with its ancient
citations; Wikidata's parents and spouses; what a free check flagged; and the standing decisions already made
(data/only_spouses.json, data/theoi_decisions.json), which it must not re-argue. It answers, for each link, right /
variant / namesake / wrong with a fix, and whether the character's own identity (Wikidata match, merged names) is
wrong. A "remove" must point to the sentence or source that contradicts the link; a verdict whose reason gives the
link a pass is dropped. Nothing in the trees changes: the verdicts are for review, and approved fixes go through
the curated files and data/theoi_decisions.json. Saved verdicts are never paid for twice.
"""
import argparse
import json
import re
import sys
from collections import defaultdict

from annotate.run import ROOT, cost, get_client
from annotate.schema import MODEL
from litparse.sentences import split_sentences

EFFORT = "medium"
MAX_TOKENS = 6000
OUT_PER_PERSON = 450          # output tokens per character, for the estimate (the first ten averaged about this)
SAVED = ROOT / "data" / "judge.json"
BATCH = ROOT / "data" / "judge_batch.json"
FAMILY = ("parent_of", "spouse_of", "sibling_of", "lover_of")
MAX_LINKS = 60                # a great god's children beyond this are listed by name only

SYSTEM_PROMPT = """You check one character's family links in a reading companion for Greek and Roman literature.
Each book of the site has its own family tree, read from the book's sentences, filled in from Wikidata and from an
"outside the book" step, and from what the other books of the site state. You are given one character as one book
names them, their Wikipedia description, every family link the tree has for them with where it came from, the
Theoi Project's family listing with its ancient citations, Wikidata's parents and spouses, what an automatic check
flagged, and decisions the site's editor has already made.

For each link decide:
- "right": the book, or the tradition it follows, says so.
- "variant": the book itself tells it as another tradition ("some say ..."); it is right for this book.
- "namesake": the link belongs to someone else of the same name (two Clymenes; Selene taken for Diana).
- "wrong": a misread sentence, a cult title taken for a person, a lover listed as a spouse, a group taken for a
  person, or an unsupported guess.
Fixes: "keep", "remove", "make_lover" (a spouse who was only a lover), "make_spouse" (a lover who was a wife or
husband), "split" (two beings joined under one name: say which relatives belong to the other).

Rules:
- "remove" only when you can name the book's sentence or an ancient source that contradicts the link, or the link
  plainly confuses two beings. A link you merely cannot confirm is "keep".
- A book that tells a rare tradition in its own words is right for that book.
- Half-siblings are siblings: a sibling link with one shared parent is right (Mars and Mercury, through Jupiter).
- The editor's decisions listed below are final. Do not argue with them.
- Name people and sources; never give Wikidata ids.
- Also judge the character's identity: is the Wikidata match this very being, and are any of their other names
  really someone else? Fix "none" when all is well.
Be brief: one short reason each. Only list links whose fix is not "keep", plus none at all if every link is fine.
Return JSON matching the schema."""

SCHEMA = {
    "type": "object",
    "properties": {
        "links": {"type": "array", "items": {"type": "object", "properties": {
            "link": {"type": "string"},
            "verdict": {"type": "string", "enum": ["right", "variant", "namesake", "wrong"]},
            "fix": {"type": "string", "enum": ["keep", "remove", "make_lover", "make_spouse", "split"]},
            "reason": {"type": "string"}},
            "required": ["link", "verdict", "fix", "reason"], "additionalProperties": False}},
        "identity": {"type": "object", "properties": {
            "fix": {"type": "string", "enum": ["none", "wrong_match", "split_names"]},
            "detail": {"type": "string"}},
            "required": ["fix", "detail"], "additionalProperties": False}},
    "required": ["links", "identity"], "additionalProperties": False}

# A reason that gives the link a pass, under a fix that takes it away, is a self-contradiction: dropped.
PASSES = re.compile(r"\b(acceptable|is right|is correct|keep as|fine|attested|confirms?)\b", re.I)


def load_all() -> tuple[dict, dict, dict]:
    trees, ent, leads = {}, {}, {}
    for f in sorted((ROOT / "data").glob("*/output/tree.json")):
        slug = f.parent.parent.name
        trees[slug] = json.loads(f.read_text(encoding="utf-8"))
        wd = f.parent / "wikidata.json"
        if wd.exists():
            ent.update(json.loads(wd.read_text(encoding="utf-8"))["entities"])
        about = f.parent / "about.json"
        if about.exists():
            for q, c in json.loads(about.read_text(encoding="utf-8")).get("characters", {}).items():
                if c.get("lead"):
                    leads[q] = c["lead"]
    return trees, ent, leads


def flags_for(p: dict, t: dict, byid: dict, ent: dict) -> list[str]:
    q, flags = p["wikidata"], []
    same = lambda x, y: y in ent.get(x, {}).get("same", []) and x in ent.get(y, {}).get("same", [])
    if len([x for x in q if not any(x != y and same(x, y) for y in q)]) > 1:
        flags.append("matched to several Wikidata entries: " + ", ".join(ent.get(x, {}).get("label", x) for x in q))
    parents = [byid[r["a"]] for r in t["relationships"] if r["relation"] == "parent_of" and r["b"] == p["id"]]
    if len(parents) >= 3:
        flags.append(f"{len(parents)} parents")
    spouses = {r["a"] if r["b"] == p["id"] else r["b"] for r in t["relationships"]
               if r["relation"] == "spouse_of" and p["id"] in (r["a"], r["b"])}
    if len(spouses) > 1:
        flags.append(f"{len(spouses)} spouses")
    wd_par = {y for x in q for y in ent.get(x, {}).get("father", []) + ent.get(x, {}).get("mother", [])}
    if wd_par and parents and not wd_par & {y for x in parents for y in x["wikidata"]}:
        flags.append("parents differ from Wikidata's")
    return flags


def people(trees: dict, ent: dict, leads: dict) -> list[dict]:
    """Every character a book names with a Wikipedia page and a family link: gods first, then the most named."""
    found = []
    for slug, t in trees.items():
        byid = {p["id"]: p for p in t["people"]}
        linked = {e for r in t["relationships"] if r["relation"] in FAMILY for e in (r["a"], r["b"])}
        for p in t["people"]:
            if p.get("from_library") or not p["section_count"] or p["id"] not in linked:
                continue
            if not any(q in leads for q in p["wikidata"]):
                continue
            found.append({"book": slug, "id": p["id"], "name": p["name"], "flags": flags_for(p, t, byid, ent),
                          "god": p["kind"] in ("god", "goddess"), "weight": p["section_count"]})
    found.sort(key=lambda s: (not s["god"], -len(s["flags"]), -s["weight"]))
    return found


def standing(name_set: set[str], slug: str) -> list[str]:
    """The editor's decisions that concern these names."""
    out = []
    only = json.loads((ROOT / "data" / "only_spouses.json").read_text(encoding="utf-8")) if (ROOT / "data" / "only_spouses.json").exists() else {}
    if only.get("about"):
        out.append("Only marriages: " + only["about"].split(":", 1)[-1].strip())
    dec_file = ROOT / "data" / "theoi_decisions.json"
    dec = json.loads(dec_file.read_text(encoding="utf-8")) if dec_file.exists() else {}
    for a, b in dec.get("remove", []):
        if a in name_set or b in name_set:
            out.append(f"Wrong everywhere: {a} parent of {b}")
    for a, b, books in dec.get("keep", []):
        if a in name_set or b in name_set:
            out.append(f"Right {'in this book' if slug in books else 'only in ' + ', '.join(books)}: {a} parent of {b}")
    return out


def describe(s: dict, trees: dict, ent: dict, leads: dict, theoi: dict, sentences: dict) -> str:
    t = trees[s["book"]]
    byid = {p["id"]: p for p in t["people"]}
    p = byid[s["id"]]
    label = lambda q: ent.get(q, {}).get("label", q)
    lead = next((leads[q] for q in p["wikidata"] if q in leads), "")
    lines = [f"Book: {s['book']}", f"Character: {p['name']} (also called: {', '.join(p['aliases'][:10]) or 'nothing else'})",
             f"Wikidata match: {', '.join(label(q) for q in p['wikidata'])}", f"Wikipedia: {lead}",
             f"Flagged: {'; '.join(s['flags']) or 'nothing'}", "", "Family links in this book's tree:"]
    links = [r for r in t["relationships"] if r["relation"] in FAMILY and s["id"] in (r["a"], r["b"])]
    names = {p["name"]}
    for k, r in enumerate(links):
        a, b = byid[r["a"]]["name"], byid[r["b"]]["name"]
        names |= {a, b}
        link = f"{a} {r['relation']} {b}"
        if k >= MAX_LINKS:
            lines.append(f"- {link}")
            continue
        if r.get("from_books"):
            where = "brought from other books of the site: " + ", ".join(r["from_books"])
        elif r["source"] == "text":
            if s["book"] not in sentences:
                sentences[s["book"]] = {sec["id"]: [x.text for x in split_sentences(sec["text"])]
                                        for sec in json.loads((ROOT / "data" / s["book"] / "output" / "sections.json")
                                                              .read_text(encoding="utf-8"))["sections"]}
            quotes = []
            for v in r["evidence"][:2]:
                sents = sentences[s["book"]].get(v["section"], [])
                if v["sentence"] < len(sents):
                    quotes.append('"' + sents[v["sentence"]][:280] + '"')
            where = "the book says: " + " / ".join(quotes)
        else:
            where = f"outside the book ({r.get('note') or 'no note'})"
        lines.append(f"- {link} | {where}" + (" | Theoi confirms" if r.get("theoi") else ""))
    for tid in [tid for q in p["wikidata"] for tid in theoi["ids"].get(q, [])]:
        page = theoi["pages"].get(tid, {})
        for kind in ("parents", "consorts"):
            for x in page.get(kind, []):
                lines.append(f"Theoi {kind}: {' & '.join(y['name'] for y in x['people'])} ({'; '.join(x['cites'][:4])})")
    for q in p["wikidata"]:
        e = ent.get(q, {})
        for field in ("father", "mother", "spouse"):
            if e.get(field):
                lines.append(f"Wikidata {field}: {', '.join(label(x) for x in e[field])}")
    decided = standing(names, s["book"])
    if decided:
        lines += ["", "Editor's decisions (final):"] + [f"- {d}" for d in decided]
    return "\n".join(lines)


def params(text: str) -> dict:
    return {"model": MODEL, "max_tokens": MAX_TOKENS, "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": text}],
            "output_config": {"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}}}


def keep_sound(answer: dict) -> dict:
    """Drop verdicts that contradict themselves ("remove: lover is acceptable")."""
    answer["links"] = [x for x in answer["links"] if x["fix"] != "keep"
                       and not (x["verdict"] in ("right", "variant") and x["fix"] == "remove")
                       and not (x["fix"] == "remove" and PASSES.search(x["reason"]))]
    return answer


def save(saved: dict, s: dict, answer: dict) -> None:
    saved[f"{s['book']}/{s['id']}"] = {"book": s["book"], "name": s["name"], "flags": s["flags"], **keep_sound(answer)}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="ask Claude (costs money)")
    ap.add_argument("--batch", action="store_true", help="send as one Message Batch (half price, within 24 hours)")
    ap.add_argument("--collect", action="store_true", help="fetch the answers of the batch sent earlier")
    ap.add_argument("--limit", type=int, default=0, help="at most this many characters")
    args = ap.parse_args()
    trees, ent, leads = load_all()
    saved = json.loads(SAVED.read_text(encoding="utf-8")) if SAVED.exists() else {}
    client = get_client()

    if args.collect:
        sent = json.loads(BATCH.read_text(encoding="utf-8"))
        batch = client.messages.batches.retrieve(sent["id"])
        print(f"Batch {batch.processing_status}: {batch.request_counts}")
        if batch.processing_status != "ended":
            return
        tokens_in = tokens_out = 0
        for item in client.messages.batches.results(sent["id"]):
            s = sent["who"][item.custom_id]
            if item.result.type != "succeeded":
                continue
            m = item.result.message
            tokens_in, tokens_out = tokens_in + m.usage.input_tokens, tokens_out + m.usage.output_tokens
            if m.stop_reason == "end_turn":
                save(saved, s, json.loads(next(b.text for b in m.content if b.type == "text")))
        SAVED.write_text(json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{len(saved)} judged in all. This batch: {tokens_in:,} in, {tokens_out:,} out = "
              f"${cost(tokens_in, tokens_out, batch=True):.2f}")
        return

    theoi = json.loads((ROOT / "data" / "theoi.json").read_text(encoding="utf-8"))
    todo = [s for s in people(trees, ent, leads) if f"{s['book']}/{s['id']}" not in saved]
    if args.limit:
        todo = todo[:args.limit]
    sentences: dict = {}
    texts = [describe(s, trees, ent, leads, theoi, sentences) for s in todo]
    print(f"{len(todo)} characters to judge ({sum(s['god'] for s in todo)} gods); already judged: {len(saved)}.")
    if not args.yes:
        tokens = sum(client.messages.count_tokens(model=MODEL, system=SYSTEM_PROMPT,
                                                  messages=[{"role": "user", "content": x}]).input_tokens for x in texts)
        out = OUT_PER_PERSON * len(todo)
        print(f"{tokens:,} tokens in, about {out:,} out: ${cost(tokens, out, batch=False):.2f} one by one, "
              f"${cost(tokens, out, batch=True):.2f} as a batch. Add --yes (and --batch) to run.")
        return
    if args.batch:
        who = {f"j{k}": s for k, s in enumerate(todo)}
        batch = client.messages.batches.create(requests=[{"custom_id": cid, "params": params(text)}
                                                         for cid, text in zip(who, texts)])
        BATCH.write_text(json.dumps({"id": batch.id, "who": who}, ensure_ascii=False), encoding="utf-8")
        print(f"Sent batch {batch.id} with {len(who)} requests. Collect with: python -m annotate.judge --collect")
        return
    tokens_in = tokens_out = 0
    for s, text in zip(todo, texts):
        r = client.messages.create(**params(text))
        tokens_in, tokens_out = tokens_in + r.usage.input_tokens, tokens_out + r.usage.output_tokens
        if r.stop_reason == "end_turn":
            save(saved, s, json.loads(next(b.text for b in r.content if b.type == "text")))
            SAVED.write_text(json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8")
            v = saved[f"{s['book']}/{s['id']}"]
            print(f"  {s['book']}: {s['name']} — {len(v['links'])} to fix; identity: {v['identity']['fix']}")
    print(f"Spent: {tokens_in:,} in, {tokens_out:,} out = ${cost(tokens_in, tokens_out, batch=False):.2f}")


if __name__ == "__main__":
    main()
