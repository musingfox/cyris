"""Harder test: do the 3 UPVOTES (not templated junk) retrieve anything coherent?
Pre-registered: I will report the top-20 neighbours verbatim and judge coherence
qualitatively. No numeric adopt/reject threshold — n=2 effective clicks for the
accepts, far below any level that could support one."""
import glob, json, os, sys, urllib.request, numpy as np, pathlib
KEY=next(l.split('=',1)[1].strip() for l in open('/Users/nickhuang/workspace/cyris/.env',encoding='utf-8') if l.startswith('GEMINI_API_KEY='))
M="gemini-embedding-001"; URL=f"https://generativelanguage.googleapis.com/v1beta/models/{M}:batchEmbedContents?key={KEY}"
rows=[]
for f in glob.glob('/Users/nickhuang/workspace/cyris/agent-vault/articles/*.json'):
    d=json.load(open(f,encoding='utf-8')); rows += d if isinstance(d,list) else d.get('articles',[])
seen=set(); arts=[]
for r in rows:
    if r['url'] not in seen: seen.add(r['url']); arts.append(r)
titles=[r['title'] for r in arts]
cache=pathlib.Path(f'{os.path.dirname(__file__)}/emb_all.npy')
if cache.exists(): E=np.load(cache)
else:
    out=[]
    for i in range(0,len(titles),100):
        body=json.dumps({"requests":[{"model":f"models/{M}","content":{"parts":[{"text":t}]}} for t in titles[i:i+100]]}).encode()
        with urllib.request.urlopen(urllib.request.Request(URL,body,{"Content-Type":"application/json"}),timeout=180) as rp:
            out += [e["values"] for e in json.loads(rp.read())["embeddings"]]
        print(f"  {len(out)}/{len(titles)}",file=sys.stderr)
    E=np.array(out,dtype=np.float32); E/=np.linalg.norm(E,axis=1,keepdims=True); np.save(cache,E)
print(f"corpus embedded: {E.shape}")
UP=["https://www.cna.com.tw/news/aopl/202608090010.aspx","https://www.cna.com.tw/news/aopl/202608080186.aspx",
    "https://techcrunch.com/2026/08/08/planned-amazon-data-center-could-become-the-biggest-climate-polluter-in-the-u-s/"]
idx={r['url']:i for i,r in enumerate(arts)}
for label, urls in [("兩則中央社 upvote (同一次點擊)", UP[:2]), ("TechCrunch upvote (獨立點擊)", UP[2:])]:
    s=[idx[u] for u in urls if u in idx]
    print(f"\n=== seed: {label} ===")
    for i in s: print(f"    seed: {titles[i][:60]}")
    c=E[s].mean(0); c/=np.linalg.norm(c)
    for r,i in enumerate(np.argsort(-(E@c))[:12]):
        if i in s: continue
        print(f"   {r+1:3}. [{arts[i]['source_name'][:14]:14}] {titles[i][:52]}")
