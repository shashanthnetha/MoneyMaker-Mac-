import re
from .db import norm_title

TAXONOMY_FACT_RE = re.compile(
    r"\b(belongs? to|is a species of|is a member of|order|family|genus|"
    r"subspecies|taxonomy|classified|named after|derives? from|etymology)\b",
    re.I,
)

def clean_title(value):
    value=(value or '').replace('‑','-').replace('‐','-').replace('‒','-').replace('’',"'").replace('‘',"'").replace('“','"').replace('”','"').replace('\xa0',' ').replace('\u202f',' ')
    value=re.sub(r'[【】「」『』\[\]]','',value); value=value.splitlines()[0] if value.splitlines() else ''
    return re.sub(r'\s+',' ',value).strip('"#*- \t\r\n')

def _name_pattern(name):
    words=name.split(); patterns=[re.escape(name)]
    if len(words)>1: patterns.append(re.escape(words[-1]))
    return r'(?:'+'|'.join(patterns)+r")(?:s|es|'s)?"

def valid_title(title,name,extinct=False):
    if not 15<=len(title)<=95:return False
    if not re.search(r'(?<!\w)'+_name_pattern(name)+r'(?!\w)',title,re.I):return False
    if extinct and not re.search(r'(?<!\w)(?:extinct|prehistoric|ancient|lost|last|fossil|ice\s+age|dinosaur)(?!\w)',title,re.I):return False
    return True

def parse_candidates(obj):
    if not isinstance(obj,dict) or not isinstance(obj.get('candidates'),list): return []
    return [x for x in obj['candidates'] if isinstance(x,dict)]

def is_taxonomy_fact(fact):
    return bool(TAXONOMY_FACT_RE.search(fact or ""))

def choose_by_interest(candidates):
    def score(candidate):
        value = candidate.get("interest", 0)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    return max(candidates, key=score) if candidates else None
