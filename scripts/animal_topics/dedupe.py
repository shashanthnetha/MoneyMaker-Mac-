import difflib, os, sys
from .db import norm_title

def _jaccard(a,b):
    a=set(a.lower().split()); b=set(b.lower().split()); return len(a&b)/len(a|b) if a|b else 1.0

def survivors(conn,species_id,candidates,judge=None):
    topics=conn.execute('SELECT title_norm FROM topics ORDER BY id DESC LIMIT 400').fetchall(); facts=conn.execute('SELECT fact_key,fact_text FROM facts WHERE species_id=?',(species_id,)).fetchall(); accepted=[]
    for raw in candidates:
        candidate=dict(raw); candidate['title_norm']=norm_title(candidate.get('title',''))
        reason=None
        if not candidate['title_norm']: reason='empty title'
        elif conn.execute('SELECT 1 FROM topics WHERE title_norm=?',(candidate['title_norm'],)).fetchone(): reason='exact duplicate'
        elif any(difflib.SequenceMatcher(None,candidate['title_norm'],row['title_norm']).ratio()>=.85 for row in topics): reason='fuzzy duplicate'
        elif any(_jaccard(candidate.get('fact_key',''),row['fact_key'])>=.5 for row in facts): reason='fact-key duplicate'
        elif any(candidate['title_norm']==row['title_norm'] or difflib.SequenceMatcher(None,candidate['title_norm'],row['title_norm']).ratio()>=.85 or _jaccard(candidate.get('fact_key',''),row.get('fact_key',''))>=.5 for row in accepted): reason='in-batch duplicate'
        if reason: print(f'rejected candidate: {reason}',file=sys.stderr); continue
        accepted.append(candidate)
    if accepted and facts:
        judge=judge or _default_judge
        try:
            result=judge(facts,accepted)
            duplicates={int(i)-1 for i in result.get('duplicates',[])} if isinstance(result,dict) else set()
            accepted=[x for i,x in enumerate(accepted) if i not in duplicates]
        except Exception as exc: print(f'candidate judge skipped: {exc}',file=sys.stderr)
    return accepted

def _default_judge(facts,candidates):
    from .groq import call
    stored='\n'.join(f"- {x['fact_text']}" for x in facts)
    numbered='\n'.join(f"{i+1}. {x.get('fact','')}" for i,x in enumerate(candidates))
    result = call(
        f'ALREADY STORED FACTS:\n{stored}\nSURVIVOR FACTS:\n{numbered}\nReturn JSON {{"duplicates":[indices]}} listing survivor indices that repeat any stored or each other fact.',
        return_metadata=True,
        model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
    )
    if isinstance(result, dict) and result.get("error") == "quota_exhausted":
        return {"_groq_quota_exhausted": True}
    return result.get("result") if isinstance(result, dict) else {}
