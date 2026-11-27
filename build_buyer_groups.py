"""
Resolve "who is the buyer": link a person to the funds/firms that file alongside them.

Evidence, strongest first (never merge on name alone):
  1. owner CIK       - same CIK = same entity (deterministic)      [v2 / DB mode]
  2. co-filing       - names on the SAME Form 4 report one beneficial-ownership group
  3. fuzzy name      - only produces SUGGESTIONS for human review (Jr/Sr can be father/son)

Usage
  python build_buyer_groups.py preview insider_trades.csv     # works on today's table (no CIKs)
  python build_buyer_groups.py db                             # v2 schema; needs SUPABASE_DB_URL
"""
import os
import re
import sys
from collections import Counter, defaultdict

ENTITY = re.compile(
    r"\b(L\.?P\.?|LLC|L\.L\.C\.?|LTD|LIMITED|INC|CORP|CORPORATION|FUND|FUNDS|CAPITAL|PARTNERS|"
    r"MANAGEMENT|HOLDINGS|TRUST|GROUP|ASSOCIATES|ADVISORS|INVESTMENTS?|VENTURES?|LLP|S\.A\.|"
    r"S\.A R\.L\.|FINANCIAL|BANK|OFFSHORE|MASTER|EQUITY|COMPANY|CO|GP|SPV|FOUNDATION|ESTATE)\b|"
    r"\bFUND\b|\d{2,}",
    re.I)
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}
MARKET_MAKERS = re.compile(r"HRT FINANCIAL|JANE STREET|CITADEL SEC|SUSQUEHANNA|VIRTU|JUMP TRADING", re.I)


def is_entity(name):
    return bool(ENTITY.search(name))


def tokens(name):
    return [t for t in re.sub(r"[^a-z ]", " ", name.lower()).split() if t]


def person_key(name):
    """EDGAR person names are 'Last First Middle [Suffix]'.
    Returns (last+first key, suffix tuple, middle initial or None)."""
    t = tokens(name)
    core = [x for x in t if x not in SUFFIX]
    suffix = tuple(sorted(x for x in t if x in SUFFIX))
    middle = core[2][0] if len(core) >= 3 else None
    return ((" ".join(core[:2]), suffix, middle) if len(core) >= 2 else (None, suffix, middle))


class UnionFind:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def classify(members, weight):
    people = [m for m in members if not is_entity(m)]
    if any(MARKET_MAKERS.search(m) for m in members):
        btype = "market_maker"
    elif len(people) == 1:
        btype = "person"
    elif len(people) > 1:
        btype = "group"          # a firm with several principals: attribute to the FIRM, not one person
    else:
        btype = "institution"
    entities = [m for m in members if is_entity(m)]
    if btype == "person":
        display = people[0]
    elif entities:
        display = max(entities, key=lambda m: weight.get(m, 0))   # name a firm group after the firm
    else:
        display = max(members, key=lambda m: weight.get(m, 0))
    return btype, display, len(people)


def suggestions(names_by_id):
    """Possible same-person matches across DIFFERENT ids, for manual review."""
    buckets = defaultdict(list)
    for i, n in names_by_id.items():
        if not is_entity(n):
            k, suf, mid = person_key(n)
            if k:
                buckets[k].append((i, n, suf, mid))
    out = []
    for k, items in buckets.items():
        for a in range(len(items)):
            for b in range(a + 1, len(items)):
                (ia, na, sa, ma), (ib, nb, sb, mb) = items[a], items[b]
                if ma and mb and ma != mb:      # conflicting middle names/initials: different people
                    continue
                out.append((ia, na, ib, nb, "same_name_diff_suffix" if sa != sb else "same_name"))
    return out


# ---------------------------------------------------------------------------
def preview(csv_path):
    import pandas as pd
    df = pd.read_csv(csv_path)
    df["td"] = df.transaction_date.str[:10]
    df["ek"] = (df.ticker.fillna("?") + "|" + df.td + "|" + df.shares_bought.astype(str) + "|"
                + df.est_value.round(2).astype(str))
    uf = UnionFind()
    for _, g in df.groupby("ek"):
        names = list(g.insider_name.unique())
        for n in names:
            uf.find(n)
        for n in names[1:]:
            uf.union(names[0], n)

    df["grp"] = df.insider_name.map(uf.find)
    rows = []
    for grp, g in df.groupby("grp"):
        members = list(g.insider_name.unique())
        weight = g.insider_name.value_counts().to_dict()
        btype, display, npeople = classify(members, weight)
        ev = g.drop_duplicates("ek")
        rows.append({
            "buyer": display, "type": btype, "n_people": npeople, "n_names": len(members),
            "events": ev.shape[0], "tickers": g.ticker.nunique(), "total_value": ev.est_value.sum(),
            "members": " | ".join(sorted(members)[:12]),
        })
    out = pd.DataFrame(rows).sort_values("events", ascending=False)
    out.to_csv("buyer_groups_preview.csv", index=False)

    sug = suggestions({n: n for n in df.insider_name.unique()})
    group_of = {n: uf.find(n) for n in df.insider_name.unique()}
    sug = [s for s in sug if group_of[s[0]] != group_of[s[2]]]
    pd.DataFrame(sug, columns=["id_a", "name_a", "id_b", "name_b", "reason"]).to_csv(
        "buyer_merge_suggestions_preview.csv", index=False)

    print(f"{df.insider_name.nunique()} names -> {len(out)} buyers "
          f"({(out.n_names > 1).sum()} multi-name groups)")
    print(out.type.value_counts().to_string())
    print(f"{len(sug)} fuzzy merge suggestions for review")
    print(out.head(15)[["buyer", "type", "n_names", "events", "tickers"]].to_string(index=False))


MAX_AUTO_PEOPLE = 3      # more individual people than this in one group = probably a chain merge
MAX_AUTO_SIZE = 30       # more owner IDs than this in one group = hold for review


def plan_groups(by_acc, name_of, confirmed_pairs=()):
    """Pure function (no database): returns (buyers, suggestions).
    buyers      = list of lists of owner CIKs, each list is one buyer
    suggestions = list of (cik_a, name_a, cik_b, name_b, reason) for a human to review
    Guard rails: a co-filed group is only merged automatically when it contains a firm/fund,
    has at most MAX_AUTO_PEOPLE individuals and at most MAX_AUTO_SIZE owners. Anything else is
    split back into single owners and put in the review queue."""
    uf = UnionFind()
    for ciks in by_acc.values():
        for c in ciks:
            uf.find(c)
        for c in ciks[1:]:
            uf.union(ciks[0], c)
    groups = defaultdict(list)
    for cik in name_of:
        groups[uf.find(cik)].append(cik)

    buyers, sugg = [], []
    for ciks in groups.values():
        if len(ciks) == 1:
            buyers.append(ciks)
            continue
        names = [name_of[c] for c in ciks]
        n_people = sum(1 for n in names if not is_entity(n))
        has_firm = n_people < len(names)
        if has_firm and n_people <= MAX_AUTO_PEOPLE and len(ciks) <= MAX_AUTO_SIZE:
            buyers.append(ciks)
        else:
            why = "people_only_cofiled" if not has_firm else "large_or_many_people_group"
            for c in ciks:
                buyers.append([c])
            first = sorted(ciks)[0]
            for c in sorted(ciks)[1:]:
                sugg.append((first, name_of[first], c, name_of[c], why))

    # human-approved merges (status = 'merge' in buyer_merge_suggestions) are applied last
    if confirmed_pairs:
        uf2 = UnionFind()
        for b in buyers:
            for c in b:
                uf2.find(c)
            for c in b[1:]:
                uf2.union(b[0], c)
        for a, b in confirmed_pairs:
            if a in name_of and b in name_of:
                uf2.union(a, b)
        merged = defaultdict(list)
        for c in name_of:
            merged[uf2.find(c)].append(c)
        buyers = list(merged.values())

    group_of = {c: i for i, b in enumerate(buyers) for c in b}
    for ia, na, ib, nb, why in suggestions(name_of):      # same name, different suffix etc.
        if group_of[ia] != group_of[ib]:
            sugg.append((ia, na, ib, nb, why))
    return buyers, sugg


def build_db():
    import psycopg2
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    cur = conn.cursor()
    cur.execute("""SELECT fo.accession_number, fo.owner_cik, fo.owner_name
                   FROM filing_owners fo JOIN sec_filings f USING (accession_number)
                   WHERE f.superseded_by IS NULL""")
    by_acc, name_of, weight = defaultdict(list), {}, Counter()
    for acc, cik, name in cur.fetchall():
        if cik not in by_acc[acc]:
            by_acc[acc].append(cik)
        name_of[cik] = name
        weight[cik] += 1

    cur.execute("SELECT cik_a, cik_b FROM buyer_merge_suggestions WHERE status = 'merge'")
    confirmed = cur.fetchall()
    buyers, sugg = plan_groups(by_acc, name_of, confirmed)

    # Rebuild from scratch each run (members reference buyers, so clear members first).
    # Decisions live in buyer_merge_suggestions.status, so nothing a human decided is lost.
    cur.execute("ALTER TABLE buyers ADD COLUMN IF NOT EXISTS member_names text")
    cur.execute("DELETE FROM buyer_members")
    cur.execute("UPDATE insider_trades SET buyer_id = NULL WHERE buyer_id IS NOT NULL")
    cur.execute("DELETE FROM buyers")
    for ciks in buyers:
        names = [name_of[c] for c in ciks]
        w = {name_of[c]: weight[c] for c in ciks}
        btype, display, npeople = classify(names, w)
        member_names = " | ".join(sorted(set(names)))[:3000]     # lets the site search every name
        cur.execute("INSERT INTO buyers (display_name, buyer_type, n_people, member_names) VALUES (%s,%s,%s,%s) RETURNING buyer_id",
                    (display, btype, npeople, member_names))
        bid = cur.fetchone()[0]
        for c in ciks:
            cur.execute("""INSERT INTO buyer_members (owner_cik, buyer_id, link_method, confidence)
                           VALUES (%s,%s,%s,%s)""",
                        (c, bid, "cofiling" if len(ciks) > 1 else "cik", 0.95 if len(ciks) > 1 else 1.0))
    for ia, na, ib, nb, why in sugg:
        cur.execute("""INSERT INTO buyer_merge_suggestions (cik_a, name_a, cik_b, name_b, reason)
                      VALUES (%s,%s,%s,%s,%s) ON CONFLICT (cik_a, cik_b) DO NOTHING""", (ia, na, ib, nb, why))
    conn.commit()
    multi = sum(1 for b in buyers if len(b) > 1)
    print(f"{len(name_of)} owner CIKs -> {len(buyers)} buyers ({multi} multi-owner groups); "
          f"{len(sugg)} suggestions queued for review.")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "preview":
        preview(sys.argv[2])
    elif len(sys.argv) >= 2 and sys.argv[1] == "db":
        build_db()
    else:
        sys.exit(__doc__)
