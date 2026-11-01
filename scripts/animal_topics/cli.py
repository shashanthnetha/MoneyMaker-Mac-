import argparse, os, sqlite3, sys
from .db import connect, export, init_schema, restore
from .discovery import discover
from .enrich import enrich
from .migrate import migrate
from .pipeline import pick_for_connection
from .stats import stats

def main(argv=None):
    parser=argparse.ArgumentParser(); parser.add_argument('--db',default='scripts/animal_topics.db'); parser.add_argument('--sql',default='scripts/animal_topics.sql'); sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('restore'); sub.add_parser('export'); d=sub.add_parser('discover'); d.add_argument('--max-new',type=int,required=True); e=sub.add_parser('enrich'); e.add_argument('--batch',type=int,required=True); sub.add_parser('stats'); m=sub.add_parser('migrate'); m.add_argument('--force',action='store_true'); dr=sub.add_parser('dry-run'); dr.add_argument('--count',type=int,required=True)
    g=sub.add_parser('mark-generated'); g.add_argument('--topic-id',type=int,required=True); g.add_argument('--task-id',required=True); g.add_argument('--video-file',required=True)
    p=sub.add_parser('mark-published'); p.add_argument('--topic-id',type=int,required=True); p.add_argument('--youtube-id')
    f=sub.add_parser('mark-failed'); f.add_argument('--topic-id',type=int,required=True)
    args=parser.parse_args(argv)
    if args.command=='restore': restore(args.db,args.sql); return
    if args.command=='export': export(args.db,args.sql); return
    conn=connect(args.db); init_schema(conn)
    try:
        if args.command=='discover': discover(conn,args.max_new,'scripts/animal_wishlist.txt')
        elif args.command=='enrich': enrich(conn,args.batch)
        elif args.command=='stats': stats(conn)
        elif args.command=='migrate': migrate(conn,force=args.force)
        elif args.command=='mark-generated':
            with conn:
                row=conn.execute("UPDATE topics SET status='generated',updated_at=datetime('now') WHERE id=? AND status='reserved'",(args.topic_id,))
                if row.rowcount:
                    conn.execute("INSERT INTO videos(topic_id,task_id,video_file,status,created_at,updated_at) VALUES(?,?,?,'generated',datetime('now'),datetime('now'))",(args.topic_id,args.task_id,args.video_file))
        elif args.command=='mark-published':
            with conn:
                conn.execute("UPDATE topics SET status='published',updated_at=datetime('now') WHERE id=? AND status='generated'",(args.topic_id,))
                conn.execute("UPDATE videos SET status='published',youtube_video_id=COALESCE(?,youtube_video_id),updated_at=datetime('now') WHERE id=(SELECT id FROM videos WHERE topic_id=? ORDER BY id DESC LIMIT 1)",(args.youtube_id,args.topic_id))
        elif args.command=='mark-failed':
            conn.execute("UPDATE topics SET status='failed',updated_at=datetime('now') WHERE id=? AND status='reserved'",(args.topic_id,)); conn.commit()
        elif args.command=='dry-run':
            memory=sqlite3.connect(':memory:'); conn.backup(memory); memory.row_factory=sqlite3.Row
            print('| # | Species | Extinct | Groups | Fact | Title | Source |', flush=True); print('|---|---|---|---|---|---|', flush=True)
            dryenv=dict(os.environ); dryenv.pop('ANIMAL_TOPIC_ID_FILE',None)
            for i in range(args.count):
                try:
                    title=pick_for_connection(memory,dryenv); topic=memory.execute('SELECT * FROM topics ORDER BY id DESC LIMIT 1').fetchone(); species=memory.execute('SELECT * FROM species WHERE id=?',(topic['species_id'],)).fetchone() if topic['species_id'] else None
                    groups=', '.join(x['name'] for x in memory.execute('SELECT g.name FROM groups g JOIN species_groups sg ON sg.group_id=g.id WHERE sg.species_id=?',(species['id'],))) if species else ''
                    fact=memory.execute('SELECT fact_text, fact_key FROM facts WHERE id=?',(topic['fact_id'],)).fetchone() if topic['fact_id'] else None
                    source = 'fallback' if fact and fact['fact_key'].startswith('template-') else 'AI'
                    print(f"| {i+1} | {species['common_name'] if species else ''} | {'Yes' if species and species['is_extinct'] else 'No'} | {groups} | {fact['fact_text'] if fact else ''} | {title} | {source} |", flush=True)
                except Exception as exc:
                    print(f'dry-run stopped: {exc}',file=sys.stderr); break
            memory.close()
    finally: conn.close()
if __name__=='__main__': main()
