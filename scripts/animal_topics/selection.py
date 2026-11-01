import datetime, random, sys
from .stats import performance_multipliers

def select_species(conn,forced=None,rng=random,exclude=()):
    rows=list(conn.execute("SELECT DISTINCT s.* FROM species s WHERE s.status='active' AND s.enrichment_status='done' AND s.wiki_text IS NOT NULL AND (s.footage_score IS NULL OR s.footage_score>=15)"))
    excluded=set(exclude); rows=[r for r in rows if r['id'] not in excluded]
    if forced:
        group=conn.execute('SELECT id FROM groups WHERE lower(name)=lower(?)',(forced,)).fetchone(); filtered=[r for r in rows if group and conn.execute('SELECT 1 FROM species_groups WHERE species_id=? AND group_id=?',(r['id'],group['id'])).fetchone()]
        if filtered: rows=filtered
        else: print(f'unknown or empty category: {forced}',file=sys.stderr)
    recent=conn.execute('SELECT s.is_extinct FROM topics t JOIN species s ON s.id=t.species_id WHERE t.species_id IS NOT NULL ORDER BY t.id DESC LIMIT 20').fetchall()
    if sum(r['is_extinct'] for r in recent)>=4:
        living=[r for r in rows if not r['is_extinct']]
        if living: rows=living
    cutoff=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=60)).isoformat()
    cooled=[r for r in rows if not r['last_used_at'] or r['last_used_at']<cutoff]
    if cooled: rows=cooled
    if not rows:return None
    minimum=min(r['use_count'] for r in rows); rows=[r for r in rows if r['use_count']==minimum]
    popularity=[r['popularity'] or 0 for r in rows]; low,high=min(popularity),max(popularity); perf=performance_multipliers(conn)
    class_recent=conn.execute("SELECT sg.group_id FROM topics t JOIN species_groups sg ON sg.species_id=t.species_id JOIN groups g ON g.id=sg.group_id WHERE g.kind='class' ORDER BY t.id DESC LIMIT 12").fetchall(); recent_ids=[x['group_id'] for x in class_recent]
    weights=[]
    for r in rows:
        classes=[x['group_id'] for x in conn.execute("SELECT sg.group_id FROM species_groups sg JOIN groups g ON g.id=sg.group_id WHERE sg.species_id=? AND g.kind='class'",(r['id'],))]; n=sum(recent_ids.count(x) for x in classes); norm=.5 if high==low else ((r['popularity'] or 0)-low)/(high-low)
        weights.append((.8+.2*norm)/(1+3*n)*r['boost']*perf.get(r['id'],1.0))
    return rng.choices(rows,weights=weights,k=1)[0]
