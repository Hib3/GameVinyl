import argparse, os, sys
from pathlib import Path
import refresh_site_data, catalog_search, audit_site

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--search-limit',type=int,choices=range(1,81),default=80)
    args=ap.parse_args()
    code=Path(__file__).resolve().parent
    root=code.parent
    site=Path(os.environ.get('GRT_DATA_DIR') or root/'data')/'site'
    db=site/'vgm_site.sqlite3'
    refresh_site_data.run(db,1,True)
    try:
        contact=(site/'musicbrainz_contact.txt').read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError):
        contact=None
    result=catalog_search.run(db,args.search_limit,contact=contact)
    audit_site.run(db,Path(os.environ.get('GRT_DATA_DIR') or root/'data')/'grt.db',
                   code/'site_template.html',site/'index.html',site/'edition_audit.csv',
                   catalog_result=result)
    return 0 if result['status'] in ('ok','skipped','cooldown') else 1

if __name__=='__main__':
    sys.exit(main())
