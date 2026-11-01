import os,re,sys
from .db import connect,restore,now_utc,norm_title,fmt_id
from .selection import select_species
from .groq import call
from .candidates import clean_title,valid_title,parse_candidates,is_taxonomy_fact,choose_by_interest
from .dedupe import survivors

def match_species(conn,title):
    best=None
    for row in conn.execute('SELECT * FROM species'):
        pattern=r'(?<!\w)'+re.escape(row['common_name'])+r"(?:s|es|'s)?(?!\w)"
        alt=r'(?<!\w)'+re.escape(row['common_name'].split()[-1])+r"(?:s|es|'s)?(?!\w)" if len(row['common_name'].split())>1 else None
        if re.search(pattern,title,re.I) or (alt and re.search(alt,title,re.I)):
            if best is None or len(row['common_name'])>len(best['common_name']): best=row
    return best

def candidate_prompt(conn,species,extra_facts=()):
    stored=[r['fact_text'] for r in conn.execute('SELECT fact_text FROM facts WHERE species_id=? ORDER BY id',(species['id'],))]; stored.extend(extra_facts)
    facts='\n'.join('- '+x for x in stored) if stored else 'None'
    return f'''Animal: {species["common_name"]}
Extinct: {bool(species["is_extinct"])}
SOURCE TEXT:
{species["wiki_text"]}
ALREADY COVERED FACTS (do not repeat or rephrase):
{facts}
1. Return JSON {{"candidates":[{{"fact":"...","fact_key":"...","title":"...","interest":N}}, ...]}} with exactly 8 candidates.
2. Every fact must be directly supported by the SOURCE TEXT. Do not use any number that does not appear in the SOURCE TEXT. Do not invent abilities.
3. Prefer facts about abilities, behaviour, hunting, survival, senses, intelligence, reproduction, extreme records or strange biology. Do NOT write about classification, naming/etymology, taxonomic relatives, geographic range or distribution, or plain size.
4. fact = one sentence. fact_key = 3–6 lowercase words naming the core fact. interest = an integer 1–10: how surprising and fun this is for a general audience.
5. title = 7–12 words, Title Case, written as a curiosity hook (for example "Why the X can ...", "How the X ...", "The X That ..."), states or clearly hints at the fact, contains the animal's name, no emojis, no quotes, no markdown.
6. If the animal is extinct, the title must contain one of: extinct, prehistoric, ancient, lost, last, fossil, "ice age", dinosaur; and the fact sentence should mention a living relative if the source text names one.'''

def _pick_on_conn(conn,env):
    override=env.get('MANUAL_TOPIC_OVERRIDE','').strip(); species=None; fact=None; manual=0
    if override:
        title=override; species=match_species(conn,title); manual=1
    else:
        failed=[]
        for _ in range(5):
            species=select_species(conn,env.get('TOPIC_CATEGORY') or None,exclude=failed)
            if not species: break
            covered=[]
            for _ in range(3):
                result=call(candidate_prompt(conn,species,covered)); candidates=[]
                for item in parse_candidates(result):
                    if is_taxonomy_fact(item.get('fact')):
                        continue
                    title=clean_title(item.get('title',''))
                    if valid_title(title,species['common_name'],bool(species['is_extinct'])):
                        item['title']=title; candidates.append(item)
                accepted=survivors(conn,species['id'],candidates)
                if accepted:
                    fact=choose_by_interest(accepted)
                    print(f"chosen candidate interest: {fact.get('interest', 0) if isinstance(fact.get('interest', 0), (int, float)) else 0}", file=sys.stderr)
                    title=fact['title']; break
                covered.extend(x.get('fact','') for x in candidates)
            if fact: break
            failed.append(species['id'])
        if not fact:
            species=select_species(conn,env.get('TOPIC_CATEGORY') or None)
            if not species: raise RuntimeError('no eligible animal species')
            number=conn.execute("SELECT count(*) FROM facts WHERE species_id=? AND fact_key LIKE 'template-%'",(species['id'],)).fetchone()[0]+1
            title=f'Surprising facts about the {"extinct " if species["is_extinct"] else ""}{species["common_name"]} you may not know'; fact={'fact':title,'fact_key':f'template-{number}'}
    timestamp=now_utc(); fact_id=None
    with conn:
        if species and fact:
            conn.execute('INSERT INTO facts(species_id,fact_text,fact_key,created_at) VALUES(?,?,?,?)',(species['id'],fact.get('fact',title),fact['fact_key'],timestamp)); fact_id=conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute('INSERT INTO topics(species_id,fact_id,title,title_norm,status,manual,run_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',(species['id'] if species else None,fact_id,title,norm_title(title),'reserved',manual,env.get('GITHUB_RUN_ID'),timestamp,timestamp)); topic_id=conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        if species: conn.execute('UPDATE species SET use_count=use_count+1,last_used_at=? WHERE id=?',(timestamp,species['id']))
    if env.get('ANIMAL_TOPIC_ID_FILE'):
        with open(env['ANIMAL_TOPIC_ID_FILE'],'w',encoding='utf-8') as f:f.write(str(topic_id))
    print(f'{fmt_id("TOP",topic_id,6)}'+(f' {fmt_id("SPC",species["id"],6)} {species["common_name"]}' if species else ''),file=sys.stderr)
    return title

def pick_for_connection(conn,env): return _pick_on_conn(conn,env)
def pick_for_workflow(env):
    db=env.get('ANIMAL_DB_PATH','scripts/animal_topics.db'); sql=env.get('ANIMAL_SQL_PATH','scripts/animal_topics.sql')
    if not os.path.exists(db): restore(db,sql)
    conn=connect(db)
    try:return _pick_on_conn(conn,env)
    finally:conn.close()
