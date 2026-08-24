"""Is the 'routine numeric wire boilerplate' class one embedding neighbourhood, or five?

PRE-REGISTERED before any embedding was computed. Thresholds below are fixed;
no result may redefine them afterwards.

T1 cohesion/separation of the 5 families (silhouette over the 5-family partition)
    silhouette >= 0.10  -> families are DISTINCT clusters -> the class is NOT one
                           neighbourhood -> a lottery-seeded centroid cannot express it
    silhouette <  0.10  -> one blob -> embedding CAN express the class
T2 production test: centroid from the 2 REAL lottery rejects, rank all 財經 titles.
    precision@260 against the 5-family class.
    >= 0.60 -> the proposed mechanism would work
    <  0.60 -> it would not
T3 control: same centroid, precision@50 against the LOTTERY family alone.
    >= 0.80 -> embeddings are fine for a topic class; any T2 failure is genre-specific
T4 the decisive one: within the 台股 topic, can cosine separate ANALYSIS (wanted)
    from BOILERPLATE (unwanted)? Compare mean cosine(analysis, boilerplate-centroid)
    against mean cosine(boilerplate, boilerplate-centroid).
    gap < 0.05 -> topic similarity CANNOT separate genre inside a topic
"""

import glob
import json
import os
import pathlib
import re
import sys
import urllib.request

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[2]
KEY = os.environ.get("GEMINI_API_KEY") or next(
    l.split("=", 1)[1].strip()
    for l in open(ROOT / ".env", encoding="utf-8")
    if l.startswith("GEMINI_API_KEY=")
)
MODEL = "gemini-embedding-001"
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:batchEmbedContents?key={KEY}"

FAMS = {
    "lottery": r"第\d+期.*(開獎|中獎)",
    "taiex": r"(台股|加權指數).*(收|開|漲|跌|點)",
    "twd": r"新台幣.*(升|貶|收)",
    "institutional": r"三大法人",
    "futures": r"期指",
}
ANALYSIS_MARK = ("投顧", "分析", "專家", "法人看", "解讀", "原因", "為何", "觀察")


def load():
    rows = []
    for f in glob.glob(str(ROOT / "agent-vault/articles/*.json")):
        d = json.load(open(f, encoding="utf-8"))
        rows += d if isinstance(d, list) else d.get("articles", [])
    fin, seen = [], set()
    for r in rows:
        if r.get("source_name") == "中央社即時新聞 財經新聞" and r["url"] not in seen:
            seen.add(r["url"])
            fin.append(r)
    return fin


def embed(texts):
    out = []
    for i in range(0, len(texts), 100):
        chunk = texts[i : i + 100]
        body = json.dumps(
            {
                "requests": [
                    {"model": f"models/{MODEL}", "content": {"parts": [{"text": t}]}}
                    for t in chunk
                ]
            }
        ).encode()
        req = urllib.request.Request(URL, body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            d = json.loads(resp.read())
        out += [e["values"] for e in d["embeddings"]]
        print(f"  embedded {len(out)}/{len(texts)}", file=sys.stderr)
    v = np.array(out, dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


fin = load()
titles = [r["title"] for r in fin]
fam_of = {}
for name, p in FAMS.items():
    for idx, t in enumerate(titles):
        if re.search(p, t) and idx not in fam_of:
            fam_of[idx] = name
print(f"財經 titles: {len(titles)}   class members: {len(fam_of)}")
print("  by family:", {n: sum(1 for v in fam_of.values() if v == n) for n in FAMS})

E = embed(titles)

# --- T1 silhouette over the 5-family partition -------------------------------
idxs = sorted(fam_of)
X = E[idxs]
lab = np.array([fam_of[i] for i in idxs])
S = X @ X.T
sil = []
for i in range(len(idxs)):
    same = lab == lab[i]
    same[i] = False
    if same.sum() == 0:
        continue
    a = 1 - S[i][same].mean()
    b = min((1 - S[i][lab == o].mean()) for o in set(lab) if o != lab[i])
    sil.append((b - a) / max(a, b))
sil = float(np.mean(sil))

# --- T2 / T3 production centroid from the 2 real rejects ---------------------
REJECTS = [
    "https://www.cna.com.tw/news/ahel/202608080203.aspx",
    "https://www.cna.com.tw/news/ahel/202608080193.aspx",
]
seed_idx = [i for i, r in enumerate(fin) if r["url"] in REJECTS]
print(f"seed titles ({len(seed_idx)}):", [titles[i] for i in seed_idx])
cen = E[seed_idx].mean(0)
cen /= np.linalg.norm(cen)
order = np.argsort(-(E @ cen))
cls = set(idxs)
lot = {i for i, f in fam_of.items() if f == "lottery"}
p260 = sum(1 for i in order[: len(cls)] if i in cls) / len(cls)
p50 = sum(1 for i in order[:50] if i in lot) / 50

# --- T4 genre inside a topic -------------------------------------------------
tw = [i for i, f in fam_of.items() if f == "taiex"]
ana = [i for i in tw if any(k in titles[i] for k in ANALYSIS_MARK)]
boil = [i for i in tw if i not in ana]
bc = E[boil].mean(0)
bc /= np.linalg.norm(bc)
gap = float((E[boil] @ bc).mean() - (E[ana] @ bc).mean())

print("\n" + "=" * 62)
print(f"T1 silhouette (5-family)      = {sil:+.4f}   [>=0.10 -> five clusters]")
print(f"T2 precision@{len(cls)} vs class    = {p260:.3f}      [>=0.60 -> mechanism works]")
print(f"T3 precision@50 vs lottery    = {p50:.3f}      [>=0.80 -> fine for a topic class]")
print(f"T4 boilerplate-vs-analysis gap= {gap:+.4f}   [<0.05 -> cannot separate genre]")
print(f"     analysis n={len(ana)}  boilerplate n={len(boil)}")
print("=" * 62)
print("\nwhat the lottery-seeded centroid actually retrieves, ranks 1-15:")
for i in order[:15]:
    print(f"   {fam_of.get(i, '-'):14} {titles[i][:46]}")
print("\nranks 46-60 (where precision decays):")
for i in order[45:60]:
    print(f"   {fam_of.get(i, '-'):14} {titles[i][:46]}")
