"""Corrected ground truth. The first run's 'lottery' regex missed 頭獎槓龜 variants,
so T2/T3 were scored against a class definition that is itself wrong — which is
exactly the reproducibility problem (HB-2) showing up inside my own measurement."""
import glob, json, pathlib, re, numpy as np

ROOT=pathlib.Path(__file__).resolve().parents[2]
rows=[]
for f in glob.glob(str(ROOT/'agent-vault/articles/*.json')):
    d=json.load(open(f,encoding='utf-8')); rows += d if isinstance(d,list) else d.get('articles',[])
fin,seen=[],set()
for r in rows:
    if r.get('source_name')=='中央社即時新聞 財經新聞' and r['url'] not in seen:
        seen.add(r['url']); fin.append(r)
titles=[r['title'] for r in fin]
E=np.load(f'{__import__("os").path.dirname(__file__)}/emb.npy')

# corrected: any lottery draw report, however phrased
LOT = re.compile(r'(今彩539|大樂透|威力彩|雙贏彩|[34]星彩|39樂合彩|運彩).*第\d+期|第\d+期.*(開獎|中獎|槓龜)')
lot = {i for i,t in enumerate(titles) if LOT.search(t)}
print(f"lottery under corrected definition: {len(lot)}   (first run's regex found 50)")

REJ={"https://www.cna.com.tw/news/ahel/202608080203.aspx","https://www.cna.com.tw/news/ahel/202608080193.aspx"}
seed=[i for i,r in enumerate(fin) if r['url'] in REJ]
cen=E[seed].mean(0); cen/=np.linalg.norm(cen)
order=np.argsort(-(E@cen))

for k in (50,100,len(lot),200):
    p=sum(1 for i in order[:k] if i in lot)/k
    print(f"  precision@{k:<4} vs corrected lottery = {p:.3f}")
rec=sum(1 for i in order[:len(lot)] if i in lot)/len(lot)
print(f"  recall@{len(lot)} = {rec:.3f}")
# where does the first non-lottery appear?
first=next(r for r,i in enumerate(order) if i not in lot)
print(f"  first non-lottery item appears at rank {first+1}: {titles[order[first]][:50]}")
